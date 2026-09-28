# 06 — Detect blocks, set speed per source

**Goal.** Trouveur knows when a site blocks it, says so, and slows down. It never mistakes a
block for "no new jobs".

**Why.** Aggregators block fast. A block that looks like an empty result is the silent failure
this project fears most, and a block loses the whole source.

**How.**
- In `sources/http.py`: treat captcha or challenge pages, 403, 429, and a 200 with an empty body
  where results are expected as `SourceBlocked`.
- Record it per source and show "blocked" on the dashboard and in the coverage report, apart
  from "0 jobs".
- Each source can set its own minimum interval between requests. The default stays 1 per
  second.
- After a block: pause the source and retry later, slower. Reuse the pause in `workers`
  (`SOURCE_BURST`, `SOURCE_PAUSE`).

**Done when.**
- Tests show each block shape is detected, and an empty page that is really empty is not.
- A blocked source shows as blocked, not as a healthy empty sweep.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Add block detection to trouveur/sources/http.py.

1. Detect these shapes and raise a SourceBlocked error with a clear message:
   - an HTML challenge or captcha page where JSON was expected;
   - HTTP 403 or 429 after retries;
   - a 200 whose body lacks the expected shape. The caller says what shape it expects.
   Record it in source_sweep and source_scope_health, and show "blocked" on the dashboard
   and in the coverage report.

2. Let each SourceSpec set its own minimum interval per provider. The current default stays.

3. After a block, pause that source and try again later with a longer interval. Reuse the
   existing burst pause in trouveur/work.

Only 429 means refused. A 503 can mean "no such board": freehire found Traffit answers an
unknown company with 503. A run that was mostly refused must be reported as failed, not as
"found little".

Tests use a stubbed transport, no network. Include a real empty result that must NOT count
as a block. One PR.
```
