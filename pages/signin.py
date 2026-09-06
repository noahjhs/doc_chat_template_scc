import json

import streamlit as st

from utils.auth import build_pairing_redirect_url, login_with_auth_service
from utils.browser_nav import autofocus_input_js, click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- keeps the tab title
# searchable by casper_tool.py's bring-tab-into-view AppleScript.
st.set_page_config(page_title="Casper", page_icon="👻")

st.title("Sign in")

callback_port = st.query_params.get("callback_port", "")
nonce = st.query_params.get("nonce", "")

if not (callback_port and nonce):
    st.info(
        "This page is part of Casper's sign-in flow — run it "
        "first (see the home page), and it'll bring you back here "
        "automatically."
    )
    st.stop()

auth_domain = st.secrets["AUTH_SERVICE_DOMAIN"]

# Once login succeeds, the form disappears entirely (replaced by the
# redirect below) rather than staying on screen -- otherwise, if the
# redirect is slow or a browser doesn't act on a dynamically-inserted
# meta-refresh tag, a confused user could click "Sign in" again, which
# would log in a second time and rotate the very token the first attempt
# is still waiting to use.
if "_login_redirect_url" not in st.session_state:
    with st.form("signin_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")

    # Ready to type into without an extra click.
    st.iframe(f"<script>{autofocus_input_js('Username')}</script>", height=1)

    st.markdown(
        "Don't have an account? "
        f'<a href="/signup?callback_port={callback_port}&nonce={nonce}" target="_self">Sign up</a>',
        unsafe_allow_html=True,
    )

    if submitted:
        result = login_with_auth_service(auth_domain, username, password)
        if "error" in result:
            st.error(result["error"])
        else:
            st.session_state["_login_redirect_url"] = build_pairing_redirect_url(
                callback_port, nonce, result["token"], result["username"]
            )
            st.rerun()  # so the form is fully gone on the next render, not shown alongside the success message

if "_login_redirect_url" in st.session_state:
    redirect_url = st.session_state["_login_redirect_url"]
    st.success("Signed in.")
    # A same-tab fallback link, in case the auto-navigate below doesn't
    # fire for some reason (st.link_button opens a new tab, which isn't
    # what we want here).
    st.markdown(
        f'<a id="continue-link" href="{redirect_url}" target="_self">Continue if nothing happens</a>',
        unsafe_allow_html=True,
    )
    # Auto-navigate by clicking that same link programmatically -- see
    # click_anchor_js's docstring for why a direct window.parent.location
    # assignment doesn't reliably work from inside st.iframe's sandbox.
    st.iframe(f"<script>{click_anchor_js(json.dumps(redirect_url))}</script>", height=1)
