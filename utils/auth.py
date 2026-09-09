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


def app_subdomain_url():
    """Base URL (with scheme) of this deployment's "app" subdomain -- where
    /signin, /signup, and /chat live. Used by the www-hosted landing/download
    pages to link there across subdomains (see APP_SUBDOMAIN_DOMAIN in
    docker/app-entrypoint.sh) -- pages already served from the app subdomain
    itself (signin.py, signup.py, chat.py) never need this; they link to
    each other with plain relative paths."""
    return _base_url(st.secrets["APP_SUBDOMAIN_DOMAIN"])


def _current_host():
    """The Host header of the current request, hostname only (no port) --
    both www.casperagent.dev/app.casperagent.dev and dev-www/dev-app resolve
    to this exact same Streamlit deployment (see ~/casper-infra's Cloudflare
    Tunnel ingress config, not tracked in this repo), so this is the only
    way a page can tell which of the two it was actually reached through.
    st.context.headers does case-insensitive lookups (confirmed directly --
    the header arrives as "Host", not "host"), so .get("host") is safe."""
    host = st.context.headers.get("host") or ""
    return host.split(":")[0].strip().lower()


def _is_local_host(host):
    return host in ("", "localhost", "127.0.0.1")


def require_www_subdomain():
    """Gate a www-only page (the landing page, /download): st.stop()s with
    a plain "not found" if reached via the app subdomain instead. A no-op
    for localhost (so local dev/testing keeps every page reachable from one
    plain `streamlit run` instance) and if APP_SUBDOMAIN_DOMAIN isn't
    configured at all (fail open rather than block real traffic over a
    config gap)."""
    host = _current_host()
    app_host = st.secrets.get("APP_SUBDOMAIN_DOMAIN", "")
    if _is_local_host(host) or not app_host:
        return
    if host == app_host.split(":")[0].strip().lower():
        st.write("Page not found.")
        st.stop()


def require_app_subdomain():
    """Gate an app-only page (signin, signup, chat): st.stop()s with a
    plain "not found" if reached via the www subdomain (or anything else)
    instead. A no-op for localhost and if APP_SUBDOMAIN_DOMAIN isn't
    configured -- same reasoning as require_www_subdomain()."""
    host = _current_host()
    app_host = st.secrets.get("APP_SUBDOMAIN_DOMAIN", "")
    if _is_local_host(host) or not app_host:
        return
    if host != app_host.split(":")[0].strip().lower():
        st.write("Page not found.")
        st.stop()


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


def build_pair_url(token, username):
    """The casper://pair URL pages/signin.py and pages/signup.py fire on
    success -- the OS hands this to the user's already-running (or
    freshly-launched) Casper daemon as an Apple Event (see
    agent/internal/urlscheme), which is what actually completes pairing.
    Replaces the old localhost-callback redirect entirely: the daemon no
    longer opens a browser tab itself, so there's nothing at localhost to
    hand a token back to."""
    return f"casper://pair?token={quote(token, safe='')}&username={quote(username, safe='')}"


def get_presence(auth_domain, token):
    """GET /presence. Returns {"connected": bool, "local_agent_url":
    str|None, "workspace": str|None}, or None on network failure -- callers
    must handle both, same posture as verify_token_with_auth_service."""
    try:
        response = requests.get(
            f"{_base_url(auth_domain)}/presence",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


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
    """Gate a page on a token minted by signing in/up (see pages/signin.py,
    pages/signup.py) -- the only sign-up/sign-in surface for the product.
    Verifies once per browser session (cached in session_state) rather than
    per query-param value -- the page strips local_agent_token from the
    visible URL after reading it, so on later reruns it may no longer be
    present in st.query_params at all; checking session_state first,
    independent of what the current query params say, is what keeps that
    safe. st.stop()s with a 'sign in' message if there's no token, it's
    invalid, or the auth service is unreachable. Returns the signed-in
    username; the verified token itself is cached separately in
    session_state["_authenticated_token"] (see current_token()) for callers
    that need it -- e.g. pages/chat.py's presence lookup."""
    if st.session_state.get("_authenticated_username"):
        return st.session_state["_authenticated_username"]

    token = st.query_params.get("local_agent_token", "")
    if not token:
        st.info(f"Sign in to use {NAME}.")
        st.stop()

    result = verify_token_with_auth_service(_auth_domain(), token)
    if result is None:
        st.error("Couldn't reach the auth service right now. Try again shortly.")
        st.stop()
    if not result.get("valid"):
        st.error("This sign-in link is no longer valid. Sign in again to get a fresh one.")
        st.stop()

    st.session_state["_authenticated_username"] = result["username"]
    st.session_state["_authenticated_token"] = token
    return result["username"]


def current_token():
    """The verified token cached by require_agent_session() -- only
    meaningful after that's already been called (and passed) earlier in the
    same script run."""
    return st.session_state.get("_authenticated_token", "")
