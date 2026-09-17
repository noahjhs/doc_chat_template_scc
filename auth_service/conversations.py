"""The tool-calling orchestration loop backing POST /conversations/step
(auth_service/main.py) -- originally a Streamlit page's own tool-calling
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

from policy import compose_policy, describe_rule, match_policy

MODEL = "gpt-4.1-mini"

SERVER_STORAGE = "server storage"

# Mirrors utils/sidebar.py's own COMMAND_CATEGORIES exactly -- duplicated,
# not imported, since auth_service is a separate, independently-deployed
# service with no shared package to pull it from (same posture as
# BLACKLIST_MATCHES_NOTHING's own duplication in models.py). Keep in sync
# by hand.
COMMAND_CATEGORIES = {
    "Git": ["status", "branch", "log"],
    "Navigation": ["pwd", "cd", "ls", "tree", "list_directories"],
    "Management": ["mkdir", "touch", "cp", "mv", "rm", "rmdir"],
    "Viewing & Searching": ["cat", "less", "head", "tail", "grep", "find"],
}

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
    so this module never needs to know auth_service's storage layout or
    STORAGE_CAP_BYTES itself. create_pending_approval likewise closes over
    the calling user_id (see main.py's _create_pending_approval_record)."""

    configs: dict[str, dict]
    default_host: str | None
    mock: bool
    read_server_storage: Callable[[str], str | None]
    write_server_storage: Callable[[str, str], tuple[str | None, int]]
    create_pending_approval: Callable[[str, str, str, str], str]


def _empty_aggregate() -> dict:
    return {
        "searches": [],
        "sources": [],
        "code_blocks": [],
        "local_agent_calls": [],
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


def _build_local_agent_tool(configs: dict[str, dict]) -> dict:
    return {
        "type": "function",
        "name": "run_local_command",
        "description": (
            "Run a command on the user's local machine via Casper, their local "
            "agent -- not arbitrary shell access, but a fixed, allowlisted set "
            "of commands, confined to whichever directories the user has "
            "explicitly added on that machine (it can't read, write, or "
            "navigate outside those trees; use 'list_directories' to see what's "
            "currently addressable -- there may be none yet). "
            "Categories: Git (status/branch/log), Navigation "
            "(pwd/cd/ls/tree/list_directories), Management (mkdir/touch/cp/mv/"
            "rm/rmdir -- 'rm' only deletes a file and 'rmdir' only an "
            "already-empty directory, never recursively), and Viewing & "
            "Searching (cat/less/head/tail/grep/find)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [cmd for commands in COMMAND_CATEGORIES.values() for cmd in commands],
                    "description": "Which local command to run.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Target file or directory. Absolute, or relative to the "
                        "current directory. Required by most actions except the "
                        "git ones and 'pwd'; for 'cp'/'mv' this is the source."
                    ),
                },
                "destination": {
                    "type": "string",
                    "description": "Destination path -- only used by 'cp' and 'mv'.",
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Search pattern -- a regex for 'grep', a filename glob "
                        "like '*.py' for 'find' (defaults to matching everything)."
                    ),
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of lines -- only used by 'head' and 'tail' (default 10).",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max results -- git log entry count, or max matches for "
                        "'grep'/'find' (default 5)."
                    ),
                },
                "host": {
                    "type": "string",
                    "enum": list(configs.keys()),
                    "description": (
                        "Which connected machine to run this on. Usually fine "
                        "to omit -- if the user is talking about a specific "
                        "one, name it explicitly, but otherwise it defaults to "
                        "the only connected host (or whichever default_host "
                        "was supplied)."
                    ),
                },
            },
            "required": ["action"],
        },
    }


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
    has any policy layers attached, mirroring how run_local_command/
    transfer_file are only added when configs is non-empty."""
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
                    "description": "Optional -- which addressable directory to run in. Defaults to the currently selected directory.",
                },
                "host": {
                    "type": "string",
                    "enum": list(configs.keys()),
                    "description": (
                        "Which connected machine to run this on. Usually fine to "
                        "omit -- defaults to the only connected host (or "
                        "whichever default_host was supplied), same as "
                        "run_local_command."
                    ),
                },
            },
            "required": ["positional_args"],
        },
    }


def build_tools(configs: dict[str, dict]) -> list[dict]:
    tools = list(BUILTIN_TOOLS)
    if configs:
        tools.append(_build_local_agent_tool(configs))
        tools.append(_build_transfer_tool(configs))
    shell_tool = _build_shell_tool(configs)
    if shell_tool:
        tools.append(shell_tool)
    return tools


def _daemon_error_detail(response, fallback: str) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return detail or fallback


def _fetch_local_json(config: dict, action: str, **kwargs) -> dict:
    """Like _call_local_agent below, but returns the parsed response dict
    (or an error dict in the same {success, stdout, stderr} shape
    commands.Result already uses) instead of a model-facing string -- used
    by _call_transfer_file's read/write-a-connected-host halves."""
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
            return {"success": False, "stdout": "", "stderr": _daemon_error_detail(response, f"HTTP {response.status_code}")}
        return response.json()
    except requests.RequestException as e:
        return {"success": False, "stdout": "", "stderr": str(e)}


def _resolve_host(
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


def _call_local_agent(configs, action, host, default_host, mock, **kwargs) -> str:
    _resolved_host, config, err = _resolve_host(configs, host, default_host)
    if err:
        return f"Local agent error: {err}"
    if mock:
        return json.dumps({"mock": True, "action": action, **{k: v for k, v in kwargs.items() if v is not None}})
    try:
        response = requests.post(
            f"{config['url']}/api/command",
            json={"action": action, **kwargs},
            headers={"X-API-Key": config["api_key"]},
            timeout=15,
        )
        if response.status_code == 401:
            return "Local agent error: invalid API key."
        if response.status_code >= 400:
            return f"Local agent error: {_daemon_error_detail(response, f'HTTP {response.status_code}')}"
        return json.dumps(response.json())
    except requests.RequestException as e:
        return f"Local agent error: {e}"


def _call_shell_command(configs, positional_args, options, host, default_host, path, mock) -> str:
    _resolved_host, config, err = _resolve_host(configs, host, default_host)
    if err:
        return f"Shell command error: {err}"
    if mock:
        return json.dumps(
            {"mock": True, "action": "run_shell_command", "positional_args": positional_args, "options": options, "path": path}
        )
    try:
        response = requests.post(
            f"{config['url']}/api/command",
            json={"action": "run_shell_command", "positional_args": positional_args, "options": options, "path": path},
            headers={"X-API-Key": config["api_key"]},
            timeout=15,
        )
        if response.status_code == 401:
            return "Shell command error: invalid API key."
        if response.status_code >= 400:
            return f"Shell command error: {_daemon_error_detail(response, f'HTTP {response.status_code}')}"
        return json.dumps(response.json())
    except requests.RequestException as e:
        return f"Shell command error: {e}"


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
        # Server storage is flat (no subdirectories -- see auth_service's
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
    """Human-readable rendering of a proposed call's arguments -- used for
    the pending-approval prompt (both channels: in-chat and native-dialog
    relay)."""
    parts = list((positional_args or [])[1:])
    for opt in options or []:
        name = f"--{opt['long']}" if opt.get("long") else f"-{opt.get('short')}"
        parts.append(f"{name} {opt['value']}" if opt.get("value") is not None else name)
    return " ".join(parts)


def _dispatch_tool_call(call: dict, ctx: DispatchContext, aggregate: dict) -> str:
    """Executes ONE already-decided tool call -- never an ask-tier
    run_shell_command still awaiting approval; _drain_pending_calls
    intercepts those before they ever reach here."""
    args = json.loads(call["arguments"])
    if call["name"] == "run_local_command":
        action = args.get("action", "")
        output = _call_local_agent(
            ctx.configs,
            action,
            host=args.get("host"),
            default_host=ctx.default_host,
            mock=ctx.mock,
            path=args.get("path"),
            destination=args.get("destination"),
            pattern=args.get("pattern"),
            lines=args.get("lines", 10),
            limit=args.get("limit", 5),
        )
        aggregate["local_agent_calls"].append({"action": action, "args": args, "output": output})
    elif call["name"] == "transfer_file":
        output = _call_transfer_file(
            ctx.configs, args.get("source"), args.get("source_path"), args.get("destination"), args.get("destination_path"), ctx
        )
        aggregate["transfer_calls"].append({"args": args, "output": output})
    elif call["name"] == "run_shell_command":
        output = _call_shell_command(
            ctx.configs,
            args.get("positional_args") or [],
            args.get("options") or [],
            host=args.get("host"),
            default_host=ctx.default_host,
            path=args.get("path"),
            mock=ctx.mock,
        )
        aggregate["shell_command_calls"].append({"args": args, "output": output})
    else:
        output = f"Unknown tool: {call['name']}"
    return output


def _decide_tier(resolved_config: dict | None, args: dict) -> str:
    """Finds the first matching rule in the already-composed policy of the
    host resolve_host resolved (None if that failed -- an unresolved host
    has no policy to check, so this falls back to "deny", same "absence
    means deny" posture the daemon itself uses) and returns its tier."""
    composed = compose_policy(resolved_config["policy_layers"]) if resolved_config else []
    roots = (resolved_config or {}).get("workspace") or []
    _, matched_rule = match_policy(composed, args.get("positional_args") or [], args.get("options") or [], roots)
    return matched_rule.tier if matched_rule else "deny"


def _drain_pending_calls(turn: dict, ctx: DispatchContext) -> bool:
    """Processes turn["pending_calls"] one at a time until either the list
    is empty (returns False) or a run_shell_command needing approval pauses
    it (sets turn["awaiting_approval"] and returns True) -- the call stays
    at the front of pending_calls in that case, popped only once resolved
    (see run_turn's own resume branch)."""
    while turn["pending_calls"]:
        call = turn["pending_calls"][0]
        if call["name"] == "run_shell_command":
            args = json.loads(call["arguments"])
            resolved_host, resolved_config, _err = _resolve_host(ctx.configs, args.get("host"), ctx.default_host)
            tier = _decide_tier(resolved_config, args)
            if tier == "deny":
                output = "Denied by policy (no matching rule allows this call)."
                turn["aggregate"]["shell_command_calls"].append({"args": args, "output": output})
                turn["outputs"].append({"type": "function_call_output", "call_id": call["call_id"], "output": output})
                turn["pending_calls"].pop(0)
                continue
            if tier == "ask":
                positional_args = args.get("positional_args") or []
                approval_id = ctx.create_pending_approval(
                    "Shell command",
                    positional_args[0] if positional_args else "",
                    _describe_call_args(positional_args, args.get("options") or []),
                    resolved_host or "",
                )
                turn["awaiting_approval"] = {
                    "call_id": call["call_id"],
                    "host": resolved_host,
                    "args": args,
                    "approval_id": approval_id,
                }
                return True
            # tier == "allow" -- fall through to dispatch below.
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
                turn["aggregate"]["shell_command_calls"].append({"args": pending["args"], "output": output})
            else:
                call = {"call_id": pending["call_id"], "name": "run_shell_command", "arguments": json.dumps(pending["args"])}
                output = _dispatch_tool_call(call, ctx, turn["aggregate"])
            turn["outputs"].append({"type": "function_call_output", "call_id": pending["call_id"], "output": output})
            turn["pending_calls"].pop(0)
            approval_decision = None  # only ever applies to the one call that was actually paused

        if turn["pending_calls"] is None:
            response = client.responses.create(
                model=MODEL,
                input=turn["input"],
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
