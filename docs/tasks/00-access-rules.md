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
- Keep the facts in the refused table. The endpoints and traps found there are still true, and
  now useful.
- The privacy rules about users' data don't change.

**Done when.**
- No rule refuses a source for robots.txt or terms.
- The facts from the old refused table are kept, as notes for future adapters.
- Tests that pinned the old policy are updated or removed.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Trouveur now puts recall first: robots.txt and
terms of service no longer rule out a source. Rewrite the parts of AGENTS.md that say
otherwise: "DON'T: robots.txt policy", the refused sources table, "Never reach a source
through a reverse-engineered private endpoint", and "Never reintroduce a user's keywords
into a source query".

Keep:
- the speed limit per provider, with a new reason: avoid blocks;
- "never use a personal account";
- the privacy rules about users' data.

Allow user keywords in discovery queries only (leads, see docs/tasks/02), never in corpus
queries.

Keep every fact from the refused table (endpoints, robots findings, dates) as notes for
future adapters.

Find the tests that pin the old policy and update them. Keep AGENTS.md's style: short, only
rules and facts. One PR.
```
