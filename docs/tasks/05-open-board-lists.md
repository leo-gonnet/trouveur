# 05 — Import open board lists

**Goal.** Take board lists that others already collected, turn each board into a lead, and let
the trial (04) keep the ones with jobs in our users' areas.

**Why.** It's the cheapest recall gain there is. Today we only sweep boards someone typed in. Free
lists name tens of thousands more, and a list costs one download.

**The lists.**

| List | What it is | Europe fit |
|---|---|---|
| [ats-scrapers](https://github.com/kalil0321/ats-scrapers) `ats-companies/*.csv` (MIT) | ~80,000 boards, one CSV per platform: `name,slug,url` | Mixed. Personio, JOIN, Teamtailor, Recruitee and softgarden lists are European. |
| [freehire](https://github.com/strelov1/freehire) public API (`/api/v1/jobs`, no key) | Jobs with their URL. Filter by the users' countries. | Only boards with jobs in our countries. IT jobs only, but a board once on brings all its jobs. |

**Be careful.**
- freehire is US-centred (~59% of its jobs are on US platforms) and IT-only. Always filter it by
  country. Its API is a third party with unknown limits: import weekly at most, never depend on
  it nightly.
- Most boards in the US-platform lists will fail the trial. That's fine: one request each.
- Order: freehire filtered by country first (small, relevant). Then European lists. Then the big
  US-platform lists, capped.
- Rows for platforms we have no adapter for (JOIN has 23,547) stay as unknown-platform leads. They
  show which adapter to write next (09).

**Done when.**
- Both lists land as leads with their own origin.
- New boards go through the trial, never straight to on.
- The coverage report shows how many boards each list brought, and how many survived the trial.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Tasks 02 and 04 must be merged first.

Add two lead origins:

1. ats_scrapers: a CLI command `trouveur discover import-lists <dir>` that reads the CSV
   files from a local checkout of github.com/kalil0321/ats-scrapers (ats-companies/*.csv,
   columns name,slug,url) and inserts one discovery_lead per row.
   - No network call. The resolver decides the board.
   - Rows for unsupported platforms stay as unknown_host leads.

2. freehire: a weekly runner step that pages freehire's public API (/api/v1/jobs), filtered
   to the countries in user profiles, and inserts one lead per job URL.
   - Archive the raw pages.
   - Keep a hard request cap.
   - Stop cleanly on a block (task 06).
   - It is IT-only and US-centred: never use it as a corpus source.

Already-known boards are skipped. New ones go through the trial (task 04), with the nightly
cap. Add "boards brought / survived the trial" per origin to the coverage report.

Tests with synthetic CSV rows and synthetic API pages. One PR.
```
