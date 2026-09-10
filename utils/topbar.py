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
    sized larger (doubled, per an explicit ask) and with zero padding of its
    own -- a separate CSS block/key prefix rather than reusing theirs, since
    the two are meant to look visibly different in scale.

    The whole row is pinned via position: fixed at the browser viewport's
    top-right corner (not just flush against whatever padding the page's
    own content column happens to have) -- "send them all the way up into
    the corner" specifically asked for the actual corner, not just less
    padding within the normal content flow. This floats above the page
    (z-index far higher than anything Streamlit itself uses) rather than
    occupying normal document space, in the same header strip
    utils/sidebar.py's re-shown stHeader/stToolbar already leaves visually
    empty on the left (where the sidebar's own collapse/expand toggle
    lives) -- this is the mirror-image control on the right.

    [data-testid="stPopoverBody"] is the popover's floating panel -- it
    carries its own generous default min-width (sized for typical popover
    content like forms/date pickers), which is what left visible empty
    space to the right of "Profile"/"Security" before this; collapsed down
    to fit its actual content instead."""
    st.html(
        """
        <style>
        .st-key-topbar_row {
            position: fixed;
            top: 0;
            right: 0;
            z-index: 999999;
        }
        .st-key-topbar_row [data-testid="stHorizontalBlock"] {
            gap: 0 !important;
        }
        .st-key-topbar_row [data-testid="stColumn"] {
            flex: 0 0 auto !important;
            width: auto !important;
            min-width: 0 !important;
            padding: 0 !important;
        }
        .st-key-topbar_settings_button button, .st-key-topbar_signout_button button {
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
            padding: 0 !important;
            margin: 0 !important;
            min-height: 0 !important;
            color: inherit !important;
            font-size: 3.4rem !important;
            line-height: 3.4rem !important;
        }
        .st-key-topbar_settings_button button:hover, .st-key-topbar_signout_button button:hover,
        .st-key-topbar_settings_button button:focus, .st-key-topbar_signout_button button:focus,
        .st-key-topbar_settings_button button:active, .st-key-topbar_signout_button button:active {
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
            color: inherit !important;
        }
        [data-testid="stPopoverBody"] {
            min-width: 0 !important;
            width: fit-content !important;
            max-width: none !important;
        }
        </style>
        """
    )


def render_settings_menu():
    """The Settings gear + sign-out icon, pinned to the top-right corner of
    the browser viewport. Call once, right after require_agent_session(),
    before any other main-content output."""
    _render_topbar_css()
    with st.container(key="topbar_row"):
        gear_col, signout_col = st.columns([1, 1])
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
