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
