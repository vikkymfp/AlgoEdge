"""HTTP layer of dashboard sign-in: /api/auth/*, /api/admin/*, and the
middleware that puts every other route behind a session.

Access policy (enforced here, on the server, for every request):

- Public: the login page and its assets, GET /api/auth/captcha and
  POST /api/auth/login.
- Loopback read-only exemption (Phase 8 collector/preflight, which send no
  credentials and are not modified): GET /api/auto-trading/status,
  GET /api/alerts and GET /api/auto-trading/option-context/{index}, only for
  a direct connection from a loopback address carrying no proxy headers.
  Nothing else is ever exempt - notably not POST /api/auto-trading/run/*.
- Everything else needs a live session. Default deny: a route added later is
  protected without being listed anywhere.
- USER is read-only. Every state-changing /api request other than
  /api/auth/* (trading controls, manual orders, broker credentials, alert
  acknowledgement, reconciliation override, /api/admin/*) and every
  /api/admin/* request needs ADMIN.
- State-changing requests carry the session's CSRF token in X-CSRF-Token and,
  when the browser sends Origin, a same-origin Origin.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from algoedge import auth_service, db
from algoedge.auth_captcha import CaptchaStore
from algoedge.auth_sessions import RateLimiter, Session, SessionStore, session_reference
from algoedge.config import Settings, get_settings
from algoedge.risk_manager import IST

logger = logging.getLogger("algoedge.auth")

COOKIE_NAME = "algoedge_session"
CSRF_HEADER = "X-CSRF-Token"
GENERIC_LOGIN_ERROR = "Invalid login credentials."
LOGIN_PAGE = "/login.html"

PUBLIC_API = {("GET", "/api/auth/captcha"), ("POST", "/api/auth/login")}
PUBLIC_STATIC = {"/login.html", "/login.js", "/auth.css", "/logo.png", "/favicon.ico"}
LOOPBACK_READONLY = (
    re.compile(r"/api/auto-trading/status"),
    re.compile(r"/api/alerts"),
    re.compile(r"/api/auto-trading/option-context/[^/]+"),
)
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "forwarded")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ADMIN_PAGES = {"/admin.html", "/admin.js"}
STRICT_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
              "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
CSP_PAGES = {"/login.html", "/admin.html"}

MAX_FIELD = {"identifier": 320, "password": 1024, "captchaId": 128, "captcha": 16}


def _ist_now() -> datetime:
    return datetime.now(IST)


@dataclass
class AuthState:
    settings: Settings
    clock: Callable[[], datetime]
    session_factory: Callable[[], Any]
    sessions: SessionStore
    captchas: CaptchaStore
    login_limiter: RateLimiter
    captcha_limiter: RateLimiter

    def factory(self):
        return self.session_factory()


def build_state(settings: Settings | None = None, *, clock: Callable[[], datetime] = _ist_now,
                session_factory: Callable[[], Any] = db.get_session_factory) -> AuthState:
    settings = settings or get_settings()
    return AuthState(
        settings=settings, clock=clock, session_factory=session_factory,
        sessions=SessionStore(idle=timedelta(minutes=settings.auth_session_idle_minutes),
                              absolute=timedelta(minutes=settings.auth_session_absolute_minutes), clock=clock),
        captchas=CaptchaStore(timedelta(seconds=settings.auth_captcha_ttl_seconds), clock),
        login_limiter=RateLimiter(settings.auth_login_rate_limit,
                                  timedelta(seconds=settings.auth_login_rate_window_seconds), clock),
        captcha_limiter=RateLimiter(settings.auth_captcha_rate_limit,
                                    timedelta(seconds=settings.auth_captcha_rate_window_seconds), clock),
    )


def install(app: FastAPI, state: AuthState | None = None) -> AuthState:
    """Registers the auth routes and middleware on `app`. Call right after
    creating the app, before any catch-all static mount."""
    state = state or build_state()
    app.state.auth = state
    app.include_router(router)
    app.add_middleware(AuthMiddleware)
    return state


# ---------------------------------------------------------------- request helpers


def _state(request: Request) -> AuthState:
    return request.app.state.auth


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _context(request: Request) -> auth_service.AttemptContext:
    return auth_service.AttemptContext(client_ip(request), request.headers.get("user-agent"))


_LOOPBACK_HOST_NAMES = {"localhost", "127.0.0.1", "::1"}


def _loopback_host_header(value: str) -> bool:
    """True for a Host header naming this machine by a loopback literal
    (127.0.0.1:5181, localhost, [::1]:5181) - what the local tooling sends."""
    host = value.strip().lower()
    if host.startswith("["):
        host = host[1:].partition("]")[0]
    elif host.count(":") == 1:
        host = host.partition(":")[0]
    if host in _LOOPBACK_HOST_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_direct_loopback(request: Request) -> bool:
    """A request made on this machine straight to the app. Three independent
    conditions: the TCP peer (after uvicorn's own trusted-proxy handling) is a
    loopback address, no proxy/forwarding header is present at all (so nothing
    relayed by Nginx qualifies, whatever it claims), and the Host header is a
    loopback literal (so a proxy that forwards `Host $host` is refused even if
    it were configured to send no forwarding header)."""
    host = client_ip(request)
    if not host or any(header in request.headers for header in PROXY_HEADERS):
        return False
    if not _loopback_host_header(request.headers.get("host", "")):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _same_origin(request: Request) -> bool:
    """True unless the browser declared a different Origin (or a cross-site fetch)."""
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
        return False
    return request.headers.get("sec-fetch-site") not in ("cross-site", "same-site")


def _cookie_secure(request: Request, setting: str) -> bool:
    if setting.lower() == "true":
        return True
    if setting.lower() == "false":
        return False
    return request.url.scheme == "https"


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _json(status: int, payload: dict[str, Any]) -> JSONResponse:
    return _no_store(JSONResponse(payload, status_code=status))


async def _json_body(request: Request, fields: dict[str, int]) -> dict[str, Any]:
    """A JSON object whose listed fields, when present, are strings within
    their length limits. Never echoes the submitted values in an error
    (FastAPI's own 422 would repeat a password back)."""
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Expected application/json.")
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request.")
    for name, limit in fields.items():
        value = body.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise HTTPException(status_code=400, detail="Invalid request.")
    return body


# ---------------------------------------------------------------- middleware


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await self._authorize(request, call_next)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path in CSP_PAGES:
            response.headers["Content-Security-Policy"] = STRICT_CSP
        return response

    async def _authorize(self, request: Request, call_next):
        state = _state(request)
        path, method = request.url.path, request.method.upper()
        is_api = path.startswith("/api/")

        if (method, path) in PUBLIC_API or (not is_api and path in PUBLIC_STATIC):
            return await call_next(request)
        if (state.settings.auth_loopback_readonly_exempt and method in ("GET", "HEAD")
                and any(pattern.fullmatch(path) for pattern in LOOPBACK_READONLY) and _is_direct_loopback(request)):
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        session = state.sessions.get(token)
        if session is not None:
            try:
                session = await run_in_threadpool(_revalidate, state, token, session)
            except auth_service.AuthUnavailable:
                return _json(503, {"detail": "Authentication is temporarily unavailable."})
        if session is None:
            return self._unauthenticated(request, is_api, had_cookie=token is not None)

        if method not in SAFE_METHODS and is_api:
            supplied = request.headers.get(CSRF_HEADER, "")
            if not _same_origin(request) or not secrets.compare_digest(supplied, session.csrf_token):
                return _json(403, {"detail": "Request rejected (CSRF check failed)."})
        needs_admin = path.startswith("/api/admin/") or path in ADMIN_PAGES or (
            is_api and method not in SAFE_METHODS and not path.startswith("/api/auth/"))
        if needs_admin and session.role != "ADMIN":
            logger.info("auth_event event=FORBIDDEN user_id=%s method=%s path=%s", session.user_id, method, path)
            if not is_api:
                return RedirectResponse("/", status_code=303)
            return _json(403, {"detail": "You do not have permission to perform this action."})

        request.state.auth_session = session
        return await call_next(request)

    @staticmethod
    def _unauthenticated(request: Request, is_api: bool, *, had_cookie: bool) -> Response:
        if is_api:
            response: Response = _json(401, {"detail": "Authentication required."})
        elif request.method.upper() in ("GET", "HEAD") and (request.url.path.endswith((".html", "/"))):
            response = RedirectResponse(LOGIN_PAGE, status_code=303)
        else:
            response = _json(401, {"detail": "Authentication required."})
        if had_cookie:
            response.delete_cookie(COOKIE_NAME, path="/")
        return response


def _revalidate(state: AuthState, token: str, session: Session) -> Session | None:
    """Re-reads the user row at most every auth_session_revalidate_seconds so
    a deactivation or role change made outside this process still applies."""
    now = state.clock()
    if now - session.validated_at < timedelta(seconds=state.settings.auth_session_revalidate_seconds):
        return session
    user = auth_service.load_active_user(state.factory(), session.user_id)
    if user is None:
        state.sessions.revoke(token)
        return None
    session.role, session.email, session.mobile_no = user["role"], user["email"], user["mobileNo"]
    session.validated_at = now
    return session


# ---------------------------------------------------------------- authorization dependencies


def require_authentication(request: Request) -> Session:
    session = getattr(request.state, "auth_session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return session


_authenticated = Depends(require_authentication)


def require_role(role: str) -> Callable[[Request], Session]:
    def dependency(session: Session = _authenticated) -> Session:
        if session.role != role:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
        return session

    return dependency


require_admin = require_role("ADMIN")
_admin_only = Depends(require_admin)

router = APIRouter()


# ---------------------------------------------------------------- /api/auth


@router.get("/api/auth/captcha")
def captcha(request: Request) -> Response:
    state = _state(request)
    if not state.captcha_limiter.hit(client_ip(request) or "unknown"):
        return _json(429, {"detail": "Too many requests. Please wait and try again."})
    return _json(200, state.captchas.issue())


@router.post("/api/auth/login")
async def login(request: Request) -> Response:
    state = _state(request)
    context = _context(request)
    if not _same_origin(request):
        return _json(403, {"detail": "Request rejected."})
    body = await _json_body(request, MAX_FIELD)
    identifier, password = body.get("identifier"), body.get("password")
    factory = state.factory()

    async def reject(reason: str, status: int, message: str) -> Response:
        await run_in_threadpool(auth_service.record_failed_attempt, factory, raw_identifier=identifier,
                                reason=reason, context=context, clock=state.clock)
        return _json(status, {"detail": message, "code": reason if reason in (
            auth_service.INVALID_CAPTCHA, auth_service.RATE_LIMITED) else None})

    if factory is None:
        return _json(503, {"detail": "Sign-in is temporarily unavailable."})
    if not state.login_limiter.hit(context.ip_address or "unknown"):
        return await reject(auth_service.RATE_LIMITED, 429, "Too many login attempts. Please wait and try again.")
    if not state.captchas.verify_and_consume(body.get("captchaId"), body.get("captcha")):
        return await reject(auth_service.INVALID_CAPTCHA, 400, "Invalid or expired CAPTCHA. Please try again.")
    if not isinstance(identifier, str) or not identifier.strip() or not isinstance(password, str) or not password:
        return await reject(auth_service.INVALID_CREDENTIALS, 401, GENERIC_LOGIN_ERROR)

    token = SessionStore.new_token()
    try:
        outcome = await run_in_threadpool(
            auth_service.authenticate, factory, raw_identifier=identifier, password=password, context=context,
            session_ref=session_reference(token), clock=state.clock,
            lockout_threshold=state.settings.auth_lockout_threshold,
            lockout_minutes=state.settings.auth_lockout_minutes)
    except auth_service.AuthUnavailable:
        return _json(503, {"detail": "Sign-in is temporarily unavailable."})
    if not outcome.success:
        return _json(401, {"detail": GENERIC_LOGIN_ERROR, "code": None})

    state.sessions.revoke(request.cookies.get(COOKIE_NAME))  # never keep a pre-login session (fixation)
    user = outcome.user or {}
    _, session = state.sessions.create(token=token, user_id=user["id"], role=user["role"], email=user["email"],
                                       mobile_no=user["mobileNo"])
    response = _json(200, {"authenticated": True, "user": user, "csrfToken": session.csrf_token})
    response.set_cookie(COOKIE_NAME, token, max_age=int(state.sessions.absolute.total_seconds()), path="/",
                        httponly=True, samesite="lax",
                        secure=_cookie_secure(request, state.settings.auth_cookie_secure))
    return response


@router.post("/api/auth/logout")
def logout(request: Request, session: Session = _authenticated) -> Response:
    _state(request).sessions.revoke(request.cookies.get(COOKIE_NAME))
    logger.info("auth_event event=LOGOUT user_id=%s session=%s ip=%s", session.user_id, session.reference,
                client_ip(request))
    response = _json(200, {"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/api/auth/me")
def me(session: Session = _authenticated) -> Response:
    return _json(200, {"authenticated": True, "csrfToken": session.csrf_token,
                       "user": {"id": session.user_id, "email": session.email, "mobileNo": session.mobile_no,
                                "role": session.role}})


# ---------------------------------------------------------------- /api/admin (ADMIN only)


def _admin_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (SQLAlchemyError, auth_service.AuthUnavailable) as error:
        logger.error("auth: admin operation failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="The user database is unavailable.") from error


def _optional_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD.") from error


@router.get("/api/admin/login-activity")
def admin_login_activity(request: Request, start: str | None = None, end: str | None = None,
                         success: str | None = None, user_id: int | None = None,
                         identifier_type: str | None = None, failure_reason: str | None = None, page: int = 1,
                         page_size: int = 50, _admin: Session = _admin_only) -> Response:
    if success not in (None, "", "true", "false"):
        raise HTTPException(status_code=400, detail="success must be true or false.")
    if identifier_type not in (None, "", auth_service.EMAIL, auth_service.MOBILE):
        raise HTTPException(status_code=400, detail="Unknown identifier type.")
    if failure_reason not in (None, "", *auth_service.FAILURE_REASONS):
        raise HTTPException(status_code=400, detail="Unknown failure reason.")
    result = _admin_call(
        auth_service.list_login_activity, _state(request).factory(), start=_optional_date(start),
        end=_optional_date(end), success=None if not success else success == "true", user_id=user_id,
        identifier_type=identifier_type or None, failure_reason=failure_reason or None, page=page,
        page_size=page_size)
    return _json(200, result)


@router.get("/api/admin/users")
def admin_users(request: Request, _admin: Session = _admin_only) -> Response:
    return _json(200, {"users": _admin_call(auth_service.list_users, _state(request).factory()),
                       "roles": list(auth_service.ROLES)})


@router.post("/api/admin/users")
async def admin_create_user(request: Request, admin: Session = _admin_only) -> Response:
    state = _state(request)
    body = await _json_body(request, {"email": 254, "mobileNo": 32, "password": 1024, "role": 16})
    user = await run_in_threadpool(
        _admin_call, auth_service.create_user, state.factory(), email=body.get("email") or None,
        mobile_no=body.get("mobileNo") or None, password=body.get("password") or "", role=body.get("role") or "USER",
        clock=state.clock, min_length=state.settings.auth_password_min_length)
    logger.info("auth_event event=ADMIN_USER_CREATE actor_id=%s user_id=%s", admin.user_id, user["id"])
    return _json(201, {"user": user})


@router.post("/api/admin/users/{user_id}/active")
async def admin_set_active(user_id: int, request: Request, admin: Session = _admin_only) -> Response:
    state = _state(request)
    body = await _json_body(request, {})
    if not isinstance(body.get("active"), bool):
        raise HTTPException(status_code=400, detail="active must be true or false.")
    user = await run_in_threadpool(_admin_call, auth_service.set_user_active, state.factory(), user_id,
                                   body["active"], actor_id=admin.user_id, clock=state.clock)
    if not body["active"]:
        state.sessions.revoke_user(user_id)
    return _json(200, {"user": user})


@router.post("/api/admin/users/{user_id}/role")
async def admin_set_role(user_id: int, request: Request, admin: Session = _admin_only) -> Response:
    state = _state(request)
    body = await _json_body(request, {"role": 16})
    user = await run_in_threadpool(_admin_call, auth_service.set_user_role, state.factory(), user_id,
                                   body.get("role") or "", actor_id=admin.user_id, clock=state.clock)
    state.sessions.revoke_user(user_id)
    return _json(200, {"user": user})


@router.post("/api/admin/users/{user_id}/password")
async def admin_set_password(user_id: int, request: Request, admin: Session = _admin_only) -> Response:
    state = _state(request)
    body = await _json_body(request, {"password": 1024})
    user = await run_in_threadpool(
        _admin_call, auth_service.set_user_password, state.factory(), user_id, body.get("password") or "",
        actor_id=admin.user_id, clock=state.clock, min_length=state.settings.auth_password_min_length)
    state.sessions.revoke_user(user_id)
    return _json(200, {"user": user})
