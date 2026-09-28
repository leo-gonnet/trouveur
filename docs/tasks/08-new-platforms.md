# 08 — New job platforms (ATS)

**Goal.** Support the job platforms that leads point to most often but that we can't read yet.

**Why.** Every unknown host in the leads is a platform whose boards we can't sweep. One adapter
unlocks all of its companies at once, including old leads (the resolver is re-run).

**Which ones.** The top unknown hosts in the coverage report. Easy ones to start with:
SmartRecruiters and Recruitee (open APIs). Likely next: Softgarden, Teamtailor, prescreen/onlyfy,
SuccessFactors, JOIN, BambooHR, d.vinci, rexx.

**Done when.** For each platform:
- an adapter;
- its URL shapes in the resolver, with `RESOLVE_VERSION` bumped;
- traps in `AGENTS.md`;
- golden files.

**Prompt**

```
Read AGENTS.md, docs/tasks/README.md, and trouveur/sources/board.py and feed.py. Add
<PLATFORM> as a source.

1. Probe live with shell commands:
   - how to list one company's jobs;
   - paging;
   - where the description and date are;
   - what an unknown company returns;
   - rate limits.
   Write every surprise into AGENTS.md with today's date.

2. If one request returns a company's full board, join the board family (board.py).
   Otherwise write a paged adapter and never report a closable scope unless the walk ended
   cleanly.

3. Client (network only) and a pure, versioned normaliser.

4. Add its scope grammar to scopes.py and its URL shapes to the resolver. Bump
   RESOLVE_VERSION so old leads resolve.

5. Hand-written synthetic fixtures, golden files, and the registry test.

One PR.
```
