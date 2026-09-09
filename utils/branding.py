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
    needed), and trims the sidebar's default top padding down to a small,
    non-zero amount. Call on every page, exactly once.

    Deliberately does NOT touch the main content block's own padding --
    an earlier attempt zeroed it out globally to pull the home link toward
    the edge, which pulled *all* page content flush to the edges along
    with it (confirmed directly: forms, titles, everything shifted). The
    main content's padding is left at Streamlit's own default now;
    page_header() instead gives the home link its own small offset via
    position:fixed, independent of whatever the content's padding is (see
    its docstring)."""
    st.markdown(
        """
        <style>
        [data-testid="stHeader"], [data-testid="stMainMenu"], [data-testid="stToolbar"] {
            display: none !important;
        }
        [data-testid="stSidebar"] div.block-container,
        [data-testid="stSidebarUserContent"],
        [data-testid="stSidebarContent"] {
            padding-top: 1rem !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def home_link_html(size=32):
    """HTML for the clickable brand+logo linking back to the home page --
    shared by page_header() (rendered in the main content area, wrapped to
    float near the corner -- see its docstring) and pages/chat.py (rendered
    directly in its sidebar's own normal flow instead, alongside its other
    real, persistent controls). A real <a> tag with target="_self" (not
    st.markdown's `[text](url)` syntax) is deliberate: st.markdown's
    markdown-syntax links default to target="_blank" regardless of the
    URL, and a link that always points at a relative "/" is always
    same-subdomain navigation, which should never open a new tab (see
    casper_app.py's other links for the same rule applied to cross-
    subdomain links, which -- correctly -- do open one)."""
    return (
        f'<a href="/" target="_self" style="text-decoration:none;color:inherit;'
        f'display:inline-flex;align-items:center;gap:0.5rem;">'
        f'{ghost_svg(size)}<span style="font-size:{round(size * 0.6)}px;font-weight:700;">{NAME}</span></a>'
    )


def page_header(size=32):
    """hide_streamlit_chrome() + the home link, floated a small, fixed
    distance from the page's top-left corner -- independent of the main
    content block's own padding (position:fixed's top/left is relative to
    the viewport, not affected by any ancestor's padding), so the link can
    sit close to the edge while everything else on the page keeps
    Streamlit's normal padding. What every page except pages/chat.py wants
    (chat.py puts the link in its sidebar's own flow instead, so it calls
    hide_streamlit_chrome() and home_link_html() directly rather than
    this)."""
    hide_streamlit_chrome()
    st.markdown(
        f'<div style="position:fixed;top:0.5rem;left:0.75rem;z-index:1000;">'
        f'{home_link_html(size)}</div>',
        unsafe_allow_html=True,
    )
