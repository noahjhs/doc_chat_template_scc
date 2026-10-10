"""Dev-only support for scripted test scenarios (tools/scenario.py): delete
a test persona's account outright, and act as one (an agent token), so a
scenario can rebuild that persona's server state from scratch each run.

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


def delete_user(db, user_id: int) -> dict:
    """Removes the account and everything that's theirs or about them --
    machines' pairings, grants either way, friendships, groups, invites,
    offerings, mirrors, conversations. Machines (hosts rows) stay: they're
    machine-level and may be shared with other accounts."""
    counts: dict[str, int] = {}

    def run(label: str, sql: str, params: tuple = ()):
        n = db.execute(sql, params).rowcount
        if n:
            counts[label] = counts.get(label, 0) + n

    ids = lambda sql, params: [r[0] for r in db.execute(sql, params).fetchall()]  # noqa: E731
    groups = ids("SELECT id FROM groups WHERE owner_user_id = ?", (user_id,))
    folders = ids("SELECT id FROM mirror_folders WHERE owner_user_id = ?", (user_id,))
    owned_hosts = ids("SELECT host_id FROM user_hosts WHERE user_id = ?", (user_id,))

    # Relation tuples: about them, about their groups, or grants on their machines.
    for row in db.execute("SELECT id, subject_type, subject_id, object_type, object_id, attrs FROM relation_tuples").fetchall():
        attrs = json.loads(row["attrs"] or "{}")
        if ((row["subject_type"] == "user" and row["subject_id"] == user_id)
                or (row["object_type"] == "user" and row["object_id"] == user_id)
                or (row["object_type"] == "group" and row["object_id"] in groups)
                or attrs.get("owner_user_id") == user_id):
            run("relation_tuples", "DELETE FROM relation_tuples WHERE id = ?", (row["id"],))
    for f in folders:
        run("mirror_status", "DELETE FROM mirror_status WHERE folder_id = ?", (f,))
        run("mirror_targets", "DELETE FROM mirror_targets WHERE folder_id = ?", (f,))
    run("mirror_targets", "DELETE FROM mirror_targets WHERE peer_owner_user_id = ?", (user_id,))
    run("mirror_folders", "DELETE FROM mirror_folders WHERE owner_user_id = ?", (user_id,))
    run("invites", "DELETE FROM invites WHERE owner_user_id = ?", (user_id,))
    run("invites", "UPDATE invites SET used_by_user_id = NULL, used_at = NULL WHERE used_by_user_id = ? AND completed_at IS NULL", (user_id,))
    run("access_requests", "DELETE FROM access_requests WHERE requester_user_id = ? OR offering_id IN (SELECT id FROM offerings WHERE owner_user_id = ?)", (user_id, user_id))
    run("offerings", "DELETE FROM offerings WHERE owner_user_id = ?", (user_id,))
    run("groups", "DELETE FROM groups WHERE owner_user_id = ?", (user_id,))
    run("friend_requests", "DELETE FROM friend_requests WHERE from_user_id = ? OR to_user_id = ?", (user_id, user_id))
    run("pending_approvals", "DELETE FROM pending_approvals WHERE user_id = ? OR requester_user_id = ?", (user_id, user_id))
    run("backups", "DELETE FROM backups WHERE owner_user_id = ? OR dest_owner_user_id = ?", (user_id, user_id))
    for table, child, key in (("environments", "environment_hosts", "environment_id"),
                              ("command_templates", "command_template_hosts", "command_template_id"),
                              ("policy_layers", "policy_layer_hosts", "policy_layer_id")):
        for parent in ids(f"SELECT id FROM {table} WHERE user_id = ?", (user_id,)):
            run(child, f"DELETE FROM {child} WHERE {key} = ?", (parent,))
            if table == "policy_layers":
                run("policy_layer_rules", "DELETE FROM policy_layer_rules WHERE policy_layer_id = ?", (parent,))
        run(table, f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    for table in ("guide_messages", "agent_tokens", "user_profile", "host_pairings", "user_hosts"):
        run(table, f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    run("users", "DELETE FROM users WHERE id = ?", (user_id,))
    return {"deleted": counts, "machines_kept": len(owned_hosts)}
