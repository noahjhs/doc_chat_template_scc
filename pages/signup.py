import json
import time

import streamlit as st

from utils.auth import build_pairing_redirect_url, signup_with_auth_service
from utils.browser_nav import autofocus_input_js, click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- a consistent tab
# identity across the whole flow.
st.set_page_config(page_title="Casper", page_icon="👻")

st.title("Sign up")

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

# Once signup succeeds, the form disappears entirely (replaced by the
# redirect below) rather than staying on screen -- otherwise, if the
# redirect is slow or a browser doesn't act on a dynamically-inserted
# meta-refresh tag, a confused user could click "Sign up" again, which
# would rotate the very token the first attempt is still waiting to use
# (and, for signup specifically, fail outright with "username taken").
if "_signup_redirect_url" not in st.session_state:
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

    st.markdown(
        "Already have an account? "
        f'<a href="/signin?callback_port={callback_port}&nonce={nonce}" target="_self">Sign in</a>',
        unsafe_allow_html=True,
    )

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
            st.session_state["_signup_redirect_url"] = build_pairing_redirect_url(
                callback_port, nonce, result["token"], result["username"]
            )
            st.rerun()  # so the form is fully gone on the next render, not shown alongside the success message

if "_signup_redirect_url" in st.session_state:
    redirect_url = st.session_state["_signup_redirect_url"]
    st.success("Signed up.")
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
