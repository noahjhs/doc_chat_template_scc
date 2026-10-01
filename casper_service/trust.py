"""The trust framework's data layer (docs/product/trust-framework.md): friend
relations, offerings, access requests and Backup Peer grants, all stored as
relation_tuples (see db.py). Plain functions over an open db connection --
no HTTP, no notifications; main.py wires those around these.

This is platform-layer storage (arbitrary relations/attrs). What the app
layer actually offers in v1 is deliberately narrow: mutual friendship, and
one offering kind (backup space) that yields one role (Backup Peer)."""

import json
from datetime import datetime, timezone

GB = 1024**3


class TrustError(Exception):
    """A request the trust framework refuses, with a message for a person."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_iso() -> str:
    """RFC 3339, so the Go daemon can parse revoked_at directly."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def user_id_by_name(db, username: str) -> int | None:
    row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return row["id"] if row else None


def username(db, user_id: int) -> str:
    row = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else f"user{user_id}"


# --- Friends -----------------------------------------------------------------
def are_friends(db, a: int, b: int) -> bool:
    row = db.execute(
        """
        SELECT 1 FROM relation_tuples
        WHERE subject_type = 'user' AND subject_id = ? AND relation = 'friend'
          AND object_type = 'user' AND object_id = ? AND revoked_at IS NULL
        """,
        (a, b),
    ).fetchone()
    return row is not None


def friends_of(db, user_id: int) -> list[int]:
    rows = db.execute(
        """
        SELECT object_id FROM relation_tuples
        WHERE subject_type = 'user' AND subject_id = ? AND relation = 'friend'
          AND object_type = 'user' AND revoked_at IS NULL
        """,
        (user_id,),
    ).fetchall()
    return [r["object_id"] for r in rows]


def create_friend_request(db, from_id: int, to_username: str) -> tuple[int, int]:
    """Returns (request_id, to_user_id). Mutual friendship needs the other
    side's consent, so this only ever creates a pending request."""
    to_id = user_id_by_name(db, to_username)
    if to_id is None:
        raise TrustError(f"No user named {to_username!r}.", 404)
    if to_id == from_id:
        raise TrustError("You can't befriend yourself.")
    if are_friends(db, from_id, to_id):
        raise TrustError(f"You're already friends with {to_username}.", 409)
    pending = db.execute(
        """
        SELECT 1 FROM friend_requests WHERE status = 'pending'
          AND ((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))
        """,
        (from_id, to_id, to_id, from_id),
    ).fetchone()
    if pending:
        raise TrustError(f"There's already a pending friend request between you and {to_username}.", 409)
    cur = db.execute("INSERT INTO friend_requests (from_user_id, to_user_id) VALUES (?, ?)", (from_id, to_id))
    return cur.lastrowid, to_id


def decide_friend_request(db, request_id: int, accept: bool) -> dict | None:
    row = db.execute("SELECT * FROM friend_requests WHERE id = ? AND status = 'pending'", (request_id,)).fetchone()
    if row is None:
        return None
    db.execute(
        "UPDATE friend_requests SET status = ?, decided_at = ? WHERE id = ?",
        ("accepted" if accept else "declined", now_iso(), request_id),
    )
    if accept:
        for a, b in ((row["from_user_id"], row["to_user_id"]), (row["to_user_id"], row["from_user_id"])):
            db.execute(
                "INSERT INTO relation_tuples (subject_type, subject_id, relation, object_type, object_id) VALUES ('user', ?, 'friend', 'user', ?)",
                (a, b),
            )
    return dict(row)


def remove_friend(db, user_id: int, other_username: str) -> list[dict]:
    """Ends a friendship in both directions, and revokes every Backup Peer
    grant between the two (either way round) -- a grant's precondition is
    the relationship (docs/product/risk-model.md, point 5). Returns the
    revoked grants, so the caller can tell daemons and notify people."""
    other_id = user_id_by_name(db, other_username)
    if other_id is None or not are_friends(db, user_id, other_id):
        raise TrustError(f"You're not friends with {other_username}.", 404)
    ts = now_iso()
    db.execute(
        """
        UPDATE relation_tuples SET revoked_at = ?
        WHERE relation = 'friend' AND revoked_at IS NULL
          AND ((subject_id = ? AND object_id = ?) OR (subject_id = ? AND object_id = ?))
        """,
        (ts, user_id, other_id, other_id, user_id),
    )
    revoked = []
    for g in grants_between(db, user_id, other_id) + grants_between(db, other_id, user_id):
        revoke_grant_row(db, g["id"], ts)
        revoked.append(g)
    return revoked


def pending_friend_requests(db, user_id: int) -> dict:
    incoming = db.execute(
        "SELECT fr.id, u.username FROM friend_requests fr JOIN users u ON u.id = fr.from_user_id WHERE fr.to_user_id = ? AND fr.status = 'pending'",
        (user_id,),
    ).fetchall()
    outgoing = db.execute(
        "SELECT fr.id, u.username FROM friend_requests fr JOIN users u ON u.id = fr.to_user_id WHERE fr.from_user_id = ? AND fr.status = 'pending'",
        (user_id,),
    ).fetchall()
    return {
        "incoming": [{"id": r["id"], "username": r["username"]} for r in incoming],
        "outgoing": [{"id": r["id"], "username": r["username"]} for r in outgoing],
    }


# --- Offerings -----------------------------------------------------------------
def owns_paired_host(db, user_id: int, host_id: int) -> bool:
    row = db.execute(
        "SELECT 1 FROM host_pairings hp WHERE hp.user_id = ? AND hp.host_id = ?", (user_id, host_id)
    ).fetchone()
    return row is not None


def host_label(db, user_id: int, host_id: int) -> str:
    row = db.execute("SELECT label FROM user_hosts WHERE user_id = ? AND host_id = ?", (user_id, host_id)).fetchone()
    return row["label"] if row else f"host{host_id}"


def publish_offering(db, owner_id: int, host_id: int, max_quota_bytes: int, write_tier: str) -> int:
    if not owns_paired_host(db, owner_id, host_id):
        raise TrustError("You can only offer space on a host you've paired.", 404)
    if write_tier not in ("allow", "ask"):
        raise TrustError("write_tier must be 'allow' or 'ask'.")
    cur = db.execute(
        "INSERT INTO offerings (owner_user_id, host_id, kind, max_quota_bytes, write_tier) VALUES (?, ?, 'backup_space', ?, ?)",
        (owner_id, host_id, max_quota_bytes, write_tier),
    )
    return cur.lastrowid


def withdraw_offering(db, owner_id: int, offering_id: int):
    """Stops new requests. Existing grants are separate things, revoked on
    their own (see revoke_grant)."""
    cur = db.execute(
        "UPDATE offerings SET withdrawn_at = ? WHERE id = ? AND owner_user_id = ? AND withdrawn_at IS NULL",
        (now_iso(), offering_id, owner_id),
    )
    if cur.rowcount == 0:
        raise TrustError("No such offering of yours.", 404)


def _offering_dict(db, row, viewer_id: int) -> dict:
    out = {
        "id": row["id"],
        "owner": username(db, row["owner_user_id"]),
        "host": host_label(db, row["owner_user_id"], row["host_id"]),
        "kind": row["kind"],
        "max_quota_gb": round(row["max_quota_bytes"] / GB, 2),
        "write_tier": row["write_tier"],
    }
    if row["owner_user_id"] != viewer_id:
        grant = active_grant(db, viewer_id, row["host_id"], row["owner_user_id"])
        pending = db.execute(
            "SELECT 1 FROM access_requests WHERE offering_id = ? AND requester_user_id = ? AND status = 'pending'",
            (row["id"], viewer_id),
        ).fetchone()
        out["yours"] = "granted" if grant else ("requested" if pending else None)
    return out


def list_offerings(db, viewer_id: int) -> dict:
    """The viewer's own offerings, and their friends' (audience 'friends'
    is the only audience in v1)."""
    mine = db.execute(
        "SELECT * FROM offerings WHERE owner_user_id = ? AND withdrawn_at IS NULL ORDER BY id", (viewer_id,)
    ).fetchall()
    friend_ids = friends_of(db, viewer_id)
    theirs = []
    if friend_ids:
        placeholders = ",".join("?" * len(friend_ids))
        theirs = db.execute(
            f"SELECT * FROM offerings WHERE owner_user_id IN ({placeholders}) AND withdrawn_at IS NULL ORDER BY id",
            friend_ids,
        ).fetchall()
    return {
        "mine": [_offering_dict(db, r, viewer_id) for r in mine],
        "friends": [_offering_dict(db, r, viewer_id) for r in theirs],
    }


def create_access_request(db, requester_id: int, offering_id: int, quota_bytes: int) -> tuple[int, dict]:
    """Returns (request_id, offering row). Requests are the app layer's
    usual route to a grant: the requester browses what a friend offers and
    asks; the owner decides (docs/product/scenarios/peer-backup.md, step 3)."""
    offering = db.execute(
        "SELECT * FROM offerings WHERE id = ? AND withdrawn_at IS NULL", (offering_id,)
    ).fetchone()
    if offering is None:
        raise TrustError("No such offering.", 404)
    owner_id = offering["owner_user_id"]
    if owner_id == requester_id:
        raise TrustError("That's your own offering.")
    if not are_friends(db, requester_id, owner_id):
        raise TrustError("Only friends can request this.", 403)
    if quota_bytes <= 0 or quota_bytes > offering["max_quota_bytes"]:
        raise TrustError(f"Ask for between 0 and {offering['max_quota_bytes'] / GB:g} GB.")
    if active_grant(db, requester_id, offering["host_id"], owner_id):
        raise TrustError("You already have backup space there.", 409)
    if db.execute(
        "SELECT 1 FROM access_requests WHERE offering_id = ? AND requester_user_id = ? AND status = 'pending'",
        (offering_id, requester_id),
    ).fetchone():
        raise TrustError("You've already asked; it's waiting on them.", 409)
    cur = db.execute(
        "INSERT INTO access_requests (offering_id, requester_user_id, quota_bytes) VALUES (?, ?, ?)",
        (offering_id, requester_id, quota_bytes),
    )
    return cur.lastrowid, dict(offering)


def decide_access_request(db, request_id: int, grant: bool) -> dict | None:
    """Returns the request (plus its offering's fields) if it was still
    pending. Granting writes the backup_peer tuple -- the grant itself."""
    row = db.execute(
        """
        SELECT ar.*, o.owner_user_id, o.host_id, o.write_tier
        FROM access_requests ar JOIN offerings o ON o.id = ar.offering_id
        WHERE ar.id = ? AND ar.status = 'pending'
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    db.execute(
        "UPDATE access_requests SET status = ?, decided_at = ? WHERE id = ?",
        ("granted" if grant else "declined", now_iso(), request_id),
    )
    if grant and are_friends(db, row["requester_user_id"], row["owner_user_id"]):
        attrs = {
            "owner_user_id": row["owner_user_id"],
            "quota_bytes": row["quota_bytes"],
            "write_tier": row["write_tier"],
            "offering_id": row["offering_id"],
        }
        db.execute(
            "INSERT INTO relation_tuples (subject_type, subject_id, relation, object_type, object_id, attrs) VALUES ('user', ?, 'backup_peer', 'host', ?, ?)",
            (row["requester_user_id"], row["host_id"], json.dumps(attrs)),
        )
    return dict(row)


# --- Grants --------------------------------------------------------------------
def _grant_dict(db, row) -> dict:
    attrs = json.loads(row["attrs"])
    return {
        "id": row["id"],
        "grantee_user_id": row["subject_id"],
        "grantee": username(db, row["subject_id"]),
        "owner_user_id": attrs["owner_user_id"],
        "owner": username(db, attrs["owner_user_id"]),
        "host_id": row["object_id"],
        "host": host_label(db, attrs["owner_user_id"], row["object_id"]),
        "quota_bytes": attrs["quota_bytes"],
        "write_tier": attrs["write_tier"],
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
    }


def _grant_rows(db, where: str, params: tuple) -> list[dict]:
    rows = db.execute(
        f"SELECT * FROM relation_tuples WHERE relation = 'backup_peer' AND object_type = 'host' AND {where} ORDER BY id",
        params,
    ).fetchall()
    return [_grant_dict(db, r) for r in rows]


def active_grant(db, grantee_id: int, host_id: int, owner_id: int) -> dict | None:
    for g in _grant_rows(db, "subject_id = ? AND object_id = ? AND revoked_at IS NULL", (grantee_id, host_id)):
        if g["owner_user_id"] == owner_id:
            return g
    return None


def latest_grant(db, grantee_id: int, host_id: int, owner_id: int) -> dict | None:
    """The newest grant, active or revoked -- for reading back backups
    stored under a grant that's since been revoked (grace period)."""
    grants = [g for g in _grant_rows(db, "subject_id = ? AND object_id = ?", (grantee_id, host_id)) if g["owner_user_id"] == owner_id]
    return grants[-1] if grants else None


def grants_held(db, grantee_id: int, include_revoked: bool = False) -> list[dict]:
    where = "subject_id = ?" + ("" if include_revoked else " AND revoked_at IS NULL")
    return _grant_rows(db, where, (grantee_id,))


def grants_given(db, owner_id: int, include_revoked: bool = False) -> list[dict]:
    rows = _grant_rows(db, "1 = 1" if include_revoked else "revoked_at IS NULL", ())
    return [g for g in rows if g["owner_user_id"] == owner_id]


def grants_between(db, grantee_id: int, owner_id: int) -> list[dict]:
    return [g for g in grants_held(db, grantee_id) if g["owner_user_id"] == owner_id]


def grants_on_host_for_daemon(db, owner_id: int, host_id: int) -> list[dict]:
    """What GET /hosts/grants tells a daemon: every Backup Peer grant its
    identity (owner_id) gave on this host, revoked ones included (so the
    daemon can honor the grace period and then purge), each with every
    backup signing key its grantee has registered."""
    out = []
    for g in _grant_rows(db, "object_id = ?", (host_id,)):
        if g["owner_user_id"] != owner_id:
            continue
        keys = db.execute(
            "SELECT backup_signing_key FROM host_pairings WHERE user_id = ? AND backup_signing_key IS NOT NULL",
            (g["grantee_user_id"],),
        ).fetchall()
        out.append(
            {
                "grantee": g["grantee"],
                "signing_keys": [k["backup_signing_key"] for k in keys],
                "quota_bytes": g["quota_bytes"],
                "write_tier": g["write_tier"],
                "revoked_at": g["revoked_at"],
            }
        )
    return out


def revoke_grant_row(db, grant_id: int, ts: str | None = None):
    db.execute(
        "UPDATE relation_tuples SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (ts or now_iso(), grant_id)
    )


def revoke_grant(db, actor_id: int, grant_id: int) -> dict:
    """Either side can end it: the host owner (revoke) or the grantee
    (give it up). Returns the grant as it was."""
    rows = _grant_rows(db, "id = ? AND revoked_at IS NULL", (grant_id,))
    if not rows or actor_id not in (rows[0]["owner_user_id"], rows[0]["grantee_user_id"]):
        raise TrustError("No such active grant of yours.", 404)
    revoke_grant_row(db, grant_id)
    return rows[0]


# --- Invites -------------------------------------------------------------------
INVITE_TTL_DAYS = 7
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: read aloud, typed by hand


def _invite_hash(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.strip().upper().replace(" ", "").encode()).hexdigest()


def new_invite_code() -> str:
    import secrets

    raw = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(12))
    return f"CASPER-{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def create_invite(db, owner_id: int, offering_id: int, quota_bytes: int, note: str = "") -> tuple[str, dict]:
    """Returns (code, offering). The code itself is shown once; only its
    hash is kept."""
    from datetime import timedelta

    offering = db.execute(
        "SELECT * FROM offerings WHERE id = ? AND owner_user_id = ? AND withdrawn_at IS NULL", (offering_id, owner_id)
    ).fetchone()
    if offering is None:
        raise TrustError("No such offering of yours.", 404)
    if quota_bytes <= 0 or quota_bytes > offering["max_quota_bytes"]:
        raise TrustError(f"Invite for between 0 and {offering['max_quota_bytes'] / GB:g} GB (the offering's limit).")
    code = new_invite_code()
    expires = (datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO invites (code_hash, owner_user_id, offering_id, quota_bytes, note, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (_invite_hash(code), owner_id, offering_id, quota_bytes, note, expires),
    )
    return code, dict(offering)


def find_invite(db, code: str) -> dict:
    row = db.execute(
        """
        SELECT i.*, o.host_id, o.write_tier, o.withdrawn_at AS offering_withdrawn
        FROM invites i JOIN offerings o ON o.id = i.offering_id WHERE i.code_hash = ?
        """,
        (_invite_hash(code),),
    ).fetchone()
    if row is None:
        raise TrustError("That invite code isn't valid -- check it was copied exactly.", 404)
    if row["cancelled_at"] or row["offering_withdrawn"]:
        raise TrustError("That invite was cancelled by the person who sent it.", 410)
    if row["used_at"]:
        raise TrustError("That invite has already been used (invites are single-use) -- ask for a new one.", 410)
    if row["expires_at"] < now_iso():
        raise TrustError("That invite has expired -- ask for a new one.", 410)
    return dict(row)


def redeem_invite(db, user_id: int, code: str) -> dict:
    """Friendship (if not already) plus the grant, in one step -- the
    inviter consented when creating the invite; the redeemer consents now."""
    inv = find_invite(db, code)
    owner_id = inv["owner_user_id"]
    if owner_id == user_id:
        raise TrustError("That's your own invite -- send it to the friend it's for.")
    if active_grant(db, user_id, inv["host_id"], owner_id):
        raise TrustError("You already have backup space there.", 409)
    if not are_friends(db, user_id, owner_id):
        for a, b in ((user_id, owner_id), (owner_id, user_id)):
            db.execute(
                "INSERT INTO relation_tuples (subject_type, subject_id, relation, object_type, object_id) VALUES ('user', ?, 'friend', 'user', ?)",
                (a, b),
            )
    attrs = {"owner_user_id": owner_id, "quota_bytes": inv["quota_bytes"], "write_tier": inv["write_tier"], "offering_id": inv["offering_id"], "invite_id": inv["id"]}
    db.execute(
        "INSERT INTO relation_tuples (subject_type, subject_id, relation, object_type, object_id, attrs) VALUES ('user', ?, 'backup_peer', 'host', ?, ?)",
        (user_id, inv["host_id"], json.dumps(attrs)),
    )
    db.execute("UPDATE invites SET used_by_user_id = ?, used_at = ? WHERE id = ?", (user_id, now_iso(), inv["id"]))
    return inv


def cancel_invite(db, owner_id: int, invite_id: int):
    cur = db.execute(
        "UPDATE invites SET cancelled_at = ? WHERE id = ? AND owner_user_id = ? AND used_at IS NULL AND cancelled_at IS NULL",
        (now_iso(), invite_id, owner_id),
    )
    if cur.rowcount == 0:
        raise TrustError("No such open invite of yours.", 404)


def open_invites(db, owner_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM invites WHERE owner_user_id = ? AND used_at IS NULL AND cancelled_at IS NULL AND expires_at > ? ORDER BY id",
        (owner_id, now_iso()),
    ).fetchall()
    return [dict(r) for r in rows]
