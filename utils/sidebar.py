import json
import threading
import time

import requests
import streamlit as st

from utils.auth import (
    build_pair_url,
    current_token,
    list_environments,
    list_hosts,
    revoke_token_with_auth_service,
    signout_all_hosts,
)
from utils.branding import NAME
from utils.browser_nav import click_anchor_js

# Mirrors casper_tool.py's COMMAND_CATEGORIES -- the two run as separate
# processes on separate machines, so this list is duplicated rather than
# imported. Keep them in sync by hand. Lives here (not pages/chat.py) since
# render_sidebar() below is what actually displays it (in the "Local
# commands" expanders), and pages/chat.py's own tool-schema code just
# imports it back for the model-facing action enum.
COMMAND_CATEGORIES = {
    "Git": ["status", "branch", "log"],
    "Navigation": ["pwd", "cd", "ls", "tree", "list_directories"],
    "Management": ["mkdir", "touch", "cp", "mv", "rm", "rmdir"],
    "Viewing & Searching": ["cat", "less", "head", "tail", "grep", "find"],
}
GIT_ACTIONS = set(COMMAND_CATEGORIES["Git"])

MAX_TREE_RENDER_DEPTH = 2  # independent of the server's own depth-4 fetch cap -- see below
MAX_TREE_ENTRIES_PER_DIR = 50


def start_sign_out():
    # Exported (not sidebar-private) -- the sign-out button itself lives in
    # utils/topbar.py's render_topbar() now, next to the Settings
    # gear, not in the sidebar; this and handle_sign_out_if_requested below
    # stayed here since they're about the sign-out *flow*, not where its
    # button happens to be drawn. on_click (not "if st.button(...):") so
    # this runs as part of Streamlit's own click-handling, before the rerun
    # it then triggers automatically -- more robust than checking the
    # button's return value inline, which a couple of reported cases
    # suggest can occasionally miss a click's first rerun. No st.rerun()
    # needed/wanted here; Streamlit already reruns once after any on_click
    # callback returns.
    st.session_state["_signing_out"] = True


def handle_sign_out_if_requested():
    """Call right after require_app_subdomain(), before require_agent_session()
    -- on every page with the sign-out icon (utils/topbar.py's
    render_topbar(), called from every real in-app page). Split from
    the button click itself (start_sign_out above) on purpose: these are
    slow, blocking network calls (up to several seconds), and running them
    directly in the button's own script run left the click looking like it
    hadn't registered -- if the user clicked again before this finished,
    Streamlit cancels the still-running script for the newer one, aborting
    these calls mid-flight. Setting a flag and rerunning first means the
    click itself is answered instantly, and this (visibly, via the spinner)
    runs on its own dedicated rerun instead. No-op (returns immediately) on
    every page load that isn't actually mid-sign-out."""
    if not st.session_state.get("_signing_out"):
        return
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


def _render_chrome_css():
    """hide_streamlit_chrome() (utils/branding.py) fully hides Streamlit's
    own header bar AND toolbar ([data-testid="stHeader"]/[data-testid=
    "stToolbar"], both display: none) -- but confirmed directly, by reading
    Streamlit's own frontend source, that the button which reopens a
    collapsed sidebar (data-testid="stExpandSidebarButton") is rendered
    *inside* stToolbar specifically, not stHeader generally. Both re-shown
    here (every page with a real sidebar needs this now, not just chat.py),
    keeping only stMainMenu (the hamburger/settings menu, a sibling of the
    expand button within stToolbar, not an ancestor) hidden. Layered after
    hide_streamlit_chrome()'s own <style> tag, so this wins the cascade for
    equally-specific, both-!important rules on the same selectors.

    Also: initial_sidebar_state="expanded" (set by each page) turned out not
    to be enough on its own to reset the sidebar to open on every fresh page
    load -- confirmed directly: Streamlit persists whatever the user last
    manually toggled the sidebar to in this browser's own localStorage (key
    pattern stSidebarCollapsed-<id>), and consults *that* before
    initial_sidebar_state at all on every future load, including a brand
    new session. Cleared here every fresh page load (not gated to run once
    per session -- each collapse the user does during a session writes a
    fresh key, so this needs to re-clear on every new load, not just the
    first one this browser has ever seen) so initial_sidebar_state actually
    gets a chance to apply each time. Iterates rather than removing one
    constructed key directly, since the exact <id> isn't a documented/stable
    value worth depending on.

    stExpandSidebarButton's own padding-top: forcing stHeader/stToolbar's
    height down to auto/0 above (needed so the collapsed header doesn't
    reserve its native ~headerHeight of dead space above the page) has a
    side effect: Streamlit's own stSidebarHeader (the sidebar's *own*
    header, holding the "collapse" arrow when expanded) keeps its native
    fixed headerHeight box with flex-centered content, so its arrow icon
    sits vertically centered within a tall box -- but stExpandSidebarButton
    (the "expand" arrow, shown in stToolbar when collapsed) now sits in a
    box shrunk to fit the icon exactly, with none of that centering
    headroom above it. The two arrows are meant to occupy the same visual
    spot as the sidebar toggles between collapsed/expanded, so this nudges
    the expand arrow down to match -- confirmed directly (reading both
    components' own styled-component definitions) they share the same
    headerHeight token and flex-center alignment otherwise; this is an
    approximation of the resulting gap, not an exact measurement, so it may
    still need a small visual tweak."""
    st.html(
        """
        <style>
        [data-testid="stHeader"], [data-testid="stToolbar"] {
            display: block !important;
            background: transparent !important;
            height: auto !important;
            min-height: 0 !important;
        }
        [data-testid="stMainMenu"] {
            display: none !important;
        }
        [data-testid="stExpandSidebarButton"] {
            padding-top: 0.75rem !important;
        }
        </style>
        """
    )
    st.iframe(
        """
        <script>
        (function() {
            try {
                var keys = Object.keys(window.parent.localStorage);
                for (var i = 0; i < keys.length; i++) {
                    if (keys[i].indexOf('stSidebarCollapsed-') === 0) {
                        window.parent.localStorage.removeItem(keys[i]);
                    }
                }
            } catch (e) {}
        })();
        </script>
        """,
        height=1,
    )
    # The Environment-gear and workspace-refresh icons (both below) are
    # meant to read as small, secondary glyphs, not full bordered buttons --
    # strips Streamlit's default button chrome down to just the icon glyph,
    # in every interaction state (hover/focus/active too, so nothing picks
    # up the theme's accent color on interaction -- "monochromatic" per the
    # original ask, still true now that these are Material icons instead of
    # emoji). font-size scales the icon glyph itself, since Streamlit
    # renders a `icon=":material/xxx:"` button icon as a ligature in an icon
    # font -- sized like any other text. Targeted by key via Streamlit's own
    # .st-key-<key> convention rather than a page-wide selector, so no other
    # button on either page is affected. See utils/topbar.py for the
    # Settings gear/sign-out icons' own (larger) sizing -- a separate CSS
    # block, since "slightly larger" than these was requested for those.
    #
    # The icon *rows* (Environment+gear, Workspace+refresh) are each their
    # own st.container(key="icon_row_...") specifically so the icon's own
    # column can be pinned to a fixed width via the shared "st-key-icon_row_"
    # prefix below -- confirmed necessary against a real narrow-sidebar
    # drag-resize: st.columns' default proportional flex widths shrink the
    # icon column right along with the label column, eventually clipping the
    # icon itself. Pinning the icon column's flex to a fixed, non-shrinking
    # basis (and letting the label column's own text clip/ellipsis instead)
    # keeps the icon fully visible at any width.
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
            font-size: 1.4rem !important;
            line-height: 1.4rem !important;
        }
        .st-key-env_gear_button button:hover, .st-key-refresh_workspace button:hover,
        .st-key-env_gear_button button:focus, .st-key-refresh_workspace button:focus,
        .st-key-env_gear_button button:active, .st-key-refresh_workspace button:active {
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
            color: inherit !important;
        }
        [class*="st-key-icon_row_"] [data-testid="stColumn"]:last-child {
            flex: 0 0 auto !important;
            width: 2.75rem !important;
            min-width: 2.75rem !important;
        }
        [class*="st-key-icon_row_"] [data-testid="stColumn"]:first-child {
            min-width: 0 !important;
            overflow: hidden !important;
        }
        [class*="st-key-icon_row_"] [data-testid="stColumn"]:first-child > div {
            min-width: 0 !important;
        }
        </style>
        """
    )


def _fetch_local_json(config, action, **kwargs):
    """Like call_local_agent (pages/chat.py), but returns the parsed
    response dict (or an error dict in the same {success, stdout, stderr}
    shape commands.Result already uses) instead of a model-facing string --
    for UI code that needs to read a result programmatically, namely the
    workspace browser below and pages/chat.py's own file-transfer tool."""
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
            # Command templates enabled on this specific host -- see
            # auth_service's HostInfo.command_templates. pages/chat.py
            # builds its run_command_template tool schema from these.
            "command_templates": h.get("command_templates") or [],
        }
    return configs


def render_sidebar(username):
    """The entire sidebar: "Signed in as", Environment selection, host
    picker, workspace directory browser, and Local commands reference --
    identical on every page that calls it (pages/chat.py,
    pages/environments.py, pages/settings_profile.py,
    pages/settings_security.py), so a user sees the exact same controls
    (and can switch host/Environment, browse directories, etc.) regardless
    of which page they're on. The brand/logo and sign-out both live in
    utils/topbar.py's render_topbar() instead, not here -- see its own
    module docstring for why. Returns (local_agent_configs,
    selected_host_label) -- pages/chat.py's own tool-calling code needs
    both; every other caller just ignores them, since only chat.py does
    tool calling."""
    _render_chrome_css()

    # Looked up once (cached in session_state) rather than carried via query
    # params -- the daemon no longer redirects a browser tab itself (see
    # pages/signin.py), so it can't hand local_agent_url/workspace along that
    # way anymore. Instead each attached daemon reports its own reachability
    # to the auth service on pairing/toggle (see
    # agent/internal/config/presence.go), and this looks the whole set up by
    # the same token require_agent_session() already verified. Deliberately
    # the same session_state keys on both pages -- a rename/forget/
    # membership change made on the Environments page is immediately what
    # this sees too, next time it loads, with no separate cache to go stale.
    if "_hosts" not in st.session_state:
        # Retried briefly rather than checked once: landing here right after
        # sign-in (the common case) races the casper://pair hand-off, which
        # typically finishes a beat after this page has already loaded --
        # confirmed directly (a real click-through showed "Not connected" on
        # first load, then the daemon's pairing log line appeared a moment
        # later). Same reasoning as the old pairing spinner's poll loop: a
        # single check is too eager, but this shouldn't retry forever
        # either, so it gives up after a few seconds, leaving whatever's
        # connected by then (possibly nothing).
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
    local_agent_configs = _build_local_agent_configs(connected_active_hosts)

    def _recheck_hosts():
        # Drops the cached (possibly stale) host/Environment state so the
        # retry loop above runs again on the rerun this triggers -- see the
        # "Check again" button below.
        st.session_state.pop("_hosts", None)
        st.session_state.pop("_environments", None)
        st.session_state.pop("_active_environment_id", None)

    def _switch_environment():
        st.session_state["_active_environment_id"] = st.session_state["_environment_selector"]

    with st.sidebar:
        # The brand/logo itself is no longer here -- see utils/topbar.py's
        # render_topbar(), which pins it to the viewport's top-left corner
        # (mirroring the Settings/sign-out icons pinned top-right) instead
        # of living inside the sidebar, so it stays visible even when the
        # sidebar is collapsed.
        st.caption(f"Signed in as {username}")

        st.divider()
        with st.container(key="icon_row_env"):
            env_label_col, env_gear_col = st.columns([5, 1])
            with env_label_col:
                st.markdown("**Environment**")
            with env_gear_col:
                st.button(
                    "",
                    icon=":material/settings:",
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
                # Click-selectable -- the selection becomes the default
                # target for local commands the model doesn't name a host
                # for (see call_local_agent's default_host in pages/chat.py),
                # same idea as the active Environment selector just above
                # it. Re-defaults to the first host whenever the current
                # selection isn't valid for this Environment any more
                # (switched Environments, or never selected yet) --
                # Streamlit's radio requires its keyed session_state value
                # to already be one of the options before it's instantiated.
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

        with st.container(key="icon_row_workspace"):
            workspace_label_col, workspace_refresh_col = st.columns([5, 1])
            with workspace_label_col:
                st.subheader("Workspace")
            with workspace_refresh_col:
                # Next to the active host's directory list, not a standalone
                # button at the bottom of it -- same label-plus-icon
                # treatment as the Environment gear above.
                if selected_config is not None and st.button(
                    "", icon=":material/refresh:", key="refresh_workspace", help="Refresh directories"
                ):
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
            # since an unchanged directory list would otherwise just
            # silently redraw looking identical to before.
            with st.spinner("Refreshing..."):
                time.sleep(0.4)
            st.rerun()
        else:

            def _start_add_directory():
                # Fast, fire-and-forget -- the daemon opens the native
                # picker in its own background goroutine and returns
                # immediately (see agent/internal/commands's
                # runAddDirectory), rather than blocking on however long the
                # user takes to respond, so doing this inline in the click
                # handler (unlike _start_sign_out's flag-and-rerun pattern)
                # doesn't stall the click.
                result = _fetch_local_json(selected_config, "add_directory")
                if not result.get("success"):
                    st.session_state["_add_directory_error"] = (
                        result.get("stderr") or "Couldn't open the folder picker."
                    )
                    st.session_state.pop("_add_directory_pending", None)
                else:
                    st.session_state["_add_directory_pending"] = selected_host_id
                    st.session_state.pop("_add_directory_error", None)

            st.button("➕ Add directory", on_click=_start_add_directory, key="add_directory_button")

            if st.session_state.get("_add_directory_error"):
                st.error(st.session_state.pop("_add_directory_error"))

            if st.session_state.get("_add_directory_pending") == selected_host_id:
                # A native dialog just opened on the *selected host's*
                # machine -- not necessarily this browser's own. Brief poll
                # (mirrors the hosts retry-loop above) for the fast case
                # where it was answered right away; the realistic case is a
                # human needs a moment to switch apps and pick a folder, so
                # this expects to usually fall through to the manual "Check
                # again" below.
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
                        # Cached per (host, directory) rather than re-fetched
                        # on every rerun (every chat message would otherwise
                        # re-walk every directory's tree) -- a single `tree`
                        # call already returns the full depth-4 structure in
                        # one request (see
                        # agent/internal/commands/commands.go's runTree), so
                        # this is one fetch per directory per browser
                        # session, refreshed only on demand.
                        cache_key = f"_tree_{selected_host_id}_{directory}"
                        if cache_key not in st.session_state:
                            with st.spinner("Loading..."):
                                st.session_state[cache_key] = _fetch_local_json(
                                    selected_config, "tree", path=directory
                                )
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
            # running on, distinct from the "connected machines" tracked
            # above (which may be entirely different physical hosts,
            # already paired elsewhere): two distinct failure modes, both
            # confirmed via a real click-through test in the original
            # single-host version -- "Reconnect" re-fires the casper://pair
            # hand-off (for when it truly never reached this machine's
            # daemon -- wasn't running yet, missed the event); "Check again"
            # just re-runs the host lookup above (for when pairing *did*
            # succeed, only moments after this page's one-shot check already
            # gave up -- the retry loop above covers the common case, but a
            # slow click through Chrome's "Open Casper?" prompt can still
            # outlast it).
            reconnect_url = build_pair_url(current_token(), username)
            st.markdown(
                f'Running {NAME} on this machine? <a href="{reconnect_url}">Reconnect</a>', unsafe_allow_html=True
            )
            st.button("Check again", on_click=_recheck_hosts)

    return local_agent_configs, selected_host_label
