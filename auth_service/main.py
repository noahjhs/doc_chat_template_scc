import hashlib
import secrets
import threading
import time
from datetime import datetime, timezone

import bcrypt
from db import get_db, init_db
from fastapi import FastAPI, Header, HTTPException, Request
from models import (
    AuthResponse,
    LoginRequest,
    RevokeResponse,
    SignupRequest,
    VerifyResponse,
)

app = FastAPI()
init_db()


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
