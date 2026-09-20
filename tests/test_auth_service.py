import base64
import os
import sys
import tempfile

import pytest

AUTH_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "auth_service")

# Must match auth_service/models.py's own Pattern.blacklist
# default exactly -- a blank blacklist round-trips as this, not None.
BLACKLIST_MATCHES_NOTHING = r"[^\s\S]"


@pytest.fixture()
def client():
    """Fresh SQLite file + freshly imported app per test, so tests don't
    leak state into each other (main.py's rate limiter and db path are
    both set at import time)."""
    sys.path.insert(0, os.path.abspath(AUTH_SERVICE_DIR))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUTH_DB_PATH"] = os.path.join(tmp, "users.db")
        os.environ["STORAGE_ROOT"] = os.path.join(tmp, "storage")
        for mod in ("main", "db", "models"):
            sys.modules.pop(mod, None)
        import main as auth_main
        from fastapi.testclient import TestClient

        yield TestClient(auth_main.app)
    sys.path.remove(os.path.abspath(AUTH_SERVICE_DIR))
    for mod in ("main", "db", "models"):
        sys.modules.pop(mod, None)


def test_signup_then_duplicate(client):
    r = client.post("/signup", json={"username": "alice", "password": "hunter2pass"})
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "alice"
    assert body["token"]

    r2 = client.post("/signup", json={"username": "alice", "password": "different1"})
    assert r2.status_code == 409

    r3 = client.post("/signup", json={"username": "ALICE", "password": "different1"})
    assert r3.status_code == 409  # case-insensitive uniqueness


def test_login_wrong_password(client):
    client.post("/signup", json={"username": "bob", "password": "correct-horse"})
    r = client.post("/login", json={"username": "bob", "password": "wrong-password"})
    assert r.status_code == 401


def test_login_rotates_token(client):
    signup = client.post("/signup", json={"username": "carol", "password": "correct-horse"}).json()
    login = client.post("/login", json={"username": "carol", "password": "correct-horse"}).json()
    assert login["token"] != signup["token"]

    # old (signup) token is dead once login rotated it
    old_verify = client.post("/verify", headers={"Authorization": f"Bearer {signup['token']}"}).json()
    assert old_verify == {"valid": False, "username": None}

    new_verify = client.post("/verify", headers={"Authorization": f"Bearer {login['token']}"}).json()
    assert new_verify == {"valid": True, "username": "carol"}


def test_verify_unknown_token(client):
    r = client.post("/verify", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 200
    assert r.json() == {"valid": False, "username": None}


def test_revoke(client):
    signup = client.post("/signup", json={"username": "dave", "password": "correct-horse"}).json()
    token = signup["token"]
    assert client.post("/verify", headers={"Authorization": f"Bearer {token}"}).json()["valid"] is True

    revoke = client.post("/revoke", headers={"Authorization": f"Bearer {token}"})
    assert revoke.status_code == 200
    assert revoke.json() == {"revoked": True}

    assert client.post("/verify", headers={"Authorization": f"Bearer {token}"}).json()["valid"] is False

    # idempotent
    assert client.post("/revoke", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_signup_validation(client):
    assert client.post("/signup", json={"username": "ab", "password": "longenough1"}).status_code == 422
    assert client.post("/signup", json={"username": "validname", "password": "short"}).status_code == 422


def test_rate_limit(client):
    for i in range(10):
        client.post("/login", json={"username": "nobody", "password": "whatever"})
    r = client.post("/login", json={"username": "nobody", "password": "whatever"})
    assert r.status_code == 429


def _signup(client, username):
    return client.post("/signup", json={"username": username, "password": "correct-horse"}).json()


def test_pair_then_list(client):
    signup = _signup(client, "erin")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    pair = client.post(
        "/hosts/pair",
        json={"routing_key": "rk-erin-1", "hostname": "erins-mac", "label": "Erin's Mac"},
        headers=headers,
    )
    assert pair.status_code == 201
    body = pair.json()
    assert body["label"] == "Erin's Mac"
    assert body["device_token"] and body["command_key"]

    # not yet reported reachable -- listed, but disconnected, no cwd yet
    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["label"] == "Erin's Mac"
    assert hosts[0]["connected"] is False
    assert hosts[0]["cwd"] == ""

    device_headers = {"Authorization": f"Bearer {body['device_token']}"}
    assert client.post("/hosts/verify", headers=device_headers).json() == {"valid": True}

    presence = client.post(
        "/hosts/presence",
        json={"local_agent_url": "https://relay.example/agent/erin", "cwd": "/Users/erin"},
        headers=device_headers,
    )
    assert presence.status_code == 200
    assert presence.json()["cwd"] == "/Users/erin"

    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert hosts[0]["connected"] is True
    assert hosts[0]["local_agent_url"] == "https://relay.example/agent/erin"
    assert hosts[0]["command_key"] == body["command_key"]
    assert hosts[0]["cwd"] == "/Users/erin"

    # toggling reachability off clears cwd too, same as local_agent_url --
    # both reflect "nothing currently reachable/known", not "still
    # remembered while disconnected"
    cleared = client.delete("/hosts/presence", headers=device_headers)
    assert cleared.json()["cwd"] == ""
    assert client.get("/hosts", headers=headers).json()["hosts"][0]["cwd"] == ""


def test_pairing_survives_an_auth_service_restart(client):
    """The actual fix host_pairings exists for: a device_token issued
    before a restart must still work after one, with no re-pairing
    needed -- only the live reachability state (local_agent_url/cwd,
    genuinely ephemeral) should reset. Simulates the restart by clearing
    main's own in-memory `_live` dict directly (what a real process
    restart empties) without touching the SQLite file (what actually
    persists) -- the precise mechanism at play, not just an approximation
    of it."""
    import main as auth_main

    signup = _signup(client, "gina2")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = client.post(
        "/hosts/pair", json={"routing_key": "rk-gina2-1", "hostname": "ginas-mac"}, headers=headers
    ).json()
    device_headers = {"Authorization": f"Bearer {pair['device_token']}"}
    client.post(
        "/hosts/presence",
        json={"local_agent_url": "https://relay.example/agent/gina2", "cwd": "/Users/gina2"},
        headers=device_headers,
    )
    assert client.get("/hosts", headers=headers).json()["hosts"][0]["connected"] is True

    # Simulate the restart -- identity (host_pairings, in SQLite) survives;
    # live reachability (in-memory) doesn't.
    auth_main._live.clear()

    # The device_token still works with zero re-pairing -- confirms
    # verify_host doesn't 401 something a real daemon's own
    # resumeSession()-on-startup would have relied on.
    assert client.post("/hosts/verify", headers=device_headers).json() == {"valid": True}

    # Reachability genuinely reset -- "connected" reflects live state, and
    # a real daemon would report presence again immediately after
    # resuming anyway.
    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert hosts[0]["connected"] is False
    assert hosts[0]["cwd"] == ""

    # A fresh presence report (exactly what a real daemon does right after
    # resuming) brings it back to fully connected, no re-pair involved.
    client.post(
        "/hosts/presence",
        json={"local_agent_url": "https://relay.example/agent/gina2", "cwd": "/Users/gina2"},
        headers=device_headers,
    )
    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert hosts[0]["connected"] is True
    assert hosts[0]["cwd"] == "/Users/gina2"


def test_pairing_the_same_host_to_a_second_account_succeeds_even_when_not_live(client):
    """One physical daemon (routing_key) can be paired to several accounts
    at once -- confirmed here from the PERSISTED pairing state, not just a
    live in-memory one, since that's the actual mechanism (host_pairings
    survives a restart; see test_pairing_survives_an_auth_service_restart).
    Both pairings must coexist as independent rows, each with its own
    device_token, neither disturbing the other."""
    a = _signup(client, "howard")
    b = _signup(client, "iris2")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}

    paired_a = client.post("/hosts/pair", json={"routing_key": "rk-shared-1"}, headers=headers_a)
    assert paired_a.status_code == 201

    paired_b = client.post("/hosts/pair", json={"routing_key": "rk-shared-1"}, headers=headers_b)
    assert paired_b.status_code == 201
    assert paired_b.json()["device_token"] != paired_a.json()["device_token"]

    # Both device_tokens remain independently valid -- pairing B never
    # revoked or overwrote A's row.
    device_headers_a = {"Authorization": f"Bearer {paired_a.json()['device_token']}"}
    device_headers_b = {"Authorization": f"Bearer {paired_b.json()['device_token']}"}
    assert client.post("/hosts/verify", headers=device_headers_a).json() == {"valid": True}
    assert client.post("/hosts/verify", headers=device_headers_b).json() == {"valid": True}


def test_pair_is_idempotent_for_the_same_user(client):
    signup = _signup(client, "frank")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    first = client.post(
        "/hosts/pair", json={"routing_key": "rk-frank-1", "hostname": "franks-pc", "label": "Frank's PC"},
        headers=headers,
    ).json()
    second = client.post(
        "/hosts/pair", json={"routing_key": "rk-frank-1", "hostname": "franks-pc"}, headers=headers
    ).json()

    assert second["host_id"] == first["host_id"]
    assert second["label"] == "Frank's PC"  # existing label untouched, not re-prompted
    assert second["device_token"] != first["device_token"]  # credentials rotated

    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert len(hosts) == 1  # no duplicate row


def test_second_user_pairing_a_taken_host_succeeds_independently(client):
    a = _signup(client, "gina")
    b = _signup(client, "hank")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}

    pair_a = client.post(
        "/hosts/pair", json={"routing_key": "rk-shared", "hostname": "shared-box"}, headers=headers_a
    ).json()
    pair_b = client.post(
        "/hosts/pair", json={"routing_key": "rk-shared", "hostname": "shared-box"}, headers=headers_b
    )
    assert pair_b.status_code == 201
    pair_b = pair_b.json()

    # a's session is undisturbed by b's pairing to the same routing_key
    device_headers_a = {"Authorization": f"Bearer {pair_a['device_token']}"}
    device_headers_b = {"Authorization": f"Bearer {pair_b['device_token']}"}
    assert client.post("/hosts/verify", headers=device_headers_a).json() == {"valid": True}
    assert client.post("/hosts/verify", headers=device_headers_b).json() == {"valid": True}

    # Each account sees only its own pairing row -- never the other's
    # device_token/command_key or pairing existence.
    hosts_a = client.get("/hosts", headers=headers_a).json()["hosts"]
    hosts_b = client.get("/hosts", headers=headers_b).json()["hosts"]
    assert len(hosts_a) == 1 and len(hosts_b) == 1
    assert hosts_a[0]["command_key"] != hosts_b[0]["command_key"]

    # Unpairing one account's device_token never disturbs the other's.
    client.post("/hosts/unpair", headers=device_headers_a)
    assert client.post("/hosts/verify", headers=device_headers_a).json() == {"valid": False}
    assert client.post("/hosts/verify", headers=device_headers_b).json() == {"valid": True}
    assert client.get("/hosts", headers=headers_b).json()["hosts"][0]["connected"] is False


def test_pairing_to_a_shared_host_keeps_policy_layers_fully_isolated(client):
    """The whole point of supporting multiple accounts on one daemon: each
    account's own policy layers must never leak to another account paired
    to the exact same physical host_id."""
    a = _signup(client, "juniper")
    b = _signup(client, "kelvin")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}

    pair_a = client.post(
        "/hosts/pair", json={"routing_key": "rk-shared-2", "hostname": "shared-box-2"}, headers=headers_a
    ).json()
    pair_b = client.post(
        "/hosts/pair", json={"routing_key": "rk-shared-2", "hostname": "shared-box-2"}, headers=headers_b
    ).json()
    assert pair_a["host_id"] == pair_b["host_id"]  # same physical machine

    layer = _create_policy_layer(client, headers_a, name="juniper-only").json()
    _add_rule(client, headers_a, layer["id"], [{"whitelist": "^npm$"}])
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair_a['host_id']}", headers=headers_a)

    device_headers_a = {"Authorization": f"Bearer {pair_a['device_token']}"}
    device_headers_b = {"Authorization": f"Bearer {pair_b['device_token']}"}
    assert len(client.get("/hosts/policy-layers", headers=device_headers_a).json()["policy_layers"]) == 1
    assert client.get("/hosts/policy-layers", headers=device_headers_b).json()["policy_layers"] == []

    host_a = client.get("/hosts", headers=headers_a).json()["hosts"][0]
    host_b = client.get("/hosts", headers=headers_b).json()["hosts"][0]
    assert len(host_a["policy_layers"]) == 1
    assert host_b["policy_layers"] == []


def test_presence_on_a_shared_host_is_visible_to_every_paired_account(client):
    """_live is genuinely machine-level -- a presence report from the one
    real daemon process should be visible under every account paired to
    it, not just whichever one happened to report it."""
    a = _signup(client, "liam")
    b = _signup(client, "mira")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}

    pair_a = client.post(
        "/hosts/pair", json={"routing_key": "rk-shared-3"}, headers=headers_a
    ).json()
    client.post("/hosts/pair", json={"routing_key": "rk-shared-3"}, headers=headers_b)

    device_headers_a = {"Authorization": f"Bearer {pair_a['device_token']}"}
    client.post(
        "/hosts/presence",
        json={"local_agent_url": "https://relay.example/agent/shared-3", "cwd": "/Users/shared"},
        headers=device_headers_a,
    )

    assert client.get("/hosts", headers=headers_a).json()["hosts"][0]["connected"] is True
    assert client.get("/hosts", headers=headers_b).json()["hosts"][0]["connected"] is True


def test_unpair_preserves_user_hosts(client):
    signup = _signup(client, "ivy")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    pair = client.post(
        "/hosts/pair", json={"routing_key": "rk-ivy-1", "hostname": "ivys-mac"}, headers=headers
    ).json()
    device_headers = {"Authorization": f"Bearer {pair['device_token']}"}

    unpair = client.post("/hosts/unpair", headers=device_headers)
    assert unpair.status_code == 200
    assert unpair.json() == {"revoked": True}

    # device_token is dead now
    assert client.post("/hosts/verify", headers=device_headers).json() == {"valid": False}

    # but the host itself is still remembered, just disconnected
    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["connected"] is False


def test_signout_all_clears_attachment_not_history(client):
    signup = _signup(client, "jack")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    pair = client.post(
        "/hosts/pair", json={"routing_key": "rk-jack-1", "hostname": "jacks-mac"}, headers=headers
    ).json()
    device_headers = {"Authorization": f"Bearer {pair['device_token']}"}
    client.post(
        "/hosts/presence",
        json={"local_agent_url": "https://relay.example/agent/jack", "cwd": "/tmp/ws"},
        headers=device_headers,
    )

    signout = client.post("/hosts/signout-all", headers=headers)
    assert signout.status_code == 200
    assert signout.json() == {"signed_out_hosts": 1}

    assert client.post("/hosts/verify", headers=device_headers).json() == {"valid": False}

    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["connected"] is False


def test_default_environment_assignment_with_zero_existing(client):
    signup = _signup(client, "karen")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    pair = client.post(
        "/hosts/pair", json={"routing_key": "rk-karen-1", "hostname": "karens-mac"}, headers=headers
    ).json()

    envs = client.get("/environments", headers=headers).json()["environments"]
    assert len(envs) == 1
    assert envs[0]["name"] == "Default"
    assert envs[0]["host_ids"] == [pair["host_id"]]


def test_default_environment_assignment_with_one_existing(client):
    signup = _signup(client, "larry")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    env = client.post("/environments", json={"name": "Home"}, headers=headers).json()
    pair = client.post(
        "/hosts/pair", json={"routing_key": "rk-larry-1", "hostname": "larrys-mac"}, headers=headers
    ).json()

    envs = client.get("/environments", headers=headers).json()["environments"]
    assert len(envs) == 1
    assert envs[0]["id"] == env["id"]
    assert envs[0]["host_ids"] == [pair["host_id"]]


def test_default_environment_assignment_with_multiple_existing(client):
    signup = _signup(client, "mona")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    client.post("/environments", json={"name": "Home"}, headers=headers)
    client.post("/environments", json={"name": "Work"}, headers=headers)
    client.post(
        "/hosts/pair", json={"routing_key": "rk-mona-1", "hostname": "monas-mac"}, headers=headers
    )

    envs = client.get("/environments", headers=headers).json()["environments"]
    assert all(e["host_ids"] == [] for e in envs)  # left unassigned, ambiguous


def test_environment_crud(client):
    signup = _signup(client, "nate")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    pair = client.post(
        "/hosts/pair", json={"routing_key": "rk-nate-1", "hostname": "nates-mac"}, headers=headers
    ).json()
    env = client.post("/environments", json={"name": "Staging"}, headers=headers).json()
    assert env["name"] == "Staging"
    assert env["host_ids"] == []

    dup = client.post("/environments", json={"name": "staging"}, headers=headers)  # case-insensitive
    assert dup.status_code == 409

    added = client.put(f"/environments/{env['id']}/hosts/{pair['host_id']}", headers=headers).json()
    assert added["host_ids"] == [pair["host_id"]]

    renamed = client.patch(f"/environments/{env['id']}", json={"name": "Prod"}, headers=headers).json()
    assert renamed["name"] == "Prod"

    removed = client.delete(f"/environments/{env['id']}/hosts/{pair['host_id']}", headers=headers).json()
    assert removed["host_ids"] == []

    deleted = client.delete(f"/environments/{env['id']}", headers=headers)
    assert deleted.status_code == 200
    remaining = [e["id"] for e in client.get("/environments", headers=headers).json()["environments"]]
    assert env["id"] not in remaining


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def test_storage_upload_list_download_delete(client):
    signup = _signup(client, "olga")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    assert client.get("/storage", headers=headers).json() == {
        "files": [], "total_bytes": 0, "cap_bytes": 1024 * 1024 * 1024,
    }

    uploaded = client.post(
        "/storage", json={"filename": "notes.txt", "content": _b64("hello world")}, headers=headers
    )
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["filename"] == "notes.txt"
    assert body["size"] == len(b"hello world")

    listing = client.get("/storage", headers=headers).json()
    assert listing["total_bytes"] == len(b"hello world")
    assert [f["filename"] for f in listing["files"]] == ["notes.txt"]

    downloaded = client.get("/storage/notes.txt", headers=headers).json()
    assert base64.b64decode(downloaded["content"]) == b"hello world"

    # re-uploading the same filename overwrites, not duplicates
    client.post("/storage", json={"filename": "notes.txt", "content": _b64("bye")}, headers=headers)
    listing = client.get("/storage", headers=headers).json()
    assert len(listing["files"]) == 1
    assert listing["total_bytes"] == len(b"bye")

    deleted = client.delete("/storage/notes.txt", headers=headers)
    assert deleted.status_code == 200
    assert client.get("/storage", headers=headers).json()["files"] == []

    # idempotent
    assert client.delete("/storage/notes.txt", headers=headers).status_code == 200

    assert client.get("/storage/nope.txt", headers=headers).status_code == 404


def test_storage_rejects_path_traversal(client):
    signup = _signup(client, "pete")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    result = client.post(
        "/storage", json={"filename": "../../etc/passwd", "content": _b64("x")}, headers=headers
    )
    assert result.status_code == 201  # basename() collapses it to a bare "passwd"
    assert result.json()["filename"] == "passwd"
    assert client.get("/storage", headers=headers).json()["files"][0]["filename"] == "passwd"


def test_storage_rejects_invalid_base64(client):
    signup = _signup(client, "quinn")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    result = client.post("/storage", json={"filename": "x.bin", "content": "not valid base64!!"}, headers=headers)
    assert result.status_code == 400


def test_storage_enforces_per_user_cap(client):
    import main as auth_main  # already imported by the client fixture; shrink the cap rather than upload 1GB+

    signup = _signup(client, "rosa")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    original_cap = auth_main.STORAGE_CAP_BYTES
    auth_main.STORAGE_CAP_BYTES = 100
    try:
        ok = client.post("/storage", json={"filename": "small.bin", "content": _b64("x" * 50)}, headers=headers)
        assert ok.status_code == 201
        too_big = client.post(
            "/storage", json={"filename": "big.bin", "content": _b64("x" * 100)}, headers=headers
        )
        assert too_big.status_code == 413
        assert [f["filename"] for f in client.get("/storage", headers=headers).json()["files"]] == ["small.bin"]
    finally:
        auth_main.STORAGE_CAP_BYTES = original_cap


def test_storage_is_per_user(client):
    a = _signup(client, "sam")
    b = _signup(client, "tina")
    client.post(
        "/storage", json={"filename": "a-only.txt", "content": _b64("secret")},
        headers={"Authorization": f"Bearer {a['token']}"},
    )
    assert client.get("/storage", headers={"Authorization": f"Bearer {b['token']}"}).json()["files"] == []
    assert client.get("/storage/a-only.txt", headers={"Authorization": f"Bearer {b['token']}"}).status_code == 404


def test_profile_defaults_and_get_creates_row(client):
    signup = _signup(client, "uma")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = client.get("/profile", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == ""
    assert body["email_notifications_enabled"] is False
    assert body["sms_number"] == ""
    assert body["sms_notifications_enabled"] is False
    for field in (
        "allow_configure_command_sets",
        "allow_configure_apps",
        "allow_configure_hosts",
        "allow_configure_environments",
        "allow_configure_local_agents",
    ):
        assert body[field] is False


def test_profile_partial_update_merges(client):
    signup = _signup(client, "vince")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r1 = client.patch("/profile", json={"email": "vince@example.com"}, headers=headers)
    assert r1.status_code == 200
    assert r1.json()["email"] == "vince@example.com"
    assert r1.json()["email_notifications_enabled"] is False

    r2 = client.patch("/profile", json={"email_notifications_enabled": True}, headers=headers)
    assert r2.status_code == 200
    # Previously-set email survives an update that doesn't mention it.
    assert r2.json()["email"] == "vince@example.com"
    assert r2.json()["email_notifications_enabled"] is True


def test_profile_rejects_invalid_email(client):
    signup = _signup(client, "wendy")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = client.patch("/profile", json={"email": "not-an-email"}, headers=headers)
    assert r.status_code == 422
    # A clean string, not FastAPI's default {"detail": [{"msg": ...}]} shape
    # -- see main.py's _validation_error_handler.
    assert r.json()["detail"] == "Enter a valid email address."


def test_profile_normalizes_valid_phone_number(client):
    signup = _signup(client, "xavier")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = client.patch("/profile", json={"sms_number": "1 (555) 234-5678"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["sms_number"] == "(555) 234-5678"


def test_profile_rejects_invalid_phone_number(client):
    signup = _signup(client, "yara")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = client.patch("/profile", json={"sms_number": "12345"}, headers=headers)
    assert r.status_code == 422
    assert r.json()["detail"] == "Enter a 10-digit phone number."


def test_profile_permission_checkboxes_default_off_and_toggle(client):
    signup = _signup(client, "zack")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = client.patch("/profile", json={"allow_configure_hosts": True}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["allow_configure_hosts"] is True
    assert body["allow_configure_environments"] is False


def test_profile_is_per_user(client):
    a = _signup(client, "amy2")
    b = _signup(client, "bob2")
    client.patch(
        "/profile", json={"email": "amy@example.com"}, headers={"Authorization": f"Bearer {a['token']}"}
    )
    b_profile = client.get("/profile", headers={"Authorization": f"Bearer {b['token']}"}).json()
    assert b_profile["email"] == ""


def _create_policy_layer(client, headers, name="npm scripts"):
    return client.post("/policy-layers", json={"name": name}, headers=headers)


def _add_rule(client, headers, layer_id, positional_constraints, option_constraints=None, tier="ask", cwd=None):
    body = {
        "positional_constraints": positional_constraints,
        "option_constraints": option_constraints or [],
        "tier": tier,
    }
    if cwd is not None:
        body["cwd"] = cwd
    return client.post(f"/policy-layers/{layer_id}/rules", json=body, headers=headers)


def test_policy_layer_crud(client):
    signup = _signup(client, "carol")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    created = _create_policy_layer(client, headers)
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "npm scripts"
    assert body["rules"] == []
    assert body["host_ids"] == []

    dup = _create_policy_layer(client, headers, name="NPM SCRIPTS")  # case-insensitive
    assert dup.status_code == 409

    listed = client.get("/policy-layers", headers=headers).json()["policy_layers"]
    assert [c["id"] for c in listed] == [body["id"]]

    renamed = client.patch(f"/policy-layers/{body['id']}", json={"name": "npm"}, headers=headers)
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "npm"

    deleted = client.delete(f"/policy-layers/{body['id']}", headers=headers)
    assert deleted.status_code == 200
    assert client.get("/policy-layers", headers=headers).json()["policy_layers"] == []


def test_policy_layer_rule_crud(client):
    signup = _signup(client, "dave2")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()

    created = _add_rule(
        client, headers, layer["id"], [{"whitelist": "^npm$"}, {"whitelist": "^run$"}], tier="allow"
    )
    assert created.status_code == 201
    rule = created.json()
    assert rule["position"] == 0
    assert rule["positional_constraints"] == [
        {"whitelist": "^npm$", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
        {"whitelist": "^run$", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
    ]
    assert rule["tier"] == "allow"

    updated = client.patch(
        f"/policy-layers/{layer['id']}/rules/{rule['id']}", json={"tier": "ask"}, headers=headers
    )
    assert updated.status_code == 200
    assert updated.json()["tier"] == "ask"
    # untouched fields survive a partial update
    assert updated.json()["positional_constraints"] == rule["positional_constraints"]

    deleted = client.delete(f"/policy-layers/{layer['id']}/rules/{rule['id']}", headers=headers)
    assert deleted.status_code == 200
    assert client.get("/policy-layers", headers=headers).json()["policy_layers"][0]["rules"] == []


def test_policy_layer_rule_cwd_and_path_resolution_round_trip(client):
    signup = _signup(client, "dave2b")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()

    created = _add_rule(
        client,
        headers,
        layer["id"],
        [{}, {"whitelist": "^safe.*", "path_resolution": "."}],
        tier="allow",
        cwd={"whitelist": "^/home/.+"},
    )
    assert created.status_code == 201
    rule = created.json()
    assert rule["cwd"] == {"whitelist": "^/home/.+", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""}
    assert rule["positional_constraints"][1]["path_resolution"] == "."

    # A partial update that doesn't touch cwd leaves it untouched (the
    # merge-then-revalidate path, same as every other field).
    updated = client.patch(
        f"/policy-layers/{layer['id']}/rules/{rule['id']}", json={"tier": "ask"}, headers=headers
    )
    assert updated.status_code == 200
    assert updated.json()["cwd"] == rule["cwd"]

    # An update that DOES touch cwd replaces it.
    recwd = client.patch(
        f"/policy-layers/{layer['id']}/rules/{rule['id']}",
        json={"cwd": {"whitelist": "^/tmp/.+"}},
        headers=headers,
    )
    assert recwd.status_code == 200
    assert recwd.json()["cwd"]["whitelist"] == "^/tmp/.+"


def test_policy_layer_rule_reorder(client):
    signup = _signup(client, "erin3")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()
    r1 = _add_rule(client, headers, layer["id"], [{"whitelist": "^a$"}]).json()
    r2 = _add_rule(client, headers, layer["id"], [{"whitelist": "^b$"}]).json()
    r3 = _add_rule(client, headers, layer["id"], [{"whitelist": "^c$"}]).json()

    reordered = client.put(
        f"/policy-layers/{layer['id']}/rules/reorder",
        json={"rule_ids": [r3["id"], r1["id"], r2["id"]]},
        headers=headers,
    )
    assert reordered.status_code == 200
    assert [r["id"] for r in reordered.json()["rules"]] == [r3["id"], r1["id"], r2["id"]]
    assert [r["position"] for r in reordered.json()["rules"]] == [0, 1, 2]


def test_policy_layer_rule_reorder_rejects_non_permutation(client):
    signup = _signup(client, "frank3")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()
    r1 = _add_rule(client, headers, layer["id"], [{"whitelist": "^a$"}]).json()
    _add_rule(client, headers, layer["id"], [{"whitelist": "^b$"}])

    missing_one = client.put(
        f"/policy-layers/{layer['id']}/rules/reorder", json={"rule_ids": [r1["id"]]}, headers=headers
    )
    assert missing_one.status_code == 400

    unknown_id = client.put(
        f"/policy-layers/{layer['id']}/rules/reorder", json={"rule_ids": [r1["id"], 999999]}, headers=headers
    )
    assert unknown_id.status_code == 400


def test_policy_layer_rule_accepts_empty_positional_constraints(client):
    # Position 0 (the binary) is optional, same as every other position --
    # an empty list means the rule doesn't constrain the binary or any
    # argument at all (still subject to whatever option_constraints say).
    signup = _signup(client, "gina3")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()
    created = _add_rule(client, headers, layer["id"], [])
    assert created.status_code == 201
    assert created.json()["positional_constraints"] == []


def test_policy_layer_rule_accepts_blank_pattern_at_any_position(client):
    signup = _signup(client, "hank3")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()
    # A blank pattern at position 0 (any binary) and mid-list (position 1
    # not required, position 2 still constrained) both accepted -- no "*"
    # sentinel needed, a missing value is matched as "" against the blank
    # pattern's empty whitelist/blacklist, which matches everything.
    created = _add_rule(client, headers, layer["id"], [{}, {}, {"whitelist": "^build$"}])
    assert created.status_code == 201
    assert created.json()["positional_constraints"] == [
        {"whitelist": "", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
        {"whitelist": "", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
        {"whitelist": "^build$", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
    ]


def test_policy_layer_rule_positional_value_not_allowed(client):
    # No dedicated "position must be absent" concept -- a missing
    # positional value is matched as "", same as a valueless option, so
    # "^$" works identically here: it requires the position be either
    # absent or an explicit empty string.
    signup = _signup(client, "hank3b")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()
    created = _add_rule(client, headers, layer["id"], [{"whitelist": "^rm$"}, {"whitelist": "^$"}])
    assert created.status_code == 201
    assert created.json()["positional_constraints"] == [
        {"whitelist": "^rm$", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
        {"whitelist": "^$", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
    ]


def test_policy_layer_rule_rejects_invalid_regex(client):
    signup = _signup(client, "kevin1")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()
    r = _add_rule(client, headers, layer["id"], [{"whitelist": "["}])
    assert r.status_code == 422
    assert "Invalid regex" in r.json()["detail"]


def test_policy_layer_rule_option_pattern_states_round_trip(client):
    # There's only ever one pattern shape now -- {whitelist, blacklist},
    # both always real strings (never None) -- not a three-way choice. A
    # blank pattern (default whitelist=""/blacklist=BLACKLIST_MATCHES_NOTHING)
    # means "any/no value accepted"; a supplied option with no value is
    # matched as "" at enforcement time, so "^$" as the whitelist means
    # "must be present with NO value" without needing a dedicated state.
    signup = _signup(client, "laura1")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()
    created = _add_rule(
        client,
        headers,
        layer["id"],
        [{}],
        option_constraints=[
            {"long": "force", "pattern": {"whitelist": "^$"}},  # must be present, no value
            {"short": "v", "pattern": {}},  # must be present, any/no value
            {"long": "output", "pattern": {"whitelist": ".+"}},  # must be present, value matching
        ],
    )
    assert created.status_code == 201
    options = created.json()["option_constraints"]
    assert options[0] == {
        "short": None,
        "long": "force",
        "pattern": {"whitelist": "^$", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
    }
    assert options[1] == {
        "short": "v",
        "long": None,
        "pattern": {"whitelist": "", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
    }
    assert options[2] == {
        "short": None,
        "long": "output",
        "pattern": {"whitelist": ".+", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
    }


def test_policy_layer_option_constraint_requires_short_or_long(client):
    signup = _signup(client, "mallory1")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers).json()
    r = _add_rule(client, headers, layer["id"], [{}], option_constraints=[{}])
    assert r.status_code == 422


def test_policy_layer_host_attachment_and_hosts_listing(client):
    signup = _signup(client, "erin2")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = client.post(
        "/hosts/pair", json={"routing_key": "rk-erin2-1", "hostname": "erins-mac"}, headers=headers
    ).json()
    layer = _create_policy_layer(client, headers).json()
    _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}])

    attached = client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers).json()
    assert attached["host_ids"] == [pair["host_id"]]

    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    (host,) = [h for h in hosts if h["host_id"] == pair["host_id"]]
    assert len(host["policy_layers"]) == 1
    assert host["policy_layers"][0]["name"] == "npm scripts"
    assert len(host["policy_layers"][0]["rules"]) == 1

    detached = client.delete(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers).json()
    assert detached["host_ids"] == []
    hosts_after = client.get("/hosts", headers=headers).json()["hosts"]
    (host_after,) = [h for h in hosts_after if h["host_id"] == pair["host_id"]]
    assert host_after["policy_layers"] == []


def test_policy_layer_daemon_facing_fetch(client):
    signup = _signup(client, "frank2")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair_a = client.post(
        "/hosts/pair", json={"routing_key": "rk-frank2-a", "hostname": "a"}, headers=headers
    ).json()
    pair_b = client.post(
        "/hosts/pair", json={"routing_key": "rk-frank2-b", "hostname": "b"}, headers=headers
    ).json()
    layer = _create_policy_layer(client, headers).json()
    r1 = _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}, {"whitelist": "^run$"}]).json()
    r2 = _add_rule(client, headers, layer["id"], [{}], tier="deny").json()
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair_a['host_id']}", headers=headers)

    device_headers_a = {"Authorization": f"Bearer {pair_a['device_token']}"}
    device_headers_b = {"Authorization": f"Bearer {pair_b['device_token']}"}

    fetched_a = client.get("/hosts/policy-layers", headers=device_headers_a).json()
    assert len(fetched_a["policy_layers"]) == 1
    fetched_rules = fetched_a["policy_layers"][0]["rules"]
    assert [r["id"] for r in fetched_rules] == [r1["id"], r2["id"]]  # rule order survives
    assert fetched_rules[0]["positional_constraints"] == [
        {"whitelist": "^npm$", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
        {"whitelist": "^run$", "blacklist": BLACKLIST_MATCHES_NOTHING, "path_resolution": ""},
    ]

    # Not attached to host B -- device_token B sees nothing.
    fetched_b = client.get("/hosts/policy-layers", headers=device_headers_b).json()
    assert fetched_b["policy_layers"] == []

    no_auth = client.get("/hosts/policy-layers")
    assert no_auth.status_code == 401


def test_policy_layer_is_per_user(client):
    a = _signup(client, "gina2")
    b = _signup(client, "hank2")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}
    _create_policy_layer(client, headers_a)
    assert client.get("/policy-layers", headers=headers_b).json()["policy_layers"] == []


def test_policy_layer_ownership_enforced(client):
    a = _signup(client, "ivan2")
    b = _signup(client, "judy2")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}
    layer = _create_policy_layer(client, headers_a).json()
    rule = _add_rule(client, headers_a, layer["id"], [{"whitelist": "^npm$"}]).json()

    assert client.patch(f"/policy-layers/{layer['id']}", json={"name": "x"}, headers=headers_b).status_code == 404
    assert client.delete(f"/policy-layers/{layer['id']}", headers=headers_b).status_code == 404
    assert (
        client.patch(
            f"/policy-layers/{layer['id']}/rules/{rule['id']}", json={"tier": "allow"}, headers=headers_b
        ).status_code
        == 404
    )
    assert client.delete(f"/policy-layers/{layer['id']}/rules/{rule['id']}", headers=headers_b).status_code == 404


def _pending_approval_body(**overrides):
    body = {"template_name": "npm scripts", "binary": "npm", "args": "run build", "host_label": "Erin's Mac"}
    body.update(overrides)
    return body


def test_attended_host_set_and_get(client):
    signup = _signup(client, "kim1")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    assert client.get("/users/me/attended-host", headers=headers).json() == {"host_id": None, "label": None}

    unowned = client.put("/users/me/attended-host", json={"host_id": 999}, headers=headers)
    assert unowned.status_code == 404

    pair = client.post("/hosts/pair", json={"routing_key": "rk-kim1-1", "hostname": "kims-mac"}, headers=headers).json()
    set_result = client.put("/users/me/attended-host", json={"host_id": pair["host_id"]}, headers=headers)
    assert set_result.status_code == 200
    assert set_result.json() == {"host_id": pair["host_id"], "label": pair["label"]}
    assert client.get("/users/me/attended-host", headers=headers).json()["host_id"] == pair["host_id"]

    cleared = client.delete("/users/me/attended-host", headers=headers)
    assert cleared.status_code == 200
    assert client.get("/users/me/attended-host", headers=headers).json() == {"host_id": None, "label": None}


def test_attended_host_cleared_on_forget_host(client):
    signup = _signup(client, "kim2")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = client.post("/hosts/pair", json={"routing_key": "rk-kim2-1", "hostname": "kims-pc"}, headers=headers).json()
    client.put("/users/me/attended-host", json={"host_id": pair["host_id"]}, headers=headers)

    client.delete(f"/hosts/{pair['host_id']}", headers=headers)

    assert client.get("/users/me/attended-host", headers=headers).json() == {"host_id": None, "label": None}


def test_pending_approval_end_to_end(client):
    signup = _signup(client, "liam1")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair_a = client.post("/hosts/pair", json={"routing_key": "rk-liam1-a", "hostname": "a"}, headers=headers).json()
    pair_b = client.post("/hosts/pair", json={"routing_key": "rk-liam1-b", "hostname": "b"}, headers=headers).json()
    client.put("/users/me/attended-host", json={"host_id": pair_a["host_id"]}, headers=headers)

    submitted = client.post("/hosts/pending-approvals", json=_pending_approval_body(), headers=headers)
    assert submitted.status_code == 201
    approval_id = submitted.json()["approval_id"]

    device_headers_a = {"Authorization": f"Bearer {pair_a['device_token']}"}
    device_headers_b = {"Authorization": f"Bearer {pair_b['device_token']}"}

    # B isn't the attended host -- its long-poll sees nothing (already
    # present, so this resolves on the first check, no real wait).
    seen_by_b = client.get("/hosts/pending-approvals", headers=device_headers_b).json()
    assert seen_by_b["pending_approvals"] == []

    seen_by_a = client.get("/hosts/pending-approvals", headers=device_headers_a).json()
    assert len(seen_by_a["pending_approvals"]) == 1
    record = seen_by_a["pending_approvals"][0]
    assert record["id"] == approval_id
    assert record["binary"] == "npm"
    assert record["args"] == "run build"
    assert record["decision"] is None

    decided = client.post(
        f"/hosts/pending-approvals/{approval_id}/decision", json={"decision": "allow"}, headers=device_headers_a
    )
    assert decided.status_code == 200
    assert decided.json()["decision"] == "allow"

    resolved = client.get(f"/hosts/pending-approvals/{approval_id}", headers=headers)
    assert resolved.status_code == 200
    assert resolved.json()["decision"] == "allow"

    # A second decision is a no-op read of the already-settled state, not
    # an error -- "whichever answers first wins".
    redundant = client.post(
        f"/hosts/pending-approvals/{approval_id}/decision", json={"decision": "deny"}, headers=device_headers_a
    )
    assert redundant.json()["decision"] == "allow"


def test_pending_approval_times_out_with_no_attended_host(client):
    import main as auth_main

    auth_main._LONG_POLL_SECONDS = 0.2  # keep the test fast; fresh module per test, nothing to restore

    signup = _signup(client, "liam2")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = client.post("/hosts/pair", json={"routing_key": "rk-liam2-a", "hostname": "a"}, headers=headers).json()
    client.post("/hosts/pending-approvals", json=_pending_approval_body(), headers=headers)

    device_headers = {"Authorization": f"Bearer {pair['device_token']}"}
    result = client.get("/hosts/pending-approvals", headers=device_headers)
    assert result.json()["pending_approvals"] == []


def test_pending_approval_decision_requires_currently_attended(client):
    signup = _signup(client, "liam3")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair_a = client.post("/hosts/pair", json={"routing_key": "rk-liam3-a", "hostname": "a"}, headers=headers).json()
    pair_b = client.post("/hosts/pair", json={"routing_key": "rk-liam3-b", "hostname": "b"}, headers=headers).json()
    client.put("/users/me/attended-host", json={"host_id": pair_a["host_id"]}, headers=headers)

    approval_id = client.post(
        "/hosts/pending-approvals", json=_pending_approval_body(), headers=headers
    ).json()["approval_id"]

    # Switch attended host to B before A gets a chance to decide.
    client.put("/users/me/attended-host", json={"host_id": pair_b["host_id"]}, headers=headers)

    device_headers_a = {"Authorization": f"Bearer {pair_a['device_token']}"}
    rejected = client.post(
        f"/hosts/pending-approvals/{approval_id}/decision", json={"decision": "allow"}, headers=device_headers_a
    )
    assert rejected.status_code == 403

    device_headers_b = {"Authorization": f"Bearer {pair_b['device_token']}"}
    accepted = client.post(
        f"/hosts/pending-approvals/{approval_id}/decision", json={"decision": "allow"}, headers=device_headers_b
    )
    assert accepted.status_code == 200


def test_pending_approval_requires_auth(client):
    assert client.post("/hosts/pending-approvals", json=_pending_approval_body()).status_code == 401
    assert client.get("/hosts/pending-approvals").status_code == 401
    assert client.get("/hosts/pending-approvals/does-not-exist").status_code == 401


def test_pending_approval_not_readable_by_another_user(client):
    import main as auth_main

    auth_main._LONG_POLL_SECONDS = 0.2

    a = _signup(client, "liam4")
    b = _signup(client, "liam5")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}
    approval_id = client.post(
        "/hosts/pending-approvals", json=_pending_approval_body(), headers=headers_a
    ).json()["approval_id"]

    assert client.get(f"/hosts/pending-approvals/{approval_id}", headers=headers_b).status_code == 404
