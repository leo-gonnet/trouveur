# 16 — Agent routines

**Goal.** Agents that run on a schedule, read the coverage report, and propose the next source or
a fix, as PRs.

**Why.** Humans don't check the numbers every week. Agents can, and they turn the report into
work.

**The routines.**
- **Source scout** (weekly): reads the top unknown platforms, the "only on this site" shares and
  the users' countries. Picks the next task 08, 09, 11 or 12 and opens a PR using that task's
  prompt.
- **Adapter doctor** (when a source is blocked or suddenly finds 0): probes the site again,
  finds what changed, and opens a fix PR plus an `AGENTS.md` note.

**Open question.** How does an agent read the report without making it public? It names users'
areas, and the repo is public. Options: an admin API token, or a private export. Decide before
building.

**Done when.** Both routines exist, read the report the chosen way, and have opened at least one
PR each.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Tasks 01, 02 and 09 must be merged first.

1. Give agents read access to `trouveur coverage --json` without making it public (it
   names users' areas). Propose two options with their trade-offs and ask me before
   building.

2. Write two Claude Code routines as prompts in docs/tasks/routines/:
   - source-scout (weekly): read the report, pick the biggest missing source or platform,
     then follow the matching task file's prompt and open one PR;
   - adapter-doctor (when a source is blocked or drops to 0): re-probe the site, fix the
     adapter, add the finding to AGENTS.md, and open one PR.

Agents never merge, never write to the production database, and never enable a board
directly.
```
