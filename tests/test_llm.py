from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from trouveur.config import Settings
from trouveur.filters import llm
from trouveur.filters.llm import build_prompt, parse_response
from trouveur.models import ProfileData


def row(**kw):
    base = dict(id=1, title="Wirtschaftsingenieur", company="ACME", location_city="Wien",
                location_country="AT", remote=True, salary_min=60000, salary_max=70000,
                description="Prozessoptimierung")
    return SimpleNamespace(**{**base, **kw})


def test_parses_a_clean_json_array():
    scores = parse_response('[{"id": 1, "score": 88, "reason": "good", "red_flags": []}]')
    assert len(scores) == 1
    assert scores[0].score == 88


def test_parses_json_wrapped_in_a_code_fence():
    scores = parse_response('```json\n[{"id": 2, "score": 40, "reason": "meh"}]\n```')
    assert scores[0].id == 2 and scores[0].score == 40


def test_parses_json_with_surrounding_prose():
    scores = parse_response(
        'Here you go:\n[{"id": 3, "score": 10, "reason": "no"}]\nHope that helps'
    )
    assert scores[0].id == 3


def test_malformed_response_yields_nothing_rather_than_zero_scores():
    """A parse failure must mean 'unscored', never a real score of 0 that hides a good job."""
    assert parse_response("the model apologises and refuses") == []
    assert parse_response("[{broken json") == []


def test_discards_entries_with_out_of_range_scores():
    scores = parse_response('[{"id": 1, "score": 900, "reason": "x"}, {"id": 2, "score": 50}]')
    assert [s.id for s in scores] == [2]


def test_prompt_contains_profile_and_every_job_id():
    prompt = build_prompt(
        ProfileData(title="Wirtschaftsingenieur", objectives="Remote roles in Wien"),
        [row(id=11), row(id=22)],
    )
    assert "Remote roles in Wien" in prompt
    assert "id=11" in prompt and "id=22" in prompt


def test_prompt_truncates_very_long_descriptions():
    prompt = build_prompt(ProfileData(), [row(description="x" * 10_000)])
    assert len(prompt) < 4_000


class _Capture:
    """Stand-in for OpenRouter. Records the request body and replays a canned response."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.body: dict = {}
        self.headers: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.body = json.loads(request.content)
        self.headers = dict(request.headers)
        return httpx.Response(self.status, json=self.payload)


def _patched_client(monkeypatch, capture: _Capture) -> None:
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        return original(*args, **kwargs, transport=httpx.MockTransport(capture.handler))

    monkeypatch.setattr(llm.httpx, "AsyncClient", factory)


def _reply(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}


async def test_score_batch_sends_the_pinned_provider_and_disables_reasoning(monkeypatch):
    """Both are load-bearing: unpinned routing is unreproducible, and a reasoning model left
    enabled burns the whole token budget and returns nothing."""
    capture = _Capture(_reply('[{"id": 1, "score": 91, "reason": "fits"}]'))
    _patched_client(monkeypatch, capture)
    settings = Settings(
        openrouter_api_key="sk-or-test", llm_model="deepseek/deepseek-v4-flash",
        llm_provider="deepinfra/fp8",
    )

    scores = await llm.score_batch(settings, ProfileData(), [row(id=1)])

    assert [s.score for s in scores] == [91]
    assert capture.body["reasoning"] == {"enabled": False}
    assert capture.body["provider"]["order"] == ["deepinfra/fp8"]
    assert capture.body["provider"]["allow_fallbacks"] is False
    assert capture.body["provider"]["data_collection"] == "deny"
    assert capture.body["model"] == "deepseek/deepseek-v4-flash"
    assert capture.body["temperature"] == 0
    assert capture.headers["authorization"] == "Bearer sk-or-test"


async def test_score_batch_omits_provider_routing_when_unpinned(monkeypatch):
    capture = _Capture(_reply('[{"id": 1, "score": 50}]'))
    _patched_client(monkeypatch, capture)
    settings = Settings(openrouter_api_key="sk-or-test", llm_provider=None)

    await llm.score_batch(settings, ProfileData(), [row(id=1)])

    assert "provider" not in capture.body


async def test_score_batch_without_a_key_is_an_error_not_a_silent_zero():
    settings = Settings(openrouter_api_key=None)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await llm.score_batch(settings, ProfileData(), [row(id=1)])


async def test_empty_batch_never_calls_the_api(monkeypatch):
    capture = _Capture(_reply("[]"))
    _patched_client(monkeypatch, capture)
    assert await llm.score_batch(Settings(openrouter_api_key="sk"), ProfileData(), []) == []
    assert capture.body == {}


async def test_truncated_response_is_unscored_rather_than_a_partial_batch(monkeypatch):
    """finish_reason=length means the array is cut off; the parser must not invent scores."""
    capture = _Capture(_reply('[{"id": 1, "score": 90, "red_flags": ["a"]', finish="length"))
    _patched_client(monkeypatch, capture)
    settings = Settings(openrouter_api_key="sk-or-test")

    assert await llm.score_batch(settings, ProfileData(), [row(id=1)]) == []
