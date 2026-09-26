"""Shared installation key, per-user credit, and a ceiling that is a day rather than a month.

Three changes that only make sense together.

Bring-your-own-key made onboarding the hard part: before a new user could be shown a single
recommendation they had to open an OpenRouter account, fund it and paste a key. The key is now an
installation setting (`OPENROUTER_API_KEY`), so `user_llm_credential` -- which existed to hold a
secret we no longer store -- goes, and with it the encryption seam that existed for that one
column. What replaces it is credit an admin grants: a user who has none buys nothing, exactly as a
user with no key bought nothing.

`user_credit_grant` is append-only and a balance is never stored. It is
`SUM(grants) - SUM(spend)`, so it cannot drift from what was actually billed, and every top-up
keeps its own row saying who gave it and when. A mutable balance column would be a second record
of a fact the spend meter already holds, and the two would disagree the first time a run died
between the call and the decrement.

The ceiling moves from a month to a DAY, and onto `user_profile` beside `scoring_enabled` -- the
user's two cost controls, in one place. On the credential it had to be copied forward whenever a
key was replaced, and the form had to distinguish "no value posted" from "reset me to the
default"; a column with a default answers both by existing. Spend is metered per day for the same
reason: a daily ceiling tested against a monthly total is not a daily ceiling.

A month was also the wrong unit for what the cap protects against. A profile change re-scores
whatever survives fusion, thousands of calls in one run, so a monthly cap either lets that run
consume the month or stops it half way -- as an edition that is quietly short, not as an error. A
day paces it, and Settings states the worst case a month of such days costs.

The old monthly spend rows are deleted rather than dated to the 1st. Carrying a month's total onto
one day would put a number the new daily history displays as fact where no day's spending happened.

Revision ID: 0016_credit_accounts
"""

from alembic import op

revision = "0016_credit_accounts"
down_revision = "0015_versioned_editions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app_user ADD COLUMN is_admin boolean NOT NULL DEFAULT false")
    # Whoever was created first on this installation is the admin: the accounts they will create do
    # not exist yet, so on an empty installation there is nobody else it could be.
    op.execute(
        """
        UPDATE app_user SET is_admin = true
         WHERE id = (SELECT id FROM app_user ORDER BY id LIMIT 1)
        """
    )

    op.execute(
        "ALTER TABLE user_profile ADD COLUMN daily_ceiling_usd numeric NOT NULL DEFAULT 0.25"
    )

    # Append-only. granted_by is kept when that admin's own account goes: the grant belongs to the
    # user who received it, and the author is only a label on it -- hence SET NULL, not CASCADE.
    op.execute(
        """
        CREATE TABLE user_credit_grant (
            id         bigserial PRIMARY KEY,
            user_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            granted_by bigint REFERENCES app_user(id) ON DELETE SET NULL,
            amount_usd numeric NOT NULL,
            note       text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_credit_grant_user ON user_credit_grant (user_id, created_at DESC)")

    op.execute("DROP TABLE user_llm_credential")

    # The rows keyed on a month cannot be re-keyed on a day they never named.
    op.execute("DELETE FROM user_llm_spend")
    op.execute("ALTER TABLE user_llm_spend DROP CONSTRAINT user_llm_spend_pkey")
    op.execute("ALTER TABLE user_llm_spend RENAME COLUMN period_month TO period_day")
    op.execute("ALTER TABLE user_llm_spend ADD PRIMARY KEY (user_id, period_day)")


def downgrade() -> None:
    op.execute("ALTER TABLE user_llm_spend DROP CONSTRAINT user_llm_spend_pkey")
    op.execute("DELETE FROM user_llm_spend")
    op.execute("ALTER TABLE user_llm_spend RENAME COLUMN period_day TO period_month")
    op.execute("ALTER TABLE user_llm_spend ADD PRIMARY KEY (user_id, period_month)")

    op.execute(
        """
        CREATE TABLE user_llm_credential (
            user_id            bigint PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
            api_key_encrypted  bytea NOT NULL,
            api_key_fingerprint text NOT NULL,
            model              text NOT NULL,
            provider_pin       text,
            monthly_budget_usd numeric NOT NULL DEFAULT 5,
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("DROP TABLE user_credit_grant")
    op.execute("ALTER TABLE user_profile DROP COLUMN daily_ceiling_usd")
    op.execute("ALTER TABLE app_user DROP COLUMN is_admin")
