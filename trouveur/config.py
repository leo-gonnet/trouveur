"""Settings, sourced from the environment only.

Secrets are delivered by the Compose env_file (.env next to compose.yaml on the host). Never
read a secret from a file inside the repo (see AGENTS.md).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

USER_AGENT = "trouveur/0.2 (+self-hosted job search; contact via repository owner)"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://trouveur@127.0.0.1:5432/trouveur"
    session_secret: str = "dev-only-insecure-secret"
    # Encrypts users' stored LLM API keys. Losing it makes them unrecoverable, which is correct:
    # a key derivable from the database would not be protecting anything.
    encryption_key: str = "dev-only-insecure-encryption-key"

    # Offered as the starting values on a new user's Settings page. There is no installation-wide
    # key: reranking runs on each user's own credential, so model and provider are per user and
    # these are only defaults.
    default_llm_model: str = "deepseek/deepseek-v4-flash"
    # Unpinned, OpenRouter spreads one model across many backends at a wide price spread and
    # differing quantisation, so neither cost nor scores are reproducible. Provider slugs are
    # quantisation-qualified, so this pins fp8.
    default_llm_provider: str | None = "deepinfra/fp8"
    # Backends vary widely in latency for a batch of this size, so the http_timeout_seconds below
    # is far too short to survive a slow one.
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

    # "local-onnx" needs `uv sync --extra embeddings`. "deterministic" produces reproducible but
    # meaningless vectors; it exists for tests and stamps its rows so its use is visible in data.
    embedding_provider: str = "local-onnx"
    embed_batch_size: int = 128

    # Arbeitsagentur only ever exposes a delta, so nothing it returns can prove a posting is gone.
    # Without an age cutoff its open set, and therefore the ANN index, would grow without bound.
    stale_close_days: int = 90

    http_timeout_seconds: float = 30.0
    request_delay_seconds: float = 1.0


def get_settings() -> Settings:
    return Settings()


