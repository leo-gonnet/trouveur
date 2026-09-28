# 14 — Browser fetcher

**Goal.** Let a source fetch a page with a real browser, when plain HTTP gets blocked or the page
needs JavaScript.

**When.** Only once a real source needs it (for example StepStone or a public service). Not
before.

**How.**
- Playwright with the installed Chromium, one page at a time, inside the runner.
- A source declares that it needs it. Every other source keeps plain HTTP.
- Same speed limits and block detection as `sources/http.py`.
- Watch memory: the server has four cores and one database.

**Done when.** One real source uses it, and the rest are unchanged.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. <SOURCE> needs a real browser: <why>.

Add a browser fetcher in trouveur/sources/:
- Playwright, one shared browser, one page at a time;
- the same per-provider speed limit and block detection as http.py;
- it returns the page's HTML, or the JSON responses it captured.

Only sources that opt in use it. Add it to the Docker image, and note the memory cost in
AGENTS.md. Switch <SOURCE> to it.

Tests must not start a browser: stub the fetcher. One PR.
```
