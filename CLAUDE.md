# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt
streamlit run casper_app.py
```

```bash
cd auth_service && pip install -r requirements.txt && uvicorn main:app --port 8100
```

```bash
cd harness && pip install -e . && harness --help
```

```bash
pytest tests/
```

## Architecture

Branded as **Casper** ("your friendly ghost.", see `utils/branding.py`). Mid-refactor (2026-09): the security-rule engine and its tool-calling orchestration used to live in a Streamlit chat page; that logic has moved server-side into `auth_service`, tested via a new CLI+library harness instead of the GUI. The Streamlit app is now a thin, simple-CRUD front end (accounts, hosts, Environments, settings) with no chat/policy-authoring UI of its own.

- **`casper_app.py`** + **`pages/`** — a multipage Streamlit app: the public landing/download page, sign-up/sign-in (the only account-creation surface, gated by `require_app_subdomain()`/`require_www_subdomain()` in `utils/auth.py`), Hosts & Environments management, and account settings. No chat page and no policy-authoring page live here anymore — see `harness/` below.
- **`agent/`** — the Go daemon ("Casper"), built via `build/build_go_macos.sh`. Runs on a user's own machine: pairs via a `casper://pair` URL handed off from sign-in/sign-up, reports its live workspace (addressable directories) and reachability to `auth_service`, and enforces every rule server-authoritatively regardless of what tier a caller already decided. Fully supersedes the old Python/PyInstaller `casper_tool.py` (deleted) on macOS; no Windows build pipeline exists yet even though the agent's own source has Windows-specific files.
- **`auth_service/`** — an independently-deployed FastAPI service. Owns user accounts, hosts, Environments, and Policy Layers (named, ordered rule lists — see `db.py`'s schema and `models.py`'s `Pattern`/`PolicyLayerRuleCreateRequest` for the whitelist/blacklist/tier schema). `policy.py` is the canonical policy-matching engine (mirrors the Go daemon's own matcher); `POST /policies/eval` evaluates a hypothetical call against an ad hoc composition of layers, no daemon involved. `conversations.py` + `POST /conversations/step` hold the actual OpenAI tool-calling loop (own copy of the matching logic for the live tier decision, no streaming, host connection details resolved server-side, a `mock` flag that fakes daemon/storage dispatch only).
- **`relay/`** — a separate Go tunneling service (not the agent, not auth_service).
- **`harness/`** — a standalone package (own `pyproject.toml`) driving `auth_service`'s HTTP API directly: `harness/client.py` (plain functions, no CLI dependency) plus a Typer CLI (`harness/cli.py`). The primary way the security-rule engine gets tested and iterated on now — `harness eval` (pure rule matching) → `harness call-tool [--mock]` (one live/mocked tool call) → `harness chat [--mock]` (a real conversational turn). Also does YAML policy-layer authoring (`harness policy apply <file.yaml>`).

The OpenAI API key and the auth service's domain (`AUTH_SERVICE_DOMAIN`) are read from `.streamlit/secrets.toml` via `st.secrets` in the Streamlit app; `auth_service` reads `OPENAI_API_KEY` from its own process environment (shared via `.env`/`env_file` in `docker-compose.yml`) since it now makes its own OpenAI calls.
