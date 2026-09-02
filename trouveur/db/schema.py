"""Table definitions used to build queries.

The authoritative DDL is in alembic/versions/. This mirrors it for query construction; the two
must be kept in step. Generated/expression columns (search_de and the GIN indexes) exist only in
the migration because SQLAlchemy Core does not need to know about them to query.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, ENUM, JSONB

metadata = sa.MetaData()

user_state_enum = ENUM(
    "new", "interested", "applied", "rejected", name="user_state", create_type=False
)
rule_verdict_enum = ENUM("pass", "reject", "unknown", name="rule_verdict", create_type=False)
salary_period_enum = ENUM(
    "YEAR", "MONTH", "WEEK", "DAY", "HOUR", "UNKNOWN", name="salary_period", create_type=False
)
pipeline_run_status_enum = ENUM(
    "queued", "running", "success", "failed", name="pipeline_run_status", create_type=False
)
pipeline_run_trigger_enum = ENUM(
    "scheduled", "manual", name="pipeline_run_trigger", create_type=False
)

job = sa.Table(
    "job",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("source_native_id", sa.Text, nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("company", sa.Text),
    sa.Column("location_city", sa.Text),
    sa.Column("location_country", sa.Text),
    sa.Column("remote", sa.Boolean),
    sa.Column("salary_min", sa.Numeric),
    sa.Column("salary_max", sa.Numeric),
    sa.Column("salary_period", salary_period_enum, nullable=False, server_default="UNKNOWN"),
    sa.Column("posted_at", sa.DateTime(timezone=True)),
    sa.Column("description", sa.Text),
    sa.Column("raw", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("content_hash", BYTEA, nullable=False),
    sa.Column("search_norm", sa.Text, nullable=False, server_default=""),
    sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("rule_verdict", rule_verdict_enum, nullable=False, server_default="unknown"),
    sa.Column("llm_score", sa.SmallInteger),
    sa.Column("llm_reason", sa.Text),
    sa.Column("llm_red_flags", JSONB),
    sa.Column("notified_at", sa.DateTime(timezone=True)),
    sa.Column("user_state", user_state_enum, nullable=False, server_default="new"),
    sa.UniqueConstraint("source", "source_native_id", name="job_source_native_uniq"),
    sa.UniqueConstraint("content_hash", name="job_content_hash_uniq"),
)

profile = sa.Table(
    "profile",
    metadata,
    sa.Column("id", sa.SmallInteger, primary_key=True, server_default="1"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("title", sa.Text, nullable=False, server_default=""),
    sa.Column("years_experience", sa.Integer, nullable=False, server_default="0"),
    sa.Column("languages", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("objectives", sa.Text, nullable=False, server_default=""),
    sa.Column("must_have", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("deal_breakers", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("keywords", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("cities", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("countries", ARRAY(sa.Text), nullable=False, server_default="{AT,DE,CH}"),
    sa.Column("remote_only", sa.Boolean, nullable=False, server_default="false"),
    sa.Column("min_salary_eur_year", sa.Numeric, nullable=False, server_default="0"),
    sa.Column("notify_threshold", sa.SmallInteger, nullable=False, server_default="70"),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

app_user = sa.Table(
    "app_user",
    metadata,
    sa.Column("id", sa.SmallInteger, primary_key=True, server_default="1"),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("failed_attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("locked_until", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

source_state = sa.Table(
    "source_state",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("cursor", sa.Text),
    sa.Column("etag", sa.Text),
    sa.Column("last_run_at", sa.DateTime(timezone=True)),
    sa.Column("last_success_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("jobs_seen", sa.Integer, nullable=False, server_default="0"),
)

llm_score_cache = sa.Table(
    "llm_score_cache",
    metadata,
    sa.Column("content_hash", BYTEA, primary_key=True),
    sa.Column("profile_version", sa.Integer, nullable=False),
    sa.Column("score", sa.SmallInteger, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("red_flags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# Owned by the runner service. The web UI enqueues pipeline_run rows and reads them back; it
# never executes a scan itself (see AGENTS.md).
pipeline_schedule = sa.Table(
    "pipeline_schedule",
    metadata,
    sa.Column("id", sa.SmallInteger, primary_key=True, server_default="1"),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("run_hour", sa.SmallInteger, nullable=False, server_default="7"),
    sa.Column("run_minute", sa.SmallInteger, nullable=False, server_default="0"),
    sa.Column("lookback_days", sa.SmallInteger, nullable=False, server_default="1"),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# Edited in the web UI. Separate from `profile` on purpose: adding a company must not bump
# profile.version, which would invalidate every cached LLM score.
personio_tenant = sa.Table(
    "personio_tenant",
    metadata,
    sa.Column("slug", sa.Text, primary_key=True),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

pipeline_run = sa.Table(
    "pipeline_run",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("trigger", pipeline_run_trigger_enum, nullable=False),
    sa.Column("status", pipeline_run_status_enum, nullable=False, server_default="queued"),
    sa.Column("lookback_days", sa.SmallInteger, nullable=False, server_default="1"),
    sa.Column("use_llm", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("send_email", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("attempts", sa.SmallInteger, nullable=False, server_default="0"),
    sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("not_before", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("report", JSONB),
    sa.Column("error", sa.Text),
)
