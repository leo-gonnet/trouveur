# Runbook — switching the embedding model in production

Companion to `docs/tasks/01-encoder-swap.md`, which carries the reasoning and the evidence. This
is the order of operations only. Rehearsed on a 25,000-posting copy of the production corpus
before being written down; the numbers below come from that rehearsal.

## Before you start

- **Production cannot do any of this until the branch is deployed.** The running image has no
  mpnet provider, no wide column and no migration `0008`. Merge and deploy first, with the
  settings still pointing at the narrow space, and confirm the system behaves exactly as before.
  That deploy is a no-op by design: new code, same vector space, same results.
- **Know the cost.** The rehearsal ran at ~77 documents a minute on this four-core host while
  nothing else competed. At that rate the full corpus is roughly two days of continuous work.
  The runner sweeping at the same time will slow both.
- **Know the disk.** A second vector space for the full corpus is on the order of a gigabyte
  including its index. Check headroom before, not during.

## Sequence

1. **Deploy the branch with both widths still set to the narrow space.** Nothing changes yet.
   Confirm recommendations still work and the dashboard reports one vector space.
2. **Raise the write width only.** Point the embed worker at the wide provider while retrieval
   keeps reading the narrow space. These are two separate settings precisely so this state is
   expressible: writes go to the new column, reads do not.
3. **Queue the work** by refilling the embed stage. The provider change changes the version
   string, so every row is considered stale. It is chunked and resumable — interrupting it is
   free.
4. **Throttle or pause the runner** before draining, so the backfill is not fighting the sweep
   for four cores. Decide deliberately whether a slower sweep for two days is acceptable; for a
   job radar, falling two days behind on discovery may cost more than the backfill gains.
5. **Drain.** Watch the dashboard's per-space coverage rather than inferring progress from the
   embedded total. Expect both numbers to be populated for a while: a backfilled row keeps its
   narrow vector and gains a wide one, which is what makes the old space safe to read throughout.
6. **Measure before flipping.** Run the planted-needle harness against a copy at the same
   coverage, on the narrow space and then the wide one. Do not flip on the strength of an
   earlier bake-off; the in-situ number is the one that counts.
7. **Flip the read width** once coverage is complete and step 6 passed. This is the only step a
   user can perceive.
8. **Live with it** before retiring anything. Then drop the narrow column in its own migration
   and rebuild what needs rebuilding.

## Rolling back

Before step 7, rollback is free: set the write width back and the wide column is simply unused
data. After step 7, rollback is setting the read width back to the narrow space, which is still
fully populated until step 8 removes it. That is the reason step 8 waits.

## Known traps

- **Shared memory.** Building an HNSW index at this size needs `maintenance_work_mem` raised, and
  a parallel build puts that memory in `/dev/shm` — capped at 64MB by Docker's default. The error
  says "could not resize shared memory segment", which reads like a full disk and is not one.
  `compose.yaml` sets `shm_size`; confirm the running container actually has it.
- **Memory.** Two embedding models resident at once will exhaust a 7GB host. One drain worker per
  model, and nothing else large running.
- **Parallel workers.** The queue uses `SKIP LOCKED`, so several drain workers are supported.
  On four cores the models already saturate the CPU, so a second worker buys less than it looks
  like it should.
- **Do not point a development or evaluation process at the production database.** The eval
  harness refuses unless given an explicit scratch URL, and that guard exists for this reason.
