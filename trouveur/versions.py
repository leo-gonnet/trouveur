"""Every derived stage's version, in one file.

Bumping one refills its work queue with every row below the new value, so upgrade and backfill
are the same code path. Bump when the function's OUTPUT would change for input it has already
seen: fixing a parser is a bump, renaming a variable is not.
"""

from __future__ import annotations

DERIVE_VERSION = 3

# Markers only; never merges or deletes rows.
DEDUP_VERSION = 1

# The field set and encoding behind content_hash. Bumping invalidates every cached LLM score and
# bills every user a full re-score, so bump it only when the hash input genuinely changed.
CONTENT_HASH_VERSION = 1

# Up to three LLM calls per profile version, never per job.
QUERY_EXPANSION_VERSION = 3
