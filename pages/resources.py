import streamlit as st

from utils.auth import (
    add_command_template_to_host,
    create_command_template,
    current_token,
    delete_command_template,
    list_command_templates,
    list_hosts,
    remove_command_template_from_host,
    require_agent_session,
    require_app_subdomain,
    update_command_template,
)
from utils.sidebar import handle_sign_out_if_requested, render_sidebar
from utils.topbar import render_topbar

st.set_page_config(page_title="Casper - Command Templates", page_icon="👻", initial_sidebar_state="expanded")
require_app_subdomain()

# Handles (and st.stop()s on) an in-flight sign-out -- see
# utils/sidebar.py's own docstring for why this has to run before
# require_agent_session() below, not after.
handle_sign_out_if_requested()

username = require_agent_session()
render_topbar()
render_sidebar(username)

st.title("Command Templates")
st.caption(
    "Reusable rules for how the assistant may invoke a CLI command on a host -- a fixed "
    "binary, a fixed set of allowed argument options, and whether it runs automatically "
    '("Allow") or needs your approval in chat first ("Ask"). See the "Resources" plan for '
    "the full design -- addressable directories (in the sidebar's Workspace section) are "
    "the other kind of Resource."
)

AUTH_DOMAIN = st.secrets["AUTH_SERVICE_DOMAIN"]
TOKEN = current_token()

# Deliberately the same "_hosts" session_state key pages/chat.py and
# pages/environments.py read -- so a change made here is immediately what
# they see too, next time it loads, with no separate cache to go stale.
if "_hosts" not in st.session_state:
    st.session_state["_hosts"] = (list_hosts(AUTH_DOMAIN, TOKEN) or {}).get("hosts", [])
if "_command_templates" not in st.session_state:
    st.session_state["_command_templates"] = (
        list_command_templates(AUTH_DOMAIN, TOKEN) or {}
    ).get("command_templates", [])


def _refresh():
    # Called only right after a change actually succeeded (every call site
    # is inside the non-error branch of a mutation) -- so it doubles as the
    # signal for the "Update saved" message at the bottom of the page.
    # Also clears the *daemon's* own cached copy is NOT done here directly
    # -- pages/chat.py's tool schema reads straight from _hosts (refreshed
    # below), and any already-running daemon picks up the change on its
    # own next pairing/resume, or via its "Refresh" trigger further down.
    st.session_state.pop("_hosts", None)
    st.session_state.pop("_command_templates", None)
    st.session_state["_just_saved"] = True


hosts = st.session_state["_hosts"]
templates = st.session_state["_command_templates"]

TIER_OPTIONS = ["ask", "allow"]
TIER_LABELS = {"ask": "Ask", "allow": "Allow"}

st.header("Create a template")
with st.form("create_template_form", clear_on_submit=True):
    name = st.text_input("Name", placeholder="npm scripts")
    binary = st.text_input("Binary", placeholder="npm")
    allowed_args_text = st.text_area(
        "Allowed arguments (one option per line)",
        placeholder="run build\nrun test\ninstall",
        help="Each line is one exact argument option the assistant may use -- nothing else is allowed.",
    )
    tier = st.radio("Tier", options=TIER_OPTIONS, format_func=lambda t: TIER_LABELS[t], horizontal=True)
    path_scoped = st.checkbox("Confine to this host's addressable directories", value=True)
    if st.form_submit_button("Create"):
        lines = [line.strip() for line in allowed_args_text.splitlines() if line.strip()]
        if not name.strip() or not binary.strip() or not lines:
            st.error("Name, binary, and at least one allowed argument option are required.")
        else:
            result = create_command_template(
                AUTH_DOMAIN,
                TOKEN,
                name.strip(),
                binary.strip(),
                [{"pattern": line} for line in lines],
                tier,
                path_scoped,
            )
            if result and result.get("error"):
                st.error(result["error"])
            else:
                _refresh()
                st.rerun()

st.divider()
st.header("Templates")
if not templates:
    st.caption("No command templates yet.")
for template in templates:
    with st.container(border=True):
        top = st.columns([3, 2, 1])
        with top[0]:
            st.markdown(f"**{template['name']}** — `{template['binary']}`")
            options = " | ".join(p["pattern"] for p in template["allowed_args"])
            st.caption(options)
        with top[1]:
            new_tier = st.radio(
                "Tier",
                options=TIER_OPTIONS,
                index=TIER_OPTIONS.index(template["tier"]),
                format_func=lambda t: TIER_LABELS[t],
                key=f"tier_{template['id']}",
                horizontal=True,
                label_visibility="collapsed",
            )
            if new_tier != template["tier"]:
                result = update_command_template(AUTH_DOMAIN, TOKEN, template["id"], tier=new_tier)
                if result and result.get("error"):
                    st.error(result["error"])
                else:
                    _refresh()
                    st.rerun()
        with top[2]:
            confirm_key = f"confirm_delete_template_{template['id']}"
            if st.session_state.get(confirm_key):
                if st.button("Confirm delete", key=f"do_delete_template_{template['id']}", type="primary"):
                    result = delete_command_template(AUTH_DOMAIN, TOKEN, template["id"])
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
                if st.button("Cancel", key=f"cancel_delete_template_{template['id']}"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
            else:
                if st.button("Delete", key=f"delete_template_{template['id']}"):
                    st.session_state[confirm_key] = True
                    st.rerun()

        if hosts:
            st.caption("Enabled on:")
            checkbox_cols = st.columns(min(len(hosts), 3) or 1)
            for i, host in enumerate(hosts):
                checked = host["host_id"] in template["host_ids"]
                with checkbox_cols[i % len(checkbox_cols)]:
                    new_checked = st.checkbox(
                        host["label"], value=checked, key=f"template_{template['id']}_host_{host['host_id']}"
                    )
                if new_checked != checked:
                    if new_checked:
                        result = add_command_template_to_host(AUTH_DOMAIN, TOKEN, template["id"], host["host_id"])
                    else:
                        result = remove_command_template_from_host(
                            AUTH_DOMAIN, TOKEN, template["id"], host["host_id"]
                        )
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.rerun()
        else:
            st.caption("No hosts to enable this on yet.")

# A fixed-height slot for the save status message -- see
# pages/settings_profile.py's own copy of this for why (reserved whether or
# not anything is actually shown in it this rerun, so "← Back to chat"
# below doesn't jump up/down depending on whether a save just happened).
st.html("<style>.st-key-save_status_row { min-height: 3rem; }</style>")
with st.container(key="save_status_row"):
    if st.session_state.pop("_just_saved", False):
        st.success("Update saved")

st.divider()
st.button("← Back to chat", on_click=lambda: st.switch_page("pages/chat.py"))
