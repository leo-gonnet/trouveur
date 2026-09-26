"""Table definitions used to build queries.

The authoritative DDL is in alembic/versions/; this mirrors it and the two must be kept in step.
Generated columns and indexes live only in the migration.
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
work_kind = _enum("work_kind", "detail", "derive", "embed", "dedup")
tenant_origin = _enum("tenant_origin", "manual", "discovered")
user_state = _enum("user_state", "new", "saved", "applied", "dismissed")
run_status = _enum("run_status", "queued", "running", "success", "failed", "cancelled")
run_trigger = _enum("run_trigger", "scheduled", "manual")

# Append-only and never pruned: the only input that cannot be recomputed.
source_document = sa.Table(
    "source_document",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("external_id", sa.Text, nullable=False),
    sa.Column("kind", document_kind, nullable=False),
    sa.Column("scope", sa.Text),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("payload_sha256", BYTEA, nullable=False),
    sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.UniqueConstraint(
        "source", "external_id", "kind", "payload_sha256", name="source_document_identity_uniq"
    ),
)

# (source, external_id) is PROVENANCE identity -- one row per posting per board. It is not "the
# same job in the world": that is dedup_group, which only ever marks. Conflating the two silently
# discards every posting that arrives from a second source.
job = sa.Table(
    "job",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("public_id", UUID(as_uuid=True), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("external_id", sa.Text, nullable=False),
    sa.Column("scope", sa.Text),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("company", sa.Text),
    sa.Column("description", sa.Text),
    sa.Column("posted_at", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
    sa.Column("closes_at", sa.DateTime(timezone=True)),
    sa.Column("locations", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("location_text", sa.Text, nullable=False, server_default=""),
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

# Interpretation, versioned separately from normalisation. A row is created with the job at
# derive_version 0, so refilling the derive queue is an index scan rather than an anti-join.
job_facet = sa.Table(
    "job_facet",
    metadata,
    sa.Column("job_id", sa.BigInteger, primary_key=True),
    sa.Column("derive_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("countries", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("regions", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("cities", ARRAY(sa.Text), nullable=False, server_default="{}"),
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

# Open postings ONLY. Closing a job deletes its row here, which keeps the ANN index proportional
# to the live corpus with no is_open flag to drift. An invariant, not an optimisation.
job_embedding = sa.Table(
    "job_embedding",
    metadata,
    sa.Column("job_id", sa.BigInteger, primary_key=True),
    # provider:model:dim, not an integer: an integer cannot express that two rows came from
    # different vector spaces, and mixing spaces returns nonsense neighbours without raising.
    sa.Column("embedding_version", sa.Text, nullable=False),
    sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

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

# `complete` is the lifecycle gate: a posting may only be closed for not being seen if the sweep
# that missed it covered the whole source. Otherwise a truncated sweep empties the corpus.
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

# The crawl set. In the database rather than the repository because more than one thing writes
# it; the cost accepted is that the corpus is not reproducible from a commit alone.
source_tenant = sa.Table(
    "source_tenant",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("scope", sa.Text, primary_key=True),
    # Discovery inserts disabled rows and a human promotes them, so it can never enlarge the
    # crawl, the bill or the politeness budget on its own.
    sa.Column("enabled", sa.Boolean, nullable=False, server_default="false"),
    sa.Column("origin", tenant_origin, nullable=False, server_default="manual"),
    sa.Column("note", sa.Text),
    sa.Column("added_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# Observation, not configuration: source_tenant is the crawl set, this is only what happened when
# we asked. Their predecessor mixed the two and half of it rotted unnoticed.
source_scope_health = sa.Table(
    "source_scope_health",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("scope", sa.Text, primary_key=True),
    sa.Column("last_ok_at", sa.DateTime(timezone=True)),
    sa.Column("last_documents", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_error", sa.Text),
    sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

app_user = sa.Table(
    "app_user",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("email", sa.Text),
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
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("title", sa.Text, nullable=False, server_default=""),
    sa.Column("years_experience", sa.Integer, nullable=False, server_default="0"),
    sa.Column("objectives", sa.Text, nullable=False, server_default=""),
    sa.Column("background", sa.Text, nullable=False, server_default=""),
    sa.Column("languages", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("must_have", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("keywords", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("countries", ARRAY(sa.Text), nullable=False, server_default="{AT,DE,CH}"),
    sa.Column("cities", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("work_modes", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("seniorities", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("employment_types", ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("min_salary_eur_year", sa.Numeric, nullable=False, server_default="0"),
    # The one cost control the user touches. Off means no paid call of any kind runs for
    # them -- scoring, and the query expansion that is also billed. Retrieval stays free and
    # keeps working, so Search is unaffected. Not a SCORING_FIELD: it changes whether we
    # spend, never what a good match is, so flipping it must not invalidate a cached score.
    sa.Column("scoring_enabled", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# The ciphertext never leaves this table: decrypted immediately before a call, never logged, and
# never rendered back to the browser -- the UI shows only a fingerprint.
user_llm_credential = sa.Table(
    "user_llm_credential",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("api_key_encrypted", BYTEA, nullable=False),
    sa.Column("api_key_fingerprint", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    # Unpinned, OpenRouter spreads one model across backends at a wide price spread and differing
    # quantisation, so neither cost nor scores are reproducible.
    sa.Column("provider_pin", sa.Text),
    sa.Column("monthly_budget_usd", sa.Numeric, nullable=False, server_default="5"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# Checked BEFORE each batch, not reported after: a retry loop on someone else's card is not
# something to discover from the user. Metered in USD, the currency OpenRouter bills in.
user_llm_spend = sa.Table(
    "user_llm_spend",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("period_month", sa.Date, primary_key=True),
    sa.Column("tokens_in", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("tokens_out", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("cost_usd", sa.Numeric, nullable=False, server_default="0"),
    sa.Column("calls", sa.Integer, nullable=False, server_default="0"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)

# The per-user overlay on a shared, user-agnostic corpus.
user_job_match = sa.Table(
    "user_job_match",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("job_id", sa.BigInteger, primary_key=True),
    sa.Column("profile_version", sa.Integer, nullable=False),
    sa.Column("retrieval_score", sa.Float),
    sa.Column("dense_rank", sa.Integer),
    sa.Column("lexical_rank", sa.Integer),
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

# A published edition: what this user was shown on one day, frozen.
#
# The score is copied here rather than read back from user_job_match, which holds only the
# CURRENT verdict. That duplication is the whole point: re-scoring under a new profile must not
# rewrite what an earlier day said. Keyed on the day it was published, never on posted_at --
# Greenhouse's discovery lag is 146 days at p90, so a posting published in April and found in
# September would otherwise belong to an edition five months old.
user_edition_item = sa.Table(
    "user_edition_item",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("job_id", sa.BigInteger, primary_key=True),
    sa.Column("profile_version", sa.Integer, nullable=False),
    sa.Column("llm_score", sa.SmallInteger, nullable=False),
    sa.Column("llm_reason", sa.Text, nullable=False, server_default=""),
    sa.Column("llm_red_flags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    # One posting reaches a user once per profile version, ever: only changing the profile can
    # bring it back. pending_rerank already declines to re-score it, but that is a WHERE clause
    # someone can edit, and a posting silently recommended twice reads as the system repeating
    # itself. The database refuses it instead.
    sa.UniqueConstraint(
        "user_id", "job_id", "profile_version", name="uq_edition_item_once_per_version"
    ),
)

# Cached by (content_hash, user, profile_version): a posting is scored once per profile, ever.
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

user_query_expansion = sa.Table(
    "user_query_expansion",
    metadata,
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("profile_version", sa.Integer, primary_key=True),
    sa.Column("expansion_version", sa.Integer, primary_key=True),
    sa.Column("queries", ARRAY(sa.Text), nullable=False, server_default="{}"),
    # Kept apart from the phrases because only the dense arm can use them.
    sa.Column("adverts", ARRAY(sa.Text), nullable=False, server_default="{}"),
    # Distilled once per profile version. The reranker reads it on every batch, so sending the
    # raw field instead would bill a pasted CV once per ten postings.
    sa.Column("background_summary", sa.Text, nullable=False, server_default=""),
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
    # Progress, written while the run is in flight: a run that reports itself only once it is
    # over cannot be told apart from a stuck one.
    sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default="false"),
    sa.Column("sources_total", sa.SmallInteger),
    sa.Column("sources_done", sa.SmallInteger, nullable=False, server_default="0"),
    sa.Column("current_source", sa.Text),
    sa.Column("match_user_id", sa.BigInteger),
)
