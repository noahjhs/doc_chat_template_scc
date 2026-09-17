"""End-to-end harness flow tests -- imports harness/client.py directly
(never the CLI, never a subprocess) and drives it against auth_service's
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

AUTH_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "auth_service")


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
    """Fresh SQLite-file-per-test auth_service app, wired to harness/
    client.py through an in-process TestClient -- no real socket, no real
    subprocess. Yields (main_module, harness_client_module)."""
    sys.path.insert(0, os.path.abspath(AUTH_SERVICE_DIR))
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
    sys.path.remove(os.path.abspath(AUTH_SERVICE_DIR))
    for mod in ("main", "db", "models", "policy", "conversations"):
        sys.modules.pop(mod, None)


DOMAIN = "test-auth.invalid"  # ignored entirely once hc.set_client() is active


def _write_yaml(tmp_path, spec: dict) -> str:
    path = os.path.join(tmp_path, "layer.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(spec, f)
    return path


def test_full_flow_signup_through_eval(harness_env, tmp_path):
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
    # duplicated.
    reapplied = hc.apply_policy_layer(DOMAIN, token, yaml_path)
    assert reapplied["id"] == layer["id"]
    assert len(reapplied["rules"]) == 2

    pair = hc.pair_host(DOMAIN, token, "rk-flow-1", hostname="flow-host")
    assert pair["label"] == "flow-host"

    hc.add_policy_layer_to_host(DOMAIN, token, layer["id"], pair["host_id"])

    matching = hc.eval_policy(DOMAIN, token, [layer["id"]], positional_args=["npm", "run"])
    assert matching["tier"] == "allow"

    non_matching = hc.eval_policy(DOMAIN, token, [layer["id"]], positional_args=["rm", "-rf"])
    assert non_matching["tier"] == "deny"

    # roots via host_id -- host isn't connected yet, so an empty roots list
    # is derived (same posture as GET /hosts' own HostInfo.workspace).
    hosts = hc.list_hosts(DOMAIN, token)["hosts"]
    assert hosts[0]["connected"] is False

    presence = hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/flow", ["/tmp/ws"])
    assert presence["connected"] is True

    hosts_after = hc.list_hosts(DOMAIN, token)["hosts"]
    assert hosts_after[0]["connected"] is True
    assert hosts_after[0]["workspace"] == ["/tmp/ws"]


def test_mock_call_tool_flow(harness_env):
    main, hc = harness_env
    signup = hc.signup(DOMAIN, "toolflowuser", "correct-horse-battery")
    token = signup["token"]

    pair = hc.pair_host(DOMAIN, token, "rk-toolflow-1", hostname="toolflow-host")
    hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/toolflow")

    main._get_openai_client = lambda: FakeClient([FakeResponse(id="resp_1", output_text="Ran it.")])

    result = hc.call_tool(DOMAIN, token, "run_local_command", {"action": "pwd"}, mock=True)
    assert result["status"] == "done"
    calls = result["turn"]["aggregate"]["local_agent_calls"]
    assert len(calls) == 1
    output = json.loads(calls[0]["output"])
    assert output["mock"] is True
    assert output["action"] == "pwd"


def test_mock_chat_flow_with_approval(harness_env, tmp_path):
    main, hc = harness_env
    signup = hc.signup(DOMAIN, "chatflowuser", "correct-horse-battery")
    token = signup["token"]

    yaml_path = _write_yaml(
        tmp_path, {"name": "rm ask", "rules": [{"tier": "ask", "positional": [{"whitelist": "^rm$"}]}]}
    )
    layer = hc.apply_policy_layer(DOMAIN, token, yaml_path)
    pair = hc.pair_host(DOMAIN, token, "rk-chatflow-1", hostname="chatflow-host")
    hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/chatflow")
    hc.add_policy_layer_to_host(DOMAIN, token, layer["id"], pair["host_id"])

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

    paused = hc.chat_step(DOMAIN, token, message="please delete scratch.txt", mock=True)
    assert paused["status"] == "pending_approval"
    assert paused["pending_approval"]["host"] == "chatflow-host"

    resumed = hc.chat_step(DOMAIN, token, turn=paused["turn"], approval_decision="allow", mock=True)
    assert resumed["status"] == "done"
    assert resumed["message"] == "Done -- deleted scratch.txt."
    calls = resumed["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    output = json.loads(calls[0]["output"])
    assert output["mock"] is True


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
