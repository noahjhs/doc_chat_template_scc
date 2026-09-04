import os

import streamlit as st

from utils.auth import require_login

authenticator = require_login()

with st.sidebar:
    authenticator.logout()
    st.caption(f"Signed in as {st.session_state.get('name')}")

DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")
DOWNLOADS = {
    "🍎 Download for Mac": os.path.join(DIST_DIR, "DocChatControlAPI-macos.zip"),
    "🪟 Download for Windows": os.path.join(DIST_DIR, "DocChatControlAPI-windows.zip"),
}

st.title("📥 Get the local agent")
st.write(
    "Download and run this on your own machine to let the assistant run a "
    "few read-only git commands (and `cd`) against your local repo. "
    "It'll open the chat app in a new browser tab once it's running — "
    "connect it from there using the URL and API key it prints."
)

for label, path in DOWNLOADS.items():
    if os.path.exists(path):
        with open(path, "rb") as f:
            st.download_button(label, f, file_name=os.path.basename(path))
    else:
        st.caption(f"{label}: not built yet.")
