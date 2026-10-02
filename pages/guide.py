"""Chat with Casper's guide -- the onboarding agent for people who don't
have their own (casper_service/guide.py). After sign-in; the same
conversation continues in Telegram once it's linked."""

import requests
import streamlit as st

from utils.auth import _base_url, login_with_auth_service, require_app_subdomain
from utils.branding import page_header

st.set_page_config(page_title="Casper - Guide", page_icon="👻")
require_app_subdomain()
page_header()
st.title("Casper's guide")

auth_domain = st.secrets["AUTH_SERVICE_DOMAIN"]
token = st.session_state.get("_login_token") or st.session_state.get("_guide_token")

if not token:
    st.write("Sign in to chat with Casper's guide. It'll help you keep your files safe with friends, or offer space to them.")
    with st.form("guide_signin"):
        username = st.text_input("Username", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        if st.form_submit_button("Sign in", type="primary"):
            try:
                result = login_with_auth_service(auth_domain, username, password)
                st.session_state["_guide_token"] = result["token"]
                st.rerun()
            except Exception as e:
                st.error(str(e))
    st.markdown("[New to Casper? Create an account](/signup)")
    st.stop()

headers = {"Authorization": f"Bearer {token}"}
base = _base_url(auth_domain)

try:
    history = requests.get(f"{base}/guide/history", headers=headers, timeout=20).json().get("messages", [])
except requests.RequestException:
    history = []

if not history:
    with st.chat_message("assistant", avatar="👻"):
        st.write("Hi! I'm Casper's guide. Would you like to **keep your files safe** on a friend's computer, "
                 "or **offer space** on yours for friends? If a friend sent you an invite code, paste it here.")
for m in history:
    with st.chat_message(m["role"], avatar="👻" if m["role"] == "assistant" else None):
        st.write(m["text"])

if text := st.chat_input("Message Casper's guide"):
    with st.chat_message("user"):
        st.write(text)
    with st.chat_message("assistant", avatar="👻"):
        with st.spinner("Thinking..."):
            try:
                r = requests.post(f"{base}/guide/message", json={"text": text}, headers=headers, timeout=180)
                reply = r.json().get("reply") if r.ok else r.json().get("detail", "Something went wrong.")
            except requests.RequestException:
                reply = "I couldn't reach Casper just now -- please try again."
        st.write(reply)
