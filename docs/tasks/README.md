# Tasks: collect every job

Trouveur shows each user the jobs that fit their profile, so they don't have to check ten sites
and can't miss a job. These tasks are about **collecting**: more sources, more boards, and
knowing what we still miss.

## How it fits together

```
aggregator searches (LinkedIn, ...) ─┐
links in our own archive            ├─> leads ─> resolver ─> boards ─> nightly sweep ─> corpus
"found it elsewhere" box            ─┘               │
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
| 05 | [Detect blocks, set speed per source](05-blocks.md) | code | – |
| 06 | [LinkedIn as a lead source](06-linkedin-leads.md) | code | 00, 02, 05 |
| 07 | [More aggregators as lead sources](07-more-aggregators.md) | code, one site per PR | 06 |
| 08 | [New job platforms (ATS)](08-new-platforms.md) | agent drafts, PR | 01, 02 |
| 09 | [Company sites with JSON-LD](09-json-ld.md) | code | 02 |
| 10 | [Public job services](10-public-services.md) | agent research, PR | 00 |
| 11 | [Big job boards as full sources](11-full-boards.md) | PR, one site each | 06 numbers |
| 12 | [Public lists and name guessing](12-public-lists.md) | code | 04 |
| 13 | [Browser fetcher](13-browser.md) | code | a source that needs it |
| 14 | [LLM reading of careers pages](14-llm-pages.md) | code + LLM | 09 |
| 15 | [Agent routines](15-agent-routines.md) | routines | 01, 02, 08 |

## Not now

- Paid APIs (Google Jobs through SerpAPI) and proxies. Only if blocks make a key source useless.
- Employer lists from Wikidata, OpenStreetMap or company registers.
- Communities: Hacker News "Who is hiring", Reddit, Discord.
- An estimate of the whole market size from source overlap.

## How to use a prompt

Start a Claude Code session on this repo and paste the task's prompt. Each prompt is written to
work alone.
