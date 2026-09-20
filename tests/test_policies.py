"""POST /policies/eval's own routing/plumbing -- host resolution, ownership,
response reshaping, and error handling. Real policy MATCHING semantics
(composition order, positional/option/cwd pattern states, path_resolution)
are no longer evaluated here at all -- eval now always routes to a real,
connected daemon and relays its verdict (see casper_service/main.py's
eval_policy and agent/internal/commands/policy.go's runEvalPolicy), so that
matching logic is tested once, where it actually runs: Go's own
agent/internal/commands/policy_test.go (TestEvalPolicy_*/TestMatchPolicy_*)
and the Go<->Python parity fixture (tests/test_policy_parity.py, which
still cross-checks policy.py's matcher against the Go daemon's -- unrelated
to which one is wired into a live request path). FakeDaemon (see
tests/fake_daemon.py) stands in for a real daemon here, always scripted
with an explicit, arbitrary verdict -- never asked to actually match
anything, matching this project's own "mocking never evaluates policy"
design (see casper_service/conversations.py's module docstring)."""

import os
import sys
import tempfile

import pytest

from fake_daemon import FakeDaemon

CASPER_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "casper_service")


@pytest.fixture()
def client():
    """Same fresh-SQLite-file-per-test posture as test_casper_service.py's own
    fixture -- duplicated rather than shared via a conftest.py, matching
    this test suite's existing self-contained-per-file convention."""
    sys.path.insert(0, os.path.abspath(CASPER_SERVICE_DIR))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUTH_DB_PATH"] = os.path.join(tmp, "users.db")
        os.environ["STORAGE_ROOT"] = os.path.join(tmp, "storage")
        for mod in ("main", "db", "models", "policy", "conversations"):
            sys.modules.pop(mod, None)
        import main as auth_main
        from fastapi.testclient import TestClient

        yield TestClient(auth_main.app)
    sys.path.remove(os.path.abspath(CASPER_SERVICE_DIR))
    for mod in ("main", "db", "models", "policy", "conversations"):
        sys.modules.pop(mod, None)


def _signup(client, username):
    return client.post("/signup", json={"username": username, "password": "correct-horse"}).json()


def _create_policy_layer(client, headers, name="npm scripts"):
    return client.post("/policy-layers", json={"name": name}, headers=headers).json()


def _add_rule(client, headers, layer_id, positional_constraints, option_constraints=None, tier="ask", cwd=None):
    body = {
        "positional_constraints": positional_constraints,
        "option_constraints": option_constraints or [],
        "tier": tier,
    }
    if cwd is not None:
        body["cwd"] = cwd
    return client.post(f"/policy-layers/{layer_id}/rules", json=body, headers=headers).json()


def _pair_and_connect_host(client, headers, routing_key, hostname, url):
    pair = client.post("/hosts/pair", json={"routing_key": routing_key, "hostname": hostname}, headers=headers).json()
    device_headers = {"Authorization": f"Bearer {pair['device_token']}"}
    client.post("/hosts/presence", json={"local_agent_url": url, "cwd": ""}, headers=device_headers)
    return pair


def _eval(client, headers, **body):
    return client.post("/policies/eval", json=body, headers=headers)


def test_eval_routes_to_connected_daemon_and_relays_verdict(client):
    """The daemon is the sole authority on the verdict -- this only checks
    that /policies/eval forwards the right request shape and correctly
    reshapes the response (matched_rule rebuilt from the caller's own
    already-fetched rule data, not re-sent by the daemon)."""
    signup = _signup(client, "bob")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers, name="npm allow")
    rule = _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}, {"whitelist": "^run$"}], tier="allow")

    with FakeDaemon(
        queue=[
            (200, {"tier": "allow", "matched_layer_id": layer["id"], "matched_rule_id": rule["id"]}),
            (200, {"tier": "deny"}),
        ]
    ) as daemon:
        pair = _pair_and_connect_host(client, headers, "rk-bob-1", "bobs-mac", daemon.url)

        matched = _eval(
            client, headers, policy_layer_ids=[layer["id"]], positional_args=["npm", "run"], host="bobs-mac"
        ).json()
        assert matched["tier"] == "allow"
        assert matched["matched_layer_id"] == layer["id"]
        assert matched["matched_rule"]["id"] == rule["id"]

        no_match = _eval(
            client, headers, policy_layer_ids=[layer["id"]], positional_args=["rm", "-rf"], host="bobs-mac"
        ).json()
        assert no_match["tier"] == "deny"
        assert no_match["matched_layer_id"] is None
        assert no_match["matched_rule"] is None

    assert len(daemon.requests) == 2
    first = daemon.requests[0]
    assert first["action"] == "eval_policy"
    assert first["positional_args"] == ["npm", "run"]
    assert first["policy_layers"][0]["id"] == layer["id"]
    assert first["policy_layers"][0]["rules"][0]["id"] == rule["id"]
    assert pair["host_id"]  # sanity -- pairing itself succeeded


def test_eval_ad_hoc_subset_sends_only_the_requested_layers(client):
    """The caller composes whichever arbitrary subset of their own layers
    they want -- confirms /policies/eval sends ONLY that subset inline,
    never every layer the user owns."""
    signup = _signup(client, "carol")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer_a = _create_policy_layer(client, headers, name="layer-a")
    layer_b = _create_policy_layer(client, headers, name="layer-b")

    with FakeDaemon(queue=[(200, {"tier": "deny"})]) as daemon:
        _pair_and_connect_host(client, headers, "rk-carol-1", "carols-mac", daemon.url)
        _eval(client, headers, policy_layer_ids=[layer_b["id"]], positional_args=["ls"], host="carols-mac")

    sent_ids = [pl["id"] for pl in daemon.requests[0]["policy_layers"]]
    assert sent_ids == [layer_b["id"]]
    assert layer_a["id"] not in sent_ids


def test_eval_requires_host_when_multiple_connected(client):
    signup = _signup(client, "dave")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    with FakeDaemon(queue=[]) as daemon_a, FakeDaemon(queue=[]) as daemon_b:
        _pair_and_connect_host(client, headers, "rk-dave-1", "daves-laptop", daemon_a.url)
        _pair_and_connect_host(client, headers, "rk-dave-2", "daves-mini", daemon_b.url)

        r = _eval(client, headers, policy_layer_ids=[], positional_args=["ls"])
        assert r.status_code == 404
        assert "specify which one" in r.json()["detail"]


def test_eval_no_connected_host_available(client):
    signup = _signup(client, "erin")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = _eval(client, headers, policy_layer_ids=[], positional_args=["ls"])
    assert r.status_code == 404
    assert r.json()["detail"] == "no connected machines available."


def test_eval_unreachable_daemon_fails_loudly(client):
    """No silent fallback to a local match when the named daemon can't be
    reached -- the accepted latency/availability tradeoff for no longer
    maintaining redundant server-side evaluation logic."""
    signup = _signup(client, "frank")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    with FakeDaemon(queue=[]) as daemon:
        _pair_and_connect_host(client, headers, "rk-frank-1", "franks-mac", daemon.url)
    # The daemon is now closed -- its port is no longer accepting connections.
    r = _eval(client, headers, policy_layer_ids=[], positional_args=["ls"], host="franks-mac")
    assert r.status_code == 502


def test_eval_daemon_rejects_stale_api_key(client):
    signup = _signup(client, "gale")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    with FakeDaemon(queue=[(401, {"detail": "Invalid or missing X-API-Key."})]) as daemon:
        _pair_and_connect_host(client, headers, "rk-gale-1", "gales-mac", daemon.url)
        r = _eval(client, headers, policy_layer_ids=[], positional_args=["ls"], host="gales-mac")
    assert r.status_code == 502
    assert "re-pair it" in r.json()["detail"]


def test_eval_ownership_enforced(client):
    a = _signup(client, "hank")
    b = _signup(client, "ivy")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}

    layer = _create_policy_layer(client, headers_a)

    other_users_layer = _eval(client, headers_b, policy_layer_ids=[layer["id"]], positional_args=[])
    assert other_users_layer.status_code == 404
