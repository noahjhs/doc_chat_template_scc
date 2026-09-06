# Doc Chat Auth Service

A small standalone FastAPI service that owns user accounts for Casper's
sign-up/sign-in flow. Deployed as its own Render web service, separate from
the main Streamlit app and from `casper_tool.py`.

## Endpoints

- `POST /signup` `{username, password}` → `201 {username, token}`, or `409`
  if the username is taken.
- `POST /login` `{username, password}` → `200 {username, token}` (this
  **rotates** the account's token — logging in elsewhere invalidates any
  previously issued token for that account), or `401` on bad credentials.
- `POST /verify` — `Authorization: Bearer <token>` → `200 {valid, username}`
  (always `200`, even for an invalid token — `valid: false` distinguishes
  "not logged in" from a network failure on the caller's side).
- `POST /revoke` — `Authorization: Bearer <token>` → `200 {revoked: true}`,
  idempotent. Only affects future `/verify` calls; it does not itself stop
  a running `casper_tool.py` process (that's `casper_tool.py`'s own
  `/api/shutdown`).

`/signup` and `/login` are rate-limited per IP (in-memory, resets on
restart) since they're public password endpoints.

## Running locally

```
pip install -r requirements.txt
AUTH_DB_PATH=./users.db uvicorn main:app --port 8100
```

## Deploying on Render

New Web Service, **Root Directory = `auth_service`**:
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Env var `AUTH_DB_PATH` pointed at a path under an attached **persistent
  disk** (e.g. `/var/data/users.db`).

**Important**: Render's default filesystem is ephemeral across redeploys.
A persistent disk must be explicitly attached to this service (Render
dashboard → this service → Disks), which requires a paid plan tier and
also means this service cannot be horizontally scaled while using
file-based SQLite (disks aren't shared across replicas). After deploying,
verify the disk actually works by signing up a test account, triggering a
real **redeploy** (not just a restart), and confirming that account can
still log in — a restart alone can appear fine even on ephemeral storage.
