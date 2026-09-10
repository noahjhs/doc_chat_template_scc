import streamlit as st

from utils.auth import (
    current_token,
    get_profile,
    require_agent_session,
    require_app_subdomain,
    update_profile,
)
from utils.sidebar import handle_sign_out_if_requested, render_sidebar
from utils.topbar import render_settings_menu

st.set_page_config(page_title="Casper - Security", page_icon="👻", initial_sidebar_state="expanded")
require_app_subdomain()

# Handles (and st.stop()s on) an in-flight sign-out -- see
# utils/sidebar.py's own docstring for why this has to run before
# require_agent_session() below, not after.
handle_sign_out_if_requested()

username = require_agent_session()
render_settings_menu()
render_sidebar(username)

st.title("Security")

AUTH_DOMAIN = st.secrets["AUTH_SERVICE_DOMAIN"]
TOKEN = current_token()

if "_profile" not in st.session_state:
    st.session_state["_profile"] = get_profile(AUTH_DOMAIN, TOKEN) or {}
profile = st.session_state["_profile"]

# Real Touch ID/passkey (WebAuthn) registration is a substantially larger,
# separate feature -- its own auth_service credential-registration flow,
# browser-side navigator.credentials JS, secure storage -- deliberately not
# built yet. This is a real, wired-up button, just one that's honest about
# not doing anything yet, rather than a fake "success" that implies
# security it doesn't actually have.
if st.button("Set up biometric authentication"):
    st.info("Biometric sign-in isn't available yet -- this is a placeholder for now.")

st.divider()
st.subheader("Allow chat to configure")
st.caption(
    "Lets the assistant make changes in these areas directly during a "
    "conversation, instead of only ever suggesting them. Off by default, "
    "and not yet enforced anywhere -- these are saved preferences for now."
)

# "Command sets"/"Apps"/"Local agents" don't correspond to any existing
# modeled concept in this codebase the way Hosts/Environments do (see
# auth_service/db.py's hosts/environments tables) -- persisted here as
# plain preferences either way, but real enforcement for those three needs
# its own design pass first (see auth_service/main.py's /profile docstring).
PERMISSION_FIELDS = [
    ("allow_configure_command_sets", "Command sets"),
    ("allow_configure_apps", "Apps"),
    ("allow_configure_hosts", "Hosts"),
    ("allow_configure_environments", "Environments"),
    ("allow_configure_local_agents", "Local agents"),
]


def _make_permission_on_change(field_key):
    def _on_change():
        result = update_profile(AUTH_DOMAIN, TOKEN, **{field_key: st.session_state[f"_perm_{field_key}"]})
        if result and result.get("error"):
            st.session_state["_profile_error"] = result["error"]
            return
        st.session_state.pop("_profile_error", None)
        st.session_state["_profile"] = result
        st.session_state["_profile_saved"] = True

    return _on_change


for field_key, label in PERMISSION_FIELDS:
    st.checkbox(
        label,
        value=profile.get(field_key, False),
        key=f"_perm_{field_key}",
        on_change=_make_permission_on_change(field_key),
    )

# A fixed-height slot for the save status message -- see
# pages/settings_profile.py's own copy of this for why (reserved whether or
# not anything is actually shown in it this rerun, so "← Back to chat"
# below doesn't jump up/down depending on whether a save just happened).
st.html("<style>.st-key-save_status_row { min-height: 3rem; }</style>")
with st.container(key="save_status_row"):
    if st.session_state.get("_profile_error"):
        st.error(st.session_state.pop("_profile_error"))
    if st.session_state.pop("_profile_saved", False):
        st.success("Update saved")

st.divider()
st.button("← Back to chat", on_click=lambda: st.switch_page("pages/chat.py"))
