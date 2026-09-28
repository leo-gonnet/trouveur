# 07 — More aggregators as lead sources

**Goal.** Do what 06 did, for other big job sites. One site per PR.

**Why.** Each aggregator knows different companies. Leads from several sites find more boards.

**Which sites.** Pick from the coverage report: the sites that matter in the users' countries.
Candidates: Indeed, StepStone, Glassdoor, XING, the national #1 board of each user country, and
Arbeitsagentur's external links. Arbeitsagentur is free: we already fetch those details.

**Done when.** For each site: leads flow in, the coverage report counts them, and a block stops
cleanly.

**Prompt**

```
Read AGENTS.md, docs/tasks/README.md and trouveur/sources/linkedin/ (task 06). Add <SITE>
as a lead source, following the LinkedIn pattern exactly:
- probe first and write the findings into AGENTS.md with the date;
- per-user queries per city;
- raw pages archived;
- leads, not corpus jobs;
- the apply URL only for new leads;
- a hard daily request cap;
- stop cleanly on a block.

If <SITE> is Arbeitsagentur: no new requests. Extract the external URL from detail
payloads already in the archive, as a pure versioned extractor.

Synthetic fixtures only. One PR.
```
