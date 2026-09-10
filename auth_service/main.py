import base64
import binascii
import concurrent.futures
import hashlib
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import bcrypt
import requests
from db import get_db, init_db
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from models import (
    AuthResponse,
    EnvironmentCreateRequest,
    EnvironmentInfo,
    EnvironmentListResponse,
    EnvironmentRenameRequest,
    HostInfo,
    HostListResponse,
    HostPairRequest,
    HostPairResponse,
    HostPresenceReport,
    HostRenameRequest,
    HostVerifyResponse,
    LoginRequest,
    ProfileInfo,
    ProfileUpdateRequest,
    RevokeResponse,
    SignOutAllResponse,
    SignupRequest,
    StorageDeleteResponse,
    StorageDownloadResponse,
    StorageFileInfo,
    StorageListResponse,
    StorageUploadRequest,
    VerifyResponse,
)

app = FastAPI()
init_db()


@app.exception_handler(RequestValidationError)
def _validation_error_handler(request: Request, exc: RequestValidationError):
    """Flattens FastAPI/pydantic's default {"detail": [{"msg": ..., ...}]}
    shape into a single human-readable string, consistent with every other
    error response in this service (e.g. HostPairResponse's 409, or
    _safe_filename's 400) -- and what utils/auth.py's _error_detail (and
    thus every settings-page st.error(result["error"])) actually expects.
    Added for ProfileUpdateRequest's email/phone validators, but applies
    service-wide since every 422 here benefits the same way."""
    first = exc.errors()[0]
    message = first.get("msg", "Invalid request.")
    # pydantic v2 prefixes a validator's own raised ValueError text with
    # "Value error, " in the formatted message -- stripped so e.g. "Enter a
    # valid email address." isn't shown as "Value error, Enter a valid
    # email address."
    message = message.removeprefix("Value error, ")
    return JSONResponse(status_code=422, content={"detail": message})


# --- Rate limiting -----------------------------------------------------
# A plain in-memory sliding window: fine at this service's scale, and the
# SQLite-on-a-single-disk constraint already rules out running more than one
# instance, so there's no multi-process state-sharing problem to solve.
RATE_LIMIT = 10  # attempts
RATE_WINDOW = 60  # seconds
_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def check_rate_limit(ip: str):
    now = time.time()
    with _attempts_lock:
        recent = [t for t in _attempts.get(ip, []) if now - t < RATE_WINDOW]
        if len(recent) >= RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many attempts. Try again in a minute.")
        recent.append(now)
        _attempts[ip] = recent


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --- Helpers -------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.execute(
        "UPDATE users SET token_hash = ?, token_created_at = ? WHERE id = ?",
        (hash_token(token), datetime.now(timezone.utc).isoformat(), user_id),
    )
    return token


# --- Endpoints -------------------------------------------------------------
@app.post("/signup", response_model=AuthResponse, status_code=201)
def signup(body: SignupRequest, request: Request):
    check_rate_limit(client_ip(request))
    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (body.username,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Username already taken.")

        cur = db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (body.username, hash_password(body.password)),
        )
        token = issue_token(db, cur.lastrowid)
        return AuthResponse(username=body.username, token=token)


@app.post("/login", response_model=AuthResponse)
def login(body: LoginRequest, request: Request):
    check_rate_limit(client_ip(request))
    with get_db() as db:
        row = db.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
            (body.username,),
        ).fetchone()
        if not row or not check_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password.")

        token = issue_token(db, row["id"])
        return AuthResponse(username=row["username"], token=token)


@app.post("/verify", response_model=VerifyResponse)
def verify(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return VerifyResponse(valid=False)
    with get_db() as db:
        row = db.execute(
            "SELECT username FROM users WHERE token_hash = ?", (hash_token(token),)
        ).fetchone()
        if not row:
            return VerifyResponse(valid=False)
        return VerifyResponse(valid=True, username=row["username"])


@app.post("/revoke", response_model=RevokeResponse)
def revoke(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if token:
        with get_db() as db:
            db.execute("UPDATE users SET token_hash = NULL WHERE token_hash = ?", (hash_token(token),))
    return RevokeResponse(revoked=True)


def _resolve_user_id(db, authorization: str) -> int | None:
    """Bearer (browser session) token -> user id, or None if missing/invalid
    -- the same resolution /verify does inline, factored out here (not
    shared with /verify itself) so other endpoints can reuse it without
    touching /verify's response shape or behavior."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    row = db.execute("SELECT id FROM users WHERE token_hash = ?", (hash_token(token),)).fetchone()
    return row["id"] if row else None


# --- Host attachment (runtime state, deliberately not persisted) ---------
# routing_key -> {user_id, host_id, device_token_hash, command_key,
#                 local_agent_url, workspace}
# Who is *currently* attached to a host, with what live credentials, and
# where it's currently reachable -- kept here rather than in SQLite for the
# same reason the rate limiter above is: fine at this service's scale, and
# the SQLite-on-a-single-disk constraint already rules out running more
# than one instance, so there's no multi-process state-sharing problem to
# solve. Keeping "who's attached" and "what's the live credential" in the
# same place also means they can never drift out of sync with each other.
# A server restart clears every attachment; each affected daemon 401s on
# its next presence report and self-heals to idle, waiting to be re-paired
# (see agent/internal/config/presence.go's ReportPresence) -- a reconnect,
# not a correctness problem.
_attached: dict[str, dict] = {}
_attached_lock = threading.Lock()


class HostAlreadyAttachedError(Exception):
    """Raised by _pair_host when routing_key is currently attached to a
    different user -- reject, never preempt (a host being in active use by
    someone else should never be silently disrupted by another account's
    pairing attempt)."""


def _resolve_attached(authorization: str) -> dict | None:
    """Bearer device_token -> its live attachment record (plus routing_key),
    or None. Linear scan under the lock -- matches the rate limiter's own
    unindexed-dict posture at this scale."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    token_hash = hash_token(token)
    with _attached_lock:
        for routing_key, info in _attached.items():
            if info["device_token_hash"] == token_hash:
                return {"routing_key": routing_key, **info}
    return None


def _pair_host(db, user_id: int, routing_key: str, hostname: str | None, requested_label: str | None):
    """Idempotent for the *same* user re-pairing the *same* routing_key
    (e.g. reconnecting after a restart) -- rotates credentials in place,
    no duplicate rows, existing label untouched. Raises
    HostAlreadyAttachedError if routing_key is currently attached to a
    different user. Returns (host_id, device_token, command_key, label,
    is_new_to_this_user)."""
    with _attached_lock:
        existing = _attached.get(routing_key)
        if existing and existing["user_id"] != user_id:
            raise HostAlreadyAttachedError()

        row = db.execute("SELECT id FROM hosts WHERE routing_key = ?", (routing_key,)).fetchone()
        if row is None:
            host_id = db.execute(
                "INSERT INTO hosts (routing_key, hostname) VALUES (?, ?)", (routing_key, hostname)
            ).lastrowid
        else:
            host_id = row["id"]
            if hostname:
                db.execute("UPDATE hosts SET hostname = ? WHERE id = ?", (hostname, host_id))

        existing_uh = db.execute(
            "SELECT label FROM user_hosts WHERE user_id = ? AND host_id = ?", (user_id, host_id)
        ).fetchone()
        is_new = existing_uh is None
        label = existing_uh["label"] if existing_uh else (requested_label or hostname or "Unnamed host")
        if is_new:
            db.execute(
                "INSERT INTO user_hosts (user_id, host_id, label) VALUES (?, ?, ?)", (user_id, host_id, label)
            )

        device_token = secrets.token_urlsafe(32)
        command_key = secrets.token_urlsafe(32)
        _attached[routing_key] = {
            "user_id": user_id,
            "host_id": host_id,
            "device_token_hash": hash_token(device_token),
            "command_key": command_key,
            "local_agent_url": None,
            "workspace": [],
        }
    return host_id, device_token, command_key, label, is_new


def _assign_default_environment(db, user_id: int, host_id: int):
    """Called only for a host the user has never paired before. Zero
    existing Environments -> create "Default" and add it. Exactly one ->
    add it there too (extends the zero-click case to the common
    single-Environment user). Two or more -> leave unassigned; guessing
    which of several user-organized Environments a brand-new host belongs
    in seems worse than one explicit click on the Environments page."""
    envs = db.execute("SELECT id FROM environments WHERE user_id = ?", (user_id,)).fetchall()
    if not envs:
        target_env_id = db.execute(
            "INSERT INTO environments (user_id, name) VALUES (?, ?)", (user_id, "Default")
        ).lastrowid
    elif len(envs) == 1:
        target_env_id = envs[0]["id"]
    else:
        return
    db.execute(
        "INSERT OR IGNORE INTO environment_hosts (environment_id, host_id) VALUES (?, ?)",
        (target_env_id, host_id),
    )


def _user_owns_host(db, user_id: int, host_id: int) -> bool:
    return (
        db.execute("SELECT 1 FROM user_hosts WHERE user_id = ? AND host_id = ?", (user_id, host_id)).fetchone()
        is not None
    )


def _user_owns_environment(db, user_id: int, environment_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM environments WHERE id = ? AND user_id = ?", (environment_id, user_id)
        ).fetchone()
        is not None
    )


def _environment_info(db, environment_id: int, name: str) -> EnvironmentInfo:
    host_ids = [
        r["host_id"]
        for r in db.execute(
            "SELECT host_id FROM environment_hosts WHERE environment_id = ?", (environment_id,)
        ).fetchall()
    ]
    return EnvironmentInfo(id=environment_id, name=name, host_ids=host_ids)


# --- Host pairing / presence (daemon-initiated, device_token-gated) ------
@app.post("/hosts/pair", response_model=HostPairResponse, status_code=201)
def pair_host(body: HostPairRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        try:
            host_id, device_token, command_key, label, is_new = _pair_host(
                db, user_id, body.routing_key, body.hostname, body.label
            )
        except HostAlreadyAttachedError:
            raise HTTPException(status_code=409, detail="This host is currently attached to another account.")
        if is_new:
            _assign_default_environment(db, user_id, host_id)
        return HostPairResponse(host_id=host_id, device_token=device_token, command_key=command_key, label=label)


@app.post("/hosts/verify", response_model=HostVerifyResponse)
def verify_host(authorization: str = Header(default="")):
    return HostVerifyResponse(valid=_resolve_attached(authorization) is not None)


@app.post("/hosts/unpair", response_model=RevokeResponse)
def unpair_host(authorization: str = Header(default="")):
    """Self-service: a daemon deregisters itself (tray "Sign out"). Only
    clears the in-memory attachment -- user_hosts/environment_hosts are
    untouched, so the host stays remembered and re-pairing it later won't
    re-prompt for a label."""
    attached = _resolve_attached(authorization)
    if attached is not None:
        with _attached_lock:
            _attached.pop(attached["routing_key"], None)
    return RevokeResponse(revoked=True)


@app.post("/hosts/presence", response_model=HostInfo)
def report_host_presence(body: HostPresenceReport, authorization: str = Header(default="")):
    attached = _resolve_attached(authorization)
    if attached is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")
    with _attached_lock:
        _attached[attached["routing_key"]]["local_agent_url"] = body.local_agent_url
        _attached[attached["routing_key"]]["workspace"] = body.workspace
    return HostInfo(
        host_id=attached["host_id"], label="", connected=True,
        local_agent_url=body.local_agent_url, workspace=body.workspace,
    )


@app.delete("/hosts/presence", response_model=HostInfo)
def clear_host_presence(authorization: str = Header(default="")):
    """Toggling the relay off is not signing out -- clears only reachability,
    the attachment (and its credentials) stays valid."""
    attached = _resolve_attached(authorization)
    if attached is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")
    with _attached_lock:
        _attached[attached["routing_key"]]["local_agent_url"] = None
        _attached[attached["routing_key"]]["workspace"] = []
    return HostInfo(host_id=attached["host_id"], label="", connected=False)


# --- Host/Environment management (browser-initiated, session-token-gated) -
@app.get("/hosts", response_model=HostListResponse)
def list_hosts(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        rows = db.execute(
            """
            SELECT h.id, h.routing_key, h.hostname, uh.label
            FROM user_hosts uh JOIN hosts h ON h.id = uh.host_id
            WHERE uh.user_id = ? ORDER BY uh.label COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()
        env_rows = db.execute(
            """
            SELECT eh.host_id, eh.environment_id FROM environment_hosts eh
            JOIN environments e ON e.id = eh.environment_id WHERE e.user_id = ?
            """,
            (user_id,),
        ).fetchall()
    envs_by_host: dict[int, list[int]] = {}
    for r in env_rows:
        envs_by_host.setdefault(r["host_id"], []).append(r["environment_id"])
    with _attached_lock:
        attached_snapshot = {k: dict(v) for k, v in _attached.items()}
    hosts_out = []
    for r in rows:
        att = attached_snapshot.get(r["routing_key"])
        mine = att is not None and att["user_id"] == user_id
        connected = mine and att["local_agent_url"] is not None
        hosts_out.append(
            HostInfo(
                host_id=r["id"], label=r["label"], hostname=r["hostname"], connected=connected,
                local_agent_url=att["local_agent_url"] if connected else None,
                workspace=att["workspace"] if connected else [],
                command_key=att["command_key"] if mine else None,
                environment_ids=envs_by_host.get(r["id"], []),
            )
        )
    return HostListResponse(hosts=hosts_out)


@app.patch("/hosts/{host_id}", response_model=RevokeResponse)
def rename_host(host_id: int, body: HostRenameRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_host(db, user_id, host_id):
            raise HTTPException(status_code=404, detail="Host not found.")
        db.execute(
            "UPDATE user_hosts SET label = ? WHERE user_id = ? AND host_id = ?", (body.label, user_id, host_id)
        )
    return RevokeResponse(revoked=True)


@app.delete("/hosts/{host_id}", response_model=RevokeResponse)
def forget_host(host_id: int, authorization: str = Header(default="")):
    """Removes this host from the caller's own remembered list and every
    Environment it belonged to. If currently attached to this same caller,
    also detaches it (forgetting a host you're using ends the session) --
    never touches another user's attachment to the same physical host."""
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_host(db, user_id, host_id):
            raise HTTPException(status_code=404, detail="Host not found.")
        row = db.execute("SELECT routing_key FROM hosts WHERE id = ?", (host_id,)).fetchone()
        db.execute(
            "DELETE FROM environment_hosts WHERE host_id = ? AND environment_id IN "
            "(SELECT id FROM environments WHERE user_id = ?)",
            (host_id, user_id),
        )
        db.execute("DELETE FROM user_hosts WHERE user_id = ? AND host_id = ?", (user_id, host_id))
    if row is not None:
        with _attached_lock:
            attached = _attached.get(row["routing_key"])
            if attached and attached["user_id"] == user_id:
                _attached.pop(row["routing_key"], None)
    return RevokeResponse(revoked=True)


@app.get("/environments", response_model=EnvironmentListResponse)
def list_environments(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        rows = db.execute(
            "SELECT id, name FROM environments WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,)
        ).fetchall()
        return EnvironmentListResponse(environments=[_environment_info(db, r["id"], r["name"]) for r in rows])


@app.post("/environments", response_model=EnvironmentInfo, status_code=201)
def create_environment(body: EnvironmentCreateRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        existing = db.execute(
            "SELECT id FROM environments WHERE user_id = ? AND name = ? COLLATE NOCASE", (user_id, body.name)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="An Environment with that name already exists.")
        environment_id = db.execute(
            "INSERT INTO environments (user_id, name) VALUES (?, ?)", (user_id, body.name)
        ).lastrowid
        return _environment_info(db, environment_id, body.name)


@app.patch("/environments/{environment_id}", response_model=EnvironmentInfo)
def rename_environment(environment_id: int, body: EnvironmentRenameRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_environment(db, user_id, environment_id):
            raise HTTPException(status_code=404, detail="Environment not found.")
        db.execute("UPDATE environments SET name = ? WHERE id = ?", (body.name, environment_id))
        return _environment_info(db, environment_id, body.name)


@app.delete("/environments/{environment_id}", response_model=RevokeResponse)
def delete_environment(environment_id: int, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_environment(db, user_id, environment_id):
            raise HTTPException(status_code=404, detail="Environment not found.")
        db.execute("DELETE FROM environment_hosts WHERE environment_id = ?", (environment_id,))
        db.execute("DELETE FROM environments WHERE id = ?", (environment_id,))
    return RevokeResponse(revoked=True)


@app.put("/environments/{environment_id}/hosts/{host_id}", response_model=EnvironmentInfo)
def add_host_to_environment(environment_id: int, host_id: int, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        env = db.execute(
            "SELECT id, name FROM environments WHERE id = ? AND user_id = ?", (environment_id, user_id)
        ).fetchone()
        if env is None or not _user_owns_host(db, user_id, host_id):
            raise HTTPException(status_code=404, detail="Environment or host not found.")
        db.execute(
            "INSERT OR IGNORE INTO environment_hosts (environment_id, host_id) VALUES (?, ?)",
            (environment_id, host_id),
        )
        return _environment_info(db, environment_id, env["name"])


@app.delete("/environments/{environment_id}/hosts/{host_id}", response_model=EnvironmentInfo)
def remove_host_from_environment(environment_id: int, host_id: int, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        env = db.execute(
            "SELECT id, name FROM environments WHERE id = ? AND user_id = ?", (environment_id, user_id)
        ).fetchone()
        if env is None:
            raise HTTPException(status_code=404, detail="Environment not found.")
        db.execute(
            "DELETE FROM environment_hosts WHERE environment_id = ? AND host_id = ?", (environment_id, host_id)
        )
        return _environment_info(db, environment_id, env["name"])


def _shutdown_hosts_best_effort(targets: list[tuple[str, str]]):
    def _one(url: str, command_key: str):
        try:
            requests.post(f"{url.rstrip('/')}/api/shutdown", headers={"X-API-Key": command_key}, timeout=5)
        except requests.RequestException:
            pass  # best-effort -- the daemon may already be unreachable

    if not targets:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda t: _one(*t), targets))


@app.post("/hosts/signout-all", response_model=SignOutAllResponse)
def signout_all_hosts(authorization: str = Header(default="")):
    """The website's "Sign out" button. Best-effort pushes /api/shutdown to
    every host currently attached to this user, then detaches all of them.
    user_hosts/environment_hosts are untouched -- signing out of the website
    detaches sessions, it does not make the user forget their hosts or
    Environments."""
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
    with _attached_lock:
        mine = [(rk, info) for rk, info in _attached.items() if info["user_id"] == user_id]
        for routing_key, _ in mine:
            _attached.pop(routing_key, None)
    targets = [(info["local_agent_url"], info["command_key"]) for _, info in mine if info["local_agent_url"]]
    _shutdown_hosts_best_effort(targets)
    return SignOutAllResponse(signed_out_hosts=len(mine))


# --- Per-user file storage (transfer feature) -----------------------------
# Filesystem-as-database, deliberately: no new SQL table, just files under
# STORAGE_ROOT/<user_id>/<filename> -- size and upload time both already
# come for free from a plain os.stat(), and there's no migration mechanism
# in this codebase worth standing up a table for (see db.py's own note).
# STORAGE_ROOT defaults to a local dev path; deployment sets it to a
# subdirectory of the same persisted volume AUTH_DB_PATH already lives on
# (see docker-entrypoint.sh / .env.example).
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", os.path.join(os.path.dirname(__file__), "storage"))
STORAGE_CAP_BYTES = 1024 * 1024 * 1024  # 1GB per user


def _user_storage_dir(user_id: int) -> str:
    path = os.path.join(STORAGE_ROOT, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def _safe_filename(filename: str) -> str:
    """Only a bare filename is ever accepted -- os.path.basename strips any
    directory components, so "../../etc/passwd" collapses to just
    "passwd", the same confined-no-escape posture the local agent's own
    RootDir confinement already applies to command paths."""
    name = os.path.basename(filename.strip())
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    return name


def _dir_total_bytes(directory: str) -> int:
    return sum(entry.stat().st_size for entry in os.scandir(directory) if entry.is_file())


def _storage_file_info(entry_path: str, filename: str) -> StorageFileInfo:
    stat = os.stat(entry_path)
    return StorageFileInfo(
        filename=filename,
        size=stat.st_size,
        uploaded_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )


@app.get("/storage", response_model=StorageListResponse)
def list_storage(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
    directory = _user_storage_dir(user_id)
    files = [
        _storage_file_info(entry.path, entry.name) for entry in os.scandir(directory) if entry.is_file()
    ]
    files.sort(key=lambda f: f.filename.lower())
    return StorageListResponse(
        files=files, total_bytes=sum(f.size for f in files), cap_bytes=STORAGE_CAP_BYTES
    )


@app.post("/storage", response_model=StorageFileInfo, status_code=201)
def upload_storage(body: StorageUploadRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
    filename = _safe_filename(body.filename)
    try:
        data = base64.b64decode(body.content, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Invalid base64 content.")

    directory = _user_storage_dir(user_id)
    target = os.path.join(directory, filename)
    existing_size = os.path.getsize(target) if os.path.exists(target) else 0
    if _dir_total_bytes(directory) - existing_size + len(data) > STORAGE_CAP_BYTES:
        raise HTTPException(
            status_code=413, detail=f"Storage cap exceeded ({STORAGE_CAP_BYTES // (1024 * 1024)}MB per user)."
        )

    with open(target, "wb") as f:
        f.write(data)
    return _storage_file_info(target, filename)


@app.get("/storage/{filename}", response_model=StorageDownloadResponse)
def download_storage(filename: str, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
    filename = _safe_filename(filename)
    target = os.path.join(_user_storage_dir(user_id), filename)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found.")
    with open(target, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    return StorageDownloadResponse(filename=filename, content=content)


@app.delete("/storage/{filename}", response_model=StorageDeleteResponse)
def delete_storage(filename: str, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
    filename = _safe_filename(filename)
    target = os.path.join(_user_storage_dir(user_id), filename)
    if os.path.isfile(target):
        os.remove(target)
    return StorageDeleteResponse(deleted=True)


# --- Profile (pages/settings_profile.py, pages/settings_security.py) ----
# Notification contact info + the "allow chat to configure..." checkboxes
# are plain per-user preferences -- persisted here, but not enforced
# anywhere yet (nothing in pages/chat.py's tool-calling loop reads
# allow_configure_* today). "Command sets"/"Apps"/"Local agents" don't
# correspond to any existing modeled concept in this codebase the way
# Hosts/Environments do (see auth_service/db.py's hosts/environments
# tables) -- wiring real enforcement for those three needs its own design
# pass first, so this deliberately stops at "saved preference" for now.


def _get_or_create_profile(db, user_id: int) -> ProfileInfo:
    row = db.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        db.execute("INSERT INTO user_profile (user_id) VALUES (?)", (user_id,))
        row = db.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
    return ProfileInfo(
        email=row["email"],
        email_notifications_enabled=bool(row["email_notifications_enabled"]),
        sms_number=row["sms_number"],
        sms_notifications_enabled=bool(row["sms_notifications_enabled"]),
        allow_configure_command_sets=bool(row["allow_configure_command_sets"]),
        allow_configure_apps=bool(row["allow_configure_apps"]),
        allow_configure_hosts=bool(row["allow_configure_hosts"]),
        allow_configure_environments=bool(row["allow_configure_environments"]),
        allow_configure_local_agents=bool(row["allow_configure_local_agents"]),
    )


@app.get("/profile", response_model=ProfileInfo)
def get_profile(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        return _get_or_create_profile(db, user_id)


@app.patch("/profile", response_model=ProfileInfo)
def update_profile(body: ProfileUpdateRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        _get_or_create_profile(db, user_id)  # ensures the row exists before the UPDATE below
        updates = body.model_dump(exclude_unset=True)
        if updates:
            db.execute(
                f"UPDATE user_profile SET {', '.join(f'{k} = ?' for k in updates)} WHERE user_id = ?",
                (*updates.values(), user_id),
            )
        return _get_or_create_profile(db, user_id)
