# 04 — Try new boards, turn on the good ones

**Goal.** Every discovered board is swept once. If it has jobs in any user's area, it's turned on
automatically. If not, it's checked again later.

**Why.** Discovery will find thousands of boards and most are elsewhere in the world. Sweeping
all of them every night would make the scan take hours.

**How.**
- Discovered boards get one **trial** sweep through the normal pipeline. Their jobs enter the
  corpus like any others.
- After derive: at least one open job inside any user's area means **on**, swept every night.
  None means **checked again monthly**.
- A board that had local jobs and has had none for a long time drops to monthly.
- Cap trial sweeps per night, so the scan time stays under control.
- Show on / trial / monthly counts in the coverage report.

**Done when.**
- A board with a local job is turned on without a human.
- A board with none is not swept nightly.
- The nightly cap is respected.

**Prompt**

```
Read AGENTS.md and docs/tasks/README.md. Task 02 must be merged first.

Discovered boards (source_tenant origin='discovered') get one trial sweep through the
normal pipeline, capped at N boards per night (a constant).

After their jobs are derived, check whether any open job passes any user's location filter.
Reuse the match run's filter; do not re-derive.
- If yes, enable the board: it is swept nightly.
- If no, check it again in 30 days.
- A board with no local job for 60 days drops to the monthly check.

Store "when to check next" as observation (source_scope_health or a new column), not as
configuration. AGENTS.md explains why the two tables are separate.

Update the AGENTS.md rule "a discovery pass inserts enabled=false, a human promotes": the
trial now promotes automatically.

Add the counts to the coverage report. Test promotion, demotion and the cap. One PR.
```
