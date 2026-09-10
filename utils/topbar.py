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

    Sizing via zoom rather than font-size (font-size confirmed to have no
    effect on a :material/ icon glyph's rendered size) or transform (also
    tried -- confirmed directly to be the wrong tool here: the popover's
    floating panel is positioned off the *reference div wrapping* the
    button, and transform: scale is a paint-only effect that doesn't change
    that div's actual layout size, so the panel opened positioned against
    the button's small pre-scale footprint -- squarely in the middle of
    the now visually-much-bigger icon, i.e. "partly obscures the gear").
    zoom actually resizes the element's real layout box, so anything
    measuring it (the popover's own positioning math included) sees the
    true, bigger size and lays out correctly around it.

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

    width: fit-content + box-sizing: border-box on the row itself turned
    out to matter just as much as the position rules above -- without an
    explicit width, this container was inheriting Streamlit's normal
    block-level width (effectively 100% of the viewport, since it's
    position: fixed with right: 0 already pinning one edge). With right:0
    *and* width:100%, left resolves to 0 too -- the box silently spanned
    the whole viewport, so its content (flex's default justify-content:
    flex-start) rendered flush against that box's *left* edge instead of
    its right one, which is what actually looked like "pinned to the
    left" (confirmed directly: the fixed positioning itself was working
    the whole time, right:0 and left:0 were just both simultaneously
    true). box-sizing: border-box on top of that is why the small padding
    was pushing it further left, off-screen: content-box (the default)
    adds padding *on top of* a 100%-wide box, so the border-box was
    actually wider than the viewport by twice the padding, with the
    overflow landing on the left. Sizing the row to fit-content removes
    the ambiguity entirely -- there's no longer any "the box is wider than
    its visible content" case for either bug to hide in.

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
            width: fit-content !important;
            box-sizing: border-box !important;
            z-index: 999999 !important;
            padding: 1rem 1.25rem !important;
        }
        .st-key-topbar_row [data-testid="stHorizontalBlock"] {
            gap: 1.5rem !important;
            justify-content: flex-end !important;
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
        .st-key-topbar_settings_button button, .st-key-topbar_signout_button button {
            zoom: 2 !important;
        }
        /* The popover trigger's own dropdown chevron (rendered next to our
        gear icon, inside the same button) -- marked aria-hidden="true" in
        Streamlit's own markup, which doubles as a clean CSS hook to hide
        it; asked for directly ("the dropdown arrow indicator is
        overlapping the signout icon"). */
        .st-key-topbar_settings_button button [aria-hidden="true"] {
            display: none !important;
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
            with st.popover("", icon=":material/settings:", key="topbar_settings_button"):
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
