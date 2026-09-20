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

Branded as **Casper** ("your friendly ghost.", see `utils/branding.py`). Mid-refactor (2026-09): the security-rule engine, its tool-calling orchestration, and (as of the most recent round) the whole authenticated account-management surface used to live in Streamlit pages; all of that has moved server-side into `auth_service`, tested/driven via a new CLI+library harness instead of a GUI.

- **`casper_app.py`** + **`pages/download.py`** (public, unauthenticated, `www` subdomain, gated by `require_www_subdomain()`) + **`pages/signin.py`**/**`pages/signup.py`** (`app` subdomain, gated by `require_app_subdomain()`) — what's left of the Streamlit app: a real end user downloads the agent binary, signs in or signs up, and that fires the `casper://pair` hand-off to their local daemon (see `utils/auth.py`'s `build_pair_url`/`login_with_auth_service`/`signup_with_auth_service`, `utils/browser_nav.py`'s `click_anchor_js` for how a custom-scheme URL actually dispatches from inside Streamlit's sandboxed iframe). That's the entire remaining surface, deliberately trimmed from what it used to be — no chat, no policy-authoring, no Environments/Settings UI, and there's still no product-facing way for a user to confirm pairing actually succeeded (see `docs/user-flows/first-time-setup.md`'s Known Issues) — account/host/Environment/profile *management* (as opposed to the one-time act of signing in) all moved to the harness (see below).
- **`agent/`** — the Go daemon ("Casper"), built via `build/build_go_macos.sh`. Runs on a user's own machine, confined to a single fixed directory (their home directory by default, computed once at startup — see `commands.Handler.HomeRoot`): pairs via a `casper://pair` URL handed off from sign-in/sign-up, reports its own confined directory and reachability to `auth_service` via presence, and enforces every rule server-authoritatively regardless of what tier a caller already decided — including a rule's own `cwd` constraint and any per-argument `path_resolution` (`.`/`$PATH`/`MANPATH`), which only the daemon itself can resolve accurately (real filesystem/environment access). One daemon can be paired to more than one Casper account at once (`server.Server` holds one `commands.Handler` per paired identity, keyed by that account's own `command_key`) — each account's policy enforcement stays fully isolated even though every identity shares the same underlying machine/filesystem. Fully supersedes the old Python/PyInstaller `casper_tool.py` (deleted) on macOS; no Windows build pipeline exists yet even though the agent's own source has Windows-specific files.
- **`auth_service/`** — an independently-deployed FastAPI service. Owns user accounts, hosts, Environments, and Policy Layers (named, ordered rule lists — see `db.py`'s schema and `models.py`'s `Pattern`/`PolicyLayerRuleCreateRequest` for the whitelist/blacklist/tier/`path_resolution`/`cwd` schema). `host_pairings` is keyed by `(routing_key, user_id)`, not `routing_key` alone — a physical host can be paired to several accounts simultaneously, each with its own independent credentials and policy layers. `policy.py` is the canonical policy-matching engine (mirrors the Go daemon's own matcher, though its own `.`/`$PATH`/`MANPATH` resolution is only ever a best-effort preview — see its module docstring); `POST /policies/eval` evaluates a hypothetical call against an ad hoc composition of layers, no daemon involved. `conversations.py` + `POST /conversations/step` hold the actual OpenAI tool-calling loop (own copy of the matching logic for the live tier decision, no streaming, host connection details resolved server-side, a `mock` flag that fakes daemon/storage dispatch only).
- **`relay/`** — a separate Go tunneling service (not the agent, not auth_service).
- **`harness/`** — a standalone package (own `pyproject.toml`) driving `auth_service`'s HTTP API directly: `harness/client.py` (plain functions, no CLI dependency) plus a Typer CLI (`harness/cli.py`). The primary way the security-rule engine gets tested and iterated on now — `harness eval` (pure rule matching) → `harness call-tool [--mock]` (one live/mocked tool call) → `harness chat [--mock]` (a real conversational turn). Also does YAML policy-layer authoring (`harness policy apply <file.yaml>`); account/host/Environment/profile management (`harness signup`/`login`, `harness pair`, `harness hosts`/`environment`/`profile` — full parity with what `pages/environments.py`/`settings_*.py` used to do); scripting a REAL daemon's pairing (`harness pair-daemon`, macOS-only, manual-verification-only — fires the same `casper://pair` Apple Event a browser sign-in click would); and checking/answering pending "ask"-tier approvals (`harness approvals list`/`respond` — a durable, per-user queue any of the account's own sessions can resolve, not a designated "attended" machine — see `auth_service`'s `pending_approvals` table).

The OpenAI API key and the auth service's domain (`AUTH_SERVICE_DOMAIN`) are read from `.streamlit/secrets.toml` via `st.secrets` in the Streamlit app; `auth_service` reads `OPENAI_API_KEY` from its own process environment (shared via `.env`/`env_file` in `docker-compose.yml`) since it now makes its own OpenAI calls.
