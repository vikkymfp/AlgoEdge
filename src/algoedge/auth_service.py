"""Dashboard sign-in: identifiers, password hashing, lockout, login
activity and user administration, over the existing `users` and
`user_login_activity` tables (algoedge.auth_models).

Access control only - nothing here touches trading, risk, orders, broker
credentials or the paper/live switch.

Unlike the rest of algoedge.db (an optional layer that trading skips when
the database is down), authentication FAILS CLOSED: with no database, or a
failed write of the attempt's own activity row, nobody is signed in.

Never logged, stored or returned: passwords, password hashes, CAPTCHA
answers, session tokens. Database errors are logged by exception type only
(their text can carry SQL parameters).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from algoedge.auth_models import User, UserLoginActivity
from algoedge.risk_manager import IST

logger = logging.getLogger("algoedge.auth")

ROLES = ("ADMIN", "USER")
EMAIL, MOBILE = "EMAIL", "MOBILE"

# failure_reason values written to user_login_activity.
INVALID_CAPTCHA = "INVALID_CAPTCHA"
INVALID_PASSWORD = "INVALID_PASSWORD"
INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
RATE_LIMITED = "RATE_LIMITED"
FAILURE_REASONS = (INVALID_CAPTCHA, INVALID_PASSWORD, INVALID_CREDENTIALS, ACCOUNT_LOCKED, ACCOUNT_DISABLED,
                   RATE_LIMITED)

MAX_PASSWORD_LENGTH = 256  # policy ceiling for new passwords (bounds hashing cost)

# argon2-cffi's defaults are Argon2id (RFC 9106 low-memory profile).
password_hasher = PasswordHasher()
_dummy_hash: str | None = None

_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_MOBILE_SEPARATORS = re.compile(r"[\s\-().]")
_INDIAN_MOBILE = re.compile(r"[6-9]\d{9}")


class AuthUnavailable(RuntimeError):
    """The database needed for authentication is not available."""


# ---------------------------------------------------------------- identifiers


def normalize_email(raw: str) -> str | None:
    value = raw.strip().lower()
    if len(value) > 254 or not _EMAIL_PATTERN.fullmatch(value):
        return None
    return value


def normalize_mobile(raw: str) -> str | None:
    """The canonical stored form: 10 digits, Indian mobile (starts 6-9).
    Accepts spaces, dashes, brackets, dots and a +91 / 91 / 0 prefix."""
    value = _MOBILE_SEPARATORS.sub("", raw.strip())
    if value.startswith("+91"):
        value = value[3:]
    elif len(value) == 12 and value.startswith("91"):
        value = value[2:]
    elif len(value) == 11 and value.startswith("0"):
        value = value[1:]
    return value if _INDIAN_MOBILE.fullmatch(value) else None


def detect_identifier(raw: object) -> tuple[str | None, str | None]:
    """(identifier_type, normalized) decided on the server alone - never from
    anything the browser claims. (None, None) when it is neither a valid
    email nor a valid mobile number."""
    if not isinstance(raw, str) or len(raw) > 320:
        return None, None
    if "@" in raw:
        email = normalize_email(raw)
        return (EMAIL, email) if email else (None, None)
    mobile = normalize_mobile(raw)
    return (MOBILE, mobile) if mobile else (None, None)


def mask_identifier(value: str | None, identifier_type: str | None) -> str | None:
    if not value:
        return value
    if identifier_type == EMAIL and "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    if identifier_type == MOBILE:
        return "******" + value[-4:]
    return "***"


# ---------------------------------------------------------------- passwords


def password_policy_error(password: object, *, min_length: int) -> str | None:
    """Server-side policy for NEW passwords (never applied to a login attempt)."""
    if not isinstance(password, str) or not password:
        return "Password is required."
    if len(password) < min_length:
        return f"Password must be at least {min_length} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    return None


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> tuple[bool, bool]:
    """(matches, needs_rehash). Library-provided safe comparison; an
    unreadable stored hash is a mismatch, never an error surfaced to the user."""
    if not stored_hash:
        return False, False
    try:
        password_hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        return False, False
    except (InvalidHashError, VerificationError):
        logger.warning("auth: stored password hash could not be verified (unsupported or corrupt format)")
        return False, False
    return True, password_hasher.check_needs_rehash(stored_hash)


def _burn_verification(password: str) -> None:
    """Spends the same hashing work as a real check, so an unknown, disabled
    or locked account answers in the same time as a wrong password."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = password_hasher.hash("algoedge-timing-equaliser")
    try:
        password_hasher.verify(_dummy_hash, password)
    except VerificationError:
        pass


# ---------------------------------------------------------------- helpers


def db_now(clock: Callable[[], datetime]) -> datetime:
    """Naive IST - the convention of every other timestamp this app writes."""
    return clock().astimezone(IST).replace(tzinfo=None)


def _factory(session_factory: sessionmaker | None) -> sessionmaker:
    if session_factory is None:
        raise AuthUnavailable("no database configured")
    return session_factory


def _truncate(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else value


def public_user(user: User) -> dict[str, Any]:
    """The only user fields that ever leave the server (never password_hash)."""
    return {"id": user.id, "email": user.email, "mobileNo": user.mobile_no, "role": user.role}


@dataclass(frozen=True)
class AttemptContext:
    ip_address: str | None
    user_agent: str | None


def _activity_row(*, user_id: int | None, identifier: str | None, identifier_type: str | None, at: datetime,
                  success: bool, failure_reason: str | None, context: AttemptContext,
                  session_ref: str | None) -> UserLoginActivity:
    return UserLoginActivity(
        user_id=user_id, login_identifier=_truncate(identifier, 254), identifier_type=identifier_type,
        attempt_at=at, success=success, failure_reason=failure_reason,
        ip_address=_truncate(context.ip_address, 45), user_agent=_truncate(context.user_agent, 1000),
        session_id=session_ref,
    )


def _audit(event: str, *, user_id: int | None, identifier: str | None, identifier_type: str | None,
           context: AttemptContext, reason: str | None = None) -> None:
    logger.info("auth_event event=%s user_id=%s identifier=%s type=%s ip=%s reason=%s", event, user_id,
                mask_identifier(identifier, identifier_type), identifier_type, context.ip_address, reason)


def record_failed_attempt(session_factory: sessionmaker | None, *, raw_identifier: object, reason: str,
                          context: AttemptContext, clock: Callable[[], datetime]) -> None:
    """Records an attempt rejected before its password was considered
    (INVALID_CAPTCHA, RATE_LIMITED). The account is looked up only to fill
    user_id; no counter or lock changes, and nothing about it reaches the
    client."""
    identifier_type, identifier = detect_identifier(raw_identifier)
    user_id = None
    try:
        with _factory(session_factory)() as session:
            user = _find_user(session, identifier_type, identifier)
            user_id = user.id if user is not None else None
            session.add(_activity_row(user_id=user_id, identifier=identifier, identifier_type=identifier_type,
                                      at=db_now(clock), success=False, failure_reason=reason, context=context,
                                      session_ref=None))
            session.commit()
    except (SQLAlchemyError, AuthUnavailable) as error:
        logger.error("auth: could not record a %s login attempt (%s)", reason, type(error).__name__)
    _audit("LOGIN_FAILURE", user_id=user_id, identifier=identifier, identifier_type=identifier_type,
           context=context, reason=reason)


# ---------------------------------------------------------------- login


@dataclass(frozen=True)
class LoginOutcome:
    success: bool
    failure_reason: str | None = None
    user: dict[str, Any] | None = None


def authenticate(session_factory: sessionmaker | None, *, raw_identifier: object, password: object,
                 context: AttemptContext, session_ref: str, clock: Callable[[], datetime],
                 lockout_threshold: int, lockout_minutes: int) -> LoginOutcome:
    """Steps 5-14 of the login flow (the caller has already rate-limited
    and consumed a valid CAPTCHA). Exactly one activity row is written per
    call, in the same transaction as the counter/lock/last-login updates;
    if that transaction fails, the attempt fails (AuthUnavailable)."""
    password = password if isinstance(password, str) else ""
    identifier_type, identifier = detect_identifier(raw_identifier)
    factory = _factory(session_factory)
    now = db_now(clock)
    try:
        with factory() as session:
            user = _find_user(session, identifier_type, identifier)
            outcome, user_id = _decide(session, user, password, now=now,
                                       lockout_threshold=lockout_threshold, lockout_minutes=lockout_minutes,
                                       context=context, identifier=identifier, identifier_type=identifier_type)
            session.add(_activity_row(user_id=user_id, identifier=identifier, identifier_type=identifier_type,
                                      at=now, success=outcome.success, failure_reason=outcome.failure_reason,
                                      context=context, session_ref=session_ref if outcome.success else None))
            session.commit()
    except SQLAlchemyError as error:
        logger.error("auth: login transaction failed (%s)", type(error).__name__)
        raise AuthUnavailable("login transaction failed") from error
    if outcome.success:
        _audit("LOGIN_SUCCESS", user_id=user_id, identifier=identifier, identifier_type=identifier_type,
               context=context)
    else:
        _audit("LOGIN_FAILURE", user_id=user_id, identifier=identifier, identifier_type=identifier_type,
               context=context, reason=outcome.failure_reason)
        if outcome.failure_reason == ACCOUNT_DISABLED:
            _audit("ACCOUNT_DISABLED", user_id=user_id, identifier=identifier, identifier_type=identifier_type,
                   context=context)
    return outcome


def _find_user(session: DbSession, identifier_type: str | None, identifier: str | None) -> User | None:
    if identifier_type == EMAIL:
        return session.scalars(select(User).where(User.email == identifier)).first()
    if identifier_type == MOBILE:
        return session.scalars(select(User).where(User.mobile_no == identifier)).first()
    return None


def _decide(session: DbSession, user: User | None, password: str, *, now: datetime, lockout_threshold: int,
            lockout_minutes: int, context: AttemptContext, identifier: str | None,
            identifier_type: str | None) -> tuple[LoginOutcome, int | None]:
    if user is None:
        _burn_verification(password)
        return LoginOutcome(False, INVALID_CREDENTIALS), None
    if not user.is_active:
        _burn_verification(password)
        return LoginOutcome(False, ACCOUNT_DISABLED), user.id
    if user.locked_until is not None and user.locked_until > now:
        # Checked before the password, and never extends the lock: a locked
        # account cannot be re-locked indefinitely by an attacker's guesses.
        _burn_verification(password)
        return LoginOutcome(False, ACCOUNT_LOCKED), user.id

    matches, needs_rehash = verify_password(user.password_hash, password)
    if not matches:
        session.execute(update(User).where(User.id == user.id)
                        .values(failed_login_count=User.failed_login_count + 1, updated_at=now))
        count = session.scalar(select(User.failed_login_count).where(User.id == user.id)) or 0
        if count >= lockout_threshold:
            session.execute(update(User).where(User.id == user.id).values(
                locked_until=now + timedelta(minutes=lockout_minutes), failed_login_count=0, updated_at=now))
            _audit("ACCOUNT_LOCKED", user_id=user.id, identifier=identifier, identifier_type=identifier_type,
                   context=context, reason=f"{count} failed passwords")
        return LoginOutcome(False, INVALID_PASSWORD), user.id

    values: dict[str, Any] = {"failed_login_count": 0, "locked_until": None, "last_login_at": now,
                              "updated_at": now}
    if needs_rehash:
        values["password_hash"] = hash_password(password)
    session.execute(update(User).where(User.id == user.id).values(**values))
    return LoginOutcome(True, None, public_user(user)), user.id


def load_active_user(session_factory: sessionmaker | None, user_id: int) -> dict[str, Any] | None:
    """The user's current public fields, or None if missing or deactivated
    (used to re-validate live sessions). Raises AuthUnavailable on DB errors."""
    try:
        with _factory(session_factory)() as session:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                return None
            return public_user(user)
    except SQLAlchemyError as error:
        logger.error("auth: session re-validation failed (%s)", type(error).__name__)
        raise AuthUnavailable("session re-validation failed") from error


# ---------------------------------------------------------------- administration


def create_user(session_factory: sessionmaker | None, *, email: str | None, mobile_no: str | None, password: str,
                role: str, clock: Callable[[], datetime], min_length: int) -> dict[str, Any]:
    """Creates one user with a hashed password. Raises ValueError on invalid input."""
    normalized_email = normalize_email(email) if email else None
    normalized_mobile = normalize_mobile(mobile_no) if mobile_no else None
    if email and not normalized_email:
        raise ValueError("Invalid email address.")
    if mobile_no and not normalized_mobile:
        raise ValueError("Invalid mobile number (10-digit Indian mobile expected).")
    if not normalized_email and not normalized_mobile:
        raise ValueError("An email address or mobile number is required.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}.")
    error = password_policy_error(password, min_length=min_length)
    if error:
        raise ValueError(error)
    now = db_now(clock)
    with _factory(session_factory)() as session:
        clashes = []
        if normalized_email:
            clashes.append(User.email == normalized_email)
        if normalized_mobile:
            clashes.append(User.mobile_no == normalized_mobile)
        if session.scalars(select(User.id).where(or_(*clashes))).first() is not None:
            raise ValueError("A user with that email or mobile number already exists.")
        user = User(email=normalized_email, mobile_no=normalized_mobile, password_hash=hash_password(password),
                    role=role, is_active=True, failed_login_count=0, created_at=now, updated_at=now)
        session.add(user)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            # Also what SQL Server reports for a second NULL in a plain (unfiltered)
            # UNIQUE email/mobile_no column - see `auth_cli verify-schema`.
            raise ValueError("This user conflicts with an existing user's email or mobile number "
                             "(on SQL Server a plain UNIQUE column also allows only one user without it).") from error
        return public_user(user)


def _user_summary(user: User) -> dict[str, Any]:
    return {**public_user(user), "isActive": user.is_active, "failedLoginCount": user.failed_login_count,
            "lockedUntil": user.locked_until.isoformat() if user.locked_until else None,
            "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
            "createdAt": user.created_at.isoformat() if user.created_at else None}


def list_users(session_factory: sessionmaker | None) -> list[dict[str, Any]]:
    with _factory(session_factory)() as session:
        return [_user_summary(user) for user in session.scalars(select(User).order_by(User.id))]


def _active_admin_count(session: DbSession) -> int:
    return session.scalar(select(func.count()).select_from(User)
                          .where(User.role == "ADMIN", User.is_active.is_(True))) or 0


def _modify_user(session_factory: sessionmaker | None, user_id: int, *, actor_id: int, clock: Callable[[], datetime],
                 apply: Callable[[DbSession, User], None]) -> dict[str, Any]:
    with _factory(session_factory)() as session:
        user = session.get(User, user_id)
        if user is None:
            raise LookupError("User not found.")
        apply(session, user)
        user.updated_at = db_now(clock)
        session.commit()
        logger.info("auth_event event=ADMIN_USER_UPDATE actor_id=%s user_id=%s", actor_id, user_id)
        return _user_summary(user)


def set_user_active(session_factory: sessionmaker | None, user_id: int, active: bool, *, actor_id: int,
                    clock: Callable[[], datetime]) -> dict[str, Any]:
    def apply(session: DbSession, user: User) -> None:
        if not active and user.id == actor_id:
            raise ValueError("You cannot deactivate your own account.")
        if not active and user.role == "ADMIN" and user.is_active and _active_admin_count(session) <= 1:
            raise ValueError("The last active ADMIN cannot be deactivated.")
        user.is_active = active
        if active:
            user.failed_login_count, user.locked_until = 0, None

    return _modify_user(session_factory, user_id, actor_id=actor_id, clock=clock, apply=apply)


def set_user_role(session_factory: sessionmaker | None, user_id: int, role: str, *, actor_id: int,
                  clock: Callable[[], datetime]) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}.")

    def apply(session: DbSession, user: User) -> None:
        if user.id == actor_id and role != user.role:
            raise ValueError("You cannot change your own role.")
        if user.role == "ADMIN" and role != "ADMIN" and user.is_active and _active_admin_count(session) <= 1:
            raise ValueError("The last active ADMIN cannot be demoted.")
        user.role = role

    return _modify_user(session_factory, user_id, actor_id=actor_id, clock=clock, apply=apply)


def set_user_password(session_factory: sessionmaker | None, user_id: int, password: str, *, actor_id: int,
                      clock: Callable[[], datetime], min_length: int) -> dict[str, Any]:
    """Admin reset: the ADMIN supplies the new password; only its hash is
    stored, nothing is displayed, and any lock is cleared."""
    error = password_policy_error(password, min_length=min_length)
    if error:
        raise ValueError(error)
    new_hash = hash_password(password)

    def apply(_session: DbSession, user: User) -> None:
        user.password_hash = new_hash
        user.failed_login_count, user.locked_until = 0, None

    return _modify_user(session_factory, user_id, actor_id=actor_id, clock=clock, apply=apply)


def list_login_activity(session_factory: sessionmaker | None, *, start: date | None = None, end: date | None = None,
                        success: bool | None = None, user_id: int | None = None,
                        identifier_type: str | None = None, failure_reason: str | None = None,
                        page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """Newest first, filtered, paginated. `start`/`end` are inclusive IST
    dates. Identifiers of attempts that matched no account are masked."""
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    conditions = []
    if start is not None:
        conditions.append(UserLoginActivity.attempt_at >= datetime.combine(start, datetime.min.time()))
    if end is not None:
        conditions.append(UserLoginActivity.attempt_at < datetime.combine(end + timedelta(days=1),
                                                                           datetime.min.time()))
    if success is not None:
        conditions.append(UserLoginActivity.success.is_(success))
    if user_id is not None:
        conditions.append(UserLoginActivity.user_id == user_id)
    if identifier_type is not None:
        conditions.append(UserLoginActivity.identifier_type == identifier_type)
    if failure_reason is not None:
        conditions.append(UserLoginActivity.failure_reason == failure_reason)
    with _factory(session_factory)() as session:
        total = session.scalar(select(func.count()).select_from(UserLoginActivity).where(*conditions)) or 0
        rows = session.execute(
            select(UserLoginActivity, User.email, User.mobile_no)
            .outerjoin(User, User.id == UserLoginActivity.user_id)
            .where(*conditions)
            .order_by(UserLoginActivity.attempt_at.desc(), UserLoginActivity.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
    items = []
    for activity, user_email, user_mobile in rows:
        identifier = activity.login_identifier
        if activity.user_id is None:
            identifier = mask_identifier(identifier, activity.identifier_type)
        items.append({
            "id": activity.id, "attemptAt": activity.attempt_at.isoformat() if activity.attempt_at else None,
            "userId": activity.user_id, "user": user_email or user_mobile,
            "identifier": identifier, "identifierType": activity.identifier_type,
            "success": activity.success, "failureReason": activity.failure_reason,
            "ipAddress": activity.ip_address, "userAgent": activity.user_agent, "sessionId": activity.session_id,
        })
    return {"items": items, "total": total, "page": page, "pageSize": page_size}
