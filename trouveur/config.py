"""Settings, sourced from the environment only.

Secrets are delivered by the Compose env_file (.env next to compose.yaml on the host). Never
read a secret from a file inside the repo (see AGENTS.md).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict

USER_AGENT = "trouveur/0.2 (+self-hosted job search; contact via repository owner)"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # The wall clock this installation reads. Every timestamp is STORED in UTC and stays that
    # way; this is what UTC is turned into at the edges -- displayed times, the scan hour the
    # operator typed, and the day an edition is published into. Not per user: the nightly scan is
    # shared, so its hour needs one answer, and the corpus is one region's boards.
    timezone: str = "Europe/Vienna"

    # What the deploy pulled. deploy.yml rewrites this in .env to the pushed commit and compose
    # resolves the image tag from it, so it cannot disagree with the code that is running: a
    # footer showing the wrong commit would mean the wrong container is up.
    trouveur_tag: str = ""

    database_url: str = "postgresql+asyncpg://trouveur@127.0.0.1:5432/trouveur"
    session_secret: str = "dev-only-insecure-secret"
    encryption_key: str = "dev-only-insecure-encryption-key"

    # Installation settings, shown read-only on Settings. Never per user: a user-chosen model
    # makes scores incomparable across users and lets the provider pin be cleared.
    default_llm_model: str = "deepseek/deepseek-v4-flash"
    # Unpinned, OpenRouter spreads one model across backends at a wide price spread and differing
    # quantisation, so neither cost nor scores are reproducible.
    default_llm_provider: str | None = "deepinfra/fp8"
    # The spending ceiling, in USD, the currency OpenRouter bills in -- an EUR setting would put a
    # stale exchange rate between the meter and the cap. Installation-wide for the same reason as
    # the model: the per-user control is the on/off switch, and a ceiling a user can raise is not
    # a ceiling. Copied onto a credential when it is saved, which is what the batch check reads.
    monthly_budget_usd: Decimal = Decimal(5)
    # Backends vary widely in latency, so http_timeout_seconds is far too short here.
    llm_timeout_seconds: float = 300.0

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None

    # Stays true even over an SSH tunnel: http://localhost is a secure context in modern browsers.
    cookie_secure: bool = True
    session_max_age_days: int = 30
    max_login_attempts: int = 5
    lockout_minutes: int = 15

    # "local-onnx" needs `uv sync --extra embeddings`.
    embedding_provider: str = "local-onnx"
    # The width the embed worker WRITES; a migration owns the column, this lets the provider
    # guard reject a mismatch loudly rather than storing a meaningless vector.
    embedding_dim: int = 384
    # The width, and then the model, retrieval READS. Both differ from the write side only for
    # the length of a model backfill, while the worker fills the new space and the dense arm
    # serves the old one. A width alone cannot say which model produced a space.
    embedding_read_dim: int = 384
    embedding_read_provider: str = ""
    embed_batch_size: int = 128

    # Comma separated. A source is retired by naming it here, never by deleting its adapter.
    disabled_sources: str = ""

    # How old a posting may be and still be recommended. Measured discovery lag is under 1.3 days
    # at p99 for every source that carries volume, so this drops old inventory, never a posting
    # we were merely slow to find.
    retrieval_horizon_days: int = 7

    # A delta source can never prove a posting is gone, so without an age cutoff its open set --
    # and the ANN index -- would grow without bound.
    stale_close_days: int = 90

    # Offsite copy of the archive, which lives on one disk. A `<owner>/<name>` Hugging Face
    # dataset repository, or `local:<path>` to rehearse into a directory without a token.
    archive_repo: str = ""
    # Scope this to that one repository: it is a credential for an account, not for a bucket.
    archive_token: str | None = None

    http_timeout_seconds: float = 30.0
    request_delay_seconds: float = 1.0


def get_settings() -> Settings:
    return Settings()


