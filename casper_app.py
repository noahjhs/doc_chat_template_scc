import os

import streamlit as st

from utils.branding import NAME, TAGLINE, ghost_svg

# The browser tab's actual title (not just the on-page st.title() heading) --
# every Casper page sets this the same way for a consistent identity across
# the whole flow.
st.set_page_config(page_title="Casper", page_icon="👻")

DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")
DOWNLOADS = {
    "🍎 Download for Mac": os.path.join(DIST_DIR, "Casper-macos.zip"),
    "🪟 Download for Windows": os.path.join(DIST_DIR, "Casper-windows.zip"),
}

col1, col2 = st.columns([1, 3])
with col1:
    st.markdown(ghost_svg(100), unsafe_allow_html=True)
with col2:
    st.title(NAME)
    st.caption(TAGLINE)

st.write(
    f"**{NAME}** connects your AI assistant to your own machine — safely, "
    "with your permission, and only doing what you allow."
)
st.markdown(
    "**To get started:**\n"
    "1. Download Casper Desktop\n"
    "2. Place the app in a workspace location of your choice\n"
    "3. Run it!"
)

for label, path in DOWNLOADS.items():
    if os.path.exists(path):
        with open(path, "rb") as f:
            st.download_button(label, f, file_name=os.path.basename(path))
    else:
        st.caption(f"{label}: not built yet.")
