import streamlit as st

from utils.auth import app_subdomain_url, require_www_subdomain
from utils.branding import NAME, TAGLINE, page_header

# The browser tab's actual title (not just the on-page st.title() heading) --
# every Casper page sets this the same way for a consistent identity across
# the whole flow.
st.set_page_config(page_title="Casper - Home", page_icon="👻")
require_www_subdomain()

page_header(size=100)
st.caption(TAGLINE)

st.write(
    f"**{NAME}** is an AI assistant with real tools. It can search the web, run code, "
    "and generate images out of the box — and once you connect the small desktop app, "
    "it can work directly in a folder on your own machine too: reading files, running "
    "git commands, searching your codebase. Everything it touches is confined to a "
    "workspace you choose, and every action it takes is shown back to you."
)

# Download stays on this same subdomain (same-tab, plain relative link).
# Sign in/up live on the app subdomain instead (see utils/auth.py's
# app_subdomain_url()) -- a genuinely different origin, so those open in a
# new tab. Real <a> tags throughout, not st.markdown's `[text](url)` syntax
# -- the latter defaults to target="_blank" regardless of the URL, which
# would open the same-subdomain Download link in a new tab too (it should
# never), so every in-app link here sets target explicitly instead.
app_url = app_subdomain_url()
st.markdown(
    '<a href="/download" target="_self">📥 Download Casper</a> · '
    f'<a href="{app_url}/signin" target="_blank" rel="noopener">Sign in</a> · '
    f'<a href="{app_url}/signup" target="_blank" rel="noopener">Sign up</a>',
    unsafe_allow_html=True,
)
