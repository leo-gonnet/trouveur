"""OpenRouter transport and cost metering. All prompt text for scoring lives in rerank.py.

Every call is made with a user's own key, so this module never reads a global credential and the
system has no LLM spend of its own. Two consequences shape the code: a failure is one user's
problem and must not touch another's run, and the cost of a call is that user's money, so it is
measured from the response rather than estimated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class LlmError(Exception):
    """A model call failed. The batch is unscored, never scored zero."""


class BudgetExceeded(LlmError):
    """The user's monthly ceiling would be passed by this call, so it was not made."""


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)


@dataclass
class Completion:
    text: str
    usage: Usage
    truncated: bool = False


async def complete(
    *,
    api_key: str,
    model: str,
    provider_pin: str | None,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> Completion:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        # Without this a reasoning model spends the entire token budget on hidden thinking and
        # returns empty content: the batch is lost and billed anyway.
        "reasoning": {"enabled": False},
        # Ask for the real charge rather than inferring one from list prices, which several
        # providers do not bill by.
        "usage": {"include": True},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if provider_pin:
        body["provider"] = {
            "order": [provider_pin],
            "allow_fallbacks": False,
            # The user's profile and advert text leave our infrastructure here. Never route them
            # to a backend that may train on them.
            "data_collection": "deny",
        }

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(
                ENDPOINT,
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "X-Title": "trouveur"},
            )
        except httpx.HTTPError as exc:
            raise LlmError(f"The model request could not be completed: {exc}") from exc

    if response.status_code == 401:
        raise LlmError("OpenRouter rejected the API key. Check it in Settings.")
    if response.status_code == 402:
        raise LlmError("The OpenRouter account has insufficient credit for this request.")
    if response.status_code == 429:
        raise LlmError("OpenRouter is rate-limiting this key; the batch will be retried later.")
    if response.status_code != 200:
        raise LlmError(f"OpenRouter returned HTTP {response.status_code}.")

    payload = response.json()
    usage = _usage(payload.get("usage") or {})
    choices = payload.get("choices") or []
    if not choices:
        raise LlmError("The model returned no choices.")
    message = choices[0].get("message") or {}
    return Completion(
        text=message.get("content") or "",
        usage=usage,
        truncated=choices[0].get("finish_reason") == "length",
    )


def _usage(node: dict) -> Usage:
    return Usage(
        tokens_in=int(node.get("prompt_tokens") or 0),
        tokens_out=int(node.get("completion_tokens") or 0),
        cost_usd=Decimal(str(node.get("cost") or 0)),
    )
