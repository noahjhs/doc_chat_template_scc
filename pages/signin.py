import json
import time

import streamlit as st

from utils.auth import build_pair_url, connected_host_ids, login_with_auth_service, require_app_subdomain
from utils.branding import page_header
from utils.browser_nav import autofocus_input_js, click_anchor_js

# Same reasoning as casper_app.py's set_page_config -- a consistent tab
# identity across the whole flow.
st.set_page_config(page_title="Casper - Sign in", page_icon="👻")
require_app_subdomain()

page_header()
st.title("Sign in")

auth_domain = st.secrets["AUTH_SERVICE_DOMAIN"]

# Once login succeeds, the form disappears entirely (replaced by the
# success state below) rather than staying on screen -- otherwise, if a
# confused user clicks "Sign in" again, that logs in a second time and
# rotates the very token the first attempt already used to pair with.
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
    pair_url = build_pair_url(token, username, auth_domain)

    st.success(f"Signed in as {username}.")
    # There's no post-pairing page to send you to anymore (Environments
    # moved to the harness, a dev tool a real user doesn't have) -- this is
    # the actual end of the flow for a real user today. Fires the
    # casper://pair hand-off via a real anchor click (a custom-scheme
    # anchor dispatches to the OS without navigating this tab away, unlike
    # an http(s) URL -- see click_anchor_js's own docstring for why a
    # direct window.parent.location assignment doesn't reliably work from
    # inside st.iframe's sandbox).
    st.iframe(f"<script>{click_anchor_js(json.dumps(pair_url))}</script>", height=1)

    # Closes the "no product-facing way to confirm pairing worked" gap
    # (previously only checkable via `harness hosts list`, an internal dev
    # tool -- see docs/user-flows/first-time-setup.md's Known Issues).
    # Snapshots which hosts were ALREADY connected before the hand-off
    # above, so a returning user with some other already-connected machine
    # doesn't get a false-positive the instant this page loads -- only a
    # host connecting AFTER the snapshot counts as confirmation this
    # specific pairing worked. A one-shot poll (guarded by
    # _pairing_poll_done so a later, unrelated rerun of this page doesn't
    # re-poll or flicker the result): bounded to a few seconds since this
    # blocks rendering, with a quiet fallback to the original generic
    # message on timeout or any API hiccup -- pairing may well have still
    # worked, this poll just isn't the source of truth for it (harness
    # hosts list / GET /hosts still is).
    if "_pairing_poll_done" not in st.session_state:
        already_connected = connected_host_ids(auth_domain, token) or set()
        confirmed = False
        with st.spinner("Waiting for Casper to connect..."):
            for _ in range(8):  # ~8 checks, 1.5s apart -- about 12s total
                time.sleep(1.5)
                current = connected_host_ids(auth_domain, token)
                if current and current - already_connected:
                    confirmed = True
                    break
        st.session_state["_pairing_poll_done"] = True
        st.session_state["_pairing_confirmed"] = confirmed

    if st.session_state["_pairing_confirmed"]:
        st.success("Casper connected. You can close this tab.")
    else:
        st.write("Check your computer -- Casper should connect automatically. You can close this tab.")
