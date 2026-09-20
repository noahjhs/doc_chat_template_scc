"""The tool-calling orchestration loop backing POST /conversations/step
(casper_service/main.py) -- originally a Streamlit page's own tool-calling
loop (_process_turn/_dispatch_tool_call/call_local_agent/
call_transfer_file), moved server-side and made stateless as part of
deprecating that GUI in favor of a CLI+library test harness. Key design
points that differ from a typical request-scoped web handler:

- No streaming -- client.responses.create(..., stream=False). Streaming is
  out of scope until a real GUI is back in play; this is a deliberate,
  temporary regression, not an oversight.
- No caller-supplied host connection details -- callers get a `configs`
  dict built by main.py from this service's OWN state (see
  _connected_host_configs there), not accepted from the request body. A
  caller only ever names a host by label (same as run_shell_command's own
  `host` field always worked), never a URL/API key.
- Turn state (the `turn` dict) is the whole state machine, externalized --
  every call to run_turn() takes it in and hands it back; there is no
  server-side session/conversation store. A turn that pauses for approval
  records that fact ON the turn itself (turn["awaiting_approval"]) rather
  than in some separate store, so resuming it is just calling run_turn()
  again with the same turn and an approval_decision this time.
- `mock`: when true, the real HTTP call to a daemon (or, for a
  "server storage" transfer, to the local filesystem) is skipped in favor
  of a canned/synthetic result -- the OpenAI call itself is never mocked by
  this flag; a test suite that wants to avoid live API calls entirely
  should inject/patch the `client` argument instead (see
  tests/test_conversations.py)."""

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import requests

from policy import compose_policy, describe_rule

MODEL = "gpt-4.1-mini"

SERVER_STORAGE = "server storage"

BUILTIN_TOOLS = [
    {"type": "web_search"},
    {"type": "code_interpreter", "container": {"type": "auto"}},
    {"type": "image_generation"},
]


@dataclass
class DispatchContext:
    """Everything a dispatched tool call needs that isn't part of the call
    itself -- bundled so run_turn/_drain_pending_calls/_dispatch_tool_call
    don't have to thread five separate parameters through each other.
    read_server_storage/write_server_storage close over the calling user's
    own storage directory and cap-check logic (see main.py's
    _make_storage_io) -- kept as callbacks rather than passing a raw path
    so this module never needs to know casper_service's storage layout or
    STORAGE_CAP_BYTES itself. create_pending_approval likewise closes over
    the calling user_id (see main.py's _create_pending_approval_record) --
    called with (description, turn_snapshot), returns an approval_id; the
    turn snapshot is what makes the pending approval durable/resumable
    from any interface, not just the one that paused it (see db.py's
    pending_approvals table)."""

    configs: dict[str, dict]
    default_host: str | None
    mock: bool
    read_server_storage: Callable[[str], str | None]
    write_server_storage: Callable[[str, str], tuple[str | None, int]]
    create_pending_approval: Callable[[str, dict], str]
    # The calling user's own custom instructions (see models.py's
    # ProfileInfo.system_prompt) -- passed as the Responses API's own
    # `instructions` on every hop of run_turn. Blank means none: the
    # model's own default behavior, unchanged from before this field
    # existed.
    system_prompt: str = ""
    # Explicit tier to simulate for a MOCKED run_shell_command call --
    # consulted only when mock is True. Mocking never evaluates policy
    # itself (see this module's own docstring): the caller supplies the
    # tier outright, and this only simulates (a) what the daemon's own
    # dispatch result would have been (for "allow", or an approved resend)
    # and (b) that the call is pausing for approval (for "ask") -- (b)'s
    # actual resolution still goes through the real, unmocked
    # pending_approvals/create_pending_approval machinery above,
    # completely unaffected by this flag. Defaults to "allow" -- the
    # common case for a mocked call is "let it run and show me the canned
    # result", same posture today's mock branch already had before there
    # was any tier to pick.
    mock_tier: str = "allow"


def _empty_aggregate() -> dict:
    return {
        "searches": [],
        "sources": [],
        "code_blocks": [],
        "transfer_calls": [],
        "shell_command_calls": [],
        "image": None,
    }


def new_turn_from_message(message: str) -> dict:
    return {
        "input": [{"role": "user", "content": message}],
        "aggregate": _empty_aggregate(),
        "full_response": "",
        "pending_calls": None,
        "outputs": None,
        "previous_response_id": None,
        "awaiting_approval": None,
    }


def new_turn_from_tool_call(name: str, arguments: dict) -> dict:
    """Testing-only escape hatch -- injects a tool call directly, as if the
    model had already proposed it, instead of crafting a prompt that talks
    the model into calling it. This is what the harness's `call tool`/
    `call mock tool` verbs use. Pre-seeding pending_calls/outputs like this
    skips straight past "call the model" into the exact same tier-decision/
    dispatch code a real model-issued call goes through. The outputs entry
    establishes the function_call half of the exchange for the model's
    benefit on the NEXT hop (a real call has this already, from the
    model's own prior response); without it, that hop's
    function_call_output would have no matching function_call anywhere in
    the model's context."""
    call_id = f"forced-{uuid.uuid4().hex[:8]}"
    arguments_json = json.dumps(arguments)
    return {
        "input": [{"role": "user", "content": f"(tool call: {name})"}],
        "aggregate": _empty_aggregate(),
        "full_response": "",
        "pending_calls": [{"call_id": call_id, "name": name, "arguments": arguments_json}],
        "outputs": [{"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments_json}],
        "previous_response_id": None,
        "awaiting_approval": None,
    }


def is_in_flight(turn: dict | None) -> bool:
    """True for a turn that's mid-hop or paused for an approval -- the
    signal main.py uses to decide whether an incoming request is resuming
    an existing turn (only approval_decision valid) vs. starting a new one
    in the same conversation (message/tool_call required, previous
    turn's previous_response_id carried forward)."""
    return turn is not None and (turn.get("pending_calls") is not None or turn.get("awaiting_approval") is not None)


def _build_transfer_tool(configs: dict[str, dict]) -> dict:
    hosts_and_storage = list(configs.keys()) + [SERVER_STORAGE]
    return {
        "type": "function",
        "name": "transfer_file",
        "description": (
            "Move a file between a connected machine and another connected "
            "machine, or between a connected machine and the user's own "
            "server storage (a small, private file store independent of any "
            "machine -- use the location 'server storage' for that side of "
            "the transfer). Reads the file from the source and writes it at "
            "the destination; the source file is left in place. Limited to "
            "files up to 10MB each."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": hosts_and_storage,
                    "description": "Where to read the file from.",
                },
                "source_path": {
                    "type": "string",
                    "description": (
                        "Path to the file on the source. For a machine, "
                        "absolute or relative to its current directory. For "
                        "server storage, just the filename."
                    ),
                },
                "destination": {
                    "type": "string",
                    "enum": hosts_and_storage,
                    "description": "Where to write the file to.",
                },
                "destination_path": {
                    "type": "string",
                    "description": "Target path on the destination -- same rules as source_path.",
                },
            },
            "required": ["source", "source_path", "destination", "destination_path"],
        },
    }


def _build_shell_tool(configs: dict[str, dict]) -> dict | None:
    """One tool covering every connected host's shell access -- host-scoped,
    not layer-scoped (see Phase 1's host-scoped auto-compose): the model
    supplies which host to run on, and the daemon (and this description's
    own preview) composes whatever policy layers are attached to THAT host
    into its Policy. Returns None (no tool at all) when no connected host
    has any policy layers attached, mirroring how transfer_file is only
    added when configs is non-empty."""
    lines = []
    for host, config in configs.items():
        composed = compose_policy(config["policy_layers"])
        if not composed:
            continue
        lines.append(f"- host={host!r}:")
        for _, rule in composed:
            lines.append(f"    {describe_rule(rule)}")
    if not lines:
        return None
    description = (
        "Run a shell command on a connected machine -- not arbitrary shell "
        "access, subject to that host's own configured policy. Supply the "
        "binary and its arguments as positional_args (index 0 MUST be the "
        "binary itself) plus any options; the daemon checks the call "
        "against the host's policy rules IN ORDER, first match wins -- if "
        "nothing matches, the call is rejected outright, don't retry it "
        "unchanged. Each connected host's current effective policy:\n" + "\n".join(lines) + "\n"
        "A matched rule's tier controls what happens next: 'ask' pauses for "
        "the user's explicit approval before it actually runs; 'allow' runs "
        "immediately; 'deny' always rejects it without running or "
        "prompting -- don't bother retrying a 'deny' call either."
    )
    return {
        "type": "function",
        "name": "run_shell_command",
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "positional_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The command's positional arguments in order -- index 0 MUST be the binary/executable name itself.",
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "short": {"type": "string", "description": "The option's short form, e.g. 'f' for -f."},
                            "long": {
                                "type": "string",
                                "description": "The option's long form, e.g. 'force' for --force.",
                            },
                            "value": {
                                "type": "string",
                                "description": "The option's value, if any -- omit entirely for a valueless option.",
                            },
                        },
                    },
                    "description": (
                        "Any options (not positional args) the command should be invoked with. "
                        "At least one of short/long is required per entry."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Optional -- directory to run in. Defaults to the machine's own confined directory.",
                },
                "host": {
                    "type": "string",
                    "enum": list(configs.keys()),
                    "description": (
                        "Which connected machine to run this on. Usually fine to "
                        "omit -- defaults to the only connected host (or "
                        "whichever default_host was supplied)."
                    ),
                },
            },
            "required": ["positional_args"],
        },
    }


def build_tools(configs: dict[str, dict]) -> list[dict]:
    tools = list(BUILTIN_TOOLS)
    if configs:
        tools.append(_build_transfer_tool(configs))
    shell_tool = _build_shell_tool(configs)
    if shell_tool:
        tools.append(shell_tool)
    return tools


def daemon_error_detail(response, fallback: str) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return detail or fallback


def _fetch_local_json(config: dict, action: str, **kwargs) -> dict:
    """POSTs one daemon action and returns the parsed response dict (or an
    error dict in the same {success, stdout, stderr} shape commands.Result
    already uses) -- used by _call_transfer_file's read/write-a-connected-
    host halves."""
    try:
        response = requests.post(
            f"{config['url']}/api/command",
            json={"action": action, **kwargs},
            headers={"X-API-Key": config["api_key"]},
            timeout=15,
        )
        if response.status_code == 401:
            return {"success": False, "stdout": "", "stderr": "Invalid API key."}
        if response.status_code >= 400:
            return {"success": False, "stdout": "", "stderr": daemon_error_detail(response, f"HTTP {response.status_code}")}
        return response.json()
    except requests.RequestException as e:
        return {"success": False, "stdout": "", "stderr": str(e)}


def resolve_host(
    configs: dict[str, dict], host: str | None, default_host: str | None
) -> tuple[str | None, dict | None, str | None]:
    """host selection isn't in any tool schema's "required" list (there's no
    way to say "required only when there's more than one option" in JSON
    Schema), so ambiguity is enforced here instead. Returns
    (resolved_host, config, None) on success or (None, None, error_message)
    otherwise -- resolved_host is returned (not just the config) so a
    caller that auto-picked "the only connected host" (host/default_host
    both unset) can still record/display which host that actually was,
    e.g. in a pending-approval prompt."""
    if not configs:
        return None, None, "no connected machines available."
    if host is None:
        if len(configs) == 1:
            host = next(iter(configs))
        elif default_host in configs:
            host = default_host
        else:
            available = ", ".join(configs)
            return None, None, f"multiple machines connected ({available}) -- specify which one via 'host'."
    config = configs.get(host)
    if config is None:
        available = ", ".join(configs)
        return None, None, f"unknown host {host!r}. Available: {available}."
    return host, config, None


def _dispatch_shell_command(
    configs: dict[str, dict],
    args: dict,
    host: str | None,
    default_host: str | None,
    mock: bool,
    mock_tier: str,
    approved: bool,
) -> tuple[str, str, str | None]:
    """The ONE place a run_shell_command call round-trips to a daemon --
    replaces the old split (a local tier decision via match_policy, THEN a
    separate dispatch call for "allow") with a single call that returns
    everything: (tier, output_text, resolved_host). No local policy
    evaluation happens here at all -- the daemon decides allow/ask/deny
    authoritatively (see agent/internal/commands/policy.go's
    runRunShellCommand); this only forwards the call and interprets its
    verdict.

    approved=True marks a RESEND of a call the user already approved via
    the durable pending_approvals flow (see run_turn's own resume branch)
    -- carried straight through to the daemon's own Request.Approved,
    consulted by the daemon ONLY when it re-matches this exact call to
    tier "ask" again; a policy change that's since made it "deny" is never
    overridden by an old approval, and the daemon has no memory of the
    earlier "ask" itself.

    mock never evaluates policy either (see DispatchContext.mock_tier) --
    it takes the caller-supplied mock_tier outright and simulates ONLY
    what the daemon's own dispatch result would have been (for "allow"/an
    approved resend) or that the call is pausing (for "ask", with an
    unapproved resend never actually happening for a mocked call in
    practice, but handled the same way regardless, for symmetry with the
    real branch below)."""
    resolved_host, config, err = resolve_host(configs, host, default_host)
    if err:
        return "deny", f"Shell command error: {err}", resolved_host

    if mock:
        if mock_tier == "deny":
            return "deny", "Denied by policy (no matching rule allows this call).", resolved_host
        if mock_tier == "ask" and not approved:
            return "ask", "", resolved_host
        return (
            "allow",
            json.dumps(
                {
                    "mock": True,
                    "action": "run_shell_command",
                    "positional_args": args.get("positional_args"),
                    "options": args.get("options"),
                    "path": args.get("path"),
                }
            ),
            resolved_host,
        )

    try:
        response = requests.post(
            f"{config['url']}/api/command",
            json={
                "action": "run_shell_command",
                "positional_args": args.get("positional_args") or [],
                "options": args.get("options") or [],
                "path": args.get("path"),
                "approved": approved,
            },
            headers={"X-API-Key": config["api_key"]},
            timeout=15,
        )
    except requests.RequestException as e:
        return "deny", f"Shell command error: {e}", resolved_host
    if response.status_code == 401:
        return "deny", "Shell command error: invalid API key.", resolved_host
    if response.status_code >= 400:
        return "deny", f"Shell command error: {daemon_error_detail(response, f'HTTP {response.status_code}')}", resolved_host

    result = response.json()
    tier = result.get("tier") or "deny"
    if tier == "deny":
        return "deny", "Denied by policy (no matching rule allows this call).", resolved_host
    if tier == "ask" and not approved:
        return "ask", "", resolved_host
    return "allow", json.dumps(result), resolved_host


def _call_transfer_file(configs, source, source_path, destination, destination_path, ctx: DispatchContext) -> str:
    if ctx.mock:
        return json.dumps(
            {
                "mock": True,
                "action": "transfer_file",
                "source": source,
                "source_path": source_path,
                "destination": destination,
                "destination_path": destination_path,
            }
        )

    if source == SERVER_STORAGE:
        content = ctx.read_server_storage(source_path)
        if content is None:
            return f"Transfer error: couldn't read {source_path!r} from server storage: file not found."
    else:
        config = configs.get(source)
        if config is None:
            available = ", ".join(list(configs) + [SERVER_STORAGE])
            return f"Transfer error: unknown source {source!r}. Available: {available}."
        read_result = _fetch_local_json(config, "read_file", path=source_path)
        if not read_result.get("success"):
            return f"Transfer error: couldn't read {source_path!r} from {source}: {read_result.get('stderr') or 'unknown error'}"
        content = read_result.get("stdout", "")

    if destination == SERVER_STORAGE:
        # Server storage is flat (no subdirectories -- see casper_service's
        # _safe_filename), so a destination_path with directory components
        # just contributes its basename.
        filename = destination_path.replace("\\", "/").rsplit("/", 1)[-1]
        error, size = ctx.write_server_storage(filename, content)
        if error:
            return f"Transfer error: couldn't write {filename!r} to server storage: {error}"
        return f"Transferred {source_path!r} from {source} to server storage as {filename!r} ({size} bytes)."
    config = configs.get(destination)
    if config is None:
        available = ", ".join(list(configs) + [SERVER_STORAGE])
        return f"Transfer error: unknown destination {destination!r}. Available: {available}."
    write_result = _fetch_local_json(config, "write_file", path=destination_path, content=content)
    if not write_result.get("success"):
        return f"Transfer error: couldn't write {destination_path!r} to {destination}: {write_result.get('stderr') or 'unknown error'}"
    return f"Transferred {source_path!r} from {source} to {destination_path!r} on {destination}."


def _describe_call_args(positional_args: list[str], options: list[dict]) -> str:
    """Human-readable rendering of a proposed call's arguments -- used to
    build a pending approval's description (see db.py's pending_approvals
    table), shown identically wherever a user checks in to answer it."""
    parts = list((positional_args or [])[1:])
    for opt in options or []:
        name = f"--{opt['long']}" if opt.get("long") else f"-{opt.get('short')}"
        parts.append(f"{name} {opt['value']}" if opt.get("value") is not None else name)
    return " ".join(parts)


def _dispatch_tool_call(call: dict, ctx: DispatchContext, aggregate: dict) -> str:
    """Executes ONE already-decided tool call -- transfer_file only.
    run_shell_command never goes through here: it's dispatched directly by
    _drain_pending_calls (a fresh call) or run_turn's own resume branch
    (an approved resend), both via _dispatch_shell_command, since those are
    the only two places that know whether a given dispatch is a fresh
    attempt or an approved resend."""
    args = json.loads(call["arguments"])
    if call["name"] == "transfer_file":
        output = _call_transfer_file(
            ctx.configs, args.get("source"), args.get("source_path"), args.get("destination"), args.get("destination_path"), ctx
        )
        aggregate["transfer_calls"].append({"args": args, "output": output})
    else:
        output = f"Unknown tool: {call['name']}"
    return output


def _drain_pending_calls(turn: dict, ctx: DispatchContext) -> bool:
    """Processes turn["pending_calls"] one at a time until either the list
    is empty (returns False) or a run_shell_command needing approval pauses
    it (sets turn["awaiting_approval"] and returns True) -- the call stays
    at the front of pending_calls in that case, popped only once resolved
    (see run_turn's own resume branch). run_shell_command makes exactly one
    round trip to the daemon (see _dispatch_shell_command) -- no local
    policy evaluation happens here at all; the daemon's own verdict is
    what decides allow/ask/deny."""
    while turn["pending_calls"]:
        call = turn["pending_calls"][0]
        if call["name"] == "run_shell_command":
            args = json.loads(call["arguments"])
            tier, output, resolved_host = _dispatch_shell_command(
                ctx.configs, args, args.get("host"), ctx.default_host, ctx.mock, ctx.mock_tier, approved=False
            )
            if tier == "ask":
                positional_args = args.get("positional_args") or []
                binary = positional_args[0] if positional_args else ""
                command_desc = f"{binary} {_describe_call_args(positional_args, args.get('options') or [])}".strip()
                description = f"run `{command_desc}` on {resolved_host}" if resolved_host else f"run `{command_desc}`"
                # awaiting_approval is set BEFORE create_pending_approval is
                # called, so the snapshot it persists (see db.py's
                # pending_approvals table) is already directly resumable via
                # run_turn -- approval_id itself is filled in after, since it
                # can only be known once the record exists; run_turn's own
                # resume branch never reads it back, only external callers
                # (harness, SMS) use it as a lookup handle.
                turn["awaiting_approval"] = {
                    "call_id": call["call_id"],
                    "host": resolved_host,
                    "args": args,
                }
                turn["awaiting_approval"]["approval_id"] = ctx.create_pending_approval(description, turn)
                return True
            turn["aggregate"]["shell_command_calls"].append({"args": args, "output": output})
            turn["outputs"].append({"type": "function_call_output", "call_id": call["call_id"], "output": output})
            turn["pending_calls"].pop(0)
            continue
        output = _dispatch_tool_call(call, ctx, turn["aggregate"])
        turn["outputs"].append({"type": "function_call_output", "call_id": call["call_id"], "output": output})
        turn["pending_calls"].pop(0)
    return False


def _extract_response(response) -> tuple[str, dict]:
    """Pulls the finished text, tool calls, and tool-activity metadata out
    of one non-streaming Response object's own .output list. Uses getattr
    defensively (not direct attribute access) so a minimal
    fake Response in tests only needs to populate the fields a given test
    actually cares about."""
    meta = {"searches": [], "sources": [], "code_blocks": [], "function_calls": [], "image": None}
    for item in getattr(response, "output", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "image_generation_call" and getattr(item, "result", None):
            meta["image"] = item.result
        elif item_type == "web_search_call" and getattr(item, "action", None):
            meta["searches"].append(item.action.query)
        elif item_type == "code_interpreter_call" and getattr(item, "code", None):
            meta["code_blocks"].append(item.code)
        elif item_type == "function_call":
            meta["function_calls"].append({"call_id": item.call_id, "name": item.name, "arguments": item.arguments})
        elif item_type == "message":
            for content in getattr(item, "content", None) or []:
                for annotation in getattr(content, "annotations", None) or []:
                    if getattr(annotation, "type", None) == "url_citation":
                        meta["sources"].append((annotation.title, annotation.url))
    hop_text = getattr(response, "output_text", "") or ""
    return hop_text, meta


def run_turn(turn: dict, approval_decision: str | None, ctx: DispatchContext, client: Any) -> tuple[str, str | None]:
    """Runs (or resumes, after an ask-tier approval/denial) the tool-calling
    loop for `turn` -- mutates it in place and returns (status, message):
    status is "done" (message is the finished turn's full text) or
    "pending_approval" (message is None; turn["awaiting_approval"] holds
    what's waiting). See this module's own docstring for the overall
    design."""
    while True:
        if turn["awaiting_approval"] is not None:
            if approval_decision is None:
                return "pending_approval", None
            pending = turn["awaiting_approval"]
            turn["awaiting_approval"] = None
            if approval_decision == "deny":
                output = "Denied by user."
            else:
                # RESEND the identical call with Approved=true -- the
                # daemon re-checks its own policy fresh (it remembers
                # nothing about the earlier "ask") and only executes if
                # the call still matches an "ask" (or now "allow") rule; a
                # policy change to "deny" since the ask always wins
                # regardless of this approval (_dispatch_shell_command
                # already produces the right denial text for that case).
                args = pending["args"]
                _tier, output, _resolved_host = _dispatch_shell_command(
                    ctx.configs, args, args.get("host"), ctx.default_host, ctx.mock, ctx.mock_tier, approved=True
                )
            turn["aggregate"]["shell_command_calls"].append({"args": pending["args"], "output": output})
            turn["outputs"].append({"type": "function_call_output", "call_id": pending["call_id"], "output": output})
            turn["pending_calls"].pop(0)
            approval_decision = None  # only ever applies to the one call that was actually paused

        if turn["pending_calls"] is None:
            response = client.responses.create(
                model=MODEL,
                input=turn["input"],
                instructions=ctx.system_prompt or None,
                previous_response_id=turn["previous_response_id"],
                tools=build_tools(ctx.configs),
                stream=False,
            )
            turn["previous_response_id"] = response.id
            hop_text, meta = _extract_response(response)
            turn["full_response"] += hop_text
            turn["aggregate"]["searches"].extend(meta["searches"])
            turn["aggregate"]["sources"].extend(meta["sources"])
            turn["aggregate"]["code_blocks"].extend(meta["code_blocks"])
            if meta["image"] is not None:
                turn["aggregate"]["image"] = meta["image"]
            if not meta["function_calls"]:
                return "done", turn["full_response"]
            turn["pending_calls"] = meta["function_calls"]
            turn["outputs"] = []

        paused = _drain_pending_calls(turn, ctx)
        if paused:
            return "pending_approval", None

        turn["input"] = turn["outputs"]
        turn["pending_calls"] = None
        turn["outputs"] = None
