"""User SQL: accounts, profiles, granted credit and metered spend."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from trouveur import clock
from trouveur.db.schema import (
    app_user,
    user_credit_grant,
    user_llm_spend,
    user_profile,
)

# Changing one of these changes what a good match IS, so it invalidates the user's cached scores
# and bills a re-score. A volume setting must never be added here: it is not a crash, it is a
# surprise invoice.
SCORING_FIELDS = frozenset(
    {
        "title", "years_experience", "objectives", "background", "languages", "must_have",
        "keywords", "countries", "city_ids", "radius_km", "remote_anywhere",
        "min_salary_eur_year",
    }
)


async def get_user_by_email(conn: AsyncConnection, email: str) -> sa.Row | None:
    """Look an account up by its login. Folded on both sides, so a caller that has not normalised
    its input still finds the row rather than silently reporting no such account -- and the
    comparison matches the unique index, which is on `lower(email)` too."""
    return (
        await conn.execute(
            app_user.select().where(
                sa.func.lower(app_user.c.email) == sa.func.lower(sa.func.btrim(email))
            )
        )
    ).one_or_none()


async def get_user(conn: AsyncConnection, user_id: int) -> sa.Row | None:
    return (
        await conn.execute(app_user.select().where(app_user.c.id == user_id))
    ).one_or_none()


async def list_users(conn: AsyncConnection) -> list[sa.Row]:
    return list(await conn.execute(app_user.select().order_by(app_user.c.email)))


async def create_user(
    conn: AsyncConnection,
    email: str,
    password_hash: str,
    display_name: str = "",
    *,
    is_admin: bool | None = None,
) -> int:
    """Create a login. `is_admin=None` means "admin if this is the first account", which is the
    only moment it can be decided without one: the accounts an admin will create do not exist
    yet, so on an empty installation there is nobody else it could be.

    The address is folded here as well as at the form, so no write path can put a mixed-case
    duplicate in front of the unique index and get a constraint error instead of an account.
    """
    if is_admin is None:
        is_admin = (
            await conn.execute(sa.select(sa.func.count()).select_from(app_user))
        ).scalar_one() == 0
    login = email.strip().lower()
    user_id = (
        await conn.execute(
            app_user.insert()
            .values(
                email=login,
                display_name=display_name.strip() or login.partition("@")[0],
                password_hash=password_hash,
                is_admin=is_admin,
            )
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


def changes_scoring(current: sa.Row | None, values: dict[str, Any]) -> bool:
    """Whether saving `values` would change what a good match is, and so bump the version.

    Separate from save_profile because the form has to ask before it saves: a bump replaces the
    day's edition, and that is the user's call to make.
    """
    return current is None or any(
        field in values and values[field] != current._mapping[field]
        for field in SCORING_FIELDS
    )


async def save_profile(
    conn: AsyncConnection, user_id: int, values: dict[str, Any]
) -> tuple[int, bool]:
    """Persist a profile. Returns (version, whether a re-score was triggered)."""
    current = await get_profile(conn, user_id)
    rescore = changes_scoring(current, values)
    version = (current.version if current else 0) + (1 if rescore else 0)
    await conn.execute(
        user_profile.update()
        .where(user_profile.c.user_id == user_id)
        .values(**values, version=max(version, 1), updated_at=sa.func.now())
    )
    # Nothing is erased here. The bump alone is enough to queue a re-score, because retrieval no
    # longer re-stamps user_job_match.profile_version, and published editions are rows of their
    # own that a re-score cannot reach.
    return max(version, 1), rescore


async def set_password(conn: AsyncConnection, user_id: int, password_hash: str) -> None:
    """Change a password. The current one is verified by the caller, which holds the hasher."""
    await conn.execute(
        app_user.update().where(app_user.c.id == user_id).values(password_hash=password_hash)
    )


async def set_display_name(conn: AsyncConnection, user_id: int, display_name: str) -> None:
    await conn.execute(
        app_user.update().where(app_user.c.id == user_id).values(display_name=display_name)
    )


async def set_active(conn: AsyncConnection, user_id: int, *, is_active: bool) -> None:
    await conn.execute(
        app_user.update().where(app_user.c.id == user_id).values(is_active=is_active)
    )


_GRANTED = (
    sa.select(sa.func.coalesce(sa.func.sum(user_credit_grant.c.amount_usd), 0))
    .where(user_credit_grant.c.user_id == app_user.c.id)
    .scalar_subquery()
)
_SPENT = (
    sa.select(sa.func.coalesce(sa.func.sum(user_llm_spend.c.cost_usd), 0))
    .where(user_llm_spend.c.user_id == app_user.c.id)
    .scalar_subquery()
)


async def credit(conn: AsyncConnection, user_id: int) -> sa.Row:
    """What this user has been granted, has spent, and has left.

    The balance is derived, never stored. A balance column would be a second record of a fact the
    spend meter already holds, and a run that died between the call and the decrement would leave
    the two disagreeing with no way to tell which was right.
    """
    return (
        await conn.execute(
            sa.select(
                _GRANTED.label("granted_usd"),
                _SPENT.label("spent_usd"),
                (_GRANTED - _SPENT).label("balance_usd"),
            ).where(app_user.c.id == user_id)
        )
    ).one()


async def list_users_with_credit(conn: AsyncConnection) -> list[sa.Row]:
    """Every account with its credit, for the admin page. One query rather than one per user."""
    return list(
        await conn.execute(
            sa.select(
                app_user.c.id,
                app_user.c.email,
                app_user.c.display_name,
                app_user.c.is_admin,
                app_user.c.is_active,
                app_user.c.created_at,
                user_profile.c.scoring_enabled,
                user_profile.c.daily_ceiling_usd,
                _GRANTED.label("granted_usd"),
                _SPENT.label("spent_usd"),
                (_GRANTED - _SPENT).label("balance_usd"),
            )
            .join_from(app_user, user_profile, app_user.c.id == user_profile.c.user_id)
            .order_by(app_user.c.email)
        )
    )


async def grant_credit(
    conn: AsyncConnection,
    user_id: int,
    *,
    amount_usd: Decimal,
    granted_by: int,
    note: str = "",
) -> None:
    """Add credit. Append-only: a top-up is a row, never an edit to a total, so the history of
    who gave what survives and a mistaken grant is corrected by a negative one."""
    await conn.execute(
        user_credit_grant.insert().values(
            user_id=user_id, amount_usd=amount_usd, granted_by=granted_by, note=note
        )
    )


async def credit_grants(conn: AsyncConnection, user_id: int, limit: int = 10) -> list[sa.Row]:
    granter = app_user.alias("granter")
    return list(
        await conn.execute(
            sa.select(
                user_credit_grant.c.amount_usd,
                user_credit_grant.c.note,
                user_credit_grant.c.created_at,
                granter.c.display_name.label("granted_by"),
            )
            .join_from(
                user_credit_grant,
                granter,
                user_credit_grant.c.granted_by == granter.c.id,
                isouter=True,
            )
            .where(user_credit_grant.c.user_id == user_id)
            .order_by(user_credit_grant.c.created_at.desc())
            .limit(limit)
        )
    )


async def today_spend(conn: AsyncConnection, user_id: int) -> sa.Row | None:
    """Spend against today's ceiling, on the installation's clock rather than the server's.

    `date.today()` is the process's local date -- UTC in the container, the operator's zone
    anywhere else -- so the day boundary would move with where this ran, and a ceiling would
    reset at the wrong hour.
    """
    return (
        await conn.execute(
            user_llm_spend.select().where(
                user_llm_spend.c.user_id == user_id,
                user_llm_spend.c.period_day == clock.today(),
            )
        )
    ).one_or_none()


async def month_to_date_spend(conn: AsyncConnection, user_id: int) -> Decimal:
    """This calendar month's spend, summed from the daily rows rather than metered separately:
    two meters for one fact eventually disagree."""
    return (
        await conn.execute(
            sa.select(sa.func.coalesce(sa.func.sum(user_llm_spend.c.cost_usd), 0)).where(
                user_llm_spend.c.user_id == user_id,
                user_llm_spend.c.period_day >= clock.today().replace(day=1),
            )
        )
    ).scalar_one()


async def add_spend(
    conn: AsyncConnection,
    user_id: int,
    *,
    tokens_in: int,
    tokens_out: int,
    cost_usd: Decimal,
) -> Decimal:
    """Record spend and return the new day-to-date total, in USD.

    USD because that is the currency OpenRouter actually bills in. Storing a euro figure would
    require an exchange rate between the meter and the cap, and a stale rate makes a budget limit
    quietly wrong in whichever direction the rate moved.

    Written in the same transaction as the scores it paid for, so a crash cannot leave a user
    holding results they were not charged for or a charge for results they never got.
    """
    stmt = pg_insert(user_llm_spend).values(
        user_id=user_id,
        period_day=clock.today(),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        calls=1,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[user_llm_spend.c.user_id, user_llm_spend.c.period_day],
        set_={
            "tokens_in": user_llm_spend.c.tokens_in + stmt.excluded.tokens_in,
            "tokens_out": user_llm_spend.c.tokens_out + stmt.excluded.tokens_out,
            "cost_usd": user_llm_spend.c.cost_usd + stmt.excluded.cost_usd,
            "calls": user_llm_spend.c.calls + 1,
            "updated_at": sa.func.now(),
        },
    ).returning(user_llm_spend.c.cost_usd)
    return (await conn.execute(stmt)).scalar_one()


async def spend_history(conn: AsyncConnection, user_id: int, days: int = 14) -> list[sa.Row]:
    return list(
        await conn.execute(
            user_llm_spend.select()
            .where(user_llm_spend.c.user_id == user_id)
            .order_by(user_llm_spend.c.period_day.desc())
            .limit(days)
        )
    )
