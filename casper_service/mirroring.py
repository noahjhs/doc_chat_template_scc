"""Mirroring with history (docs/product/scenarios/mirroring.md): the
casper_service half.

casper_service never moves a byte of mirrored data -- Syncthing does that
directly between peers. Its jobs:
  - discovery: each Mac's Syncthing device ID and addresses;
  - computing each Mac's *desired state* from the trust framework (which
    folders it owns and mirrors out, which it holds for whom, which peers it
    may talk to) -- a Mac only ever receives peers and folders an active
    grant on its own host allows;
  - consent (once per arrangement, outside the agent), protection status,
    nudges, version listing/restore and disaster-restore orchestration.

Plain functions over the db; main.py wires HTTP, MCP and notifications."""

import json
import os
import secrets
import threading

import requests
import trust
from db import get_db

NUDGE_AFTER_SECONDS = 6 * 3600
NUDGE_EVERY_SECONDS = 12 * 3600


class MirrorError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- Discovery ---------------------------------------------------------------------------
def report_device(db, routing_key: str, device_id: str, addresses: list[str]):
    db.execute(
        """
        INSERT INTO mirror_devices (routing_key, device_id, addresses, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(routing_key) DO UPDATE SET device_id = excluded.device_id, addresses = excluded.addresses, updated_at = excluded.updated_at
        """,
        (routing_key, device_id, json.dumps(addresses[:32]), trust.now_iso()),
    )


def _device_for_host(db, host_id: int) -> dict | None:
    row = db.execute(
        "SELECT md.device_id, md.addresses, h.hostname FROM hosts h JOIN mirror_devices md ON md.routing_key = h.routing_key WHERE h.id = ?",
        (host_id,),
    ).fetchone()
    if row is None:
        return None
    return {"device_id": row["device_id"], "addresses": json.loads(row["addresses"]), "name": row["hostname"] or f"host{host_id}"}


# --- Casper's own catcher (the catcher of last resort) ----------------------------------
def catcher_available() -> bool:
    return bool(os.environ.get("CATCHER_URL"))


_catcher_device_id: dict = {}


def _catcher_api(method: str, path: str, body=None):
    r = requests.request(
        method, os.environ["CATCHER_URL"] + path, json=body,
        headers={"X-API-Key": os.environ.get("CATCHER_API_KEY", "")}, timeout=20,
    )
    r.raise_for_status()
    return r.json() if r.content else None


def catcher_device() -> dict | None:
    """Casper's catcher as a peer: its device ID and the addresses owners'
    Macs can reach it on (CATCHER_ADDRESSES)."""
    if not catcher_available():
        return None
    if "id" not in _catcher_device_id:
        try:
            _catcher_device_id["id"] = _catcher_api("GET", "/rest/system/status")["myID"]
        except requests.RequestException:
            return None
    addrs = [a.strip() for a in os.environ.get("CATCHER_ADDRESSES", "").split(",") if a.strip()]
    return {"device_id": _catcher_device_id["id"], "addresses": addrs, "name": "casper-catcher"}


def reconcile_catcher():
    """Make Casper's catcher hold exactly the staging folders of mirrored
    folders that use it, encrypted, with no history -- and nothing else."""
    if catcher_device() is None:
        return
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM mirror_folders WHERE status = 'active' AND use_casper_catcher = 1"
        ).fetchall()
        wanted_devices, wanted_folders = {}, {}
        for f in rows:
            owners = [d for d in (_device_for_host(db, f["owner_host_id"]), _device_for_host(db, f["restore_host_id"]) if f["restore_host_id"] else None) if d]
            if not owners:
                continue
            for d in owners:
                wanted_devices[d["device_id"]] = {"deviceID": d["device_id"], "name": "casper:" + d["name"], "addresses": d["addresses"] + ["dynamic"]}
            fid = f["id"] + ".catch"
            wanted_folders[fid] = {
                "id": fid, "label": fid, "path": f"/var/syncthing/held/{fid}", "type": "receiveencrypted",
                "devices": [{"deviceID": d["device_id"], "encryptionPassword": ""} for d in owners],
                "fsWatcherEnabled": False, "rescanIntervalS": 3600,
                "versioning": {"type": "", "params": {}, "cleanupIntervalS": 3600},
            }
    try:
        _catcher_api("PATCH", "/rest/config/options", {
            "globalAnnounceEnabled": False, "localAnnounceEnabled": False, "relaysEnabled": False,
            "urAccepted": -1, "crashReportingEnabled": False, "autoUpgradeIntervalH": 0,
        })
        for dev in wanted_devices.values():
            _catcher_api("PUT", f"/rest/config/devices/{dev['deviceID']}", dev)
        for folder in wanted_folders.values():
            _catcher_api("PUT", f"/rest/config/folders/{folder['id']}", folder)
        for folder in _catcher_api("GET", "/rest/config/folders") or []:
            if folder["id"].startswith("casper-") and folder["id"] not in wanted_folders:
                _catcher_api("DELETE", f"/rest/config/folders/{folder['id']}")
        for dev in _catcher_api("GET", "/rest/config/devices") or []:
            if dev.get("name", "").startswith("casper:") and dev["deviceID"] not in wanted_devices:
                _catcher_api("DELETE", f"/rest/config/devices/{dev['deviceID']}")
    except requests.RequestException as e:
        print(f"catcher reconcile failed: {e}")


def start_catcher_loop(interval: float = 15.0):
    if not catcher_available():
        return

    def loop():
        import time

        while True:
            reconcile_catcher()
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()


# --- Desired state ---------------------------------------------------------------------
def _active_targets(db, folder_id: str) -> list:
    """Targets whose grant is still active."""
    rows = db.execute(
        """
        SELECT mt.* FROM mirror_targets mt JOIN relation_tuples rt ON rt.id = mt.grant_id
        WHERE mt.folder_id = ? AND mt.removed_at IS NULL AND rt.revoked_at IS NULL
        """,
        (folder_id,),
    ).fetchall()
    return list(rows)


def desired_state(db, user_id: int, host_id: int) -> dict:
    """What this identity's pairing on this host should be doing -- the
    daemon's mirror-config (agent/internal/mirror.Desired)."""
    peers: dict[str, dict] = {}

    def add_peer(dev):
        if dev:
            peers.setdefault(dev["device_id"], {"device_id": dev["device_id"], "name": dev["name"], "addresses": dev["addresses"]})

    owned, held = [], []
    rows = db.execute(
        """
        SELECT * FROM mirror_folders WHERE owner_user_id = ? AND status = 'active'
          AND (owner_host_id = ? OR restore_host_id = ?)
        """,
        (user_id, host_id, host_id),
    ).fetchall()
    for f in rows:
        restoring = f["restore_host_id"] == host_id and f["owner_host_id"] != host_id
        mirrors, catcher = [], ""
        for t in _active_targets(db, f["id"]):
            dev = _device_for_host(db, t["peer_host_id"])
            if not dev:
                continue
            add_peer(dev)
            if t["role"] == "mirror":
                mirrors.append(dev["device_id"])
            else:
                catcher = dev["device_id"]
        if not catcher and f["use_casper_catcher"]:
            cd = catcher_device()
            if cd:
                add_peer(cd)
                catcher = cd["device_id"]
        owned.append({
            "folder_id": f["id"], "label": f["label"],
            "path": f["restore_path"] if restoring else f["path"],
            "mirrors": mirrors, "catcher": catcher, "restore": restoring,
        })

    # Folders this identity holds for friends: targets on THIS host, under
    # grants this identity gave.
    rows = db.execute(
        """
        SELECT mt.*, mf.owner_user_id, mf.owner_host_id, mf.restore_host_id, mf.label, rt.attrs
        FROM mirror_targets mt
        JOIN mirror_folders mf ON mf.id = mt.folder_id
        JOIN relation_tuples rt ON rt.id = mt.grant_id
        WHERE mt.peer_host_id = ? AND mt.peer_owner_user_id = ? AND mt.removed_at IS NULL
          AND rt.revoked_at IS NULL AND mf.status = 'active'
        """,
        (host_id, user_id),
    ).fetchall()
    for t in rows:
        owner_devs = [d for d in (_device_for_host(db, t["owner_host_id"]),
                                  _device_for_host(db, t["restore_host_id"]) if t["restore_host_id"] else None) if d]
        if not owner_devs:
            continue
        for d in owner_devs:
            add_peer(d)
        held.append({
            "folder_id": t["folder_id"],
            "label": f"{trust.username(db, t['owner_user_id'])}: {t['label']}",
            "owner_devices": [d["device_id"] for d in owner_devs],
            "kind": t["role"],
            "quota_bytes": json.loads(t["attrs"]).get("quota_bytes", 0),
        })
    return {"peers": list(peers.values()), "owned": owned, "held": held}


# --- Folders: creation, consent, stopping ------------------------------------------------
def resolve_peer_grants(db, user_id: int, names: list[str], relation: str) -> list[dict]:
    """names are "<owner>/<host>" (as list_hosts shows them)."""
    out = []
    for name in names:
        owner_name, _, label = name.partition("/")
        owner_id = trust.user_id_by_name(db, owner_name)
        grant = next((g for g in trust.grants_held(db, user_id, relation=relation)
                      if g["owner_user_id"] == owner_id and g["host"] == label), None)
        if grant is None:
            kind = "mirror" if relation == "mirror_peer" else "catcher"
            raise MirrorError(f"You don't have {kind} space on {name} (see list_hosts).")
        out.append(grant)
    return out


def used_bytes(db, grant_id: int, excluding: str | None = None) -> int:
    rows = db.execute(
        """
        SELECT ms.status FROM mirror_targets mt JOIN mirror_status ms ON ms.folder_id = mt.folder_id
        JOIN mirror_folders mf ON mf.id = mt.folder_id
        WHERE mt.grant_id = ? AND mt.removed_at IS NULL AND mf.status IN ('active', 'awaiting_consent') AND mt.folder_id != ?
        """,
        (grant_id, excluding or ""),
    ).fetchall()
    return sum(json.loads(r["status"]).get("local_bytes", 0) for r in rows)


def create_folder(db, user_id: int, host_id: int, path: str, label: str, mirror_grants: list[dict],
                  catcher_grant: dict | None, use_casper_catcher: bool) -> str:
    folder_id = "casper-" + secrets.token_hex(8)
    db.execute(
        "INSERT INTO mirror_folders (id, owner_user_id, owner_host_id, path, label, status, use_casper_catcher) VALUES (?, ?, ?, ?, ?, 'awaiting_consent', ?)",
        (folder_id, user_id, host_id, path, label, int(use_casper_catcher)),
    )
    for g in mirror_grants:
        db.execute(
            "INSERT INTO mirror_targets (folder_id, role, grant_id, peer_owner_user_id, peer_host_id) VALUES (?, 'mirror', ?, ?, ?)",
            (folder_id, g["id"], g["owner_user_id"], g["host_id"]),
        )
    if catcher_grant:
        db.execute(
            "INSERT INTO mirror_targets (folder_id, role, grant_id, peer_owner_user_id, peer_host_id) VALUES (?, 'catcher', ?, ?, ?)",
            (folder_id, catcher_grant["id"], catcher_grant["owner_user_id"], catcher_grant["host_id"]),
        )
    return folder_id


def folder_row(db, folder_id: str):
    return db.execute("SELECT * FROM mirror_folders WHERE id = ?", (folder_id,)).fetchone()


def find_owned(db, user_id: int, name: str):
    """A person's folder by label, path, or id."""
    rows = db.execute(
        "SELECT * FROM mirror_folders WHERE owner_user_id = ? AND status IN ('active', 'awaiting_consent') ORDER BY created_at",
        (user_id,),
    ).fetchall()
    for f in rows:
        if name in (f["id"], f["label"], f["path"]) or os.path.basename(f["path"].rstrip("/")) == name:
            return f
    raise MirrorError(f"No mirrored folder of yours called {name!r} (see protection_status).")


def peers_of(db, folder_id: str) -> list:
    return _active_targets(db, folder_id)


# --- Protection status -----------------------------------------------------------------
def store_status(db, user_id: int, folders: list[dict]) -> list[dict]:
    """Saves the owner daemon's reports; returns the rows that now need a
    nudge (unprotected changes older than the threshold, not recently
    nudged)."""
    import time

    nudges = []
    for st in folders or []:
        f = folder_row(db, st.get("folder_id", ""))
        if f is None or f["owner_user_id"] != user_id:
            continue
        prev = db.execute("SELECT nudged_at FROM mirror_status WHERE folder_id = ?", (f["id"],)).fetchone()
        db.execute(
            """
            INSERT INTO mirror_status (folder_id, status, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(folder_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (f["id"], json.dumps(st), trust.now_iso()),
        )
        oldest = st.get("unprotected_oldest_unix") or 0
        if st.get("unprotected_files") and oldest and time.time() - oldest > NUDGE_AFTER_SECONDS:
            last = prev["nudged_at"] if prev else None
            if not last or (time.time() - _iso_to_unix(last)) > NUDGE_EVERY_SECONDS:
                db.execute("UPDATE mirror_status SET nudged_at = ? WHERE folder_id = ?", (trust.now_iso(), f["id"]))
                nudges.append({"folder": f, "status": st})
    return nudges


def _iso_to_unix(s: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(s).timestamp()


def latest_status(db, folder_id: str) -> dict | None:
    row = db.execute("SELECT status, updated_at FROM mirror_status WHERE folder_id = ?", (folder_id,)).fetchone()
    if row is None:
        return None
    return {**json.loads(row["status"]), "reported_at": row["updated_at"]}
