"""The free deterministic cut between retrieval and reranking.

Its only job is to make the paid stage smaller without costing recall, so the bias is explicit:
when a signal is missing or ambiguous, PASS. A false pass costs a fraction of a cent at the
reranker; a false reject means the user never sees a job they wanted, and never finds out.

Hard structural filters (country, work mode, seniority, salary floor) already ran in SQL during
retrieval. What is left here is the text-level judgement that cannot be indexed.
"""

from __future__ import annotations

from trouveur.models import Candidate, RuleVerdict, UserProfile, fold

# Role types that are never a fit for an experienced hire and appear constantly in DACH listings.
# Matched against the title only: a senior advert that merely mentions supervising Werkstudenten
# is not itself a Werkstudent role.
DEFAULT_EXCLUDES = (
    "praktikum", "praktikant", "werkstudent", "ausbildung", "auszubildende",
    "duales studium", "schulerpraktikum", "ferialjob", "schnupperlehre",
)


def evaluate(candidate: Candidate, profile: UserProfile) -> tuple[RuleVerdict, str]:
    title = fold(candidate.title)
    # Two haystacks, deliberately different. Deal-breakers must see the company name, since that
    # is how staffing agencies are caught. Role relevance must not: a tax-clerk vacancy posted by
    # "Dipl.-Wirtschaftsingenieur Peter X" is not a Wirtschaftsingenieur role.
    full_text = fold(
        " ".join(filter(None, [candidate.title, candidate.company, candidate.description]))
    )

    for term in DEFAULT_EXCLUDES:
        if term in title:
            return RuleVerdict.REJECT, f"excluded role type: {term}"

    for term in profile.deal_breakers:
        folded = fold(term)
        if folded and folded in full_text:
            return RuleVerdict.REJECT, f"deal-breaker: {term}"

    return RuleVerdict.PASS, "passed rules"
