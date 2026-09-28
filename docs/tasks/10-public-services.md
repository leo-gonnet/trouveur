# 10 — Public job services

**Goal.** Add the national public employment service of each user country, like Arbeitsagentur
is for Germany.

**Why.** These hold many jobs that are on no other site, mostly outside tech.

**Which.** The countries in the coverage report. For Austria that means AMS (its web app loads
jobs from a JSON API behind the page). Also EURES, the EU portal that gets jobs from national
services.

**Done when.** Per service: an adapter swept nightly, delta-first, with its traps in
`AGENTS.md`.

**Prompt**

```
Read AGENTS.md (Arbeitsagentur section) and docs/tasks/README.md. Task 00 must be merged
first.

Add <SERVICE> as a corpus source.

1. Open its public job search in a browser (Playwright, /opt/pw-browsers/chromium) and
   record the JSON requests the page makes: search, paging, filters, detail, and any keys
   or headers they need.

2. Probe those endpoints with shell commands: page size limits, result caps, date filters,
   and what fails silently. Write everything into AGENTS.md with the date.

3. Write the adapter like arbeitsagentur/:
   - delta-first;
   - partitioned if the result window is capped;
   - a detail phase if the description is separate;
   - a pure, versioned normaliser.
   Keys come from settings or the environment.

Synthetic fixtures, golden files, and regression tests for every trap. One PR.
```
