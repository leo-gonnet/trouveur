"""SMTP digest.

One email per run, containing only jobs above the threshold that have not been notified before.
`notified_at` is set by the pipeline in the same transaction, so a rerun sends nothing.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from trouveur.config import Settings

log = logging.getLogger(__name__)


def render(rows: list[Any], threshold: int) -> tuple[str, str, str]:
    """Return (subject, plaintext, html)."""
    subject = f"Trouveur: {len(rows)} new match{'es' if len(rows) != 1 else ''}"

    lines = [f"{len(rows)} job(s) scored at or above {threshold}.", ""]
    cards = []
    for row in rows:
        score = row.llm_score if row.llm_score is not None else "-"
        where = ", ".join(filter(None, [row.location_city, row.location_country])) or "n/a"
        salary = _salary(row)
        remote = "remote" if row.remote else ("on-site" if row.remote is False else "remote n/a")

        lines += [
            f"[{score}] {row.title}",
            f"    {row.company or 'unknown company'} — {where} — {remote}{salary}",
            f"    {row.llm_reason or ''}".rstrip(),
            f"    {row.url}",
            "",
        ]
        cards.append(
            f"""
            <div style="border-left:4px solid #2b6cb0;padding:8px 12px;margin:0 0 16px">
              <div style="font-size:15px;font-weight:600">
                <span style="background:#2b6cb0;color:#fff;border-radius:3px;
                             padding:1px 6px;font-size:12px">{score}</span>
                <a href="{_esc(row.url)}" style="color:#1a202c;text-decoration:none">
                  {_esc(row.title)}</a>
              </div>
              <div style="color:#4a5568;font-size:13px;margin-top:2px">
                {_esc(row.company or "unknown company")} &middot; {_esc(where)}
                &middot; {remote}{_esc(salary)}
              </div>
              <div style="color:#2d3748;font-size:13px;margin-top:6px">
                {_esc(row.llm_reason or "")}</div>
            </div>"""
        )

    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif">
      <p style="color:#4a5568;font-size:13px">
        {len(rows)} job(s) scored at or above {threshold}.</p>
      {"".join(cards)}
    </body></html>"""
    return subject, "\n".join(lines), html


def _salary(row: Any) -> str:
    if row.salary_min is None and row.salary_max is None:
        return ""
    lo = f"{int(row.salary_min):,}" if row.salary_min is not None else "?"
    hi = f"{int(row.salary_max):,}" if row.salary_max is not None else "?"
    period = (row.salary_period or "").lower()
    return f" — {lo}–{hi} EUR/{period}"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def send(settings: Settings, subject: str, text: str, html: str) -> None:
    if not (settings.smtp_host and settings.smtp_from and settings.smtp_to):
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_HOST, SMTP_FROM and SMTP_TO in the environment."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = settings.smtp_to
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)
    log.info("digest sent to %s", settings.smtp_to)
