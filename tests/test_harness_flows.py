"""End-to-end harness flow tests -- imports harness/client.py directly
(never the CLI, never a subprocess) and drives it against casper_service's
FastAPI app through an in-process fastapi.testclient.TestClient (the same
one test_conversations.py/test_policies.py already use), so this exercises
the exact same code path a real `harness` CLI invocation over a real
network would, with no live server process and no real network at all.
Same "test user flow" posture agreed on during the harness design
discussion. The one thing this deliberately does NOT cover is a live
OpenAI call -- see this repo's manual verification note (a real, non-mock
`harness chat` session) for that; here, main._get_openai_client is always
patched with a scripted fake, same posture as tests/test_conversations.py."""

import json
import os
import sys
import tempfile

import pytest
import yaml

from fake_daemon import FakeDaemon

CASPER_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "casper_service")


class FakeItem:
    def __init__(self, type, **kwargs):
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeResponse:
    def __init__(self, id, output_text="", output=None):
        self.id = id
        self.output_text = output_text
        self.output = output or []


class FakeResponses:
    def __init__(self, queue):
        self._queue = list(queue)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise AssertionError("FakeResponses queue exhausted -- test scripted too few hops.")
        return self._queue.pop(0)


class FakeClient:
    def __init__(self, responses_queue):
        self.responses = FakeResponses(responses_queue)


@pytest.fixture()
def harness_env():
    """Fresh SQLite-file-per-test casper_service app, wired to harness/
    client.py through an in-process TestClient -- no real socket, no real
    subprocess. Yields (main_module, harness_client_module)."""
    sys.path.insert(0, os.path.abspath(CASPER_SERVICE_DIR))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUTH_DB_PATH"] = os.path.join(tmp, "users.db")
        os.environ["STORAGE_ROOT"] = os.path.join(tmp, "storage")
        for mod in ("main", "db", "models", "policy", "conversations"):
            sys.modules.pop(mod, None)
        import main as auth_main
        from fastapi.testclient import TestClient

        import harness.client as hc

        hc.set_client(TestClient(auth_main.app))
        try:
            yield auth_main, hc
        finally:
            hc.set_client(None)
    sys.path.remove(os.path.abspath(CASPER_SERVICE_DIR))
    for mod in ("main", "db", "models", "policy", "conversations"):
        sys.modules.pop(mod, None)


DOMAIN = "test-auth.invalid"  # ignored entirely once hc.set_client() is active


def _write_yaml(tmp_path, spec: dict) -> str:
    path = os.path.join(tmp_path, "layer.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(spec, f)
    return path


def test_full_flow_signup_through_eval(harness_env, tmp_path):
    """/policies/eval now routes to a real, connected daemon (see
    casper_service/main.py's eval_policy) -- FakeDaemon stands in for one,
    scripted to return exactly the verdict each call should get, never
    asked to actually match anything itself (that's the Go daemon's own
    tested concern -- see agent/internal/commands/policy_test.go and
    tests/test_policy_parity.py). This test is about the ROUTING/plumbing
    (host resolution, response reshaping back into matched_rule), not
    matching semantics."""
    _main, hc = harness_env

    signup = hc.signup(DOMAIN, "flowuser", "correct-horse-battery")
    token = signup["token"]
    assert signup["username"] == "flowuser"

    yaml_path = _write_yaml(
        tmp_path,
        {
            "name": "npm scripts",
            "rules": [
                {"tier": "allow", "positional": [{"whitelist": "^npm$"}, {"whitelist": "^run$"}]},
                {"tier": "deny"},
            ],
        },
    )
    layer = hc.apply_policy_layer(DOMAIN, token, yaml_path)
    assert layer["name"] == "npm scripts"
    assert len(layer["rules"]) == 2

    # Re-applying is idempotent -- same layer id, same rule count, not
    # duplicated, though re-applying does replace the rule rows (new rule
    # ids) -- allow_rule_id is captured AFTER this, from the rules that
    # actually persist.
    reapplied = hc.apply_policy_layer(DOMAIN, token, yaml_path)
    assert reapplied["id"] == layer["id"]
    assert len(reapplied["rules"]) == 2
    allow_rule_id = reapplied["rules"][0]["id"]

    pair = hc.pair_host(DOMAIN, token, "rk-flow-1", hostname="flow-host")
    assert pair["label"] == "flow-host"

    hc.add_policy_layer_to_host(DOMAIN, token, layer["id"], pair["host_id"])

    hosts = hc.list_hosts(DOMAIN, token)["hosts"]
    assert hosts[0]["connected"] is False

    with FakeDaemon(
        queue=[
            (200, {"tier": "allow", "matched_layer_id": layer["id"], "matched_rule_id": allow_rule_id}),
            (200, {"tier": "deny"}),
        ]
    ) as daemon:
        presence = hc.report_host_presence(DOMAIN, pair["device_token"], daemon.url, "/tmp/ws")
        assert presence["connected"] is True

        hosts_after = hc.list_hosts(DOMAIN, token)["hosts"]
        assert hosts_after[0]["connected"] is True
        assert hosts_after[0]["cwd"] == "/tmp/ws"

        matching = hc.eval_policy(DOMAIN, token, [layer["id"]], positional_args=["npm", "run"], host="flow-host")
        assert matching["tier"] == "allow"
        assert matching["matched_layer_id"] == layer["id"]
        assert matching["matched_rule"]["id"] == allow_rule_id

        non_matching = hc.eval_policy(DOMAIN, token, [layer["id"]], positional_args=["rm", "-rf"], host="flow-host")
        assert non_matching["tier"] == "deny"
        assert non_matching["matched_rule"] is None

        assert [req["action"] for req in daemon.requests] == ["eval_policy", "eval_policy"]


def test_mock_call_tool_flow(harness_env):
    main, hc = harness_env
    signup = hc.signup(DOMAIN, "toolflowuser", "correct-horse-battery")
    token = signup["token"]

    pair = hc.pair_host(DOMAIN, token, "rk-toolflow-1", hostname="toolflow-host")
    hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/toolflow")

    main._get_openai_client = lambda: FakeClient([FakeResponse(id="resp_1", output_text="Ran it.")])

    result = hc.call_tool(DOMAIN, token, "run_shell_command", {"positional_args": ["pwd"]}, mock=True)
    assert result["status"] == "done"
    calls = result["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    output = json.loads(calls[0]["output"])
    assert output["mock"] is True
    assert output["positional_args"] == ["pwd"]


def test_mock_chat_flow_with_approval(harness_env, tmp_path):
    main, hc = harness_env
    signup = hc.signup(DOMAIN, "chatflowuser", "correct-horse-battery")
    token = signup["token"]

    pair = hc.pair_host(DOMAIN, token, "rk-chatflow-1", hostname="chatflow-host")
    hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/chatflow")

    first_hop = FakeResponse(
        id="resp_1",
        output_text="",
        output=[
            FakeItem(
                type="function_call",
                call_id="call_1",
                name="run_shell_command",
                arguments=json.dumps({"positional_args": ["rm", "scratch.txt"]}),
            )
        ],
    )
    second_hop = FakeResponse(id="resp_2", output_text="Done -- deleted scratch.txt.")
    fake = FakeClient([first_hop, second_hop])
    main._get_openai_client = lambda: fake

    paused = hc.chat_step(DOMAIN, token, message="please delete scratch.txt", mock=True, mock_tier="ask")
    assert paused["status"] == "pending_approval"
    assert paused["pending_approval"]["host"] == "chatflow-host"

    resumed = hc.chat_step(DOMAIN, token, turn=paused["turn"], approval_decision="allow", mock=True)
    assert resumed["status"] == "done"
    assert resumed["message"] == "Done -- deleted scratch.txt."
    calls = resumed["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    output = json.loads(calls[0]["output"])
    assert output["mock"] is True


def test_pending_approval_answerable_from_any_session_via_the_durable_queue(harness_env):
    """The durable, per-user pending-approval queue (replacing the removed
    "attended host" native-dialog relay): a pause from one call is
    discoverable and resolvable via list_pending_approvals/
    decide_pending_approval using nothing but the account's own session
    token -- no host/device_token designation involved at all, and it
    actually resumes the conversation (unlike the old device_token
    channel, which only ever recorded a decision nothing else read back)."""
    main, hc = harness_env
    signup = hc.signup(DOMAIN, "attendeduser", "correct-horse-battery")
    token = signup["token"]

    pair = hc.pair_host(DOMAIN, token, "rk-attended-dispatch-1", hostname="dispatch-host")
    hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/dispatch")

    fake = FakeClient([FakeResponse(id="resp_1", output_text="Ran it.")])
    main._get_openai_client = lambda: fake

    paused = hc.call_tool(
        DOMAIN, token, "run_shell_command", {"positional_args": ["rm", "scratch.txt"]}, mock=True, mock_tier="ask"
    )
    assert paused["status"] == "pending_approval"
    approval_id = paused["pending_approval"]["approval_id"]
    assert approval_id

    pending = hc.list_pending_approvals(DOMAIN, token)["pending_approvals"]
    assert [p["id"] for p in pending] == [approval_id]

    resumed = hc.decide_pending_approval(DOMAIN, token, approval_id, "allow")
    assert resumed["status"] == "done"
    calls = resumed["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    assert json.loads(calls[0]["output"])["mock"] is True

    # Resolved -- gone from the queue.
    assert hc.list_pending_approvals(DOMAIN, token)["pending_approvals"] == []


def test_policy_apply_and_delete(harness_env, tmp_path):
    _main, hc = harness_env
    signup = hc.signup(DOMAIN, "deleteflowuser", "correct-horse-battery")
    token = signup["token"]

    yaml_path = _write_yaml(tmp_path, {"name": "throwaway", "rules": []})
    layer = hc.apply_policy_layer(DOMAIN, token, yaml_path)
    assert hc.list_policy_layers(DOMAIN, token)["policy_layers"]

    hc.delete_policy_layer(DOMAIN, token, layer["id"])
    assert hc.list_policy_layers(DOMAIN, token)["policy_layers"] == []


def test_api_error_surfaces_status_and_detail(harness_env):
    _main, hc = harness_env
    with pytest.raises(hc.ApiError) as exc_info:
        hc.list_hosts(DOMAIN, "not-a-real-token")
    assert exc_info.value.status_code == 401


def test_host_rename_and_forget_flow(harness_env):
    """Covers the parity surface that replaced pages/environments.py's own
    host-management controls."""
    _main, hc = harness_env
    signup = hc.signup(DOMAIN, "hostmgmtuser", "correct-horse-battery")
    token = signup["token"]

    pair = hc.pair_host(DOMAIN, token, "rk-hostmgmt-1", hostname="hostmgmt-host")
    assert pair["label"] == "hostmgmt-host"

    hc.rename_host(DOMAIN, token, pair["host_id"], "Renamed Host")
    hosts = hc.list_hosts(DOMAIN, token)["hosts"]
    assert hosts[0]["label"] == "Renamed Host"

    hc.forget_host(DOMAIN, token, pair["host_id"])
    assert hc.list_hosts(DOMAIN, token)["hosts"] == []


def test_environment_crud_flow(harness_env):
    """Covers the parity surface that replaced pages/environments.py's own
    Environment-management controls."""
    _main, hc = harness_env
    signup = hc.signup(DOMAIN, "envmgmtuser", "correct-horse-battery")
    token = signup["token"]
    pair = hc.pair_host(DOMAIN, token, "rk-envmgmt-1", hostname="envmgmt-host")

    created = hc.create_environment(DOMAIN, token, "Staging")
    assert created["name"] == "Staging"
    assert created["host_ids"] == []

    listed = hc.list_environments(DOMAIN, token)["environments"]
    assert any(e["id"] == created["id"] for e in listed)

    renamed = hc.rename_environment(DOMAIN, token, created["id"], "Staging (renamed)")
    assert renamed["name"] == "Staging (renamed)"

    attached = hc.add_host_to_environment(DOMAIN, token, created["id"], pair["host_id"])
    assert attached["host_ids"] == [pair["host_id"]]

    detached = hc.remove_host_from_environment(DOMAIN, token, created["id"], pair["host_id"])
    assert detached["host_ids"] == []

    hc.delete_environment(DOMAIN, token, created["id"])
    remaining_ids = [e["id"] for e in hc.list_environments(DOMAIN, token)["environments"]]
    assert created["id"] not in remaining_ids


def test_profile_get_and_merge_update_flow(harness_env):
    """Covers the parity surface that replaced pages/settings_profile.py's
    and pages/settings_security.py's own controls -- both were thin
    wrappers over this same GET/PATCH /profile."""
    _main, hc = harness_env
    signup = hc.signup(DOMAIN, "profilemgmtuser", "correct-horse-battery")
    token = signup["token"]

    fresh = hc.get_profile(DOMAIN, token)
    assert fresh["email"] == ""
    assert fresh["allow_configure_hosts"] is False

    updated = hc.update_profile(DOMAIN, token, email="a@example.com", allow_configure_hosts=True)
    assert updated["email"] == "a@example.com"
    assert updated["allow_configure_hosts"] is True

    # A second, disjoint partial update leaves the first update's fields
    # untouched -- confirms this is a real merge, not a full replace.
    second = hc.update_profile(DOMAIN, token, sms_notifications_enabled=True)
    assert second["email"] == "a@example.com"
    assert second["allow_configure_hosts"] is True
    assert second["sms_notifications_enabled"] is True
