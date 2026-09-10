import streamlit as st

from utils.auth import (
    current_token,
    get_profile,
    require_agent_session,
    require_app_subdomain,
    update_profile,
)
from utils.sidebar import handle_sign_out_if_requested, render_sidebar
from utils.topbar import render_topbar

st.set_page_config(page_title="Casper - Profile", page_icon="👻", initial_sidebar_state="expanded")
require_app_subdomain()

# Handles (and st.stop()s on) an in-flight sign-out -- see
# utils/sidebar.py's own docstring for why this has to run before
# require_agent_session() below, not after.
handle_sign_out_if_requested()

username = require_agent_session()
render_topbar()
render_sidebar(username)

st.title("Profile")

AUTH_DOMAIN = st.secrets["AUTH_SERVICE_DOMAIN"]
TOKEN = current_token()

if "_profile" not in st.session_state:
    st.session_state["_profile"] = get_profile(AUTH_DOMAIN, TOKEN) or {}
profile = st.session_state["_profile"]


def _save(**fields):
    """Saves immediately on change (no separate "Save" button, matching
    pages/environments.py's own auto-save-per-field convention) and shows
    the same "Update saved" toast on success. auth_service does the actual
    validation/masking (see models.py's ProfileUpdateRequest) -- this just
    surfaces whatever it says back, success or error. Returns the updated
    profile dict on success, or None on error -- callers that need to
    reflect a server-normalized value back into their own widget (e.g. the
    reformatted phone number below) use the return value for that."""
    result = update_profile(AUTH_DOMAIN, TOKEN, **fields)
    if result and result.get("error"):
        st.session_state["_profile_error"] = result["error"]
        return None
    st.session_state.pop("_profile_error", None)
    st.session_state["_profile"] = result
    st.session_state["_profile_saved"] = True
    return result


def _on_email_change():
    _save(email=st.session_state["_email_input"].strip())


def _on_email_notifications_change():
    _save(email_notifications_enabled=st.session_state["_email_notifications_checkbox"])


def _on_sms_number_change():
    result = _save(sms_number=st.session_state["_sms_number_input"].strip())
    if result is not None:
        # Reflects auth_service's normalized "(XXX) XXX-XXXX" mask back into
        # the widget -- st.text_input's value= param only seeds the very
        # first render, so the widget's own session_state key is what
        # actually controls what's displayed on every render after that.
        st.session_state["_sms_number_input"] = result["sms_number"]


def _on_sms_notifications_change():
    _save(sms_notifications_enabled=st.session_state["_sms_notifications_checkbox"])


# Streamlit has no live, keystroke-level input-masking widget -- the
# "masking" half of "input masking and data validation" happens once a
# field is actually submitted (Enter/blur, same as any st.text_input):
# auth_service normalizes a valid phone number into a canonical
# "(XXX) XXX-XXXX" display form server-side (see models.py), and this page
# re-displays whatever comes back by writing straight into the widget's own
# session_state key -- st.text_input's value= param only seeds the very
# first render, so this is what makes the reformatted value actually show
# up after a successful save.
if "_email_input" not in st.session_state:
    st.session_state["_email_input"] = profile.get("email", "")
if "_sms_number_input" not in st.session_state:
    st.session_state["_sms_number_input"] = profile.get("sms_number", "")

st.text_input(
    "Email",
    key="_email_input",
    on_change=_on_email_change,
    placeholder="you@example.com",
)
st.checkbox(
    "Allow email notifications?",
    value=profile.get("email_notifications_enabled", False),
    key="_email_notifications_checkbox",
    on_change=_on_email_notifications_change,
)

st.divider()

st.text_input(
    "SMS number",
    key="_sms_number_input",
    on_change=_on_sms_number_change,
    placeholder="(555) 123-4567",
)
st.checkbox(
    "Allow texts?",
    value=profile.get("sms_notifications_enabled", False),
    key="_sms_notifications_checkbox",
    on_change=_on_sms_notifications_change,
)

# A fixed-height slot for the save status message -- reserved whether or
# not anything is actually shown in it this rerun, so the "← Back to chat"
# button below doesn't jump up/down depending on whether a save just
# happened. Plain st.empty() alone doesn't do this (it collapses to zero
# height with nothing written into it); the min-height on this specific
# keyed container is what actually holds the space open.
st.html("<style>.st-key-save_status_row { min-height: 3rem; }</style>")
with st.container(key="save_status_row"):
    if st.session_state.get("_profile_error"):
        st.error(st.session_state.pop("_profile_error"))
    if st.session_state.pop("_profile_saved", False):
        st.success("Update saved")

st.divider()
st.button("← Back to chat", on_click=lambda: st.switch_page("pages/chat.py"))
