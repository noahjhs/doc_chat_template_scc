"""Tests for the v1 platform work (see docs/product/v1-implementation-plan.md):
generic approvals/notifications, agent tokens + the MCP server, the trust
framework tables, and backup orchestration. Daemons are FakeDaemons -- this
file never re-tests real policy matching or real encryption, both of which
are the Go daemon's own tested concerns."""

import os
import sys
import tempfile

import pytest

CASPER_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "casper_service")
_MODULES = ("main", "db", "models", "policy", "conversations", "mcp_server", "trust", "backups")


@pytest.fixture()
def app_env():
    sys.path.insert(0, os.path.abspath(CASPER_SERVICE_DIR))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUTH_DB_PATH"] = os.path.join(tmp, "users.db")
        os.environ["STORAGE_ROOT"] = os.path.join(tmp, "storage")
        for mod in _MODULES:
            sys.modules.pop(mod, None)
        import main as auth_main
        from fastapi.testclient import TestClient

        yield auth_main, TestClient(auth_main.app)
    sys.path.remove(os.path.abspath(CASPER_SERVICE_DIR))
    for mod in _MODULES:
        sys.modules.pop(mod, None)


def _signup(client, username):
    body = client.post("/signup", json={"username": username, "password": "correct-horse"}).json()
    return body, {"Authorization": f"Bearer {body['token']}"}


def _user_id(main, username):
    with main.get_db() as db:
        return db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]


# --- Phase 0: generic approvals --------------------------------------------
def test_generic_approval_is_listed_for_the_approver_with_its_requester(app_env):
    main, client = app_env
    _, sam_headers = _signup(client, "sam")
    _, riley_headers = _signup(client, "riley")
    decisions = []

    def handler(row, decision):
        decisions.append((row["kind"], decision))
        return f"handled {decision}"

    main._APPROVAL_HANDLERS["test_kind"] = handler
    approval_id = main.create_approval(_user_id(main, "sam"), _user_id(main, "riley"), "test_kind", "Riley wants X", {"a": 1})

    listed = client.get("/conversations/pending-approvals", headers=sam_headers).json()["pending_approvals"]
    assert [(p["id"], p["kind"], p["requester"]) for p in listed] == [(approval_id, "test_kind", "riley")]
    # The requester can't see or decide it -- it isn't their approval.
    assert client.get("/conversations/pending-approvals", headers=riley_headers).json()["pending_approvals"] == []
    r = client.post(f"/conversations/pending-approvals/{approval_id}/decide", json={"decision": "allow"}, headers=riley_headers)
    assert r.status_code == 404

    r = client.post(f"/conversations/pending-approvals/{approval_id}/decide", json={"decision": "allow"}, headers=sam_headers)
    assert r.status_code == 200
    assert r.json()["message"] == "handled allow"
    assert decisions == [("test_kind", "allow")]
    # Resolved exactly once.
    r = client.post(f"/conversations/pending-approvals/{approval_id}/decide", json={"decision": "allow"}, headers=sam_headers)
    assert r.status_code == 404


def test_own_approval_has_no_separate_requester(app_env):
    main, client = app_env
    _, sam_headers = _signup(client, "sam")
    sam_id = _user_id(main, "sam")
    main._APPROVAL_HANDLERS["test_kind"] = lambda row, decision: "ok"
    main.create_approval(sam_id, sam_id, "test_kind", "my own call", {})
    listed = client.get("/conversations/pending-approvals", headers=sam_headers).json()["pending_approvals"]
    assert listed[0]["requester"] is None


def test_notify_sends_buttons_only_to_linked_opted_in_users(app_env, monkeypatch):
    main, client = app_env
    _, sam_headers = _signup(client, "sam")
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(main.requests, "post", lambda url, json=None, timeout=None: sent.append(json))

    main.notify(_user_id(main, "sam"), "hello")
    assert sent == []  # not linked/opted in

    with main.get_db() as db:
        db.execute("INSERT OR IGNORE INTO user_profile (user_id) VALUES (?)", (_user_id(main, "sam"),))
        db.execute("UPDATE user_profile SET telegram_chat_id = '42', telegram_notifications_enabled = 1")
    main.notify(_user_id(main, "sam"), "hello", buttons=[("Yes", "approve:x")])
    assert sent == [
        {"chat_id": "42", "text": "hello", "reply_markup": {"inline_keyboard": [[{"text": "Yes", "callback_data": "approve:x"}]]}}
    ]
