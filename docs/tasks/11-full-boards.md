# 11 — Big job boards as full sources

**Goal.** Put a big job site's jobs straight into the corpus. Do it only when the numbers say
many jobs live **only** there.

**Why.** Some jobs have no other home, like LinkedIn "Easy Apply" jobs from companies with no
board. Leads alone can't give us those.

**When.** When the coverage report shows a real share of leads with no link out (06, 07). A few
percent can wait. Twenty percent can't.

**How.** A normal corpus source for that site. The raw pages are already archived by the lead
source, so old ones can become jobs without new requests.

**Done when.** The site's "only here" jobs appear in Recommendations, and the report shows the gap
closing.

**Prompt**

```
Read AGENTS.md, docs/tasks/README.md, and the lead source for <SITE> in
trouveur/sources/<site>/.

Turn <SITE> into a corpus source as well:
- a pure, versioned normaliser from the raw pages it already archives to CanonicalJob;
- a sweep for the users' areas;
- a detail fetch for descriptions if needed.

Only jobs with no external link need to become corpus jobs. The others arrive through
their own boards; dedup_group marks any duplicates.

Re-normalise the leads already archived before fetching anything new. Keep the daily
request cap and the block handling. Synthetic fixtures and golden files. One PR.
```
