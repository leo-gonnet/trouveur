# 13 — Public lists and name guessing

**Goal.** Find boards without waiting for a lead to point at them.

**How.**
- **Certificate logs** (crt.sh): list every `*.jobs.personio.de`, `*.myworkdayjobs.com`,
  `*.breezy.hr`, and similar. This works for platforms that give each company a subdomain.
- **Common Crawl index**: list `jobs.lever.co/*`, `boards.greenhouse.io/*`,
  `jobs.ashbyhq.com/*`. This works for platforms that put the company in the path.
- **Name guessing**: for leads with a company but no link, try the company name as a slug on each
  supported platform. Keep a guess only if the board's company name matches.
- Everything becomes leads and goes through the trial (04). The trial cap keeps the scan short.

**Known traps** (from freehire, `cmd/harvest-boards`):
- Common Crawl: a wildcard like `*.myworkdayjobs.com` spans ~22 index pages per snapshot. A small
  page cap silently cuts the tail while the run reports success.
- Recent snapshots overlap a lot. Two or three are enough.
- Only 429 means "refused". Some platforms answer an unknown company with 503 (Traffit), so a
  503 is "no board", not a block.
- A run that was mostly refused did not happen. Report it as failed, not as "found little".

**Done when.** New boards arrive from each method, marked with their origin, and the trial cap
holds.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Tasks 02 and 04 must be merged first.

Add three lead origins, each a weekly runner step with a hard cap:
1. crt.sh: query certificate logs for each subdomain-per-tenant platform we support and
   turn the subdomains into leads.
2. The Common Crawl URL index: query each path-per-tenant platform we support and turn the
   slugs into leads.
3. Name guessing: for leads with a company name and no URL, try a slug made from the name on
   each supported platform. Keep it only if the board states a matching company name.

Archive the raw responses. Record the origin on each lead. Leads go through the resolver
and the trial (task 04), never straight to enabled.

Tests with synthetic responses. One PR.
```
