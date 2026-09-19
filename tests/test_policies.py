import os
import sys
import tempfile

import pytest

AUTH_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "auth_service")

# Must match auth_service/models.py's own Pattern.blacklist default exactly.
BLACKLIST_MATCHES_NOTHING = r"[^\s\S]"


@pytest.fixture()
def client():
    """Same fresh-SQLite-file-per-test posture as test_auth_service.py's own
    fixture -- duplicated rather than shared via a conftest.py, matching
    this test suite's existing self-contained-per-file convention."""
    sys.path.insert(0, os.path.abspath(AUTH_SERVICE_DIR))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUTH_DB_PATH"] = os.path.join(tmp, "users.db")
        os.environ["STORAGE_ROOT"] = os.path.join(tmp, "storage")
        for mod in ("main", "db", "models", "policy"):
            sys.modules.pop(mod, None)
        import main as auth_main
        from fastapi.testclient import TestClient

        yield TestClient(auth_main.app)
    sys.path.remove(os.path.abspath(AUTH_SERVICE_DIR))
    for mod in ("main", "db", "models", "policy"):
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


def _eval(client, headers, **body):
    return client.post("/policies/eval", json=body, headers=headers)


def test_eval_no_layers_means_terminal_deny(client):
    signup = _signup(client, "alice")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = _eval(client, headers, policy_layer_ids=[], positional_args=["npm", "run"])
    assert r.status_code == 200
    assert r.json() == {"tier": "deny", "matched_layer_id": None, "matched_rule": None}


def test_eval_composes_multiple_layers_in_id_order_first_match_wins(client):
    signup = _signup(client, "bob")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    # layer_a is created (and so gets a lower id) before layer_b.
    layer_a = _create_policy_layer(client, headers, name="npm allow")
    _add_rule(client, headers, layer_a["id"], [{"whitelist": "^npm$"}, {"whitelist": "^run$"}], tier="allow")
    layer_b = _create_policy_layer(client, headers, name="catch-all deny")
    _add_rule(client, headers, layer_b["id"], [], tier="deny")

    # Composed regardless of the order layer ids are listed in the request --
    # sorted ascending (layer_a's rule first) before matching.
    matched = _eval(
        client, headers, policy_layer_ids=[layer_b["id"], layer_a["id"]], positional_args=["npm", "run"]
    ).json()
    assert matched["tier"] == "allow"
    assert matched["matched_layer_id"] == layer_a["id"]

    # A call layer_a's rule doesn't match falls through to layer_b's catch-all.
    fallthrough = _eval(
        client, headers, policy_layer_ids=[layer_a["id"], layer_b["id"]], positional_args=["rm", "-rf"]
    ).json()
    assert fallthrough["tier"] == "deny"
    assert fallthrough["matched_layer_id"] == layer_b["id"]

    # A subset composition (ad hoc, harness-style) only sees what it's given.
    only_b = _eval(
        client, headers, policy_layer_ids=[layer_b["id"]], positional_args=["npm", "run"]
    ).json()
    assert only_b["tier"] == "deny"
    assert only_b["matched_layer_id"] == layer_b["id"]


def test_eval_positional_pattern_states(client):
    signup = _signup(client, "carol")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers)
    # position 0: value required (must be "rm"); position 1: value not
    # allowed (must be absent/""); position 2: value not required (blank).
    _add_rule(
        client, headers, layer["id"], [{"whitelist": "^rm$"}, {"whitelist": "^$"}, {}], tier="allow"
    )

    matches = _eval(client, headers, policy_layer_ids=[layer["id"]], positional_args=["rm"]).json()
    assert matches["tier"] == "allow"

    wrong_binary = _eval(client, headers, policy_layer_ids=[layer["id"]], positional_args=["mv"]).json()
    assert wrong_binary["tier"] == "deny"

    disallowed_value_present = _eval(
        client, headers, policy_layer_ids=[layer["id"]], positional_args=["rm", "-rf"]
    ).json()
    assert disallowed_value_present["tier"] == "deny"

    # position 2 accepts anything (or absence) -- doesn't affect the match.
    extra_arg_ok = _eval(
        client, headers, policy_layer_ids=[layer["id"]], positional_args=["rm", "", "anything"]
    ).json()
    assert extra_arg_ok["tier"] == "allow"


def test_eval_option_pattern_states(client):
    signup = _signup(client, "dave")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers)
    _add_rule(
        client,
        headers,
        layer["id"],
        [{}],
        option_constraints=[
            {"long": "force", "pattern": {"whitelist": "^$"}},  # must be present, no value
            {"long": "output", "pattern": {"whitelist": ".+"}},  # must be present, value required
        ],
        tier="allow",
    )

    matches = _eval(
        client,
        headers,
        policy_layer_ids=[layer["id"]],
        positional_args=["cmd"],
        options=[{"long": "force"}, {"long": "output", "value": "out.txt"}],
    ).json()
    assert matches["tier"] == "allow"

    missing_required_option = _eval(
        client, headers, policy_layer_ids=[layer["id"]], positional_args=["cmd"], options=[{"long": "force"}]
    ).json()
    assert missing_required_option["tier"] == "deny"

    force_with_disallowed_value = _eval(
        client,
        headers,
        policy_layer_ids=[layer["id"]],
        positional_args=["cmd"],
        options=[{"long": "force", "value": "yes"}, {"long": "output", "value": "out.txt"}],
    ).json()
    assert force_with_disallowed_value["tier"] == "deny"


def test_eval_cwd_constraint(client):
    signup = _signup(client, "cwd-user")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers)
    _add_rule(
        client, headers, layer["id"], [{}], tier="allow", cwd={"whitelist": "^/home/alice(/.*)?$"}
    )

    inside = _eval(
        client, headers, policy_layer_ids=[layer["id"]], positional_args=["ls"], cwd="/home/alice/project"
    ).json()
    assert inside["tier"] == "allow"

    outside = _eval(
        client, headers, policy_layer_ids=[layer["id"]], positional_args=["ls"], cwd="/etc"
    ).json()
    assert outside["tier"] == "deny"

    # cwd omitted entirely ("") doesn't satisfy a non-blank cwd pattern --
    # same fail-closed posture as every other constraint.
    omitted = _eval(client, headers, policy_layer_ids=[layer["id"]], positional_args=["ls"]).json()
    assert omitted["tier"] == "deny"


def test_eval_blank_cwd_means_any_directory(client):
    signup = _signup(client, "cwd-user2")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{}], tier="allow")

    matched = _eval(
        client, headers, policy_layer_ids=[layer["id"]], positional_args=["ls"], cwd="/anywhere/at/all"
    ).json()
    assert matched["tier"] == "allow"


def test_eval_dot_path_resolution_is_best_effort_join(client):
    """This service can't resolve symlinks (no real filesystem access) --
    it does a plain string-join of a relative value against cwd, matched
    against the pattern as given. See policy.py's own module docstring for
    why the Go daemon, not this preview, is what actually enforces this
    accurately."""
    signup = _signup(client, "cwd-user3")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    layer = _create_policy_layer(client, headers)
    _add_rule(
        client,
        headers,
        layer["id"],
        [{}, {"whitelist": "^/home/alice/notes\\.txt$", "path_resolution": "."}],
        tier="allow",
    )

    joined = _eval(
        client,
        headers,
        policy_layer_ids=[layer["id"]],
        positional_args=["cat", "notes.txt"],
        cwd="/home/alice",
    ).json()
    assert joined["tier"] == "allow"

    elsewhere = _eval(
        client,
        headers,
        policy_layer_ids=[layer["id"]],
        positional_args=["cat", "notes.txt"],
        cwd="/etc",
    ).json()
    assert elsewhere["tier"] == "deny"


def test_eval_ownership_enforced(client):
    a = _signup(client, "hank")
    b = _signup(client, "ivy")
    headers_a = {"Authorization": f"Bearer {a['token']}"}
    headers_b = {"Authorization": f"Bearer {b['token']}"}

    layer = _create_policy_layer(client, headers_a)

    other_users_layer = _eval(client, headers_b, policy_layer_ids=[layer["id"]], positional_args=[])
    assert other_users_layer.status_code == 404
