# 06 — LinkedIn as a lead source

**Goal.** Search LinkedIn once a day with each user's queries in each of their cities. Every job
found becomes a **lead**, not a corpus job.

**Why.** Most companies post on LinkedIn, and many LinkedIn jobs link to the company's real
board. Only a few pages a day are needed, so no proxy.

**How.**
- Use the logged-out (guest) job search. **Probe it first**: page size, result cap, filters, where
  the apply link is, and when it blocks. Write the findings into `AGENTS.md`.
- Queries: each user's expanded queries (`user_query_expansion`) × each city in their profile.
  Start small: per-user queries only.
- Store raw pages in the archive. Each job becomes a lead. Fetch the job page for the apply link
  only for leads we haven't seen before.
- Run once a day, before the nightly scan, so found boards are swept the same night.
- Two numbers go into the coverage report:
  - the share of LinkedIn jobs we already had;
  - the share with **no link out** (they exist only on LinkedIn). That number decides task 11.

**Done when.**
- Leads flow from LinkedIn into `discovery_lead` and get resolved.
- The two numbers show in the report.
- A block stops the run cleanly (05).

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Tasks 00, 02 and 05 must be merged first.

1. Probe LinkedIn's logged-out job search with throwaway shell commands, not tests. Find:
   the search endpoint, page size, result cap per query, location and date filters, where a
   job page states its external apply URL, and how many requests it takes to get blocked.
   Write the findings into AGENTS.md with today's date.

2. Add trouveur/sources/linkedin/ as a LEAD source, not a corpus source:
   - For each user, for each city in their profile, run their expanded queries from
     user_query_expansion, past 24 hours only.
   - Archive the raw pages.
   - Turn each job into a discovery_lead. Fetch the job page for its apply URL only when
     the lead is new.
   - No login, no account.

3. Run it once a day before the nightly scan, as a runner step with a hard request cap.

4. Add to the coverage report: the share of leads already in the corpus, and the share with
   no external link.

Synthetic fixtures only. Tests for the parser and the cap. One PR.
```
