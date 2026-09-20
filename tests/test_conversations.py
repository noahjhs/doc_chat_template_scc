import json
import os
import sys
import tempfile

import pytest

AUTH_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "auth_service")


class FakeItem:
    """A minimal stand-in for one openai.types.responses output item --
    conversations.py's own _extract_response reads attributes via getattr
    with defaults, so a fake only needs to set what a given test cares
    about."""

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
    """Queue-driven fake for client.responses -- each .create() call pops
    the next scripted FakeResponse, so a test can script exactly what the
    model "says" at each hop without touching the real API."""

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
def app_env():
    """Same fresh-SQLite-file-per-test posture as test_auth_service.py's own
    fixture, but yields (main_module, TestClient) instead of just the
    client -- tests need the module reference to patch _get_openai_client
    (module-boundary injection, per the plan's own testing guidance)."""
    sys.path.insert(0, os.path.abspath(AUTH_SERVICE_DIR))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUTH_DB_PATH"] = os.path.join(tmp, "users.db")
        os.environ["STORAGE_ROOT"] = os.path.join(tmp, "storage")
        for mod in ("main", "db", "models", "policy", "conversations"):
            sys.modules.pop(mod, None)
        import main as auth_main
        from fastapi.testclient import TestClient

        yield auth_main, TestClient(auth_main.app)
    sys.path.remove(os.path.abspath(AUTH_SERVICE_DIR))
    for mod in ("main", "db", "models", "policy", "conversations"):
        sys.modules.pop(mod, None)


def _signup(client, username):
    return client.post("/signup", json={"username": username, "password": "correct-horse"}).json()


def _pair_and_connect_host(client, headers, routing_key, hostname, cwd=""):
    pair = client.post("/hosts/pair", json={"routing_key": routing_key, "hostname": hostname}, headers=headers).json()
    device_headers = {"Authorization": f"Bearer {pair['device_token']}"}
    client.post(
        "/hosts/presence",
        json={"local_agent_url": f"https://relay.example/agent/{hostname}", "cwd": cwd},
        headers=device_headers,
    )
    return pair


def _create_policy_layer(client, headers, name="test layer"):
    return client.post("/policy-layers", json={"name": name}, headers=headers).json()


def _add_rule(client, headers, layer_id, positional_constraints, option_constraints=None, tier="ask", cwd=None):
    body = {"positional_constraints": positional_constraints, "option_constraints": option_constraints or [], "tier": tier}
    if cwd is not None:
        body["cwd"] = cwd
    return client.post(f"/policy-layers/{layer_id}/rules", json=body, headers=headers).json()


def _step(client, headers, **body):
    return client.post("/conversations/step", json=body, headers=headers)


def test_conversation_requires_message_or_tool_call(app_env):
    _main, client = app_env
    signup = _signup(client, "alice")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = _step(client, headers)
    assert r.status_code == 400


def test_conversation_simple_message_no_tools(app_env):
    main, client = app_env
    signup = _signup(client, "bob")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    fake = FakeClient([FakeResponse(id="resp_1", output_text="Hello there!")])
    main._get_openai_client = lambda: fake

    r = _step(client, headers, message="hi")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["message"] == "Hello there!"
    assert body["turn"]["full_response"] == "Hello there!"
    assert body["turn"]["previous_response_id"] == "resp_1"
    assert len(fake.responses.calls) == 1
    assert fake.responses.calls[0]["stream"] is False
    assert fake.responses.calls[0]["previous_response_id"] is None


def test_conversation_passes_profile_system_prompt_as_instructions(app_env):
    main, client = app_env
    signup = _signup(client, "dana")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    client.patch("/profile", json={"system_prompt": "Always answer in haiku."}, headers=headers)

    fake = FakeClient([FakeResponse(id="resp_1", output_text="Hello there!")])
    main._get_openai_client = lambda: fake

    r = _step(client, headers, message="hi")
    assert r.status_code == 200
    assert fake.responses.calls[0]["instructions"] == "Always answer in haiku."


def test_conversation_blank_system_prompt_passes_none_as_instructions(app_env):
    main, client = app_env
    signup = _signup(client, "erin3")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    fake = FakeClient([FakeResponse(id="resp_1", output_text="Hello there!")])
    main._get_openai_client = lambda: fake

    r = _step(client, headers, message="hi")
    assert r.status_code == 200
    assert fake.responses.calls[0]["instructions"] is None


def test_conversation_continues_same_conversation(app_env):
    main, client = app_env
    signup = _signup(client, "carol")
    headers = {"Authorization": f"Bearer {signup['token']}"}

    fake = FakeClient(
        [FakeResponse(id="resp_1", output_text="first"), FakeResponse(id="resp_2", output_text="second")]
    )
    main._get_openai_client = lambda: fake

    first = _step(client, headers, message="hi").json()
    assert first["status"] == "done"

    second = _step(client, headers, turn=first["turn"], message="and then?").json()
    assert second["status"] == "done"
    assert second["message"] == "second"
    # The second hop chained off the first response's id -- same
    # conversation, not a fresh one.
    assert fake.responses.calls[1]["previous_response_id"] == "resp_1"


def test_conversation_rejects_message_and_tool_call_together(app_env):
    _main, client = app_env
    signup = _signup(client, "dave")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    r = _step(client, headers, message="hi", tool_call={"name": "run_shell_command", "arguments": {}})
    assert r.status_code == 422


def test_conversation_tool_call_injection_dispatches_with_mock(app_env):
    main, client = app_env
    signup = _signup(client, "erin")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = _pair_and_connect_host(client, headers, "rk-erin-1", "erins-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^echo$"}], tier="allow")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_1", output_text="Ran it.")])
    main._get_openai_client = lambda: fake

    r = _step(
        client,
        headers,
        tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["echo", "hi"]}},
        mock=True,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    calls = body["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    output = json.loads(calls[0]["output"])
    assert output["mock"] is True
    assert output["positional_args"] == ["echo", "hi"]
    # The model saw the forced call's own function_call + function_call_output
    # as its first-hop input -- confirmed by the fake having been called at
    # all with previous_response_id None (a fresh conversation).
    assert fake.responses.calls[0]["previous_response_id"] is None


def test_conversation_shell_command_deny_tier_never_pauses(app_env):
    main, client = app_env
    signup = _signup(client, "frank")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = _pair_and_connect_host(client, headers, "rk-frank-1", "franks-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}], tier="allow")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_1", output_text="done")])
    main._get_openai_client = lambda: fake

    r = _step(
        client,
        headers,
        tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["rm", "-rf", "/"]}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    calls = body["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    assert calls[0]["output"] == "Denied by policy (no matching rule allows this call)."


def test_conversation_shell_command_ask_tier_pauses_then_resumes(app_env):
    main, client = app_env
    signup = _signup(client, "gina")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = _pair_and_connect_host(client, headers, "rk-gina-1", "ginas-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}, {"whitelist": "^run$"}], tier="ask")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_2", output_text="ok, ran it")])
    main._get_openai_client = lambda: fake

    paused = _step(
        client,
        headers,
        tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["npm", "run"]}},
        mock=True,
    ).json()
    assert paused["status"] == "pending_approval"
    assert paused["pending_approval"]["host"] == "ginas-mac"
    assert paused["pending_approval"]["approval_id"]
    # No model call yet -- still blocked on the human decision.
    assert fake.responses.calls == []

    resumed = _step(
        client, headers, turn=paused["turn"], approval_decision="allow", mock=True
    ).json()
    assert resumed["status"] == "done"
    calls = resumed["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    output = json.loads(calls[0]["output"])
    assert output["mock"] is True
    assert output["positional_args"] == ["npm", "run"]
    assert len(fake.responses.calls) == 1  # the follow-up hop, after resuming


def test_conversation_pending_approval_visible_and_resolvable_via_decide_endpoint(app_env):
    """The new, durable per-user path: a pending approval is listed via
    GET /conversations/pending-approvals and resolved via POST
    .../decide -- usable from ANY interface holding the user's own session
    token (not just the one that paused it), and never visible to, or
    resolvable by, another user."""
    main, client = app_env
    signup = _signup(client, "hank3")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    other = _signup(client, "ivy3")
    other_headers = {"Authorization": f"Bearer {other['token']}"}

    pair = _pair_and_connect_host(client, headers, "rk-hank3-1", "hanks-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}, {"whitelist": "^run$"}], tier="ask")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_2", output_text="ok, ran it")])
    main._get_openai_client = lambda: fake

    paused = _step(
        client, headers,
        tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["npm", "run"]}}, mock=True,
    ).json()
    approval_id = paused["pending_approval"]["approval_id"]

    mine = client.get("/conversations/pending-approvals", headers=headers).json()["pending_approvals"]
    assert [a["id"] for a in mine] == [approval_id]
    assert client.get("/conversations/pending-approvals", headers=other_headers).json()["pending_approvals"] == []

    assert (
        client.post(
            f"/conversations/pending-approvals/{approval_id}/decide", json={"decision": "allow"}, headers=other_headers
        ).status_code
        == 404
    )

    resolved = client.post(
        f"/conversations/pending-approvals/{approval_id}/decide", json={"decision": "allow"}, headers=headers
    )
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["status"] == "done"
    calls = body["turn"]["aggregate"]["shell_command_calls"]
    assert len(calls) == 1
    assert json.loads(calls[0]["output"])["positional_args"] == ["npm", "run"]

    # Resolved -- gone from the queue, can't be resolved twice.
    assert client.get("/conversations/pending-approvals", headers=headers).json()["pending_approvals"] == []
    assert (
        client.post(
            f"/conversations/pending-approvals/{approval_id}/decide", json={"decision": "allow"}, headers=headers
        ).status_code
        == 404
    )


def test_sms_inbound_resolves_pending_approval_and_resumes_conversation(app_env):
    """The full text-message-approval lifecycle: a real ask-tier pause,
    notified (mocked) via SMS, resolved by a real (signature-verified)
    inbound Twilio webhook reply -- confirms it actually resumes the
    conversation, not just records a decision."""
    from twilio.request_validator import RequestValidator

    main, client = app_env
    os.environ["TWILIO_AUTH_TOKEN"] = "test-auth-token"
    validator = RequestValidator("test-auth-token")

    signup = _signup(client, "juan")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    client.patch("/profile", json={"sms_notifications_enabled": True, "sms_number": "555-234-5678"}, headers=headers)

    pair = _pair_and_connect_host(client, headers, "rk-juan-1", "juans-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}, {"whitelist": "^run$"}], tier="ask")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_sms", output_text="ok, ran it")])
    main._get_openai_client = lambda: fake

    paused = _step(
        client, headers,
        tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["npm", "run"]}}, mock=True,
    ).json()
    assert paused["status"] == "pending_approval"

    params = {"From": "+15552345678", "Body": "yes please"}
    url = "http://testserver/sms/inbound"
    signature = validator.compute_signature(url, params)
    reply = client.post("/sms/inbound", data=params, headers={"X-Twilio-Signature": signature})
    assert reply.status_code == 200
    assert "Approved" in reply.text

    assert client.get("/conversations/pending-approvals", headers=headers).json()["pending_approvals"] == []
    assert len(fake.responses.calls) == 1  # only the follow-up hop after resuming -- the injected tool_call never needed a model call to pause


def test_conversation_shell_command_cwd_scoped_tier_decision(app_env):
    """_decide_tier threads args["path"] through as cwd -- a rule scoped to
    one directory allows a call whose path matches it and denies (never
    pauses) one that doesn't, purely from this service's own tier preview,
    no daemon/mock dispatch needed to observe the decision."""
    main, client = app_env
    signup = _signup(client, "iris")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = _pair_and_connect_host(client, headers, "rk-iris-1", "iris-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(
        client, headers, layer["id"], [{"whitelist": "^ls$"}], tier="allow", cwd={"whitelist": "^/home/iris(/.*)?$"}
    )
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_1", output_text="done"), FakeResponse(id="resp_2", output_text="done")])
    main._get_openai_client = lambda: fake

    allowed = _step(
        client,
        headers,
        tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["ls"], "path": "/home/iris/docs"}},
        mock=True,
    ).json()
    assert allowed["status"] == "done"
    assert json.loads(allowed["turn"]["aggregate"]["shell_command_calls"][0]["output"])["mock"] is True

    denied = _step(
        client,
        headers,
        tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["ls"], "path": "/etc"}},
        mock=True,
    ).json()
    assert denied["status"] == "done"
    assert denied["turn"]["aggregate"]["shell_command_calls"][0]["output"] == "Denied by policy (no matching rule allows this call)."


def test_conversation_shell_command_falls_back_to_presence_reported_cwd(app_env):
    """When the model's own call doesn't override `path`, _decide_tier uses
    the host's presence-reported cwd (its daemon's own homeRoot) as the
    join base for a cwd-constrained rule -- no explicit path needed."""
    main, client = app_env
    signup = _signup(client, "jill")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = _pair_and_connect_host(client, headers, "rk-jill-1", "jills-mac", cwd="/home/jill")
    layer = _create_policy_layer(client, headers)
    _add_rule(
        client, headers, layer["id"], [{"whitelist": "^ls$"}], tier="allow", cwd={"whitelist": "^/home/jill(/.*)?$"}
    )
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_1", output_text="done")])
    main._get_openai_client = lambda: fake

    r = _step(
        client, headers, tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["ls"]}}, mock=True
    ).json()
    assert r["status"] == "done"
    assert json.loads(r["turn"]["aggregate"]["shell_command_calls"][0]["output"])["mock"] is True


def test_conversation_shell_command_denied_by_user(app_env):
    main, client = app_env
    signup = _signup(client, "hank")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = _pair_and_connect_host(client, headers, "rk-hank-1", "hanks-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}], tier="ask")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_2", output_text="okay, skipped")])
    main._get_openai_client = lambda: fake

    paused = _step(
        client, headers, tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["npm"]}}
    ).json()
    assert paused["status"] == "pending_approval"

    resumed = _step(client, headers, turn=paused["turn"], approval_decision="deny").json()
    assert resumed["status"] == "done"
    calls = resumed["turn"]["aggregate"]["shell_command_calls"]
    assert calls[0]["output"] == "Denied by user."


def test_conversation_rejects_message_while_in_flight(app_env):
    main, client = app_env
    signup = _signup(client, "ivy")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = _pair_and_connect_host(client, headers, "rk-ivy-1", "ivys-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^npm$"}], tier="ask")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)
    main._get_openai_client = lambda: FakeClient([])

    paused = _step(
        client, headers, tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["npm"]}}
    ).json()
    assert paused["status"] == "pending_approval"

    r = _step(client, headers, turn=paused["turn"], message="never mind")
    assert r.status_code == 400


def test_conversation_default_host_used_for_ambiguous_call(app_env):
    main, client = app_env
    signup = _signup(client, "jack")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    _pair_and_connect_host(client, headers, "rk-jack-1", "jacks-laptop")
    pair2 = _pair_and_connect_host(client, headers, "rk-jack-2", "jacks-mini")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^pwd$"}], tier="allow")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair2['host_id']}", headers=headers)

    fake = FakeClient([FakeResponse(id="resp_1", output_text="done")])
    main._get_openai_client = lambda: fake

    r = _step(
        client,
        headers,
        tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["pwd"]}},
        default_host="jacks-mini",
        mock=True,
    ).json()
    assert r["status"] == "done"
    output = json.loads(r["turn"]["aggregate"]["shell_command_calls"][0]["output"])
    assert output["mock"] is True


def test_conversation_ambiguous_host_without_default_errors(app_env):
    """Unlike a tool that resolves its own host directly (e.g. transfer_file),
    run_shell_command's host resolution happens as a side effect of tier
    decision (_decide_tier composes an unresolved host's policy as empty) --
    an ambiguous host with no default therefore surfaces as a policy denial,
    not a distinct "which host?" error message."""
    main, client = app_env
    signup = _signup(client, "karen")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    _pair_and_connect_host(client, headers, "rk-karen-1", "karens-laptop")
    _pair_and_connect_host(client, headers, "rk-karen-2", "karens-mini")

    fake = FakeClient([FakeResponse(id="resp_1", output_text="done")])
    main._get_openai_client = lambda: fake

    r = _step(
        client, headers, tool_call={"name": "run_shell_command", "arguments": {"positional_args": ["pwd"]}}, mock=True
    ).json()
    output = r["turn"]["aggregate"]["shell_command_calls"][0]["output"]
    assert output == "Denied by policy (no matching rule allows this call)."


def test_conversation_model_initiated_tool_call(app_env):
    """Exercises the realistic path -- the model itself decides to call a
    tool (rather than the tool_call injection escape hatch) -- covering
    _extract_response's function_call parsing."""
    main, client = app_env
    signup = _signup(client, "laura")
    headers = {"Authorization": f"Bearer {signup['token']}"}
    pair = _pair_and_connect_host(client, headers, "rk-laura-1", "lauras-mac")
    layer = _create_policy_layer(client, headers)
    _add_rule(client, headers, layer["id"], [{"whitelist": "^pwd$"}], tier="allow")
    client.put(f"/policy-layers/{layer['id']}/hosts/{pair['host_id']}", headers=headers)

    first_hop = FakeResponse(
        id="resp_1",
        output_text="",
        output=[
            FakeItem(
                type="function_call",
                call_id="call_abc",
                name="run_shell_command",
                arguments=json.dumps({"positional_args": ["pwd"]}),
            )
        ],
    )
    second_hop = FakeResponse(id="resp_2", output_text="You're in /Users/laura.")
    fake = FakeClient([first_hop, second_hop])
    main._get_openai_client = lambda: fake

    r = _step(client, headers, message="where am I?", mock=True).json()
    assert r["status"] == "done"
    assert r["message"] == "You're in /Users/laura."
    assert len(r["turn"]["aggregate"]["shell_command_calls"]) == 1
    # The second hop's input was the function_call_output fed back.
    second_call_input = fake.responses.calls[1]["input"]
    assert second_call_input[0]["type"] == "function_call_output"
    assert second_call_input[0]["call_id"] == "call_abc"


def test_conversation_requires_auth(app_env):
    _main, client = app_env
    r = client.post("/conversations/step", json={"message": "hi"})
    assert r.status_code == 401
