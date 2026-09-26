"""Email is the login; a display name is what the UI shows; the digest gets a real switch.

Two identifiers for one account was one too many. `email` already existed on the row, the digest
already required it, and an admin creating an account already typed it -- so `username` was a
second name to invent, keep unique and keep in step, earning nothing.

What the topbar shows is a separate question from what identifies the account, so it becomes its
own column rather than disappearing: an address is long, and it would otherwise sit in every
screenshot. Existing rows carry their username over, which is exactly what those users see today;
an account created later without one takes the part before the `@`.

The unique index is on `lower(email)`, not on `email`. Case-folding at the write alone leaves the
database willing to hold `Leo@x.com` beside `leo@x.com`, and the second account is invisible until
somebody cannot log in -- the address they typed matches a row, just not theirs. Existing addresses
are folded here for the same reason the index exists.

Emails that are missing become `<username>@invalid`. `.invalid` is reserved by RFC 2606 and can
never resolve, so nothing is ever mailed there by accident, and it is obvious on the Users page
that it wants replacing. Deriving something plausible-looking instead would produce an address that
might belong to a real person.

`digest_enabled` restores an opt-out that this change would otherwise have removed in silence: a
missing email was the only way not to receive a digest, and every address is now present. It sits
beside `scoring_enabled` -- the user's switches, in one place -- and defaults to on, because a
handful of users who each asked for an account want the mail.

Revision ID: 0017_email_logins
"""

from alembic import op

revision = "0017_email_logins"
down_revision = "0016_credit_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE app_user
           SET email = username || '@invalid'
         WHERE email IS NULL OR btrim(email) = ''
        """
    )
    op.execute("UPDATE app_user SET email = lower(btrim(email))")

    op.execute("ALTER TABLE app_user ADD COLUMN display_name text NOT NULL DEFAULT ''")
    op.execute("UPDATE app_user SET display_name = username")

    op.execute("ALTER TABLE app_user ALTER COLUMN email SET NOT NULL")
    op.execute("CREATE UNIQUE INDEX uq_app_user_email ON app_user (lower(email))")
    op.execute("ALTER TABLE app_user DROP COLUMN username")

    op.execute(
        "ALTER TABLE user_profile ADD COLUMN digest_enabled boolean NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_profile DROP COLUMN digest_enabled")

    # Lossy in one direction only: a display name is not guaranteed unique, so the username it
    # becomes is disambiguated by id rather than colliding.
    op.execute("ALTER TABLE app_user ADD COLUMN username text")
    op.execute(
        """
        UPDATE app_user a
           SET username = CASE
                 WHEN (SELECT count(*) FROM app_user b WHERE b.display_name = a.display_name) > 1
                 THEN a.display_name || '-' || a.id
                 ELSE a.display_name
               END
        """
    )
    op.execute("ALTER TABLE app_user ALTER COLUMN username SET NOT NULL")
    op.execute("ALTER TABLE app_user ADD CONSTRAINT app_user_username_key UNIQUE (username)")

    op.execute("DROP INDEX uq_app_user_email")
    op.execute("ALTER TABLE app_user ALTER COLUMN email DROP NOT NULL")
    op.execute("ALTER TABLE app_user DROP COLUMN display_name")
