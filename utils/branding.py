import streamlit as st

NAME = "Casper"
TAGLINE = "your friendly ghost."

# A simple, original ghost illustration (not any existing character's
# design) -- plain SVG markup, safe to render via st.markdown(unsafe_allow_html=True)
# since it's static vector markup, not a <script> tag (which that mechanism
# can't reliably execute -- see pages/chat.py's notes on st.iframe).
GHOST_SVG = """
<svg width="{size}" height="{size}" viewBox="0 0 120 140" xmlns="http://www.w3.org/2000/svg">
  <path d="M60 8 C31 8 8 31 8 62 L8 122
           C8 130 17 135 24 129 L35 118
           C39 114 45 114 49 118 L55 125
           C58 129 63 129 66 125 L72 118
           C76 114 82 114 86 118 L97 129
           C104 135 113 130 113 122 L113 62
           C113 31 90 8 60 8 Z"
        fill="#F3F7FF" stroke="#B9CDEE" stroke-width="2.5"/>
  <circle cx="42" cy="60" r="7.5" fill="#3A4A63"/>
  <circle cx="79" cy="60" r="7.5" fill="#3A4A63"/>
  <path d="M44 84 Q60 97 77 84" stroke="#3A4A63" stroke-width="4" fill="none" stroke-linecap="round"/>
</svg>
"""


def ghost_svg(size=120):
    return GHOST_SVG.format(size=size)


def hide_streamlit_chrome():
    """Hides Streamlit's own header bar and three-dot menu (home_link_html()
    is our own replacement for that navigation surface, so Streamlit's isn't
    needed) and pulls the main content block's default padding way in, so
    content -- in particular the home link -- sits flush against the page's
    edges instead of visibly inset from them. Call on every page, exactly
    once. Several selectors targeted redundantly (older `.block-container`
    class, newer `stMainBlockContainer`/`stAppViewContainer` testids, and
    the vertical-block wrapper Streamlit actually renders top-level content
    into) since which one carries the real padding varies by Streamlit
    version and turned out not to be fully covered by `.block-container`
    alone -- confirmed directly (a first attempt at just that selector left
    visible left-padding)."""
    st.markdown(
        """
        <style>
        [data-testid="stHeader"], [data-testid="stMainMenu"], [data-testid="stToolbar"] {
            display: none !important;
        }
        [data-testid="stAppViewContainer"],
        [data-testid="stMainBlockContainer"],
        div.block-container,
        [data-testid="stMainBlockContainer"] > div,
        [data-testid="stVerticalBlock"] {
            padding-top: 0 !important;
            padding-left: 0 !important;
            margin-left: 0 !important;
            max-width: 100% !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def home_link_html(size=32):
    """HTML for the clickable brand+logo linking back to the home page --
    shared by page_header() (rendered inline, in the main content area) and
    pages/chat.py (rendered in the sidebar instead -- see its own call
    site). A real <a> tag with target="_self" (not st.markdown's
    `[text](url)` syntax) is deliberate: st.markdown's markdown-syntax
    links default to target="_blank" regardless of the URL, and a link
    that always points at a relative "/" is always same-subdomain
    navigation, which should never open a new tab (see casper_app.py's
    other links for the same rule applied to cross-subdomain links, which
    -- correctly -- do open one)."""
    return (
        f'<a href="/" target="_self" style="text-decoration:none;color:inherit;'
        f'display:inline-flex;align-items:center;gap:0.5rem;margin:0 0 1rem 0.25rem;">'
        f'{ghost_svg(size)}<span style="font-size:{round(size * 0.6)}px;font-weight:700;">{NAME}</span></a>'
    )


def page_header(size=32):
    """hide_streamlit_chrome() + the home link rendered inline in the main
    content area -- what every page except pages/chat.py wants (chat.py
    puts the link in its sidebar instead, so it calls hide_streamlit_chrome()
    and home_link_html() directly rather than this)."""
    hide_streamlit_chrome()
    st.markdown(home_link_html(size), unsafe_allow_html=True)
