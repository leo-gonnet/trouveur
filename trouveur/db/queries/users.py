"""User SQL: accounts, profiles, stored credentials and metered spend."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur.db.schema import app_user, user_llm_credential, user_llm_spend, user_profile

# Changing one of these changes what a good match *is*, so it invalidates that user's cached LLM
# scores and forces a re-score they will be billed for. Everything else about a profile -- the
# how many results to retrieve or rerank -- changes presentation or volume only and must not
# bump the version. Getting this wrong is not a crash, it is a surprise invoice.
SCORING_FIELDS = frozenset(
    {
        "title", "years_experience", "objectives", "languages", "must_have",
        "deal_breakers", "keywords", "countries", "cities", "work_modes",
        "seniorities", "employment_types", "min_salary_eur_year",
    }
)


async def get_user_by_username(conn: AsyncConnection, username: str) -> sa.Row | None:
    return (
        await conn.execute(app_user.select().where(app_user.c.username == username))
    ).one_or_none()


async def get_user(conn: AsyncConnection, user_id: int) -> sa.Row | None:
    return (
        await conn.execute(app_user.select().where(app_user.c.id == user_id))
    ).one_or_none()


async def list_users(conn: AsyncConnection) -> list[sa.Row]:
    return list(await conn.execute(app_user.select().order_by(app_user.c.username)))


async def create_user(
    conn: AsyncConnection, username: str, password_hash: str, email: str | None = None
) -> int:
    user_id = (
        await conn.execute(
            app_user.insert()
            .values(username=username, password_hash=password_hash, email=email)
            .returning(app_user.c.id)
        )
    ).scalar_one()
    await conn.execute(
        pg_insert(user_profile)
        .values(user_id=user_id)
        .on_conflict_do_nothing(index_elements=[user_profile.c.user_id])
    )
    return user_id


async def record_login_result(
    conn: AsyncConnection, user_id: int, *, success: bool, lockout_minutes: int, max_attempts: int
) -> None:
    if success:
        await conn.execute(
            app_user.update()
            .where(app_user.c.id == user_id)
            .values(failed_attempts=0, locked_until=None)
        )
        return
    attempts = app_user.c.failed_attempts + 1
    await conn.execute(
        app_user.update()
        .where(app_user.c.id == user_id)
        .values(
            failed_attempts=attempts,
            locked_until=sa.case(
                (
                    attempts >= max_attempts,
                    sa.func.now() + sa.func.make_interval(0, 0, 0, 0, 0, lockout_minutes),
                ),
                else_=None,
            ),
        )
    )


async def get_profile(conn: AsyncConnection, user_id: int) -> sa.Row | None:
    return (
        await conn.execute(user_profile.select().where(user_profile.c.user_id == user_id))
    ).one_or_none()


async def save_profile(
    conn: AsyncConnection, user_id: int, values: dict[str, Any]
) -> tuple[int, bool]:
    """Persist a profile. Returns (version, whether cached scores were invalidated)."""
    current = await get_profile(conn, user_id)
    rescore = current is None or any(
        field in values and values[field] != current._mapping[field]
        for field in SCORING_FIELDS
    )
    version = (current.version if current else 0) + (1 if rescore else 0)
    await conn.execute(
        user_profile.update()
        .where(user_profile.c.user_id == user_id)
        .values(**values, version=max(version, 1), updated_at=sa.func.now())
    )
    return max(version, 1), rescore


async def get_credential(conn: AsyncConnection, user_id: int) -> sa.Row | None:
    return (
        await conn.execute(
            user_llm_credential.select().where(user_llm_credential.c.user_id == user_id)
        )
    ).one_or_none()


async def save_credential(
    conn: AsyncConnection,
    user_id: int,
    *,
    api_key_encrypted: bytes,
    api_key_fingerprint: str,
    model: str,
    provider_pin: str | None,
    monthly_budget_usd: Decimal,
) -> None:
    stmt = pg_insert(user_llm_credential).values(
        user_id=user_id,
        api_key_encrypted=api_key_encrypted,
        api_key_fingerprint=api_key_fingerprint,
        model=model,
        provider_pin=provider_pin,
        monthly_budget_usd=monthly_budget_usd,
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[user_llm_credential.c.user_id],
            set_={
                "api_key_encrypted": stmt.excluded.api_key_encrypted,
                "api_key_fingerprint": stmt.excluded.api_key_fingerprint,
                "model": stmt.excluded.model,
                "provider_pin": stmt.excluded.provider_pin,
                "monthly_budget_usd": stmt.excluded.monthly_budget_usd,
                "updated_at": sa.func.now(),
            },
        )
    )


async def update_budget(
    conn: AsyncConnection, user_id: int, monthly_budget_usd: Decimal
) -> None:
    await conn.execute(
        user_llm_credential.update()
        .where(user_llm_credential.c.user_id == user_id)
        .values(monthly_budget_usd=monthly_budget_usd, updated_at=sa.func.now())
    )


async def delete_credential(conn: AsyncConnection, user_id: int) -> None:
    await conn.execute(
        user_llm_credential.delete().where(user_llm_credential.c.user_id == user_id)
    )


def _month(when: date | None = None) -> date:
    today = when or date.today()
    return today.replace(day=1)


async def month_spend(conn: AsyncConnection, user_id: int) -> sa.Row | None:
    return (
        await conn.execute(
            user_llm_spend.select().where(
                user_llm_spend.c.user_id == user_id,
                user_llm_spend.c.period_month == _month(),
            )
        )
    ).one_or_none()


async def add_spend(
    conn: AsyncConnection,
    user_id: int,
    *,
    tokens_in: int,
    tokens_out: int,
    cost_usd: Decimal,
) -> Decimal:
    """Record spend and return the new month-to-date total, in USD.

    USD because that is the currency OpenRouter actually bills in. Storing a euro figure would
    require an exchange rate between the meter and the cap, and a stale rate makes a budget limit
    quietly wrong in whichever direction the rate moved.

    Written in the same transaction as the scores it paid for, so a crash cannot leave a user
    holding results they were not charged for or a charge for results they never got.
    """
    stmt = pg_insert(user_llm_spend).values(
        user_id=user_id,
        period_month=_month(),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        calls=1,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[user_llm_spend.c.user_id, user_llm_spend.c.period_month],
        set_={
            "tokens_in": user_llm_spend.c.tokens_in + stmt.excluded.tokens_in,
            "tokens_out": user_llm_spend.c.tokens_out + stmt.excluded.tokens_out,
            "cost_usd": user_llm_spend.c.cost_usd + stmt.excluded.cost_usd,
            "calls": user_llm_spend.c.calls + 1,
            "updated_at": sa.func.now(),
        },
    ).returning(user_llm_spend.c.cost_usd)
    return (await conn.execute(stmt)).scalar_one()


async def spend_history(conn: AsyncConnection, user_id: int, months: int = 6) -> list[sa.Row]:
    return list(
        await conn.execute(
            user_llm_spend.select()
            .where(user_llm_spend.c.user_id == user_id)
            .order_by(user_llm_spend.c.period_month.desc())
            .limit(months)
        )
    )
