# 01 — Coverage report

**Goal.** One place that says, for the areas in user profiles: how many open jobs we have, from
which source, and what we miss.

**Why.** Without it, nobody can tell whether a new source helped. It is also what agents read to
choose the next source, since the repo names no city.

**What it shows.** Per user area (their countries and city circles):
- open jobs per source;
- jobs that only one source has (that source is irreplaceable);
- boards on, boards found, boards tried and dropped;
- later, as other tasks land: leads we already had (06), "found it elsewhere" results (03), top
  unknown platforms (02), blocked sources (05).

**Where.** A section on the dashboard, plus `trouveur coverage --json` for agents and scripts.

**Done when.**
- The page and the command show the same numbers.
- Areas are read from profiles. Nothing is hardcoded.
- An integration test renders it with seeded data.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Build a coverage report for Trouveur.

For each user's area (the countries and city circles in their profile), show:
- open jobs per source;
- jobs found by only one source (use dedup_group);
- tenants per source by state (on, discovered, tried and dropped).

Areas must come from user_profile. Never name a city in code.

Reuse the location filter the match run already uses (GeoNames ids, radius, countries). Do
not re-derive locations.

Show it as a section on the dashboard and as `trouveur coverage --json` (the same query).
Leave clear hooks for rows later tasks add: leads already held, "found it elsewhere"
results, unknown platforms, blocked sources.

SQL goes in trouveur/db/queries/. Add it to the query execution test. Run the integration
suite. One PR.
```
