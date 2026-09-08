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


def page_header(size=32):
    """The clickable brand+logo every page shows in the upper-left, linking
    back to the home page -- so no page ever needs its own "back home" link.
    A real <a> tag with target="_self" (not st.markdown's `[text](url)`
    syntax) is deliberate: st.markdown's markdown-syntax links default to
    target="_blank" regardless of the URL, and a link that always points at
    a relative "/" is always same-subdomain navigation, which should never
    open a new tab (see casper_app.py's other links for the same rule
    applied to cross-subdomain links, which -- correctly -- do open one).

    Also hides Streamlit's own header bar and three-dot menu (this logo
    link is our own replacement for that navigation surface, so Streamlit's
    isn't needed) and pulls the main content block's default top/left
    padding in, so the logo actually sits flush in the page's top-left
    corner instead of visibly inset from it. Both target-testid selectors
    (older `.block-container` class and the newer `stMainBlockContainer`
    testid) are set for the same rule, since which one is authoritative
    varies by Streamlit version -- belt and suspenders."""
    st.markdown(
        """
        <style>
        [data-testid="stHeader"], [data-testid="stMainMenu"], [data-testid="stToolbar"] {
            display: none !important;
        }
        div.block-container, [data-testid="stMainBlockContainer"] {
            padding-top: 0.5rem !important;
            padding-left: 0.5rem !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<a href="/" target="_self" style="text-decoration:none;color:inherit;'
        f'display:inline-flex;align-items:center;gap:0.5rem;margin-bottom:1rem;">'
        f'{ghost_svg(size)}<span style="font-size:{round(size * 0.6)}px;font-weight:700;">{NAME}</span></a>',
        unsafe_allow_html=True,
    )
