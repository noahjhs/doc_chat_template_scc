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


# --- Shared helpers for the scenario tests ---------------------------------------
import json  # noqa: E402

from fake_daemon import FakeDaemon  # noqa: E402

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "mcp-protocol-version": "2025-06-18"}


def _pair(client, headers, routing_key, hostname, url):
    pair = client.post("/hosts/pair", json={"routing_key": routing_key, "hostname": hostname}, headers=headers).json()
    device = {"Authorization": f"Bearer {pair['device_token']}"}
    client.post("/hosts/presence", json={"local_agent_url": url, "cwd": "/Users/x"}, headers=device)
    return device


def _host_id(client, headers, label):
    return next(h["host_id"] for h in client.get("/hosts", headers=headers).json()["hosts"] if h["label"] == label)


def _agent_token(client, headers):
    return client.post("/agent-tokens", json={"name": "claude"}, headers=headers).json()["token"]


def _mcp(client, agent_token, tool, **arguments):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        headers={**MCP_HEADERS, "Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    if "structuredContent" in result and "result" in result["structuredContent"]:
        return result["structuredContent"]["result"]
    return "\n".join(c.get("text", "") for c in result["content"])


def _approvals(client, headers):
    return client.get("/conversations/pending-approvals", headers=headers).json()["pending_approvals"]


def _decide(client, headers, approval_id, decision="allow"):
    r = client.post(f"/conversations/pending-approvals/{approval_id}/decide", json={"decision": decision}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["message"]


def _ok(**data):
    return 200, {"success": True, "cwd": "/Users/x", "stdout": json.dumps(data), "stderr": ""}


def _befriend(client, a_headers, b_headers, b_name):
    client.post("/friends/requests", json={"username": b_name}, headers=a_headers)
    [req] = [p for p in _approvals(client, b_headers) if p["kind"] == "friend_request"]
    _decide(client, b_headers, req["id"])


# --- Phase 1: agent tokens + MCP ------------------------------------------------------
def test_mcp_rejects_missing_and_revoked_agent_tokens(app_env):
    main, client = app_env
    with client:
        _, headers = _signup(client, "riley")
        r = client.post("/mcp", json={}, headers=MCP_HEADERS)
        assert r.status_code == 401
        created = client.post("/agent-tokens", json={"name": "claude"}, headers=headers).json()
        # A session token is not an agent token.
        assert client.post("/mcp", json={}, headers={**MCP_HEADERS, **headers}).status_code == 401
        assert _mcp(client, created["token"], "list_hosts") == []
        client.delete(f"/agent-tokens/{created['id']}", headers=headers)
        assert client.post("/mcp", json={}, headers={**MCP_HEADERS, "Authorization": f"Bearer {created['token']}"}).status_code == 401
        assert client.get("/agent-tokens", headers=headers).json()["agent_tokens"][0]["revoked"] is True


def test_mcp_run_shell_command_allow_and_ask(app_env):
    main, client = app_env

    def respond(body):
        if body["positional_args"][0] == "uptime":
            return 200, {"success": True, "cwd": "/", "stdout": "up 3 days", "stderr": "", "exit_code": 0, "tier": "allow"}
        if body.get("approved"):
            return 200, {"success": True, "cwd": "/", "stdout": "restarted", "stderr": "", "exit_code": 0, "tier": "ask"}
        return 200, {"success": False, "cwd": "/", "stdout": "", "stderr": "", "tier": "ask"}

    with client, FakeDaemon(respond_with=respond) as daemon:
        _, headers = _signup(client, "sam")
        _pair(client, headers, "rk-mini", "sam-mini", daemon.url)
        token = _agent_token(client, headers)

        assert _mcp(client, token, "list_hosts") == [{"host": "sam-mini", "role": "owner", "connected": True}]
        assert _mcp(client, token, "run_shell_command", host="sam-mini", positional_args=["uptime"]) == "up 3 days"

        out = _mcp(client, token, "run_shell_command", host="sam-mini", positional_args=["brew", "services", "restart"])
        assert "needs the owner's approval" in out
        [approval] = _approvals(client, headers)
        assert approval["kind"] == "shell_command"
        assert _decide(client, headers, approval["id"]) == "restarted"
        # The resend carried approved=true; the daemon re-decided it.
        assert daemon.requests[-1]["approved"] is True


# --- Phase 2: trust framework ------------------------------------------------------------
def test_friendship_is_mutual_and_consensual(app_env):
    main, client = app_env
    _, riley = _signup(client, "riley")
    _, sam = _signup(client, "sam")
    assert client.post("/friends/requests", json={"username": "nobody"}, headers=riley).status_code == 404
    assert client.post("/friends/requests", json={"username": "riley"}, headers=riley).status_code == 400
    client.post("/friends/requests", json={"username": "sam"}, headers=riley)
    assert client.post("/friends/requests", json={"username": "riley"}, headers=sam).status_code == 409  # already pending
    assert client.get("/friends", headers=riley).json() == {"friends": [], "incoming": [], "outgoing": [{"id": 1, "username": "sam"}]}
    [req] = _approvals(client, sam)
    assert req["requester"] == "riley"
    _decide(client, sam, req["id"], "deny")
    assert client.get("/friends", headers=sam).json()["friends"] == []

    _befriend(client, riley, sam, "sam")
    assert client.get("/friends", headers=riley).json()["friends"] == ["sam"]
    assert client.get("/friends", headers=sam).json()["friends"] == ["riley"]


def test_offerings_are_friends_only_and_requests_are_bounded(app_env):
    main, client = app_env
    with FakeDaemon(queue=[]) as daemon:
        _, sam = _signup(client, "sam")
        _, riley = _signup(client, "riley")
        _, eve = _signup(client, "eve")
        _pair(client, sam, "rk-mini", "sam-mini", daemon.url)
        host_id = _host_id(client, sam, "sam-mini")
        # Can't offer someone else's host.
        assert client.post("/offerings", json={"host_id": host_id, "max_quota_gb": 20}, headers=riley).status_code == 404
        offering_id = client.post("/offerings", json={"host_id": host_id, "max_quota_gb": 20, "write_tier": "ask"}, headers=sam).json()["id"]
        _befriend(client, riley, sam, "sam")

        assert client.get("/offerings", headers=eve).json()["friends"] == []  # not a friend: can't see it
        assert client.post(f"/offerings/{offering_id}/requests", json={"quota_gb": 5}, headers=eve).status_code == 403
        assert client.post(f"/offerings/{offering_id}/requests", json={"quota_gb": 50}, headers=riley).status_code == 400

        [offer] = client.get("/offerings", headers=riley).json()["friends"]
        assert (offer["owner"], offer["host"], offer["max_quota_gb"], offer["yours"]) == ("sam", "sam-mini", 20.0, None)


def test_scenario_peer_backup_end_to_end(app_env):
    """docs/product/scenarios/peer-backup.md, steps 1-10, with both daemons
    faked: the fakes script verdicts; they never decide anything."""
    main, client = app_env
    import backups

    notes = []
    backups._deps.run_async = lambda fn: fn()
    main.notify = lambda user_id, text, buttons=None: notes.append((user_id, text))

    def riley_daemon(body):
        a = body["action"]
        if a == "backup_prepare":
            assert body["path"] == "~/Documents/taxes"
            return _ok(backup_id=body["backup_id"], plaintext_bytes=1_200_000, name="taxes")
        if a == "backup_prepare_status":
            return _ok(state="ready", manifest="TUFO", signature="U0lH", total_bytes=1_300_000, chunk_count=2)
        if a == "backup_read_chunk":
            return _ok(content=f"chunk{body['index']}")
        if a in ("backup_cleanup", "backup_restore_begin", "backup_restore_chunk"):
            return _ok()
        if a == "backup_unpack":
            return _ok(restored_to="/Users/riley/Casper Restores/taxes-20260928-120000")
        raise AssertionError(a)

    def sam_daemon(body):
        a = body["action"]
        assert body.get("grantee", "riley") == "riley"
        if a == "backup_authorize":
            return 200, {"success": False, "cwd": "", "stdout": json.dumps({"reason": ""}), "stderr": "", "tier": "ask"}
        if a == "backup_write_chunk":
            tier = "ask"
            return 200, {"success": bool(body.get("approved")), "cwd": "", "stdout": json.dumps({"reason": "", "complete": body["index"] == 1}), "stderr": "", "tier": tier}
        if a == "backup_get_manifest":
            return _ok(manifest="TUFO", signature="U0lH", chunk_count=2)
        if a == "backup_get_chunk":
            return _ok(content=f"chunk{body['index']}")
        if a in ("refresh_grants", "backup_delete"):
            return _ok()
        raise AssertionError(a)

    with client, FakeDaemon(respond_with=riley_daemon) as rd, FakeDaemon(respond_with=sam_daemon) as sd:
        _, sam = _signup(client, "sam")
        _, riley = _signup(client, "riley")
        sam_device = _pair(client, sam, "rk-mini", "sam-mini", sd.url)
        riley_device = _pair(client, riley, "rk-laptop", "riley-laptop", rd.url)
        client.post("/hosts/backup-key", json={"signing_public_key": "RILEYKEY", "key_storage": "local"}, headers=riley_device)

        # 1. Friends.  2. Sam offers space.  3. Riley's agent requests it; Sam grants.
        _befriend(client, riley, sam, "sam")
        client.post("/offerings", json={"host_id": _host_id(client, sam, "sam-mini"), "max_quota_gb": 20, "write_tier": "ask"}, headers=sam)
        agent = _agent_token(client, riley)
        [offer] = _mcp(client, agent, "list_offerings")
        assert "Requested 10 GB" in _mcp(client, agent, "request_access", offering_id=offer["id"], quota_gb=10)
        [req] = [p for p in _approvals(client, sam) if p["kind"] == "access_request"]
        assert "10 GB of backup space on sam-mini" in req["description"]
        _decide(client, sam, req["id"])
        assert any("granted you 10.0 GB" in t for _, t in notes)

        # Sam's daemon sees the grant, with Riley's registered signing key.
        [grant] = client.get("/hosts/grants", headers=sam_device).json()["grants"]
        assert grant == {"grantee": "riley", "signing_keys": ["RILEYKEY"], "quota_bytes": 10 * 1024**3, "write_tier": "ask", "revoked_at": None}
        hosts = _mcp(client, agent, "list_hosts")
        assert {"host": "sam/sam-mini", "role": "backup_peer", "approval_needed_to_write": True}.items() <= next(h for h in hosts if h["host"] == "sam/sam-mini").items()

        # 5-7. Push: nothing moves until Sam approves; the agent gets a plain answer.
        out = _mcp(client, agent, "backup_push", source_host="riley-laptop", path="~/Documents/taxes", dest_host="sam/sam-mini", preview=False)
        assert "waiting for sam to approve" in out
        assert not any(r["action"] == "backup_write_chunk" for r in sd.requests)
        [ask] = [p for p in _approvals(client, sam) if p["kind"] == "backup_write"]
        assert ask["requester"] == "riley" and "taxes" not in ask["description"]  # Sam never sees names

        # 8. Sam approves; every chunk goes out with approved=true, manifest on chunk 0.
        _decide(client, sam, ask["id"])
        writes = [r for r in sd.requests if r["action"] == "backup_write_chunk"]
        assert [(w["index"], w["approved"], "manifest" in w) for w in writes] == [(0, True, True), (1, True, False)]
        assert any("Backed up taxes to sam/sam-mini" in t for _, t in notes)
        backup_id = json.loads(_mcp(client, agent, "backup_status"))[0]["backup_id"]
        assert json.loads(_mcp(client, agent, "backup_status", backup_id=backup_id))["status"] == "complete"

        # 9. Restore to Riley's own host.
        assert "Restoring taxes" in _mcp(client, agent, "backup_restore", backup_id=backup_id, dest_host="riley-laptop")
        assert [r["action"] for r in rd.requests[-4:]] == ["backup_restore_begin", "backup_restore_chunk", "backup_restore_chunk", "backup_unpack"]
        assert any("Casper Restores/taxes-" in t for _, t in notes)

        # Fail fast when the destination is offline.
        client.delete("/hosts/presence", headers=sam_device)
        assert "sam/sam-mini is offline" in _mcp(client, agent, "backup_push", source_host="riley-laptop", path="~/x", dest_host="sam/sam-mini", preview=False)
        client.post("/hosts/presence", json={"local_agent_url": sd.url, "cwd": "/"}, headers=sam_device)

        # 10. Sam revokes: Riley is told, the daemon is told to refresh, the grant shows revoked.
        grant_id = client.get("/grants", headers=sam).json()["given"][0]["id"]
        assert client.delete(f"/grants/{grant_id}", headers=sam).status_code == 200
        assert any("revoked your backup space" in t for _, t in notes)
        [revoked] = client.get("/hosts/grants", headers=sam_device).json()["grants"]
        assert revoked["revoked_at"] is not None
        assert not any(h["host"] == "sam/sam-mini" for h in _mcp(client, agent, "list_hosts"))
        assert "don't have backup space" in _mcp(client, agent, "backup_push", source_host="riley-laptop", path="~/x", dest_host="sam/sam-mini", preview=False)


def test_backup_denied_by_daemon_quota(app_env):
    main, client = app_env
    import backups

    backups._deps.run_async = lambda fn: fn()
    main.notify = lambda *a, **k: None

    def riley_daemon(body):
        if body["action"] == "backup_prepare":
            return _ok(plaintext_bytes=5, name="big")
        return _ok()

    def sam_daemon(body):
        if body["action"] == "backup_authorize":
            return 200, {"success": False, "cwd": "", "stdout": json.dumps({"reason": "over quota: 9 GB used"}), "stderr": "", "tier": "deny"}
        return _ok()

    with client, FakeDaemon(respond_with=riley_daemon) as rd, FakeDaemon(respond_with=sam_daemon) as sd:
        _, sam = _signup(client, "sam")
        _, riley = _signup(client, "riley")
        _pair(client, sam, "rk-mini", "sam-mini", sd.url)
        _pair(client, riley, "rk-laptop", "riley-laptop", rd.url)
        _befriend(client, riley, sam, "sam")
        oid = client.post("/offerings", json={"host_id": _host_id(client, sam, "sam-mini"), "max_quota_gb": 20, "write_tier": "allow"}, headers=sam).json()["id"]
        client.post(f"/offerings/{oid}/requests", json={"quota_gb": 10}, headers=riley)
        _decide(client, sam, _approvals(client, sam)[0]["id"])
        agent = _agent_token(client, riley)
        out = _mcp(client, agent, "backup_push", source_host="riley-laptop", path="~/big", dest_host="sam/sam-mini", preview=False)
        assert out == "sam/sam-mini refused the backup: over quota: 9 GB used."
        assert rd.requests[-1]["action"] == "backup_cleanup"


def test_unfriending_revokes_grants(app_env):
    main, client = app_env
    with client, FakeDaemon(respond_with=lambda b: _ok()) as sd:
        _, sam = _signup(client, "sam")
        _, riley = _signup(client, "riley")
        _pair(client, sam, "rk-mini", "sam-mini", sd.url)
        _befriend(client, riley, sam, "sam")
        oid = client.post("/offerings", json={"host_id": _host_id(client, sam, "sam-mini"), "max_quota_gb": 20}, headers=sam).json()["id"]
        client.post(f"/offerings/{oid}/requests", json={"quota_gb": 10}, headers=riley)
        _decide(client, sam, _approvals(client, sam)[0]["id"])
        assert len(client.get("/grants", headers=riley).json()["held"]) == 1
        assert client.delete("/friends/sam", headers=riley).status_code == 200
        assert client.get("/grants", headers=riley).json()["held"] == []
        assert client.get("/friends", headers=sam).json()["friends"] == []


# --- Agent onboarding (docs/product/scenarios/agent-onboarding.md) -------------------
import re  # noqa: E402


def _onboard_two(client, sd_url):
    _, sam = _signup(client, "sam")
    _, riley = _signup(client, "riley")
    _pair(client, sam, "rk-mini", "sam-mini", sd_url)
    return sam, riley, _agent_token(client, sam), _agent_token(client, riley)


def test_onboarding_invite_flow_previews_then_acts(app_env):
    main, client = app_env
    notes = []
    main.notify = lambda user_id, text, buttons=None: notes.append(text)
    with client, FakeDaemon(respond_with=lambda b: _ok()) as sd:
        sam, riley, sam_agent, riley_agent = _onboard_two(client, sd.url)

        # Previews change nothing.
        plan = _mcp(client, sam_agent, "publish_offering", host="sam-mini", max_gb=20)
        assert plan.startswith("PREVIEW") and "can never read it" in plan
        assert client.get("/offerings", headers=sam).json()["mine"] == []
        assert "Done (offering" in _mcp(client, sam_agent, "publish_offering", host="sam-mini", max_gb=20, preview=False)
        assert _mcp(client, sam_agent, "create_invite", quota_gb=10, for_whom="Riley").startswith("PREVIEW")

        out = _mcp(client, sam_agent, "create_invite", quota_gb=10, for_whom="Riley", preview=False)
        code = re.search(r"CASPER-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", out).group(0)
        assert "agents.md" in out and code in out.split("Message for the person")[1]  # ready-to-send message carries both

        assert "you and sam become friends" in _mcp(client, riley_agent, "redeem_invite", code=code)
        assert client.get("/friends", headers=riley).json()["friends"] == []  # still just a preview
        done = _mcp(client, riley_agent, "redeem_invite", code=code, preview=False)
        assert "10.0 GB of mirror space on sam/sam-mini" in done and "offer sam mirror space in return" in done
        assert client.get("/friends", headers=riley).json()["friends"] == ["sam"]
        assert any("riley used your Casper invite" in t for t in notes)
        assert "already been used" in _mcp(client, riley_agent, "redeem_invite", code=code, preview=False)

        ledger = _mcp(client, sam_agent, "my_casper")
        assert "Friends: riley" in ledger and "mirror space for riley: 10.0 GB on sam-mini" in ledger
        assert "mirror space: 10.0 GB on sam/sam-mini" in _mcp(client, riley_agent, "my_casper")

        # Undo: cancelled invites stop working; ending space is one call.
        out2 = _mcp(client, sam_agent, "create_invite", quota_gb=5, preview=False)
        code2 = re.search(r"CASPER-[A-Z2-9-]{14}", out2).group(0)
        invite_id = re.search(r"\[invite (\d+)\]", _mcp(client, sam_agent, "my_casper")).group(1)
        assert "Cancelled" in _mcp(client, sam_agent, "revoke", kind="invite", id_or_name=invite_id)
        assert "cancelled" in _mcp(client, riley_agent, "redeem_invite", code=code2)
        grant_id = re.search(r"\[grant (\d+)\]", _mcp(client, sam_agent, "my_casper")).group(1)
        assert "Ended riley's" in _mcp(client, sam_agent, "revoke", kind="grant", id_or_name=grant_id)
        assert client.get("/grants", headers=riley).json()["held"] == []


def test_invites_reject_bad_quota_self_use_and_typos(app_env):
    main, client = app_env
    with client, FakeDaemon(respond_with=lambda b: _ok()) as sd:
        sam, riley, sam_agent, riley_agent = _onboard_two(client, sd.url)
        _mcp(client, sam_agent, "publish_offering", host="sam-mini", max_gb=5, preview=False)
        assert "Can't" in _mcp(client, sam_agent, "create_invite", quota_gb=50, preview=False)
        out = _mcp(client, sam_agent, "create_invite", quota_gb=1, preview=False)
        code = re.search(r"CASPER-[A-Z2-9-]{14}", out).group(0)
        assert "your own invite" in _mcp(client, sam_agent, "redeem_invite", code=code, preview=False)
        assert "isn't valid" in _mcp(client, riley_agent, "redeem_invite", code="CASPER-AAAA-BBBB-CCCC")
        # Codes are forgiving of case and spacing when read aloud or retyped.
        assert "PREVIEW" in _mcp(client, riley_agent, "redeem_invite", code=" " + code.lower() + " ")


def test_agent_relays_decisions_on_others_requests_but_never_its_own(app_env):
    main, client = app_env

    def respond(body):
        return 200, {"success": False, "cwd": "/", "stdout": "", "stderr": "", "tier": "ask"}

    with client, FakeDaemon(respond_with=respond) as sd:
        sam, riley, sam_agent, riley_agent = _onboard_two(client, sd.url)
        _mcp(client, riley_agent, "add_friend", username="sam")
        _mcp(client, sam_agent, "run_shell_command", host="sam-mini", positional_args=["brew", "upgrade"])

        listing = _mcp(client, sam_agent, "list_approvals")
        assert "from riley" in listing and "1 of the person's own requests" in listing
        own_id = next(p["id"] for p in _approvals(client, sam) if p["kind"] == "shell_command")
        friend_id = next(p["id"] for p in _approvals(client, sam) if p["kind"] == "friend_request")

        assert "can't be approved from their agent" in _mcp(client, sam_agent, "decide_approval", approval_id=own_id, approve=True)
        assert any(p["id"] == own_id for p in _approvals(client, sam))  # untouched
        assert "now friends" in _mcp(client, sam_agent, "decide_approval", approval_id=friend_id, approve=True)


def test_backup_push_preview_states_the_plan(app_env):
    main, client = app_env
    with client, FakeDaemon(respond_with=lambda b: _ok()) as sd, FakeDaemon(respond_with=lambda b: _ok()) as rd:
        sam, riley, sam_agent, riley_agent = _onboard_two(client, sd.url)
        _pair(client, riley, "rk-laptop", "riley-laptop", rd.url)
        _mcp(client, sam_agent, "publish_offering", host="sam-mini", max_gb=20, kind="backup", preview=False)
        code = re.search(r"CASPER-[A-Z2-9-]{14}", _mcp(client, sam_agent, "create_invite", quota_gb=10, preview=False)).group(0)
        _mcp(client, riley_agent, "redeem_invite", code=code, preview=False)
        out = _mcp(client, riley_agent, "backup_push", source_host="riley-laptop", path="~/Documents", dest_host="sam/sam-mini", preview=True)
        assert out.startswith("PREVIEW") and "sam can never read it" in out
        assert rd.requests == []  # nothing prepared


def test_onboarding_files_are_served_with_this_deployments_url(app_env):
    main, client = app_env
    md = client.get("/agents.md", headers={"host": "dev-auth.casperagent.dev", "x-forwarded-proto": "https"})
    assert md.status_code == 200
    assert "https://dev-auth.casperagent.dev/download/casper/macos" in md.text and "{{" not in md.text
    skill = client.get("/onboarding/skills/casper-mirror.md")
    assert skill.status_code == 200 and "recovery-kit" in skill.text and "Allow" in skill.text
    assert client.get("/onboarding/skills/..%2Fsecrets.md").status_code == 404

    import io
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(client.get("/onboarding.zip").content))
    names = set(z.namelist())
    assert {"casper/AGENTS.md", "casper/CLAUDE.md", "casper/.claude/skills/casper-setup/SKILL.md"} <= names
    assert "{{" not in z.read("casper/AGENTS.md").decode()


def test_app_download(app_env, tmp_path, monkeypatch):
    main, client = app_env
    monkeypatch.setattr(main, "DIST_DIR", str(tmp_path))
    assert client.get("/download/casper-macos.zip").status_code == 404
    (tmp_path / "Casper-macos.zip").write_bytes(b"PK zip")
    for path in ("/download/casper/macos", "/download/casper-macos.zip"):
        r = client.get(path)
        assert r.status_code == 200 and r.content == b"PK zip"
        assert r.headers["cache-control"] == "no-store"
    assert client.get("/agents.md").headers["cache-control"] == "no-store"


# --- Mirroring: validation ----------------------------------------------------------------
def test_catcher_on_a_mirrors_machine_is_refused(app_env):
    main, client = app_env

    def riley_daemon(body):
        if body["action"] == "mirror_folder_size":
            return _ok(path="/Users/riley/Documents", bytes=1000)
        return _ok()

    with client, FakeDaemon(respond_with=lambda b: _ok()) as sd, FakeDaemon(respond_with=riley_daemon) as rd:
        sam, riley, sam_agent, riley_agent = _onboard_two(client, sd.url)
        _pair(client, riley, "rk-laptop", "riley-laptop", rd.url)
        for kind in ("mirror", "catcher"):
            oid = re.search(r"offering (\d+)", _mcp(client, sam_agent, "publish_offering", host="sam-mini", max_gb=5, kind=kind, preview=False)).group(1)
            code = re.search(r"CASPER-[A-Z2-9-]{14}", _mcp(client, sam_agent, "create_invite", quota_gb=1, offering_id=int(oid), preview=False)).group(0)
            assert "Done" in _mcp(client, riley_agent, "redeem_invite", code=code, preview=False)
        out = _mcp(client, riley_agent, "mirror_folder", source_host="riley-laptop", path="Documents",
                   mirrors=["sam/sam-mini"], catcher="sam/sam-mini", preview=True)
        assert "already one of the mirrors" in out
        ok = _mcp(client, riley_agent, "mirror_folder", source_host="riley-laptop", path="Documents", mirrors=["sam/sam-mini"], preview=True)
        assert ok.startswith("PREVIEW") and "wait on this Mac" in ok
