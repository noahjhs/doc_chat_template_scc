import base64
import json
import threading
import time

import requests
import streamlit as st
from openai import OpenAI

from utils.auth import (
    build_pair_url,
    current_token,
    download_storage,
    list_environments,
    list_hosts,
    require_agent_session,
    require_app_subdomain,
    revoke_token_with_auth_service,
    signout_all_hosts,
    upload_storage,
)
from utils.branding import NAME, hide_streamlit_chrome, home_link_html
from utils.browser_nav import click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- a consistent tab
# identity across the whole flow.
st.set_page_config(page_title="Casper - Chat", page_icon="👻", initial_sidebar_state="expanded")
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
    # Also clears the token utils/auth.py's require_agent_session() stashes
    # in localStorage for refresh recovery -- otherwise a signed-out
    # browser could still get silently re-authenticated by that recovery
    # path on its next visit (harmless in practice, since a revoked token
    # fails verification anyway, but there's no reason to leave it there).
    st.iframe(
        f"<script>window.parent.localStorage.removeItem('casper_auth_token');{click_anchor_js(json.dumps('/signin'))}</script>",
        height=1,
    )
    st.stop()

username = require_agent_session()
client = get_client()

# Chrome hidden on every page (see utils/branding.py), but the home link
# itself lives in the sidebar here instead of the main content area --
# unlike every other page, chat.py's sidebar is already real, persistent
# UI (sign-out, connection status), so the link belongs there rather than
# floating above the conversation.
hide_streamlit_chrome()

# hide_streamlit_chrome() fully hides Streamlit's own header bar
# ([data-testid="stHeader"], display: none) on every page -- but the
# control that reopens the sidebar once it's been collapsed lives inside
# that same header (it has to: it's outside the sidebar itself, since a
# collapsed sidebar can't hold the only way to un-collapse it). Hiding the
# whole header therefore leaves no way back once you collapse the sidebar
# here -- re-shown page-locally (not in branding.py's shared function),
# since only this page actually has sidebar content worth reopening; every
# other page keeps the header fully hidden. Layered after
# hide_streamlit_chrome()'s own <style> tag, so this wins the cascade for
# equally-specific, both-!important rules on the same selector. The
# menu/toolbar (the parts of the header actually worth hiding) stay hidden
# regardless.
st.html(
    """
    <style>
    [data-testid="stHeader"] {
        display: block !important;
        background: transparent !important;
        height: auto !important;
        min-height: 0 !important;
    }
    [data-testid="stMainMenu"], [data-testid="stToolbar"] {
        display: none !important;
    }
    </style>
    """
)

# The Environment-management gear and the workspace refresh icon (both
# below) are meant to read as small, secondary glyphs next to their
# labels, not full buttons -- strips Streamlit's default bordered-button
# chrome (border, background, shadow) down to just the icon, in every
# interaction state (hover/focus/active too, so nothing picks up the
# theme's accent color on interaction -- "monochromatic" per the ask).
# Targeted by key via Streamlit's own .st-key-<key> convention rather than
# a page-wide selector, so no other button on this page is affected.
st.html(
    """
    <style>
    .st-key-env_gear_button button, .st-key-refresh_workspace button {
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
        padding: 0.1rem 0.35rem !important;
        min-height: 0 !important;
        color: inherit !important;
    }
    .st-key-env_gear_button button:hover, .st-key-refresh_workspace button:hover,
    .st-key-env_gear_button button:focus, .st-key-refresh_workspace button:focus,
    .st-key-env_gear_button button:active, .st-key-refresh_workspace button:active {
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
        color: inherit !important;
    }
    </style>
    """
)

# Mirrors casper_tool.py's COMMAND_CATEGORIES — the two run as separate
# processes on separate machines, so this list is duplicated rather than
# imported. Keep them in sync by hand.
COMMAND_CATEGORIES = {
    "Git": ["status", "branch", "log"],
    "Navigation": ["pwd", "cd", "ls", "tree", "list_directories"],
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
            "workspace": h.get("workspace") or [],
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
    # again" button below.
    st.session_state.pop("_hosts", None)
    st.session_state.pop("_environments", None)
    st.session_state.pop("_active_environment_id", None)


def _switch_environment():
    st.session_state["_active_environment_id"] = st.session_state["_environment_selector"]


def _fetch_local_json(config, action, **kwargs):
    """Like call_local_agent below, but returns the parsed response dict
    (or an error dict in the same {success, stdout, stderr} shape
    commands.Result already uses) instead of a model-facing string --
    for UI code that needs to read a result programmatically, namely the
    workspace browser below. Kept separate from call_local_agent rather
    than sharing a helper, since the two need genuinely different return
    shapes for their different callers (model vs. this page's own code)."""
    try:
        response = requests.post(
            f"{config['url']}/api/command",
            json={"action": action, **kwargs},
            headers={"X-API-Key": config["api_key"]},
            timeout=15,
        )
        if response.status_code == 401:
            return {"success": False, "stdout": "", "stderr": "Invalid API key."}
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        return {"success": False, "stdout": "", "stderr": str(e)}


def _dir_label(path):
    """The trailing path component of an absolute directory path (from
    either OS's separator), for a short expander label -- falls back to
    the full path for a root-ish path with no basename (e.g. "/")."""
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name or path


def _parse_tree_output(stdout):
    """Parses the local agent's `tree` action output (2-space indent per
    depth level, directories suffixed with "/", already capped at depth 4
    -- see agent/internal/commands/commands.go's runTree) into a nested
    [{"name", "is_dir", "children"}, ...] structure for rendering as
    nested expanders below."""
    root = []
    stack = [(-1, root)]
    for line in stdout.splitlines():
        if not line.strip():
            continue
        depth = (len(line) - len(line.lstrip(" "))) // 2
        name = line.strip()
        is_dir = name.endswith("/")
        if is_dir:
            name = name[:-1]
        node = {"name": name, "is_dir": is_dir, "children": []}
        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack[-1][1].append(node)
        if is_dir:
            stack.append((depth, node["children"]))
    return root


MAX_TREE_RENDER_DEPTH = 2  # independent of the server's own depth-4 fetch cap -- see below
MAX_TREE_ENTRIES_PER_DIR = 50


def _render_tree(nodes, depth=0):
    """Renders nested expanders for a parsed tree -- capped shallower than
    the server's own fetch depth (commands.go's runTree already limits to
    4), confirmed necessary against a real workspace: Streamlit expanders
    render their contents into the page regardless of collapsed/expanded
    state (there's no built-in lazy loading), so eagerly rendering a full
    depth-4 tree of a large, real directory (e.g. a node_modules/ with 80+
    entries) means hundreds of sidebar widgets even though the underlying
    fetch is a single cached request. Deeper levels just show a count
    instead of recursing further; per-directory entries are similarly
    capped so one huge flat folder can't blow up the sidebar on its own.
    Directories sort before files (a stable sort -- each group keeps the
    daemon's own alphabetical order), matching the usual file-browser
    convention rather than the daemon's plain alphabetical `tree` output;
    the entry cap above applies after this sort, so a folder with more
    than 50 entries shows its subdirectories first before truncating into
    its files."""
    nodes = sorted(nodes, key=lambda n: not n["is_dir"])
    for node in nodes[:MAX_TREE_ENTRIES_PER_DIR]:
        if node["is_dir"]:
            with st.expander(f"📁 {node['name']}"):
                if not node["children"]:
                    st.caption("(empty)")
                elif depth < MAX_TREE_RENDER_DEPTH:
                    _render_tree(node["children"], depth + 1)
                else:
                    st.caption(f"{len(node['children'])} item(s) -- open in a local file browser to go deeper.")
        else:
            st.caption(f"📄 {node['name']}")
    if len(nodes) > MAX_TREE_ENTRIES_PER_DIR:
        st.caption(f"... and {len(nodes) - MAX_TREE_ENTRIES_PER_DIR} more.")


with st.sidebar:
    st.markdown(home_link_html(size=32), unsafe_allow_html=True)
    st.button("Sign out", key="sign_out_button", on_click=_start_sign_out)
    st.caption(f"Signed in as {username}")

    st.divider()
    env_label_col, env_gear_col = st.columns([5, 1])
    with env_label_col:
        st.markdown("**Environment**")
    with env_gear_col:
        st.button(
            "⚙️",
            key="env_gear_button",
            help="Manage hosts & Environments",
            on_click=lambda: st.switch_page("pages/environments.py"),
        )
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
            label_visibility="collapsed",
        )
        env_host_ids = active_environment["host_ids"] if active_environment else []
        if env_host_ids:
            # Click-selectable -- the selection becomes the default target
            # for local commands the model doesn't name a host for (see
            # call_local_agent's default_host below), same idea as the
            # active Environment selector just above it. Re-defaults to
            # the first host whenever the current selection isn't valid
            # for this Environment any more (switched Environments, or
            # never selected yet) -- Streamlit's radio requires its keyed
            # session_state value to already be one of the options before
            # it's instantiated.
            if (
                "_selected_host_id" not in st.session_state
                or st.session_state["_selected_host_id"] not in env_host_ids
            ):
                st.session_state["_selected_host_id"] = env_host_ids[0]

            def _format_host_option(host_id):
                host = hosts_by_id.get(host_id)
                if host is None:
                    return str(host_id)
                icon = "🔧" if host.get("connected") else "⚪"
                return f"{icon} {host['label']}"

            st.radio(
                "Hosts",
                options=env_host_ids,
                format_func=_format_host_option,
                key="_selected_host_id",
                label_visibility="collapsed",
            )
        else:
            st.caption("No hosts in this Environment yet.")
    else:
        st.caption("No Environments yet.")

    selected_host_id = st.session_state.get("_selected_host_id")
    selected_host_label = next(
        (label for label, cfg in local_agent_configs.items() if cfg.get("host_id") == selected_host_id), None
    )
    selected_config = local_agent_configs.get(selected_host_label)
    directories = (selected_config.get("workspace") or []) if selected_config else []

    workspace_label_col, workspace_refresh_col = st.columns([5, 1])
    with workspace_label_col:
        st.subheader("Workspace")
    with workspace_refresh_col:
        # Next to the active host's directory list, not a standalone
        # button at the bottom of it -- same label-plus-icon treatment as
        # the Environment gear above.
        if selected_config is not None and st.button("🔄", key="refresh_workspace", help="Refresh directories"):
            for directory in directories:
                st.session_state.pop(f"_tree_{selected_host_id}_{directory}", None)
            st.session_state["_workspace_refreshing"] = True
            st.rerun()

    if selected_config is None:
        st.caption("Select a connected host above to browse its directories.")
    elif st.session_state.pop("_workspace_refreshing", False):
        # Deliberately renders nothing here for one rerun cycle, then
        # reruns again to show the (freshly re-fetched, since the cache
        # was already cleared above) directory list -- visible "gone,
        # then back" feedback that the click actually did something,
        # since an unchanged directory list would otherwise just silently
        # redraw looking identical to before.
        with st.spinner("Refreshing..."):
            time.sleep(0.4)
        st.rerun()
    else:

        def _start_add_directory():
            # Fast, fire-and-forget -- the daemon opens the native picker
            # in its own background goroutine and returns immediately (see
            # agent/internal/commands's runAddDirectory), rather than
            # blocking on however long the user takes to respond, so doing
            # this inline in the click handler (unlike _start_sign_out's
            # flag-and-rerun pattern) doesn't stall the click.
            result = _fetch_local_json(selected_config, "add_directory")
            if not result.get("success"):
                st.session_state["_add_directory_error"] = result.get("stderr") or "Couldn't open the folder picker."
                st.session_state.pop("_add_directory_pending", None)
            else:
                st.session_state["_add_directory_pending"] = selected_host_id
                st.session_state.pop("_add_directory_error", None)

        st.button("➕ Add directory", on_click=_start_add_directory, key="add_directory_button")

        if st.session_state.get("_add_directory_error"):
            st.error(st.session_state.pop("_add_directory_error"))

        if st.session_state.get("_add_directory_pending") == selected_host_id:
            # A native dialog just opened on the *selected host's* machine
            # -- not necessarily this browser's own. Brief poll (mirrors
            # the hosts retry-loop above) for the fast case where it was
            # answered right away; the realistic case is a human needs a
            # moment to switch apps and pick a folder, so this expects to
            # usually fall through to the manual "Check again" below.
            found = False
            with st.spinner("Waiting for a folder to be chosen on the local machine..."):
                previous_count = len(directories)
                for attempt in range(6):
                    fresh = list_hosts(st.secrets["AUTH_SERVICE_DOMAIN"], current_token())
                    fresh_host = next(
                        (h for h in (fresh or {}).get("hosts", []) if h["host_id"] == selected_host_id), None
                    )
                    if fresh_host and len(fresh_host.get("workspace") or []) > previous_count:
                        found = True
                        break
                    if attempt < 5:
                        time.sleep(1)
            if found:
                st.session_state.pop("_add_directory_pending", None)
                st.session_state.pop("_hosts", None)
                st.rerun()
            else:
                st.caption(
                    "Still waiting -- check the local machine for a folder-picker dialog, "
                    "choose a folder, then click below."
                )
                if st.button("Check again", key="check_add_directory"):
                    st.session_state.pop("_hosts", None)
                    st.rerun()

        if not directories:
            st.caption("No directories added yet on this host.")
        else:
            for directory in directories:
                with st.expander(f"📁 {_dir_label(directory)}", expanded=False):
                    # Cached per (host, directory) rather than re-fetched on
                    # every rerun (every chat message would otherwise
                    # re-walk every directory's tree) -- a single `tree`
                    # call already returns the full depth-4 structure in
                    # one request (see agent/internal/commands/commands.go's
                    # runTree), so this is one fetch per directory per
                    # browser session, refreshed only on demand.
                    cache_key = f"_tree_{selected_host_id}_{directory}"
                    if cache_key not in st.session_state:
                        with st.spinner("Loading..."):
                            st.session_state[cache_key] = _fetch_local_json(selected_config, "tree", path=directory)
                    tree_result = st.session_state[cache_key]
                    if tree_result.get("success"):
                        _render_tree(_parse_tree_output(tree_result.get("stdout", "")))
                    else:
                        st.caption(f"Couldn't load: {tree_result.get('stderr') or 'unknown error'}")

    st.divider()
    st.subheader("Local commands")
    for category, commands in COMMAND_CATEGORIES.items():
        with st.expander(category):
            st.markdown("\n".join(f"- `{cmd}`" for cmd in commands))
    st.caption("Confined to whichever directories have been added on each machine (see Workspace above).")
    if local_agent_configs:
        st.caption(f"🔧 {NAME} connected and ready to run.")
    else:
        st.caption(f"No connected machines in this Environment. Download {NAME} from the home page to add one.")
        # Manual fallbacks for the machine this browser tab is itself
        # running on, distinct from the "connected machines" tracked above
        # (which may be entirely different physical hosts, already paired
        # elsewhere): two distinct failure modes, both confirmed via a real
        # click-through test in the original single-host version --
        # "Reconnect" re-fires the casper://pair hand-off (for when it
        # truly never reached this machine's daemon -- wasn't running yet,
        # missed the event); "Check again" just re-runs the host lookup
        # above (for when pairing *did* succeed, only moments after this
        # page's one-shot check already gave up -- the retry loop above
        # covers the common case, but a slow click through Chrome's "Open
        # Casper?" prompt can still outlast it).
        reconnect_url = build_pair_url(current_token(), username)
        st.markdown(f'Running {NAME} on this machine? <a href="{reconnect_url}">Reconnect</a>', unsafe_allow_html=True)
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

if "messages" not in st.session_state:
    st.session_state.messages = []
    # Only worth a proactive welcome message when there's exactly one
    # connected machine to name -- with several, "your directories are X"
    # would just be misleading about which one.
    if len(local_agent_configs) == 1:
        (only_config,) = local_agent_configs.values()
        directories = only_config.get("workspace") or []
        if directories:
            plural = "y is" if len(directories) == 1 else "ies are"
            st.session_state.messages.append(
                {"role": "assistant", "content": f"Your director{plural} {', '.join(directories)}"}
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


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message.get("image"):
            st.image(base64.b64decode(message["image"]))
        show_web_search(message.get("searches"), message.get("sources", []))
        show_code_interpreter(message.get("code_blocks", []))
        show_local_agent_calls(message.get("local_agent_calls", []))
        show_transfer_calls(message.get("transfer_calls", []))

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
