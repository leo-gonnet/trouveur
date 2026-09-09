"""Version registry for every derived stage.

Everything downstream of the raw archive is a pure function of it, and each of those functions
carries a version here. Bumping one refills the corresponding work queue with every row below the
new value, so a backfill and an upgrade are the same code path (see AGENTS.md > Architecture).

Bump a version when the function's *output would change* for input it has already seen. Renaming a
variable is not a bump; fixing a parser is.
"""

from __future__ import annotations

# Deterministic facet derivation: location parsing, salary annualisation, seniority, work mode.
#
# 3: a free-text location's country is found wherever it sits, not only in the last comma-part.
#    Sources that write "AT, Vienna" previously derived the country code as the city and no
#    country at all, so every posting from one is re-derived.
DERIVE_VERSION = 3

# Semantic-duplicate marker pass. Markers only; this never merges or deletes rows.
DEDUP_VERSION = 1

# Field set and encoding used by content_hash. Bumping invalidates every cached LLM score and
# forces a full re-score, which costs the users real money -- do it only when the hash input
# genuinely changed, and say so in the commit message.
CONTENT_HASH_VERSION = 1

# Profile -> retrieval query expansion. One LLM call per profile version, not per job.
QUERY_EXPANSION_VERSION = 1
