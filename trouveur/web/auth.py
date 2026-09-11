"""Session auth.

There is deliberately no registration route, no password reset and no user-listing endpoint: the
only way to create a login is `trouveur create-user` on the server. That omission removes the
largest attack surface of an internet-facing app, and it is the reason this application can be
exposed without an identity provider in front of it.

Sessions carry the user id, so every query downstream is scoped to one user rather than assuming
there is only one.
"""

from __future__ import annotations

import logging
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


def hash_password(password: str) -> str:
    return _hasher.hash(password)


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
        # Hash anyway, so a missing username and a wrong password take comparable time and the
        # response cannot be used to enumerate accounts.
        _hasher.hash(password)
        log.warning("failed login attempt for unknown username")
        return None, "Invalid username or password."

    if user.locked_until is not None:
        remaining = (user.locked_until - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            return None, f"Too many failed attempts. Try again in {int(remaining // 60) + 1} min."

    ok = user.is_active
    if ok:
        try:
            _hasher.verify(user.password_hash, password)
        except (VerifyMismatchError, VerificationError):
            ok = False

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
