import base64
import os
import sys
import tempfile

import pytest

AUTH_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "auth_service")


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

    # not yet reported reachable -- listed, but disconnected
    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["label"] == "Erin's Mac"
    assert hosts[0]["connected"] is False

    device_headers = {"Authorization": f"Bearer {body['device_token']}"}
    assert client.post("/hosts/verify", headers=device_headers).json() == {"valid": True}

    presence = client.post(
        "/hosts/presence",
        json={"local_agent_url": "https://relay.example/agent/erin", "workspace": "/Users/erin/project"},
        headers=device_headers,
    )
    assert presence.status_code == 200

    hosts = client.get("/hosts", headers=headers).json()["hosts"]
    assert hosts[0]["connected"] is True
    assert hosts[0]["local_agent_url"] == "https://relay.example/agent/erin"
    assert hosts[0]["command_key"] == body["command_key"]


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


def test_second_user_pairing_a_taken_host_gets_409(client):
    a = _signup(client, "gina")
    b = _signup(client, "hank")

    pair_a = client.post(
        "/hosts/pair", json={"routing_key": "rk-shared", "hostname": "shared-box"},
        headers={"Authorization": f"Bearer {a['token']}"},
    ).json()

    conflict = client.post(
        "/hosts/pair", json={"routing_key": "rk-shared", "hostname": "shared-box"},
        headers={"Authorization": f"Bearer {b['token']}"},
    )
    assert conflict.status_code == 409

    # a's session is undisturbed
    device_headers = {"Authorization": f"Bearer {pair_a['device_token']}"}
    assert client.post("/hosts/verify", headers=device_headers).json() == {"valid": True}


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
        json={"local_agent_url": "https://relay.example/agent/jack", "workspace": "/tmp/ws"},
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
