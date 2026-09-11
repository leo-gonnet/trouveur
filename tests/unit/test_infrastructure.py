"""Guards for the embedding seam, credential handling, the work queue and the schedule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trouveur.ingest.embed import EMBEDDING_DIM, DeterministicProvider, version_of
from trouveur.ingest.embed.text import embedding_text
from trouveur.runner.service import is_scheduled_run_due
from trouveur.work import WorkKind
from trouveur.work.queue import _stale_query


class _Schedule:
    def __init__(self, enabled=True, run_hour=7, run_minute=0):
        self.enabled = enabled
        self.run_hour = run_hour
        self.run_minute = run_minute


async def test_embedding_provider_is_deterministic_and_correctly_sized():
    provider = DeterministicProvider()
    first = await provider.embed_documents(["Wirtschaftsingenieur München"])
    again = await provider.embed_documents(["Wirtschaftsingenieur München"])
    assert first == again
    assert len(first[0]) == EMBEDDING_DIM


async def test_documents_and_queries_are_embedded_asymmetrically():
    """e5 models are trained with distinct passage/query prefixes.

    Embedding both sides identically costs real recall while looking like it works, so the
    provider owns the prefixes rather than trusting every caller to remember them.
    """
    provider = DeterministicProvider()
    as_document = await provider.embed_documents(["process engineer"])
    as_query = await provider.embed_queries(["process engineer"])
    assert as_document != as_query


def test_embedding_version_names_the_vector_space():
    """An integer could not express that two rows came from different spaces.

    Mixing spaces in one column returns confident nonsense rather than an error, so the version
    string carries provider, model and width.
    """
    version = version_of(DeterministicProvider())
    assert version == "deterministic:sha256:384"
    assert str(EMBEDDING_DIM) in version


def test_embedding_text_puts_discriminating_fields_before_prose():
    text = embedding_text("Process Engineer", "ACME", ["Wien"], "x" * 5000)
    assert text.startswith("Process Engineer\nACME\nWien")
    # Bounded on purpose: the model's context is finite, so the cut is chosen, not accidental.
    assert len(text) < 2000


def test_credentials_round_trip_without_leaking_the_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "a-test-encryption-secret")
    from trouveur import crypto

    crypto._cipher.cache_clear()
    from trouveur.config import get_settings

    assert get_settings().encryption_key == "a-test-encryption-secret"

    secret = "sk-or-v1-supersecrettoken"
    token = crypto.encrypt(secret)
    assert crypto.decrypt(token) == secret
    assert secret.encode() not in token
    # The fingerprint must not be key material: it is shown in a UI and pasted into support threads.
    assert secret[-4:] not in crypto.fingerprint(secret)
    crypto._cipher.cache_clear()


def test_encryption_refuses_the_development_default(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "dev-only-insecure-encryption-key")
    from trouveur import crypto

    crypto._cipher.cache_clear()
    with pytest.raises(crypto.CredentialError, match="ENCRYPTION_KEY"):
        crypto.encrypt("anything")
    crypto._cipher.cache_clear()


def test_detail_work_is_never_refilled():
    """Only the source knows whether it has a detail phase.

    Refilling detail centrally would mean encoding that per-source fact in the queue, which is
    exactly the leak the registry exists to prevent.
    """
    with pytest.raises(ValueError, match="never refilled"):
        _stale_query(WorkKind.DETAIL, "1", 0, 10)


def test_refill_queries_select_only_out_of_date_rows():
    for kind in (WorkKind.DERIVE, WorkKind.DEDUP, WorkKind.EMBED):
        sql = str(_stale_query(kind, "2", 0, 10).compile())
        assert "LIMIT" in sql
        # Keyset, not OFFSET: this runs over the whole corpus after a version bump, and OFFSET
        # would re-scan everything it had already skipped on every successive chunk.
        assert "OFFSET" not in sql


def test_embed_refill_ignores_closed_postings():
    sql = str(_stale_query(WorkKind.EMBED, "v", 0, 10).compile())
    assert "closed_at IS NULL" in sql


def test_a_missed_schedule_slot_runs_once_when_the_runner_returns():
    schedule = _Schedule(run_hour=7)
    now = datetime(2026, 9, 8, 11, 0, tzinfo=UTC)
    # Runner was down over the 07:00 slot: it should run now...
    assert is_scheduled_run_due(schedule, now, datetime(2026, 9, 7, 7, 0, tzinfo=UTC)) is True
    # ...but exactly once, not repeatedly to "catch up".
    assert is_scheduled_run_due(schedule, now, datetime(2026, 9, 8, 7, 30, tzinfo=UTC)) is False


def test_schedule_does_not_fire_before_its_slot():
    schedule = _Schedule(run_hour=7)
    before = datetime(2026, 9, 8, 6, 59, tzinfo=UTC)
    assert is_scheduled_run_due(schedule, before, None) is False


def test_disabled_schedule_never_fires():
    schedule = _Schedule(enabled=False)
    now = datetime.now(UTC) + timedelta(hours=1)
    assert is_scheduled_run_due(schedule, now, None) is False


def test_the_image_prewarms_the_model_the_code_actually_loads():
    """The Dockerfile must import the model name, never spell it out again.

    It did spell it out, and the two drifted: the image cached
    intfloat/multilingual-e5-small while the app loads
    paraphrase-multilingual-MiniLM-L12-v2. The layer exists precisely to stop the runner
    downloading a model on first scan, so a mismatch defeats it silently -- and stayed silent
    until a fastembed release dropped the stale name and failed the build instead.
    """
    from pathlib import Path

    from trouveur.ingest.embed.local import MODEL

    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text("utf-8")
    # Comments may name the old model to explain the trap; only the instructions must not.
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    prewarm = instructions.split("FASTEMBED_CACHE_PATH", 1)[1]
    assert "from trouveur.ingest.embed.local import MODEL" in prewarm
    assert "model_name=MODEL" in prewarm
    assert MODEL not in instructions
    assert "intfloat/" not in instructions
