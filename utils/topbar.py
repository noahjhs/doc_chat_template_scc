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
    doubled in size and with zero padding of its own -- a separate CSS
    block/key prefix rather than reusing theirs, since the two are meant to
    look visibly different in scale.

    Sizing via transform: scale(2) rather than font-size -- confirmed
    directly (a previous font-size attempt visibly had no effect at all)
    that the rendered :material/ icon glyph's own size isn't governed by
    the surrounding button's font-size the way ordinary text is; a
    transform scales whatever's actually there regardless of how its size
    is otherwise determined, so it isn't hostage to that. transform-origin
    top right on the rightmost (sign-out) icon and top on the gear next to
    it keeps the visible top-right corner anchored in place as they grow,
    instead of growing symmetrically outward from center and drifting
    further from the actual corner.

    The whole row is pinned via position: fixed at the browser viewport's
    top-right corner (not just flush against whatever padding the page's
    own content column happens to have) -- every declaration is !important
    and left is pinned to auto explicitly, since a previous attempt without
    !important on the position rules landed top-left instead of top-right
    (some Streamlit-authored rule with equal-or-greater specificity was
    evidently winning that fight). Floats above the page (z-index far
    higher than anything Streamlit itself uses) rather than occupying
    normal document space, in the same header strip utils/sidebar.py's
    re-shown stHeader/stToolbar already leaves visually empty on the left
    (where the sidebar's own collapse/expand toggle lives) -- this is the
    mirror-image control on the right. A small padding on the row itself
    (not the individual buttons, which are transform-scaled and would just
    scale their own padding too) insets the icons a bit from the literal
    corner, per an explicit ask.

    [data-testid="stPopoverBody"] is the popover's floating panel -- it
    carries its own generous default min-width (sized for typical popover
    content like forms/date pickers), which is what left visible empty
    space to the right of "Profile"/"Security" before this; collapsed down
    to fit its actual content instead."""
    st.html(
        """
        <style>
        .st-key-topbar_row {
            position: fixed !important;
            top: 0 !important;
            right: 0 !important;
            left: auto !important;
            z-index: 999999 !important;
            padding: 0.4rem 0.6rem !important;
        }
        .st-key-topbar_row [data-testid="stHorizontalBlock"] {
            gap: 0.75rem !important;
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
        }
        .st-key-topbar_settings_button button {
            transform: scale(2) !important;
            transform-origin: top !important;
        }
        .st-key-topbar_signout_button button {
            transform: scale(2) !important;
            transform-origin: top right !important;
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
