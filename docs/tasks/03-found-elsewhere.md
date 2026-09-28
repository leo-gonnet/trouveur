# 03 — "Found it elsewhere" box

**Goal.** A user pastes a link to a job they found somewhere else. Trouveur says whether it had
the job, and if not, it learns the board.

**Why.** It's the only measure of recall on jobs people really wanted. It also finds boards.

**How.**
- The web app only stores the URL. The runner does the rest (web never fetches).
- The runner resolves the link (02). Then it checks the corpus and records one answer:
  - we had it, and recommended it;
  - we had it, but didn't recommend it, and why (location filter, low score, not retrieved);
  - we didn't have it: the board is missing (then it becomes a lead) or the platform is unknown.
- The user sees the answer next to their link. The totals go into the coverage report (01).

**Done when.**
- Each of the answers above is covered by a test.
- The web route makes no network call.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Task 02 (leads and resolver) must be merged first.

Add a "Found it elsewhere?" box where a signed-in user pastes a job URL.

- The web route only inserts a row. No network call.
- The runner handles it on its next tick:
  1. resolve the URL (trouveur/discovery/resolve.py);
  2. look for the job in the corpus;
  3. record one outcome: had_and_recommended, had_not_recommended (with the reason:
     location filter, not retrieved, or low score), missing_board (the board becomes a
     discovery_lead), or unknown_platform.
- Show the user their links with outcomes. Add the totals to the coverage report.

Use the existing design system (app.css, _macros.html). One test per outcome. Run the
integration suite, since templates changed. One PR.
```
