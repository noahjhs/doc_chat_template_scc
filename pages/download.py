import os

import streamlit as st

from utils.branding import NAME, ghost_svg

st.set_page_config(page_title="Casper", page_icon="👻")

DIST_DIR = os.path.join(os.path.dirname(__file__), "..", "dist")
DOWNLOADS = {
    "🍎 Download for Mac": os.path.join(DIST_DIR, "Casper-macos.zip"),
    "🪟 Download for Windows": os.path.join(DIST_DIR, "Casper-windows.zip"),
}

col1, col2 = st.columns([1, 6])
with col1:
    st.markdown(ghost_svg(72), unsafe_allow_html=True)
with col2:
    st.title(f"Download {NAME}")

st.markdown('<a href="/">← Back home</a>', unsafe_allow_html=True)

st.markdown(
    f"**{NAME}** runs as a small background app on your own machine — download it, "
    "open it once, and sign in from the website whenever you want to start a session."
)

st.markdown(
    "**Getting started:**\n"
    f"1. Download {NAME} for your platform below.\n"
    "2. Move it to wherever you'd like its workspace to live — the folder it opens in "
    "becomes the confined directory it can work in (drag it into a project folder, "
    "or `~/Applications` if you'd rather point it at a workspace later).\n"
    f"3. Open it once. {NAME} appears as a small icon in your status bar (look for 👻) "
    "and quietly waits there — consider adding it to your Login Items so it's always "
    "ready.\n"
    "4. [Sign in](/signin) from this website — it connects automatically, no extra steps."
)

for label, path in DOWNLOADS.items():
    if os.path.exists(path):
        with open(path, "rb") as f:
            st.download_button(label, f, file_name=os.path.basename(path))
    else:
        st.caption(f"{label}: not built yet.")
