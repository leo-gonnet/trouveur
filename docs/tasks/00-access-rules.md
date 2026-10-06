# 00 — Update the access rules in AGENTS.md

**Goal.** Make `AGENTS.md` match the new choice: recall comes first.

**Why.** `AGENTS.md` still refuses LinkedIn, AMS, willhaben, StepStone, SmartRecruiters and
others because of robots.txt or terms. Every later task breaks those rules. A stale rule file is
worse than none.

**What changes.**
- robots.txt and terms no longer refuse a source.
- The per-provider speed limit stays, but its reason changes: **don't get blocked**.
- Private endpoints are allowed. Keys come from the environment, never from the repo.
- Never use a personal account.
- A user's keywords are allowed in **discovery** queries (leads). They stay forbidden in
  **corpus** queries.
- The privacy rules about users' data don't change.

**Done when.**
- No rule refuses a source for robots.txt or terms.
- Tests that pinned the old policy are updated or removed.

**Done, 2026-10-06, and wider than drafted.** The refused table was deleted rather than kept:
under recall-first its purpose was gone, since it existed to stop someone re-adding those sources
and re-adding them is now the plan. Every one of them is re-probed by tasks 07–12 anyway, and the
findings were a month old. No test pinned the old policy — it lived only in prose.

The same pass cut AGENTS.md from 1,367 lines to 299. Source-specific facts are not repeated
there: each lives in a comment beside the code that depends on it, which is where every one of them
already was. **AGENTS.md now holds only rules that cross modules, and editing it needs human
approval.** A later task that learns something about a source writes it into that adapter.

**Prompt**

```
Done. Kept for the record; see "Done" above for what was actually changed.
```
