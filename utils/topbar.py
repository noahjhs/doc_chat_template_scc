import streamlit as st

from utils.sidebar import start_sign_out

# Rendered at the top of every real in-app page's *main* content area (not
# the sidebar -- see utils/sidebar.py's render_sidebar for that), right
# after require_agent_session(): a Settings gear (a popover -- Streamlit has
# no native anchored dropdown menu, but a popover is the closest built-in
# equivalent) linking to the Profile/Security pages, plus the sign-out icon
# next to it. Sign-out moved here from the sidebar specifically so it sits
# beside Settings instead.


def _render_topbar_css():
    """Same "strip default button chrome down to just the icon glyph"
    treatment as utils/sidebar.py's Environment-gear/refresh icons, but
    sized larger ("slightly larger" than those was requested for this one)
    -- a separate CSS block/key prefix rather than reusing theirs, since the
    two are meant to look visibly different in scale."""
    st.html(
        """
        <style>
        .st-key-topbar_settings_button button, .st-key-topbar_signout_button button {
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
            padding: 0.1rem 0.35rem !important;
            min-height: 0 !important;
            color: inherit !important;
            font-size: 1.7rem !important;
            line-height: 1.7rem !important;
        }
        .st-key-topbar_settings_button button:hover, .st-key-topbar_signout_button button:hover,
        .st-key-topbar_settings_button button:focus, .st-key-topbar_signout_button button:focus,
        .st-key-topbar_settings_button button:active, .st-key-topbar_signout_button button:active {
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
            color: inherit !important;
        }
        [class*="st-key-topbar_row"] [data-testid="stColumn"] {
            display: flex;
            align-items: center;
            justify-content: flex-end;
        }
        </style>
        """
    )


def render_settings_menu():
    """The Settings gear + sign-out icon, right-aligned at the top of the
    page's main content. Call once, right after require_agent_session(),
    before any other main-content output."""
    _render_topbar_css()
    with st.container(key="topbar_row"):
        _spacer, gear_col, signout_col = st.columns([10, 1, 1])
        with gear_col:
            with st.popover("", icon=":material/settings:", key="topbar_settings_button", help="Settings"):
                st.page_link("pages/settings_profile.py", label="Profile", icon=":material/person:")
                st.page_link("pages/settings_security.py", label="Security", icon=":material/lock:")
        with signout_col:
            st.button(
                "",
                icon=":material/logout:",
                key="topbar_signout_button",
                help="Sign out",
                on_click=start_sign_out,
            )
