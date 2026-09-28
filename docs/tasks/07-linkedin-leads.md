# 07 — LinkedIn as a lead source

**Goal.** Search LinkedIn once a day with each user's queries in each of their cities. Every job
found becomes a **lead**, not a corpus job.

**Why.** Most companies post on LinkedIn. After the open lists (05), LinkedIn's main value is
**jobs that live only on LinkedIn**, not new boards. freehire measured it: once its catalogue was
big, 6 queries over 15 pages found only 12 new companies. Our catalogue is small, so we'll find
more at first, but expect that number to drop.

**How.**
- Use the logged-out (guest) job search. **Probe it first**: page size, result cap, filters, where
  the apply link is, and when it blocks. Write the findings into `AGENTS.md`.
- Queries: each user's expanded queries (`user_query_expansion`) × each city in their profile.
  Start small: per-user queries only.
- Store raw pages in the archive. Each job becomes a lead. For new leads only, find the real board
  two ways:
  - **Direct apply link.** The job page carries it (JobSpy reads `code#applyUrl`).
  - **Company website.** The company profile names its website. Follow it to the careers page and
    look for a platform link. freehire does this in `cmd/harvest-linkedin`, and uses the posting's
    ATS id (JSON-LD `identifier`) to prove a guessed board is the right one. About half of
    postings carry that id.
- Run once a day, before the nightly scan, so found boards are swept the same night.
- Two numbers go into the coverage report:
  - the share of LinkedIn jobs we already had;
  - the share with **no link out** (they exist only on LinkedIn). That number decides task 12.

**Done when.**
- Leads flow from LinkedIn into `discovery_lead` and get resolved.
- The two numbers show in the report.
- A block stops the run cleanly (06).

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Tasks 00, 02 and 06 must be merged first.
Read how others do it first: speedyapply/JobSpy (jobspy/linkedin) and strelov1/freehire
(cmd/harvest-linkedin), both MIT.

1. Probe LinkedIn's logged-out job search with throwaway shell commands, not tests. Find:
   the search endpoint, page size, result cap per query, location and date filters, where a
   job page states its external apply URL, what the company profile says about the website,
   and how many requests it takes to get blocked. Write the findings into AGENTS.md with
   today's date.

2. Add trouveur/sources/linkedin/ as a LEAD source, not a corpus source:
   - For each user, for each city in their profile, run their expanded queries from
     user_query_expansion, past 24 hours only.
   - Archive the raw pages.
   - Turn each job into a discovery_lead.
   - For new leads only, record the direct apply URL. If there is none, record the company
     website and the posting's ATS id (JSON-LD identifier), if any.
   - No login, no account.

3. Run it once a day before the nightly scan, as a runner step with a hard request cap.

4. Add to the coverage report: the share of leads already in the corpus, and the share with
   no external link.

Synthetic fixtures only. Tests for the parser and the cap. One PR.
```
