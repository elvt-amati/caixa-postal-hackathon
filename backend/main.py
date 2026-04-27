"""FastAPI app: AG-UI agent endpoint + ops dashboard + briefing + static frontend."""
import os

os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"] = "all"

from pathlib import Path

import asyncio
import contextvars
import json
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import boto3

import auth

from ag_ui_strands import StrandsAgent, add_strands_fastapi_endpoint

from registry import AgentRegistry, build_concierge
from briefing import briefing_prompt
from transcribe_util import transcribe
import mcp_loader
from store import (
    list_by_category, clear_user,
    update_item as store_update, delete_item as store_delete,
    undo_delete as store_undo, get_item as store_get,
    save_thread_message, load_thread_messages, clear_thread,
    save_chat_public, load_chat_public,
    save_chat_dm, load_chat_dm,
    current_user_id as _current_user_id,
    _table as _ddb_table,
)
from pitch_deck import load_pitch, render_deck_html
from tools import TOOLS_BY_NAME

REGION = os.environ.get("AWS_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ.get("CAIXA_COGNITO_USER_POOL_ID", "")

app = FastAPI(title="Caixa Postal Universal")


@app.on_event("startup")
def _on_startup():
    # Default asyncio executor is `min(32, cpu+4)` = 5 on a 1-vCPU task.
    # Our per-request work (Bedrock Converse, Transcribe polling, DDB
    # writes) runs in that executor via _run_in_thread, so 5 inflight
    # users per task fully saturates it and later streams stall behind
    # a FIFO queue until ALB idle-timeout (60s) terminates them.
    # Bump to 64 so a single task can carry ~60 concurrent inflight
    # requests — matches our ~30 user × 2 active-stream budget across
    # 3 tasks with plenty of headroom.
    loop = asyncio.get_event_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=64, thread_name_prefix="caixa"))


@app.on_event("shutdown")
def _on_shutdown():
    # P1.13: stop MCP subprocess clients so they don't zombie when the container reloads.
    mcp_loader.shutdown_all()


# Transcribe is heavy (60s timeout polling Bedrock Transcribe). Cap concurrent
# transcribe jobs per task so a voice-note stampede can't starve the /agent
# pool. Size = ~1/4 of thread pool so chat always has room.
_TRANSCRIBE_SEM = asyncio.Semaphore(12)


# ---------------------------------------------------------------------------
# P0.8 Rate limiter — in-memory token bucket per (client_ip, path-bucket).
# Not multi-worker-safe (use Redis for prod), but saves us from trivial DoS
# and accidental cost attacks during the hackathon. Limits scoped per endpoint
# family so the chat stream isn't starved by transcribe polling.
# ---------------------------------------------------------------------------
_RL_WINDOW_S = 60
_RL_LIMITS = {
    "/agent": 30,            # 30 req/min per IP for agent streams (≥ 1 msg / 2 s)
    "/api/transcribe": 15,   # 15 audio uploads / min / IP
    "/api/ops": 60,          # 60/min caps the 5s-jittered home-card polling (12/min base + burst)
    "/api/threads": 60,      # chat history polling on agent switch
    "/api/briefing": 10,     # daily summary shouldn't be hammered
    # Event chat on /chat. GET happens every 3s per open tab; 60/min
    # per-cookie covers both public + one DM thread open at a time.
    # POST goes through the same bucket; 60/min is way above any
    # realistic human typing cadence.
    "/api/chat": 60,
}
_RL_STATE: dict[tuple[str, str], deque[float]] = defaultdict(deque)
# Cap the state map so unique-IP churn (NAT, bots, scrapers) can't leak
# memory over the event. Stale keys are swept when the cap is exceeded.
_RL_MAX_KEYS = 20_000


def _rl_check(client_key: str, bucket: str, limit: int) -> tuple[bool, int]:
    now = time.time()
    key = (client_key, bucket)
    dq = _RL_STATE[key]
    cutoff = now - _RL_WINDOW_S
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= limit:
        retry = int(_RL_WINDOW_S - (now - dq[0]))
        return False, max(1, retry)
    dq.append(now)
    # Lazy eviction: if the map blew past the cap, drop any bucket whose
    # deque has gone fully stale. Cheap enough to run occasionally.
    if len(_RL_STATE) > _RL_MAX_KEYS:
        stale = [k for k, v in _RL_STATE.items() if not v or v[-1] < cutoff]
        for k in stale:
            _RL_STATE.pop(k, None)
    return True, 0


@app.middleware("http")
async def rate_limit_middleware(req: Request, call_next):
    path = req.url.path
    bucket = None
    for prefix, _ in _RL_LIMITS.items():
        if path.startswith(prefix):
            bucket = prefix
            break
    if bucket:
        # Key per authenticated user when available — fair across NAT-shared
        # venue wifi — falls back to client IP for anonymous sessions.
        user = auth.current_user(req) if auth.AUTH_ENABLED else auth.ANONYMOUS
        if user.id != "anon":
            client_key = f"u:{user.id}"
        else:
            client_key = "ip:" + (
                (req.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
                or (req.client.host if req.client else "unknown")
            )
        ok, retry = _rl_check(client_key, bucket, _RL_LIMITS[bucket])
        if not ok:
            return JSONResponse(
                {"error": "rate limit excedido", "retry_after_s": retry},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
    return await call_next(req)

registry = AgentRegistry()

# -----------------------------------------------------------------------------
# /agent — Concierge on AgentCore Runtime
# -----------------------------------------------------------------------------
# The concierge now runs on Bedrock AgentCore Runtime (server lives in
# ../agentcore_runtime/server.py). We accept the same AG-UI RunAgentInput
# shape the frontend has always sent, forward it to InvokeAgentRuntime via
# boto3, and stream the SSE body straight through. ECS is just a thin
# authz + SSE-proxy layer for the concierge path now.
#
# /agent/{name} specialist endpoints stay local for this iteration — they
# don't sit behind Runtime because they're only invoked transitively via the
# Concierge's `call_specialist` tool, which the Runtime concierge uses via
# its Gateway-backed tool catalog.
AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")

if AGENTCORE_RUNTIME_ARN:
    # Proxy to AgentCore Runtime (production: concierge hosted externally)
    _agentcore = boto3.client("bedrock-agentcore", region_name=REGION)

    @app.post("/agent")
    async def agent_proxy(req: Request):
        payload = await req.body() or b"{}"
        try:
            body = json.loads(payload.decode("utf-8"))
        except Exception:
            body = {"prompt": ""}
        user = getattr(req.state, "user", None)
        if user is not None:
            body.setdefault("user_id", user.id)
            state = body.setdefault("state", {})
            if isinstance(state, dict):
                state.setdefault("user_id", user.id)

        def _stream():
            resp = _agentcore.invoke_agent_runtime(
                agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
                contentType="application/json",
                accept="text/event-stream",
                payload=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )
            body_stream = resp.get("response")
            if body_stream is None:
                return
            for chunk in body_stream.iter_chunks():
                if chunk:
                    yield chunk

        return StreamingResponse(_stream(), media_type="text/event-stream")
else:
    # Local mode: run concierge via Strands directly (no AgentCore Runtime)
    _concierge_agent = build_concierge(registry)
    _concierge_agui = StrandsAgent(
        agent=_concierge_agent,
        name="concierge",
        description="Concierge orchestrator",
    )
    add_strands_fastapi_endpoint(app, _concierge_agui, "/agent")


# Specialists still mounted locally — the Concierge inside Runtime doesn't
# need them directly (it uses Gateway tools), but the frontend has per-specialist
# chat tabs that expect /agent/{name}. Keep these until the UI drops them.
for _name, _spec in registry.specialists.items():
    _agui_spec = StrandsAgent(
        agent=_spec,
        name=_name,
        description=registry.specialist_meta[_name].get("description", ""),
    )
    add_strands_fastapi_endpoint(app, _agui_spec, f"/agent/{_name}")


@app.get("/healthz")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@app.get("/auth/login")
def auth_login(req: Request):
    # Serve custom PT-BR login page instead of redirecting to English Hosted UI
    login_page = Path(__file__).parent.parent / "frontend" / "login.html"
    if login_page.exists():
        return FileResponse(str(login_page))
    return auth.login(req)


@app.post("/auth/signin")
async def auth_signin(req: Request):
    """Direct Cognito USER_PASSWORD_AUTH — avoids English Hosted UI."""
    from botocore.exceptions import ClientError
    body = await req.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    if not email or not password:
        return JSONResponse({"ok": False, "message": "E-mail e senha obrigatorios"})
    try:
        cog = boto3.client("cognito-idp", region_name=REGION)
        resp = cog.initiate_auth(
            ClientId=auth.CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )
        result = resp.get("AuthenticationResult")
        if not result:
            challenge = resp.get("ChallengeName", "")
            if challenge == "NEW_PASSWORD_REQUIRED":
                return JSONResponse({"ok": False, "message": "Troca de senha necessaria. Contate um organizador."})
            return JSONResponse({"ok": False, "message": f"Desafio nao suportado: {challenge}"})
        id_token = result["IdToken"]
        claims = auth._parse_id_token(id_token)
        user_id = claims.get("sub") or email
        auth._TOKEN_STORE[user_id] = id_token
        session_jwt = auth.jwt_encode({
            "sub": user_id,
            "email": email,
            "name": claims.get("name", email),
            "picture": claims.get("picture", ""),
            "iat": int(time.time()),
            "exp": int(time.time()) + 12 * 3600,
        })
        out = JSONResponse({"ok": True})
        out.set_cookie(
            auth.COOKIE_NAME, session_jwt,
            httponly=True, secure=True, samesite="lax", max_age=12 * 3600, path="/",
        )
        return out
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "NotAuthorizedException":
            return JSONResponse({"ok": False, "message": "E-mail ou senha incorretos"})
        if code == "UserNotConfirmedException":
            return JSONResponse({"ok": False, "error": "confirm_required", "message": "Confirme seu e-mail primeiro"})
        return JSONResponse({"ok": False, "message": e.response["Error"].get("Message", str(e))})


@app.post("/auth/signup")
async def auth_signup(req: Request):
    """Register new user via Cognito, auto-confirm, and return session cookie."""
    from botocore.exceptions import ClientError
    body = await req.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    name = body.get("name", "").strip() or email.split("@")[0]
    if not email or not password:
        return JSONResponse({"ok": False, "message": "E-mail e senha obrigatorios"})
    try:
        cog = boto3.client("cognito-idp", region_name=REGION)
        cog.sign_up(
            ClientId=auth.CLIENT_ID,
            Username=email,
            Password=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "name", "Value": name},
            ],
        )
        # Auto-confirm — no email verification needed at the hackathon.
        if COGNITO_USER_POOL_ID:
            try:
                cog.admin_confirm_sign_up(
                    UserPoolId=COGNITO_USER_POOL_ID,
                    Username=email,
                )
            except ClientError:
                # If admin confirm fails we still return ok=True; user can login
                # after manual confirmation.
                return JSONResponse({"ok": True, "auto_login": False})
        else:
            # No pool ID configured — fall back to old flow (email verification).
            return JSONResponse({"ok": True, "auto_login": False})
        # Auto-login: initiate auth and set session cookie.
        try:
            resp = cog.initiate_auth(
                ClientId=auth.CLIENT_ID,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
            result = resp.get("AuthenticationResult")
            if not result:
                return JSONResponse({"ok": True, "auto_login": False})
            id_token = result["IdToken"]
            claims = auth._parse_id_token(id_token)
            user_id = claims.get("sub") or email
            auth._TOKEN_STORE[user_id] = id_token
            session_jwt = auth.jwt_encode({
                "sub": user_id,
                "email": email,
                "name": claims.get("name", name),
                "picture": claims.get("picture", ""),
                "iat": int(time.time()),
                "exp": int(time.time()) + 12 * 3600,
            })
            out = JSONResponse({"ok": True, "auto_login": True})
            out.set_cookie(
                auth.COOKIE_NAME, session_jwt,
                httponly=True, secure=True, samesite="lax", max_age=12 * 3600, path="/",
            )
            return out
        except ClientError:
            return JSONResponse({"ok": True, "auto_login": False})
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"].get("Message", "")
        if code == "UsernameExistsException":
            return JSONResponse({"ok": False, "message": "Esse e-mail ja esta cadastrado"})
        if code == "InvalidPasswordException":
            if "uppercase" in msg.lower():
                return JSONResponse({"ok": False, "message": "Senha precisa ter ao menos 1 letra maiuscula"})
            if "number" in msg.lower() or "numeric" in msg.lower():
                return JSONResponse({"ok": False, "message": "Senha precisa ter ao menos 1 numero"})
            return JSONResponse({"ok": False, "message": "Senha fraca — minimo 8 caracteres com maiuscula e numero"})
        return JSONResponse({"ok": False, "message": msg or str(e)})


@app.post("/auth/confirm")
async def auth_confirm(req: Request):
    """Confirm email with verification code."""
    from botocore.exceptions import ClientError
    body = await req.json()
    email = body.get("email", "").strip().lower()
    code = body.get("code", "").strip()
    if not email or not code:
        return JSONResponse({"ok": False, "message": "E-mail e codigo obrigatorios"})
    try:
        cog = boto3.client("cognito-idp", region_name=REGION)
        cog.confirm_sign_up(
            ClientId=auth.CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
        )
        return JSONResponse({"ok": True})
    except ClientError as e:
        code_err = e.response["Error"]["Code"]
        if code_err == "CodeMismatchException":
            return JSONResponse({"ok": False, "message": "Codigo incorreto"})
        if code_err == "ExpiredCodeException":
            return JSONResponse({"ok": False, "message": "Codigo expirado — solicite um novo"})
        return JSONResponse({"ok": False, "message": e.response["Error"].get("Message", str(e))})


@app.get("/auth/callback")
def auth_callback(req: Request):
    return auth.callback(req)


@app.get("/auth/logout")
def auth_logout():
    return auth.logout()


@app.get("/api/me")
def api_me(req: Request):
    u = auth.current_user(req)
    return {"user": u.to_dict(), "auth_enabled": auth.AUTH_ENABLED, "authenticated": u.id != "anon"}


# Auth middleware: protect /agent* and /api/* (except /api/me, /api/healthz) when auth is enabled.
# ALSO binds the active user_id into the store.current_user_id ContextVar so tools
# and store functions automatically scope their reads/writes to the user (P0.3).
@app.middleware("http")
async def auth_middleware(req: Request, call_next):
    # Three possible user sources:
    #   1. AUTH_ENABLED + valid Cognito session cookie → real user.
    #   2. Guest cookie present → pseudo-user persisted across visits (30d).
    #   3. Neither → mint a guest id now, attach cookie on the response.
    # This gives every visitor their own DynamoDB partition without login
    # friction, so at an event the shared live URL doesn't collapse 30
    # people into a single `anon` partition (GAP 1 of pre-event review).
    # Resolve identity: Cognito session → guest cookie → mint new guest.
    # This chain works whether AUTH_ENABLED is true or false. Cognito
    # users get their sub; anonymous visitors still get guest cookies
    # so chat DMs and /api/ops have per-browser isolation.
    new_guest_id: str | None = None
    if auth.AUTH_ENABLED:
        user = auth.current_user(req)
    else:
        user = auth.ANONYMOUS
    # If still anon (auth disabled OR auth enabled but no session), use guest cookie
    if user.id == "anon":
        existing = auth._read_guest_cookie(req)
        if existing:
            user = auth.guest_user(existing)
        else:
            new_guest_id = auth.new_guest_id()
            user = auth.guest_user(new_guest_id)
    token = _current_user_id.set(user.id)
    def _maybe_set_guest_cookie(resp):
        if new_guest_id:
            resp.set_cookie(
                auth.GUEST_COOKIE_NAME, new_guest_id,
                httponly=True,
                secure=req.headers.get("x-forwarded-proto", req.url.scheme) == "https",
                samesite="lax",
                max_age=auth.GUEST_COOKIE_MAX_AGE,
                path="/",
            )
        return resp

    try:
        if not auth.AUTH_ENABLED:
            req.state.user = user
            return _maybe_set_guest_cookie(await call_next(req))
        path = req.url.path
        # Open paths: healthz, auth flow, static assets, user info, the app
        # itself, event chat (has its own guest-cookie identity), and all
        # GET pages (challenge, keynote, etc).
        OPEN = ("/healthz", "/auth/", "/static/", "/api/me", "/favicon.ico",
                "/api/chat/", "/api/ops", "/api/agents", "/api/briefing",
                "/api/validate-secret", "/api/connections/", "/api/submissions",
                "/desafio", "/variacoes", "/keynote", "/cheatsheet",
                "/chat", "/ops")
        if any(path.startswith(p) for p in OPEN) or path == "/":
            req.state.user = user
            return _maybe_set_guest_cookie(await call_next(req))
        if user.id == "anon":
            if path.startswith("/api/") or path.startswith("/agent"):
                return JSONResponse({"error": "nao autenticado — faca login em /auth/login", "login": "/auth/login"}, status_code=401)
            return RedirectResponse("/auth/login", status_code=302)
        req.state.user = user
        return _maybe_set_guest_cookie(await call_next(req))
    finally:
        _current_user_id.reset(token)


# ---------------------------------------------------------------------------
# Thread persistence endpoints
# ---------------------------------------------------------------------------


class ThreadMsg(BaseModel):
    role: str
    html: str
    text: str | None = None
    attachments: list | None = None


# Very small allowlist sanitiser. Regex not a parser — but we do:
#   1) strip <script>...</script> blocks entirely (incl. contents).
#   2) strip on-* event handler attributes (`onerror=`, `onclick=`...).
#   3) strip `javascript:` URL handlers.
# This blocks the stored-XSS path without forcing a full HTML parser into
# the Lambda image. Callers that want rich formatting still render the
# assistant's Bedrock output client-side where DOMPurify already runs.
import re as _re
_XSS_SCRIPT = _re.compile(r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>", _re.I | _re.S)
_XSS_SCRIPT_OPEN = _re.compile(r"<\s*/?\s*script\b[^>]*>", _re.I)
_XSS_ON_ATTR = _re.compile(r"""\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", _re.I)
_XSS_JSURL = _re.compile(r"""(href|src|xlink:href)\s*=\s*(['"]?)\s*javascript:""", _re.I)


def _sanitize_html(html: str) -> str:
    s = _XSS_SCRIPT.sub("", html or "")
    s = _XSS_SCRIPT_OPEN.sub("", s)
    s = _XSS_ON_ATTR.sub("", s)
    s = _XSS_JSURL.sub(r"\1=\2about:blank", s)
    return s[:16384]  # bound payload length so one message can't DoS the UI


@app.get("/api/threads/{agent}")
def api_thread_load(agent: str, req: Request):
    u = auth.current_user(req)
    msgs = load_thread_messages(u.id, agent, limit=200)
    return {"user_id": u.id, "agent": agent, "messages": msgs}


@app.post("/api/threads/{agent}")
def api_thread_append(agent: str, body: ThreadMsg, req: Request):
    u = req.state.user  # resolved by middleware; supports guest+Cognito.
    safe_html = _sanitize_html(body.html)
    saved = save_thread_message(u.id, agent, body.role, html=safe_html, text=body.text)
    return {"ok": True, "message": saved}


@app.delete("/api/threads/{agent}")
def api_thread_clear(agent: str, req: Request):
    u = auth.current_user(req)
    n = clear_thread(u.id, agent)
    return {"ok": True, "deleted": n}


class TranscribeResp(BaseModel):
    text: str


_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB — generous for mobile-captured audio
_ALLOWED_AUDIO_EXT = {"mp3", "mp4", "wav", "flac", "ogg", "webm", "m4a"}


async def _run_in_thread(fn, *args, **kwargs):
    """Run a blocking callable in a worker thread while propagating the
    ContextVar state (current_user_id etc.) so tools/store see the right user.
    Python 3.9+ has `asyncio.to_thread` but it doesn't forward contextvars —
    we wrap with copy_context().run to be explicit.
    """
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: ctx.run(fn, *args, **kwargs))


@app.post("/api/transcribe", response_model=TranscribeResp)
async def api_transcribe(audio: UploadFile = File(...)):
    # P1.14: bound the upload so a rogue client can't OOM the task.
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty audio")
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(
            413,
            f"audio muito grande: {len(data)} bytes (max {_MAX_AUDIO_BYTES})",
        )
    ctype = (audio.content_type or "").lower()
    fmt = (audio.filename or "").rsplit(".", 1)[-1].lower() or "webm"
    if fmt not in _ALLOWED_AUDIO_EXT and not ctype.startswith("audio/"):
        raise HTTPException(415, f"formato não suportado: {fmt or ctype}")
    # Non-blocking acquire so voice-note stampedes degrade gracefully with
    # a clear Retry-After instead of eating thread-pool slots chat needs.
    try:
        await asyncio.wait_for(_TRANSCRIBE_SEM.acquire(), timeout=2.0)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "transcribe busy", "retry_after_s": 10},
            status_code=503,
            headers={"Retry-After": "10"},
        )
    try:
        # P0.9: transcribe() polls Bedrock/Transcribe synchronously; push off the event loop.
        text = await _run_in_thread(transcribe, data, media_format=fmt)
    except Exception as e:
        raise HTTPException(500, f"transcribe failed: {e}")
    finally:
        _TRANSCRIBE_SEM.release()
    return TranscribeResp(text=text)


@app.get("/api/briefing")
async def api_briefing():
    """On-demand morning briefing generated by the Agenda specialist.

    P0.9: agent.run() synchronously invokes Bedrock. Push it off the event loop
    so concurrent requests aren't serialized. ContextVar state (user_id) is
    propagated via copy_context so the specialist sees the correct user's items.
    """
    text = await _run_in_thread(registry.call, "agenda", briefing_prompt())
    return {"text": text}


@app.get("/api/agents")
def api_agents():
    """Exposed so the frontend can show the list of available agents + their card catalog."""
    out = {}
    for name, meta in registry.specialist_meta.items():
        cfg = registry.config.get(name, {})
        out[name] = {**meta, "cards": cfg.get("cards", [])}
    # Core pseudo-agent: the canonical category cards (backward compat for pinning)
    out["__core__"] = {
        "display_name": "Caixa (núcleo)",
        "emoji": "📬",
        "description": "Cards canônicos por categoria.",
        "cards": [
            {"key": "task",     "template": "kanban",   "title": "Tarefas",    "source": {"cat": "task"}},
            {"key": "payment",  "template": "calendar", "title": "Pagamentos", "source": {"cat": "payment"}},
            {"key": "reminder", "template": "list",     "title": "Lembretes",  "source": {"cat": "reminder"}},
            {"key": "contact",  "template": "list",     "title": "Contatos",   "source": {"cat": "contact"}},
            {"key": "note",     "template": "list",     "title": "Notas",      "source": {"cat": "note"}},
            {"key": "email",    "template": "list",     "title": "Emails",     "source": {"cat": "email"}},
            {"key": "pitch",    "template": "list",     "title": "Pitches",    "source": {"cat": "pitch"}},
        ],
    }
    return {"agents": out}


@app.get("/api/ops")
def api_ops():
    cats = ["task", "payment", "reminder", "contact", "note", "email", "pitch"]
    return {c: list_by_category(c, limit=40) for c in cats}


@app.get("/api/pitch/{pitch_id}")
def api_pitch(pitch_id: str):
    pitch = load_pitch(pitch_id)
    if not pitch:
        raise HTTPException(404, "pitch not found")
    return HTMLResponse(content=render_deck_html(pitch))


# ---------------------------------------------------------------------------
# Event chat — /chat page + /api/chat endpoints (polling, cookie-based name)
# ---------------------------------------------------------------------------
# One public room + per-pair DMs, no auth beyond a chosen display name that
# the client stores in localStorage. DynamoDB TTL of 48h sweeps everything
# after the event. Only serves on the canonical Elevata stack — participant
# forks redirect /chat to us (same mechanism as /desafio).

_CHAT_NAME_RE = _re.compile(r"^[\w\-\. ]{1,24}$")
_CHAT_TEXT_MAX = 1000


class ChatPostBody(BaseModel):
    name: str
    text: str
    image: str | None = None  # base64 data URL for pasted screenshots


def _validate_chat(body: ChatPostBody) -> tuple[str, str] | None:
    name = (body.name or "").strip()
    text = (body.text or "").strip()
    if not name or not _CHAT_NAME_RE.match(name):
        return None
    if not text or len(text) > _CHAT_TEXT_MAX:
        return None
    return name, text


# ---------------------------------------------------------------------------
# BeautyCore connection — participants test JWT auth against the company API
# ---------------------------------------------------------------------------
BEAUTYCORE_COGNITO_CLIENT_ID = os.environ.get(
    "BEAUTYCORE_CLIENT_ID", "15t4b9mabb36mknj2uvudtd5ul"
)
BEAUTYCORE_API_URL = os.environ.get(
    "BEAUTYCORE_API_URL", "https://i3nc4271ve.execute-api.us-east-1.amazonaws.com"
)


class BCLoginBody(BaseModel):
    email: str
    password: str


@app.post("/api/connections/beautycore/login")
def bc_login(body: BCLoginBody):
    """Authenticate against BeautyCore Cognito, return JWT + decoded groups."""
    import urllib.request as _ur
    try:
        cognito = boto3.client("cognito-idp", region_name=REGION)
        resp = cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=BEAUTYCORE_COGNITO_CLIENT_ID,
            AuthParameters={"USERNAME": body.email, "PASSWORD": body.password},
        )
        tokens = resp.get("AuthenticationResult", {})
        id_token = tokens.get("IdToken", "")
        access_token = tokens.get("AccessToken", "")
        # Decode claims from id_token (unverified — just for display)
        import base64 as _b64
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(_b64.urlsafe_b64decode(payload_b64))
        groups = claims.get("cognito:groups", [])
        if isinstance(groups, str):
            groups = groups.strip("[]").replace(",", " ").split()
        # Quick test: call /produtos to verify the token works
        test_req = _ur.Request(
            f"{BEAUTYCORE_API_URL}/produtos?categoria=batom",
            headers={"Authorization": f"Bearer {id_token}"},
        )
        try:
            with _ur.urlopen(test_req, timeout=5) as r:
                test_data = json.loads(r.read())
                test_ok = True
                test_count = test_data.get("total", 0)
        except Exception as e:
            test_ok = False
            test_count = 0

        return {
            "ok": True,
            "email": body.email,
            "groups": groups,
            "id_token": id_token[:50] + "...",  # truncated for display
            "full_id_token": id_token,  # for copying
            "access_token": access_token[:50] + "...",
            "api_url": BEAUTYCORE_API_URL,
            "cognito_client_id": BEAUTYCORE_COGNITO_CLIENT_ID,
            "test": {"ok": test_ok, "batons": test_count},
        }
    except cognito.exceptions.NotAuthorizedException:
        return JSONResponse({"ok": False, "error": "Email ou senha incorretos"}, status_code=401)
    except cognito.exceptions.UserNotFoundException:
        return JSONResponse({"ok": False, "error": "Usuario nao encontrado"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/connections/beautycore/info")
def bc_info():
    """Public info about the BeautyCore connection (no auth needed)."""
    return {
        "api_url": BEAUTYCORE_API_URL,
        "cognito_client_id": BEAUTYCORE_COGNITO_CLIENT_ID,
        "users": [
            {"email": "admin@beautycore.com.br", "group": "admin", "desc": "CRUD total"},
            {"email": "analista@beautycore.com.br", "group": "analista", "desc": "leitura"},
            {"email": "estoque@beautycore.com.br", "group": "estoque", "desc": "inventario"},
        ],
    }


@app.get("/api/chat/public")
def api_chat_public_get(since: int = 0):
    """Return public room messages newer than ``since`` (ms since epoch).

    Each message carries ``guest_id`` alongside ``name`` so the frontend
    can map name → id for DM initiation without guesswork.
    """
    return {"messages": load_chat_public(since_ms=since, limit=200)}


@app.post("/api/chat/public")
def api_chat_public_post(body: ChatPostBody, req: Request):
    v = _validate_chat(body)
    if not v:
        raise HTTPException(400, "nome e texto obrigatorios")
    _, text = v
    user = getattr(req.state, "user", auth.ANONYMOUS)
    # Use Cognito name if logged in; fall back to body.name for guest/anon
    name = user.name if (user.id != "anon" and user.name) else body.name
    guest_id = user.id
    image = None
    if body.image and body.image.startswith("data:image/") and len(body.image) < 750_000:
        image = body.image
    return {"ok": True, "message": save_chat_public(guest_id, name, _sanitize_html(text), image=image)}


# ---------------------------------------------------------------------------
# Submissions — structured hackathon entries
# ---------------------------------------------------------------------------
# pk=SUBMISSIONS, sk=TS#<ms>#<id>
# Each submission records participant name, track (base/beautycore/bonus), URL,
# timestamp, and the submitter's guest_id for deduplication.

class SubmissionBody(BaseModel):
    name: str
    track: str   # base | beautycore | bonus
    url: str


_VALID_TRACKS = {"base", "beautycore", "bonus"}


@app.post("/api/submissions")
def api_submissions_post(body: SubmissionBody, req: Request):
    name = (body.name or "").strip()
    track = (body.track or "").strip().lower()
    url = (body.url or "").strip()
    if not name or not url:
        raise HTTPException(400, "name e url obrigatorios")
    if track not in _VALID_TRACKS:
        raise HTTPException(400, f"track invalido: {track}")
    user = getattr(req.state, "user", auth.ANONYMOUS)
    guest_id = user.id
    now_ms = int(time.time() * 1000)
    item = {
        "pk": "SUBMISSIONS",
        "sk": f"TS#{now_ms:013d}#{uuid.uuid4().hex[:8]}",
        "name": name,
        "track": track,
        "url": url,
        "ts": now_ms,
        "guest_id": guest_id,
    }
    _ddb_table.put_item(Item=item)
    return {"ok": True, "submission": item}


@app.get("/api/submissions")
def api_submissions_get():
    from boto3.dynamodb.conditions import Key as _DKey
    resp = _ddb_table.query(
        KeyConditionExpression=_DKey("pk").eq("SUBMISSIONS"),
        ScanIndexForward=True,
    )
    raw = resp.get("Items", [])
    # json-safe: convert Decimal
    from decimal import Decimal as _Dec
    def _safe(v):
        if isinstance(v, _Dec):
            return float(v) if v % 1 else int(v)
        if isinstance(v, dict):
            return {k: _safe(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_safe(x) for x in v]
        return v
    return {"submissions": [_safe(x) for x in raw]}


# ---------------------------------------------------------------------------
# Jailbreak bonus — server-side secret validation
# ---------------------------------------------------------------------------
# The secret word is hashed so it never appears in client-side code or the
# starter repo. Only our demo's Runtime system prompt has it.
import hashlib as _hashlib
_SECRET_HASH = "e1356fc19e338f60e1890ca8ca3aca065ff7e61c6f8612efa6f4d5288ae77ed2"


class SecretGuess(BaseModel):
    guess: str
    name: str = "anon"


@app.post("/api/validate-secret")
def api_validate_secret(body: SecretGuess, req: Request):
    guess = (body.guess or "").strip().lower()
    correct = _hashlib.sha256(guess.encode()).hexdigest() == _SECRET_HASH
    # Post to public chat so everyone sees attempts
    guest_id = getattr(req.state, "user", auth.ANONYMOUS).id
    if correct:
        save_chat_public(guest_id, body.name,
                         f"🏆 JAILBREAK! {body.name} descobriu a palavra secreta!")
    else:
        save_chat_public(guest_id, body.name,
                         f"🔒 Tentativa: \"{guess}\" — errado")
    return {"correct": correct}


@app.get("/api/chat/dm/{peer_id}")
def api_chat_dm_get(peer_id: str, req: Request, since: int = 0):
    """Return the DM thread between ``me`` (from cookie) and ``peer_id``.

    Identity is validated server-side from the ``caixa_guest`` cookie,
    not from a user-supplied query param. You can only read DMs that
    belong to YOUR cookie's guest_id.
    """
    my_id = req.state.user.id
    return {"messages": load_chat_dm(my_id, peer_id, since_ms=since, limit=200)}


@app.post("/api/chat/dm/{peer_id}")
def api_chat_dm_post(peer_id: str, body: ChatPostBody, req: Request):
    v = _validate_chat(body)
    if not v:
        raise HTTPException(400, "nome e texto obrigatorios")
    _, text = v
    user = getattr(req.state, "user", auth.ANONYMOUS)
    name = user.name if (user.id != "anon" and user.name) else body.name
    my_id = user.id
    if peer_id == my_id:
        raise HTTPException(400, "nao da pra mandar DM pra si mesmo")
    image = None
    if body.image and body.image.startswith("data:image/") and len(body.image) < 750_000:
        image = body.image
    return {"ok": True, "message": save_chat_dm(
        sender_id=my_id, sender_name=name,
        recipient_id=peer_id, recipient_name="",
        text=_sanitize_html(text), image=image,
    )}


@app.post("/api/ops/reset")
def api_reset(req: Request):
    # Scope to the active user only — never a table-wide wipe. The auth
    # middleware binds current_user_id before this handler runs, so
    # clear_user() picks it up automatically.
    u = auth.current_user(req)
    n = clear_user(u.id)
    return {"deleted": n, "user_id": u.id}


class PatchBody(BaseModel):
    fields: dict


# Whitelist per category — mirrors CANONICAL_FIELDS in tools.py.
# Keep in sync. P0.4: prevents the HTTP PATCH endpoint from overwriting system fields
# (deleted_at, user_id, pk/sk) or injecting arbitrary attributes.
_PATCH_ALLOWED = {
    "task":     {"title", "notes", "due_date", "status"},
    "payment":  {"description", "amount", "due_date", "payee", "status"},
    "reminder": {"title", "at", "notes"},
    "contact":  {"name", "phone", "email", "notes"},
    "note":     {"title", "body"},
    "email":    {"to", "subject", "body", "status"},
}


@app.patch("/api/item/{category}/{sk:path}")
def api_patch_item(category: str, sk: str, body: PatchBody):
    allowed = _PATCH_ALLOWED.get(category)
    if allowed is None:
        raise HTTPException(400, f"categoria desconhecida: {category}")
    clean = {k: v for k, v in (body.fields or {}).items() if k in allowed}
    if not clean:
        raise HTTPException(400, "nenhum campo válido. permitidos: " + ", ".join(sorted(allowed)))
    updated = store_update(category, sk, clean)
    if not updated:
        raise HTTPException(404, "item not found")
    return {"ok": True, "item": updated, "updated_fields": sorted(clean.keys())}


@app.delete("/api/item/{category}/{sk:path}")
def api_delete_item(category: str, sk: str, req: Request):
    # Always soft-delete via this endpoint. The tool-side path can still do
    # hard deletes via the `delete_item` tool (agent-initiated, inside the
    # user's own partition). Exposing ?hard=true over HTTP would let a
    # participant permanently purge any item whose sk they can guess.
    # `req` is accepted so FastAPI anchors the current_user_id ContextVar
    # in this sync handler via starlette's threadpool copy_context.
    _ = req
    res = store_delete(category, sk, hard=False)
    return res


@app.post("/api/item/{category}/{sk:path}/undo")
def api_undo_item(category: str, sk: str):
    restored = store_undo(category, sk)
    if not restored:
        raise HTTPException(404, "item not found")
    return {"ok": True, "item": restored}


@app.get("/api/item/{category}/{sk:path}")
def api_get_item(category: str, sk: str):
    it = store_get(category, sk)
    if not it:
        raise HTTPException(404, "item not found")
    return it


# Static frontend
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/ops")
    def ops_page():
        return FileResponse(str(FRONTEND_DIR / "ops.html"))

    # Event-material pages (challenge briefing, deck, cheatsheet, variations)
    # are the curated, canonical content Savio's deck points at. When a
    # participant deploys their own fork of this repo to their lab account,
    # hitting /desafio there should bounce them back to our authoritative
    # CloudFront URL so everyone reads the same instructions and so
    # future edits don't fragment across copies.
    #
    # The backend detects "forked host" by comparing the request's Host /
    # X-Forwarded-Host against CAIXA_CANONICAL_HOST (defaults to our CF
    # domain). Our own ECS tasks sit behind that CF distribution, so the
    # header matches there and the FileResponse is served directly; any
    # other host 302s to the canonical version.
    CANONICAL_HOST = os.environ.get(
        "CAIXA_CANONICAL_HOST", "d168tci1ssss8v.cloudfront.net"
    )

    def _serve_or_bounce(req: Request, filename: str, route_path: str):
        host = (req.headers.get("x-forwarded-host")
                or req.headers.get("host", "")
                or "").split(":")[0].lower()
        if CANONICAL_HOST and host != CANONICAL_HOST.lower():
            return RedirectResponse(
                f"https://{CANONICAL_HOST}{route_path}", status_code=302
            )
        return FileResponse(str(FRONTEND_DIR / filename))

    @app.get("/desafio")
    def challenge_page(req: Request):
        return _serve_or_bounce(req, "challenge.html", "/desafio")

    @app.get("/variacoes")
    def variations_page(req: Request):
        return _serve_or_bounce(req, "variations.html", "/variacoes")

    @app.get("/keynote")
    def keynote_page(req: Request):
        return _serve_or_bounce(req, "keynote.html", "/keynote")

    @app.get("/cheatsheet")
    def cheatsheet_page(req: Request):
        return _serve_or_bounce(req, "cheatsheet.html", "/cheatsheet")

    @app.get("/chat")
    def chat_page(req: Request):
        return _serve_or_bounce(req, "chat.html", "/chat")
