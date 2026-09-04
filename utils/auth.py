import streamlit as st
import streamlit_authenticator as stauth


def build_authenticator():
    # Not cached: Authenticate() creates a cookie-manager widget component
    # internally, and Streamlit disallows widget commands in cached functions.
    auth_config = st.secrets["auth"]
    credentials = {
        "usernames": {
            username: dict(fields)
            for username, fields in auth_config["credentials"]["usernames"].items()
        }
    }
    return stauth.Authenticate(
        credentials=credentials,
        cookie_name=auth_config["cookie"]["name"],
        cookie_key=auth_config["cookie"]["key"],
        cookie_expiry_days=auth_config["cookie"]["expiry_days"],
    )


def require_login():
    """Gate the current page behind login — shared by every page, so logging
    in once (session state + cookie) covers all of them. Stops the script if
    not authenticated; returns the authenticator so the caller can render a
    logout button."""
    authenticator = build_authenticator()
    authenticator.login()

    if st.session_state.get("authentication_status") is False:
        st.error("Username/password is incorrect")
        st.stop()
    elif st.session_state.get("authentication_status") is None:
        st.warning("Please enter your username and password")
        st.stop()

    return authenticator
