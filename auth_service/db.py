import contextlib
import os
import sqlite3

DB_PATH = os.environ.get("AUTH_DB_PATH", os.path.join(os.path.dirname(__file__), "users.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    token_hash TEXT UNIQUE,
    token_created_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_users_token_hash ON users(token_hash);

-- Identity only -- one row per physical Casper daemon installation
-- (keyed by its stable, self-persisted routing_key -- see
-- agent/internal/config/routingkey.go), never tied to a user. Multiple
-- users can each know/use the same host over time (see user_hosts below);
-- who's *currently* attached is runtime state, deliberately not persisted
-- here -- see auth_service/main.py's _attached in-memory map and its
-- docstring for why.
CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routing_key TEXT NOT NULL UNIQUE,
    hostname TEXT
);

-- A user's permanent, remembered relationship to a host -- independent of
-- whether it's currently attached (to this user, to someone else, or to
-- no one), and independent of any other user's own separate relationship
-- to the same physical host. Per-user label, since two users sharing a
-- host might reasonably call it different things.
CREATE TABLE IF NOT EXISTS user_hosts (
    user_id INTEGER NOT NULL REFERENCES users(id),
    host_id INTEGER NOT NULL REFERENCES hosts(id),
    label TEXT NOT NULL,
    first_paired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, host_id)
);
CREATE INDEX IF NOT EXISTS idx_user_hosts_host_id ON user_hosts(host_id);

-- User-defined named groupings of their known hosts -- which hosts are
-- "in play" for the assistant during a given chat session.
CREATE TABLE IF NOT EXISTS environments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, name COLLATE NOCASE)
);
CREATE INDEX IF NOT EXISTS idx_environments_user_id ON environments(user_id);

-- Many-to-many: a host can belong to more than one of a user's Environments.
CREATE TABLE IF NOT EXISTS environment_hosts (
    environment_id INTEGER NOT NULL REFERENCES environments(id),
    host_id INTEGER NOT NULL REFERENCES hosts(id),
    PRIMARY KEY (environment_id, host_id)
);
CREATE INDEX IF NOT EXISTS idx_environment_hosts_host_id ON environment_hosts(host_id);

-- Notification contact info + assistant-permission preferences (pages/
-- settings_profile.py, pages/settings_security.py) -- a new table rather
-- than new columns on users, same reasoning as hosts/environments above
-- (no migration mechanism, and users.db already has real rows on deployed
-- instances). One row per user, created lazily with defaults on first
-- read/write -- see main.py's _get_or_create_profile.
CREATE TABLE IF NOT EXISTS user_profile (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    email TEXT NOT NULL DEFAULT '',
    email_notifications_enabled INTEGER NOT NULL DEFAULT 0,
    sms_number TEXT NOT NULL DEFAULT '',
    sms_notifications_enabled INTEGER NOT NULL DEFAULT 0,
    allow_configure_command_sets INTEGER NOT NULL DEFAULT 0,
    allow_configure_apps INTEGER NOT NULL DEFAULT 0,
    allow_configure_hosts INTEGER NOT NULL DEFAULT 0,
    allow_configure_environments INTEGER NOT NULL DEFAULT 0,
    allow_configure_local_agents INTEGER NOT NULL DEFAULT 0
);

-- A user-authored, reusable rule for how the assistant may invoke one CLI
-- command -- see the "Resources: command templates" plan. Which hosts it's
-- actually enabled on is a separate many-to-many (command_template_hosts
-- below), mirroring environment_hosts' relationship to environments.
CREATE TABLE IF NOT EXISTS command_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    binary TEXT NOT NULL,
    -- JSON list of {"pattern": str, "slots": {}} objects (see
    -- models.CommandTemplateArgPattern) -- v1 only ever stores/enforces
    -- zero-slot (exact-match) patterns; "slots" is reserved for future
    -- parameterized authorization, not migrated in later.
    allowed_args TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'ask',       -- 'allow' | 'ask' -- no stored 'deny': absence of a matching template already means deny, same as any unrecognized action today
    path_scoped INTEGER NOT NULL DEFAULT 1, -- confined to the host's own addressable directories via the daemon's existing resolvePath/roots machinery
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, name COLLATE NOCASE)
);
CREATE INDEX IF NOT EXISTS idx_command_templates_user_id ON command_templates(user_id);

CREATE TABLE IF NOT EXISTS command_template_hosts (
    command_template_id INTEGER NOT NULL REFERENCES command_templates(id),
    host_id INTEGER NOT NULL REFERENCES hosts(id),
    PRIMARY KEY (command_template_id, host_id)
);
CREATE INDEX IF NOT EXISTS idx_command_template_hosts_host_id ON command_template_hosts(host_id);
"""


def init_db():
    with get_db() as db:
        db.executescript(SCHEMA)


@contextlib.contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
