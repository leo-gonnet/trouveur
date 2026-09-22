"""The expansion artifacts the code as it stands would produce, cached on disk.

`expansions_llm.json` was written by `generate.py` against prompts that were candidates at the
time. These are different: they come from `trouveur.match.expand` itself -- the same prompts, the
same model, the same provider pin production sends -- so an experiment that starts here is
measuring the deployed path rather than a stand-in for it.

Two variants per persona, because the question brief 04 asks is what the background field buys:
`bg` has it, `nobg` has the same profile with the field cleared. Keeping both under one cache
makes the pair reproducible and means the generation is paid for once.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trouveur.config import get_settings
from trouveur.eval.harness import _ensure_persona, load_personas
from trouveur.match import expand
from trouveur.models import UserProfile

CACHE = Path(__file__).parent / "live_expansions.json"
VARIANTS = ("nobg", "bg")


@dataclass
class Artifacts:
    queries: list[str]
    adverts: list[str]
    summary: str


def variant_profile(profile: UserProfile, variant: str) -> UserProfile:
    """The profile as one variant sees it. `nobg` is today's profile, before brief 04."""
    return profile if variant == "bg" else profile.model_copy(update={"background": ""})


async def _generate(
    settings, profile: UserProfile, key: str, model: str
) -> tuple[Artifacts, Decimal]:
    pin = settings.default_llm_provider
    cost = Decimal(0)

    phrases, usage = await expand.expand_with_model(
        settings, profile, api_key=key, model=model, provider_pin=pin
    )
    cost += usage.cost_usd
    adverts, usage = await expand.expand_adverts(
        settings, profile, api_key=key, model=model, provider_pin=pin
    )
    cost += usage.cost_usd

    summary = ""
    if profile.background.strip():
        summary, usage = await expand.summarise_background(
            settings, profile, api_key=key, model=model, provider_pin=pin
        )
        cost += usage.cost_usd

    return Artifacts(expand.combine(expand.deterministic_queries(profile), phrases),
                     adverts, summary), cost


async def load(*, regenerate: bool = False) -> dict[str, Artifacts]:
    """Every persona's artifacts in both variants, keyed `<persona>:<variant>`."""
    cache = json.loads(CACHE.read_text("utf-8")) if CACHE.exists() and not regenerate else {}
    settings = get_settings()
    model = os.environ.get("TROUVEUR_EVAL_LLM_MODEL") or settings.default_llm_model
    total = Decimal(0)

    for persona in load_personas():
        stored = await _ensure_persona(persona)
        for variant in VARIANTS:
            slot = f"{persona['key']}:{variant}"
            if slot in cache:
                continue
            artifacts, cost = await _generate(
                settings, variant_profile(stored, variant),
                os.environ["TROUVEUR_EVAL_LLM_KEY"], model,
            )
            total += cost
            cache[slot] = {
                "queries": artifacts.queries,
                "adverts": artifacts.adverts,
                "summary": artifacts.summary,
            }
            print(f"  {slot:<32} {len(artifacts.queries)} queries, "
                  f"{len(artifacts.adverts)} adverts, {len(artifacts.summary)} chars of summary",
                  flush=True)
            CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")

    if total:
        print(f"  generation cost ${total:.5f}")
    return {slot: Artifacts(**value) for slot, value in cache.items()}


if __name__ == "__main__":
    asyncio.run(load(regenerate=bool(os.environ.get("EVALX_REGENERATE"))))
