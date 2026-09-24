"""What we actually embed: discriminating fields first, then a bounded slice of prose.

An advert is mostly boilerplate, and embedding all of it pulls every posting from one employer
toward the same point. The budget is chosen, not an accidental truncation.
"""

from __future__ import annotations

_DESCRIPTION_BUDGET = 1200


def embedding_text(
    title: str,
    company: str | None,
    locations: list[str],
    description: str | None,
) -> str:
    parts = [title]
    if company:
        parts.append(company)
    if locations:
        parts.append(", ".join(locations))
    if description:
        parts.append(description[:_DESCRIPTION_BUDGET])
    return "\n".join(parts)
