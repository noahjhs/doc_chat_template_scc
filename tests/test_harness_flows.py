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

    hosts = hc.list_hosts(DOMAIN, token)["hosts"]
    assert hosts[0]["connected"] is False

    presence = hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/flow", "/tmp/ws")
    assert presence["connected"] is True

    hosts_after = hc.list_hosts(DOMAIN, token)["hosts"]
    assert hosts_after[0]["connected"] is True
    assert hosts_after[0]["cwd"] == "/tmp/ws"


def test_mock_call_tool_flow(harness_env):
    main, hc = harness_env
    signup = hc.signup(DOMAIN, "toolflowuser", "correct-horse-battery")
    token = signup["token"]

    pair = hc.pair_host(DOMAIN, token, "rk-toolflow-1", hostname="toolflow-host")
    hc.report_host_presence(DOMAIN, pair["device_token"], "https://relay.example/agent/toolflow")
    layer = hc.create_policy_layer(DOMAIN, token, "toolflow layer")
    hc.create_policy_layer_rule(DOMAIN, token, layer["id"], [{"whitelist": "^pwd$"}], [], "allow")
    hc.add_policy_layer_to_host(DOMAIN, token, layer["id"], pair["host_id"])

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


def test_attended_host_answers_approval_via_device_token_channel(harness_env, tmp_path):
    """Phase 4's own protocol-level test: a harness-faked host is set as
    the attended host and decides the approval via the device_token
    channel (list_pending_approvals/decide_pending_approval) -- the same
    channel a real native dialog (dialog.go's osascript loop) or a
    harness `respond-approvals` session uses -- rather than the caller
    supplying approval_decision to /conversations/step directly. Confirms
    that decision is then usable to resume the conversation, same as any
    other approval_decision value."""
    main, hc = harness_env
    signup = hc.signup(DOMAIN, "attendeduser", "correct-horse-battery")
    token = signup["token"]

    dispatch_pair = hc.pair_host(DOMAIN, token, "rk-attended-dispatch-1", hostname="dispatch-host")
    hc.report_host_presence(DOMAIN, dispatch_pair["device_token"], "https://relay.example/agent/dispatch")

    yaml_path = _write_yaml(
        tmp_path, {"name": "rm ask", "rules": [{"tier": "ask", "positional": [{"whitelist": "^rm$"}]}]}
    )
    layer = hc.apply_policy_layer(DOMAIN, token, yaml_path)
    hc.add_policy_layer_to_host(DOMAIN, token, layer["id"], dispatch_pair["host_id"])

    # A SEPARATE host stands in for "the attended device" -- never itself
    # dispatches anything; auth_service has no binding requiring it to.
    attendant_pair = hc.pair_host(DOMAIN, token, "rk-attended-watcher-1", hostname="attendant-host")
    hc.set_attended_host(DOMAIN, token, attendant_pair["host_id"])
    assert hc.get_attended_host(DOMAIN, token)["host_id"] == attendant_pair["host_id"]

    fake = FakeClient([FakeResponse(id="resp_1", output_text="Ran it.")])
    main._get_openai_client = lambda: fake

    paused = hc.call_tool(
        DOMAIN, token, "run_shell_command", {"positional_args": ["rm", "scratch.txt"]}, mock=True
    )
    assert paused["status"] == "pending_approval"
    approval_id = paused["pending_approval"]["approval_id"]
    assert approval_id

    # Nothing decided yet -- undecided in the attended device's own queue.
    pending = hc.list_pending_approvals(DOMAIN, attendant_pair["device_token"])["pending_approvals"]
    assert any(p["id"] == approval_id and p["decision"] is None for p in pending)

    decided = hc.decide_pending_approval(DOMAIN, attendant_pair["device_token"], approval_id, "allow")
    assert decided["decision"] == "allow"

    # The submitter's own poll (a *different* credential -- the session
    # token, not either device_token) sees the same decision land.
    submitter_view = hc.get_pending_approval(DOMAIN, token, approval_id, wait_seconds=0)
    assert submitter_view["decision"] == "allow"

    # The caller relays the decision it just learned via the device_token
    # channel back into /conversations/step's own approval_decision field
    # to actually resume the conversation.
    resumed = hc.conversation_step(DOMAIN, token, turn=paused["turn"], approval_decision="allow", mock=True)
    assert resumed["status"] == "done"
    calls = resumed["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    assert json.loads(calls[0]["output"])["mock"] is True


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
