"""Mirroring with history, end to end, with REAL code everywhere: a uvicorn
casper_service and four headless daemons (agent/cmd/casper-testdaemon), each
running its own real Syncthing. Driven through MCP as agents would:

  Riley mirrors Documents to Sam (mirror space) with Cat as catcher.
  Riley confirms outside the agent (the native-dialog endpoint); the agent
  itself can't. Sam and Cat hold only ciphertext. An old version comes back.
  Sam sleeps; a new change is caught by Cat. Riley's Mac is then lost; Sam
  wakes; a replacement Mac with Riley's "recovery kit" rebuilds Documents
  from Sam plus Cat's gap -- including the change Sam never saw.

Skipped without Go or a syncthing binary ($CASPER_TEST_SYNCTHING, or
~/st-spike/bin/syncthing)."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
SYNCTHING = os.environ.get("CASPER_TEST_SYNCTHING", str(Path.home() / "st-spike" / "bin" / "syncthing"))
pytestmark = pytest.mark.skipif(
    shutil.which("go") is None or not Path(SYNCTHING).exists(), reason="needs Go and a syncthing binary"
)
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "mcp-protocol-version": "2025-06-18"}


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(fn, what, timeout=120, every=0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


class World:
    def __init__(self, tmp: Path):
        self.tmp, self.procs = tmp, {}
        self.bin = tmp / "casper-testdaemon"
        subprocess.run(["go", "build", "-o", str(self.bin), "./cmd/casper-testdaemon"], cwd=ROOT / "agent", check=True)
        port = _port()
        env = {**os.environ, "AUTH_DB_PATH": str(tmp / "users.db"), "STORAGE_ROOT": str(tmp / "storage")}
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("CATCHER_URL", None)
        self.svc_log = tmp / "casper_service.log"
        self.svc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(port), "--log-level", "warning"],
                                    cwd=ROOT / "casper_service", env=env, stderr=open(self.svc_log, "w"))
        self.domain, self.base = f"127.0.0.1:{port}", f"http://127.0.0.1:{port}"
        _wait(lambda: self._up(), "casper_service")
        self.people = {}

    def _up(self):
        try:
            return requests.get(self.base + "/docs", timeout=1).status_code == 200
        except requests.RequestException:
            return False

    def person(self, name):
        token = requests.post(self.base + "/signup", json={"username": name, "password": "correct-horse"}).json()["token"]
        agent = requests.post(self.base + "/agent-tokens", json={"name": "agent"}, headers={"Authorization": f"Bearer {token}"}).json()["token"]
        self.people[name] = {"token": token, "agent": agent}
        return self.people[name]

    def host(self, person, hostname, data_from=None):
        p = self.people[person]
        pair = requests.post(self.base + "/hosts/pair", json={"routing_key": f"rk-{hostname}", "hostname": hostname},
                             headers={"Authorization": f"Bearer {p['token']}"}).json()
        home = self.tmp / hostname / "home"
        data = self.tmp / hostname / "data"
        home.mkdir(parents=True)
        data.mkdir(parents=True)
        if data_from:  # the "recovery kit": folder passwords from the lost Mac
            shutil.copy(self.tmp / data_from / "data" / "mirror-passwords.json", data / "mirror-passwords.json")
        h = {"pair": pair, "home": home, "data": data, "http": _port(), "st": _port(), "person": person, "name": hostname}
        self.start(h)
        return h

    def start(self, h):
        p = subprocess.Popen(
            [str(self.bin), "-port", str(h["http"]), "-auth-domain", self.domain, "-username", h["person"],
             "-device-token", h["pair"]["device_token"], "-command-key", h["pair"]["command_key"],
             "-home", str(h["home"]), "-data", str(h["data"]), "-syncthing", SYNCTHING, "-st-listen", f"tcp://127.0.0.1:{h['st']}"],
            stdout=subprocess.PIPE, stderr=open(h["data"] / "daemon.log", "a"), text=True,
        )
        assert p.stdout.readline().strip() == "ready"
        self.procs[h["name"]] = p
        requests.post(self.base + "/hosts/presence", json={"local_agent_url": f"http://127.0.0.1:{h['http']}", "cwd": str(h["home"])},
                      headers={"Authorization": f"Bearer {h['pair']['device_token']}"})

    def stop(self, h):
        p = self.procs.pop(h["name"])
        p.terminate()
        p.wait(timeout=20)
        requests.delete(self.base + "/hosts/presence", headers={"Authorization": f"Bearer {h['pair']['device_token']}"})

    def mcp(self, person, tool, **args):
        r = requests.post(self.base + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
                          headers={**MCP_HEADERS, "Authorization": f"Bearer {self.people[person]['agent']}"}, timeout=120)
        res = r.json()["result"]
        if res.get("isError"):
            full = self.svc_log.read_text() if self.svc_log.exists() else ""
            Path("/tmp/casper_service_last_failure.log").write_text(full)
            tail = full[-4000:]
            raise AssertionError(f"{tool} failed: {res}\n--- casper_service log ---\n{tail}")
        if "structuredContent" in res and "result" in res["structuredContent"]:
            return res["structuredContent"]["result"]
        return "\n".join(c.get("text", "") for c in res["content"])

    def close(self):
        for p in list(self.procs.values()):
            p.terminate()
        for p in list(self.procs.values()):
            p.wait(timeout=20)
        self.svc.terminate()
        self.svc.wait(timeout=20)


@pytest.fixture()
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def _leaks(root: Path, *needles: bytes) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            if any(n in data or n.decode(errors="ignore") in p.name for n in needles if n):
                hits.append(str(p))
    return hits


def test_mirroring_end_to_end(world):
    for name in ("riley", "sam", "cat"):
        world.person(name)
    riley = world.host("riley", "riley-mac")
    sam = world.host("sam", "sam-mac")
    world.host("cat", "cat-server")

    # Sam gives mirror space; Cat gives catcher space; Riley takes both.
    assert "PREVIEW" in world.mcp("sam", "publish_offering", host="sam-mac", max_gb=1)
    world.mcp("sam", "publish_offering", host="sam-mac", max_gb=1, preview=False)
    world.mcp("cat", "publish_offering", host="cat-server", max_gb=1, kind="catcher", preview=False)
    for giver in ("sam", "cat"):
        out = world.mcp(giver, "create_invite", quota_gb=0.5, for_whom="Riley", preview=False)
        code = re.search(r"CASPER-[A-Z2-9-]{14}", out).group(0)
        done = world.mcp("riley", "redeem_invite", code=code, preview=False)
        assert "Done" in done
    assert "offer sam mirror space in return" in done or "catch your changes" in done

    docs = riley["home"] / "Documents"
    (docs / "taxes").mkdir(parents=True)
    (docs / "notes.txt").write_text("receipts are in the blue folder")
    big = os.urandom(3 << 20)
    (docs / "taxes" / "return.pdf").write_bytes(big)

    plan = world.mcp("riley", "mirror_folder", source_host="riley-mac", path="Documents", mirrors=["sam/sam-mac"], catcher="cat/cat-server")
    assert plan.startswith("PREVIEW") and "can never read it" in plan
    out = world.mcp("riley", "mirror_folder", source_host="riley-mac", path="Documents", mirrors=["sam/sam-mac"], catcher="cat/cat-server", preview=False)
    assert "can't confirm it for them" in out

    # The agent can't confirm; the person (the native dialog) can.
    pending = requests.get(world.base + "/conversations/pending-approvals", headers={"Authorization": f"Bearer {world.people['riley']['token']}"}).json()["pending_approvals"]
    [consent] = [p for p in pending if p["kind"] == "mirror_consent"]
    assert "can't be approved from their agent" in world.mcp("riley", "decide_approval", approval_id=consent["id"], approve=True)
    r = requests.post(world.base + "/hosts/consent", json={"approval_id": consent["id"], "approve": True},
                      headers={"Authorization": f"Bearer {riley['pair']['device_token']}"})
    assert r.status_code == 200

    _wait(lambda: "every change is on at least one mirror" in world.mcp("riley", "protection_status"), "the first full mirror", timeout=180)
    assert not _leaks(sam["data"] / "mirror" / "held", b"receipts", b"notes.txt", b"taxes", big[:64])

    # An accident: an edit we want to undo.
    (docs / "notes.txt").write_text("receipts are in the red folder")
    listing = _wait(lambda: (lambda t: "notes.txt" in t and t)(world.mcp("riley", "list_versions", folder="Documents", name_contains="notes")), "a stored version", timeout=120)
    at = re.search(r"notes\.txt  at=(\S+)", listing).group(1)
    restored = world.mcp("riley", "restore_version", folder="Documents", name="notes.txt", at=at)
    assert "nothing was overwritten" in restored, restored
    restored_path = Path(re.search(r" to (/.*notes\.txt)", restored).group(1))
    assert restored_path.read_text() == "receipts are in the blue folder"
    assert (docs / "notes.txt").read_text() == "receipts are in the red folder"

    # Sam's Mac sleeps; a new change is caught by Cat, not lost.
    world.stop(sam)
    (docs / "written-while-sam-slept.txt").write_text("a new idea")
    _wait(lambda: "held by the catcher" in world.mcp("riley", "protection_status"), "the catcher to hold the gap", timeout=180)
    cat_held = world.tmp / "cat-server" / "data" / "mirror" / "held"
    assert not _leaks(cat_held, b"a new idea", b"written-while")

    # Riley's Mac is lost before Sam wakes. Sam wakes. A replacement Mac,
    # with the recovery kit, rebuilds Documents from Sam + Cat's gap.
    world.stop(riley)
    world.start(sam)
    replacement = world.host("riley", "riley-new-mac", data_from="riley-mac")
    target = replacement["home"] / "Documents"
    out = world.mcp("riley", "restore_folder", folder="Documents", dest_host="riley-new-mac", dest_path=str(target))
    assert "Restoring Documents" in out, out

    def rebuilt():
        try:
            return ((target / "taxes" / "return.pdf").read_bytes() == big
                    and (target / "notes.txt").read_text() == "receipts are in the red folder"
                    and (target / "written-while-sam-slept.txt").read_text() == "a new idea")
        except OSError:
            return False

    _wait(rebuilt, "the replacement Mac to rebuild Documents, including the caught change", timeout=240)
