import streamlit as st

from utils.branding import NAME, TAGLINE, ghost_svg

# The browser tab's actual title (not just the on-page st.title() heading) --
# every Casper page sets this the same way for a consistent identity across
# the whole flow.
st.set_page_config(page_title="Casper", page_icon="👻")

col1, col2 = st.columns([1, 3])
with col1:
    st.markdown(ghost_svg(100), unsafe_allow_html=True)
with col2:
    st.title(NAME)
    st.caption(TAGLINE)

st.write(
    f"**{NAME}** is an AI assistant with real tools. It can search the web, run code, "
    "and generate images out of the box — and once you connect the small desktop app, "
    "it can work directly in a folder on your own machine too: reading files, running "
    "git commands, searching your codebase. Everything it touches is confined to a "
    "workspace you choose, and every action it takes is shown back to you."
)

st.markdown(
    "[📥 Download Casper](/download) · [Sign in](/signin) · [Sign up](/signup)",
    unsafe_allow_html=True,
)
