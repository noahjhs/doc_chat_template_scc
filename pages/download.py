import os

import streamlit as st

from utils.auth import app_subdomain_url, require_www_subdomain
from utils.branding import NAME, page_header

st.set_page_config(page_title="Casper - Download", page_icon="👻")
require_www_subdomain()

DIST_DIR = os.path.join(os.path.dirname(__file__), "..", "dist")
DOWNLOADS = {
    "🍎 Download for Mac": os.path.join(DIST_DIR, "Casper-macos.zip"),
    "🪟 Download for Windows": os.path.join(DIST_DIR, "Casper-windows.zip"),
}

page_header()
st.title(f"Download {NAME}")

st.markdown(
    f"**{NAME}** runs as a small background app on your own machine — download it, "
    "open it once, and sign in from the website whenever you want to start a session."
)

# Sign in lives on the app subdomain -- a different origin from this page,
# so it opens in a new tab (see casper_app.py's matching link for why).
signin_url = f"{app_subdomain_url()}/signin"
st.markdown(
    "**Getting started:**\n"
    f"1. Download {NAME} for your platform below.\n"
    f"2. Open it once — {NAME} appears as a small icon in your status bar (look for 👻) "
    "and quietly waits there. It's confined to your home directory by default, so it "
    "can never touch anything outside it — consider adding it to your Login Items so "
    "it's always ready.\n"
    f'3. <a href="{signin_url}" target="_blank" rel="noopener">Sign in</a> from this '
    "website — it connects automatically, no extra steps.",
    unsafe_allow_html=True,
)

st.warning(
    f"**macOS will confirm the first time you open {NAME}**, since it's a fresh "
    'download from the internet — just click **Open** on the dialog. This '
    "first-run check only happens once."
)

for label, path in DOWNLOADS.items():
    if os.path.exists(path):
        with open(path, "rb") as f:
            st.download_button(label, f, file_name=os.path.basename(path))
    else:
        st.caption(f"{label}: not built yet.")
