from urllib.parse import quote

import requests
import streamlit as st

from utils.branding import NAME


def _base_url(domain):
    domain = domain.strip().strip("/")
    scheme = "http" if domain.startswith("localhost") or domain.startswith("127.0.0.1") else "https"
    return f"{scheme}://{domain}"


def _auth_domain():
    return st.secrets["AUTH_SERVICE_DOMAIN"]


def signup_with_auth_service(auth_domain, username, password):
    """POST /signup. Returns {"username", "token"} on success, or
    {"error": str} -- a taken username, a validation failure, and a network
    problem all collapse to the same shape so the signup page can just
    st.error() whatever comes back."""
    try:
        response = requests.post(
            f"{_base_url(auth_domain)}/signup",
            json={"username": username, "password": password},
            timeout=10,
        )
    except requests.RequestException as e:
        return {"error": f"Couldn't reach the auth service: {e}"}
    if response.status_code == 201:
        return response.json()
    return {"error": _error_detail(response, "Sign up failed.")}


def login_with_auth_service(auth_domain, username, password):
    """POST /login. Same {"username", "token"} / {"error": str} shape."""
    try:
        response = requests.post(
            f"{_base_url(auth_domain)}/login",
            json={"username": username, "password": password},
            timeout=10,
        )
    except requests.RequestException as e:
        return {"error": f"Couldn't reach the auth service: {e}"}
    if response.status_code == 200:
        return response.json()
    return {"error": _error_detail(response, "Log in failed.")}


def _error_detail(response, fallback):
    try:
        return response.json().get("detail", fallback)
    except ValueError:
        return fallback


def verify_token_with_auth_service(auth_domain, token):
    """POST /verify. Returns {"valid": bool, "username": str|None}, or None
    on network failure -- callers must handle both."""
    try:
        response = requests.post(
            f"{_base_url(auth_domain)}/verify",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def build_pairing_redirect_url(callback_port, nonce, token, username):
    """The URL pages/signin.py and pages/signup.py redirect the browser to
    on success -- casper_tool.py's local loopback listener, which is what
    actually hands the token back to the waiting process."""
    return (
        f"http://localhost:{callback_port}/?nonce={quote(nonce, safe='')}"
        f"&token={quote(token, safe='')}&username={quote(username, safe='')}"
    )


def revoke_token_with_auth_service(auth_domain, token):
    """POST /revoke. Best-effort -- never raises."""
    try:
        requests.post(
            f"{_base_url(auth_domain)}/revoke",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
    except requests.RequestException:
        pass


def require_agent_session():
    """Gate a page on a token minted by a locally-run casper_tool.py --
    the only sign-up/sign-in surface for the product. Verifies once per
    browser session (cached in session_state) rather than per query-param
    value -- the page strips local_agent_token from the visible URL after
    reading it, so on later reruns it may no longer be present in
    st.query_params at all; checking session_state first, independent of
    what the current query params say, is what keeps that safe. st.stop()s
    with a 'run Casper' message if there's no token, it's invalid, or the
    auth service is unreachable. Returns the signed-in username."""
    if st.session_state.get("_authenticated_username"):
        return st.session_state["_authenticated_username"]

    token = st.query_params.get("local_agent_token", "")
    if not token:
        st.info(f"Run {NAME} to sign in.")
        st.stop()

    result = verify_token_with_auth_service(_auth_domain(), token)
    if result is None:
        st.error("Couldn't reach the auth service right now. Try again shortly.")
        st.stop()
    if not result.get("valid"):
        st.error(f"This sign-in link is no longer valid. Run {NAME} again to get a fresh one.")
        st.stop()

    st.session_state["_authenticated_username"] = result["username"]
    return result["username"]
