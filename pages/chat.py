import base64
import json
import threading
import time
from datetime import datetime, timezone

import requests
import streamlit as st
from openai import OpenAI

from utils.auth import (
    build_pair_url,
    current_token,
    list_environments,
    list_hosts,
    require_agent_session,
    require_app_subdomain,
    revoke_token_with_auth_service,
    signout_all_hosts,
)
from utils.branding import NAME, hide_streamlit_chrome, home_link_html
from utils.browser_nav import click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- a consistent tab
# identity across the whole flow.
st.set_page_config(page_title="Casper", page_icon="👻")
require_app_subdomain()


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
    with st.spinner("Signing out..."):
        # signout_all_hosts (best-effort /api/shutdown fanned out to every
        # attached daemon, then detach them all) and revoking this browser's
        # own login token are independent auth_service state now -- host
        # attachment vs. users.token_hash, see the credential-split note in
        # auth_service/main.py -- so run them in parallel rather than
        # sequentially, same reasoning the old single-host version had for
        # overlapping its own shutdown/revoke calls.
        def _signout_all_hosts():
            signout_all_hosts(st.secrets["AUTH_SERVICE_DOMAIN"], current_token())

        hosts_thread = threading.Thread(target=_signout_all_hosts)
        hosts_thread.start()
        revoke_token_with_auth_service(st.secrets["AUTH_SERVICE_DOMAIN"], current_token())
        hosts_thread.join(timeout=10)
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

# Chrome hidden on every page (see utils/branding.py), but the home link
# itself lives in the sidebar here instead of the main content area --
# unlike every other page, chat.py's sidebar is already real, persistent
# UI (sign-out, connection status), so the link belongs there rather than
# floating above the conversation.
hide_streamlit_chrome()

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

# Looked up once (cached in session_state) rather than carried via query
# params -- the daemon no longer redirects a browser tab itself (see
# pages/signin.py), so it can't hand local_agent_url/workspace along that
# way anymore. Instead each attached daemon reports its own reachability to
# the auth service on pairing/toggle (see agent/internal/config/presence.go),
# and this looks the whole set up by the same token require_agent_session()
# already verified.
if "_hosts" not in st.session_state:
    # Retried briefly rather than checked once: landing here right after
    # sign-in (the common case) races the casper://pair hand-off, which
    # typically finishes a beat after this page has already loaded --
    # confirmed directly (a real click-through showed "Not connected" on
    # first load, then the daemon's pairing log line appeared a moment
    # later). Same reasoning as the old pairing spinner's poll loop: a
    # single check is too eager, but this shouldn't retry forever either,
    # so it gives up after a few seconds, leaving whatever's connected by
    # then (possibly nothing).
    hosts_result = None
    for attempt in range(6):
        hosts_result = list_hosts(st.secrets["AUTH_SERVICE_DOMAIN"], current_token())
        if hosts_result and any(h.get("connected") for h in hosts_result.get("hosts", [])):
            break
        if attempt < 5:
            time.sleep(0.5)
    st.session_state["_hosts"] = (hosts_result or {}).get("hosts", [])
    st.session_state["_environments"] = (
        list_environments(st.secrets["AUTH_SERVICE_DOMAIN"], current_token()) or {}
    ).get("environments", [])
hosts = st.session_state["_hosts"]
environments = st.session_state["_environments"]

if "_active_environment_id" not in st.session_state:
    st.session_state["_active_environment_id"] = environments[0]["id"] if environments else None
active_environment = next(
    (e for e in environments if e["id"] == st.session_state["_active_environment_id"]), None
)
active_host_ids = set(active_environment["host_ids"]) if active_environment else set()
hosts_by_id = {h["host_id"]: h for h in hosts}
connected_active_hosts = [
    h for h in hosts if h["host_id"] in active_host_ids and h.get("connected") and h.get("local_agent_url")
]


def _build_local_agent_configs(connected_hosts):
    """Keys each connected host by a de-duplicated label -- its own label in
    the common case, with its host_id appended only when two hosts in the
    active Environment happen to share a label."""
    label_counts = {}
    for h in connected_hosts:
        label_counts[h["label"]] = label_counts.get(h["label"], 0) + 1
    configs = {}
    for h in connected_hosts:
        key = h["label"] if label_counts[h["label"]] == 1 else f"{h['label']} ({h['host_id']})"
        configs[key] = {
            "host_id": h["host_id"],
            "url": h["local_agent_url"].rstrip("/"),
            "api_key": h["command_key"],
            "workspace": h.get("workspace", ""),
        }
    return configs


local_agent_configs = _build_local_agent_configs(connected_active_hosts)

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


def _epoch_millis(sqlite_timestamp):
    """Parses auth_service's CURRENT_TIMESTAMP format ("YYYY-MM-DD
    HH:MM:SS", implicitly UTC) into epoch milliseconds, comparable against
    a browser's own Date.now(). None for anything that doesn't parse (a
    missing value, or a server-side format change)."""
    if not sqlite_timestamp:
        return None
    try:
        dt = datetime.strptime(sqlite_timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _find_local_host_id(hosts, local_paired_at):
    """Correlates this browser's own casper_last_paired_at stamp (set by
    pages/signin.py the moment it fires a casper://pair dispatch) against
    each known host's first_paired_at, treating whichever host got paired
    shortly *after* that stamp as this browser's own machine. A fuzzy,
    one-time-historical match rather than a live check -- there's no way
    for the browser to directly confirm which physical machine actually
    received a given pairing dispatch, since a background fetch to the
    local daemon is blocked outright by browsers' mixed-content policy
    (confirmed the hard way: an https page can never fetch a plain
    http:// resource, even localhost, regardless of CORS/Private Network
    Access headers). But since both timestamps are fixed once set, this
    stays correct on every future visit, not just right after pairing.
    -5s of slack covers the browser's clock running slightly ahead of
    auth_service's; 120s covers how long pairing can realistically take
    (Gatekeeper prompts, etc.) before it stops being a plausible match."""
    if local_paired_at is None:
        return None
    best_id, best_gap = None, None
    for h in hosts:
        paired_at = _epoch_millis(h.get("first_paired_at"))
        if paired_at is None:
            continue
        gap = paired_at - local_paired_at
        if -5_000 <= gap <= 120_000 and (best_gap is None or gap < best_gap):
            best_id, best_gap = h["host_id"], gap
    return best_id


# Reads back this browser's own casper_last_paired_at localStorage stamp
# (see pages/signin.py and the Reconnect link below for where it's set) --
# pure same-origin storage access, no mixed-content issue at all, unlike
# an earlier version of this that tried to have the browser directly probe
# the local daemon. One-shot per browser session, same reload-carries-a-
# query-param pattern used throughout this file for JS-to-Python hand-offs.
if "local_paired_at" in st.query_params:
    st.session_state["_local_paired_at"] = st.query_params["local_paired_at"]
    st.iframe(
        "<script>window.parent.history.replaceState(null, '', window.parent.location.pathname);</script>",
        height=1,
    )
elif "_local_paired_at_read" not in st.session_state:
    st.session_state["_local_paired_at_read"] = True
    st.iframe(
        """
        <script>
        (function() {
            var stamp = window.parent.localStorage.getItem('casper_last_paired_at');
            if (stamp) {
                var url = new URL(window.parent.location.href);
                url.searchParams.set('local_paired_at', stamp);
                window.parent.location.href = url.toString();
            }
        })();
        </script>
        """,
        height=1,
    )
try:
    local_paired_at = int(st.session_state.get("_local_paired_at") or "")
except ValueError:
    local_paired_at = None
local_host_id = _find_local_host_id(hosts, local_paired_at)
local_host = next(
    (label for label, cfg in local_agent_configs.items() if cfg.get("host_id") == local_host_id), None
)

# A second, defensive attempt at bringing this tab into focus on its very
# first load after sign-in (see pages/signin.py's own focus() call, right
# before it redirects here, for the first attempt and why this exists at
# all -- a reported case of the tab not ending up focused after sign-in).
# Only once per session, same reasoning as _url_cleaned above.
if "_focus_attempted" not in st.session_state:
    st.iframe("<script>window.parent.focus();</script>", height=1)
    st.session_state["_focus_attempted"] = True

def _start_sign_out():
    # on_click (not "if st.button(...):") so this runs as part of Streamlit's
    # own click-handling, before the rerun it then triggers automatically --
    # more robust than checking the button's return value inline, which a
    # couple of reported cases suggest can occasionally miss a click's first
    # rerun. No st.rerun() needed/wanted here; Streamlit already reruns once
    # after any on_click callback returns.
    st.session_state["_signing_out"] = True


def _recheck_hosts():
    # Drops the cached (possibly stale) host/Environment state so the retry
    # loop above runs again on the rerun this triggers -- see the "Check
    # again" button below. Also re-arms the local-host probe (otherwise a
    # one-shot per browser session -- see its own comment above) so a
    # daemon that came up, or got fixed/redeployed, after this tab's first
    # (failed) attempt gets a fresh try instead of being stuck forever.
    st.session_state.pop("_hosts", None)
    st.session_state.pop("_environments", None)
    st.session_state.pop("_active_environment_id", None)
    st.session_state.pop("_local_paired_at_read", None)
    st.session_state.pop("_local_paired_at", None)


def _switch_environment():
    st.session_state["_active_environment_id"] = st.session_state["_environment_selector"]


with st.sidebar:
    st.markdown(home_link_html(size=32), unsafe_allow_html=True)
    st.button("Sign out", key="sign_out_button", on_click=_start_sign_out)
    st.caption(f"Signed in as {username}")

    st.divider()
    if environments:
        env_ids = [e["id"] for e in environments]
        st.selectbox(
            "Environment",
            options=env_ids,
            format_func=lambda eid: next(e["name"] for e in environments if e["id"] == eid),
            index=env_ids.index(st.session_state["_active_environment_id"])
            if st.session_state["_active_environment_id"] in env_ids
            else 0,
            key="_environment_selector",
            on_change=_switch_environment,
        )
        if active_environment:
            for host_id in active_environment["host_ids"]:
                host = hosts_by_id.get(host_id)
                if host is None:
                    continue
                if host.get("connected"):
                    icon, status = "🔧", "active"
                else:
                    icon, status = "⚪", "inactive"
                # Marks whichever host this specific browser tab was
                # correlated to this machine (see _find_local_host_id
                # above) -- matched by host_id, not label, since two hosts
                # could share a label. A plain word rather than an emoji/
                # icon -- less likely to go unnoticed or fail to render
                # depending on the system's emoji font.
                marker = ", this machine" if host["host_id"] == local_host_id else ""
                st.caption(f"{icon} {host['label']} ({status}{marker})")
            if not active_environment["host_ids"]:
                st.caption("No hosts in this Environment yet.")
    else:
        st.caption("No Environments yet.")
    st.page_link("pages/environments.py", label="Manage hosts & Environments")

    st.divider()
    st.subheader("Local commands")
    for category, commands in COMMAND_CATEGORIES.items():
        with st.expander(category):
            st.markdown("\n".join(f"- `{cmd}`" for cmd in commands))
    st.caption(f"Confined to the directory tree {NAME} runs in, on each machine.")
    if local_agent_configs:
        st.caption(f"🔧 {NAME} connected and ready to run.")
    else:
        st.caption(f"No connected machines in this Environment. Download {NAME} from the home page to add one.")
        # "Reconnect" re-fires the casper://pair hand-off (for when it
        # truly never reached this machine's daemon -- wasn't running yet,
        # missed the event), confirmed via a real click-through test in the
        # original single-host version. onclick (not window.parent.-
        # prefixed -- this markdown is already rendered directly in the
        # top-level page, not nested in an st.iframe) stamps the same
        # casper_last_paired_at localStorage marker pages/signin.py sets,
        # so a reconnect-driven re-pairing gets local-host correlation too.
        reconnect_url = build_pair_url(current_token(), username)
        st.markdown(
            f'Running {NAME} on this machine? <a href="{reconnect_url}" '
            f"onclick=\"localStorage.setItem('casper_last_paired_at', Date.now())\">Reconnect</a>",
            unsafe_allow_html=True,
        )
    if not local_host:
        # Covers two distinct cases with one button: no connected machines
        # yet (re-runs the host lookup above, for when pairing *did*
        # succeed only moments after this page's one-shot check already
        # gave up), and connected-but-not-detected-as-local (re-arms the
        # local-host probe, for when this tab's own daemon came up, or got
        # fixed/redeployed, after this tab's first attempt already failed
        # and gave up -- see _recheck_hosts's own comment).
        st.button("Check again", on_click=_recheck_hosts)

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
            "host": {
                "type": "string",
                "enum": list(local_agent_configs.keys()),
                "description": (
                    "Which connected machine to run this on. Usually fine "
                    "to omit -- if the user is talking about a specific "
                    "one ('on the mini', 'my laptop'), name it explicitly, "
                    "but otherwise it defaults to whichever machine the "
                    "user is currently chatting from (or the only "
                    "connected one). Ask the user to clarify only if the "
                    "system reports it's still ambiguous."
                ),
            },
        },
        "required": ["action"],
    },
}
active_tools = TOOLS + ([LOCAL_AGENT_TOOL] if local_agent_configs else [])

if "messages" not in st.session_state:
    st.session_state.messages = []
    # Only worth a proactive welcome message when there's exactly one
    # connected machine to name -- with several, "your workspace is X"
    # would just be misleading about which one.
    if len(local_agent_configs) == 1:
        (only_config,) = local_agent_configs.values()
        if only_config.get("workspace"):
            st.session_state.messages.append(
                {"role": "assistant", "content": f"Your workspace is {only_config['workspace']}"}
            )

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
    here instead. default_host is the machine this browser tab was itself
    detected running on (see the local-daemon probe above) -- preferred
    over asking the model to guess or forcing the user to clarify, since
    "run this" with no machine named almost always means "here"."""
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
                            local_agent_configs,
                            action,
                            host=args.get("host"),
                            default_host=local_host,
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
