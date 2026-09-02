"""Single-user session auth.

There is deliberately no registration, password-reset or user-listing route: the only way to
create the login is `trouveur create-user` on the CLI. That omission removes the largest attack
surface of an internet-facing app (see AGENTS.md).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from trouveur.config import Settings
from trouveur.db import queries as q

log = logging.getLogger(__name__)

COOKIE_NAME = "trouveur_session"
_hasher = PasswordHasher()


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="trouveur-session")


def issue_session(settings: Settings, username: str) -> str:
    return _serializer(settings).dumps({"u": username})


def read_session(settings: Settings, request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        payload = _serializer(settings).loads(
            token, max_age=settings.session_max_age_days * 86400
        )
    except (BadSignature, SignatureExpired):
        return None
    return payload.get("u")


def set_cookie(response, settings: Settings, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=settings.session_max_age_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


async def authenticate(conn, settings: Settings, username: str, password: str) -> tuple[bool, str]:
    """Verify credentials. Returns (ok, message)."""
    user = await q.get_user(conn)
    if user is None:
        return False, "No user exists yet. Run `trouveur create-user` on the server."

    now = datetime.now(UTC)
    if user.locked_until and user.locked_until > now:
        wait = int((user.locked_until - now).total_seconds() // 60) + 1
        return False, f"Too many failed attempts. Try again in {wait} minute(s)."

    ok = user.username == username
    if ok:
        try:
            _hasher.verify(user.password_hash, password)
        except (VerifyMismatchError, VerificationError):
            ok = False

    locked_until = None
    if not ok and user.failed_attempts + 1 >= settings.max_login_attempts:
        locked_until = now + timedelta(minutes=settings.lockout_minutes)
    await q.record_login_result(conn, ok, locked_until)

    if not ok:
        log.warning("failed login attempt for username=%r", username)
        return False, "Invalid username or password."
    return True, ""
