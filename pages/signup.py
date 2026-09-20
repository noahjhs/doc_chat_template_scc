import json
import time

import streamlit as st

from utils.auth import build_pair_url, require_app_subdomain, signup_with_auth_service
from utils.branding import page_header
from utils.browser_nav import autofocus_input_js, click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- a consistent tab
# identity across the whole flow.
st.set_page_config(page_title="Casper - Sign up", page_icon="👻")
require_app_subdomain()

page_header()
st.title("Sign up")

auth_domain = st.secrets["AUTH_SERVICE_DOMAIN"]

# Once signup succeeds, the form disappears entirely (replaced by the
# success state below) rather than staying on screen -- otherwise a
# confused re-click would fail outright with "username taken".
if "_signup_token" not in st.session_state:
    with st.form("signup_form"):
        # autocomplete="new-password" (not "current-password") is the actual
        # signal browsers use to tell a signup form apart from a login one --
        # it's what stops Keychain/Chrome from offering to autofill an
        # *existing* saved credential here, while still letting them offer to
        # generate/remember a new one, which is the behavior we want.
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="new-password")
        submitted = st.form_submit_button("Sign up", type="primary")

    # Ready to type into without an extra click.
    st.iframe(f"<script>{autofocus_input_js('Username')}</script>", height=1)

    st.markdown('Already have an account? <a href="/signin" target="_self">Sign in</a>', unsafe_allow_html=True)

    if submitted:
        # See pages/signin.py's identical spinner for why, including the
        # minimum-duration padding: without it, a repeated failure (e.g.
        # "username taken") renders identical text in the same spot, and the
        # request round trip is fast enough that the spinner alone flashes
        # by too quickly to actually be seen.
        start = time.monotonic()
        with st.spinner("Signing up..."):
            result = signup_with_auth_service(auth_domain, username, password)
            time.sleep(max(0.0, 0.5 - (time.monotonic() - start)))
        if "error" in result:
            st.error(result["error"])
        else:
            st.session_state["_signup_token"] = result["token"]
            st.session_state["_signup_username"] = result["username"]
            st.rerun()  # so the form is fully gone on the next render, not shown alongside the success message

if "_signup_token" in st.session_state:
    token = st.session_state["_signup_token"]
    username = st.session_state["_signup_username"]
    pair_url = build_pair_url(token, username, auth_domain)

    st.success(f"Signed up as {username}.")
    # See pages/signin.py's identical block for why this is the actual end
    # of the flow now (no more /environments to send you to), and why a
    # real anchor click is what actually fires the casper:// hand-off.
    st.iframe(f"<script>{click_anchor_js(json.dumps(pair_url))}</script>", height=1)
    st.write("Check your computer -- Casper should connect automatically. You can close this tab.")
