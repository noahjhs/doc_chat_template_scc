import json
import time
from urllib.parse import quote

import streamlit as st

from utils.auth import build_pair_url, login_with_auth_service
from utils.browser_nav import autofocus_input_js, click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- a consistent tab
# identity across the whole flow.
st.set_page_config(page_title="Casper", page_icon="👻")

st.title("Sign in")

auth_domain = st.secrets["AUTH_SERVICE_DOMAIN"]

# Once login succeeds, the form disappears entirely (replaced by the
# redirect below) rather than staying on screen -- otherwise, if the
# redirect is slow or a browser doesn't act on a dynamically-inserted
# meta-refresh tag, a confused user could click "Sign in" again, which
# would log in a second time and rotate the very token the first attempt
# is still waiting to use.
if "_login_token" not in st.session_state:
    with st.form("signin_form"):
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in", type="primary")

    # Ready to type into without an extra click.
    st.iframe(f"<script>{autofocus_input_js('Username')}</script>", height=1)

    st.markdown('Don\'t have an account? <a href="/signup" target="_self">Sign up</a>', unsafe_allow_html=True)

    if submitted:
        # The spinner is the fix for a real reported bug: a repeated
        # wrong-password submission renders the exact same st.error() text
        # in the exact same spot, so nothing on screen visibly changes and
        # the user can't tell the click even registered. The auth service
        # sits behind a fast local tunnel though (well under half a second
        # round trip), so on its own the spinner flashes too briefly to
        # actually see -- padding to a minimum visible duration is what
        # makes it register as a real, perceptible transition.
        start = time.monotonic()
        with st.spinner("Signing in..."):
            result = login_with_auth_service(auth_domain, username, password)
            time.sleep(max(0.0, 0.5 - (time.monotonic() - start)))
        if "error" in result:
            st.error(result["error"])
        else:
            st.session_state["_login_token"] = result["token"]
            st.session_state["_login_username"] = result["username"]
            st.rerun()  # so the form is fully gone on the next render, not shown alongside the success message

if "_login_token" in st.session_state:
    token = st.session_state["_login_token"]
    username = st.session_state["_login_username"]
    pair_url = build_pair_url(token, username)
    chat_url = f"/chat?local_agent_token={quote(token, safe='')}"

    st.success("Signed in.")
    # A same-tab fallback link, in case the auto-navigate below doesn't
    # fire for some reason (st.link_button opens a new tab, which isn't
    # what we want here).
    st.markdown(
        f'<a id="continue-link" href="{chat_url}" target="_self">Continue if nothing happens</a>',
        unsafe_allow_html=True,
    )
    # Fires the casper://pair hand-off first (a custom-scheme anchor click
    # dispatches to the OS without navigating the tab away -- unlike an
    # http(s) URL), then redirects this same tab to /chat. Both in one
    # script so the pairing dispatch has definitely started before the /chat
    # navigation unloads the page. See click_anchor_js's docstring for why a
    # direct window.parent.location assignment doesn't reliably work from
    # inside st.iframe's sandbox.
    st.iframe(
        f"<script>{click_anchor_js(json.dumps(pair_url))}{click_anchor_js(json.dumps(chat_url))}</script>",
        height=1,
    )
