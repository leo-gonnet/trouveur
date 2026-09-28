# 11 — Public job services

**Goal.** Add the public employment services of the users' countries, like Arbeitsagentur is for
Germany.

**Why.** These hold many jobs that are on no other site, mostly outside tech.

**Which, in order.**
1. **EURES first.** The EU portal gets jobs from every national service, AMS included: ~2.7M jobs
   behind one public JSON API. ats-scrapers already reads it (`scrapers/eures.py`, MIT).
2. **Then a national service**, only where the coverage report shows EURES misses many of its
   jobs. For Austria that's AMS: its web app loads jobs from a JSON API behind the page.

**Known about EURES** (from ats-scrapers, re-probe before trusting):
- `POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search`, no key.
- 50 results a page, 200 pages max: **10,000 results per query**. Past page 200, HTTP 400.
- Split to stay under the cap: country, then NUTS region, then sector (NACE), then schedule. The
  response's facet counts say when to split, without extra requests.
- The detail is a separate endpoint.
- The same job may also come from Arbeitsagentur: `dedup_group` must mark it.

**Done when.** Per service: an adapter swept nightly, delta-first, with its traps in
`AGENTS.md`.

**Prompt**

```
Read AGENTS.md (Arbeitsagentur section) and docs/tasks/README.md. Task 00 must be merged
first.

Add <SERVICE> as a corpus source. For EURES, read github.com/kalil0321/ats-scrapers
(src/ats_scrapers/scrapers/eures.py, MIT) first: it documents the endpoint, the 10,000-result
cap and how to split queries.

1. For a service with no known API: open its public job search in a browser (Playwright,
   /opt/pw-browsers/chromium) and record the JSON requests the page makes: search, paging,
   filters, detail, and any keys or headers they need.

2. Probe the endpoints with shell commands: page size limits, result caps, date filters,
   and what fails silently. Write everything into AGENTS.md with the date.

3. Write the adapter like arbeitsagentur/:
   - delta-first;
   - partitioned if the result window is capped (EURES: country, then NUTS region, then
     sector);
   - a detail phase if the description is separate;
   - a pure, versioned normaliser.
   Sweep only the countries in user profiles. Keys come from settings or the environment.

Synthetic fixtures, golden files, and regression tests for every trap. One PR.
```
