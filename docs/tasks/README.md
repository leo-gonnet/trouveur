# Tasks: collect every job

Trouveur shows each user the jobs that fit their profile, so they don't have to check ten sites
and can't miss a job. These tasks are about **collecting**: more sources, more boards, and
knowing what we still miss.

## How it fits together

```
open board lists (ats-scrapers, ...) ─┐
aggregator searches (LinkedIn, ...)   │
links in our own archive              ├─> leads ─> resolver ─> boards ─> nightly sweep ─> corpus
"found it elsewhere" box             ─┘                │
                                                       └─> unknown platforms ─> agent ─> adapter PR

coverage report ─> tells humans and agents what to build next
```

A **lead** is one job seen somewhere, with its company and its link. The **resolver** turns the
link into a board we can sweep. One lead can bring a whole board, so one Java job can bring the
company's other forty jobs too.

## Rules for every task

- **Cities come from user profiles, never from this repo.** No task names a city. The coverage
  report (01) says where the users are and what is missing there.
- **Recall comes first.** robots.txt and terms of service don't rule out a source (see 00).
- **Go slow enough not to get blocked.** A block loses the whole source for days.
- **Never use a personal account** on any site.
- **Agents propose, humans merge.** An agent opens a PR or writes a disabled row. It never
  changes production directly.
- One task, one PR. Read `AGENTS.md` first. Probe a live site before writing code for it, and
  write what you learn into `AGENTS.md`.

## Order

| # | Task | Done by | Needs |
|---|---|---|---|
| 00 | [Update the access rules in AGENTS.md](00-access-rules.md) | PR | – |
| 01 | [Coverage report](01-coverage-report.md) | code | – |
| 02 | [Leads and resolver](02-leads-and-resolver.md) | code | – |
| 03 | ["Found it elsewhere" box](03-found-elsewhere.md) | code | 02 |
| 04 | [Try new boards, turn on the good ones](04-board-trial.md) | code | 02 |
| 05 | [Import open board lists](05-open-board-lists.md) | code | 02, 04 |
| 06 | [Detect blocks, set speed per source](06-blocks.md) | code | – |
| 07 | [LinkedIn as a lead source](07-linkedin-leads.md) | code | 00, 02, 06 |
| 08 | [More aggregators as lead sources](08-more-aggregators.md) | code, one site per PR | 07 |
| 09 | [New job platforms (ATS)](09-new-platforms.md) | agent drafts, PR | 01, 02 |
| 10 | [Company sites with JSON-LD](10-json-ld.md) | code | 02 |
| 11 | [Public job services](11-public-services.md) | agent research, PR | 00 |
| 12 | [Big job boards as full sources](12-full-boards.md) | PR, one site each | 07 numbers |
| 13 | [Public lists and name guessing](13-public-lists.md) | code | 04 |
| 14 | [Browser fetcher](14-browser.md) | code | a source that needs it |
| 15 | [LLM reading of careers pages](15-llm-pages.md) | code + LLM | 10 |
| 16 | [Agent routines](16-agent-routines.md) | routines | 01, 02, 09 |

## Not now

- Paid APIs (Google Jobs through SerpAPI) and proxies. Only if blocks make a key source useless.
- Employer lists from Wikidata, OpenStreetMap or company registers.
- Communities: Hacker News "Who is hiring", Reddit, Discord.
- An estimate of the whole market size from source overlap.
- freehire's API as a nightly source. Fine as a board list (05), too risky to depend on.
- Liveness checks for jobs no board sweep revisits (freehire's `cmd/liveness`). Needed once we
  import single jobs by URL.
- Telegram job channels as a lead source (freehire does this with an LLM).

## Related projects

Others have built parts of this. Read them before building the same thing.

| Project | Useful for |
|---|---|
| [freehire](https://github.com/strelov1/freehire) (Go, MIT) | The whole discovery chain: URL to board rules (`internal/ingest/atsdetect`, `atsboard`), board probing (`cmd/harvest-boards`), LinkedIn harvest, Common Crawl, JSON-LD import by URL. US-centred and IT-only. |
| [ats-scrapers](https://github.com/kalil0321/ats-scrapers) (Python, MIT) | ~80,000 company boards in CSV files; scrapers for EURES, jobs.ch, softgarden, Teamtailor, JOIN. |
| [JobSpy](https://github.com/speedyapply/JobSpy) (Python, MIT) | LinkedIn, Indeed and Glassdoor search; the LinkedIn apply link parser. |
| [career-ops](https://github.com/career-ops-hq/career-ops) (JS, MIT) | ~115 providers, a company to board resolver, LLM scoring of jobs against a CV. |

What none of them does, and Trouveur does: every job in a user's area, not only IT, ranked for
that user every day.

## How to use a prompt

Start a Claude Code session on this repo and paste the task's prompt. Each prompt is written to
work alone.
