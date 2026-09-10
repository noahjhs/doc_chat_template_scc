import base64
import json

import requests
import streamlit as st
from openai import OpenAI

from utils.auth import (
    current_token,
    download_storage,
    require_agent_session,
    require_app_subdomain,
    upload_storage,
)
from utils.branding import NAME
from utils.sidebar import (
    COMMAND_CATEGORIES,
    GIT_ACTIONS,
    _fetch_local_json,
    handle_sign_out_if_requested,
    render_sidebar,
)
from utils.topbar import render_settings_menu

# Same reasoning as casper_app.py's set_page_config -- a consistent tab
# identity across the whole flow.
st.set_page_config(page_title="Casper - Chat", page_icon="👻", initial_sidebar_state="expanded")
require_app_subdomain()


@st.cache_resource
def get_client():
    return OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


# Handles (and st.stop()s on) an in-flight sign-out -- see
# utils/sidebar.py's own docstring for why this has to run before
# require_agent_session() below, not after.
handle_sign_out_if_requested()

username = require_agent_session()
client = get_client()

# render_settings_menu() (utils/topbar.py) is the Settings gear + sign-out
# icon, top-right of the main content -- render_sidebar() (utils/sidebar.py)
# builds the rest: brand, Environment/host selection, workspace directory
# browser, Local commands reference -- identical to pages/environments.py's
# own sidebar, so switching between pages doesn't lose any of that context.
render_settings_menu()
local_agent_configs, selected_host_label = render_sidebar(username)

# Cosmetic: once the query params have been read (above), drop them from
# the visible URL so the address bar just shows .../chat. This only
# rewrites what's displayed (history.replaceState doesn't fire a
# navigation or a popstate event), so it doesn't affect the already-cached
# session_state values above or trigger a rerun. Runs in the *parent* page
# (window.parent), since the script itself executes inside st.iframe's own
# iframe -- st.iframe (not st.markdown(unsafe_allow_html=True)) is what
# actually gets a script to run at all here.
if "_url_cleaned" not in st.session_state:
    st.iframe(
        "<script>window.parent.history.replaceState(null, '', window.parent.location.pathname);</script>",
        height=1,
    )
    st.session_state["_url_cleaned"] = True

# A second, defensive attempt at bringing this tab into focus on its very
# first load after sign-in (see pages/signin.py's own focus() call, right
# before it redirects here, for the first attempt and why this exists at
# all -- a reported case of the tab not ending up focused after sign-in).
# Only once per session, same reasoning as _url_cleaned above.
if "_focus_attempted" not in st.session_state:
    st.iframe("<script>window.parent.focus();</script>", height=1)
    st.session_state["_focus_attempted"] = True

# All the built-in Responses API tools that don't need extra setup (unlike
# file_search, which needs a vector store), plus the local git tool if
# the agent server above is configured.
TOOLS = [
    {"type": "web_search"},
    {"type": "code_interpreter", "container": {"type": "auto"}},
    {"type": "image_generation"},
]
LOCAL_AGENT_TOOL = {
    "type": "function",
    "name": "run_local_command",
    "description": (
        "Run a command on the user's local machine via Casper, their local "
        "agent — not arbitrary shell access, but a fixed, allowlisted set "
        "of commands, confined to whichever directories the user has "
        "explicitly added on that machine (it can't read, write, or "
        "navigate outside those trees; use 'list_directories' to see what's "
        "currently addressable — there may be none yet). "
        "Categories: Git (status/branch/log), Navigation "
        "(pwd/cd/ls/tree/list_directories), Management (mkdir/touch/cp/mv/"
        "rm/rmdir — 'rm' only deletes a file and 'rmdir' only an "
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
                "description": "Destination path — only used by 'cp' and 'mv'.",
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Search pattern — a regex for 'grep', a filename glob "
                    "like '*.py' for 'find' (defaults to matching everything)."
                ),
            },
            "lines": {
                "type": "integer",
                "description": "Number of lines — only used by 'head' and 'tail' (default 10).",
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Max results — git log entry count, or max matches for "
                    "'grep'/'find' (default 5)."
                ),
            },
            "host": {
                "type": "string",
                "enum": list(local_agent_configs.keys()),
                "description": (
                    "Which connected machine to run this on. Usually fine "
                    "to omit -- if the user is talking about a specific "
                    "one ('on the mini', 'my laptop'), name it explicitly, "
                    "but otherwise it defaults to whichever host is "
                    "selected in the sidebar (or the only connected one). "
                    "Ask the user to clarify only if the system reports "
                    "it's still ambiguous."
                ),
            },
        },
        "required": ["action"],
    },
}

# "server storage" is the one location value that's never a key in
# local_agent_configs -- a small, private, per-account file store on the
# server itself (independent of any connected machine, capped at 1GB --
# see auth_service/main.py's STORAGE_CAP_BYTES), useful as a hop between
# two hosts that aren't both online at once, or just as scratch space.
SERVER_STORAGE = "server storage"
TRANSFER_TOOL = {
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
                "enum": list(local_agent_configs.keys()) + [SERVER_STORAGE],
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
                "enum": list(local_agent_configs.keys()) + [SERVER_STORAGE],
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

active_tools = TOOLS + ([LOCAL_AGENT_TOOL, TRANSFER_TOOL] if local_agent_configs else [])

def _build_welcome_message():
    """A status snapshot of the active Environment/selected host, read
    straight from the session_state render_sidebar() (utils/sidebar.py)
    already populated -- rather than plumbing extra return values through
    it, since every value needed here (hosts, Environments, the active
    Environment/selected host ids) is already sitting there once it's run."""
    environments = st.session_state.get("_environments", [])
    hosts_by_id = {h["host_id"]: h for h in st.session_state.get("_hosts", [])}
    active_environment = next(
        (e for e in environments if e["id"] == st.session_state.get("_active_environment_id")), None
    )
    environment_name = active_environment["name"] if active_environment else "None"
    env_host_ids = active_environment["host_ids"] if active_environment else []
    total_hosts = len(env_host_ids)
    active_hosts = sum(1 for host_id in env_host_ids if hosts_by_id.get(host_id, {}).get("connected"))

    lines = [
        "Welcome to Casper!",
        f'Your environment is "{environment_name}"',
        f"consisting of {total_hosts} host{'s' if total_hosts != 1 else ''} ({active_hosts} active).",
    ]

    selected_host = hosts_by_id.get(st.session_state.get("_selected_host_id"))
    if selected_host is None:
        lines.append("No host is currently selected.")
    else:
        directories = selected_host.get("workspace") or []
        lines.append(f"The selected host, {selected_host['label']}, has these folders available:")
        lines.extend(directories if directories else ["(none added yet)"])

    return "\n".join(lines)


if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": _build_welcome_message()},
        {"role": "assistant", "content": "What's on your mind today?"},
    ]
    # The second message reveals itself a couple seconds after the first,
    # rather than both landing at once -- consumed (popped) below on the
    # very next render, so this only ever applies to this pair, once, right
    # after they're first created; a later rerun (the user sending a
    # message, switching Environments, etc.) just renders history normally,
    # instantly, with nothing hidden.
    st.session_state["_delay_second_welcome_message"] = True

# The Responses API tracks conversation history server-side, keyed off the
# previous turn's response id.
if "previous_response_id" not in st.session_state:
    st.session_state.previous_response_id = None


def call_local_agent(local_agent_configs, action, host=None, default_host=None, **kwargs):
    """Call one of the user's connected local agent servers; never raises,
    so a connection failure (or an ambiguous/unknown host) just gets
    reported back to the model as text. host selection isn't in the tool
    schema's "required" list (there's no way to say "required only when
    there's more than one option" in JSON Schema), so ambiguity is enforced
    here instead. default_host is whichever host is currently selected in
    the sidebar (see the host radio above) -- tried before falling back to
    asking the model to specify one."""
    if not local_agent_configs:
        return "Local agent error: no connected machines available."
    if host is None:
        if len(local_agent_configs) == 1:
            host = next(iter(local_agent_configs))
        elif default_host in local_agent_configs:
            host = default_host
        else:
            available = ", ".join(local_agent_configs)
            return f"Local agent error: multiple machines connected ({available}) -- specify which one via 'host'."
    config = local_agent_configs.get(host)
    if config is None:
        available = ", ".join(local_agent_configs)
        return f"Local agent error: unknown host {host!r}. Available: {available}."
    try:
        response = requests.post(
            f"{config['url']}/api/command",
            json={"action": action, **kwargs},
            headers={"X-API-Key": config["api_key"]},
            timeout=15,
        )
        if response.status_code == 401:
            return "Local agent error: invalid API key."
        response.raise_for_status()
        return json.dumps(response.json())
    except requests.RequestException as e:
        return f"Local agent error: {e}"


def call_transfer_file(local_agent_configs, source, source_path, destination, destination_path):
    """Reads source_path from source (a connected host, or the user's own
    server storage) and writes it to destination_path on destination.
    read_file/write_file are binary-safe (base64 over the wire, decoded/
    re-encoded nowhere in between -- the same base64 string just moves
    from one side's response into the other side's request) and each
    capped at 10MB server-side (agent/internal/commands/commands.go's
    maxTransferFileBytes); auth_service separately enforces its own 1GB-
    per-user *total* cap for server storage specifically. Never raises."""
    auth_domain = st.secrets["AUTH_SERVICE_DOMAIN"]

    if source == SERVER_STORAGE:
        result = download_storage(auth_domain, current_token(), source_path)
        if "error" in result:
            return f"Transfer error: couldn't read {source_path!r} from server storage: {result['error']}"
        content = result["content"]
    else:
        config = local_agent_configs.get(source)
        if config is None:
            available = ", ".join(list(local_agent_configs) + [SERVER_STORAGE])
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
        result = upload_storage(auth_domain, current_token(), filename, content)
        if "error" in result:
            return f"Transfer error: couldn't write {filename!r} to server storage: {result['error']}"
        return f"Transferred {source_path!r} from {source} to server storage as {filename!r} ({result['size']} bytes)."
    config = local_agent_configs.get(destination)
    if config is None:
        available = ", ".join(list(local_agent_configs) + [SERVER_STORAGE])
        return f"Transfer error: unknown destination {destination!r}. Available: {available}."
    write_result = _fetch_local_json(config, "write_file", path=destination_path, content=content)
    if not write_result.get("success"):
        return f"Transfer error: couldn't write {destination_path!r} to {destination}: {write_result.get('stderr') or 'unknown error'}"
    return f"Transferred {source_path!r} from {source} to {destination_path!r} on {destination}."


def describe_local_command(action, args):
    """A single source of truth for how a local command call is displayed,
    live and in history — used by both the tool-call status label and
    show_local_agent_calls() below."""
    path = args.get("path")
    if action in GIT_ACTIONS:
        return f"$ git {action}"
    if action in ("cp", "mv"):
        return f"$ {action} {path or ''} {args.get('destination', '')}".rstrip()
    if action in ("grep", "find") and args.get("pattern"):
        base = f"$ {action} '{args['pattern']}'"
        return f"{base} {path}" if path else base
    if path:
        return f"$ {action} {path}"
    return f"$ {action}"


def show_web_search(searches, sources):
    """Render a demo-friendly summary of a web search tool call: the
    query(ies) used and the deduplicated list of cited sources."""
    if not searches:
        return
    with st.expander(f"🔍 Searched the web: {', '.join(searches)}"):
        seen = {}
        for title, url in sources:
            seen.setdefault(url, title)
        for url, title in seen.items():
            st.markdown(f"- [{title}]({url})")


def show_code_interpreter(code_blocks):
    """Render a demo-friendly summary of code interpreter tool calls: the
    Python code that was actually executed."""
    if not code_blocks:
        return
    label = f"🧮 Ran code ({len(code_blocks)} block{'s' if len(code_blocks) != 1 else ''})"
    with st.expander(label):
        for code in code_blocks:
            st.code(code, language="python")


def show_local_agent_calls(calls):
    """Render a demo-friendly summary of local agent tool calls."""
    if not calls:
        return
    label = f"🔧 Ran {len(calls)} {NAME} command{'s' if len(calls) != 1 else ''}"
    with st.expander(label):
        for entry in calls:
            prefix = describe_local_command(entry["action"], entry.get("args", {}))
            st.code(f"{prefix}\n{entry['output']}", language="text")


def show_transfer_calls(calls):
    """Render a demo-friendly summary of file transfers."""
    if not calls:
        return
    label = f"📤 {len(calls)} file transfer{'s' if len(calls) != 1 else ''}"
    with st.expander(label):
        for entry in calls:
            args = entry.get("args", {})
            prefix = f"$ transfer {args.get('source_path')} ({args.get('source')} -> {args.get('destination')})"
            st.code(f"{prefix}\n{entry['output']}", language="text")


def _render_message(message):
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message.get("image"):
            st.image(base64.b64decode(message["image"]))
        show_web_search(message.get("searches"), message.get("sources", []))
        show_code_interpreter(message.get("code_blocks", []))
        show_local_agent_calls(message.get("local_agent_calls", []))
        show_transfer_calls(message.get("transfer_calls", []))


# One-shot: True only on the render right after the welcome pair is first
# created (see above) -- popped immediately so it can never re-trigger on
# a later rerun.
delay_second_message = st.session_state.pop("_delay_second_welcome_message", False)
for i, message in enumerate(st.session_state.messages):
    if delay_second_message and i == 1:
        # Rendered normally (nothing here blocks/sleeps -- chat_input and
        # everything else below is fully interactive immediately), just
        # started hidden and revealed a couple seconds later by a plain
        # client-side timer -- a real delay with zero server-side cost.
        with st.container(key="delayed_welcome_message"):
            _render_message(message)
        st.html("<style>.st-key-delayed_welcome_message { display: none; }</style>")
        st.iframe(
            """
            <script>
            setTimeout(function() {
                try {
                    var el = window.parent.document.querySelector('.st-key-delayed_welcome_message');
                    if (el) { el.style.display = ''; }
                } catch (e) {}
            }, 2500);
            </script>
            """,
            height=1,
        )
    else:
        _render_message(message)

if prompt := st.chat_input("Chat"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    def capture_response_meta(stream, meta):
        """Captures the response id (for chaining), plus tool activity, from
        the stream as a side effect, since st.write_stream fully consumes it."""
        for event in stream:
            if event.type == "response.completed":
                meta["id"] = event.response.id
                for item in event.response.output:
                    if item.type == "image_generation_call" and item.result:
                        meta["image"] = item.result
                    elif item.type == "web_search_call" and item.action:
                        meta["searches"].append(item.action.query)
                    elif item.type == "code_interpreter_call" and item.code:
                        meta["code_blocks"].append(item.code)
                    elif item.type == "function_call":
                        meta["function_calls"].append(
                            {
                                "call_id": item.call_id,
                                "name": item.name,
                                "arguments": item.arguments,
                            }
                        )
                    elif item.type == "message":
                        for content in item.content:
                            for annotation in (
                                getattr(content, "annotations", None) or []
                            ):
                                if annotation.type == "url_citation":
                                    meta["sources"].append(
                                        (annotation.title, annotation.url)
                                    )
            yield event

    # Accumulates results across hops of the tool-calling loop below: the
    # model can request a tool call, get its output fed back, and decide to
    # call more before giving a final answer.
    aggregate = {
        "searches": [],
        "sources": [],
        "code_blocks": [],
        "local_agent_calls": [],
        "transfer_calls": [],
        "image": None,
    }
    turn_input = [{"role": "user", "content": prompt}]
    full_response = ""

    with st.chat_message("assistant"):
        while True:
            response_meta = {
                "searches": [],
                "sources": [],
                "code_blocks": [],
                "function_calls": [],
            }
            with st.spinner("Thinking..."):
                stream = client.responses.create(
                    model="gpt-4.1-mini",
                    input=turn_input,
                    previous_response_id=st.session_state.previous_response_id,
                    tools=active_tools,
                    stream=True,
                )
                hop_text = st.write_stream(capture_response_meta(stream, response_meta))

            full_response += hop_text
            st.session_state.previous_response_id = response_meta["id"]
            aggregate["searches"].extend(response_meta["searches"])
            aggregate["sources"].extend(response_meta["sources"])
            aggregate["code_blocks"].extend(response_meta["code_blocks"])
            if "image" in response_meta:
                aggregate["image"] = response_meta["image"]

            if not response_meta["function_calls"]:
                break

            # Dispatch each requested tool call and feed the output back in
            # as the next hop's input.
            turn_input = []
            for call in response_meta["function_calls"]:
                args = json.loads(call["arguments"])
                if call["name"] == "run_local_command":
                    action = args.get("action", "")
                    label = f"🔧 {describe_local_command(action, args)}"
                    with st.status(label):
                        output = call_local_agent(
                            local_agent_configs,
                            action,
                            host=args.get("host"),
                            default_host=selected_host_label,
                            path=args.get("path"),
                            destination=args.get("destination"),
                            pattern=args.get("pattern"),
                            lines=args.get("lines", 10),
                            limit=args.get("limit", 5),
                        )
                        st.code(output, language="text")
                    aggregate["local_agent_calls"].append(
                        {"action": action, "args": args, "output": output}
                    )
                elif call["name"] == "transfer_file":
                    label = (
                        f"📤 Transfer {args.get('source_path')} "
                        f"({args.get('source')} → {args.get('destination')})"
                    )
                    with st.status(label):
                        output = call_transfer_file(
                            local_agent_configs,
                            args.get("source"),
                            args.get("source_path"),
                            args.get("destination"),
                            args.get("destination_path"),
                        )
                        st.code(output, language="text")
                    aggregate["transfer_calls"].append({"args": args, "output": output})
                else:
                    output = f"Unknown tool: {call['name']}"
                turn_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": output,
                    }
                )

        if aggregate["image"]:
            st.image(base64.b64decode(aggregate["image"]))
        show_web_search(aggregate["searches"], aggregate["sources"])
        show_code_interpreter(aggregate["code_blocks"])
        show_local_agent_calls(aggregate["local_agent_calls"])
        show_transfer_calls(aggregate["transfer_calls"])

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": full_response,
            "image": aggregate["image"],
            "searches": aggregate["searches"],
            "sources": aggregate["sources"],
            "code_blocks": aggregate["code_blocks"],
            "local_agent_calls": aggregate["local_agent_calls"],
            "transfer_calls": aggregate["transfer_calls"],
        }
    )
