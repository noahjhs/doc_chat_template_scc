import base64
import binascii
import concurrent.futures
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone

import bcrypt
import requests
from db import get_db, init_db
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from policy import compose_policy, match_policy
import conversations
from models import (
    AttendedHostInfo,
    AttendedHostUpdateRequest,
    AuthResponse,
    ConversationPendingApproval,
    ConversationStepRequest,
    ConversationStepResponse,
    EnvironmentCreateRequest,
    EnvironmentInfo,
    EnvironmentListResponse,
    EnvironmentRenameRequest,
    HostInfo,
    HostListResponse,
    HostPairRequest,
    HostPairResponse,
    HostPolicyLayerInfo,
    HostPolicyLayerListResponse,
    HostPresenceReport,
    HostRenameRequest,
    HostVerifyResponse,
    LoginRequest,
    PendingApprovalCreateRequest,
    PendingApprovalCreateResponse,
    PendingApprovalDecisionRequest,
    PendingApprovalInfo,
    PendingApprovalListResponse,
    PolicyEvalRequest,
    PolicyEvalResponse,
    PolicyLayerCreateRequest,
    PolicyLayerInfo,
    PolicyLayerListResponse,
    PolicyLayerRenameRequest,
    PolicyLayerRuleCreateRequest,
    PolicyLayerRuleInfo,
    PolicyLayerRuleReorderRequest,
    PolicyLayerRuleUpdateRequest,
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

_openai_client = None


def _get_openai_client():
    """Lazily constructed, cached singleton -- not built at import time
    since OPENAI_API_KEY need not be set for every deployment context (e.g.
    running just the test suite). Tests patch this function itself (module-
    boundary injection) to exercise /conversations/step's tool-calling loop
    without hitting the real API -- see tests/test_conversations.py."""
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


@app.exception_handler(RequestValidationError)
def _validation_error_handler(request: Request, exc: RequestValidationError):
    """Flattens FastAPI/pydantic's default {"detail": [{"msg": ..., ...}]}
    shape into a single human-readable string, consistent with every other
    error response in this service (e.g. HostPairResponse's 409, or
    _safe_filename's 400) -- and what utils/auth.py's _error_detail (and
    thus every settings-page st.error(result["error"])) actually expects.
    Added for ProfileUpdateRequest's email/phone validators, but applies
    service-wide since every 422 here benefits the same way."""
    return JSONResponse(status_code=422, content={"detail": _first_validation_message(exc.errors())})


def _first_validation_message(errors: list[dict]) -> str:
    """Shared by the global RequestValidationError handler above and by any
    handler that manually re-validates a merged PATCH body through a
    *CreateRequest model (see update_policy_layer_rule).

    Prefers a "value_error" entry (a validator's own deliberately-written
    message, e.g. Pattern's "Invalid regex ...") over a generic
    structural one (e.g. "literal_error") when both are present -- this
    mattered while positional_constraints/OptionConstraint.pattern were
    still `Literal["*"] | Pattern` unions (since dropped in favor
    of Pattern alone being expressive enough on its own): a
    genuinely-invalid Pattern payload failed BOTH union branches
    (not the literal "*", *and* the model's own validator rejected it),
    and pydantic reports every attempted branch's error in declaration
    order -- so without this, a real "invalid regex" mistake confusingly
    surfaced as "Input should be '*'" (the *other* branch's unrelated
    failure) purely because that branch happened to be declared first.
    Kept as the general-purpose preference regardless, in case a future
    union-typed field hits the same issue.

    pydantic v2 prefixes a validator's own raised ValueError text with
    "Value error, " in the formatted message, stripped so e.g. "Enter a
    valid email address." isn't shown as "Value error, Enter a valid email
    address.\""""
    if not errors:
        return "Invalid request."
    best = next((e for e in errors if e.get("type") == "value_error"), errors[0])
    return best.get("msg", "Invalid request.").removeprefix("Value error, ")


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


def _host_workspace(db, user_id: int, host_id: int) -> list[str]:
    """Live, daemon-reported workspace for one of the caller's own hosts --
    same "only when actually connected" posture as list_hosts' own
    HostInfo.workspace (a disconnected host has no live roots to check
    "{roots}" containment against). Returns [] rather than raising for an
    unowned/unconnected host -- ownership is checked separately by the
    caller so the 404 message stays uniform."""
    row = db.execute("SELECT routing_key FROM hosts WHERE id = ?", (host_id,)).fetchone()
    if row is None:
        return []
    with _attached_lock:
        att = _attached.get(row["routing_key"])
    if att is None or att["user_id"] != user_id or att["local_agent_url"] is None:
        return []
    return att["workspace"]


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


def _user_owns_policy_layer(db, user_id: int, policy_layer_id: int) -> bool:
    return (
        db.execute("SELECT 1 FROM policy_layers WHERE id = ? AND user_id = ?", (policy_layer_id, user_id)).fetchone()
        is not None
    )


def _user_owns_policy_layer_rule(db, user_id: int, policy_layer_id: int, rule_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM policy_layer_rules plr JOIN policy_layers pl ON pl.id = plr.policy_layer_id "
            "WHERE plr.id = ? AND plr.policy_layer_id = ? AND pl.user_id = ?",
            (rule_id, policy_layer_id, user_id),
        ).fetchone()
        is not None
    )


def _policy_layer_rule_info(row) -> PolicyLayerRuleInfo:
    return PolicyLayerRuleInfo(
        id=row["id"],
        position=row["position"],
        positional_constraints=json.loads(row["positional_constraints"]),
        option_constraints=json.loads(row["option_constraints"]),
        tier=row["tier"],
    )


def _policy_layer_rules(db, policy_layer_id: int) -> list[PolicyLayerRuleInfo]:
    rows = db.execute(
        "SELECT id, position, positional_constraints, option_constraints, tier "
        "FROM policy_layer_rules WHERE policy_layer_id = ? ORDER BY position",
        (policy_layer_id,),
    ).fetchall()
    return [_policy_layer_rule_info(r) for r in rows]


def _policy_layer_info(db, policy_layer_id: int) -> PolicyLayerInfo:
    row = db.execute("SELECT id, name FROM policy_layers WHERE id = ?", (policy_layer_id,)).fetchone()
    host_ids = [
        r["host_id"]
        for r in db.execute(
            "SELECT host_id FROM policy_layer_hosts WHERE policy_layer_id = ?", (policy_layer_id,)
        ).fetchall()
    ]
    return PolicyLayerInfo(id=row["id"], name=row["name"], rules=_policy_layer_rules(db, policy_layer_id), host_ids=host_ids)


def _connected_host_configs(db, user_id: int) -> dict[str, dict]:
    """Every one of the caller's own hosts that's currently connected,
    keyed by label -- POST /conversations/step's own analogue of
    pages/chat.py's old local_agent_configs, built from THIS service's own
    state instead of accepted from the caller (see conversations.py's own
    module docstring on why that matters). policy_layers is left
    un-composed (a list of (layer_id, rules) pairs) -- composition happens
    per-call via policy.compose_policy, same as everywhere else this
    project composes a host's policy."""
    rows = db.execute(
        "SELECT h.id, h.routing_key, uh.label FROM user_hosts uh JOIN hosts h ON h.id = uh.host_id WHERE uh.user_id = ?",
        (user_id,),
    ).fetchall()
    layer_host_rows = db.execute(
        """
        SELECT plh.host_id, pl.id
        FROM policy_layer_hosts plh JOIN policy_layers pl ON pl.id = plh.policy_layer_id
        WHERE pl.user_id = ?
        """,
        (user_id,),
    ).fetchall()
    layers_by_host: dict[int, list[tuple[int, list[PolicyLayerRuleInfo]]]] = {}
    for r in layer_host_rows:
        layers_by_host.setdefault(r["host_id"], []).append((r["id"], _policy_layer_rules(db, r["id"])))
    with _attached_lock:
        attached_snapshot = {k: dict(v) for k, v in _attached.items()}
    configs = {}
    for r in rows:
        att = attached_snapshot.get(r["routing_key"])
        if att is None or att["user_id"] != user_id or att["local_agent_url"] is None:
            continue
        configs[r["label"]] = {
            "host_id": r["id"],
            "url": att["local_agent_url"],
            "api_key": att["command_key"],
            "workspace": att["workspace"],
            "policy_layers": layers_by_host.get(r["id"], []),
        }
    return configs


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
        layer_host_rows = db.execute(
            """
            SELECT plh.host_id, pl.id, pl.name
            FROM policy_layer_hosts plh JOIN policy_layers pl ON pl.id = plh.policy_layer_id
            WHERE pl.user_id = ? ORDER BY pl.name COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()
        rule_rows = db.execute(
            """
            SELECT plr.policy_layer_id, plr.id, plr.position, plr.positional_constraints,
                   plr.option_constraints, plr.tier
            FROM policy_layer_rules plr JOIN policy_layers pl ON pl.id = plr.policy_layer_id
            WHERE pl.user_id = ? ORDER BY plr.position
            """,
            (user_id,),
        ).fetchall()
    envs_by_host: dict[int, list[int]] = {}
    for r in env_rows:
        envs_by_host.setdefault(r["host_id"], []).append(r["environment_id"])
    rules_by_layer: dict[int, list[PolicyLayerRuleInfo]] = {}
    for r in rule_rows:
        rules_by_layer.setdefault(r["policy_layer_id"], []).append(_policy_layer_rule_info(r))
    # Attached regardless of live connection state -- like environment_ids
    # above (persisted config), not like workspace/local_agent_url below
    # (live daemon-reported state) -- a disconnected host's attached
    # policy layers are still meaningful to show (e.g. in a policy-authoring
    # UI's host-attachment grid).
    layers_by_host: dict[int, list[HostPolicyLayerInfo]] = {}
    for r in layer_host_rows:
        layers_by_host.setdefault(r["host_id"], []).append(
            HostPolicyLayerInfo(id=r["id"], name=r["name"], rules=rules_by_layer.get(r["id"], []))
        )
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
                policy_layers=layers_by_host.get(r["id"], []),
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
        db.execute(
            "DELETE FROM user_attended_host WHERE user_id = ? AND host_id = ?", (user_id, host_id)
        )
    if row is not None:
        with _attached_lock:
            attached = _attached.get(row["routing_key"])
            if attached and attached["user_id"] == user_id:
                _attached.pop(row["routing_key"], None)
    return RevokeResponse(revoked=True)


# --- Attended host (browser-facing) ---------------------------------------
# Which one of the user's own known hosts they're currently physically at
# -- used only to route a pending approval's native-dialog prompt to the
# right daemon (see the pending-approvals section below). Deliberately
# separate from host *selection* in chat (which host the assistant acts
# on) -- a user can direct the assistant at one machine while sitting at
# another.
@app.put("/users/me/attended-host", response_model=AttendedHostInfo)
def set_attended_host(body: AttendedHostUpdateRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        row = db.execute(
            "SELECT label FROM user_hosts WHERE user_id = ? AND host_id = ?", (user_id, body.host_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Host not found.")
        db.execute(
            """
            INSERT INTO user_attended_host (user_id, host_id) VALUES (?, ?)
            ON CONFLICT (user_id) DO UPDATE SET host_id = excluded.host_id, set_at = CURRENT_TIMESTAMP
            """,
            (user_id, body.host_id),
        )
        return AttendedHostInfo(host_id=body.host_id, label=row["label"])


@app.get("/users/me/attended-host", response_model=AttendedHostInfo)
def get_attended_host(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        row = db.execute(
            """
            SELECT uah.host_id AS host_id, uh.label AS label FROM user_attended_host uah
            JOIN user_hosts uh ON uh.user_id = uah.user_id AND uh.host_id = uah.host_id
            WHERE uah.user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return AttendedHostInfo()
        return AttendedHostInfo(host_id=row["host_id"], label=row["label"])


@app.delete("/users/me/attended-host", response_model=RevokeResponse)
def clear_attended_host(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        db.execute("DELETE FROM user_attended_host WHERE user_id = ?", (user_id,))
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


# --- Policy Layers (browser-initiated CRUD, session-token-gated) ---------
# A user-authored, reusable, ORDERED list of rules for how the assistant
# may invoke CLI commands on a host -- see db.py's own schema comment for
# the full rule shape. Which hosts a layer is enabled on is separate
# (policy_layer_hosts), mirroring environments/environment_hosts above.
@app.get("/policy-layers", response_model=PolicyLayerListResponse)
def list_policy_layers(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        rows = db.execute(
            "SELECT id FROM policy_layers WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,)
        ).fetchall()
        return PolicyLayerListResponse(policy_layers=[_policy_layer_info(db, r["id"]) for r in rows])


@app.post("/policy-layers", response_model=PolicyLayerInfo, status_code=201)
def create_policy_layer(body: PolicyLayerCreateRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        existing = db.execute(
            "SELECT id FROM policy_layers WHERE user_id = ? AND name = ? COLLATE NOCASE", (user_id, body.name)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="A policy layer with that name already exists.")
        policy_layer_id = db.execute("INSERT INTO policy_layers (user_id, name) VALUES (?, ?)", (user_id, body.name)).lastrowid
        return _policy_layer_info(db, policy_layer_id)


@app.patch("/policy-layers/{policy_layer_id}", response_model=PolicyLayerInfo)
def rename_policy_layer(policy_layer_id: int, body: PolicyLayerRenameRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer(db, user_id, policy_layer_id):
            raise HTTPException(status_code=404, detail="Policy layer not found.")
        db.execute("UPDATE policy_layers SET name = ? WHERE id = ?", (body.name, policy_layer_id))
        return _policy_layer_info(db, policy_layer_id)


@app.delete("/policy-layers/{policy_layer_id}", response_model=RevokeResponse)
def delete_policy_layer(policy_layer_id: int, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer(db, user_id, policy_layer_id):
            raise HTTPException(status_code=404, detail="Policy layer not found.")
        db.execute("DELETE FROM policy_layer_hosts WHERE policy_layer_id = ?", (policy_layer_id,))
        db.execute("DELETE FROM policy_layer_rules WHERE policy_layer_id = ?", (policy_layer_id,))
        db.execute("DELETE FROM policy_layers WHERE id = ?", (policy_layer_id,))
    return RevokeResponse(revoked=True)


@app.put("/policy-layers/{policy_layer_id}/hosts/{host_id}", response_model=PolicyLayerInfo)
def add_policy_layer_to_host(policy_layer_id: int, host_id: int, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer(db, user_id, policy_layer_id) or not _user_owns_host(db, user_id, host_id):
            raise HTTPException(status_code=404, detail="Policy layer or host not found.")
        db.execute(
            "INSERT OR IGNORE INTO policy_layer_hosts (policy_layer_id, host_id) VALUES (?, ?)",
            (policy_layer_id, host_id),
        )
        return _policy_layer_info(db, policy_layer_id)


@app.delete("/policy-layers/{policy_layer_id}/hosts/{host_id}", response_model=PolicyLayerInfo)
def remove_policy_layer_from_host(policy_layer_id: int, host_id: int, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer(db, user_id, policy_layer_id):
            raise HTTPException(status_code=404, detail="Policy layer not found.")
        db.execute(
            "DELETE FROM policy_layer_hosts WHERE policy_layer_id = ? AND host_id = ?", (policy_layer_id, host_id)
        )
        return _policy_layer_info(db, policy_layer_id)


def _serialize_positional_constraints(constraints) -> str:
    return json.dumps([c.model_dump() for c in constraints])


def _serialize_option_constraints(constraints) -> str:
    return json.dumps([{"short": c.short, "long": c.long, "pattern": c.pattern.model_dump()} for c in constraints])


@app.post("/policy-layers/{policy_layer_id}/rules", response_model=PolicyLayerRuleInfo, status_code=201)
def create_policy_layer_rule(
    policy_layer_id: int, body: PolicyLayerRuleCreateRequest, authorization: str = Header(default="")
):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer(db, user_id, policy_layer_id):
            raise HTTPException(status_code=404, detail="Policy layer not found.")
        next_position = db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM policy_layer_rules WHERE policy_layer_id = ?",
            (policy_layer_id,),
        ).fetchone()["next_position"]
        rule_id = db.execute(
            "INSERT INTO policy_layer_rules "
            "(policy_layer_id, position, positional_constraints, option_constraints, tier) VALUES (?, ?, ?, ?, ?)",
            (
                policy_layer_id,
                next_position,
                _serialize_positional_constraints(body.positional_constraints),
                _serialize_option_constraints(body.option_constraints),
                body.tier,
            ),
        ).lastrowid
        row = db.execute(
            "SELECT id, position, positional_constraints, option_constraints, tier "
            "FROM policy_layer_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        return _policy_layer_rule_info(row)


@app.patch("/policy-layers/{policy_layer_id}/rules/{rule_id}", response_model=PolicyLayerRuleInfo)
def update_policy_layer_rule(
    policy_layer_id: int, rule_id: int, body: PolicyLayerRuleUpdateRequest, authorization: str = Header(default="")
):
    """Re-validates the MERGED (current row + patch) shape by reconstructing
    it through PolicyLayerRuleCreateRequest -- a partial patch can't be
    cross-field-validated (position 0, "{roots}"+tier) in isolation, same
    merge-then-revalidate trick this service uses for every other partial
    update (see e.g. ProfileUpdateRequest's own docstring)."""
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer_rule(db, user_id, policy_layer_id, rule_id):
            raise HTTPException(status_code=404, detail="Rule not found.")
        current = db.execute(
            "SELECT positional_constraints, option_constraints, tier FROM policy_layer_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        merged = {
            "positional_constraints": json.loads(current["positional_constraints"]),
            "option_constraints": json.loads(current["option_constraints"]),
            "tier": current["tier"],
        }
        merged.update(body.model_dump(exclude_unset=True))
        try:
            validated = PolicyLayerRuleCreateRequest(**merged)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=_first_validation_message(e.errors())) from e
        db.execute(
            "UPDATE policy_layer_rules SET positional_constraints = ?, option_constraints = ?, tier = ? WHERE id = ?",
            (
                _serialize_positional_constraints(validated.positional_constraints),
                _serialize_option_constraints(validated.option_constraints),
                validated.tier,
                rule_id,
            ),
        )
        row = db.execute(
            "SELECT id, position, positional_constraints, option_constraints, tier "
            "FROM policy_layer_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        return _policy_layer_rule_info(row)


@app.delete("/policy-layers/{policy_layer_id}/rules/{rule_id}", response_model=RevokeResponse)
def delete_policy_layer_rule(policy_layer_id: int, rule_id: int, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer_rule(db, user_id, policy_layer_id, rule_id):
            raise HTTPException(status_code=404, detail="Rule not found.")
        db.execute("DELETE FROM policy_layer_rules WHERE id = ?", (rule_id,))
    return RevokeResponse(revoked=True)


@app.put("/policy-layers/{policy_layer_id}/rules/reorder", response_model=PolicyLayerInfo)
def reorder_policy_layer_rules(
    policy_layer_id: int, body: PolicyLayerRuleReorderRequest, authorization: str = Header(default="")
):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer(db, user_id, policy_layer_id):
            raise HTTPException(status_code=404, detail="Policy layer not found.")
        current_ids = {
            r["id"] for r in db.execute("SELECT id FROM policy_layer_rules WHERE policy_layer_id = ?", (policy_layer_id,)).fetchall()
        }
        if len(body.rule_ids) != len(current_ids) or set(body.rule_ids) != current_ids:
            raise HTTPException(
                status_code=400, detail="rule_ids must be exactly a permutation of the layer's current rules."
            )
        for position, rule_id in enumerate(body.rule_ids):
            db.execute("UPDATE policy_layer_rules SET position = ? WHERE id = ?", (position, rule_id))
        return _policy_layer_info(db, policy_layer_id)


# --- Policy evaluation (browser/harness-facing, no daemon involved) ------
# The fast/deterministic bottom of the testing pyramid: evaluates one
# hypothetical call against an ad hoc composition of the caller's own
# policy layers, with no side effects and no daemon round trip. Distinct
# from GET /hosts/policy-layers below (the daemon's own, host-scoped,
# always-every-attached-layer fetch) -- eval lets the caller compose
# whichever arbitrary subset of layers it wants, exactly the "policy is a
# layer composition, layer IDs may be ad hoc" vocabulary this project
# settled on.
@app.post("/policies/eval", response_model=PolicyEvalResponse)
def eval_policy(body: PolicyEvalRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        for policy_layer_id in body.policy_layer_ids:
            if not _user_owns_policy_layer(db, user_id, policy_layer_id):
                raise HTTPException(status_code=404, detail="Policy layer not found.")
        if body.host_id is not None:
            if not _user_owns_host(db, user_id, body.host_id):
                raise HTTPException(status_code=404, detail="Host not found.")
            roots = _host_workspace(db, user_id, body.host_id)
        else:
            roots = body.roots or []
        layers = [(policy_layer_id, _policy_layer_rules(db, policy_layer_id)) for policy_layer_id in body.policy_layer_ids]
    composed = compose_policy(layers)
    options = [o.model_dump() for o in body.options]
    matched_layer_id, matched_rule = match_policy(composed, body.positional_args, options, roots)
    tier = matched_rule.tier if matched_rule else "deny"
    return PolicyEvalResponse(tier=tier, matched_layer_id=matched_layer_id, matched_rule=matched_rule)


# --- Policy Layers (daemon-facing fetch, device_token-gated) -------------
# What THIS host's own enforcement copy should be -- independent of GET
# /hosts's browser-facing copy above (same underlying data, different
# credential/audience). The daemon fetches this for itself rather than
# trusting the browser/model to have applied a rule correctly -- every rule
# is structurally re-enforced daemon-side regardless of what tier
# pages/chat.py already decided client-side.
@app.get("/hosts/policy-layers", response_model=HostPolicyLayerListResponse)
def list_host_policy_layers(authorization: str = Header(default="")):
    attached = _resolve_attached(authorization)
    if attached is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")
    with get_db() as db:
        layer_rows = db.execute(
            """
            SELECT pl.id, pl.name
            FROM policy_layer_hosts plh JOIN policy_layers pl ON pl.id = plh.policy_layer_id
            WHERE plh.host_id = ? AND pl.user_id = ? ORDER BY pl.name COLLATE NOCASE
            """,
            (attached["host_id"], attached["user_id"]),
        ).fetchall()
        policy_layers = [
            HostPolicyLayerInfo(id=r["id"], name=r["name"], rules=_policy_layer_rules(db, r["id"])) for r in layer_rows
        ]
    return HostPolicyLayerListResponse(policy_layers=policy_layers)


# --- Pending approvals (native-dialog relay) ------------------------------
# An "ask"-tier command-template call awaiting a human decision, answerable
# from either the browser's in-chat Approve/Deny UI or a native OS dialog
# on whichever host the user has designated as their attended host (see
# above) -- both channels write the same decision here, and whichever
# answers first wins. Kept in-memory rather than in SQLite, same
# single-process reasoning as _attached above: a server restart loses any
# outstanding approval, an accepted rough edge (same one _attached already
# accepts for live attachments). A single Condition guards the whole store
# and serves both directions this needs to long-poll: the attended daemon
# waiting for a new approval targeting it, and the submitter waiting for a
# decision on the one it created.
_PENDING_APPROVAL_TTL_SECONDS = 15 * 60
_LONG_POLL_SECONDS = 25.0

_pending_approvals: dict[str, dict] = {}
_pending_cond = threading.Condition()


def _prune_pending_approvals_locked():
    """Caller must hold _pending_cond. Drops anything older than the TTL,
    decided or not -- an undecided one that's aged out is exactly as
    unreachable as one that was decided and already consumed."""
    cutoff = time.time() - _PENDING_APPROVAL_TTL_SECONDS
    stale = [aid for aid, rec in _pending_approvals.items() if rec["created_ts"] < cutoff]
    for aid in stale:
        del _pending_approvals[aid]


def _pending_approval_info(record: dict) -> PendingApprovalInfo:
    return PendingApprovalInfo(
        id=record["id"],
        template_name=record["template_name"],
        binary=record["binary"],
        args=record["args"],
        host_label=record["host_label"],
        decision=record["decision"],
        created_at=record["created_at"],
    )


def _resolve_submitter(authorization: str) -> int | None:
    """Session token OR device token -> user_id. Only the web app submits
    today, but this is written so a daemon can submit its own mid-chain
    asks later (once "hard" action chains exist) without this shape
    changing."""
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
    if user_id is not None:
        return user_id
    attached = _resolve_attached(authorization)
    return attached["user_id"] if attached else None


def _create_pending_approval_record(user_id: int, template_name: str, binary: str, args: str, host_label: str) -> str:
    """Factored out of the create_pending_approval endpoint below so
    /conversations/step's run_shell_command "ask" path can create one
    in-process (direct function call, not a self-HTTP round trip) --
    same store, same long-poll wakeup, just a different caller."""
    approval_id = uuid.uuid4().hex
    with _pending_cond:
        _prune_pending_approvals_locked()
        _pending_approvals[approval_id] = {
            "id": approval_id,
            "user_id": user_id,
            "template_name": template_name,
            "binary": binary,
            "args": args,
            "host_label": host_label,
            "decision": None,
            "created_ts": time.time(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _pending_cond.notify_all()
    return approval_id


@app.post("/hosts/pending-approvals", response_model=PendingApprovalCreateResponse, status_code=201)
def create_pending_approval(body: PendingApprovalCreateRequest, authorization: str = Header(default="")):
    user_id = _resolve_submitter(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")
    approval_id = _create_pending_approval_record(user_id, body.template_name, body.binary, body.args, body.host_label)
    return PendingApprovalCreateResponse(approval_id=approval_id)


@app.get("/hosts/pending-approvals/{approval_id}", response_model=PendingApprovalInfo)
def get_pending_approval(approval_id: str, wait_seconds: float = _LONG_POLL_SECONDS, authorization: str = Header(default="")):
    """The submitter's long-poll -- waits up to wait_seconds (default
    _LONG_POLL_SECONDS, clamped to that as a ceiling) for a decision to
    land, so the caller can immediately re-request rather than fast-polling
    on a fixed timer. wait_seconds exists for pages/chat.py's
    st.fragment(run_every=...)-driven poll specifically: blocking the full
    ~25s there would stall that browser session's script thread (and its
    own Approve/Deny buttons) for the same duration, so it asks for a short
    wait (a few seconds) instead -- the daemon's own long-poll (a Go
    goroutine, not constrained the same way) keeps using the default."""
    user_id = _resolve_submitter(authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")

    wait_seconds = max(0.0, min(wait_seconds, _LONG_POLL_SECONDS))
    deadline = time.monotonic() + wait_seconds
    with _pending_cond:
        while True:
            _prune_pending_approvals_locked()
            record = _pending_approvals.get(approval_id)
            if record is None or record["user_id"] != user_id:
                raise HTTPException(status_code=404, detail="Unknown or expired approval.")
            if record["decision"] is not None:
                return _pending_approval_info(record)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _pending_approval_info(record)
            _pending_cond.wait(remaining)


@app.get("/hosts/pending-approvals", response_model=PendingApprovalListResponse)
def list_pending_approvals_for_attended_host(authorization: str = Header(default="")):
    """The attended daemon's long-poll -- waits up to _LONG_POLL_SECONDS
    for any undecided approval belonging to a user whose current attended
    host is this caller's own resolved host_id. Every daemon runs this
    loop unconditionally; it's this query, not any local state on the
    daemon, that decides whether it ever receives anything."""
    attached = _resolve_attached(authorization)
    if attached is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")
    host_id = attached["host_id"]

    def _matching_records() -> list[dict]:
        with get_db() as db:
            attended_user_ids = {
                r["user_id"]
                for r in db.execute(
                    "SELECT user_id FROM user_attended_host WHERE host_id = ?", (host_id,)
                ).fetchall()
            }
        return [
            rec
            for rec in _pending_approvals.values()
            if rec["decision"] is None and rec["user_id"] in attended_user_ids
        ]

    deadline = time.monotonic() + _LONG_POLL_SECONDS
    with _pending_cond:
        while True:
            _prune_pending_approvals_locked()
            matches = _matching_records()
            if matches:
                return PendingApprovalListResponse(pending_approvals=[_pending_approval_info(r) for r in matches])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return PendingApprovalListResponse(pending_approvals=[])
            _pending_cond.wait(remaining)


@app.post("/hosts/pending-approvals/{approval_id}/decision", response_model=PendingApprovalInfo)
def decide_pending_approval(
    approval_id: str, body: PendingApprovalDecisionRequest, authorization: str = Header(default="")
):
    """Only from a daemon that IS currently the attended host for that
    approval's user -- a daemon that's since been un-designated can't
    decide someone else's pending approval just because it still holds a
    valid device token."""
    attached = _resolve_attached(authorization)
    if attached is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")

    with _pending_cond:
        _prune_pending_approvals_locked()
        record = _pending_approvals.get(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown or expired approval.")
        with get_db() as db:
            still_attended = db.execute(
                "SELECT 1 FROM user_attended_host WHERE user_id = ? AND host_id = ?",
                (record["user_id"], attached["host_id"]),
            ).fetchone()
        if still_attended is None:
            raise HTTPException(
                status_code=403, detail="This host is no longer the attended host for that approval."
            )
        if record["decision"] is None:
            record["decision"] = body.decision
            _pending_cond.notify_all()
        return _pending_approval_info(record)


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


def _make_storage_io(user_id: int):
    """Read/write closures over one user's own storage directory and cap-
    check logic, handed to conversations.DispatchContext as callbacks --
    conversations.py never needs to know this service's storage layout or
    STORAGE_CAP_BYTES itself (see its own DispatchContext docstring).
    Reuses the exact same helpers list_storage/upload_storage/
    download_storage already call, just in-process rather than over HTTP
    (transfer_file's "server storage" side used to go through those
    endpoints via pages/chat.py's own download_storage/upload_storage HTTP
    wrappers; now that orchestration lives in the same service, that round
    trip is pointless)."""
    directory = _user_storage_dir(user_id)

    def read(filename: str) -> str | None:
        try:
            safe = _safe_filename(filename)
        except HTTPException:
            return None
        target = os.path.join(directory, safe)
        if not os.path.isfile(target):
            return None
        with open(target, "rb") as f:
            return base64.b64encode(f.read()).decode()

    def write(filename: str, content_b64: str) -> tuple[str | None, int]:
        """Returns (error_message, 0) on failure or (None, byte_size) on success."""
        try:
            safe = _safe_filename(filename)
        except HTTPException:
            return "Invalid filename.", 0
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            return "Invalid base64 content.", 0
        target = os.path.join(directory, safe)
        existing_size = os.path.getsize(target) if os.path.exists(target) else 0
        if _dir_total_bytes(directory) - existing_size + len(data) > STORAGE_CAP_BYTES:
            return f"Storage cap exceeded ({STORAGE_CAP_BYTES // (1024 * 1024)}MB per user).", 0
        with open(target, "wb") as f:
            f.write(data)
        return None, len(data)

    return read, write


# --- Conversations (browser/harness-facing tool-calling orchestration) ---
# The stateless counterpart to pages/chat.py's own tool-calling loop -- see
# conversations.py's module docstring for the full design (no streaming,
# no caller-supplied host connection details, turn state fully
# externalized, mock only fakes daemon/storage dispatch, never the model
# call itself).
@app.post("/conversations/step", response_model=ConversationStepResponse)
def step_conversation(body: ConversationStepRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        configs = _connected_host_configs(db, user_id)

    in_flight = conversations.is_in_flight(body.turn)
    if in_flight:
        if body.message is not None or body.tool_call is not None:
            raise HTTPException(
                status_code=400,
                detail="Cannot supply message/tool_call while a turn is in flight -- resolve the pending approval first.",
            )
        if body.approval_decision is None:
            raise HTTPException(status_code=400, detail="This turn is awaiting an approval decision.")
        turn = body.turn
    else:
        if body.message is None and body.tool_call is None:
            raise HTTPException(status_code=400, detail="Supply message or tool_call to start a new turn.")
        previous_response_id = (body.turn or {}).get("previous_response_id")
        if body.message is not None:
            turn = conversations.new_turn_from_message(body.message)
        else:
            turn = conversations.new_turn_from_tool_call(body.tool_call.name, body.tool_call.arguments)
        turn["previous_response_id"] = previous_response_id

    read_storage, write_storage = _make_storage_io(user_id)
    ctx = conversations.DispatchContext(
        configs=configs,
        default_host=body.default_host,
        mock=body.mock,
        read_server_storage=read_storage,
        write_server_storage=write_storage,
        create_pending_approval=lambda template_name, binary, args, host_label: _create_pending_approval_record(
            user_id, template_name, binary, args, host_label
        ),
    )
    status, message = conversations.run_turn(turn, body.approval_decision, ctx, _get_openai_client())

    pending_approval = None
    if status == "pending_approval":
        pending = turn["awaiting_approval"]
        pending_approval = ConversationPendingApproval(
            call_id=pending["call_id"], host=pending.get("host"), args=pending["args"], approval_id=pending.get("approval_id")
        )
    return ConversationStepResponse(
        turn=turn, status=status, message=message if status == "done" else None, pending_approval=pending_approval
    )


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
