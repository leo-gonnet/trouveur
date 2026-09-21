"""Generate the proposed expansion artifacts with the real production LLM path.

The frozen fixtures in expansions.json were written by a large model and set an upper bound on
quality. What ships would be the cheap model production already pins, so the same three artifacts
are generated here through trouveur.match.llm -- same transport, same provider pin, same
temperature -- and evaluated beside them. A proposal that only works when a frontier model writes
the adverts is a different proposal.

Adverts are generated at two lengths on purpose. The query encoder truncates at 128 tokens
(measured, not assumed), so a 200-word advert is silently cut to its first third; asking for both
lengths isolates whether that matters from whether the model is any good.
"""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

from trouveur.config import get_settings
from trouveur.eval.harness import _ensure_persona, load_personas
from trouveur.match import expand, llm

MAX_TOKENS = int(os.environ.get('EVALX_MAX_TOKENS', '6000'))
OUT = Path(__file__).parent / "expansions_llm.json"

ADS_LONG = """You write synthetic job adverts for a search engine.

Given a candidate profile, write 15 job adverts for roles this candidate would realistically be
hired into, today, with the experience they have. These are not adverts for their current job:
they are the adverts a good recruiter would send them.

Each advert is about 200 words, written as a real posting: a title line, what the role does, and
what it requires. Vary the roles across the plausible range -- same role at different kinds of
employer, and the adjacent roles the profile opens. Write in German or English as the corpus is
DACH-wide and mixes both; match the candidate's languages.

No company names, no locations, no salary, no seniority labels.

Return ONLY a JSON array of 15 strings."""

ADS_SHORT = ADS_LONG.replace(
    "Each advert is about 200 words", "Each advert is about 70 words, densely worded"
)

TITLES = """You expand a candidate profile into job titles for a search engine.

Return 30 job titles this candidate would plausibly be hired under: the common synonyms of their
role, the same role written the way different employers write it, and the adjacent roles their
objectives open. German and English both, because the corpus is DACH-wide.

No locations, no seniority words, no company names.

Return ONLY a JSON array of 30 strings."""

TOKENS = """You extract high-signal search tokens from a candidate profile.

Return 20 rare, exact tokens a matching job advert would contain literally: specific tools,
frameworks, standards, certifications, regulations, methods. Prefer tokens that are rare in a job
corpus and therefore discriminating; skip common words like "team", "agile" or "software".

Only tokens the profile implies -- what the candidate has, plus spellings and variants of it. Do
not invent tools someone with this title might plausibly know.

Return ONLY a JSON array of 20 strings."""


async def one(settings, profile, system: str, key: str, model: str, cap: int):
    completion = await llm.complete(
        api_key=key, model=model, provider_pin=settings.default_llm_provider,
        system=system, user=expand.build_prompt(profile),
        max_tokens=MAX_TOKENS, timeout=120.0,
    )
    text = completion.text
    start, end = text.find("["), text.rfind("]")
    try:
        items = json.loads(text[start : end + 1]) if start != -1 else []
    except json.JSONDecodeError as exc:
        print(f"    ! JSON decode failed: {exc}; truncated={completion.truncated} "
              f"chars={len(text)} out_tokens={completion.usage.tokens_out}")
        items = []
    # The cheap model answers "a JSON array of strings" with an array of {title, description}
    # objects perhaps a third of the time. Production's parse_response filters those out and
    # silently returns nothing, which is worth fixing there too; here they are accepted so the
    # proposal is judged on its content and not on one model's JSON habits.
    flat = []
    for item in items:
        if isinstance(item, str) and item.strip():
            flat.append(item.strip())
        elif isinstance(item, dict):
            parts = [str(v).strip() for v in item.values() if isinstance(v, str | int | float)]
            if parts:
                flat.append(". ".join(p for p in parts if p))
    items = flat[:cap]
    if len(items) < cap:
        print(f"    ! got {len(items)}/{cap}; truncated={completion.truncated} "
              f"out_tokens={completion.usage.tokens_out}")
    return items, completion.usage


async def main() -> None:
    key = os.environ["TROUVEUR_EVAL_LLM_KEY"]
    settings = get_settings()
    model = os.environ.get("TROUVEUR_EVAL_LLM_MODEL") or settings.default_llm_model
    out = {"model": model, "personas": {}}
    cost = Decimal(0)

    for persona in load_personas():
        profile = await _ensure_persona(persona)
        got = {}
        for name, system, cap in (
            ("ads_long", ADS_LONG, 15),
            ("ads_short", ADS_SHORT, 15),
            ("titles", TITLES, 30),
            ("tokens", TOKENS, 20),
        ):
            items, usage = await one(settings, profile, system, key, model, cap)
            cost += usage.cost_usd
            got[name] = items
            words = [len(i.split()) for i in items] or [0]
            print(
                f"{persona['key']:<22} {name:<10} {len(items):>3} items  "
                f"median {sorted(words)[len(words)//2]:>3} words  ${usage.cost_usd:.5f}"
            )
        # The production prompt as it stands today, for the with-key baseline.
        phrases, usage = await expand.expand_with_model(
            settings, profile, api_key=key, model=model,
            provider_pin=settings.default_llm_provider,
        )
        cost += usage.cost_usd
        got["phrases_production"] = phrases
        print(f"{persona['key']:<22} {'phrases':<10} {len(phrases):>3} items  "
              f"${usage.cost_usd:.5f}")
        out["personas"][persona["key"]] = got

    out["cost_usd"] = str(cost)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\ntotal ${cost:.5f} -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
