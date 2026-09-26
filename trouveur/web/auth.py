"""Session auth.

There is deliberately no registration route and no password reset. An account is created by an
admin -- on the Users page, or with `trouveur create-user` for the FIRST one, which is the only
account that cannot have been created by an admin because there was none.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from trouveur.config import Settings
from trouveur.db.queries import users as users_q

log = logging.getLogger(__name__)

COOKIE_NAME = "trouveur_session"
_hasher = PasswordHasher()

# This login faces the internet, so it is the same floor wherever a password is set -- the CLI, the
# admin's Users page and a user changing their own.
MIN_PASSWORD_CHARS = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Check a password with no side effect. `authenticate` is for logging IN: it counts failures
    and can lock the account, which is wrong for confirming the password of a session that is
    already open -- five typos there would lock the owner out of their own account."""
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False
    return True


def generate_password() -> str:
    """An initial password for an account an admin creates, shown to them once to pass on.

    Random rather than memorable. A two-word-plus-digits password is nicer to read down a phone,
    and at roughly thirty bits it is also guessable; this is sixteen url-safe characters, and the
    person can replace it with something they like from Settings.
    """
    return secrets.token_urlsafe(12)


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="trouveur-session")


def issue_session(settings: Settings, user_id: int, username: str) -> str:
    return _serializer(settings).dumps({"uid": user_id, "u": username})


def read_session(settings: Settings, request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = _serializer(settings).loads(
            token, max_age=settings.session_max_age_days * 86400
        )
    except (BadSignature, SignatureExpired):
        return None
    return payload if isinstance(payload, dict) and "uid" in payload else None


def set_cookie(response, settings: Settings, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_max_age_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


async def authenticate(
    conn, settings: Settings, username: str, password: str
) -> tuple[int | None, str]:
    """Verify credentials. Returns (user_id, message)."""
    user = await users_q.get_user_by_username(conn, username)
    if user is None:
        # Hash anyway, so the response time cannot be used to enumerate accounts.
        _hasher.hash(password)
        log.warning("failed login attempt for unknown username")
        return None, "Invalid username or password."

    if user.locked_until is not None:
        remaining = (user.locked_until - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            return None, f"Too many failed attempts. Try again in {int(remaining // 60) + 1} min."

    ok = user.is_active and verify_password(user.password_hash, password)

    await users_q.record_login_result(
        conn,
        user.id,
        success=ok,
        lockout_minutes=settings.lockout_minutes,
        max_attempts=settings.max_login_attempts,
    )
    if not ok:
        log.warning("failed login attempt for username=%r", username)
        return None, "Invalid username or password."
    return user.id, ""
