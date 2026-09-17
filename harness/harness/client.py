"""Plain functions wrapping auth_service's HTTP API -- no CLI dependency
here at all (no typer, no rich): tests/test_harness_flows.py imports this
module directly for end-to-end automation, same posture as any other unit
under test, and harness/cli.py is a thin presentation layer on top of it.
Every function takes an explicit domain + (where needed) token -- no
hidden global session state; harness/cli.py is what adds a persisted
session on top of this.

DEBUG (a module-level flag, not a per-call parameter -- see the module's
own toggle_debug()) prints every request/response when enabled, the
"surface what the agent/backend actually sees" observability concern from
the original harness design discussion."""

import json

import httpx

DEFAULT_TIMEOUT = 15.0

DEBUG = False

# Test-only injection point (see set_client below) -- real usage (the CLI)
# never touches this; it stays None, meaning "real network".
_client_override = None


def toggle_debug(on: bool) -> None:
    global DEBUG
    DEBUG = on


def set_client(client_obj) -> None:
    """Routes every request through the given object's own .request(method,
    url, headers=..., **kwargs) instead of a real socket -- what
    tests/test_harness_flows.py uses (a fastapi.testclient.TestClient bound
    directly to auth_service's FastAPI app) to exercise this library
    end-to-end with no live server process and no real network at all.
    domain is ignored entirely when a client_obj is set (TestClient already
    scopes every request to its own app). Pass None to restore real-network
    behavior."""
    global _client_override
    _client_override = client_obj


class ApiError(Exception):
    """Raised for any non-2xx response. .status_code and .detail (the
    server's own {"detail": ...} message when present) let a caller
    distinguish e.g. a 404 from a 422 without parsing str(e)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


def _url(domain: str, path: str) -> str:
    # localhost/127.0.0.1 (local dev servers) get plain http; every real
    # deployment domain is always behind TLS.
    scheme = "http" if domain.startswith("localhost") or domain.startswith("127.0.0.1") else "https"
    return f"{scheme}://{domain}{path}"


def _request(domain: str, method: str, path: str, token: str | None = None, **kwargs) -> dict | None:
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if DEBUG:
        print(f">>> {method} {path if _client_override else _url(domain, path)}")
        if "json" in kwargs:
            print(f"    body: {json.dumps(kwargs['json'])}")
    if _client_override is not None:
        response = _client_override.request(method, path, headers=headers, **kwargs)
    else:
        http_client = httpx.Client(timeout=DEFAULT_TIMEOUT)
        try:
            response = http_client.request(method, _url(domain, path), headers=headers, **kwargs)
        finally:
            http_client.close()
    if DEBUG:
        print(f"<<< {response.status_code} {response.text}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ApiError(response.status_code, detail)
    if not response.content:
        return None
    return response.json()


# --- Auth ------------------------------------------------------------------
def signup(domain: str, username: str, password: str) -> dict:
    return _request(domain, "POST", "/signup", json={"username": username, "password": password})


def signin(domain: str, username: str, password: str) -> dict:
    return _request(domain, "POST", "/login", json={"username": username, "password": password})


def verify(domain: str, token: str) -> dict:
    return _request(domain, "POST", "/verify", token=token)


# --- Hosts -------------------------------------------------------------------
def list_hosts(domain: str, token: str) -> dict:
    return _request(domain, "GET", "/hosts", token=token)


def pair_host(domain: str, token: str, routing_key: str, hostname: str | None = None, label: str | None = None) -> dict:
    """Normally the Go daemon does this itself (scanning a casper://pair
    URL); exposed here too so the harness can stand up a fake/test host --
    one that never actually reports presence -- purely to exercise policy
    layer attachment/eval without a live daemon."""
    body = {"routing_key": routing_key}
    if hostname is not None:
        body["hostname"] = hostname
    if label is not None:
        body["label"] = label
    return _request(domain, "POST", "/hosts/pair", token=token, json=body)


def report_host_presence(domain: str, device_token: str, local_agent_url: str, workspace: list[str] | None = None) -> dict:
    """For a fake test host to make itself "connected" (see pair_host) --
    a real daemon does this on its own; the harness only needs it to
    exercise policy eval/enforcement against a host with a live workspace,
    with no real machine involved."""
    return _request(
        domain,
        "POST",
        "/hosts/presence",
        token=device_token,
        json={"local_agent_url": local_agent_url, "workspace": workspace or []},
    )


# --- Policy layers -----------------------------------------------------------
def list_policy_layers(domain: str, token: str) -> dict:
    return _request(domain, "GET", "/policy-layers", token=token)


def create_policy_layer(domain: str, token: str, name: str) -> dict:
    return _request(domain, "POST", "/policy-layers", token=token, json={"name": name})


def rename_policy_layer(domain: str, token: str, layer_id: int, name: str) -> dict:
    return _request(domain, "PATCH", f"/policy-layers/{layer_id}", token=token, json={"name": name})


def delete_policy_layer(domain: str, token: str, layer_id: int) -> dict:
    return _request(domain, "DELETE", f"/policy-layers/{layer_id}", token=token)


def add_policy_layer_to_host(domain: str, token: str, layer_id: int, host_id: int) -> dict:
    return _request(domain, "PUT", f"/policy-layers/{layer_id}/hosts/{host_id}", token=token)


def remove_policy_layer_from_host(domain: str, token: str, layer_id: int, host_id: int) -> dict:
    return _request(domain, "DELETE", f"/policy-layers/{layer_id}/hosts/{host_id}", token=token)


def create_policy_layer_rule(
    domain: str, token: str, layer_id: int, positional_constraints: list[dict], option_constraints: list[dict], tier: str
) -> dict:
    return _request(
        domain,
        "POST",
        f"/policy-layers/{layer_id}/rules",
        token=token,
        json={"positional_constraints": positional_constraints, "option_constraints": option_constraints, "tier": tier},
    )


def delete_policy_layer_rule(domain: str, token: str, layer_id: int, rule_id: int) -> dict:
    return _request(domain, "DELETE", f"/policy-layers/{layer_id}/rules/{rule_id}", token=token)


def _option_from_yaml(option: dict) -> dict:
    """YAML's flat {short, long, whitelist, blacklist} -> the API's nested
    {short, long, pattern: {whitelist, blacklist}} -- the one translation
    the YAML authoring format needs, since a whitelist/blacklist key
    genuinely belongs to a nested Pattern everywhere else in this schema
    too (positional constraints are Pattern objects directly, no
    flattening needed there)."""
    result = {}
    if "short" in option:
        result["short"] = option["short"]
    if "long" in option:
        result["long"] = option["long"]
    pattern = {}
    if "whitelist" in option:
        pattern["whitelist"] = option["whitelist"]
    if "blacklist" in option:
        pattern["blacklist"] = option["blacklist"]
    result["pattern"] = pattern
    return result


def load_policy_layer_yaml(yaml_path: str) -> dict:
    """Parses a policy layer YAML file into {"name": ..., "rules": [...]}
    already shaped for create_policy_layer_rule -- a rule is
    {tier, positional: [{whitelist?, blacklist?}, ...],
    options: [{short?, long?, whitelist?, blacklist?}, ...]}. Blank
    whitelist/blacklist keys are simply omitted -- the API already
    defaults them ("" / BLACKLIST_MATCHES_NOTHING) server-side, so this
    format never needs to duplicate that sentinel itself."""
    import yaml

    with open(yaml_path) as f:
        spec = yaml.safe_load(f) or {}
    name = spec["name"]
    rules = []
    for rule_spec in spec.get("rules", []):
        rules.append(
            {
                "positional_constraints": list(rule_spec.get("positional", [])),
                "option_constraints": [_option_from_yaml(o) for o in rule_spec.get("options", [])],
                "tier": rule_spec.get("tier", "ask"),
            }
        )
    return {"name": name, "rules": rules}


def apply_policy_layer(domain: str, token: str, yaml_path: str) -> dict:
    """kubectl apply-style: create the layer if none with this name exists
    yet, then replace its rules wholesale (delete every existing rule,
    re-POST from the YAML in order) -- v1 keeps this simple rather than
    diffing rule-by-rule. Idempotent: applying the same file twice leaves
    the layer in the same state both times."""
    spec = load_policy_layer_yaml(yaml_path)
    existing = list_policy_layers(domain, token)["policy_layers"]
    match = next((layer for layer in existing if layer["name"] == spec["name"]), None)
    if match is None:
        layer = create_policy_layer(domain, token, spec["name"])
    else:
        layer = match
        for rule in layer["rules"]:
            delete_policy_layer_rule(domain, token, layer["id"], rule["id"])

    layer_id = layer["id"]
    for rule in spec["rules"]:
        create_policy_layer_rule(domain, token, layer_id, rule["positional_constraints"], rule["option_constraints"], rule["tier"])

    refreshed = list_policy_layers(domain, token)["policy_layers"]
    return next(l for l in refreshed if l["id"] == layer_id)


# --- Policy evaluation ---------------------------------------------------
def eval_policy(
    domain: str,
    token: str,
    policy_layer_ids: list[int],
    positional_args: list[str] | None = None,
    options: list[dict] | None = None,
    roots: list[str] | None = None,
    host_id: int | None = None,
) -> dict:
    body = {"policy_layer_ids": policy_layer_ids, "positional_args": positional_args or [], "options": options or []}
    if roots is not None:
        body["roots"] = roots
    if host_id is not None:
        body["host_id"] = host_id
    return _request(domain, "POST", "/policies/eval", token=token, json=body)


# --- Conversations ---------------------------------------------------------
def conversation_step(
    domain: str,
    token: str,
    turn: dict | None = None,
    message: str | None = None,
    tool_call: dict | None = None,
    approval_decision: str | None = None,
    default_host: str | None = None,
    mock: bool = False,
) -> dict:
    body: dict = {"mock": mock}
    if turn is not None:
        body["turn"] = turn
    if message is not None:
        body["message"] = message
    if tool_call is not None:
        body["tool_call"] = tool_call
    if approval_decision is not None:
        body["approval_decision"] = approval_decision
    if default_host is not None:
        body["default_host"] = default_host
    return _request(domain, "POST", "/conversations/step", token=token, json=body)


def call_tool(
    domain: str,
    token: str,
    name: str,
    arguments: dict,
    mock: bool = False,
    turn: dict | None = None,
    default_host: str | None = None,
) -> dict:
    """Convenience wrapper over conversation_step for the harness's `call
    tool`/`call mock tool` verbs -- injects a tool call directly (see
    auth_service/conversations.py's new_turn_from_tool_call), bypassing
    the model's own decision to call it."""
    return conversation_step(
        domain, token, turn=turn, tool_call={"name": name, "arguments": arguments}, mock=mock, default_host=default_host
    )


def chat_step(
    domain: str,
    token: str,
    message: str | None = None,
    turn: dict | None = None,
    approval_decision: str | None = None,
    default_host: str | None = None,
    mock: bool = False,
) -> dict:
    """The harness's `chat`/`chat --mock` verb -- a real conversational
    turn where the model decides what to call, as opposed to call_tool's
    direct injection."""
    return conversation_step(
        domain, token, turn=turn, message=message, approval_decision=approval_decision, default_host=default_host, mock=mock
    )
