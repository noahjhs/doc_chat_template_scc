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
    "2. Move it to wherever you'd like its workspace to live — the folder it opens in "
    "becomes the confined directory it can work in (drag it into a project folder, "
    "or `~/Applications` if you'd rather point it at a workspace later).\n"
    f"3. Open it once. {NAME} appears as a small icon in your status bar (look for 👻) "
    "and quietly waits there — consider adding it to your Login Items so it's always "
    "ready.\n"
    f'4. <a href="{signin_url}" target="_blank" rel="noopener">Sign in</a> from this '
    "website — it connects automatically, no extra steps.",
    unsafe_allow_html=True,
)

st.warning(
    f"**macOS will refuse to open {NAME} the first time**, with a message like "
    '"Apple could not verify Casper.app is free of malware." This is expected -- '
    f"{NAME} isn't yet notarized by Apple (a paid process we haven't done), and "
    "there's no \"Open Anyway\" button for this particular warning on current "
    "macOS versions. To open it anyway:\n"
    "1. Open **Terminal** (Spotlight search for it).\n"
    "2. Type `xattr -cr ` (note the trailing space), then drag `Casper.app` from "
    "Finder into the Terminal window -- this fills in its path -- and press Return.\n"
    f"3. Open {NAME} again; it'll launch normally from now on."
)

for label, path in DOWNLOADS.items():
    if os.path.exists(path):
        with open(path, "rb") as f:
            st.download_button(label, f, file_name=os.path.basename(path))
    else:
        st.caption(f"{label}: not built yet.")
