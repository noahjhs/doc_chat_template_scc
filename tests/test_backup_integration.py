"""Peer backup end to end with REAL code on both sides: a real
casper_service (uvicorn subprocess) and two real daemons
(agent/cmd/casper-testdaemon -- the real HTTP server, command handler and
backup store, headless, with an in-memory key store instead of the
Keychain and no relay). Drives the whole scenario through the real MCP
endpoint the way an agent would, then checks the bytes on disk: nothing
readable on the peer, an identical restore on the owner.

Skipped when Go isn't available (it builds the test daemon)."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="needs Go to build the test daemon")

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "mcp-protocol-version": "2025-06-18"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url, timeout=1)
            return
        except requests.RequestException:
            time.sleep(0.1)
    raise RuntimeError(f"{url} never came up")


@pytest.fixture()
def world(tmp_path):
    procs = []
    daemon_bin = tmp_path / "casper-testdaemon"
    subprocess.run(["go", "build", "-o", str(daemon_bin), "./cmd/casper-testdaemon"], cwd=ROOT / "agent", check=True)

    port = _free_port()
    env = {**os.environ, "AUTH_DB_PATH": str(tmp_path / "users.db"), "STORAGE_ROOT": str(tmp_path / "storage")}
    env.pop("TELEGRAM_BOT_TOKEN", None)
    procs.append(
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT / "casper_service", env=env,
        )
    )
    domain = f"127.0.0.1:{port}"
    base = f"http://{domain}"
    _wait_http(base + "/docs")

    def person(name: str, host: str):
        token = requests.post(base + "/signup", json={"username": name, "password": "correct-horse"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        pair = requests.post(base + "/hosts/pair", json={"routing_key": f"rk-{host}", "hostname": host}, headers=headers).json()
        home = tmp_path / name / "home"
        home.mkdir(parents=True)
        dport = _free_port()
        p = subprocess.Popen(
            [str(daemon_bin), "-port", str(dport), "-auth-domain", domain, "-username", name,
             "-device-token", pair["device_token"], "-command-key", pair["command_key"],
             "-home", str(home), "-data", str(tmp_path / name / "data")],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        procs.append(p)
        assert p.stdout.readline().strip() == "ready"
        requests.post(
            base + "/hosts/presence", json={"local_agent_url": f"http://127.0.0.1:{dport}", "cwd": str(home)},
            headers={"Authorization": f"Bearer {pair['device_token']}"},
        )
        return {"headers": headers, "home": home, "data": tmp_path / name / "data"}

    yield base, person
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait(timeout=10)


def _mcp(base, agent_token, tool, **arguments):
    r = requests.post(
        base + "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        headers={**MCP_HEADERS, "Authorization": f"Bearer {agent_token}"},
    )
    result = r.json()["result"]
    assert not result.get("isError"), result
    if "structuredContent" in result and "result" in result["structuredContent"]:
        return result["structuredContent"]["result"]
    return "\n".join(c.get("text", "") for c in result["content"])


def _approve_only(base, headers, kind):
    pending = requests.get(base + "/conversations/pending-approvals", headers=headers).json()["pending_approvals"]
    [p] = [p for p in pending if p["kind"] == kind]
    r = requests.post(base + f"/conversations/pending-approvals/{p['id']}/decide", json={"decision": "allow"}, headers=headers)
    assert r.status_code == 200, r.text
    return p


def _wait_for(fn, what, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def test_peer_backup_real_daemons(world):
    base, person = world
    sam = person("sam", "sam-mini")
    riley = person("riley", "riley-laptop")

    # Something worth backing up, bigger than one 4MB chunk.
    taxes = riley["home"] / "taxes"
    (taxes / "2025").mkdir(parents=True)
    (taxes / "notes.txt").write_text("receipts are in the blue folder")
    big = os.urandom(5 * 1024 * 1024)
    (taxes / "2025" / "return.pdf").write_bytes(big)

    requests.post(base + "/friends/requests", json={"username": "sam"}, headers=riley["headers"])
    _approve_only(base, sam["headers"], "friend_request")
    sam_host = next(h["host_id"] for h in requests.get(base + "/hosts", headers=sam["headers"]).json()["hosts"])
    requests.post(base + "/offerings", json={"host_id": sam_host, "max_quota_gb": 1, "write_tier": "ask"}, headers=sam["headers"])

    agent = requests.post(base + "/agent-tokens", json={"name": "claude"}, headers=riley["headers"]).json()["token"]
    [offer] = _mcp(base, agent, "list_offerings")
    _mcp(base, agent, "request_access", offering_id=offer["id"], quota_gb=0.5)
    _approve_only(base, sam["headers"], "access_request")

    out = _mcp(base, agent, "backup_push", source_host="riley-laptop", path="taxes", dest_host="sam/sam-mini", preview=False)
    assert "waiting for sam to approve" in out, out
    ask = _approve_only(base, sam["headers"], "backup_write")
    assert "taxes" not in ask["description"]

    status = _wait_for(
        lambda: (s := json.loads(_mcp(base, agent, "backup_status"))[0])["status"] in ("complete", "failed") and s,
        "the backup to finish",
    )
    assert status["status"] == "complete", status

    # The peer holds only ciphertext: no name, no content, anywhere on disk.
    stored = [p for p in (sam["data"]).rglob("*") if p.is_file()]
    assert stored
    for p in stored:
        data = p.read_bytes()
        for secret in (b"taxes", b"notes.txt", b"receipts", big[:64]):
            assert secret not in data, f"{p} leaks {secret[:20]!r}"

    _mcp(base, agent, "backup_restore", backup_id=status["backup_id"], dest_host="riley-laptop")
    restored = _wait_for(
        lambda: (s := json.loads(_mcp(base, agent, "backup_status", backup_id=status["backup_id"]))).get("restore", "").startswith(("restored", "failed")) and s,
        "the restore to finish",
    )
    assert restored["restore"].startswith("restored"), restored
    [folder] = (riley["home"] / "Casper Restores").iterdir()
    assert folder.name.startswith("taxes-")
    assert (folder / "notes.txt").read_text() == "receipts are in the blue folder"
    assert (folder / "2025" / "return.pdf").read_bytes() == big

    # Revocation: writes stop at once.
    grant_id = requests.get(base + "/grants", headers=sam["headers"]).json()["given"][0]["id"]
    requests.delete(base + f"/grants/{grant_id}", headers=sam["headers"])
    assert "don't have backup space" in _mcp(base, agent, "backup_push", source_host="riley-laptop", path="taxes", dest_host="sam/sam-mini", preview=False)
