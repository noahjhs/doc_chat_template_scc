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

-- Where a user's Casper daemon is currently reachable, so the web app can
-- learn its relay URL/workspace without the daemon redirecting a browser
-- tab itself (the old callback_port/nonce pairing flow this replaces --
-- see pages/signin.py). One row per user, matching the one-active-token
-- model /login already enforces. A new table rather than new columns on
-- users -- there's no migration mechanism here (init_db() is purely
-- additive), so extending an already-live table isn't safe.
CREATE TABLE IF NOT EXISTS agent_presence (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    local_agent_url TEXT NOT NULL,
    workspace TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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
