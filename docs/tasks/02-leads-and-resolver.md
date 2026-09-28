# 02 — Leads and resolver

**Goal.** A table of leads (jobs seen anywhere, with a link), and a resolver that turns a link
into a board we can sweep.

**Why.** This is the core of discovery. Every other lead source (03, 05, 07, 08, 13) writes
here.

**How.**
- `discovery_lead`: where it came from, company, title, place, link, and the result (a board, an
  unknown host, no link, or a duplicate).
- The resolver is **pure and versioned** (`RESOLVE_VERSION` in `versions.py`). Link to
  `(source, scope)` for every supported platform, reusing `registry.clean_scope`.
- **Port the URL rules from freehire** (`internal/ingest/atsdetect`, `internal/ingest/atsboard`,
  MIT) instead of writing them from scratch. They cover ~92 platforms and are tested. Credit the
  source in a comment.
- The raw input stays in the raw archive. When a new platform adapter ships, bump the version
  and every old lead is resolved again. No new request is needed.
- A resolved board goes into `source_tenant` as `origin = discovered`, `enabled = false`.
  Task 04 decides whether to turn it on.
- First lead source, free: **links inside our own archive** (descriptions, apply links).

**Done when.**
- Every supported platform's URL shapes resolve (tests with synthetic URLs).
- An unknown host is kept and counted, not dropped.
- A version bump re-resolves old leads.
- Archive mining runs in the runner in small batches, like derive.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Add discovery leads to Trouveur.

1. A migration and a schema.py table `discovery_lead`: origin (archive, user_report, and
   later aggregators), a reference to the raw document, company, title, location text,
   url, host, result (resolved source+scope, unknown_host, no_url, duplicate), and
   resolve_version.

2. trouveur/discovery/resolve.py: a pure function url -> (source, scope) | unknown host.
   - Port the URL rules from freehire (github.com/strelov1/freehire,
     internal/ingest/atsdetect and internal/ingest/atsboard, MIT licence). Keep a comment
     crediting it. Port their test cases too, as synthetic URLs.
   - Cover every tenant-scoped source in the registry and reuse registry.clean_scope.
   - Add RESOLVE_VERSION to trouveur/versions.py.
   - A version bump must re-resolve old leads through the work queue, like derive.

3. A resolved scope is inserted into source_tenant with origin='discovered' and
   enabled=false. Never change an existing row.

4. The first lead source: extract URLs from postings already in the raw archive
   (descriptions, apply links). Keep it pure and versioned, and run it in the runner in
   bounded batches.

Tests:
- every URL shape of every supported platform resolves;
- unknown hosts are kept;
- a version bump re-queues old leads.

SQL only in db/queries. Run the integration suite. One PR.
```
