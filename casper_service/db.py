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
-- _attached dict this replaced, this survives an casper_service restart --
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
    allow_configure_local_agents INTEGER NOT NULL DEFAULT 0,
    system_prompt TEXT NOT NULL DEFAULT '',
    telegram_notifications_enabled INTEGER NOT NULL DEFAULT 0,
    telegram_chat_id TEXT NOT NULL DEFAULT ''
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
-- see casper_service's /policies/eval); a rule is only ever evaluated as
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

-- A durable, per-user queue of "ask"-tier pauses waiting on a decision --
-- deliberately host-agnostic (no host_id/device_token anywhere here): who
-- answers is whichever interface the user checks in from (harness, a text
-- reply -- see main.py's /sms/inbound -- eventually a browser), never a
-- designated machine. turn is the JSON-serialized paused conversation
-- turn, snapshotted right after run_turn sets turn["awaiting_approval"],
-- so resolving a row later is just calling run_turn(turn, decision, ...)
-- again -- see main.py's pending-approval endpoints. Replaces an earlier,
-- in-memory-only design that routed through a designated "attended host"
-- daemon's own native dialog -- removed as a real design mistake (a
-- native dialog fires wherever a host happens to be designated, not
-- necessarily wherever a human is actually watching; see git history for
-- the removed user_attended_host table this replaces).
CREATE TABLE IF NOT EXISTS pending_approvals (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    description TEXT NOT NULL,
    turn TEXT NOT NULL,
    default_host TEXT,
    mock INTEGER NOT NULL DEFAULT 0,
    mock_tier TEXT NOT NULL DEFAULT 'allow',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_user_id ON pending_approvals(user_id);

-- Tokens an agent (any MCP client) presents to /mcp -- each acts on behalf
-- of user_id, with at most that user's permissions (docs/product/
-- trust-framework.md's "Agents as subjects", v1). Separate from the
-- browser/harness session token on users, so an agent can be revoked on
-- its own and audited as its own subject.
CREATE TABLE IF NOT EXISTS agent_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_user_id ON agent_tokens(user_id);

-- The trust framework's single ReBAC-shaped store (docs/product/
-- trust-framework.md): (subject, relation, object) tuples, everything else
-- derived from them. v1 relations: 'friend' (user -> user, one tuple per
-- direction) and 'backup_peer' (user -> host, attrs: owner_user_id,
-- quota_bytes, write_tier, offering_id). Revocation sets revoked_at rather
-- than deleting, since a revoked backup_peer grant still matters for its
-- grace period (and for the audit trail).
CREATE TABLE IF NOT EXISTS relation_tuples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id INTEGER NOT NULL,
    attrs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_relation_tuples_subject ON relation_tuples(subject_type, subject_id, relation);
CREATE INDEX IF NOT EXISTS idx_relation_tuples_object ON relation_tuples(object_type, object_id, relation);

-- A pending/decided friend request. Decided through the ordinary approvals
-- queue (kind 'friend_request', approver = the recipient), so a Telegram
-- tap works the same as for every other decision.
CREATE TABLE IF NOT EXISTS friend_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL REFERENCES users(id),
    to_user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'accepted' | 'declined'
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT
);

-- What an owner is willing to share, visible to their friends (docs/
-- product/trust-framework.md's "Offers, requests and grants"). v1 kind:
-- 'backup_space' only. write_tier is the Backup Peer role's write tier on
-- the resulting grant ('allow' | 'ask').
CREATE TABLE IF NOT EXISTS offerings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    host_id INTEGER NOT NULL REFERENCES hosts(id),
    kind TEXT NOT NULL,
    audience TEXT NOT NULL DEFAULT 'friends',
    max_quota_bytes INTEGER NOT NULL,
    write_tier TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    withdrawn_at TEXT
);

-- A request for an offering; approving it (approvals kind
-- 'access_request', approver = the offering's owner) writes the grant.
CREATE TABLE IF NOT EXISTS access_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offering_id INTEGER NOT NULL REFERENCES offerings(id),
    requester_user_id INTEGER NOT NULL REFERENCES users(id),
    quota_bytes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'granted' | 'declined'
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT
);

-- One peer backup, as its owner (and casper_service) tracks it. The name
-- is known here (the owner's agent named the folder), never on the peer.
CREATE TABLE IF NOT EXISTS backups (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    source_host_id INTEGER NOT NULL REFERENCES hosts(id),
    dest_host_id INTEGER NOT NULL REFERENCES hosts(id),
    dest_owner_user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,   -- awaiting_approval | transferring | complete | denied | failed | deleted
    plaintext_bytes INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    chunks_done INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    restore_status TEXT NOT NULL DEFAULT '',
    restore_detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_backups_owner ON backups(owner_user_id);

-- Mirroring (docs/product/scenarios/mirroring.md). casper_service is the
-- discovery service: each Mac's Syncthing device ID and addresses, keyed by
-- the machine's routing_key (one Syncthing per machine, shared by every
-- account paired there).
CREATE TABLE IF NOT EXISTS mirror_devices (
    routing_key TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    addresses TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

-- A folder a person mirrors. status: awaiting_consent -> active (the
-- person confirmed outside their agent) | declined; stopped when they end
-- it. restore_host_id/restore_path: a replacement Mac rebuilding it.
CREATE TABLE IF NOT EXISTS mirror_folders (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    owner_host_id INTEGER NOT NULL REFERENCES hosts(id),
    path TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT,
    use_casper_catcher INTEGER NOT NULL DEFAULT 0,
    restore_host_id INTEGER REFERENCES hosts(id),
    restore_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at TEXT,
    stopped_at TEXT
);

-- Where a folder is mirrored: a friend's mirror, or a friend's catcher,
-- each under a grant on that friend's host.
CREATE TABLE IF NOT EXISTS mirror_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id TEXT NOT NULL REFERENCES mirror_folders(id),
    role TEXT NOT NULL,                 -- 'mirror' | 'catcher'
    grant_id INTEGER NOT NULL REFERENCES relation_tuples(id),
    peer_owner_user_id INTEGER NOT NULL REFERENCES users(id),
    peer_host_id INTEGER NOT NULL REFERENCES hosts(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_at TEXT
);

-- The owner's daemon's latest protection report per folder.
CREATE TABLE IF NOT EXISTS mirror_status (
    folder_id TEXT PRIMARY KEY REFERENCES mirror_folders(id),
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    nudged_at TEXT
);

-- Casper's guide (casper_service/guide.py): one conversation per person,
-- shared by the web chat and Telegram. OpenAI-format messages, including
-- tool calls and results.
CREATE TABLE IF NOT EXISTS guide_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_guide_messages_user ON guide_messages(user_id, id);

-- Single-use invites to an offering (docs/product/scenarios/
-- agent-onboarding.md, "Why invite codes"): the inviter consents by creating
-- one for a specific amount; redeeming it makes the redeemer their friend
-- and grants the space in one step. Only the code's hash is stored.
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    offering_id INTEGER NOT NULL REFERENCES offerings(id),
    quota_bytes INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    used_by_user_id INTEGER REFERENCES users(id),
    used_at TEXT,
    cancelled_at TEXT,
    group_id INTEGER REFERENCES groups(id),
    delivered_via TEXT NOT NULL DEFAULT '',
    completed_at TEXT
);

-- A person's named circle ("Mutual Aid"): the name an invite welcomes
-- someone into. Redeeming an invite to a group adds a 'member' tuple
-- (user -> group) in relation_tuples; the owner is groups.owner_user_id.
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_user_id, name)
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


def _add_system_prompt_column_to_user_profile(db):
    """One-time ALTER TABLE ADD COLUMN for user_profile.system_prompt --
    the custom instructions text prepended to every conversation turn (see
    conversations.py's run_turn / DispatchContext.system_prompt). Same
    posture as _add_cwd_column_to_policy_layer_rules: runs AFTER the
    CREATE TABLE IF NOT EXISTS script (a fresh database already gets the
    column from SCHEMA directly; this only ever fires against an existing
    database that predates it). Idempotent: no-ops once the column already
    exists, and never runs at all against a database that doesn't have the
    table yet."""
    existing = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "user_profile" not in existing:
        return
    columns = {row["name"] for row in db.execute("PRAGMA table_info(user_profile)").fetchall()}
    if "system_prompt" in columns:
        return
    db.execute("ALTER TABLE user_profile ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''")


def _add_telegram_columns_to_user_profile(db):
    """One-time ALTER TABLE ADD COLUMN for user_profile.telegram_chat_id/
    telegram_notifications_enabled (see main.py's telegram_link/
    telegram_webhook). Same posture as the other user_profile column
    migrations above."""
    existing = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "user_profile" not in existing:
        return
    columns = {row["name"] for row in db.execute("PRAGMA table_info(user_profile)").fetchall()}
    if "telegram_chat_id" not in columns:
        db.execute("ALTER TABLE user_profile ADD COLUMN telegram_chat_id TEXT NOT NULL DEFAULT ''")
    if "telegram_notifications_enabled" not in columns:
        db.execute("ALTER TABLE user_profile ADD COLUMN telegram_notifications_enabled INTEGER NOT NULL DEFAULT 0")


def _add_mock_tier_column_to_pending_approvals(db):
    """One-time ALTER TABLE ADD COLUMN for pending_approvals.mock_tier --
    the explicit tier a mocked run_shell_command call was paused under
    (see conversations.py's DispatchContext.mock_tier), needed to resume a
    mocked "ask" the same way it was originally simulated. Same posture as
    the other one-time column migrations above: runs AFTER the CREATE
    TABLE IF NOT EXISTS script, idempotent, never runs against a database
    that doesn't have the table yet."""
    existing = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "pending_approvals" not in existing:
        return
    columns = {row["name"] for row in db.execute("PRAGMA table_info(pending_approvals)").fetchall()}
    if "mock_tier" in columns:
        return
    db.execute("ALTER TABLE pending_approvals ADD COLUMN mock_tier TEXT NOT NULL DEFAULT 'allow'")


def _add_kind_columns_to_pending_approvals(db):
    """One-time ALTER TABLE ADD COLUMNs generalizing pending_approvals
    beyond paused conversation turns (added 2026-09-28 for the MCP/peer-
    backup work -- see docs/product/v1-implementation-plan.md's Phase 0):
    kind names what kind of paused thing a row resumes ('conversation' --
    every pre-existing row, whose state lives in `turn` -- or one of
    main.py's _APPROVAL_HANDLERS kinds, whose state lives in `payload`);
    requester_user_id is who ASKED, since the approver (user_id) is no
    longer always the same person -- e.g. Sam approving Riley's backup
    onto Sam's host. NULL requester_user_id means "same as user_id"."""
    existing = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "pending_approvals" not in existing:
        return
    columns = {row["name"] for row in db.execute("PRAGMA table_info(pending_approvals)").fetchall()}
    if "kind" not in columns:
        db.execute("ALTER TABLE pending_approvals ADD COLUMN kind TEXT NOT NULL DEFAULT 'conversation'")
    if "payload" not in columns:
        db.execute("ALTER TABLE pending_approvals ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
    if "requester_user_id" not in columns:
        db.execute("ALTER TABLE pending_approvals ADD COLUMN requester_user_id INTEGER REFERENCES users(id)")


def _add_backup_key_columns_to_host_pairings(db):
    """One-time ALTER TABLE ADD COLUMNs: each pairing's backup signing
    public key (registered by the daemon -- POST /hosts/backup-key) and
    where its private keys live ('icloud' | 'local'), per the plan's Phase
    3. Per pairing, not per user: each of a user's machines has its own
    keys, and a peer trusts any of them."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(host_pairings)").fetchall()}
    if "backup_signing_key" not in columns:
        db.execute("ALTER TABLE host_pairings ADD COLUMN backup_signing_key TEXT")
    if "backup_key_storage" not in columns:
        db.execute("ALTER TABLE host_pairings ADD COLUMN backup_key_storage TEXT NOT NULL DEFAULT ''")


def init_db():
    with get_db() as db:
        _rename_legacy_rule_chain_tables(db)
        db.executescript(SCHEMA)
        _add_cwd_column_to_policy_layer_rules(db)
        _add_system_prompt_column_to_user_profile(db)
        _add_telegram_columns_to_user_profile(db)
        _add_mock_tier_column_to_pending_approvals(db)
        _add_kind_columns_to_pending_approvals(db)
        _add_backup_key_columns_to_host_pairings(db)
        _add_group_columns_to_invites(db)


def _add_group_columns_to_invites(db):
    """One-time ALTER TABLE ADD COLUMNs: which group an invite welcomes
    someone into, and how it was delivered ('email', 'telegram'). Same
    posture as the migrations above."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(invites)").fetchall()}
    if "group_id" not in columns:
        db.execute("ALTER TABLE invites ADD COLUMN group_id INTEGER REFERENCES groups(id)")
    if "delivered_via" not in columns:
        db.execute("ALTER TABLE invites ADD COLUMN delivered_via TEXT NOT NULL DEFAULT ''")
    if "completed_at" not in columns:
        # An accepted invite stays usable (e.g. by the same person starting
        # over) until their first mirror is running -- see trust.find_invite.
        db.execute("ALTER TABLE invites ADD COLUMN completed_at TEXT")


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
