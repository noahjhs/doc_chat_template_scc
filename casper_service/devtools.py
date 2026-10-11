"""Dev-only support for scripted test scenarios (tools/scenario.py): delete
a test persona's account outright, act as one (an agent token), and capture
and restore one exactly (export/import), so a checkpoint's VM snapshot and
its server state can be saved and brought back together.

Enabled only when DEV_ADMIN_TOKEN is set in the environment -- never on
prod. Every endpoint checks it; without it they don't exist (404)."""

import json
import os
import secrets

from fastapi import Header, HTTPException


def require_dev_admin(x_dev_admin: str = Header(default="")):
    expected = os.environ.get("DEV_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if not secrets.compare_digest(x_dev_admin, expected):
        raise HTTPException(status_code=403, detail="Not allowed.")


def _selectors(db, user_id: int) -> list[tuple[str, str, tuple, str]]:
    """Every row that's the person's or about them: (table, WHERE, params,
    what deleting does). One definition for deleting AND capturing, so a
    capture holds exactly what a reset removes. Children come before their
    parents (their WHEREs read the parents). Actions: "delete"; "unuse"
    (someone else's invite they accepted: put it back unaccepted); "keep"
    (machine-level rows, captured but never deleted)."""
    groups = [r[0] for r in db.execute("SELECT id FROM groups WHERE owner_user_id = ?", (user_id,)).fetchall()]
    tuples = []
    for row in db.execute("SELECT id, subject_type, subject_id, object_type, object_id, attrs FROM relation_tuples").fetchall():
        attrs = json.loads(row["attrs"] or "{}")
        if ((row["subject_type"] == "user" and row["subject_id"] == user_id)
                or (row["object_type"] == "user" and row["object_id"] == user_id)
                or (row["object_type"] == "group" and row["object_id"] in groups)
                or attrs.get("owner_user_id") == user_id):
            tuples.append(row["id"])
    ids = lambda xs: "(" + ",".join(str(int(x)) for x in xs) + ")" if xs else "(NULL)"  # noqa: E731
    u = (user_id,)
    folders = f"(SELECT id FROM mirror_folders WHERE owner_user_id = {int(user_id)})"
    mine = lambda table: f"(SELECT id FROM {table} WHERE user_id = {int(user_id)})"  # noqa: E731
    return [
        ("relation_tuples", f"id IN {ids(tuples)}", (), "delete"),
        ("mirror_status", f"folder_id IN {folders}", (), "delete"),
        ("mirror_targets", f"folder_id IN {folders} OR peer_owner_user_id = ?", u, "delete"),
        ("mirror_folders", "owner_user_id = ?", u, "delete"),
        ("invites", "owner_user_id = ?", u, "delete"),
        ("invites", "used_by_user_id = ? AND owner_user_id != ? AND completed_at IS NULL", (user_id, user_id), "unuse"),
        ("access_requests", "requester_user_id = ? OR offering_id IN (SELECT id FROM offerings WHERE owner_user_id = ?)", (user_id, user_id), "delete"),
        ("offerings", "owner_user_id = ?", u, "delete"),
        ("groups", "owner_user_id = ?", u, "delete"),
        ("friend_requests", "from_user_id = ? OR to_user_id = ?", (user_id, user_id), "delete"),
        ("pending_approvals", "user_id = ? OR requester_user_id = ?", (user_id, user_id), "delete"),
        ("backups", "owner_user_id = ? OR dest_owner_user_id = ?", (user_id, user_id), "delete"),
        ("environment_hosts", f"environment_id IN {mine('environments')}", (), "delete"),
        ("environments", "user_id = ?", u, "delete"),
        ("command_template_hosts", f"command_template_id IN {mine('command_templates')}", (), "delete"),
        ("command_templates", "user_id = ?", u, "delete"),
        ("policy_layer_hosts", f"policy_layer_id IN {mine('policy_layers')}", (), "delete"),
        ("policy_layer_rules", f"policy_layer_id IN {mine('policy_layers')}", (), "delete"),
        ("policy_layers", "user_id = ?", u, "delete"),
        ("mirror_devices", "routing_key IN (SELECT routing_key FROM hosts WHERE id IN (SELECT host_id FROM user_hosts WHERE user_id = ?))", u, "keep"),
        ("hosts", "id IN (SELECT host_id FROM user_hosts WHERE user_id = ?)", u, "keep"),
        ("guide_messages", "user_id = ?", u, "delete"),
        ("agent_tokens", "user_id = ?", u, "delete"),
        ("user_profile", "user_id = ?", u, "delete"),
        ("host_pairings", "user_id = ?", u, "delete"),
        ("user_hosts", "user_id = ?", u, "delete"),
        ("users", "id = ?", u, "delete"),
    ]


def delete_user(db, user_id: int) -> dict:
    """Removes the account and everything that's theirs or about them.
    Machines (hosts rows) stay: they're machine-level and may be shared."""
    counts: dict[str, int] = {}
    for table, where, params, action in _selectors(db, user_id):
        if action == "keep":
            continue
        sql = (f"UPDATE {table} SET used_by_user_id = NULL, used_at = NULL WHERE {where}" if action == "unuse"
               else f"DELETE FROM {table} WHERE {where}")
        n = db.execute(sql, params).rowcount
        if n:
            counts[table] = counts.get(table, 0) + n
    machines = db.execute("SELECT COUNT(*) FROM hosts WHERE id IN (SELECT host_id FROM user_hosts WHERE user_id = ?)", (user_id,)).fetchone()[0]
    return {"deleted": counts, "machines_kept": machines}


def export_user(db, user_id: int) -> dict:
    """Exactly the rows a reset would remove (and the machine rows it keeps),
    to restore later with import_rows."""
    tables: dict[str, list[dict]] = {}
    for table, where, params, _ in _selectors(db, user_id):
        rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()]
        if rows:
            tables.setdefault(table, []).extend(rows)
    return {"tables": tables}


def import_rows(db, tables: dict[str, list[dict]]) -> dict:
    """Puts captured rows back as they were (same ids), replacing anything
    with the same key. Columns are matched by name, so a capture taken before
    a column was added still loads (the new column gets its default)."""
    counts = {}
    for table, rows in tables.items():
        current = {c[1] for c in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if not current:
            continue
        for row in rows:
            cols = [c for c in row if c in current]
            db.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [row[c] for c in cols])
        counts[table] = len(rows)
    return counts
