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
import subprocess
import sys
from urllib.parse import quote

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


def report_host_presence(domain: str, device_token: str, local_agent_url: str, cwd: str = "") -> dict:
    """For a fake test host to make itself "connected" (see pair_host) --
    a real daemon does this on its own; the harness only needs it to
    exercise policy eval/enforcement against a host with a live cwd, with
    no real machine involved."""
    return _request(
        domain,
        "POST",
        "/hosts/presence",
        token=device_token,
        json={"local_agent_url": local_agent_url, "cwd": cwd},
    )


def rename_host(domain: str, token: str, host_id: int, label: str) -> dict:
    return _request(domain, "PATCH", f"/hosts/{host_id}", token=token, json={"label": label})


def forget_host(domain: str, token: str, host_id: int) -> dict:
    return _request(domain, "DELETE", f"/hosts/{host_id}", token=token)


# --- Environments ------------------------------------------------------------
def list_environments(domain: str, token: str) -> dict:
    return _request(domain, "GET", "/environments", token=token)


def create_environment(domain: str, token: str, name: str) -> dict:
    return _request(domain, "POST", "/environments", token=token, json={"name": name})


def rename_environment(domain: str, token: str, environment_id: int, name: str) -> dict:
    return _request(domain, "PATCH", f"/environments/{environment_id}", token=token, json={"name": name})


def delete_environment(domain: str, token: str, environment_id: int) -> dict:
    return _request(domain, "DELETE", f"/environments/{environment_id}", token=token)


def add_host_to_environment(domain: str, token: str, environment_id: int, host_id: int) -> dict:
    return _request(domain, "PUT", f"/environments/{environment_id}/hosts/{host_id}", token=token)


def remove_host_from_environment(domain: str, token: str, environment_id: int, host_id: int) -> dict:
    return _request(domain, "DELETE", f"/environments/{environment_id}/hosts/{host_id}", token=token)


# --- Profile -----------------------------------------------------------------
def get_profile(domain: str, token: str) -> dict:
    return _request(domain, "GET", "/profile", token=token)


def update_profile(domain: str, token: str, **fields) -> dict:
    """PATCH /profile -- merge-updates only the given fields (any subset),
    e.g. update_profile(domain, token, email="a@b.com")."""
    return _request(domain, "PATCH", "/profile", token=token, json=fields)


# --- Attended host / native-dialog-approval simulation ------------------------
def set_attended_host(domain: str, token: str, host_id: int) -> dict:
    return _request(domain, "PUT", "/users/me/attended-host", token=token, json={"host_id": host_id})


def get_attended_host(domain: str, token: str) -> dict:
    return _request(domain, "GET", "/users/me/attended-host", token=token)


def clear_attended_host(domain: str, token: str) -> dict:
    return _request(domain, "DELETE", "/users/me/attended-host", token=token)


def list_pending_approvals(domain: str, device_token: str, wait_seconds: float | None = None) -> dict:
    """GET /hosts/pending-approvals -- the attended daemon's own long-poll,
    device_token-gated. Stands in for the real native-dialog relay: any
    device_token that's currently the attended host for its user's account
    can call this (and decide_pending_approval below) exactly as a real
    daemon would -- see auth_service/main.py's list_pending_approvals_for_
    attended_host, which has no binding to which daemon actually dispatches
    a given call."""
    params = {"wait_seconds": wait_seconds} if wait_seconds is not None else None
    return _request(domain, "GET", "/hosts/pending-approvals", token=device_token, params=params)


def decide_pending_approval(domain: str, device_token: str, approval_id: str, decision: str) -> dict:
    return _request(
        domain,
        "POST",
        f"/hosts/pending-approvals/{approval_id}/decision",
        token=device_token,
        json={"decision": decision},
    )


def get_pending_approval(domain: str, token: str, approval_id: str, wait_seconds: float | None = None) -> dict:
    """GET /hosts/pending-approvals/{id} -- the SUBMITTER's own long-poll
    (session token, not device_token), for learning a decision that landed
    via the *other* channel (a real native dialog, or a harness
    `respond-approvals` session acting as the attended host) -- the
    counterpart to list_pending_approvals/decide_pending_approval above,
    which are the attended-device side of the same exchange."""
    params = {"wait_seconds": wait_seconds} if wait_seconds is not None else None
    return _request(domain, "GET", f"/hosts/pending-approvals/{approval_id}", token=token, params=params)


# --- Real-daemon pairing (manual verification only, not CI-automatable) ------
def _pair_url_scheme(auth_domain: str) -> str:
    """"casper-dev" for a dev deployment (auth_domain starting with
    "dev-", this project's established dev/prod naming convention), else
    "casper" -- mirrors auth_service/utils/auth.py's own
    pair_url_scheme (duplicated, not imported -- independently deployed
    services). Must match whichever scheme the TARGET daemon build
    actually registered (see build_go_macos.sh's DEPLOY_ENV-driven
    URL_SCHEME) -- a dev build and a prod build register different
    schemes specifically so they can coexist on one machine without
    LaunchServices routing a pairing link to the wrong one."""
    return "casper-dev" if auth_domain.strip().lower().startswith("dev-") else "casper"


def trigger_real_pairing(domain: str, username: str, token: str) -> None:
    """Fires the same <scheme>://pair hand-off a browser sign-in click
    would, via `open` -- the OS-level Apple Event dispatch this depends on
    is macOS-only (matching this project's current single-platform
    reality), and requires an already-installed, registered Casper.app on
    this same machine (it doesn't need to already be running -- `open`
    launches it fresh if not, and the event still delivers). Doesn't call
    auth_service itself; the daemon that receives the event does that,
    exactly as it does for a real sign-in. Not automatable in CI -- see
    harness/cli.py's `pair-daemon` command, the intended manual-
    verification entry point. domain picks the scheme (see
    _pair_url_scheme) -- get this wrong (e.g. dev domain, prod-built
    daemon) and the event silently reaches no daemon at all, not the
    wrong one, since nothing registered that scheme."""
    if sys.platform != "darwin":
        raise RuntimeError("trigger_real_pairing is macOS-only (pairing is dispatched via an Apple Event).")
    scheme = _pair_url_scheme(domain)
    url = f"{scheme}://pair?token={quote(token, safe='')}&username={quote(username, safe='')}"
    subprocess.run(["open", url], check=True)


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
    domain: str,
    token: str,
    layer_id: int,
    positional_constraints: list[dict],
    option_constraints: list[dict],
    tier: str,
    cwd: dict | None = None,
) -> dict:
    body = {"positional_constraints": positional_constraints, "option_constraints": option_constraints, "tier": tier}
    if cwd is not None:
        body["cwd"] = cwd
    return _request(domain, "POST", f"/policy-layers/{layer_id}/rules", token=token, json=body)


def delete_policy_layer_rule(domain: str, token: str, layer_id: int, rule_id: int) -> dict:
    return _request(domain, "DELETE", f"/policy-layers/{layer_id}/rules/{rule_id}", token=token)


def _option_from_yaml(option: dict) -> dict:
    """YAML's flat {short, long, whitelist, blacklist, path_resolution} ->
    the API's nested {short, long, pattern: {whitelist, blacklist,
    path_resolution}} -- the one translation the YAML authoring format
    needs, since a whitelist/blacklist/path_resolution key genuinely
    belongs to a nested Pattern everywhere else in this schema too
    (positional constraints -- and cwd, see load_policy_layer_yaml -- are
    Pattern objects directly, no flattening needed there)."""
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
    if "path_resolution" in option:
        pattern["path_resolution"] = option["path_resolution"]
    result["pattern"] = pattern
    return result


def load_policy_layer_yaml(yaml_path: str) -> dict:
    """Parses a policy layer YAML file into {"name": ..., "rules": [...]}
    already shaped for create_policy_layer_rule -- a rule is
    {tier, positional: [{whitelist?, blacklist?, path_resolution?}, ...],
    options: [{short?, long?, whitelist?, blacklist?, path_resolution?}, ...],
    cwd?: {whitelist?, blacklist?}}. Blank whitelist/blacklist keys are
    simply omitted -- the API already defaults them ("" /
    BLACKLIST_MATCHES_NOTHING) server-side, so this format never needs to
    duplicate that sentinel itself. path_resolution is never valid on cwd
    (see Pattern's own docstring in auth_service/models.py for why) -- this
    format doesn't accept one there, same as the API itself doesn't expect
    one to matter."""
    import yaml

    with open(yaml_path) as f:
        spec = yaml.safe_load(f) or {}
    name = spec["name"]
    rules = []
    for rule_spec in spec.get("rules", []):
        rule = {
            "positional_constraints": list(rule_spec.get("positional", [])),
            "option_constraints": [_option_from_yaml(o) for o in rule_spec.get("options", [])],
            "tier": rule_spec.get("tier", "ask"),
        }
        if "cwd" in rule_spec:
            rule["cwd"] = {k: v for k, v in rule_spec["cwd"].items() if k in ("whitelist", "blacklist")}
        rules.append(rule)
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
        create_policy_layer_rule(
            domain,
            token,
            layer_id,
            rule["positional_constraints"],
            rule["option_constraints"],
            rule["tier"],
            cwd=rule.get("cwd"),
        )

    refreshed = list_policy_layers(domain, token)["policy_layers"]
    return next(l for l in refreshed if l["id"] == layer_id)


# --- Policy evaluation ---------------------------------------------------
def eval_policy(
    domain: str,
    token: str,
    policy_layer_ids: list[int],
    positional_args: list[str] | None = None,
    options: list[dict] | None = None,
    cwd: str = "",
) -> dict:
    body = {
        "policy_layer_ids": policy_layer_ids,
        "positional_args": positional_args or [],
        "options": options or [],
        "cwd": cwd,
    }
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
