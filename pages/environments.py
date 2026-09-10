import streamlit as st

from utils.auth import (
    add_host_to_environment,
    create_environment,
    current_token,
    delete_environment,
    forget_host,
    list_environments,
    list_hosts,
    remove_host_from_environment,
    rename_environment,
    rename_host,
    require_agent_session,
    require_app_subdomain,
)
from utils.branding import NAME
from utils.sidebar import handle_sign_out_if_requested, render_sidebar
from utils.topbar import render_topbar

st.set_page_config(page_title="Casper - Hosts & Environments", page_icon="👻", initial_sidebar_state="expanded")
require_app_subdomain()

# Handles (and st.stop()s on) an in-flight sign-out -- see
# utils/sidebar.py's own docstring for why this has to run before
# require_agent_session() below, not after.
handle_sign_out_if_requested()

username = require_agent_session()

# Same brand/Settings/sign-out topbar + sidebar as every other real in-app
# page -- this page doesn't do any tool-calling, so render_sidebar's own
# local_agent_configs/selected_host_label return value is simply unused
# here.
render_topbar()
render_sidebar(username)

st.title("Hosts & Environments")

AUTH_DOMAIN = st.secrets["AUTH_SERVICE_DOMAIN"]
TOKEN = current_token()

# Deliberately the same session_state keys pages/chat.py reads -- so a
# rename/forget/membership change made here is immediately what chat.py
# sees too, next time it loads, with no separate cache to go stale.
if "_hosts" not in st.session_state:
    st.session_state["_hosts"] = (list_hosts(AUTH_DOMAIN, TOKEN) or {}).get("hosts", [])
if "_environments" not in st.session_state:
    st.session_state["_environments"] = (list_environments(AUTH_DOMAIN, TOKEN) or {}).get("environments", [])


def _refresh():
    # Called only right after a change actually succeeded (every call site
    # is inside the non-error branch of a mutation) -- so it doubles as the
    # signal for the "Update saved" message at the bottom of the page.
    st.session_state.pop("_hosts", None)
    st.session_state.pop("_environments", None)
    st.session_state["_just_saved"] = True


hosts = st.session_state["_hosts"]
environments = st.session_state["_environments"]

st.header("Hosts")
st.caption(f"Every machine you've ever paired {NAME} on -- connected or not.")
if not hosts:
    st.caption(f"No hosts paired yet. Download {NAME} from the home page and sign in from a machine to pair it.")
for host in hosts:
    with st.container(border=True):
        top = st.columns([3, 2, 1])
        with top[0]:
            status = "🔧 Connected" if host.get("connected") else "⚪ Not connected"
            st.markdown(f"**{host['label']}** — {status}")
            if host.get("hostname"):
                st.caption(host["hostname"])
        with top[1]:
            new_label = st.text_input(
                "Rename", value=host["label"], key=f"rename_{host['host_id']}", label_visibility="collapsed"
            )
            if new_label.strip() and new_label != host["label"]:
                if st.button("Save name", key=f"save_{host['host_id']}"):
                    result = rename_host(AUTH_DOMAIN, TOKEN, host["host_id"], new_label.strip())
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.rerun()
        with top[2]:
            confirm_key = f"confirm_forget_{host['host_id']}"
            if st.session_state.get(confirm_key):
                if st.button("Confirm forget", key=f"do_forget_{host['host_id']}", type="primary"):
                    result = forget_host(AUTH_DOMAIN, TOKEN, host["host_id"])
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
                if st.button("Cancel", key=f"cancel_forget_{host['host_id']}"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
            else:
                if st.button("Forget", key=f"forget_{host['host_id']}"):
                    st.session_state[confirm_key] = True
                    st.rerun()

st.divider()
st.header("Environments")
st.caption("Named groupings of your hosts -- which machines are available to the assistant during a chat session.")

with st.form("create_environment_form", clear_on_submit=True):
    new_env_name = st.text_input("New Environment name")
    if st.form_submit_button("Create Environment") and new_env_name.strip():
        result = create_environment(AUTH_DOMAIN, TOKEN, new_env_name.strip())
        if result and result.get("error"):
            st.error(result["error"])
        else:
            _refresh()
            st.rerun()

if not environments:
    st.caption("No Environments yet.")
for env in environments:
    with st.container(border=True):
        top = st.columns([3, 2, 1])
        with top[0]:
            st.markdown(f"**{env['name']}**")
        with top[1]:
            new_name = st.text_input(
                "Rename", value=env["name"], key=f"env_name_{env['id']}", label_visibility="collapsed"
            )
            if new_name.strip() and new_name != env["name"]:
                if st.button("Save name", key=f"env_save_{env['id']}"):
                    result = rename_environment(AUTH_DOMAIN, TOKEN, env["id"], new_name.strip())
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.rerun()
        with top[2]:
            confirm_key = f"confirm_delete_env_{env['id']}"
            if st.session_state.get(confirm_key):
                if st.button("Confirm delete", key=f"do_delete_env_{env['id']}", type="primary"):
                    result = delete_environment(AUTH_DOMAIN, TOKEN, env["id"])
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
                if st.button("Cancel", key=f"env_cancel_{env['id']}"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
            else:
                if st.button("Delete", key=f"env_delete_{env['id']}"):
                    st.session_state[confirm_key] = True
                    st.rerun()

        if hosts:
            st.caption("Hosts in this Environment:")
            checkbox_cols = st.columns(min(len(hosts), 3) or 1)
            for i, host in enumerate(hosts):
                checked = host["host_id"] in env["host_ids"]
                with checkbox_cols[i % len(checkbox_cols)]:
                    new_checked = st.checkbox(
                        host["label"], value=checked, key=f"env_{env['id']}_host_{host['host_id']}"
                    )
                if new_checked != checked:
                    if new_checked:
                        result = add_host_to_environment(AUTH_DOMAIN, TOKEN, env["id"], host["host_id"])
                    else:
                        result = remove_host_from_environment(AUTH_DOMAIN, TOKEN, env["id"], host["host_id"])
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.rerun()
        else:
            st.caption("No hosts to add yet.")

if st.session_state.pop("_just_saved", False):
    st.success("Update saved")

st.divider()
st.button("← Back to chat", on_click=lambda: st.switch_page("pages/chat.py"))
