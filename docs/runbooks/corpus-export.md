# Runbook — the offsite corpus export

The raw archive is the only thing in this system that cannot be recomputed. Everything else —
postings, facets, vectors — is a pure function of it. It currently lives on one disk on one VPS.
This puts a copy somewhere else, nightly.

`trouveur export` writes one Parquet file per stream per day to a private Hugging Face dataset
repository. A day is written once and never rewritten, so the export has no cursor to keep in
sync: "which days are done" is read back from the destination. A run that dies halfway is
resumed by the next one.

## The three streams

| stream | keyed on | lag | what it is |
|---|---|---|---|
| `documents/<date>.parquet` | `fetched_at` | 0 | raw payloads, exactly as Postgres stored them |
| `jobs/<date>.parquet` | `first_seen_at` | 3 days | normalised postings joined to their facets |
| `lifecycle/<date>.parquet` | `closed_at` | 0 | retirements |

`documents` is the one that matters. If storage ever runs short, drop the other two: they are
recomputable and it is not.

`jobs` lags three days because a posting's description arrives on a later fetch than its listing,
so a day frozen the moment it ends would archive rows that are still filling in. `lifecycle`
exists because a posting first seen in March and closed in September would otherwise have to
reach back into a file written six months earlier — which is the one thing an append-only
archive must never do.

Deliberately **not** exported: embeddings (recomputable, and worthless in another model's vector
space), and every table holding an account, a credential or a match history. A private repository
is not a reason to upload those. A test asserts it.

## Setting it up

1. **Create the dataset repository.** On huggingface.co: New → Dataset, visibility **Private**.
   Note the `<owner>/<name>`.

2. **Create a fine-grained token** (Settings → Access Tokens → Create new → Fine-grained) with
   **write** permission on that one repository and nothing else. This token sits in a cron job on
   a public-facing host; a broad token there is a standing offer.

3. **Put both in `.env`** next to `compose.yaml`, which is already `chmod 600` and gitignored:

   ```
   ARCHIVE_REPO=<owner>/<name>
   ARCHIVE_TOKEN=hf_...
   ```

4. **Rehearse into a directory first.** No token needed, and it tells you the real size and
   duration before anything leaves the host:

   ```bash
   docker compose run --rm -v /tmp/archive:/tmp/archive web \
       trouveur export --to local:/tmp/archive --dry-run
   docker compose run --rm -v /tmp/archive:/tmp/archive web \
       trouveur export --to local:/tmp/archive
   ```

5. **Run it for real.** The first run carries the whole history and is the long one; every run
   after it carries one day.

   ```bash
   docker compose run --rm web trouveur export
   ```

6. **Schedule it,** on the host, from the compose directory — the same shape as `backup.sh`:

   ```cron
   30 4 * * *  cd /path/to/trouveur && docker compose run --rm web trouveur export >> /var/log/trouveur-export.log 2>&1
   ```

   After the scheduled sweep, not before: a day is frozen only once it is over, so the export
   wants the day's ingest to have happened.

## Reading it back

```python
import pyarrow.parquet as pq
from huggingface_hub import snapshot_download

path = snapshot_download("<owner>/<name>", repo_type="dataset", allow_patterns="jobs/*")
table = pq.read_table(f"{path}/jobs")
```

`payload` is the raw JSON as text, not a struct: every source has a different shape and several
change theirs without notice. A schema that unified them would be a normaliser, which is the
stage this stream exists to be independent of.

To reconstruct the corpus as it stood on a date, take every `jobs` partition up to that date and
replay `lifecycle` over it: a posting is open on date *D* if no lifecycle row closed it by then.

## Before deleting anything from production

**The export must have run, and completed, first.** Partitions are written once and skipped
thereafter, so a day that was exported before a deletion keeps the deleted rows forever — and a
day that was not is simply gone. Check the destination holds every day in the range before
removing a source's rows from the database.

## Known traps

- **A partition is immutable, including a wrong one.** If a bug ships a bad partition, the fix is
  to delete that file at the destination and re-run; the export will rebuild it. It will never
  overwrite one on its own, by design.
- **The export reads, only.** Asserted by a test, because a nightly job with a write in it is a
  nightly job that can damage the thing it exists to protect.
- **Empty days are not uploaded.** A zero-row file would assert "this day is done" about a day
  that may still be backfilled, so an empty day is re-checked — one indexed lookup — until it has
  something in it.
- **`pyarrow` is a build dependency now.** The image needs `--extra archive`; a deploy that
  predates it has no `trouveur export`.
