import contextlib
import json
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
-- see host_pairings below for which one currently owns it.
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

-- Which user(s) currently OWN a routing_key, and the live credentials that
-- prove each of their pairings -- one row per (routing_key, user_id), so
-- the SAME physical daemon can be paired to several different accounts at
-- once (each fully independent: its own device_token/command_key, its own
-- policy_layers via policy_layer_hosts keyed off host_id+that user's own
-- user_id -- see main.py's _connected_host_configs/list_hosts). Re-pairing
-- the same (routing_key, user_id) is idempotent -- replaces that one row's
-- credentials in place (see main.py's _pair_host), never disturbs any
-- other user's row for the same routing_key. Unlike the old in-memory-only
-- _attached dict this replaced, this survives an auth_service restart --
-- the actual fix for daemons needing to be manually re-paired after every
-- routine redeploy: device_token_hash stays valid, so a daemon's own
-- existing resumeSession()-on-startup logic (agent/cmd/casper/main.go)
-- just works again on its own, no `casper://pair` round trip needed unless
-- this row is genuinely gone (a real first-time pairing, or an explicit
-- unpair/forget/sign-out of that specific account).
-- Deliberately does NOT include local_agent_url/cwd -- those are live
-- reachability facts a restart should legitimately forget (a daemon
-- reports them fresh on its next presence beat regardless), not identity
-- -- see main.py's _live in-memory dict for those (genuinely machine-level,
-- shared across every account paired to a routing_key, not per-pairing).
CREATE TABLE IF NOT EXISTS host_pairings (
    routing_key TEXT NOT NULL REFERENCES hosts(routing_key),
    user_id INTEGER NOT NULL REFERENCES users(id),
    host_id INTEGER NOT NULL REFERENCES hosts(id),
    device_token_hash TEXT NOT NULL UNIQUE,
    command_key TEXT NOT NULL,
    paired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (routing_key, user_id)
);
CREATE INDEX IF NOT EXISTS idx_host_pairings_device_token_hash ON host_pairings(device_token_hash);
CREATE INDEX IF NOT EXISTS idx_host_pairings_routing_key ON host_pairings(routing_key);

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

-- Notification contact info + assistant-permission preferences -- a new
-- table rather than new columns on users, same reasoning as hosts/environments above
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

-- SUPERSEDED by rule_chains/rule_chain_rules below -- kept only because
-- there's no migration mechanism and real rows exist on deployed
-- instances. No app code reads or writes these anymore.
CREATE TABLE IF NOT EXISTS command_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    binary TEXT NOT NULL,
    allowed_args TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'ask',
    path_scoped INTEGER NOT NULL DEFAULT 1,
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

-- A user-authored, reusable, ORDERED list of rules for how the assistant
-- may invoke CLI commands on a host -- replaces command_templates' flat
-- exact-match allowlist with sequential first-match-wins evaluation (see
-- policy_layer_rules below). A "Policy" is the composition of one or more
-- layers for a single tool (v1: shell only, by straight concatenation --
-- see auth_service's /policies/eval); a rule is only ever evaluated as
-- part of a policy, never a layer standalone. "Policy Layer" is
-- deliberately not called "Toolset" -- that name is reserved for a
-- later, broader concept spanning every tool (not just shell), once more
-- than one tool has security considerations of its own. Which hosts a
-- layer is enabled on is a separate many-to-many (policy_layer_hosts),
-- mirroring environment_hosts/command_template_hosts.
CREATE TABLE IF NOT EXISTS policy_layers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, name COLLATE NOCASE)
);
CREATE INDEX IF NOT EXISTS idx_policy_layers_user_id ON policy_layers(user_id);

CREATE TABLE IF NOT EXISTS policy_layer_hosts (
    policy_layer_id INTEGER NOT NULL REFERENCES policy_layers(id),
    host_id INTEGER NOT NULL REFERENCES hosts(id),
    PRIMARY KEY (policy_layer_id, host_id)
);
CREATE INDEX IF NOT EXISTS idx_policy_layer_hosts_host_id ON policy_layer_hosts(host_id);

-- One row per rule (not a JSON list column on policy_layers) -- rules are
-- individually created/edited/deleted/reordered from client tooling, so a
-- child table gives per-rule CRUD and a plain ORDER BY position without a
-- read-modify-write of the whole list on every single-rule edit.
CREATE TABLE IF NOT EXISTS policy_layer_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_layer_id INTEGER NOT NULL REFERENCES policy_layers(id),
    position INTEGER NOT NULL,             -- this rule's place in the LAYER's eval order (unrelated to argv positions)
    -- JSON list of {"whitelist":.., "blacklist":..} -- list index IS the
    -- argv position (index 0 = the binary itself); a value beyond the
    -- list's length, or a fully blank entry, is unconstrained ("value not
    -- required" -- a missing value is matched as "", which a blank
    -- pattern always matches; no "*" sentinel needed).
    positional_constraints TEXT NOT NULL,
    -- JSON list of {"short":.., "long":.., "pattern": {"whitelist":..,
    -- "blacklist":..}} -- "options" (not "flags"): including one at all
    -- means it must be PRESENT; pattern is checked against its value, or
    -- against "" if present with no value, so a blank pattern means
    -- "value not required" and a whitelist of "^$" means "value not
    -- allowed" -- the exact same {whitelist,blacklist} shape and "missing
    -- means empty string" convention positional_constraints uses above.
    option_constraints TEXT NOT NULL,
    -- One JSON {"whitelist":.., "blacklist":.., "path_resolution":..}
    -- object (a single Pattern, not a list -- there's only ever one cwd
    -- per call) -- optionally constrains the directory the call runs in.
    -- Added by _add_cwd_column_to_policy_layer_rules below; a NOT NULL
    -- column with no schema-level default since every INSERT always
    -- supplies one (see main.py's create_policy_layer_rule) -- the
    -- migration itself backfills existing rows.
    cwd TEXT NOT NULL,
    tier TEXT NOT NULL,                    -- 'allow' | 'ask' | 'deny' -- no default; every rule states its own
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_policy_layer_rules_policy_layer_id ON policy_layer_rules(policy_layer_id, position);

-- Which one of a user's known hosts they're currently physically at --
-- used to route a pending approval's native-dialog prompt to the right
-- daemon (see main.py's pending-approvals endpoints). One row per user
-- (upserted), not a column on users, same "new tables only" reasoning as
-- everything else here. Must reference a row already in user_hosts
-- (enforced in main.py, not by a foreign key -- PRAGMA foreign_keys is
-- never turned on in this file, so none of this schema's REFERENCES
-- clauses are DB-enforced).
CREATE TABLE IF NOT EXISTS user_attended_host (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    host_id INTEGER NOT NULL REFERENCES hosts(id),
    set_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _rename_legacy_rule_chain_tables(db):
    """One-time rename from the rule_chain* naming era to policy_layer* --
    a straight rename of the same data/shape (not a schema/semantic
    change), so a real ALTER TABLE is correct here, unlike this file's
    usual "new tables only, no migration" convention for actual schema
    changes. Must run before the CREATE TABLE IF NOT EXISTS script below
    -- otherwise that script would create a fresh, empty policy_layers
    table first, and this would then see the new name already "existing"
    and skip, silently stranding the real data under the old name.
    Idempotent: no-ops once the new names already exist (or there was
    never a rule_chains table to begin with, e.g. a brand-new deploy)."""
    existing = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "rule_chains" not in existing or "policy_layers" in existing:
        return
    db.execute("ALTER TABLE rule_chains RENAME TO policy_layers")
    db.execute("DROP INDEX IF EXISTS idx_rule_chains_user_id")

    db.execute("ALTER TABLE rule_chain_hosts RENAME TO policy_layer_hosts")
    db.execute("ALTER TABLE policy_layer_hosts RENAME COLUMN rule_chain_id TO policy_layer_id")
    db.execute("DROP INDEX IF EXISTS idx_rule_chain_hosts_host_id")

    db.execute("ALTER TABLE rule_chain_rules RENAME TO policy_layer_rules")
    db.execute("ALTER TABLE policy_layer_rules RENAME COLUMN rule_chain_id TO policy_layer_id")
    db.execute("DROP INDEX IF EXISTS idx_rule_chain_rules_chain_id")


def _add_cwd_column_to_policy_layer_rules(db):
    """One-time ALTER TABLE ADD COLUMN for policy_layer_rules.cwd (added
    alongside path_resolution -- see Pattern's own docstring in models.py)
    -- a real schema change, unlike _rename_legacy_rule_chain_tables's
    straight rename, so this runs AFTER the CREATE TABLE IF NOT EXISTS
    script below (a fresh database already gets the column from SCHEMA
    directly; this only ever fires against an existing database that
    predates it). Backfills every existing row with a blank ("any
    directory") Pattern -- the literal JSON mirrors Pattern's own default
    field values in models.py by hand (this module has no dependency on
    that one). Idempotent: no-ops once the column already exists, and
    never runs at all against a database that doesn't have the table yet."""
    existing = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "policy_layer_rules" not in existing:
        return
    columns = {row["name"] for row in db.execute("PRAGMA table_info(policy_layer_rules)").fetchall()}
    if "cwd" in columns:
        return
    blank_pattern = json.dumps({"whitelist": "", "blacklist": r"[^\s\S]", "path_resolution": ""}).replace("'", "''")
    db.execute(f"ALTER TABLE policy_layer_rules ADD COLUMN cwd TEXT NOT NULL DEFAULT '{blank_pattern}'")


def init_db():
    with get_db() as db:
        _rename_legacy_rule_chain_tables(db)
        db.executescript(SCHEMA)
        _add_cwd_column_to_policy_layer_rules(db)


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
