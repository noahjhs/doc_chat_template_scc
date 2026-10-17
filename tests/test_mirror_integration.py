"""Mirroring with history, end to end, with REAL code everywhere: a uvicorn
casper_service and four headless daemons (agent/cmd/casper-testdaemon), each
running its own real Syncthing. Driven through MCP as agents would:

  Riley mirrors Documents to Sam (mirror space) with Cat as catcher.
  Riley confirms outside the agent (the native-dialog endpoint); the agent
  itself can't. Sam and Cat hold only ciphertext. An old version comes back.
  Sam sleeps; a new change is caught by Cat. Riley's Mac is then lost; Sam
  wakes; a replacement Mac opens Riley's real recovery kit file (locked with
  a passphrase) and rebuilds Documents from Sam plus Cat's gap -- including
  the change Sam never saw -- through /hosts/restore, with no agent. It is
  then promoted to Documents' Mac: new changes on it reach Sam's mirror, and
  the lost Mac, if it turns up again, gets nothing.

Skipped without Go or a syncthing binary ($CASPER_TEST_SYNCTHING, or
~/st-spike/bin/syncthing)."""

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import sqlite3
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

    def make_kit(self, h, path, passphrase, *extra):
        """A real recovery kit file from a (stopped) Mac's keys and folder passwords."""
        subprocess.run([str(self.bin), "-auth-domain", self.domain, "-username", h["person"], "-data", str(h["data"]),
                        "-kit-out", str(path), "-kit-pass", passphrase, "-kit-only", *extra], check=True)

    def host(self, person, hostname, kit=None, kit_pass=""):
        p = self.people[person]
        pair = requests.post(self.base + "/hosts/pair", json={"routing_key": f"rk-{hostname}", "hostname": hostname},
                             headers={"Authorization": f"Bearer {p['token']}"}).json()
        home = self.tmp / hostname / "home"
        data = self.tmp / hostname / "data"
        home.mkdir(parents=True)
        data.mkdir(parents=True)
        h = {"pair": pair, "home": home, "data": data, "http": _port(), "st": _port(), "person": person, "name": hostname,
             "kit": kit, "kit_pass": kit_pass}
        self.start(h)
        return h

    def start(self, h):
        recover = ["-recover-kit", str(h["kit"]), "-kit-pass", h["kit_pass"]] if h.get("kit") else []
        p = subprocess.Popen(
            [str(self.bin), "-port", str(h["http"]), "-auth-domain", self.domain, "-username", h["person"],
             "-device-token", h["pair"]["device_token"], "-command-key", h["pair"]["command_key"],
             "-home", str(h["home"]), "-data", str(h["data"]), "-syncthing", SYNCTHING, "-st-listen", f"tcp://127.0.0.1:{h['st']}", *recover],
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
    assert "offer sam space back" in done or "is their catcher" in done

    docs = riley["home"] / "Documents"
    (docs / "taxes").mkdir(parents=True)
    (docs / "notes.txt").write_text("receipts are in the blue folder")
    big = os.urandom(3 << 20)
    (docs / "taxes" / "return.pdf").write_bytes(big)

    plan = world.mcp("riley", "mirror_folder", source_host="riley-mac", path="Documents", mirrors=["sam/sam-mac"], catcher="cat/cat-server")
    assert plan.startswith("PREVIEW") and "Encrypted, 30 days of history." in plan
    out = world.mcp("riley", "mirror_folder", source_host="riley-mac", path="Documents", mirrors=["sam/sam-mac"], catcher="cat/cat-server", preview=False)
    assert "can't confirm it for them" in out

    # The agent can't confirm; the person (the native dialog) can.
    pending = requests.get(world.base + "/conversations/pending-approvals", headers={"Authorization": f"Bearer {world.people['riley']['token']}"}).json()["pending_approvals"]
    [consent] = [p for p in pending if p["kind"] == "mirror_consent"]
    assert "can't be approved from their agent" in world.mcp("riley", "decide_approval", approval_id=consent["id"], approve=True)
    r = requests.post(world.base + "/hosts/consent", json={"approval_id": consent["id"], "approve": True},
                      headers={"Authorization": f"Bearer {riley['pair']['device_token']}"})
    assert r.status_code == 200

    _wait(lambda: "Every change is on at least one mirror" in world.mcp("riley", "protection_status"), "the first full mirror", timeout=180)
    assert not _leaks(sam["data"] / "mirror" / "held", b"receipts", b"notes.txt", b"taxes", big[:64])
    # Nor does the holder's own Syncthing know the folder's name (it used to read "riley: Documents"): finding 8.
    config = next((sam["data"] / "syncthing").rglob("config.xml")).read_text()
    assert "Documents" not in config and 'label="riley: encrypted mirror"' in config

    # An accident: an edit we want to undo.
    (docs / "notes.txt").write_text("receipts are in the red folder")
    listing = _wait(lambda: (lambda t: "notes.txt" in t and t)(world.mcp("riley", "list_versions", folder="Documents", name_contains="notes")), "a stored version", timeout=120)
    at = re.search(r"notes\.txt  at=(\S+)", listing).group(1)
    restored = world.mcp("riley", "restore_version", folder="Documents", name="notes.txt", at=at)
    assert "nothing was overwritten" in restored, restored
    restored_path = Path(re.search(r" to (/.*?\.txt) --", restored).group(1))
    assert restored_path.read_text() == "receipts are in the blue folder"
    assert restored_path.parent == docs and restored_path.name.startswith("notes (as of ")  # next to the original, dated
    assert (docs / "notes.txt").read_text() == "receipts are in the red folder"

    # Sam's Mac sleeps; a new change is caught by Cat, not lost.
    world.stop(sam)
    (docs / "written-while-sam-slept.txt").write_text("a new idea")
    _wait(lambda: "held by the catcher" in world.mcp("riley", "protection_status"), "the catcher to hold the gap", timeout=180)
    cat_held = world.tmp / "cat-server" / "data" / "mirror" / "held"
    assert not _leaks(cat_held, b"a new idea", b"written-while")

    # Riley's Mac is lost before Sam wakes. (Her kit was saved earlier, in the
    # real file format, outside any mirrored folder.) Sam wakes. A replacement
    # Mac opens the kit and rebuilds Documents from Sam + Cat's gap.
    world.stop(riley)
    kit_file = world.tmp / "Casper Recovery Kit (riley).txt"
    world.make_kit(riley, kit_file, "a long kit passphrase")
    assert b"receipts" not in kit_file.read_bytes() and b"casper-" not in kit_file.read_bytes().split(b"-----BEGIN")[1]
    world.start(sam)
    replacement = world.host("riley", "riley-new-mac", kit=kit_file, kit_pass="a long kit passphrase")
    # Riley's old home isn't this Mac's home, so Documents can't return to its original place.
    target = replacement["home"] / "Casper Restores" / "Documents"

    def rebuilt():
        try:
            return ((target / "taxes" / "return.pdf").read_bytes() == big
                    and (target / "notes.txt").read_text() == "receipts are in the red folder"
                    and (target / "written-while-sam-slept.txt").read_text() == "a new idea")
        except OSError:
            return False

    _wait(rebuilt, "the replacement Mac to rebuild Documents, including the caught change", timeout=240)

    # Promotion: the replacement becomes Documents' Mac (no agent involved).
    def owner():
        con = sqlite3.connect(world.tmp / "users.db")
        try:
            return con.execute("SELECT h.routing_key, mf.restore_host_id FROM mirror_folders mf JOIN hosts h ON h.id = mf.owner_host_id").fetchone()
        finally:
            con.close()

    _wait(lambda: owner() == ("rk-riley-new-mac", None), "the replacement Mac to be promoted", timeout=120)
    config = requests.get(world.base + "/hosts/mirror-config", headers={"Authorization": f"Bearer {replacement['pair']['device_token']}"}).json()
    [mine] = config["owned"]
    assert mine["restore"] is False and mine["path"] == str(target)

    # New changes on it reach Sam's mirror: its encrypted files there become all five (return.pdf, notes, the dated
    # copy of notes restored earlier (it lives beside the original, so it is mirrored too), the change only the
    # catcher had -- which the new Mac now sends on -- and the one written after recovery).
    def held_files():
        return [f for f in (sam["data"] / "mirror" / "held").rglob("*")
                if f.is_file() and ".stversions" not in f.parts and ".stfolder" not in f.parts]

    (target / "notes.txt").write_text("receipts are in the green folder")
    (target / "after-recovery.txt").write_text("written on the new Mac")
    _wait(lambda: len(held_files()) == 5, "the new Mac's files to reach Sam's mirror", timeout=180)
    _wait(lambda: "Every change is on at least one mirror" in world.mcp("riley", "protection_status"), "the new Mac's changes to be on a mirror", timeout=180)
    assert not _leaks(sam["data"] / "mirror" / "held", b"after-recovery", b"green folder")

    # The lost Mac turns up: it has nothing to send, and nothing it writes spreads.
    (riley["home"] / "Documents").mkdir(exist_ok=True)
    world.start(riley)
    old = requests.get(world.base + "/hosts/mirror-config", headers={"Authorization": f"Bearer {riley['pair']['device_token']}"}).json()
    assert old["owned"] == []
    (riley["home"] / "Documents" / "ghost.txt").write_text("from the replaced Mac")
    time.sleep(15)
    assert not (target / "ghost.txt").exists()
    assert "ghost" not in world.mcp("riley", "list_versions", folder="Documents", name_contains="ghost")


def test_restore_deleted_files_dated_copies_and_folders_as_of_a_time(world):
    """Restore design (docs/product/restore-design.md), with real Syncthing:
    a deleted file comes back where it was, a folder comes back as it was at
    a moment (history plus untouched files; nothing made later), and replace
    puts a version over the current file. Nothing is ever overwritten by a copy."""
    from datetime import datetime

    for name in ("riley", "sam"):
        world.person(name)
    riley = world.host("riley", "riley-mac")
    world.host("sam", "sam-mac")
    world.mcp("sam", "publish_offering", host="sam-mac", max_gb=1, preview=False)
    code = re.search(r"CASPER-[A-Z2-9-]{14}", world.mcp("sam", "create_invite", quota_gb=0.5, for_whom="Riley", preview=False)).group(0)
    assert "Done" in world.mcp("riley", "redeem_invite", code=code, preview=False)

    docs = riley["home"] / "Documents"
    (docs / "taxes").mkdir(parents=True)
    (docs / "keep.txt").write_text("keeps")
    (docs / "draft.txt").write_text("v1")
    (docs / "doomed.txt").write_text("goodbye")
    (docs / "taxes" / "return.txt").write_text("2025")
    args = dict(source_host="riley-mac", path="Documents", mirrors=["sam/sam-mac"])
    world.mcp("riley", "mirror_folder", preview=False, **args)
    pending = requests.get(world.base + "/conversations/pending-approvals", headers={"Authorization": f"Bearer {world.people['riley']['token']}"}).json()["pending_approvals"]
    [consent] = [p for p in pending if p["kind"] == "mirror_consent"]
    requests.post(world.base + "/hosts/consent", json={"approval_id": consent["id"], "approve": True}, headers={"Authorization": f"Bearer {riley['pair']['device_token']}"})
    _wait(lambda: "Every change is on at least one mirror" in world.mcp("riley", "protection_status"), "the first full mirror", timeout=180)

    time.sleep(1.5)
    moment = datetime.now().astimezone().replace(microsecond=0).isoformat()
    time.sleep(3)
    (docs / "draft.txt").write_text("v2")
    (docs / "doomed.txt").unlink()
    (docs / "later.txt").write_text("born after the moment")
    _wait(lambda: "doomed.txt" in world.mcp("riley", "list_versions", folder="Documents", deleted_only=True), "the deleted file in history", timeout=120)
    _wait(lambda: "draft.txt" in world.mcp("riley", "list_versions", folder="Documents", name_contains="draft"), "the edit in history", timeout=120)
    assert "draft.txt" not in world.mcp("riley", "list_versions", folder="Documents", deleted_only=True)  # still there, only edited

    # A deleted file comes back where it was, under a dated name.
    out = world.mcp("riley", "restore_version", folder="Documents", name="doomed")
    back = Path(re.search(r" to (/.*?) --", out).group(1))
    assert back.parent == docs and back.name.startswith("doomed (as of ") and back.read_text() == "goodbye"
    assert not (docs / "doomed.txt").exists()

    # The whole folder as it was at the moment: a new sibling folder.
    out = world.mcp("riley", "restore_as_of", folders=["Documents"], as_of=moment)
    copy = Path(re.search(r" in (/.*?) --", out).group(1))
    assert copy.parent == riley["home"] and copy.name.startswith("Documents (as of ")
    assert {p.relative_to(copy).as_posix(): p.read_text() for p in copy.rglob("*") if p.is_file()} == {
        "keep.txt": "keeps", "draft.txt": "v1", "doomed.txt": "goodbye", "taxes/return.txt": "2025"}
    assert (docs / "draft.txt").read_text() == "v2" and (docs / "later.txt").exists()  # the originals are untouched

    # Replace needs the person's yes (preview first), then puts the old version over the current file.
    assert world.mcp("riley", "restore_version", folder="Documents", name="draft", mode="replace").startswith("PREVIEW")
    assert (docs / "draft.txt").read_text() == "v2"
    done = world.mcp("riley", "restore_version", folder="Documents", name="draft", mode="replace", preview=False)
    assert "in place of the current file" in done and (docs / "draft.txt").read_text() == "v1"
    assert not list(docs.glob("*.casper-restoring"))


def _derived_password(master: bytes, folder_id: str) -> str:
    """recovery-v2-design.md 2.2: HKDF-SHA256, salt "casper-v2", info "folder-password\\0<id>" -- computed
    here independently of the Go code."""
    prk = hmac.new(b"casper-v2", master, hashlib.sha256).digest()
    okm = hmac.new(prk, b"folder-password\x00" + folder_id.encode() + b"\x01", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(okm).decode().rstrip("=")


def test_master_key_kit_covers_later_and_legacy_folders(world):
    """P2: Riley has an old folder with a random password (Documents -> legacy)
    and, after she made her kit, a new one (Photos -> derived from her master key).
    The one kit, with no copy of the old password in it, brings both back on a
    replacement Mac (Documents via the sealed bundle on casper_service). The
    server's database holds no folder password and no master key."""
    for name in ("riley", "sam"):
        world.person(name)
    riley = world.host("riley", "riley-mac")
    world.host("sam", "sam-mac")
    world.mcp("sam", "publish_offering", host="sam-mac", max_gb=1, preview=False)
    out = world.mcp("sam", "create_invite", quota_gb=0.5, for_whom="Riley", preview=False)
    assert "Done" in world.mcp("riley", "redeem_invite", code=re.search(r"CASPER-[A-Z2-9-]{14}", out).group(0), preview=False)

    def db():
        return sqlite3.connect(world.tmp / "users.db")

    def mirror(path, legacy_password=None):
        folder = riley["home"] / path
        folder.mkdir(parents=True)
        (folder / "note.txt").write_text(f"{path} is safe")
        (folder / "blob.bin").write_bytes(os.urandom(300_000))
        world.mcp("riley", "mirror_folder", source_host="riley-mac", path=path, mirrors=["sam/sam-mac"], preview=False)
        con = db()
        [(folder_id,)] = con.execute("SELECT id FROM mirror_folders WHERE label = ?", (path,)).fetchall()
        if legacy_password:  # a folder from before the master key: random password, already on her Mac
            con.execute("UPDATE mirror_folders SET key_scheme = 'legacy' WHERE id = ?", (folder_id,))
            con.commit()
            (riley["data"] / "mirror-passwords.json").write_text(json.dumps({f"{world.domain}/riley/{folder_id}": legacy_password}))
        con.close()
        pending = requests.get(world.base + "/conversations/pending-approvals", headers={"Authorization": f"Bearer {world.people['riley']['token']}"}).json()["pending_approvals"]
        [consent] = [p for p in pending if p["kind"] == "mirror_consent"]
        assert requests.post(world.base + "/hosts/consent", json={"approval_id": consent["id"], "approve": True},
                             headers={"Authorization": f"Bearer {riley['pair']['device_token']}"}).status_code == 200
        held = world.tmp / "sam-mac" / "data" / "mirror" / "held" / folder_id
        _wait(lambda: sum(f.is_file() and ".stfolder" not in f.parts for f in held.rglob("*")) >= 2, f"{path} to reach Sam's mirror", timeout=180)
        _wait(lambda: "Every change is on at least one mirror" in world.mcp("riley", "protection_status"), f"{path} to be mirrored", timeout=180)
        return folder_id

    old_password = "legacy-" + base64.urlsafe_b64encode(os.urandom(24)).decode()
    docs_id = mirror("Documents", legacy_password=old_password)

    # The master key now exists, and the old folder's password is carried -- sealed.
    master = base64.b64decode(json.loads((riley["data"] / "keystore.json").read_text())[f"{world.domain}/riley"])
    assert len(master) == 32
    con = db()
    [(blob,)] = _wait(lambda: con.execute("SELECT blob FROM legacy_bundles").fetchall(), "the legacy bundle to be stored")
    con.close()
    assert old_password not in blob and old_password.encode() not in base64.b64decode(blob)

    kit = world.tmp / "Casper Recovery Kit (riley).txt"
    world.stop(riley)
    world.make_kit(riley, kit, "a long kit passphrase", "-kit-no-bundle")
    world.start(riley)
    assert old_password.encode() not in kit.read_bytes()

    # A folder made after the kit: its password is derived, never stored anywhere.
    photos_id = mirror("Photos")
    con = db()
    assert dict(con.execute("SELECT id, key_scheme FROM mirror_folders").fetchall()) == {docs_id: "legacy", photos_id: "derived1"}
    con.close()
    assert photos_id not in (riley["data"] / "mirror-passwords.json").read_text()

    # Nothing in casper_service's database lets it open a folder.
    con = db()
    con.close()
    dump = (world.tmp / "users.db").read_bytes()
    for secret in (old_password, _derived_password(master, photos_id), _derived_password(master, docs_id),
                   base64.b64encode(master).decode(), base64.urlsafe_b64encode(master).decode(), base64.urlsafe_b64encode(master).decode().rstrip("=")):
        assert secret.encode() not in dump, "a secret reached casper_service"
    assert master not in dump

    # Riley's Mac is lost; the replacement opens the one older kit.
    world.stop(riley)
    replacement = world.host("riley", "riley-new-mac", kit=kit, kit_pass="a long kit passphrase")
    for label in ("Documents", "Photos"):
        target = replacement["home"] / "Casper Restores" / label

        def back(target=target, label=label):
            try:
                return (target / "note.txt").read_text() == f"{label} is safe" and (target / "blob.bin").stat().st_size == 300_000
            except OSError:
                return False

        _wait(back, f"{label} to come back from the kit made before Photos existed", timeout=240)

    def owners():
        con = db()
        try:
            return dict(con.execute("SELECT mf.label, h.routing_key FROM mirror_folders mf JOIN hosts h ON h.id = mf.owner_host_id").fetchall())
        finally:
            con.close()

    _wait(lambda: owners() == {"Documents": "rk-riley-new-mac", "Photos": "rk-riley-new-mac"}, "both folders to be promoted", timeout=120)
    # The replacement derives Photos' password from the key in the kit -- the same one the first Mac used.
    new_master = base64.b64decode(json.loads((replacement["data"] / "keystore.json").read_text())[f"{world.domain}/riley"])
    assert new_master == master
