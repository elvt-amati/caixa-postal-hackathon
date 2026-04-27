"""Auth module — OIDC login + session JWT. Drop-in for most OIDC providers.

Configured via env vars (all optional, disabled when CAIXA_AUTH_ENABLED != "true"):
    CAIXA_AUTH_ENABLED=true|false     (default: false — anonymous user)
    CAIXA_AUTH_OIDC_ISSUER            (e.g., https://accounts.google.com)
    CAIXA_AUTH_OIDC_CLIENT_ID
    CAIXA_AUTH_OIDC_CLIENT_SECRET
    CAIXA_AUTH_OIDC_SCOPES            (default: "openid email profile")
    CAIXA_AUTH_JWT_SECRET             (HMAC secret for session JWT; falls back to CLIENT_SECRET)
    CAIXA_AUTH_COOKIE_NAME            (default: "caixa_session")
    CAIXA_AUTH_ALLOWED_EMAILS         (optional whitelist, comma-separated)
    CAIXA_PUBLIC_URL                  (your public base URL, e.g., https://caixa.example.com;
                                       needed so OIDC callback URI matches registered redirect)

End-to-end design:
    user browser → /auth/login
       → OIDC provider → /auth/callback
       → we verify id_token, issue OUR session JWT
       → cookie set; plus the id_token kept server-side for MCP passthrough
    Every request hits require_user() dependency, which validates the session JWT.
    The original id_token is exposed to MCP servers via CAIXA_USER_TOKEN env var.
"""
from __future__ import annotations

import json
import os
import time
import base64
import hmac
import hashlib
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from fastapi import Cookie, HTTPException, Request, Response
from fastapi.responses import RedirectResponse


AUTH_ENABLED = os.environ.get("CAIXA_AUTH_ENABLED", "false").lower() == "true"
ISSUER = os.environ.get("CAIXA_AUTH_OIDC_ISSUER", "").rstrip("/")
CLIENT_ID = os.environ.get("CAIXA_AUTH_OIDC_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CAIXA_AUTH_OIDC_CLIENT_SECRET", "")
SCOPES = os.environ.get("CAIXA_AUTH_OIDC_SCOPES", "openid email profile")
JWT_SECRET = os.environ.get("CAIXA_AUTH_JWT_SECRET") or (CLIENT_SECRET or secrets.token_urlsafe(32))
COOKIE_NAME = os.environ.get("CAIXA_AUTH_COOKIE_NAME", "caixa_session")
PUBLIC_URL = os.environ.get("CAIXA_PUBLIC_URL", "").rstrip("/")
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("CAIXA_AUTH_ALLOWED_EMAILS", "").split(",") if e.strip()}

_OIDC_CONFIG_CACHE: dict = {}
_STATE_STORE: dict[str, dict] = {}  # transient CSRF/state → {nonce, created_at}
# Keep id_tokens accessible to current worker for MCP passthrough
_TOKEN_STORE: dict[str, str] = {}   # user_id → id_token


@dataclass
class User:
    id: str
    email: str
    name: str
    picture: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name, "picture": self.picture}


ANONYMOUS = User(id="anon", email="anon@local", name="Anônimo", picture="")

# Pseudonymous session — when real auth is disabled we still want each browser
# to get its own partition in DynamoDB instead of all 30 participants sharing
# `user_id=anon`. Cookie is long-lived (30 days), httpOnly, regenerated if
# missing. Fake id format: ``guest-<6 hex>`` — short enough to read in logs,
# opaque enough to not leak PII.
GUEST_COOKIE_NAME = "caixa_guest"
GUEST_COOKIE_MAX_AGE = 30 * 24 * 3600


def _read_guest_cookie(req: Request) -> Optional[str]:
    val = req.cookies.get(GUEST_COOKIE_NAME)
    if not val or not val.startswith("guest-") or len(val) > 24:
        return None
    return val


def new_guest_id() -> str:
    return "guest-" + secrets.token_hex(6)


def guest_user(uid: str) -> User:
    return User(id=uid, email=f"{uid}@guest.local", name=f"Convidado {uid[-6:]}", picture="")


# -----------------------------------------------------------------------------
# Tiny JWT helpers (HS256) — stdlib only, no PyJWT dependency
# -----------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def jwt_encode(payload: dict, secret: str = JWT_SECRET) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


def jwt_decode(token: str, secret: str = JWT_SECRET) -> dict:
    header, body, sig = token.split(".")
    expected = _b64url(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad signature")
    payload = json.loads(_b64url_decode(body))
    if payload.get("exp") and payload["exp"] < int(time.time()):
        raise ValueError("expired")
    return payload


# -----------------------------------------------------------------------------
# OIDC discovery + exchange
# -----------------------------------------------------------------------------


def _get_oidc_config() -> dict:
    if _OIDC_CONFIG_CACHE:
        return _OIDC_CONFIG_CACHE
    if not ISSUER:
        raise RuntimeError("CAIXA_AUTH_OIDC_ISSUER not set")
    url = f"{ISSUER}/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=10) as r:
        cfg = json.loads(r.read())
    _OIDC_CONFIG_CACHE.update(cfg)
    return cfg


def _http_post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _parse_id_token(id_token: str) -> dict:
    # Trust the provider's id_token payload here (verified by HTTPS + audience check below).
    # For production you'd verify signature against JWKS; keep simple for hackathon.
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("bad id_token format")
    payload = json.loads(_b64url_decode(parts[1]))
    if payload.get("aud") != CLIENT_ID and CLIENT_ID not in (payload.get("aud") or []):
        raise ValueError("aud mismatch")
    if payload.get("exp") and payload["exp"] < int(time.time()):
        raise ValueError("id_token expired")
    return payload


# -----------------------------------------------------------------------------
# Flask-less FastAPI routes
# -----------------------------------------------------------------------------


def _redirect_uri(req: Request) -> str:
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/auth/callback"
    # derive from request
    scheme = req.headers.get("x-forwarded-proto", req.url.scheme)
    host = req.headers.get("x-forwarded-host") or req.headers.get("host", "")
    return f"{scheme}://{host}/auth/callback"


def login(req: Request) -> RedirectResponse:
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=302)
    cfg = _get_oidc_config()
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    _STATE_STORE[state] = {"nonce": nonce, "created_at": int(time.time())}
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": _redirect_uri(req),
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
    }
    return RedirectResponse(f"{cfg['authorization_endpoint']}?{urllib.parse.urlencode(params)}", status_code=302)


def callback(req: Request) -> RedirectResponse:
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=302)
    code = req.query_params.get("code")
    state = req.query_params.get("state")
    if not code or not state or state not in _STATE_STORE:
        raise HTTPException(400, "invalid callback — code/state missing or expired")
    _STATE_STORE.pop(state, None)

    cfg = _get_oidc_config()
    tokens = _http_post(cfg["token_endpoint"], {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(req),
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(400, "no id_token returned")
    claims = _parse_id_token(id_token)

    email = (claims.get("email") or "").lower()
    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        raise HTTPException(403, f"{email} não autorizado")

    user_id = claims.get("sub") or email
    _TOKEN_STORE[user_id] = id_token  # cached for MCP passthrough

    # Issue our session JWT (12h)
    session_jwt = jwt_encode({
        "sub": user_id,
        "email": email,
        "name": claims.get("name", email),
        "picture": claims.get("picture", ""),
        "iat": int(time.time()),
        "exp": int(time.time()) + 12 * 3600,
    })

    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        COOKIE_NAME, session_jwt,
        httponly=True, secure=req.url.scheme == "https", samesite="lax", max_age=12 * 3600, path="/",
    )
    return resp


def logout() -> RedirectResponse:
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


def current_user(req: Request) -> User:
    """Extract user from cookie. Falls back to ANONYMOUS when auth disabled or cookie missing."""
    if not AUTH_ENABLED:
        return ANONYMOUS
    cookie = req.cookies.get(COOKIE_NAME)
    if not cookie:
        return ANONYMOUS  # callers decide whether to 401
    try:
        c = jwt_decode(cookie)
        return User(id=c["sub"], email=c.get("email", ""), name=c.get("name", ""), picture=c.get("picture", ""))
    except Exception:
        return ANONYMOUS


def require_user(req: Request) -> User:
    """Dependency that 401s when auth is enabled but user is anon."""
    u = current_user(req)
    if AUTH_ENABLED and u.id == "anon":
        raise HTTPException(401, "não autenticado — /auth/login")
    return u


def user_id_token(user_id: str) -> Optional[str]:
    """Provided to MCP spawner. When auth disabled, returns None → MCP runs without user token."""
    if not AUTH_ENABLED or user_id == "anon":
        return None
    return _TOKEN_STORE.get(user_id)
