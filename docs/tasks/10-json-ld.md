# 10 — Company sites with JSON-LD

**Goal.** One generic source for any company website that marks its jobs with schema.org
`JobPosting` (JSON-LD).

**Why.** Companies add this markup to appear in Google Jobs. Many small firms use no platform we
support, and this covers them all with one adapter.

**How.**
- A tenant is a company site (its careers URL).
- Find job pages from the sitemap, or from links on the careers page.
- Read the `JobPosting` block. One schema, so one normaliser.
- Closing: a page that is gone, or a `validThrough` in the past. Only a complete read of the list
  may close anything.
- The resolver sends "company's own site" leads here when the page has the markup.

**Done when.**
- A synthetic site with JSON-LD jobs is swept and normalised.
- A partial read closes nothing.
- The resolver routes matching leads here.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Add a generic "jsonld" source.

- A tenant is a company careers URL.
- The client finds job page URLs from sitemap.xml, or from links on the careers page, then
  fetches each page and archives it.
- The pure normaliser reads the schema.org JobPosting JSON-LD: title, description,
  jobLocation, datePosted, validThrough, employmentType, baseSalary, hiringOrganization.
- Report a closable scope only when the whole list was read.

In the resolver: a lead whose URL is on a company's own domain becomes a jsonld tenant
candidate. Task 04 then tries it.

Watch these traps and write them into AGENTS.md:
- several JSON-LD blocks on one page;
- @graph wrappers;
- HTML inside the description;
- a location as a string or as an object.

Synthetic fixtures and golden files. One PR.
```
