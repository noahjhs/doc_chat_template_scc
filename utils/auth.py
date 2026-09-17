import streamlit as st


def _base_url(domain):
    domain = domain.strip().strip("/")
    scheme = "http" if domain.startswith("localhost") or domain.startswith("127.0.0.1") else "https"
    return f"{scheme}://{domain}"


def app_subdomain_url():
    """Base URL (with scheme) of this deployment's "app" subdomain. Used by
    the www-hosted landing/download pages to link there across subdomains
    (see APP_SUBDOMAIN_DOMAIN in docker/app-entrypoint.sh) -- the app
    subdomain itself no longer hosts anything but the harness now drives
    (sign-in/sign-up/Environments/Settings moved there; see harness/), so
    this link mainly points a real end user at instructions rather than a
    live page today."""
    return _base_url(st.secrets["APP_SUBDOMAIN_DOMAIN"])


def www_subdomain_url():
    """Base URL (with scheme) of this deployment's "www" subdomain -- where
    the public landing/download page (casper_app.py) lives. The reverse of
    app_subdomain_url(), for utils/branding.py's brand/logo link (a bare
    relative "/" resolved against a non-www origin serves no page, so this
    always needs the explicit www URL). There's no separate stored
    www-domain secret (require_www_subdomain() only ever needs to know a
    host *isn't* the app host, never its own value), so this derives it
    from APP_SUBDOMAIN_DOMAIN by swapping "app" for "www" in its first
    label only: "app.X" -> "www.X", "dev-app.X" -> "dev-www.X" -- matching
    the actual naming convention this deployment uses. Falls back to "/" if
    APP_SUBDOMAIN_DOMAIN isn't configured at all (e.g. local dev), where
    every page already lives at the same origin anyway."""
    app_domain = st.secrets.get("APP_SUBDOMAIN_DOMAIN", "")
    if not app_domain:
        return "/"
    labels = app_domain.split(".")
    labels[0] = labels[0].replace("app", "www", 1)
    return _base_url(".".join(labels))


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
