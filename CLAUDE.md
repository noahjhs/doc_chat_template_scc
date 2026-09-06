# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt
streamlit run casper_app.py
```

## Architecture

Multipage Streamlit app, branded as **Casper** ("your friendly ghost.", see `utils/branding.py`). `casper_app.py` is the public landing/download page for the local agent binary. `pages/chat.py` is the actual OpenAI Responses API chat assistant, gated by `require_agent_session()` (`utils/auth.py`) — access requires a token minted by running the downloaded `casper_tool.py` binary (built as `Casper`), which is the only sign-up/sign-in surface (see `pages/signin.py` and `pages/signup.py` for the browser-side half of that flow). `casper_tool.py` is a separate FastAPI tool a user runs on their own machine to give the assistant a confined, allowlisted set of local filesystem commands. `auth_service/` is a third, independently-deployed FastAPI service owning user accounts.

The OpenAI API key and the auth service's domain (`AUTH_SERVICE_DOMAIN`) are read from `.streamlit/secrets.toml` via `st.secrets`.
