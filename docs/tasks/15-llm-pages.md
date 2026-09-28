# 15 — LLM reading of careers pages

**Goal.** Read jobs from company careers pages that have no JSON-LD and use no known platform.

**Why.** This is the last part of the long tail. Do it last, when the report shows these pages
still matter.

**How.**
- Fetch the page, and ask an LLM for the list of jobs (title, place, link, text).
- **Archive the LLM's answer as the raw document.** A second call would not give the same
  answer, so the answer itself is what we keep. The normaliser stays pure.
- Paid from the operator's budget with a daily cap, never from a user's credit.
- Only for sites already tried as JSON-LD (10) that failed.

**Done when.** One such site is swept, the cap holds, and user credit is untouched.

**Prompt**

```
Read AGENTS.md (LLM cost discipline) and docs/tasks/README.md. Task 10 must be merged
first.

Add a "careers_page" source for company sites where the jsonld source found no JobPosting.

- The client fetches the careers page and asks the installation's LLM to return the job
  list as strict JSON: title, location, url, description.
- Archive that JSON as the raw document. The normaliser reads it, pure and versioned.
- Use the pinned model and provider, with reasoning disabled.
- A daily dollar cap paid by the installation, never by a user's credit, visible on the
  dashboard.
- A malformed answer stores nothing and is retried next time.

Tests with a stubbed LLM. One PR.
```
