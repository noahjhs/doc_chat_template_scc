"""Peer-backup orchestration (docs/product/scenarios/peer-backup.md, steps
5-10). casper_service's whole role here is to MOVE ciphertext between two
daemons and keep the owner informed -- it never decides whether a write is
allowed (the destination daemon does, from its own grants, on every chunk)
and never holds a key.

main.py supplies everything environment-specific through configure():
reaching daemons, notifying people, recording approvals, running work in
the background. Tests swap run_async for a synchronous call."""

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests
from db import get_db

import trust

PREPARE_TIMEOUT_SECONDS = 30 * 60
DAEMON_TIMEOUT_SECONDS = 30


@dataclass
class Deps:
    # (owner_user_id, host_id) -> {"url", "api_key", "label"} if that
    # identity's pairing on that host is connected right now, else None.
    host_config: Callable[[int, int], dict | None]
    # user_id -> {label: {"host_id", ...}} for that user's own connected hosts.
    own_configs: Callable[[int], dict]
    notify: Callable[..., None]
    create_approval: Callable[..., str]
    run_async: Callable[[Callable[[], None]], None]


_deps: Deps | None = None


def _background(fn):
    threading.Thread(target=fn, daemon=True).start()


def configure(deps: Deps):
    global _deps
    _deps = deps


def default_run_async() -> Callable:
    return _background


class BackupError(Exception):
    """Something the owner's agent should hear about in plain words."""


# --- Talking to daemons ---------------------------------------------------------
def daemon_call(config: dict, action: str, **kwargs) -> dict:
    """POSTs one action to a daemon's /api/command. Always returns the
    daemon's Result shape: a connection/HTTP failure comes back as
    success=False with the reason in stderr, never an exception."""
    try:
        r = requests.post(
            f"{config['url']}/api/command",
            json={"action": action, **kwargs},
            headers={"X-API-Key": config["api_key"]},
            timeout=DAEMON_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        return {"success": False, "stdout": "", "stderr": f"couldn't reach {config.get('label', 'host')}: {e}"}
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = r.text
        return {"success": False, "stdout": "", "stderr": detail or f"HTTP {r.status_code}"}
    return r.json()


def _data(result: dict) -> dict:
    try:
        return json.loads(result.get("stdout") or "{}")
    except ValueError:
        return {}


def _require_ok(result: dict, what: str) -> dict:
    if not result.get("success"):
        raise BackupError(f"{what}: {result.get('stderr') or 'failed'}")
    return _data(result)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# --- Rows -------------------------------------------------------------------------
def _row(backup_id: str):
    with get_db() as db:
        return db.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()


def _update(backup_id: str, **fields):
    if not fields:
        return
    with get_db() as db:
        db.execute(
            f"UPDATE backups SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), backup_id)
        )


def _dest_name(db, row) -> str:
    return f"{trust.username(db, row['dest_owner_user_id'])}/{trust.host_label(db, row['dest_owner_user_id'], row['dest_host_id'])}"


def _describe(row) -> dict:
    with get_db() as db:
        dest = _dest_name(db, row)
    out = {
        "backup_id": row["id"],
        "name": row["name"],
        "stored_on": dest,
        "status": row["status"],
        "size": human(row["total_bytes"] or row["plaintext_bytes"]),
        "created_at": row["created_at"],
    }
    if row["status"] == "transferring" and row["chunk_count"]:
        out["progress"] = f"{row['chunks_done']}/{row['chunk_count']} chunks"
    if row["error"]:
        out["error"] = row["error"]
    if row["restore_status"]:
        out["restore"] = f"{row['restore_status']}: {row['restore_detail']}".rstrip(": ")
    return out


# --- Resolving hosts ----------------------------------------------------------
def resolve_dest(user_id: int, dest_host: str) -> tuple[dict, dict]:
    """dest_host is "<owner>/<label>" (list_hosts' naming for hosts shared
    with you). Returns (grant, connected config) -- fails fast if the
    grant is missing or the host is offline (scenario step 5)."""
    owner_name, _, label = dest_host.partition("/")
    if not label:
        raise BackupError(f"{dest_host!r} isn't a shared host -- use the owner/host name list_hosts shows (e.g. sam/sam-mini).")
    with get_db() as db:
        owner_id = trust.user_id_by_name(db, owner_name)
        grants = [g for g in trust.grants_held(db, user_id) if g["owner_user_id"] == owner_id and g["host"] == label]
    if owner_id is None or not grants:
        raise BackupError(f"You don't have backup space on {dest_host}.")
    grant = grants[0]
    config = _deps.host_config(owner_id, grant["host_id"])
    if config is None:
        raise BackupError(f"{dest_host} is offline.")
    return grant, config


def resolve_own(user_id: int, host: str) -> dict:
    configs = _deps.own_configs(user_id)
    if host not in configs:
        with get_db() as db:
            known = db.execute("SELECT 1 FROM user_hosts WHERE user_id = ? AND label = ?", (user_id, host)).fetchone()
        raise BackupError(f"{host} is offline." if known else f"{host} isn't one of your hosts.")
    return {**configs[host], "label": host}


# --- Push -----------------------------------------------------------------------------
def push(user_id: int, source_host: str, path: str, dest_host: str) -> str:
    """Scenario steps 5-8. Returns what the agent should tell its user."""
    try:
        src = resolve_own(user_id, source_host)
        grant, dest = resolve_dest(user_id, dest_host)
    except BackupError as e:
        return f"Can't back up: {e}"

    backup_id = "b" + secrets.token_urlsafe(12)
    prep = daemon_call(src, "backup_prepare", path=path, backup_id=backup_id)
    if not prep.get("success"):
        return f"Can't back up {path} on {source_host}: {prep.get('stderr')}"
    prep_data = _data(prep)
    size = int(prep_data.get("plaintext_bytes", 0))

    with get_db() as db:
        me = trust.username(db, user_id)
    verdict = daemon_call(dest, "backup_authorize", grantee=me, backup_id=backup_id, total_bytes=size)
    tier = verdict.get("tier") or "deny"
    with get_db() as db:
        db.execute(
            """
            INSERT INTO backups (id, owner_user_id, name, source_host_id, dest_host_id, dest_owner_user_id, status, plaintext_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (backup_id, user_id, prep_data.get("name") or path, src["host_id"], grant["host_id"], grant["owner_user_id"], "pending", size),
        )

    if tier == "deny":
        reason = _data(verdict).get("reason") or verdict.get("stderr") or "refused"
        _update(backup_id, status="denied", error=reason)
        daemon_call(src, "backup_cleanup", backup_id=backup_id)
        return f"{dest_host} refused the backup: {reason}."

    if tier == "ask":
        with get_db() as db:
            used = sum(r["total_bytes"] for r in db.execute(
                "SELECT total_bytes FROM backups WHERE owner_user_id = ? AND dest_host_id = ? AND status = 'complete'",
                (user_id, grant["host_id"]),
            ).fetchall())
        description = (
            f"{me} wants to store {human(size)} of encrypted backup on {grant['host']} "
            f"({human(used)} of their {human(grant['quota_bytes'])} used). Approve?"
        )
        _deps.create_approval(grant["owner_user_id"], user_id, "backup_write", description, {"backup_id": backup_id})
        _update(backup_id, status="awaiting_approval")
        return (
            f"Backup {backup_id} of {prep_data.get('name')} ({human(size)}) is waiting for {grant['owner']} to approve it. "
            f"It's being encrypted on {source_host} meanwhile. {me} will be notified when it finishes, or if it's denied."
        )

    _update(backup_id, status="transferring")
    _deps.run_async(lambda: transfer(backup_id, approved=False))
    return (
        f"Backing up {prep_data.get('name')} ({human(size)}) from {source_host} to {dest_host} as {backup_id}. "
        f"It's encrypted on {source_host} first; {me} will be notified when it finishes."
    )


def decide_write(row, decision: str) -> str:
    """The 'backup_write' approval handler (the destination owner's
    decision). Approving re-sends the whole operation with approved=true;
    the destination daemon re-decides every chunk regardless."""
    backup_id = json.loads(row["payload"])["backup_id"]
    b = _row(backup_id)
    if b is None or b["status"] != "awaiting_approval":
        return "That backup is no longer waiting."
    with get_db() as db:
        dest = _dest_name(db, b)
    if decision != "allow":
        _update(backup_id, status="denied", error="denied by the host's owner")
        src = _src_config(b)
        if src:
            daemon_call(src, "backup_cleanup", backup_id=backup_id)
        _deps.notify(b["owner_user_id"], f"Your backup of {b['name']} to {dest} was denied by its owner.")
        return f"Denied the backup of {human(b['plaintext_bytes'])}."
    _update(backup_id, status="transferring")
    _deps.run_async(lambda: transfer(backup_id, approved=True))
    return f"Approved -- storing {human(b['plaintext_bytes'])} of encrypted backup."


def _src_config(b) -> dict | None:
    c = _deps.host_config(b["owner_user_id"], b["source_host_id"])
    if c:
        with get_db() as db:
            c = {**c, "label": trust.host_label(db, b["owner_user_id"], b["source_host_id"])}
    return c


def _dest_config(b) -> dict | None:
    c = _deps.host_config(b["dest_owner_user_id"], b["dest_host_id"])
    if c:
        with get_db() as db:
            c = {**c, "label": _dest_name(db, b)}
    return c


def transfer(backup_id: str, approved: bool):
    """Moves every chunk source -> destination. Fails fast (and tells the
    owner) the moment either host is unreachable or the destination says
    no -- nothing queues for later (scenario step 8)."""
    b = _row(backup_id)
    with get_db() as db:
        me = trust.username(db, b["owner_user_id"])
        dest_name = _dest_name(db, b)
    src = None
    try:
        src, dest = _src_config(b), _dest_config(b)
        if src is None:
            raise BackupError("the source host went offline")
        if dest is None:
            raise BackupError(f"{dest_name} went offline")

        deadline = time.time() + PREPARE_TIMEOUT_SECONDS
        while True:
            status = _require_ok(daemon_call(src, "backup_prepare_status", backup_id=backup_id), "encrypting")
            if status.get("state") == "ready":
                break
            if status.get("state") == "failed":
                raise BackupError(f"encrypting failed: {status.get('error')}")
            if time.time() > deadline:
                raise BackupError("encrypting took too long")
            time.sleep(1)

        count = int(status["chunk_count"])
        _update(backup_id, total_bytes=int(status["total_bytes"]), chunk_count=count)
        for i in range(count):
            chunk = _require_ok(daemon_call(src, "backup_read_chunk", backup_id=backup_id, index=i), "reading")
            extra = {"manifest": status["manifest"], "signature": status["signature"]} if i == 0 else {}
            w = daemon_call(
                dest, "backup_write_chunk", grantee=me, backup_id=backup_id, index=i,
                content=chunk["content"], approved=approved, **extra,
            )
            tier = w.get("tier")
            if tier == "deny" or (tier == "ask" and not approved) or tier is None:
                raise BackupError(_data(w).get("reason") or w.get("stderr") or f"{dest_name} refused chunk {i}")
            _update(backup_id, chunks_done=i + 1)
        _update(backup_id, status="complete", completed_at=trust.now_iso())
        daemon_call(src, "backup_cleanup", backup_id=backup_id)
        _deps.notify(b["owner_user_id"], f"Backed up {b['name']} to {dest_name}: {human(int(status['total_bytes']))}.")
    except BackupError as e:
        _update(backup_id, status="failed", error=str(e))
        if src:
            daemon_call(src, "backup_cleanup", backup_id=backup_id)
        _deps.notify(b["owner_user_id"], f"Backup of {b['name']} to {dest_name} failed: {e}.")


# --- Status, list, restore, delete ----------------------------------------------------
def _own_row(user_id: int, backup_id: str):
    b = _row(backup_id)
    if b is None or b["owner_user_id"] != user_id or b["status"] == "deleted":
        raise BackupError(f"No backup {backup_id} of yours.")
    return b


def status(user_id: int, backup_id: str | None) -> str:
    if backup_id:
        try:
            return json.dumps(_describe(_own_row(user_id, backup_id)), indent=2)
        except BackupError as e:
            return str(e)
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM backups WHERE owner_user_id = ? AND status != 'deleted' ORDER BY created_at DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    return json.dumps([_describe(r) for r in rows], indent=2) if rows else "No backups yet."


def list_backups(user_id: int) -> str:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM backups WHERE owner_user_id = ? AND status = 'complete' ORDER BY created_at DESC", (user_id,)
        ).fetchall()
    return json.dumps([_describe(r) for r in rows], indent=2) if rows else "No completed backups stored anywhere yet."


def restore(user_id: int, backup_id: str, dest_host: str) -> str:
    """Scenario step 9. Reading back your own blobs is authorized by owning
    them (the peer daemon checks), and decryption only works on a host
    holding the key that made the backup."""
    try:
        b = _own_row(user_id, backup_id)
        if b["status"] != "complete":
            raise BackupError(f"Backup {backup_id} is {b['status']}, not complete.")
        target = resolve_own(user_id, dest_host)
        peer = _dest_config(b)
        if peer is None:
            with get_db() as db:
                raise BackupError(f"{_dest_name(db, b)} is offline.")
    except BackupError as e:
        return f"Can't restore: {e}"
    _update(backup_id, restore_status="restoring", restore_detail=f"to {dest_host}")
    _deps.run_async(lambda: _restore(b, target, peer))
    return f"Restoring {b['name']} to {dest_host}, into a new folder under 'Casper Restores'. You'll be notified when it's done."


def _restore(b, target: dict, peer: dict):
    with get_db() as db:
        me = trust.username(db, b["owner_user_id"])
    try:
        m = _require_ok(daemon_call(peer, "backup_get_manifest", grantee=me, backup_id=b["id"]), "fetching the manifest")
        _require_ok(daemon_call(target, "backup_restore_begin", backup_id=b["id"], manifest=m["manifest"], signature=m["signature"]), "starting the restore")
        for i in range(int(m["chunk_count"])):
            c = _require_ok(daemon_call(peer, "backup_get_chunk", grantee=me, backup_id=b["id"], index=i), "fetching")
            _require_ok(daemon_call(target, "backup_restore_chunk", backup_id=b["id"], index=i, content=c["content"]), "restoring")
        out = _require_ok(daemon_call(target, "backup_unpack", backup_id=b["id"]), "decrypting")
        _update(b["id"], restore_status="restored", restore_detail=out.get("restored_to", ""))
        _deps.notify(b["owner_user_id"], f"Restored {b['name']} to {target['label']}: {out.get('restored_to')}.")
    except BackupError as e:
        _update(b["id"], restore_status="failed", restore_detail=str(e))
        _deps.notify(b["owner_user_id"], f"Restoring {b['name']} failed: {e}.")


def delete(user_id: int, backup_id: str) -> str:
    try:
        b = _own_row(user_id, backup_id)
        peer = _dest_config(b)
        if b["status"] in ("complete", "failed", "transferring") and peer is None:
            with get_db() as db:
                raise BackupError(f"{_dest_name(db, b)} is offline, so it can't delete the stored copy right now.")
    except BackupError as e:
        return f"Can't delete: {e}"
    if peer is not None:
        with get_db() as db:
            me = trust.username(db, user_id)
        r = daemon_call(peer, "backup_delete", grantee=me, backup_id=backup_id)
        if not r.get("success") and b["status"] == "complete":
            return f"Can't delete: {r.get('stderr')}"
    _update(backup_id, status="deleted")
    return f"Deleted backup {backup_id} ({b['name']})."
