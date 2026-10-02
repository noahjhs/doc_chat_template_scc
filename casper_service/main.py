import base64
import binascii
import concurrent.futures
import contextlib
import hashlib
import json
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
from pydantic import ValidationError
import backups
import conversations
import mcp_server
import trust
from models import (
    AccessRequestCreate,
    AgentTokenCreateRequest,
    AgentTokenCreateResponse,
    AgentTokenInfo,
    AgentTokenListResponse,
    BackupKeyReport,
    FriendRequestCreate,
    OfferingCreateRequest,
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
    Pattern,
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
    TelegramLinkResponse,
    VerifyResponse,
)

@contextlib.asynccontextmanager
async def _lifespan(_app):
    # The MCP transport's session manager needs a running task group (see
    # the MCP section at the bottom of this module).
    async with _mcp.session_manager.run():
        yield


app = FastAPI(lifespan=_lifespan)
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


# Telegram approvals send/receive via plain requests.post -- no SDK
# dependency needed (unlike Twilio), see _send_approval_telegram and the
# /telegram/webhook handler below.
_telegram_link_tokens: dict[str, tuple[int, float]] = {}  # token -> (user_id, created_ts)
_telegram_link_lock = threading.Lock()
_TELEGRAM_LINK_TTL_SECONDS = 10 * 60


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


# --- Host pairing (persisted) + live reachability (in-memory) ------------
# Pairing identity (who owns a routing_key, and the device_token/command_key
# that prove it) lives in SQLite's host_pairings table -- see db.py's own
# schema comment for why this used to be an in-memory-only dict, and why
# that was the actual cause of daemons needing to be manually re-paired
# after every routine redeploy (an casper_service restart lost it, even
# though the daemon's own device_token was still perfectly valid).
#
# Live reachability (where a paired host is CURRENTLY reachable, and its
# reported cwd) is still deliberately in-memory only, keyed by routing_key
# -- this genuinely SHOULD reset on a restart (a daemon reports it fresh on
# its next presence beat regardless of whether it had to re-pair), so
# there's nothing to persist here, unlike the identity half above.
_live: dict[str, dict] = {}
_live_lock = threading.Lock()


def _pairing_by_device_token(db, token: str):
    return db.execute("SELECT * FROM host_pairings WHERE device_token_hash = ?", (hash_token(token),)).fetchone()


def _resolve_attached(db, authorization: str) -> dict | None:
    """Bearer device_token -> its persisted pairing (routing_key, user_id,
    host_id, command_key) plus whatever live reachability state is
    currently cached in memory for that routing_key (empty/None if this
    process hasn't seen a presence report since it last started -- e.g.
    right after a restart; the daemon's own reconnect-and-report-presence
    loop fixes that within one round trip, no re-pairing involved)."""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    row = _pairing_by_device_token(db, token)
    if row is None:
        return None
    with _live_lock:
        live = dict(_live.get(row["routing_key"], {}))
    return {
        "routing_key": row["routing_key"],
        "user_id": row["user_id"],
        "host_id": row["host_id"],
        "command_key": row["command_key"],
        "local_agent_url": live.get("local_agent_url"),
        "cwd": live.get("cwd", ""),
    }


def _pair_host(db, user_id: int, routing_key: str, hostname: str | None, requested_label: str | None):
    """Idempotent for the *same* user re-pairing the *same* routing_key
    (e.g. reconnecting after a restart) -- rotates credentials in place
    (a fresh UPSERT into host_pairings keyed on (routing_key, user_id)),
    no duplicate rows, existing label untouched. A *different* user pairing
    the same routing_key is fully supported -- one physical daemon can be
    paired to several accounts at once, each getting its own independent
    row/credentials/policy layers (see host_pairings' own doc comment).
    Returns (host_id, device_token, command_key, label,
    is_new_to_this_user)."""
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
    db.execute(
        """
        INSERT INTO host_pairings (routing_key, user_id, host_id, device_token_hash, command_key)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(routing_key, user_id) DO UPDATE SET
            host_id = excluded.host_id,
            device_token_hash = excluded.device_token_hash,
            command_key = excluded.command_key,
            paired_at = CURRENT_TIMESTAMP
        """,
        (routing_key, user_id, host_id, hash_token(device_token), command_key),
    )
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
        cwd=json.loads(row["cwd"]),
        tier=row["tier"],
    )


def _policy_layer_rules(db, policy_layer_id: int) -> list[PolicyLayerRuleInfo]:
    rows = db.execute(
        "SELECT id, position, positional_constraints, option_constraints, cwd, tier "
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
    keyed by label -- built from THIS service's own state instead of
    accepted from the caller (see conversations.py's own module docstring
    on why that matters). policy_layers is left
    un-composed (a list of (layer_id, rules) pairs) -- composition happens
    per-call via policy.compose_policy, same as everywhere else this
    project composes a host's policy. cwd is the host's own daemon-reported
    confined directory (see report_host_presence) -- kept here for display/
    debugging purposes; no server-side policy preview consults it anymore
    (that matching now only ever happens daemon-side -- see
    conversations.py's own module docstring)."""
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
    pairings_by_routing_key = {
        r["routing_key"]: r
        for r in db.execute(
            "SELECT routing_key, command_key FROM host_pairings WHERE user_id = ?", (user_id,)
        ).fetchall()
    }
    with _live_lock:
        live_snapshot = {k: dict(v) for k, v in _live.items()}
    configs = {}
    for r in rows:
        pairing = pairings_by_routing_key.get(r["routing_key"])
        if pairing is None:
            continue
        live = live_snapshot.get(r["routing_key"])
        if live is None or live.get("local_agent_url") is None:
            continue
        configs[r["label"]] = {
            "host_id": r["id"],
            "url": live["local_agent_url"],
            "api_key": pairing["command_key"],
            "policy_layers": layers_by_host.get(r["id"], []),
            "cwd": live.get("cwd", ""),
        }
    return configs


# --- Host pairing / presence (daemon-initiated, device_token-gated) ------
@app.post("/hosts/pair", response_model=HostPairResponse, status_code=201)
def pair_host(body: HostPairRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        host_id, device_token, command_key, label, is_new = _pair_host(
            db, user_id, body.routing_key, body.hostname, body.label
        )
        if is_new:
            _assign_default_environment(db, user_id, host_id)
        return HostPairResponse(host_id=host_id, device_token=device_token, command_key=command_key, label=label)


@app.post("/hosts/verify", response_model=HostVerifyResponse)
def verify_host(authorization: str = Header(default="")):
    with get_db() as db:
        return HostVerifyResponse(valid=_resolve_attached(db, authorization) is not None)


@app.post("/hosts/unpair", response_model=RevokeResponse)
def unpair_host(authorization: str = Header(default="")):
    """Self-service: a daemon deregisters itself (tray "Sign out"). Deletes
    only THIS account's persisted pairing (host_pairings) -- a routing_key
    can be paired to several accounts at once, so this is scoped by
    user_id too, never touching another account's pairing to the same
    physical host. user_hosts/environment_hosts are untouched, so the host
    stays remembered and re-pairing it later won't re-prompt for a label.
    Deliberately doesn't touch _live -- that's genuinely machine-level
    reachability, still meaningful to any other account still paired to
    this routing_key; it self-heals independently via presence reports."""
    with get_db() as db:
        attached = _resolve_attached(db, authorization)
        if attached is not None:
            db.execute(
                "DELETE FROM host_pairings WHERE routing_key = ? AND user_id = ?",
                (attached["routing_key"], attached["user_id"]),
            )
    return RevokeResponse(revoked=True)


@app.post("/hosts/presence", response_model=HostInfo)
def report_host_presence(body: HostPresenceReport, authorization: str = Header(default="")):
    with get_db() as db:
        attached = _resolve_attached(db, authorization)
    if attached is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")
    with _live_lock:
        _live[attached["routing_key"]] = {"local_agent_url": body.local_agent_url, "cwd": body.cwd}
    return HostInfo(
        host_id=attached["host_id"], label="", connected=True,
        local_agent_url=body.local_agent_url, cwd=body.cwd,
    )


@app.delete("/hosts/presence", response_model=HostInfo)
def clear_host_presence(authorization: str = Header(default="")):
    """Toggling the relay off is not signing out -- clears only reachability,
    the pairing (and its credentials) stays valid."""
    with get_db() as db:
        attached = _resolve_attached(db, authorization)
    if attached is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")
    with _live_lock:
        _live[attached["routing_key"]] = {"local_agent_url": None, "cwd": ""}
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
                   plr.option_constraints, plr.cwd, plr.tier
            FROM policy_layer_rules plr JOIN policy_layers pl ON pl.id = plr.policy_layer_id
            WHERE pl.user_id = ? ORDER BY plr.position
            """,
            (user_id,),
        ).fetchall()
        pairing_rows = db.execute(
            "SELECT routing_key, command_key FROM host_pairings WHERE user_id = ?", (user_id,)
        ).fetchall()
    envs_by_host: dict[int, list[int]] = {}
    for r in env_rows:
        envs_by_host.setdefault(r["host_id"], []).append(r["environment_id"])
    rules_by_layer: dict[int, list[PolicyLayerRuleInfo]] = {}
    for r in rule_rows:
        rules_by_layer.setdefault(r["policy_layer_id"], []).append(_policy_layer_rule_info(r))
    # Attached regardless of live connection state -- like environment_ids
    # above (persisted config), not like cwd/local_agent_url below (live
    # daemon-reported state) -- a disconnected host's attached
    # policy layers are still meaningful to show (e.g. in a policy-authoring
    # UI's host-attachment grid).
    layers_by_host: dict[int, list[HostPolicyLayerInfo]] = {}
    for r in layer_host_rows:
        layers_by_host.setdefault(r["host_id"], []).append(
            HostPolicyLayerInfo(id=r["id"], name=r["name"], rules=rules_by_layer.get(r["id"], []))
        )
    pairings_by_routing_key = {r["routing_key"]: r for r in pairing_rows}
    with _live_lock:
        live_snapshot = {k: dict(v) for k, v in _live.items()}
    hosts_out = []
    for r in rows:
        pairing = pairings_by_routing_key.get(r["routing_key"])
        mine = pairing is not None
        live = live_snapshot.get(r["routing_key"], {})
        connected = mine and live.get("local_agent_url") is not None
        hosts_out.append(
            HostInfo(
                host_id=r["id"], label=r["label"], hostname=r["hostname"], connected=connected,
                local_agent_url=live.get("local_agent_url") if connected else None,
                cwd=live.get("cwd", "") if connected else "",
                command_key=pairing["command_key"] if mine else None,
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
    Environment it belonged to. If currently PAIRED to this same caller,
    also unpairs it (deletes its host_pairings row -- forgetting a host
    you're using ends the pairing, requiring a real re-pair later, not
    just a reconnect) -- never touches another user's pairing to the same
    physical host."""
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
            db.execute(
                "DELETE FROM host_pairings WHERE routing_key = ? AND user_id = ?", (row["routing_key"], user_id)
            )
    # Deliberately doesn't touch _live -- see unpair_host's identical
    # reasoning: it's genuinely machine-level reachability, still
    # meaningful to any other account still paired to this routing_key.
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


def _serialize_cwd(pattern: Pattern) -> str:
    return json.dumps(pattern.model_dump())


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
            "(policy_layer_id, position, positional_constraints, option_constraints, cwd, tier) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                policy_layer_id,
                next_position,
                _serialize_positional_constraints(body.positional_constraints),
                _serialize_option_constraints(body.option_constraints),
                _serialize_cwd(body.cwd),
                body.tier,
            ),
        ).lastrowid
        row = db.execute(
            "SELECT id, position, positional_constraints, option_constraints, cwd, tier "
            "FROM policy_layer_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        return _policy_layer_rule_info(row)


@app.patch("/policy-layers/{policy_layer_id}/rules/{rule_id}", response_model=PolicyLayerRuleInfo)
def update_policy_layer_rule(
    policy_layer_id: int, rule_id: int, body: PolicyLayerRuleUpdateRequest, authorization: str = Header(default="")
):
    """Re-validates the MERGED (current row + patch) shape by reconstructing
    it through PolicyLayerRuleCreateRequest -- a partial patch's individual
    fields (e.g. a lone pattern's regex) can't be validated in isolation
    from the rest of the rule, same merge-then-revalidate trick this
    service uses for every other partial update (see e.g.
    ProfileUpdateRequest's own docstring)."""
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        if not _user_owns_policy_layer_rule(db, user_id, policy_layer_id, rule_id):
            raise HTTPException(status_code=404, detail="Rule not found.")
        current = db.execute(
            "SELECT positional_constraints, option_constraints, cwd, tier FROM policy_layer_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        merged = {
            "positional_constraints": json.loads(current["positional_constraints"]),
            "option_constraints": json.loads(current["option_constraints"]),
            "cwd": json.loads(current["cwd"]),
            "tier": current["tier"],
        }
        merged.update(body.model_dump(exclude_unset=True))
        try:
            validated = PolicyLayerRuleCreateRequest(**merged)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=_first_validation_message(e.errors())) from e
        db.execute(
            "UPDATE policy_layer_rules SET positional_constraints = ?, option_constraints = ?, cwd = ?, tier = ? WHERE id = ?",
            (
                _serialize_positional_constraints(validated.positional_constraints),
                _serialize_option_constraints(validated.option_constraints),
                _serialize_cwd(validated.cwd),
                validated.tier,
                rule_id,
            ),
        )
        row = db.execute(
            "SELECT id, position, positional_constraints, option_constraints, cwd, tier "
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


# --- Policy evaluation (browser/harness-facing, ROUTES TO A REAL DAEMON) --
# No local evaluation attempt of any kind happens here anymore -- this asks
# a REAL, connected daemon (the same one run_shell_command itself would
# dispatch to) to evaluate the hypothetical call, via its own eval_policy
# action (agent/internal/commands/policy.go's runEvalPolicy), using the
# daemon's own authoritative matcher. The caller still composes whichever
# arbitrary/ad hoc subset of their own policy layers they want (exactly the
# "policy is a layer composition, layer IDs may be ad hoc" vocabulary this
# project settled on) -- those layer definitions are sent inline on the
# request, never assumed to be attached to the target host. If the named
# host isn't currently connected, or its daemon can't be reached, this
# fails loudly (502) -- there is deliberately no silent fallback to a local
# match.
@app.post("/policies/eval", response_model=PolicyEvalResponse)
def eval_policy(body: PolicyEvalRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        for policy_layer_id in body.policy_layer_ids:
            if not _user_owns_policy_layer(db, user_id, policy_layer_id):
                raise HTTPException(status_code=404, detail="Policy layer not found.")
        policy_layers = [
            HostPolicyLayerInfo(
                id=pid,
                name=db.execute("SELECT name FROM policy_layers WHERE id = ?", (pid,)).fetchone()["name"],
                rules=_policy_layer_rules(db, pid),
            )
            for pid in body.policy_layer_ids
        ]
        configs = _connected_host_configs(db, user_id)

    resolved_host, config, err = conversations.resolve_host(configs, body.host, None)
    if err:
        raise HTTPException(status_code=404, detail=err)
    try:
        response = requests.post(
            f"{config['url']}/api/command",
            json={
                "action": "eval_policy",
                "positional_args": body.positional_args,
                "options": [o.model_dump() for o in body.options],
                "cwd": body.cwd,
                "policy_layers": [pl.model_dump() for pl in policy_layers],
            },
            headers={"X-API-Key": config["api_key"]},
            timeout=15,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Couldn't reach host {resolved_host!r}: {e}") from e
    if response.status_code == 401:
        raise HTTPException(status_code=502, detail=f"Host {resolved_host!r} rejected its own API key -- re-pair it.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=conversations.daemon_error_detail(response, f"HTTP {response.status_code}"))

    result = response.json()
    tier = result.get("tier") or "deny"
    matched_layer_id = result.get("matched_layer_id") or None
    matched_rule_id = result.get("matched_rule_id") or None
    matched_rule = None
    if matched_layer_id is not None and matched_rule_id is not None:
        layer = next((pl for pl in policy_layers if pl.id == matched_layer_id), None)
        matched_rule = next((r for r in layer.rules if r.id == matched_rule_id), None) if layer else None
    return PolicyEvalResponse(tier=tier, matched_layer_id=matched_layer_id, matched_rule=matched_rule)


# --- Policy Layers (daemon-facing fetch, device_token-gated) -------------
# What THIS host's own enforcement copy should be -- independent of GET
# /hosts's browser-facing copy above (same underlying data, different
# credential/audience). The daemon fetches this for itself rather than
# trusting the caller to have applied a rule correctly -- every rule is
# structurally re-enforced daemon-side regardless of what tier
# conversations.py's own tier decision already reached.
@app.get("/hosts/policy-layers", response_model=HostPolicyLayerListResponse)
def list_host_policy_layers(authorization: str = Header(default="")):
    with get_db() as db:
        attached = _resolve_attached(db, authorization)
        if attached is None:
            raise HTTPException(status_code=401, detail="Invalid or missing device token.")
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
    every host currently paired to this user, then unpairs all of them
    (deletes their host_pairings rows). user_hosts/environment_hosts are
    untouched -- signing out of the website ends pairings, it does not
    make the user forget their hosts or Environments."""
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        mine = db.execute(
            "SELECT routing_key, command_key FROM host_pairings WHERE user_id = ?", (user_id,)
        ).fetchall()
        db.execute("DELETE FROM host_pairings WHERE user_id = ?", (user_id,))
    with _live_lock:
        local_agent_urls = {r["routing_key"]: _live.pop(r["routing_key"], {}).get("local_agent_url") for r in mine}
    targets = [
        (local_agent_urls[r["routing_key"]], r["command_key"]) for r in mine if local_agent_urls[r["routing_key"]]
    ]
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
    download_storage already call, just in-process rather than over HTTP,
    since transfer_file's "server storage" side and those endpoints both
    live in this same service now."""
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


# --- Conversations (harness/future-frontend-facing tool-calling loop) ----
# See conversations.py's module docstring for the full design (no
# streaming, no caller-supplied host connection details, turn state fully
# externalized, mock only fakes daemon/storage dispatch, never the model
# call itself).
@app.post("/conversations/step", response_model=ConversationStepResponse)
def step_conversation(body: ConversationStepRequest, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        configs = _connected_host_configs(db, user_id)
        system_prompt = _get_or_create_profile(db, user_id).system_prompt

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
        mock_tier=body.mock_tier,
        system_prompt=system_prompt,
        read_server_storage=read_storage,
        write_server_storage=write_storage,
        create_pending_approval=lambda description, turn_snapshot: _create_pending_approval_record(
            user_id, description, turn_snapshot, body.default_host, body.mock, body.mock_tier
        ),
    )
    return _step_response(turn, body.approval_decision, ctx)


def _step_response(turn: dict, approval_decision: str | None, ctx: "conversations.DispatchContext") -> ConversationStepResponse:
    """Runs run_turn and shapes the result into a ConversationStepResponse
    -- shared by /conversations/step above and the pending-approval
    resolution paths below (POST .../decide, and the SMS webhook), so all
    three ways of advancing a turn behave identically."""
    status, message = conversations.run_turn(turn, approval_decision, ctx, _get_openai_client())
    pending_approval = None
    if status == "pending_approval":
        pending = turn["awaiting_approval"]
        pending_approval = ConversationPendingApproval(
            call_id=pending["call_id"], host=pending.get("host"), args=pending["args"], approval_id=pending.get("approval_id")
        )
    return ConversationStepResponse(
        turn=turn, status=status, message=message if status == "done" else None, pending_approval=pending_approval
    )


# --- Pending approvals (durable, per-user) ---------------------------------
# A durable record of an "ask"-tier pause (see db.py's pending_approvals
# table and conversations.py's DispatchContext.create_pending_approval),
# resolvable from whichever interface the user actually checks in from --
# harness (below), a Telegram reply (see /telegram/webhook), eventually a
# browser. Deliberately host/device-agnostic: no routing_key/device_token
# appears anywhere here, unlike the native-dialog "attended host" relay
# this replaces (removed as a design mistake -- see git history -- since
# it conflated "which host executes a command" with "which screen a human
# happens to be watching").
def _create_pending_approval_record(
    user_id: int, description: str, turn: dict, default_host: str | None, mock: bool, mock_tier: str
) -> str:
    """Self-contained (opens its own db connection) since every caller --
    a fresh /conversations/step call, or a resume in _resume_pending_approval
    below -- invokes this via DispatchContext.create_pending_approval from
    OUTSIDE any db connection it's itself holding open (matching
    step_conversation's own db-work-then-network-call ordering). mock_tier
    persists the explicit tier a mocked call was paused under (see
    conversations.py's DispatchContext.mock_tier), so resuming it later
    simulates the same decision, not a fresh (and now policy-eval-free)
    one."""
    approval_id = secrets.token_urlsafe(12)
    with get_db() as db:
        db.execute(
            """
            INSERT INTO pending_approvals (id, user_id, description, turn, default_host, mock, mock_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (approval_id, user_id, description, json.dumps(turn), default_host, int(mock), mock_tier),
        )
    _send_approval_telegram(user_id, description, approval_id)
    return approval_id


def _send_approval_telegram(user_id: int, description: str, approval_id: str):
    """Sends an approval request to user_id with inline Approve/Deny
    buttons keyed to this exact approval_id -- a tap posts straight back
    as a callback_query with that id, so there's no "which pending
    approval does a bare yes mean" ambiguity to resolve, unlike a plain
    text reply channel. Best-effort and opt-in, like every notification
    (see notify below) -- the same session, or `harness approvals`, still
    works regardless."""
    notify(
        user_id,
        description,
        buttons=[("✅ Approve", f"approve:{approval_id}"), ("❌ Deny", f"deny:{approval_id}")],
    )


def notify(user_id: int, text: str, buttons: list[tuple[str, str]] | None = None):
    """The one place casper_service tells a person something out of band
    -- an approval request (with buttons, see _send_approval_telegram
    above) or an outcome (a friend's backup finished, an access request was
    granted...). Best-effort: a Telegram failure (not configured for this
    deployment, an API error, an unlinked chat) must never break whatever
    triggered it. No-ops unless the user has BOTH
    telegram_notifications_enabled and a linked telegram_chat_id (see
    models.py's ProfileInfo, and /telegram/link below for how linking
    happens). Telegram is the only channel in v1."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    with get_db() as db:
        profile = _get_or_create_profile(db, user_id)
    if not bot_token or not profile.telegram_notifications_enabled or not profile.telegram_chat_id:
        return
    message: dict = {"chat_id": profile.telegram_chat_id, "text": text}
    if buttons:
        message["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
    try:
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json=message, timeout=10)
    except requests.RequestException as e:
        print(f"Couldn't send Telegram message to user {user_id}: {e}")


def create_approval(approver_id: int, requester_id: int, kind: str, description: str, payload: dict) -> str:
    """Records a non-conversation pending approval (see
    _APPROVAL_HANDLERS below for what each kind does once decided) and
    notifies the approver. requester_id is who asked -- the approver
    themselves for their own agent's ask-tier call, or someone else
    entirely (a friend's backup onto the approver's host)."""
    approval_id = secrets.token_urlsafe(12)
    with get_db() as db:
        db.execute(
            """
            INSERT INTO pending_approvals (id, user_id, description, turn, kind, payload, requester_user_id)
            VALUES (?, ?, ?, '{}', ?, ?, ?)
            """,
            (approval_id, approver_id, description, kind, json.dumps(payload), requester_id),
        )
    _send_approval_telegram(approver_id, description, approval_id)
    return approval_id


# kind -> handler(row, decision) -> a short, human-readable outcome. Each
# handler is responsible for acting on the decision (re-dispatching to a
# daemon, writing a grant, ...) and for notifying the requester, since
# that's the person who's actually waiting on it. Filled in by the
# features that create each kind (MCP shell calls, access requests,
# backups) further down this module.
_APPROVAL_HANDLERS: dict = {}


def _resume_pending_approval(row, decision: str) -> ConversationStepResponse:
    """Rebuilds a DispatchContext for an already-loaded pending_approvals
    row (scoped to the right user by the caller) and resumes it -- shared
    by decide_pending_approval below and the Telegram webhook (see
    /telegram/webhook), which differ only in how they find `row` and what
    they do with the result. Deletes the row so the same approval can't be
    resolved twice; a follow-up ask-tier pause, if any, creates its own
    fresh row through the normal create_pending_approval path."""
    if row["kind"] != "conversation":
        with get_db() as db:
            db.execute("DELETE FROM pending_approvals WHERE id = ?", (row["id"],))
        handler = _APPROVAL_HANDLERS.get(row["kind"])
        message = handler(row, decision) if handler else f"Unknown approval kind {row['kind']!r}."
        return ConversationStepResponse(turn={}, status="done", message=message)
    user_id = row["user_id"]
    with get_db() as db:
        configs = _connected_host_configs(db, user_id)
        system_prompt = _get_or_create_profile(db, user_id).system_prompt
        db.execute("DELETE FROM pending_approvals WHERE id = ?", (row["id"],))
    turn = json.loads(row["turn"])
    read_storage, write_storage = _make_storage_io(user_id)
    ctx = conversations.DispatchContext(
        configs=configs,
        default_host=row["default_host"],
        mock=bool(row["mock"]),
        mock_tier=row["mock_tier"],
        system_prompt=system_prompt,
        read_server_storage=read_storage,
        write_server_storage=write_storage,
        create_pending_approval=lambda description, turn_snapshot: _create_pending_approval_record(
            user_id, description, turn_snapshot, row["default_host"], bool(row["mock"]), row["mock_tier"]
        ),
    )
    return _step_response(turn, decision, ctx)


@app.get("/conversations/pending-approvals", response_model=PendingApprovalListResponse)
def list_pending_approvals(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        rows = db.execute(
            """
            SELECT pa.*, u.username AS requester_username
            FROM pending_approvals pa LEFT JOIN users u ON u.id = pa.requester_user_id
            WHERE pa.user_id = ? ORDER BY pa.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return PendingApprovalListResponse(
        pending_approvals=[
            PendingApprovalInfo(
                id=r["id"],
                description=r["description"],
                created_at=r["created_at"],
                kind=r["kind"],
                requester=r["requester_username"] if r["requester_user_id"] not in (None, user_id) else None,
            )
            for r in rows
        ]
    )


@app.post("/conversations/pending-approvals/{approval_id}/decide", response_model=ConversationStepResponse)
def decide_pending_approval(
    approval_id: str, body: PendingApprovalDecisionRequest, authorization: str = Header(default="")
):
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
        row = db.execute(
            "SELECT * FROM pending_approvals WHERE id = ? AND user_id = ?", (approval_id, user_id)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown pending approval.")
    return _resume_pending_approval(row, body.decision)


def _telegram_send_text(chat_id: str, text: str):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10
        )
    except requests.RequestException as e:
        print(f"Couldn't send Telegram message to chat {chat_id}: {e}")


def _telegram_answer_callback(callback_query_id: str, text: str):
    """Acknowledges a button tap -- shows `text` as a brief toast in the
    Telegram client. Required within a few seconds or the client shows a
    perpetual loading spinner on the tapped button."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Couldn't answer Telegram callback query: {e}")


def _prune_telegram_link_tokens_locked():
    """Caller must hold _telegram_link_lock. An unused link token going
    stale just means the user requests a fresh one -- low-stakes, short-
    lived, no reason to persist these to SQLite the way pending_approvals
    itself needs to be."""
    cutoff = time.time() - _TELEGRAM_LINK_TTL_SECONDS
    stale = [t for t, (_, created_ts) in _telegram_link_tokens.items() if created_ts < cutoff]
    for t in stale:
        del _telegram_link_tokens[t]


@app.post("/telegram/link", response_model=TelegramLinkResponse)
def telegram_link(authorization: str = Header(default="")):
    """Generates a one-time deep-link token -- opening the returned URL
    (or messaging the bot `/start <token>` directly) links the caller's
    Telegram chat to their Casper account, so approval notifications know
    where to go (see _send_approval_telegram) and /telegram/webhook knows
    whose approval a button tap belongs to."""
    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME")
    if not bot_username:
        raise HTTPException(status_code=503, detail="Telegram isn't configured on this deployment.")
    with get_db() as db:
        user_id = _resolve_user_id(db, authorization)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid or missing token.")
    token = secrets.token_urlsafe(16)
    with _telegram_link_lock:
        _prune_telegram_link_tokens_locked()
        _telegram_link_tokens[token] = (user_id, time.time())
    return TelegramLinkResponse(link_url=f"https://t.me/{bot_username}?start={token}")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Telegram's webhook -- covers two things a user does from their
    chat with the bot: completing account linking (`/start <token>`, see
    /telegram/link above) and answering an approval (tapping an inline
    Approve/Deny button, see _send_approval_telegram -- arrives as a
    callback_query, never as plain text, so there's no "which pending
    approval" ambiguity: the button's own callback_data names the exact
    approval_id). X-Telegram-Bot-Api-Secret-Token (set once via Telegram's
    own setWebhook call, matched against TELEGRAM_WEBHOOK_SECRET here) is
    the actual security boundary -- this endpoint has no session auth."""
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=403, detail="Telegram approvals aren't configured on this deployment.")
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        raise HTTPException(status_code=403, detail="Invalid secret token.")

    update = await request.json()

    if "callback_query" in update:
        callback = update["callback_query"]
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        callback_id = callback.get("id", "")
        action, _, approval_id = str(callback.get("data", "")).partition(":")
        if action not in ("approve", "deny") or not approval_id:
            _telegram_answer_callback(callback_id, "Unrecognized action.")
            return {"ok": True}
        with get_db() as db:
            row = db.execute("SELECT * FROM pending_approvals WHERE id = ?", (approval_id,)).fetchone()
            owner = db.execute(
                "SELECT user_id FROM user_profile WHERE telegram_chat_id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            _telegram_answer_callback(callback_id, "That approval is no longer pending.")
            return {"ok": True}
        if owner is None or owner["user_id"] != row["user_id"]:
            _telegram_answer_callback(callback_id, "This isn't your approval to decide.")
            return {"ok": True}
        decision = "allow" if action == "approve" else "deny"
        _resume_pending_approval(row, decision)
        verb = "Approved" if decision == "allow" else "Denied"
        _telegram_answer_callback(callback_id, verb)
        _telegram_send_text(chat_id, f"{verb}: {row['description']}.")
        return {"ok": True}

    if "message" in update:
        message = update["message"]
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = str(message.get("text", "")).strip()
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            token = parts[1] if len(parts) > 1 else ""
            with _telegram_link_lock:
                _prune_telegram_link_tokens_locked()
                entry = _telegram_link_tokens.pop(token, None)
            if entry is None:
                _telegram_send_text(chat_id, "This link has expired -- request a new one from `harness profile telegram-link`.")
                return {"ok": True}
            user_id, _created_ts = entry
            with get_db() as db:
                _get_or_create_profile(db, user_id)  # ensures the row exists before the UPDATE below
                db.execute("UPDATE user_profile SET telegram_chat_id = ? WHERE user_id = ?", (chat_id, user_id))
            _telegram_send_text(chat_id, "Linked! Casper approval requests will show up here -- and you can chat with Casper's guide here any time.")
            return {"ok": True}
        if text:
            # Anything else from a linked chat is a message for Casper's guide.
            with get_db() as db:
                owner = db.execute("SELECT user_id FROM user_profile WHERE telegram_chat_id = ?", (chat_id,)).fetchone()
            if owner is None:
                _telegram_send_text(chat_id, "This chat isn't linked to a Casper account yet -- link it with your agent or `harness profile telegram-link`.")
            else:
                _guide_via_telegram(chat_id, owner["user_id"], text)
        return {"ok": True}

    return {"ok": True}


# --- Profile ---------------------------------------------------------------
# Notification contact info + the "allow chat to configure..." checkboxes
# are plain per-user preferences -- persisted here, but not enforced
# anywhere yet (nothing in conversations.py's tool-calling loop reads
# allow_configure_* today). "Command sets"/"Apps"/"Local agents" don't
# correspond to any existing modeled concept in this codebase the way
# Hosts/Environments do (see casper_service/db.py's hosts/environments
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
        system_prompt=row["system_prompt"],
        telegram_notifications_enabled=bool(row["telegram_notifications_enabled"]),
        telegram_chat_id=row["telegram_chat_id"],
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


# =============================================================================
# v1 platform (docs/product/v1-implementation-plan.md): agent tokens, the
# trust framework (friends, offerings, access requests, grants), daemon-side
# backup support, peer-backup orchestration, and the MCP server.
# =============================================================================
def _require_user(db, authorization: str) -> int:
    user_id = _resolve_user_id(db, authorization)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")
    return user_id


@contextlib.contextmanager
def _trust_errors():
    try:
        yield
    except trust.TrustError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from e


def _host_config(owner_id: int, host_id: int) -> dict | None:
    """The live connection to one identity's pairing on one host, if it's
    connected right now -- how casper_service reaches a daemon AS that
    identity (its command_key), e.g. Sam's pairing on sam-mini when storing
    Riley's backup there."""
    with get_db() as db:
        configs = _connected_host_configs(db, owner_id)
    for label, c in configs.items():
        if c["host_id"] == host_id:
            return {**c, "label": label}
    return None


def _own_configs(user_id: int) -> dict:
    with get_db() as db:
        return _connected_host_configs(db, user_id)


def _refresh_daemon_grants(owner_id: int, host_id: int):
    """Best-effort nudge so a grant change takes effect on the daemon now,
    rather than when its cached grant list next expires (<= 60s)."""
    config = _host_config(owner_id, host_id)
    if config is not None:
        threading.Thread(target=lambda: backups.daemon_call(config, "refresh_grants"), daemon=True).start()


# --- Agent tokens -------------------------------------------------------------
@app.post("/agent-tokens", response_model=AgentTokenCreateResponse, status_code=201)
def create_agent_token(body: AgentTokenCreateRequest, authorization: str = Header(default="")):
    token = "cas_" + secrets.token_urlsafe(32)
    with get_db() as db:
        user_id = _require_user(db, authorization)
        cur = db.execute(
            "INSERT INTO agent_tokens (user_id, name, token_hash) VALUES (?, ?, ?)", (user_id, body.name, hash_token(token))
        )
        row = db.execute("SELECT * FROM agent_tokens WHERE id = ?", (cur.lastrowid,)).fetchone()
    return AgentTokenCreateResponse(id=row["id"], name=row["name"], created_at=row["created_at"], revoked=False, token=token)


@app.get("/agent-tokens", response_model=AgentTokenListResponse)
def list_agent_tokens(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _require_user(db, authorization)
        rows = db.execute("SELECT * FROM agent_tokens WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    return AgentTokenListResponse(
        agent_tokens=[
            AgentTokenInfo(id=r["id"], name=r["name"], created_at=r["created_at"], revoked=r["revoked_at"] is not None)
            for r in rows
        ]
    )


@app.delete("/agent-tokens/{token_id}", response_model=RevokeResponse)
def revoke_agent_token(token_id: int, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _require_user(db, authorization)
        cur = db.execute(
            "UPDATE agent_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (trust.now_iso(), token_id, user_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such active agent token.")
    return RevokeResponse(revoked=True)


def _resolve_agent(authorization: str) -> dict | None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT id, user_id, name FROM agent_tokens WHERE token_hash = ? AND revoked_at IS NULL", (hash_token(token),)
        ).fetchone()
    return {"agent_token_id": row["id"], "user_id": row["user_id"], "name": row["name"]} if row else None


# --- Friends ---------------------------------------------------------------------
@app.post("/friends/requests", status_code=201)
def create_friend_request(body: FriendRequestCreate, authorization: str = Header(default="")):
    with get_db() as db, _trust_errors():
        user_id = _require_user(db, authorization)
        request_id, to_id = trust.create_friend_request(db, user_id, body.username)
        me = trust.username(db, user_id)
    create_approval(to_id, user_id, "friend_request", f"{me} wants to be friends on Casper. Accept?", {"request_id": request_id})
    return {"id": request_id, "status": "pending"}


def _decide_friend_request(row, decision: str) -> str:
    request_id = json.loads(row["payload"])["request_id"]
    with get_db() as db:
        fr = trust.decide_friend_request(db, request_id, decision == "allow")
        if fr is None:
            return "That friend request is no longer pending."
        me, them = trust.username(db, fr["to_user_id"]), trust.username(db, fr["from_user_id"])
    if decision == "allow":
        notify(fr["from_user_id"], f"{me} accepted your friend request.")
        return f"You and {them} are now friends."
    return f"Declined {them}'s friend request."


@app.get("/friends")
def list_friends(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _require_user(db, authorization)
        friends = sorted(trust.username(db, f) for f in trust.friends_of(db, user_id))
        return {"friends": friends, **trust.pending_friend_requests(db, user_id)}


@app.delete("/friends/{username}", response_model=RevokeResponse)
def remove_friend(username: str, authorization: str = Header(default="")):
    with get_db() as db, _trust_errors():
        user_id = _require_user(db, authorization)
        revoked = trust.remove_friend(db, user_id, username)
    for g in revoked:
        _refresh_daemon_grants(g["owner_user_id"], g["host_id"])
        notify(g["grantee_user_id"], f"Your backup space on {g['owner']}/{g['host']} was revoked (friendship ended). Stored backups stay restorable for 7 days.")
    return RevokeResponse(revoked=True)


# --- Offerings, access requests, grants ---------------------------------------
@app.post("/offerings", status_code=201)
def publish_offering(body: OfferingCreateRequest, authorization: str = Header(default="")):
    with get_db() as db, _trust_errors():
        user_id = _require_user(db, authorization)
        offering_id = trust.publish_offering(db, user_id, body.host_id, int(body.max_quota_gb * trust.GB), body.write_tier)
    return {"id": offering_id}


@app.get("/offerings")
def list_offerings(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _require_user(db, authorization)
        return trust.list_offerings(db, user_id)


@app.delete("/offerings/{offering_id}", response_model=RevokeResponse)
def withdraw_offering(offering_id: int, authorization: str = Header(default="")):
    with get_db() as db, _trust_errors():
        user_id = _require_user(db, authorization)
        trust.withdraw_offering(db, user_id, offering_id)
    return RevokeResponse(revoked=True)


def _request_access(user_id: int, offering_id: int, quota_gb: float) -> dict:
    with get_db() as db:
        request_id, offering = trust.create_access_request(db, user_id, offering_id, int(quota_gb * trust.GB))
        me = trust.username(db, user_id)
        label = trust.host_label(db, offering["owner_user_id"], offering["host_id"])
    create_approval(
        offering["owner_user_id"], user_id, "access_request",
        f"{me} is asking for {quota_gb:g} GB of backup space on {label}. Grant it?",
        {"request_id": request_id},
    )
    return {"id": request_id, "status": "pending"}


@app.post("/offerings/{offering_id}/requests", status_code=201)
def request_access(offering_id: int, body: AccessRequestCreate, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _require_user(db, authorization)
    with _trust_errors():
        out = _request_access(user_id, offering_id, body.quota_gb)
    return {"id": out["id"], "status": "pending"}


def _decide_access_request(row, decision: str) -> str:
    request_id = json.loads(row["payload"])["request_id"]
    with get_db() as db:
        ar = trust.decide_access_request(db, request_id, decision == "allow")
        if ar is None:
            return "That request is no longer pending."
        owner, requester = trust.username(db, ar["owner_user_id"]), trust.username(db, ar["requester_user_id"])
        label = trust.host_label(db, ar["owner_user_id"], ar["host_id"])
    size = backups.human(ar["quota_bytes"])
    if decision == "allow":
        _refresh_daemon_grants(ar["owner_user_id"], ar["host_id"])
        notify(ar["requester_user_id"], f"{owner} granted you {size} of backup space on {label}. Back up to it as {owner}/{label}.")
        return f"Granted {requester} {size} on {label}."
    notify(ar["requester_user_id"], f"{owner} declined your request for backup space on {label}.")
    return f"Declined {requester}'s request."


@app.get("/grants")
def list_grants(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _require_user(db, authorization)
        return {"given": trust.grants_given(db, user_id), "held": trust.grants_held(db, user_id)}


@app.delete("/grants/{grant_id}", response_model=RevokeResponse)
def revoke_grant(grant_id: int, authorization: str = Header(default="")):
    """Either side ends it (docs/product/scenarios/peer-backup.md, step
    10). Writes stop at once; the grantee can still restore/delete what's
    stored for 7 days, then the host's daemon purges it."""
    with get_db() as db, _trust_errors():
        user_id = _require_user(db, authorization)
        g = trust.revoke_grant(db, user_id, grant_id)
    _refresh_daemon_grants(g["owner_user_id"], g["host_id"])
    if user_id == g["owner_user_id"]:
        notify(g["grantee_user_id"], f"{g['owner']} revoked your backup space on {g['host']}. Your stored backups stay restorable for 7 days.")
    else:
        notify(g["owner_user_id"], f"{g['grantee']} gave up their backup space on {g['host']}.")
    return RevokeResponse(revoked=True)


# --- Daemon-facing (device-token-gated) backup support --------------------------
@app.get("/hosts/grants")
def host_grants(authorization: str = Header(default="")):
    """Every Backup Peer grant this daemon identity gave on this host --
    what the daemon decides every peer-side request from (see
    agent/internal/config/grants.go)."""
    with get_db() as db:
        attached = _resolve_attached(db, authorization)
        if attached is None:
            raise HTTPException(status_code=401, detail="Invalid or missing device token.")
        return {"grants": trust.grants_on_host_for_daemon(db, attached["user_id"], attached["host_id"])}


@app.post("/hosts/backup-key")
def report_backup_key(body: BackupKeyReport, authorization: str = Header(default="")):
    with get_db() as db:
        attached = _resolve_attached(db, authorization)
        if attached is None:
            raise HTTPException(status_code=401, detail="Invalid or missing device token.")
        db.execute(
            "UPDATE host_pairings SET backup_signing_key = ? WHERE routing_key = ? AND user_id = ?",
            (body.signing_public_key, attached["routing_key"], attached["user_id"]),
        )
        if body.key_storage:
            db.execute(
                "UPDATE host_pairings SET backup_key_storage = ? WHERE routing_key = ? AND user_id = ?",
                (body.key_storage, attached["routing_key"], attached["user_id"]),
            )
    return {"ok": True}


# --- Backup key export (owner's own session only) --------------------------------
@app.post("/hosts/{host_id}/backup-key/export")
def export_backup_key(host_id: int, body: dict, authorization: str = Header(default="")):
    """The recovery fallback for keys that live only in this machine's local
    Keychain: returns them encrypted to a passphrase. Only ever to the
    host's own paired owner."""
    with get_db() as db:
        user_id = _require_user(db, authorization)
    config = _host_config(user_id, host_id)
    if config is None:
        raise HTTPException(status_code=404, detail="That host isn't one of yours, or it's offline.")
    r = backups.daemon_call(config, "backup_export_key", passphrase=str(body.get("passphrase", "")))
    if not r.get("success"):
        raise HTTPException(status_code=400, detail=r.get("stderr") or "Export failed.")
    return json.loads(r["stdout"])


# --- Approval handlers ------------------------------------------------------------
def _decide_shell_command(row, decision: str) -> str:
    """An MCP run_shell_command the owner approved (or not): re-sends the
    identical call with approved=true -- the daemon re-matches it fresh --
    and tells the requester what happened."""
    payload = json.loads(row["payload"])
    requester = row["requester_user_id"] or row["user_id"]
    if decision != "allow":
        notify(requester, f"Denied: {row['description']}.")
        return "Denied."
    configs = _own_configs(row["user_id"])
    tier, output, _ = conversations._dispatch_shell_command(configs, payload["args"], payload["host"], None, False, "allow", approved=True)
    text = _shell_output_text(tier, output)
    notify(requester, f"Approved and ran on {payload['host']}:\n{text[:3500]}")
    return text


_APPROVAL_HANDLERS.update(
    {
        "shell_command": _decide_shell_command,
        "friend_request": _decide_friend_request,
        "access_request": _decide_access_request,
        "backup_write": backups.decide_write,
    }
)


# --- MCP tool implementations --------------------------------------------------------
def _shell_output_text(tier: str, output: str) -> str:
    if tier != "allow":
        return output
    try:
        result = json.loads(output)
    except ValueError:
        return output
    parts = []
    if result.get("stdout"):
        parts.append(result["stdout"])
    if result.get("stderr"):
        parts.append(f"[stderr]\n{result['stderr']}")
    if result.get("exit_code") not in (None, 0):
        parts.append(f"[exit code {result['exit_code']}]")
    return "\n".join(parts) or "(no output)"


def _mcp_list_hosts(user_id: int) -> list[dict]:
    with get_db() as db:
        own = db.execute(
            """
            SELECT h.id, uh.label FROM user_hosts uh JOIN hosts h ON h.id = uh.host_id
            JOIN host_pairings hp ON hp.routing_key = h.routing_key AND hp.user_id = uh.user_id
            WHERE uh.user_id = ? ORDER BY uh.label COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()
        held = trust.grants_held(db, user_id)
        used_by_host = {
            r["dest_host_id"]: r["used"]
            for r in db.execute(
                "SELECT dest_host_id, SUM(total_bytes) AS used FROM backups WHERE owner_user_id = ? AND status = 'complete' GROUP BY dest_host_id",
                (user_id,),
            ).fetchall()
        }
    connected = {c["host_id"] for c in _own_configs(user_id).values()}
    out = [{"host": r["label"], "role": "owner", "connected": r["id"] in connected} for r in own]
    for g in held:
        entry = {
            "host": f"{g['owner']}/{g['host']}",
            "owner": g["owner"],
            "role": g["relation"],
            "quota": backups.human(g["quota_bytes"]),
            "connected": _host_config(g["owner_user_id"], g["host_id"]) is not None,
        }
        if g["relation"] == "backup_peer":
            entry["used"] = backups.human(used_by_host.get(g["host_id"]) or 0)
            entry["approval_needed_to_write"] = g["write_tier"] == "ask"
        out.append(entry)
    return out


def _mcp_run_shell_command(user_id: int, host: str, positional_args: list, options: list, path: str | None) -> str:
    configs = _own_configs(user_id)
    if host not in configs:
        return f"{host} isn't one of your connected hosts. Connected: {', '.join(configs) or 'none'}."
    args = {"positional_args": positional_args, "options": options, "path": path}
    tier, output, resolved = conversations._dispatch_shell_command(configs, args, host, None, False, "allow", approved=False)
    if tier == "ask":
        command = " ".join([positional_args[0] if positional_args else "", conversations._describe_call_args(positional_args, options)]).strip()
        create_approval(user_id, user_id, "shell_command", f"Run `{command}` on {resolved}?", {"host": resolved, "args": args})
        return f"`{command}` on {resolved} needs the owner's approval first. They've been notified; the result will be sent to them once it runs."
    return _shell_output_text(tier, output)


def _mcp_list_offerings(user_id: int) -> list[dict]:
    with get_db() as db:
        offerings = trust.list_offerings(db, user_id)
    return offerings["friends"] + [{**o, "yours": "you offer this"} for o in offerings["mine"]]


def _mcp_request_access(user_id: int, offering_id: int, quota_gb: float) -> str:
    try:
        _request_access(user_id, offering_id, quota_gb)
    except trust.TrustError as e:
        return f"Can't request that: {e.message}"
    return f"Requested {quota_gb:g} GB. The owner has been notified and decides; you'll be told when they do."


backups.configure(
    backups.Deps(
        host_config=_host_config,
        own_configs=_own_configs,
        # Late-bound, so a test (or anything else) replacing main.notify /
        # main.create_approval affects backups too.
        notify=lambda *a, **k: notify(*a, **k),
        create_approval=lambda *a, **k: create_approval(*a, **k),
        run_async=backups.default_run_async(),
    )
)

_mcp_services = mcp_server.Services(
    resolve_agent=_resolve_agent,
    list_hosts=_mcp_list_hosts,
    run_shell_command=_mcp_run_shell_command,
    list_offerings=_mcp_list_offerings,
    request_access=_mcp_request_access,
    backup_push=backups.push,
    backup_status=backups.status,
    backup_list=backups.list_backups,
    backup_restore=backups.restore,
    backup_delete=backups.delete,
)
_mcp = mcp_server.build(_mcp_services)
app.router.add_route("/mcp", mcp_server.asgi_app(_mcp, _mcp_services), methods=["GET", "POST", "DELETE"], include_in_schema=False)


# =============================================================================
# Agent onboarding (docs/product/scenarios/agent-onboarding.md): the ledger,
# offerings/invites/approvals as MCP tools with plain-language previews, the
# approvals-relay rule, undo, and the files/downloads an agent fetches.
# =============================================================================
def _gb(n: int) -> str:
    return backups.human(n)


def _mcp_my_casper(user_id: int) -> str:
    """The ledger (agent-first-ux.md, principle 3): everything this person
    has shared or been given, and anything waiting on them, in plain words."""
    with get_db() as db:
        me = trust.username(db, user_id)
        friends = sorted(trust.username(db, f) for f in trust.friends_of(db, user_id))
        offerings = trust.list_offerings(db, user_id)["mine"]
        invites = trust.open_invites(db, user_id)
        given = trust.grants_given(db, user_id)
        held = trust.grants_held(db, user_id)
        pending = db.execute(
            "SELECT requester_user_id FROM pending_approvals WHERE user_id = ?", (user_id,)
        ).fetchall()
        stored_by_friend = {
            r["owner_user_id"]: r["used"]
            for r in db.execute(
                "SELECT owner_user_id, SUM(total_bytes) AS used FROM backups WHERE dest_owner_user_id = ? AND status = 'complete' GROUP BY owner_user_id",
                (user_id,),
            ).fetchall()
        }
        my_backups = db.execute(
            "SELECT * FROM backups WHERE owner_user_id = ? AND status = 'complete' ORDER BY created_at DESC", (user_id,)
        ).fetchall()
    hosts = [h for h in _mcp_list_hosts(user_id) if h["role"] == "owner"]
    lines = [f"Casper account: {me}"]
    lines.append("Your machines: " + (", ".join(f"{h['host']} ({'online' if h['connected'] else 'offline'})" for h in hosts) or "none paired"))
    lines.append("Friends: " + (", ".join(friends) or "none yet"))
    if offerings:
        lines.append("You offer:")
        for o in offerings:
            if o["kind"] == "mirror_space":
                lines.append(f"  - mirror space: up to {o['max_quota_gb']:g} GB per friend on {o['host']} [offering {o['id']}]")
            elif o["kind"] == "catcher_space":
                lines.append(f"  - catcher space on {o['host']} [offering {o['id']}]")
            else:
                each = "you approve each backup" if o["write_tier"] == "ask" else "any backup within a friend's share is allowed"
                lines.append(f"  - backup space: up to {o['max_quota_gb']:g} GB per friend on {o['host']} ({each}) [offering {o['id']}]")
    if invites:
        lines.append("Open invites (single-use, not yet redeemed):")
        for i in invites:
            lines.append(f"  - {_gb(i['quota_bytes'])}{' for ' + i['note'] if i['note'] else ''}, expires {i['expires_at'][:10]} [invite {i['id']}]")
    kinds = {"backup_peer": "backup", "mirror_peer": "mirror", "catcher_peer": "catcher"}
    if given:
        lines.append("Space you give friends (everything arrives encrypted; you can't read it):")
        for g in given:
            using = f", using {_gb(stored_by_friend.get(g['grantee_user_id'], 0))}" if g["relation"] == "backup_peer" else ""
            lines.append(f"  - {kinds[g['relation']]} space for {g['grantee']}: {_gb(g['quota_bytes'])} on {g['host']}{using} [grant {g['id']}]")
    if held:
        lines.append("Space friends have given you:")
        for g in held:
            lines.append(f"  - {kinds[g['relation']]} space: {_gb(g['quota_bytes'])} on {g['owner']}/{g['host']} [grant {g['id']}]")
    with get_db() as db:
        mirrored = db.execute(
            "SELECT * FROM mirror_folders WHERE owner_user_id = ? AND status IN ('active', 'awaiting_consent')", (user_id,)
        ).fetchall()
        if mirrored:
            lines.append("Mirrored folders:")
            for f in mirrored:
                lines.append("  " + _status_text(db, f).replace("\n", "\n  "))
    if my_backups:
        lines.append("Your backups:")
        for b in my_backups:
            with get_db() as db:
                dest = f"{trust.username(db, b['dest_owner_user_id'])}/{trust.host_label(db, b['dest_owner_user_id'], b['dest_host_id'])}"
            lines.append(f"  - {b['name']} ({_gb(b['total_bytes'])}) on {dest}, {b['completed_at'][:10] if b['completed_at'] else ''} [backup {b['id']}]")
    others = [p for p in pending if p["requester_user_id"] not in (None, user_id)]
    own = len(pending) - len(others)
    if others:
        lines.append(f"Waiting for your decision: {len(others)} request(s) from friends (list_approvals).")
    if own:
        lines.append(f"Waiting for your own approval outside this agent: {own} (Telegram or `harness approvals`).")
    return "\n".join(lines)


def _own_host_id(user_id: int, label: str) -> int | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT h.id FROM user_hosts uh JOIN hosts h ON h.id = uh.host_id
            JOIN host_pairings hp ON hp.routing_key = h.routing_key AND hp.user_id = uh.user_id
            WHERE uh.user_id = ? AND uh.label = ?
            """,
            (user_id, label),
        ).fetchone()
    return row["id"] if row else None


def _mcp_add_friend(user_id: int, username: str) -> str:
    try:
        with get_db() as db:
            request_id, to_id = trust.create_friend_request(db, user_id, username)
            me = trust.username(db, user_id)
    except trust.TrustError as e:
        return f"Can't: {e.message}"
    create_approval(to_id, user_id, "friend_request", f"{me} wants to be friends on Casper. Accept?", {"request_id": request_id})
    return f"Sent {username} a friend request. They'll be asked to accept."


def _mcp_publish_offering(user_id: int, host: str, max_gb: float, approve_each_backup: bool, preview: bool, kind: str = "backup") -> str:
    host_id = _own_host_id(user_id, host)
    if host_id is None:
        return f"{host} isn't one of your paired machines (see list_hosts)."
    if kind in ("mirror", "catcher"):
        return _publish_mirror_offering(user_id, host, host_id, max_gb, kind, preview)
    each = (
        "you'll be asked to approve each backup before it's stored"
        if approve_each_backup
        else "any backup that fits within a friend's share is stored without asking you"
    )
    plan = (
        f"Offer backup space on {host}: friends you invite (or who ask) can each get up to {max_gb:g} GB. "
        f"What they store is encrypted on their own machine first -- you can never read it, and they can't read anything of yours. "
        f"{each[0].upper() + each[1:]}. Casper must be running on {host} for them to use it. You can withdraw this or revoke anyone's space at any time."
    )
    if preview:
        return "PREVIEW (nothing changed yet): " + plan
    try:
        with get_db() as db:
            offering_id = trust.publish_offering(db, user_id, host_id, int(max_gb * trust.GB), "ask" if approve_each_backup else "allow")
    except trust.TrustError as e:
        return f"Can't: {e.message}"
    return f"Done (offering {offering_id}). {plan}"


def _publish_mirror_offering(user_id: int, host: str, host_id: int, max_gb: float, kind: str, preview: bool) -> str:
    if kind == "mirror":
        plan = (
            f"Offer mirror space on {host}: friends you invite can each keep up to {max_gb:g} GB of folders mirrored here, "
            "with 30 days of history. Everything arrives encrypted -- you can never read it, and they can't see anything of yours. "
            f"Nothing is asked in return. {host} needs to be on and running Casper to receive changes; while it's asleep, friends' "
            "changes wait or go to a catcher. You can withdraw this or end anyone's space at any time."
        )
    else:
        plan = (
            f"Offer catcher space on {host} (best on a machine that's always on): when a friend's mirrors are all asleep, their "
            f"newest changes are held here, encrypted, until a mirror wakes up -- usually only megabytes, up to {max_gb:g} GB each. "
            "Nothing is asked in return. You can withdraw this or end anyone's space at any time."
        )
    if preview:
        return "PREVIEW (nothing changed yet): " + plan
    try:
        with get_db() as db:
            offering_id = trust.publish_offering(db, user_id, host_id, int(max_gb * trust.GB), "allow", f"{kind}_space")
    except trust.TrustError as e:
        return f"Can't: {e.message}"
    return f"Done (offering {offering_id}). {plan}"


def _mcp_create_invite(user_id: int, quota_gb: float, offering_id: int | None, for_whom: str, preview: bool) -> str:
    with get_db() as db:
        mine = trust.list_offerings(db, user_id)["mine"]
    if offering_id is None:
        if len(mine) != 1:
            return "Say which offering (offering_id) -- " + ("you have none yet; publish_offering first." if not mine else f"you have {len(mine)}.")
        offering_id = mine[0]["id"]
    offering = next((o for o in mine if o["id"] == offering_id), None)
    if offering is None:
        return "No such offering of yours."
    who = for_whom or "the friend you send it to"
    plan = (
        f"Create a single-use invite giving {who} {quota_gb:g} GB of backup space on {offering['host']}. "
        f"Whoever redeems it becomes your friend on Casper and gets that space; it expires in {trust.INVITE_TTL_DAYS} days and you can cancel it until then."
    )
    if preview:
        return "PREVIEW (nothing changed yet): " + plan
    try:
        with get_db() as db:
            code, _ = trust.create_invite(db, user_id, offering_id, int(quota_gb * trust.GB), for_whom)
            me = trust.username(db, user_id)
    except trust.TrustError as e:
        return f"Can't: {e.message}"
    url = f"{_public_base_url()}/agents.md"
    gift = {
        "mirror_space": f"room for {quota_gb:g} GB of your folders to be mirrored on my computer with Casper -- kept up to date as you work, with 30 days of history",
        "catcher_space": "a catcher on my always-on computer with Casper -- it holds your newest changes while your mirrors are asleep",
    }.get(offering["kind"], f"{quota_gb:g} GB of backup space for you on my computer with Casper")
    message = (
        f"I've set aside {gift} -- your files get encrypted "
        f"before they leave your machine, so I can't read them. To use it, tell your AI agent (e.g. Claude Code): "
        f"\"Set me up with Casper using {url} -- my invite code is {code}\". (From {me}; the code works once and expires in {trust.INVITE_TTL_DAYS} days.)"
    )
    return f"Invite created: {code}\n\nMessage for the person to send {who}:\n{message}"


def _mcp_redeem_invite(user_id: int, code: str, preview: bool) -> str:
    try:
        with get_db() as db:
            inv = trust.find_invite(db, code)
            owner = trust.username(db, inv["owner_user_id"])
            label = trust.host_label(db, inv["owner_user_id"], inv["host_id"])
            already = trust.are_friends(db, user_id, inv["owner_user_id"])
    except trust.TrustError as e:
        return f"Can't use that invite: {e.message}"
    each = "they'll approve each backup before it's stored" if inv["write_tier"] == "ask" else "backups within your share are stored without waiting for them"
    what = {
        "mirror_space": f"{_gb(inv['quota_bytes'])} of mirror space on {owner}'s machine ({owner}/{label}) -- folders you choose are kept there continuously, with 30 days of history",
        "catcher_space": f"catcher space on {owner}'s machine ({owner}/{label}) -- it holds your newest changes while your mirrors sleep",
    }.get(inv["kind"], f"{_gb(inv['quota_bytes'])} of backup space on {owner}'s machine ({owner}/{label}); {each}")
    plan = (
        f"Use {owner}'s invite: {'' if already else f'you and {owner} become friends on Casper, and '}you get {what}. "
        f"Your files are encrypted on your own machine before they're sent, so {owner} can never read them. "
        f"Either of you can end this at any time."
    )
    if preview:
        return "PREVIEW (nothing changed yet): " + plan
    try:
        with get_db() as db:
            trust.redeem_invite(db, user_id, code)
            me = trust.username(db, user_id)
    except trust.TrustError as e:
        return f"Can't use that invite: {e.message}"
    _refresh_daemon_grants(inv["owner_user_id"], inv["host_id"])
    notify(inv["owner_user_id"], f"{me} used your Casper invite: they now have {_gb(inv['quota_bytes'])} of backup space on {label}.")
    if inv["kind"] == "mirror_space":
        return (
            f"Done. You and {owner} are friends, and you have {_gb(inv['quota_bytes'])} of mirror space on {owner}/{label}. "
            f"Next: ask the person which folder to mirror there (mirror_folder). Casper defaults to generosity: suggest the person "
            f"also offer {owner} mirror space in return (publish_offering + create_invite) -- {owner} is free to decline."
        )
    if inv["kind"] == "catcher_space":
        return f"Done. You and {owner} are friends; {owner}/{label} can now catch your changes while your mirrors sleep (use it as the catcher in mirror_folder)."
    return f"Done. {plan} Back up to it as {owner}/{label}."


def _mcp_list_approvals(user_id: int) -> str:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT pa.*, u.username AS requester_username FROM pending_approvals pa
            LEFT JOIN users u ON u.id = pa.requester_user_id
            WHERE pa.user_id = ? ORDER BY pa.created_at
            """,
            (user_id,),
        ).fetchall()
    others = [r for r in rows if r["requester_user_id"] not in (None, user_id)]
    own = len(rows) - len(others)
    if not others:
        text = "Nothing from friends is waiting for a decision."
    else:
        text = "Waiting for the person's decision (ask them; record exactly what they say with decide_approval):\n" + "\n".join(
            f"  - [{r['id']}] from {r['requester_username']}: {r['description']}" for r in others
        )
    if own:
        text += f"\n({own} of the person's own requests also wait for approval; those can only be decided outside this agent, in Telegram or `harness approvals`.)"
    return text


def _mcp_decide_approval(user_id: int, approval_id: str, approve: bool) -> str:
    """Rule of Two (agent-first-ux.md, principle 4): an agent may record its
    person's decision on OTHER people's requests, never approve its own
    person's -- those stay outside the agent."""
    with get_db() as db:
        row = db.execute("SELECT * FROM pending_approvals WHERE id = ? AND user_id = ?", (approval_id, user_id)).fetchone()
    if row is None:
        return "No such pending request."
    if row["requester_user_id"] in (None, user_id):
        return "That's the person's own request; it can't be approved from their agent. They can decide it in Telegram or with `harness approvals`."
    return _resume_pending_approval(row, "allow" if approve else "deny").message or "Done."


def _mcp_revoke(user_id: int, kind: str, id_or_name: str) -> str:
    try:
        with get_db() as db:
            if kind == "grant":
                g = trust.revoke_grant(db, user_id, int(id_or_name))
            elif kind == "invite":
                trust.cancel_invite(db, user_id, int(id_or_name))
                return "Cancelled the invite; the code no longer works."
            elif kind == "offering":
                trust.withdraw_offering(db, user_id, int(id_or_name))
                return "Withdrew the offering: no new invites or requests. Space already given stays until you revoke it (kind='grant')."
            elif kind == "friend":
                revoked = trust.remove_friend(db, user_id, id_or_name)
                for g in revoked:
                    _refresh_daemon_grants(g["owner_user_id"], g["host_id"])
                return f"No longer friends with {id_or_name}" + (f"; also ended {len(revoked)} backup-space arrangement(s) between you." if revoked else ".")
            else:
                return "kind must be one of: grant, invite, offering, friend."
    except (trust.TrustError, ValueError) as e:
        return f"Can't: {getattr(e, 'message', str(e))}"
    _refresh_daemon_grants(g["owner_user_id"], g["host_id"])
    other = g["grantee_user_id"] if user_id == g["owner_user_id"] else g["owner_user_id"]
    notify(other, f"Backup space on {g['owner']}/{g['host']} was ended. Stored backups stay restorable for 7 days.")
    kind = {"backup_peer": "backup", "mirror_peer": "mirror", "catcher_peer": "catcher"}[g["relation"]]
    return f"Ended {g['grantee']}'s {kind} space on {g['host']}. New changes stop now; what's stored stays restorable by its owner for 7 days, then it's deleted."


def _mcp_backup_push(user_id: int, source_host: str, path: str, dest_host: str, preview: bool = False) -> str:
    if not preview:
        return backups.push(user_id, source_host, path, dest_host)
    try:
        backups.resolve_own(user_id, source_host)
        grant, _ = backups.resolve_dest(user_id, dest_host)
    except backups.BackupError as e:
        return f"Can't back up: {e}"
    return (
        f"PREVIEW (nothing changed yet): encrypt {path} on {source_host} with a key only this machine holds, then store the "
        f"encrypted copy on {dest_host} (your share there: {_gb(grant['quota_bytes'])}"
        + ("; its owner approves each backup" if grant["write_tier"] == "ask" else "")
        + f"). {grant['owner']} can never read it. You'll be told when it's done."
    )


# --- Public onboarding files and downloads -----------------------------------------
# Onboarding files and the app change with every deploy; never let a CDN
# edge (Cloudflare, in front of every deployment) serve a stale copy.
_NO_STORE = {"Cache-Control": "no-store"}
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ONBOARDING_DIR = os.environ.get("ONBOARDING_DIR", os.path.join(_REPO_ROOT, "onboarding"))
DIST_DIR = os.environ.get("DIST_DIR", os.path.join(_REPO_ROOT, "dist"))
_public_base: dict = {}


def _public_base_url() -> str:
    if os.environ.get("PUBLIC_DOMAIN"):
        return "https://" + os.environ["PUBLIC_DOMAIN"]
    return _public_base.get("url") or "http://localhost:8100"


@app.middleware("http")
async def _remember_public_base(request: Request, call_next):
    """Onboarding text names this deployment's own URL; learn it from how
    we're actually reached (Cloudflare Tunnel sets X-Forwarded-Proto)."""
    host = request.headers.get("host", "")
    if host and "url" not in _public_base and not host.startswith(("localhost", "127.0.0.1", "testserver")):
        proto = request.headers.get("x-forwarded-proto", "https")
        _public_base["url"] = f"{proto}://{host}"
    return await call_next(request)


def _render_onboarding(text: str, request: Request) -> str:
    host = request.headers.get("host", "localhost:8100")
    proto = request.headers.get("x-forwarded-proto") or ("http" if host.startswith(("localhost", "127.0.0.1", "testserver")) else "https")
    return text.replace("{{CASPER_URL}}", f"{proto}://{host}")


def _onboarding_files() -> list[tuple[str, str]]:
    """(relative path, absolute path) for every onboarding file."""
    out = []
    for root, _dirs, files in os.walk(ONBOARDING_DIR):
        for f in sorted(files):
            if f.startswith(".DS_Store"):
                continue
            full = os.path.join(root, f)
            out.append((os.path.relpath(full, ONBOARDING_DIR), full))
    return out


@app.get("/agents.md", include_in_schema=False)
def onboarding_agents_md(request: Request):
    from fastapi.responses import PlainTextResponse

    with open(os.path.join(ONBOARDING_DIR, "AGENTS.md")) as f:
        return PlainTextResponse(_render_onboarding(f.read(), request), media_type="text/markdown", headers=_NO_STORE)


@app.get("/onboarding/skills/{name}.md", include_in_schema=False)
def onboarding_skill(name: str, request: Request):
    from fastapi.responses import PlainTextResponse

    path = os.path.join(ONBOARDING_DIR, ".claude", "skills", name, "SKILL.md")
    if not name.replace("-", "").isalnum() or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No such skill.")
    with open(path) as f:
        return PlainTextResponse(_render_onboarding(f.read(), request), media_type="text/markdown", headers=_NO_STORE)


@app.get("/onboarding.zip", include_in_schema=False)
def onboarding_zip(request: Request):
    import io
    import zipfile

    from fastapi.responses import Response

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, full in _onboarding_files():
            with open(full) as f:
                z.writestr(os.path.join("casper", rel), _render_onboarding(f.read(), request))
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="casper-onboarding.zip"', **_NO_STORE})


@app.get("/download/casper/macos", include_in_schema=False)
@app.get("/download/casper-macos.zip", include_in_schema=False)
def download_app():
    """The extensionless path is the one to publish: Cloudflare caches
    ".zip" URLs at its edge by default, which served a stale build after a
    release (found in the 2026-09-30 onboarding run). no-store keeps the
    edge from caching either path from now on."""
    from fastapi.responses import FileResponse

    path = os.path.join(DIST_DIR, "Casper-macos.zip")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No macOS build is published on this deployment.")
    return FileResponse(path, media_type="application/zip", filename="Casper-macos.zip", headers=_NO_STORE)


for _name, _fn in {
    "my_casper": _mcp_my_casper,
    "add_friend": _mcp_add_friend,
    "publish_offering": _mcp_publish_offering,
    "create_invite": _mcp_create_invite,
    "redeem_invite": _mcp_redeem_invite,
    "list_approvals": _mcp_list_approvals,
    "decide_approval": _mcp_decide_approval,
    "revoke": _mcp_revoke,
    "backup_push": _mcp_backup_push,
}.items():
    setattr(_mcp_services, _name, _fn)


# =============================================================================
# Mirroring with history (docs/product/scenarios/mirroring.md). Syncthing
# moves the data directly between peers; casper_service is discovery, the
# desired state (from grants), consent, protection status and restores.
# =============================================================================
import mirroring  # noqa: E402


def _attached_or_401(db, authorization: str) -> dict:
    attached = _resolve_attached(db, authorization)
    if attached is None:
        raise HTTPException(status_code=401, detail="Invalid or missing device token.")
    return attached


@app.post("/hosts/mirror-device")
def report_mirror_device(body: dict, authorization: str = Header(default="")):
    with get_db() as db:
        attached = _attached_or_401(db, authorization)
        device_id = str(body.get("device_id", ""))
        if not device_id or len(device_id) > 80:
            raise HTTPException(status_code=400, detail="device_id is required.")
        addresses = [str(a) for a in (body.get("addresses") or []) if isinstance(a, str) and len(a) < 120]
        mirroring.report_device(db, attached["routing_key"], device_id, addresses)
    return {"ok": True}


@app.get("/hosts/mirror-config")
def mirror_config(authorization: str = Header(default="")):
    """This identity's desired mirroring state on this host -- computed
    from grants on every call, so a revoked grant disappears at the
    daemon's next reconcile."""
    with get_db() as db:
        attached = _attached_or_401(db, authorization)
        return mirroring.desired_state(db, attached["user_id"], attached["host_id"])


@app.post("/hosts/mirror-status")
def report_mirror_status(body: dict, authorization: str = Header(default="")):
    with get_db() as db:
        attached = _attached_or_401(db, authorization)
        nudges = mirroring.store_status(db, attached["user_id"], body.get("folders") or [])
    for n in nudges:
        st = n["status"]
        notify(
            attached["user_id"],
            f"{st.get('unprotected_files')} recent change(s) in your {n['folder']['label']} aren't on any mirror yet -- "
            "the computers mirroring it have been off for a while. Nothing to do if they'll be back soon; "
            "otherwise, ask your agent about adding a mirror or a catcher.",
        )
    return {"ok": True}


@app.post("/hosts/consent")
def record_consent(body: dict, authorization: str = Header(default="")):
    """The person's answer from Casper's native dialog on their own Mac --
    the out-of-agent confirmation for their own consequential actions."""
    with get_db() as db:
        attached = _attached_or_401(db, authorization)
        row = db.execute(
            "SELECT * FROM pending_approvals WHERE id = ? AND user_id = ? AND kind = 'mirror_consent'",
            (str(body.get("approval_id", "")), attached["user_id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such pending confirmation.")
    _resume_pending_approval(row, "allow" if body.get("approve") else "deny")
    return {"ok": True}


def _mirror_refresh(user_id: int, host_id: int):
    config = _host_config(user_id, host_id)
    if config is not None:
        threading.Thread(target=lambda: backups.daemon_call(config, "mirror_refresh"), daemon=True).start()


def _refresh_folder_parties(folder) -> None:
    _mirror_refresh(folder["owner_user_id"], folder["owner_host_id"])
    if folder["restore_host_id"]:
        _mirror_refresh(folder["owner_user_id"], folder["restore_host_id"])
    with get_db() as db:
        targets = mirroring.peers_of(db, folder["id"])
    for t in targets:
        _mirror_refresh(t["peer_owner_user_id"], t["peer_host_id"])


def _decide_mirror_consent(row, decision: str) -> str:
    folder_id = json.loads(row["payload"])["folder_id"]
    with get_db() as db:
        f = mirroring.folder_row(db, folder_id)
        if f is None or f["status"] != "awaiting_consent":
            return "That's no longer waiting."
        if decision != "allow":
            db.execute("UPDATE mirror_folders SET status = 'declined', stopped_at = ? WHERE id = ?", (trust.now_iso(), folder_id))
            return f"Didn't start mirroring {f['label']}."
        db.execute("UPDATE mirror_folders SET status = 'active', activated_at = ? WHERE id = ?", (trust.now_iso(), folder_id))
        f = mirroring.folder_row(db, folder_id)
        me = trust.username(db, f["owner_user_id"])
        targets = mirroring.peers_of(db, folder_id)
        hosts = [trust.host_label(db, t["peer_owner_user_id"], t["peer_host_id"]) for t in targets]
    _refresh_folder_parties(f)
    for t in targets:
        notify(t["peer_owner_user_id"], f"{me}'s {f['label']} is now mirrored on your Mac, encrypted -- you can't read it. Thanks for keeping a copy safe.")
    where = ", ".join(hosts) or ("Casper's catcher" if f["use_casper_catcher"] else "no one yet")
    return f"Mirroring {f['label']} to {where}. Changes reach them within seconds whenever both computers are on."


_APPROVAL_HANDLERS["mirror_consent"] = _decide_mirror_consent


def _folder_size(user_id: int, host_label: str, path: str) -> tuple[dict, int, str]:
    configs = _own_configs(user_id)
    if host_label not in configs:
        raise mirroring.MirrorError(f"{host_label} isn't one of your connected machines.")
    config = {**configs[host_label], "label": host_label}
    r = backups.daemon_call(config, "mirror_folder_size", path=path)
    if not r.get("success"):
        raise mirroring.MirrorError(r.get("stderr") or "couldn't read that folder")
    data = json.loads(r["stdout"])
    return config, int(data["bytes"]), data["path"]


def _mcp_mirror_folder(user_id: int, source_host: str, path: str, mirrors: list, catcher: str, use_casper_catcher: bool, label: str, preview: bool) -> str:
    try:
        config, size, abs_path = _folder_size(user_id, source_host, path)
        with get_db() as db:
            mirror_grants = mirroring.resolve_peer_grants(db, user_id, mirrors or [], "mirror_peer")
            catcher_grant = mirroring.resolve_peer_grants(db, user_id, [catcher], "catcher_peer")[0] if catcher else None
            for g in mirror_grants:
                free = g["quota_bytes"] - mirroring.used_bytes(db, g["id"])
                if size > free:
                    raise mirroring.MirrorError(
                        f"{os.path.basename(abs_path)} is {backups.human(size)}, but your space on {g['owner']}/{g['host']} has {backups.human(max(free, 0))} free."
                    )
            if not mirror_grants:
                raise mirroring.MirrorError("Name at least one friend's machine to mirror to (list_hosts shows the machines you have mirror space on).")
            if catcher_grant and catcher_grant["host_id"] in {g["host_id"] for g in mirror_grants}:
                raise mirroring.MirrorError(
                    f"{catcher_grant['owner']}/{catcher_grant['host']} is already one of the mirrors. A catcher only helps on a "
                    "different machine (it holds changes while the mirrors are asleep), so choose another catcher or none."
                )
    except mirroring.MirrorError as e:
        return f"Can't: {e.message}"
    label = label or os.path.basename(abs_path.rstrip("/")) or "Folder"
    if use_casper_catcher and catcher_grant:
        use_casper_catcher = False
    where = ", ".join(f"{g['owner']}/{g['host']}" for g in mirror_grants)
    catch = (
        f" While they're all asleep, new changes are caught by {catcher_grant['owner']}/{catcher_grant['host']}." if catcher_grant
        else " While they're all asleep, new changes are caught by Casper's own server (encrypted; only until a friend can catch for you)." if use_casper_catcher
        else " If they're all asleep at once, recent changes wait on this Mac until one wakes up."
    )
    plan = (
        f"Mirror {label} ({backups.human(size)}) from {source_host} to {where}: every change is encrypted here and reaches them within "
        f"seconds while both computers are on, with 30 days of history (every version for a week, then daily). They can never read it.{catch}"
    )
    if preview:
        return "PREVIEW (nothing changed yet): " + plan
    with get_db() as db:
        folder_id = mirroring.create_folder(db, user_id, config["host_id"], abs_path, label, mirror_grants, catcher_grant, use_casper_catcher)
    description = f"Start mirroring {label} ({backups.human(size)}) to {where}?"
    approval_id = create_approval(user_id, user_id, "mirror_consent", description, {"folder_id": folder_id})
    with get_db() as db:
        db.execute("UPDATE mirror_folders SET approval_id = ? WHERE id = ?", (approval_id, folder_id))
    backups.daemon_call(config, "ask_consent", approval_id=approval_id, text=f"{plan}\n\nStart mirroring?")
    return (
        f"Waiting for the person to confirm. A Casper dialog is open on {source_host} (it can also be answered in Telegram). "
        "This confirmation can't come from you -- it's the person's own decision. Mirroring starts the moment they allow it."
    )


def _status_text(db, f) -> str:
    st = mirroring.latest_status(db, f["id"]) or {}
    names = {}
    for t in mirroring.peers_of(db, f["id"]):
        dev = mirroring._device_for_host(db, t["peer_host_id"])
        if dev:
            names[dev["device_id"]] = f"{trust.username(db, t['peer_owner_user_id'])}/{trust.host_label(db, t['peer_owner_user_id'], t['peer_host_id'])}"
    cd = mirroring.catcher_device()
    if cd:
        names[cd["device_id"]] = "Casper's catcher"
    if f["status"] == "awaiting_consent":
        return f"{f['label']}: waiting for the person to confirm (Casper dialog or Telegram)."
    if f["restore_host_id"] and st.get("restoring"):
        state = "restored" if st.get("need_files") == 0 and st.get("state") == "idle" and st.get("local_files") else "restoring"
        return f"{f['label']}: {state} on {trust.host_label(db, f['owner_user_id'], f['restore_host_id'])} -- {st.get('local_files', 0)} files so far."
    if not st:
        return f"{f['label']}: starting up (no report from {trust.host_label(db, f['owner_user_id'], f['owner_host_id'])} yet)."
    lines = []
    unprotected = st.get("unprotected_files", 0)
    if unprotected:
        import time as _t

        age = int((_t.time() - st.get("unprotected_oldest_unix", _t.time())) / 60)
        lines.append(f"{f['label']}: {unprotected} recent change(s) not on any mirror or catcher yet (oldest {age} min).")
        files = _unprotected_names(f)
        if files:
            more = f" (and {unprotected - min(len(files), 10)} more)" if unprotected > min(len(files), 10) else ""
            lines.append("  not yet protected: " + ", ".join(files[:10]) + more)
    elif st.get("at_risk_files"):
        lines.append(f"{f['label']}: protected -- {st['at_risk_files']} recent change(s) are held by the catcher until a mirror wakes up.")
    else:
        lines.append(f"{f['label']}: protected -- every change is on at least one mirror.")
    for p in st.get("peers", []):
        who = names.get(p["device_id"], p["device_id"][:7])
        state = "up to date" if p.get("completion") == 100 and p.get("connected") else ("connected, catching up" if p.get("connected") else f"offline (last seen {(p.get('last_seen') or '?')[:16]})")
        lines.append(f"  - {p['role']}: {who}: {state}")
    lines.append(f"  ({st.get('local_files', 0)} files, {backups.human(st.get('local_bytes', 0))}; as of {st.get('reported_at', '')[:19]})")
    return "\n".join(lines)


def _unprotected_names(f) -> list[str]:
    """Asked live from the owner's own Mac and passed straight to the
    person's agent -- never stored here (file names stay off the server)."""
    config = _host_config(f["owner_user_id"], f["owner_host_id"])
    if config is None:
        return []
    r = backups.daemon_call(config, "mirror_unprotected_files", folder_id=f["id"])
    if not r.get("success"):
        return []
    try:
        return json.loads(r["stdout"]).get("files") or []
    except ValueError:
        return []


def _mcp_protection_status(user_id: int) -> str:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM mirror_folders WHERE owner_user_id = ? AND status IN ('active', 'awaiting_consent') ORDER BY created_at", (user_id,)
        ).fetchall()
        if not rows:
            return "Nothing is mirrored yet."
        return "\n".join(_status_text(db, f) for f in rows)


def _version_listing(user_id: int, folder_name: str) -> tuple:
    with get_db() as db:
        f = mirroring.find_owned(db, user_id, folder_name)
        targets = [t for t in mirroring.peers_of(db, f["id"]) if t["role"] == "mirror"]
    owner = _host_config(user_id, f["owner_host_id"])
    if owner is None:
        raise mirroring.MirrorError("Your Mac that owns this folder is offline; versions are decrypted there.")
    for t in targets:
        peer = _host_config(t["peer_owner_user_id"], t["peer_host_id"])
        if peer is None:
            continue
        r = backups.daemon_call(peer, "mirror_version_trailers", folder_id=f["id"])
        if not r.get("success"):
            continue
        items = json.loads(r["stdout"])["versions"]
        d = backups.daemon_call(owner, "mirror_decrypt_trailers", folder_id=f["id"], items=items)
        if not d.get("success"):
            raise mirroring.MirrorError(d.get("stderr") or "couldn't read the version list")
        return f, owner, peer, json.loads(d["stdout"])["versions"]
    raise mirroring.MirrorError("None of the computers mirroring this folder is online right now; versions live on them.")


def _mcp_list_versions(user_id: int, folder: str, name_contains: str) -> str:
    try:
        f, _, _, versions = _version_listing(user_id, folder)
    except mirroring.MirrorError as e:
        return f"Can't: {e.message}"
    versions = [v for v in versions if name_contains.lower() in v["name"].lower()]
    if not versions:
        return "No earlier versions match." if name_contains else "No earlier versions yet (they appear when files change or are deleted)."
    lines = [f"Earlier versions in {f['label']} (newest first; restore with restore_version(folder, name, at)):"]
    for v in sorted(versions, key=lambda v: v["at"], reverse=True)[:100]:
        lines.append(f"  - {v['name']}  at={v['at']}  ({backups.human(v['size'])}{', deleted afterwards' if v.get('deleted') else ''})")
    return "\n".join(lines)


def _mcp_restore_version(user_id: int, folder: str, name: str, at: str) -> str:
    try:
        f, owner, peer, versions = _version_listing(user_id, folder)
    except mirroring.MirrorError as e:
        return f"Can't: {e.message}"
    v = next((v for v in versions if v["name"] == name and (v["at"] == at or at in ("", "latest"))), None)
    if v is None:
        return f"No version of {name!r} at {at!r} (use list_versions)."
    offset, total = 0, None
    while total is None or offset < total:
        r = backups.daemon_call(peer, "mirror_read_version", folder_id=f["id"], encrypted_path=v["encrypted_path"], at=v["at"], offset=offset)
        if not r.get("success"):
            return f"Can't fetch that version: {r.get('stderr')}"
        data = json.loads(r["stdout"])
        total = int(data["total"])
        w = backups.daemon_call(owner, "mirror_restore_version_chunk", folder_id=f["id"], encrypted_path=v["encrypted_path"], offset=offset, content=data["content"])
        if not w.get("success"):
            return f"Can't restore: {w.get('stderr')}"
        chunk = len(base64.b64decode(data["content"]))
        if chunk == 0:
            break
        offset += chunk
    done = backups.daemon_call(owner, "mirror_restore_version_finish", folder_id=f["id"], encrypted_path=v["encrypted_path"])
    if not done.get("success"):
        return f"Can't restore: {done.get('stderr')}"
    return f"Restored {name} as it was at {v['at']} to {json.loads(done['stdout'])['restored_to']} -- a new copy; nothing was overwritten."


def _mcp_restore_folder(user_id: int, folder: str, dest_host: str, dest_path: str) -> str:
    """A replacement (or second) Mac rebuilds a mirrored folder from the
    mirrors, plus the catcher's gap. The Mac needs the folder's password
    first: `Casper setup restore` imports it from the recovery kit."""
    with get_db() as db:
        try:
            f = mirroring.find_owned(db, user_id, folder)
        except mirroring.MirrorError as e:
            return f"Can't: {e.message}"
    configs = _own_configs(user_id)
    if dest_host not in configs:
        return f"Can't: {dest_host} isn't one of your connected machines."
    host_id = configs[dest_host]["host_id"]
    if host_id == f["owner_host_id"]:
        return "That's the Mac the folder already lives on; use restore_version for individual files."
    path = dest_path or f"~/Casper Restores/{f['label']}"
    home = configs[dest_host].get("cwd") or ""
    abs_path = path.replace("~", home, 1) if path.startswith("~") and home else path
    with get_db() as db:
        db.execute("UPDATE mirror_folders SET restore_host_id = ?, restore_path = ? WHERE id = ?", (host_id, abs_path, f["id"]))
        f = mirroring.folder_row(db, f["id"])
    _refresh_folder_parties(f)
    return (
        f"Restoring {f['label']} onto {dest_host} at {abs_path} from its mirrors"
        + (" and catcher" if f["use_casper_catcher"] or _has_catcher(f["id"]) else "")
        + ". This needs the folder's password on that Mac (from the recovery kit: `Casper setup restore`). Check progress with protection_status."
    )


def _has_catcher(folder_id: str) -> bool:
    with get_db() as db:
        return any(t["role"] == "catcher" for t in mirroring.peers_of(db, folder_id))


def _mcp_stop_mirroring(user_id: int, folder: str) -> str:
    with get_db() as db:
        try:
            f = mirroring.find_owned(db, user_id, folder)
        except mirroring.MirrorError as e:
            return f"Can't: {e.message}"
        db.execute("UPDATE mirror_folders SET status = 'stopped', stopped_at = ? WHERE id = ?", (trust.now_iso(), f["id"]))
    _refresh_folder_parties(f)
    return f"Stopped mirroring {f['label']}. The copies on your friends' computers are deleted after 7 days (until then they can still be restored)."


for _name, _fn in {
    "mirror_folder": _mcp_mirror_folder,
    "protection_status": _mcp_protection_status,
    "list_versions": _mcp_list_versions,
    "restore_version": _mcp_restore_version,
    "restore_folder": _mcp_restore_folder,
    "stop_mirroring": _mcp_stop_mirroring,
}.items():
    setattr(_mcp_services, _name, _fn)

mirroring.start_catcher_loop()


# =============================================================================
# Casper's own guide (casper_service/guide.py) -- the onboarding agent for
# people without one, on the web (after sign-in) and in Telegram.
# =============================================================================
import guide  # noqa: E402

_guide_locks: dict[int, threading.Lock] = {}
_guide_locks_guard = threading.Lock()


def _guide_lock(user_id: int) -> threading.Lock:
    with _guide_locks_guard:
        return _guide_locks.setdefault(user_id, threading.Lock())


def _guide_reply(user_id: int, text: str) -> tuple[str, list[dict]]:
    """One turn with the guide, serialized per person (web and Telegram
    share the conversation)."""
    if not os.environ.get("NOUS_API_KEY"):
        return "Casper's guide isn't available on this deployment yet.", []
    prompt = guide.system_prompt(
        os.environ.get("GUIDE_DOWNLOAD_URL", "https://www.casperagent.dev/download"),
        os.environ.get("GUIDE_SIGNIN_URL", "https://app.casperagent.dev/signin"),
    )
    with _guide_lock(user_id):
        history = guide.load_history(user_id)
        before = len(history)
        try:
            reply, messages = guide.run(guide.client_from_env(), os.environ.get("GUIDE_MODEL", "deepseek/deepseek-v4.1-flash"),
                                        _mcp_services, user_id, history, text[:4000], prompt)
        except Exception as e:  # the model provider being down shouldn't 500 the page
            print(f"guide failed for user {user_id}: {e}")
            return "Sorry -- I'm having trouble thinking right now. Please try again in a minute.", []
        guide.save_new(user_id, before, messages)
    return reply, messages


@app.post("/guide/message")
def guide_message(body: dict, authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _require_user(db, authorization)
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Say something.")
    reply, _ = _guide_reply(user_id, text)
    return {"reply": reply}


@app.get("/guide/history")
def guide_history(authorization: str = Header(default="")):
    with get_db() as db:
        user_id = _require_user(db, authorization)
    return {"messages": guide.visible(guide.load_history(user_id))}


def _guide_via_telegram(chat_id: str, user_id: int, text: str):
    def work():
        reply, _ = _guide_reply(user_id, text)
        _telegram_send_text(chat_id, reply[:4000])

    threading.Thread(target=work, daemon=True).start()
