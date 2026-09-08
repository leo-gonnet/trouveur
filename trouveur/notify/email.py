"""SMTP digest, one per user per run.

Contains only postings above that user's threshold that have not been sent before. notified_at is
written in the same transaction that sends, so a re-run sends nothing rather than repeating a
digest the user already read.

A user with no address simply gets no digest: the web UI is the primary surface and email is a
convenience, so a missing address is a configuration state, not an error.
"""

from __future__ import annotations

import html
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from trouveur.config import Settings

log = logging.getLogger(__name__)


def _places(row: Any) -> str:
    return ", ".join(
        item.get("raw", "") for item in (row.locations or []) if item.get("raw")
    ) or "location not stated"


def _salary(row: Any) -> str:
    low, high = row.salary_min_eur_year, row.salary_max_eur_year
    if not low and not high:
        return "salary not stated"
    if low and high:
        return f"{int(low):,}-{int(high):,} EUR/yr"
    return f"{int(low or high):,} EUR/yr"


def render(rows: list[Any], threshold: int) -> tuple[str, str, str]:
    """Return (subject, plaintext, html)."""
    subject = f"Trouveur: {len(rows)} new match{'es' if len(rows) != 1 else ''}"
    lines = [f"{len(rows)} posting(s) scored at or above {threshold}.", ""]
    cards = []

    for row in rows:
        score = row.llm_score if row.llm_score is not None else "-"
        lines += [
            f"[{score}] {row.title}",
            f"    {row.company or 'unknown company'} - {_places(row)} - {_salary(row)}",
            f"    {row.llm_reason or ''}".rstrip(),
            f"    {row.url}",
            "",
        ]
        cards.append(
            f"""
            <div style="border-left:4px solid #2f6fed;padding:8px 12px;margin:0 0 16px">
              <div style="font-size:15px;font-weight:600">
                <span style="background:#2f6fed;color:#fff;border-radius:3px;
                             padding:1px 6px;font-size:12px">{score}</span>
                <a href="{html.escape(row.url)}" style="color:#16202c;text-decoration:none">
                  {html.escape(row.title)}</a>
              </div>
              <div style="color:#64748b;font-size:13px;margin-top:2px">
                {html.escape(row.company or "unknown company")} &middot;
                {html.escape(_places(row))} &middot; {html.escape(_salary(row))}
              </div>
              <div style="color:#16202c;font-size:13px;margin-top:6px">
                {html.escape(row.llm_reason or "")}</div>
            </div>"""
        )

    body = (
        '<div style="font-family:system-ui,sans-serif;max-width:640px;margin:0 auto">'
        f"<p style='color:#64748b'>{len(rows)} posting(s) scored at or above {threshold}.</p>"
        + "".join(cards)
        + "</div>"
    )
    return subject, "\n".join(lines), body


def send(settings: Settings, to: str, subject: str, text: str, body: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_HOST and SMTP_FROM, or run without notifications."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = to
    message.set_content(text)
    message.add_alternative(body, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        if settings.smtp_user and settings.smtp_password:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)
    log.info("digest sent to %s", to)
