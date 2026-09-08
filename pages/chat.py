import base64
import json
import threading
import time
from datetime import datetime

import requests
import streamlit as st
from openai import OpenAI

from utils.auth import (
    build_pair_url,
    current_token,
    get_presence,
    require_agent_session,
    revoke_token_with_auth_service,
)
from utils.branding import NAME, TAGLINE, ghost_svg
from utils.browser_nav import click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- a consistent tab
# identity across the whole flow.
st.set_page_config(page_title="Casper", page_icon="👻")


@st.cache_resource
def get_client():
    return OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


if st.session_state.get("_signing_out"):
    # Split from the button click itself (below) on purpose: these are
    # slow, blocking network calls (up to several seconds), and running
    # them directly in the button's own script run left the click looking
    # like it hadn't registered -- if the user clicked again before this
    # finished, Streamlit cancels the still-running script for the newer
    # one, aborting these calls mid-flight. Setting a flag and rerunning
    # first means the click itself is answered instantly, and this
    # (visibly, via the spinner) runs on its own dedicated rerun instead.
    agent_config = st.session_state.get("_local_agent_config")
    with st.spinner("Signing out..."):
        if agent_config:
            # Was sequential (up to 5s + 5s) -- run the shutdown call on a
            # background thread so it overlaps with the revoke call instead
            # of adding to it, since both are independent, best-effort, and
            # already individually timeout-bounded. Unlike the old one-shot
            # agent, /api/shutdown no longer ends the daemon's process -- it
            # just clears its session and leaves it running, idle, waiting
            # to be paired again.
            def _shutdown_local_agent():
                try:
                    requests.post(
                        f"{agent_config['url']}/api/shutdown",
                        headers={"X-API-Key": agent_config["api_key"]},
                        timeout=5,
                    )
                except requests.RequestException:
                    pass  # best-effort -- the local daemon may be unreachable

            shutdown_thread = threading.Thread(target=_shutdown_local_agent)
            shutdown_thread.start()
            revoke_token_with_auth_service(st.secrets["AUTH_SERVICE_DOMAIN"], agent_config["api_key"])
            shutdown_thread.join(timeout=5)
    st.session_state.clear()
    st.query_params.clear()
    # Signing out no longer means this tab's job is done (the daemon behind
    # it isn't dying) -- send the browser back to the login page instead of
    # a dead-end "you may close this tab" page, so starting a new session is
    # just clicking "Sign in" again.
    st.success("Signed out.")
    st.iframe(f"<script>{click_anchor_js(json.dumps('/signin'))}</script>", height=1)
    st.stop()

username = require_agent_session()
client = get_client()

col1, col2 = st.columns([1, 8])
with col1:
    st.markdown(ghost_svg(48), unsafe_allow_html=True)
with col2:
    st.subheader(NAME)
    st.caption(TAGLINE)

# Mirrors casper_tool.py's COMMAND_CATEGORIES — the two run as separate
# processes on separate machines, so this list is duplicated rather than
# imported. Keep them in sync by hand.
COMMAND_CATEGORIES = {
    "Git": ["status", "branch", "log"],
    "Navigation": ["pwd", "cd", "ls", "tree"],
    "Management": ["mkdir", "touch", "cp", "mv", "rm", "rmdir"],
    "Viewing & Searching": ["cat", "less", "head", "tail", "grep", "find"],
}
GIT_ACTIONS = set(COMMAND_CATEGORIES["Git"])

# One entry per request: each script rerun (page load, widget change, chat
# message) is a fresh request from the browser. Streamlit reports None for
# localhost connections specifically (see st.context.ip_address docs) —
# show that plainly rather than the literal string "None".
if "ip_log" not in st.session_state:
    st.session_state.ip_log = []
ip = st.context.ip_address or "localhost"
st.session_state.ip_log.append(f"{datetime.now().strftime('%H:%M:%S')}  {ip}")
st.session_state.ip_log = st.session_state.ip_log[-100:]  # cap growth

# Looked up once (cached in session_state) rather than carried via query
# params -- the daemon no longer redirects a browser tab itself (see
# pages/signin.py), so it can't hand local_agent_url/workspace along that
# way anymore. Instead it reports them to the auth service on pairing/
# toggle (see agent/internal/config/presence.go), and this looks that up by
# the same token require_agent_session() already verified.
if "_local_agent_config" not in st.session_state:
    # Retried briefly rather than checked once: landing here right after
    # sign-in (the common case) races the casper://pair hand-off, which
    # typically finishes a beat after this page has already loaded --
    # confirmed directly (a real click-through showed "Not connected" on
    # first load, then the daemon's pairing log line appeared a moment
    # later). Same reasoning as the old pairing spinner's poll loop: a
    # single check is too eager, but this shouldn't retry forever either,
    # so it gives up (leaving local_agent_config None) after a few seconds.
    presence = None
    for attempt in range(6):
        presence = get_presence(st.secrets["AUTH_SERVICE_DOMAIN"], current_token())
        if presence and presence.get("connected") and presence.get("local_agent_url"):
            break
        if attempt < 5:
            time.sleep(0.5)
    st.session_state["_local_agent_config"] = (
        {
            "url": presence["local_agent_url"].rstrip("/"),
            "api_key": current_token(),
            "workspace": presence.get("workspace", ""),
        }
        if presence and presence.get("connected") and presence.get("local_agent_url")
        else None
    )
local_agent_config = st.session_state["_local_agent_config"]

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

def _start_sign_out():
    # on_click (not "if st.button(...):") so this runs as part of Streamlit's
    # own click-handling, before the rerun it then triggers automatically --
    # more robust than checking the button's return value inline, which a
    # couple of reported cases suggest can occasionally miss a click's first
    # rerun. No st.rerun() needed/wanted here; Streamlit already reruns once
    # after any on_click callback returns.
    st.session_state["_signing_out"] = True


def _recheck_local_agent_config():
    # Drops the cached (possibly stale) connection state so the retry loop
    # above runs again on the rerun this triggers -- see the "Check again"
    # button below.
    st.session_state.pop("_local_agent_config", None)


with st.sidebar:
    st.button("Sign out", key="sign_out_button", on_click=_start_sign_out)
    st.caption(f"Signed in as {username}")

    st.divider()
    st.subheader("Request IP log")
    st.text_area(
        "Request IP log",
        value="\n".join(reversed(st.session_state.ip_log)),
        height=150,
        disabled=True,
        label_visibility="collapsed",
    )

    st.divider()
    st.subheader("Local commands")
    for category, commands in COMMAND_CATEGORIES.items():
        with st.expander(category):
            st.markdown("\n".join(f"- `{cmd}`" for cmd in commands))
    st.caption(f"Confined to the directory tree {NAME} runs in.")
    if local_agent_config:
        st.caption(f"🔧 {NAME} connected and ready to run.")
    else:
        st.caption(f"Not connected. Download {NAME} from the home page and run it on your own machine.")
        # Two distinct manual fallbacks for two distinct failure modes, both
        # confirmed via a real click-through test: "Reconnect" re-fires the
        # casper://pair hand-off (for when it truly never reached the
        # daemon -- wasn't running yet, missed the event); "Check again"
        # just re-runs the presence lookup above (for when pairing *did*
        # succeed, only moments after this page's one-shot check already
        # gave up and cached "not connected" -- the retry loop above covers
        # the common case, but a slow click through Chrome's "Open
        # Casper?" prompt can still outlast it).
        reconnect_url = build_pair_url(current_token(), username)
        st.markdown(f'Already running {NAME}? <a href="{reconnect_url}">Reconnect</a>', unsafe_allow_html=True)
        st.button("Check again", on_click=_recheck_local_agent_config)

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
        "of commands, confined to the directory tree the agent server runs "
        "in (it can't read, write, or navigate outside that tree). "
        "Categories: Git (status/branch/log), Navigation (pwd/cd/ls/tree), "
        "Management (mkdir/touch/cp/mv/rm/rmdir — 'rm' only deletes a file "
        "and 'rmdir' only an already-empty directory, never recursively), "
        "and Viewing & Searching (cat/less/head/tail/grep/find)."
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
        },
        "required": ["action"],
    },
}
active_tools = TOOLS + ([LOCAL_AGENT_TOOL] if local_agent_config else [])

if "messages" not in st.session_state:
    st.session_state.messages = []
    if local_agent_config and local_agent_config.get("workspace"):
        st.session_state.messages.append(
            {"role": "assistant", "content": f"Your workspace is {local_agent_config['workspace']}"}
        )

# The Responses API tracks conversation history server-side, keyed off the
# previous turn's response id.
if "previous_response_id" not in st.session_state:
    st.session_state.previous_response_id = None


def call_local_agent(local_agent_config, action, **kwargs):
    """Call the user's local agent server; never raises, so a connection
    failure just gets reported back to the model as text."""
    try:
        response = requests.post(
            f"{local_agent_config['url']}/api/command",
            json={"action": action, **kwargs},
            headers={"X-API-Key": local_agent_config["api_key"]},
            timeout=15,
        )
        if response.status_code == 401:
            return "Local agent error: invalid API key."
        response.raise_for_status()
        return json.dumps(response.json())
    except requests.RequestException as e:
        return f"Local agent error: {e}"


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


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message.get("image"):
            st.image(base64.b64decode(message["image"]))
        show_web_search(message.get("searches"), message.get("sources", []))
        show_code_interpreter(message.get("code_blocks", []))
        show_local_agent_calls(message.get("local_agent_calls", []))

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
                            local_agent_config,
                            action,
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

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": full_response,
            "image": aggregate["image"],
            "searches": aggregate["searches"],
            "sources": aggregate["sources"],
            "code_blocks": aggregate["code_blocks"],
            "local_agent_calls": aggregate["local_agent_calls"],
        }
    )
