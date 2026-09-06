import base64
import json
from datetime import datetime

import requests
import streamlit as st
from openai import OpenAI

from utils.auth import require_agent_session, revoke_token_with_auth_service
from utils.branding import NAME, TAGLINE, ghost_svg
from utils.browser_nav import click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- keeps the tab title
# searchable by casper_tool.py's bring-tab-into-view AppleScript.
st.set_page_config(page_title="Casper", page_icon="👻")


@st.cache_resource
def get_client():
    return OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


if st.session_state.get("_signed_out"):
    # A plain, chrome-less goodbye page -- no navigation, no tab-closing
    # attempt of any kind (neither window.close() nor casper_tool.py
    # AppleScript-ing the browser -- both were tried; confirmed directly
    # that window.close() is a silent no-op here, since this tab crosses
    # origins twice over its lifetime -- the deployed app ->
    # casper_tool.py's own localhost listener -> the deployed app again,
    # for the /signin -> /chat handoff -- and AppleScript-closing it back
    # in casper_tool.py needed Automation permission and still wasn't
    # reliable). Simplest fix: just ask the user to close it themselves,
    # with Streamlit's own sidebar/header/menu hidden (real, confirmed
    # data-testid selectors from the installed Streamlit build, not
    # guessed) so this doesn't look like a broken app still sitting there.
    st.markdown(
        """
        <style>
        [data-testid="stHeader"], [data-testid="stSidebar"],
        [data-testid="stExpandSidebarButton"], [data-testid="stToolbar"],
        [data-testid="stMainMenu"], [data-testid="stStatusWidget"] {
            display: none;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.write("Session finished! You may close this tab.")
    st.stop()

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
            try:
                requests.post(
                    f"{agent_config['url']}/api/shutdown",
                    headers={"X-API-Key": agent_config["api_key"]},
                    timeout=5,
                )
            except requests.RequestException:
                pass  # best-effort -- the local process may already be gone
            revoke_token_with_auth_service(st.secrets["AUTH_SERVICE_DOMAIN"], agent_config["api_key"])
    st.session_state.clear()
    st.query_params.clear()
    st.session_state["_signed_out"] = True
    st.rerun()

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

# Read once from the query params and cache in session_state -- the URL
# gets its query string stripped (below) right after this first read, so a
# later rerun can't rely on st.query_params still having these.
if "_local_agent_config" not in st.session_state:
    local_agent_url = st.query_params.get("local_agent_url", "")
    local_agent_token = st.query_params.get("local_agent_token", "")
    local_agent_port = st.query_params.get("local_agent_port", "")
    local_agent_workspace = st.query_params.get("local_agent_workspace", "")
    st.session_state["_local_agent_config"] = (
        {
            "url": local_agent_url.rstrip("/"),
            "api_key": local_agent_token,
            "port": local_agent_port,
            "workspace": local_agent_workspace,
        }
        if local_agent_url and local_agent_token
        else None
    )
local_agent_config = st.session_state["_local_agent_config"]

# Background reconnect: if Casper gets fully quit and relaunched while this
# tab is still open, its tunnel dies with it, and a new one opens for the
# fresh run -- rather than leaving this tab dead (and a second tab getting
# opened for that new run), poll Casper directly on localhost (same
# machine, not through the tunnel) for its current tunnel URL, and if it's
# different from this tab's, navigate this same tab to it. Requires the
# port Casper's own server is bound to, which older links (from before this
# feature) won't carry -- skip silently if so.
if local_agent_config and local_agent_config.get("port"):
    reconnect_nav_js = click_anchor_js(
        'window.parent.location.pathname + "?" + params.toString()'
    )
    st.iframe(
        f"""<script>
(function() {{
    var port = {json.dumps(local_agent_config["port"])};
    var token = {json.dumps(local_agent_config["api_key"])};
    var currentUrl = {json.dumps(local_agent_config["url"])};
    function poll() {{
        fetch("http://localhost:" + port + "/api/session-info", {{headers: {{"X-API-Key": token}}}})
            .then(function(r) {{ return r.ok ? r.json() : null; }})
            .then(function(data) {{
                if (data && data.tunnel_url && data.tunnel_url !== currentUrl) {{
                    var params = new URLSearchParams();
                    params.set("local_agent_url", data.tunnel_url);
                    params.set("local_agent_token", token);
                    params.set("local_agent_port", port);
                    {reconnect_nav_js}
                }}
            }})
            .catch(function() {{}});
    }}
    setInterval(poll, 5000);
}})();
</script>""",
        height=1,
    )

# Cosmetic: once the query params have been read (above), drop them from
# the visible URL so the address bar just shows .../chat. This only
# rewrites what's displayed (history.replaceState doesn't fire a
# navigation or a popstate event), so it doesn't affect the already-cached
# session_state values above or trigger a rerun. Runs in the *parent* page
# (window.parent), since the script itself executes inside st.iframe's own
# iframe -- see the note on the sign-out modal above for why st.iframe
# instead of st.markdown(unsafe_allow_html=True).
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


with st.sidebar:
    st.button("Sign out", on_click=_start_sign_out)
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
        st.caption(
            f"Not connected. Download {NAME} from the home page and run it "
            "on your own machine — it'll bring you back here already "
            "connected."
        )

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
