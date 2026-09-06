import os

import streamlit as st

from utils.branding import NAME, TAGLINE, ghost_svg
from utils.browser_nav import click_anchor_js

# The browser tab's actual title (not just the on-page st.title() heading) --
# every Casper page sets this the same way so casper_tool.py's
# bring-tab-into-view AppleScript can find any of them by matching "Casper"
# in the tab title.
st.set_page_config(page_title="Casper", page_icon="👻")

# Casper's own default port (CONTROL_TOOL_PORT's default in casper_tool.py).
# Landing here has no prior connection to know a customized port, so this
# assumes the default -- if someone's changed it, auto-discovery below just
# doesn't find anything, same as any other default-assuming behavior in
# this app (e.g. --agent-server's default target).
DEFAULT_LOCAL_PORT = 8000

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

# Background: if Casper is (re)launched on this same machine while this
# landing page happens to be open, jump to its sign-in page automatically
# instead of Casper opening a separate new tab for it. Harmless the rest of
# the time -- this just gets connection-refused silently. Polls immediately
# (not just on the first setInterval tick, ~750ms later) and every 750ms
# after -- casper_tool.py's own grace period (how long it waits for exactly
# this poll before giving up and opening a new tab itself) is sized against
# this interval, so tightening one without the other reopens either a
# missed handoff or a needless pause; see its comment.
st.iframe(
    f"""<script>
function poll() {{
    fetch("http://localhost:{DEFAULT_LOCAL_PORT}/api/pairing-info")
        .then(function(r) {{ return r.ok ? r.json() : null; }})
        .then(function(data) {{
            if (data && data.signin_url) {{
                {click_anchor_js("data.signin_url")}
            }}
        }})
        .catch(function() {{}});
}}
poll();
setInterval(poll, 750);
</script>""",
    height=1,
)
