"""Per-user digest delivery."""

from __future__ import annotations

import logging

from trouveur.config import Settings
from trouveur.db.engine import connect
from trouveur.db.queries import match as match_q
from trouveur.db.queries import users as users_q
from trouveur.notify import email

log = logging.getLogger(__name__)


async def send_digests(settings: Settings) -> int:
    """Send each user their own digest. One failure never blocks another user's mail."""
    async with connect() as conn:
        users = await users_q.list_users(conn)

    sent = 0
    for user in users:
        if not user.is_active:
            continue
        async with connect() as conn:
            # Every account has an address now, so the switch is what decides this. It used to be
            # decided by the address being missing, which meant opting out required having no way
            # to be reached at all.
            profile = await users_q.get_profile(conn, user.id)
            if profile is None or not profile.digest_enabled:
                continue
            rows = await match_q.pending_digest(conn, user.id)
            if not rows:
                continue
            subject, text, body = email.render(rows)
            try:
                email.send(settings, user.email, subject, text, body)
            except Exception as exc:  # noqa: BLE001 - one user's mail must not block another's
                log.warning("digest to user %s failed: %s", user.id, exc)
                continue
            # In the same transaction as the send, so a crash cannot mark postings as notified
            # that no mail ever mentioned.
            await match_q.mark_notified(conn, user.id, [row.id for row in rows])
        sent += 1
    return sent
