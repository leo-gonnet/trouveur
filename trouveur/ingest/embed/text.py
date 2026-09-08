"""What we actually embed. One implementation, used at ingest and never re-derived elsewhere.

A vacancy advert is mostly boilerplate -- benefits, equal-opportunity statements, company history.
Embedding all of it pulls every posting from one employer toward the same point and washes out the
role, so the text is assembled deliberately: the discriminating fields first, then a bounded slice
of prose. The model's context is ~512 tokens, so anything past the budget is not truncated by
accident, it is chosen.
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
