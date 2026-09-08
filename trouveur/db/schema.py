"""Table definitions used to build queries.

The authoritative DDL is in alembic/versions/; this mirrors it so SQLAlchemy Core can construct
statements, and the two must be kept in step. Generated columns and index definitions live only in
the migration -- queries do not need them declared to use them.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, ENUM, JSONB, UUID

metadata = sa.MetaData()


def _enum(name: str, *values: str) -> ENUM:
    return ENUM(*values, name=name, create_type=False)


document_kind = _enum("document_kind", "listing", "detail")
salary_period = _enum("salary_period", "YEAR", "MONTH", "WEEK", "DAY", "HOUR", "UNKNOWN")
work_mode = _enum("work_mode", "onsite", "hybrid", "remote", "unknown")
seniority = _enum("seniority", "intern", "junior", "mid", "senior", "lead", "executive", "unknown")
employment_type = _enum(
    "employment_type", "full_time", "part_time", "contract", "temporary",
    "internship", "apprenticeship", "unknown",
)
work_kind = _enum("work_kind", "derive", "embed", "dedup")
rule_verdict = _enum("rule_verdict", "pass", "reject", "unknown")
user_state = _enum("user_state", "new", "saved", "applied", "dismissed")
run_status = _enum("run_status", "queued", "running", "success", "failed")
run_trigger = _enum("run_trigger", "scheduled", "manual")

# The raw archive. Append-only and never pruned: it is the only input that cannot be recomputed.
source_document = sa.Table(
    "source_document",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("external_id", sa.Text, nullable=False),
    sa.Column("kind", document_kind, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("payload_sha256", BYTEA, nullable=False),
    sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.UniqueConstraint(
        "source", "external_id", "kind", "payload_sha256", name="source_document_identity_uniq"
    ),
)

# Provenance identity: one row per posting per board, keyed by (source, external_id). This is
# emphatically NOT "the same job in the world" -- that question is answered by dedup_group below,
# which only ever marks. Conflating the two is how V1 silently discarded every posting that
# reached it from a second source.
job = sa.Table(
    "job",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("public_id", UUID(as_uuid=True), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("external_id", sa.Text, nullable=False),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("company", sa.Text),
    sa.Column("description", sa.Text),
    sa.Column("posted_at", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
    sa.Column("closes_at", sa.DateTime(timezone=True)),
    sa.Column("locations", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("salary_amount_min", sa.Numeric),
    sa.Column("salary_amount_max", sa.Numeric),
    sa.Column("salary_currency", sa.Text),
    sa.Column("salary_period", salary_period, nullable=False, server_default="UNKNOWN"),
    sa.Column("remote_hint", sa.Boolean),
    sa.Column("employment_type_hint", sa.Text),
    sa.Column("agency_hint", sa.Boolean),
    sa.Column("language_hint", sa.Text),
    sa.Column("department_hint", sa.Text),
    sa.Column("content_hash", BYTEA, nullable=False),
    sa.Column("normalize_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("listing_document_id", sa.BigInteger),
    sa.Column("detail_document_id", sa.BigInteger),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.Column("closed_at", sa.DateTime(timezone=True)),
    sa.Column("dedup_group", BYTEA),
    sa.Column("dedup_version", sa.Integer, nullable=False, server_default="0"),
    sa.UniqueConstraint("source", "external_id", name="job_provenance_uniq"),
)

# Interpretation, versioned separately from normalisation because the two are fixed at different
# times for different reasons. A row is created with the job at derive_version 0, so refilling the
# derive queue is an index scan on one column rather than an anti-join.
job_facet = sa.Table(
    "job_facet",
    metadata,
    sa.Column("job_id", sa.BigInteger, primary_key=True),
    sa.Column("derive_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("country", sa.CHAR(2)),
    sa.Column("region", sa.Text),
    sa.Column("city", sa.Text),
    sa.Column("work_mode", work_mode, nullable=False, server_default="unknown"),
    sa.Column("seniority", seniority, nullable=False, server_default="unknown"),
    sa.Column("employment_type", employment_type, nullable=False, server_default="unknown"),
    sa.Column("salary_min_eur_year", sa.Numeric),
    sa.Column("salary_max_eur_year", sa.Numeric),
    sa.Column("salary_annualised", sa.Boolean, nullable=False, server_default="false"),
    sa.Column("language", sa.Text),
    sa.Column("is_agency", sa.Boolean),
    sa.Column("skills", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("derived_at", sa.DateTime(timezone=True)),
)

# Open postings only. Closing a job DELETES its row here, so this table is its own partial index:
# the ANN index stays proportional to the live corpus rather than to all history, with no
# denormalised is_open flag to drift out of sync. The vector is a pure function of job text, so a
# reopened posting is simply re-embedded.
job_embedding = sa.Table(
    "job_embedding",
    metadata,
    sa.Column("job_id", sa.BigInteger, primary_key=True),
    # provider:model:dim, not an integer. An integer cannot express that two rows came from
    # different vector spaces, and mixing spaces in one column is a similarity bug that never
    # raises -- it just quietly returns nonsense neighbours.
    sa.Column("embedding_version", sa.Text, nullable=False),
    sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# The single queue. Deliberately not paired with a separate outbox: "this row needs work" and
# "this row changed, tell a consumer" are the same statement, and two mechanisms for it would be
# two things to keep in sync.
work_item = sa.Table(
    "work_item",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("kind", work_kind, nullable=False),
    sa.Column("job_id", sa.BigInteger, nullable=False),
    sa.Column("target_version", sa.Text, nullable=False),
    sa.Column("not_before", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.Column("attempts", sa.SmallInteger, nullable=False, server_default="0"),
    sa.Column("last_error", sa.Text),
    sa.Column("claimed_at", sa.DateTime(timezone=True)),
    sa.Column("claimed_by", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.UniqueConstraint("kind", "job_id", name="work_item_kind_job_uniq"),
)

# One row per source per sweep. `complete` is the lifecycle gate: a posting may only be closed for
# not being seen if the sweep that failed to see it actually covered the whole source. A truncated
# or partially failed sweep that closed everything it missed would empty the corpus silently.
source_sweep = sa.Table(
    "source_sweep",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("ok", sa.Boolean),
    sa.Column("complete", sa.Boolean, nullable=False, server_default="false"),
    sa.Column("partitions_total", sa.Integer, nullable=False, server_default="0"),
    sa.Column("partitions_done", sa.Integer, nullable=False, server_default="0"),
    sa.Column("partitions_overflowed", sa.Integer, nullable=False, server_default="0"),
    sa.Column("documents_seen", sa.Integer, nullable=False, server_default="0"),
    sa.Column("jobs_upserted", sa.Integer, nullable=False, server_default="0"),
    sa.Column("jobs_closed", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error", sa.Text),
)

# Greenhouse publishes no index of boards, so this is the only list of tenants we can sweep.
# Its own table rather than a profile column: adding a company must not bump anyone's profile
# version, which would invalidate every cached score and bill every user for a re-score.
greenhouse_board = sa.Table(
    "greenhouse_board",
    metadata,
    sa.Column("slug", sa.Text, primary_key=True),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("added_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.Column("last_ok_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
)

app_user = sa.Table(
    "app_user",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("failed_attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("locked_until", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

user_profile = sa.Table(
    "user_profile",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    # Per user, not global. Editing a profile invalidates that user's cached scores and nobody
    # else's, which is only expressible with the version living here.
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("title", sa.Text, nullable=False, server_default=""),
    sa.Column("years_experience", sa.Integer, nullable=False, server_default="0"),
    sa.Column("objectives", sa.Text, nullable=False, server_default=""),
    sa.Column("languages", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("must_have", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("deal_breakers", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("keywords", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("countries", ARRAY(sa.Text), nullable=False, server_default="{AT,DE,CH}"),
    sa.Column("cities", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("work_modes", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("seniorities", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("employment_types", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("min_salary_eur_year", sa.Numeric, nullable=False, server_default="0"),
    sa.Column("retrieval_limit", sa.Integer, nullable=False, server_default="400"),
    sa.Column("rerank_limit", sa.Integer, nullable=False, server_default="150"),
    sa.Column("notify_threshold", sa.SmallInteger, nullable=False, server_default="70"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# Reranking runs on the user's own OpenRouter key. The ciphertext never leaves this table: it is
# decrypted immediately before a call, never logged, and never rendered back into the settings
# form (the UI shows only a fingerprint).
user_llm_credential = sa.Table(
    "user_llm_credential",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("api_key_encrypted", BYTEA, nullable=False),
    sa.Column("api_key_fingerprint", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    # Unpinned, OpenRouter spreads one model across many backends at a wide price spread and
    # differing quantisation, so neither cost nor scores are reproducible.
    sa.Column("provider_pin", sa.Text),
    sa.Column("monthly_budget_eur", sa.Numeric, nullable=False, server_default="5"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# Spend is metered per user per calendar month and checked *before* each batch, not reported
# after. A retry loop on someone else's credit card is not a bug you want reported by the user.
user_llm_spend = sa.Table(
    "user_llm_spend",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("period_month", sa.Date, primary_key=True),
    sa.Column("tokens_in", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("tokens_out", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("cost_eur", sa.Numeric, nullable=False, server_default="0"),
    sa.Column("calls", sa.Integer, nullable=False, server_default="0"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# The per-user overlay on a shared, user-agnostic corpus. Everything V1 hung off the job row
# lives here instead, which is what makes a second user a row rather than a rewrite.
user_job_match = sa.Table(
    "user_job_match",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("job_id", sa.BigInteger, primary_key=True),
    sa.Column("profile_version", sa.Integer, nullable=False),
    sa.Column("retrieval_score", sa.Float),
    sa.Column("dense_rank", sa.Integer),
    sa.Column("lexical_rank", sa.Integer),
    sa.Column("rule_verdict", rule_verdict, nullable=False, server_default="unknown"),
    sa.Column("rule_reason", sa.Text),
    sa.Column("llm_score", sa.SmallInteger),
    sa.Column("llm_reason", sa.Text),
    sa.Column("llm_red_flags", JSONB),
    sa.Column("scored_at", sa.DateTime(timezone=True)),
    sa.Column("notified_at", sa.DateTime(timezone=True)),
    sa.Column("state", user_state, nullable=False, server_default="new"),
    sa.Column("state_changed_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# Scores are cached corpus-wide by (content_hash, profile_version): the same posting scored for
# the same profile is never paid for twice, including after a re-fetch that changed nothing.
llm_score_cache = sa.Table(
    "llm_score_cache",
    metadata,
    sa.Column("content_hash", BYTEA, primary_key=True),
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("profile_version", sa.Integer, primary_key=True),
    sa.Column("score", sa.SmallInteger, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, server_default=""),
    sa.Column("red_flags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# One LLM call per profile version, not per job: the expansion is reused for every retrieval that
# profile performs until the user edits it.
user_query_expansion = sa.Table(
    "user_query_expansion",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("profile_version", sa.Integer, primary_key=True),
    sa.Column("expansion_version", sa.Integer, primary_key=True),
    sa.Column("queries", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

pipeline_schedule = sa.Table(
    "pipeline_schedule",
    metadata,
    sa.Column("id", sa.SmallInteger, primary_key=True),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("run_hour", sa.SmallInteger, nullable=False, server_default="7"),
    sa.Column("run_minute", sa.SmallInteger, nullable=False, server_default="0"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

pipeline_run = sa.Table(
    "pipeline_run",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("trigger", run_trigger, nullable=False),
    sa.Column("status", run_status, nullable=False, server_default="queued"),
    sa.Column("only_source", sa.Text),
    sa.Column("backfill", sa.Boolean, nullable=False, server_default="false"),
    sa.Column("attempts", sa.SmallInteger, nullable=False, server_default="0"),
    sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.Column("not_before", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("report", JSONB),
    sa.Column("error", sa.Text),
)
