# Run-artifact retention policy (proposed)

**Status: proposed policy, written 2026-11-13. It is a written commitment only. No pruning code exists, and
nothing here deletes or moves a file.** Today the artifact tree under `cron_tracking/` is never pruned
(`PRODUCTION_READINESS.md` gap 2: "no retention policy"), so this page records what should be kept, for how
long, and which parts are the audit record, so that a later prune job has a spec to implement and a backup job
has a spec to cover.

This is an engineering-hygiene policy for a personal account, not legal or compliance advice. If the account
ever carries other people's money or falls under a recordkeeping rule (for example SEC 17a-4 or FINRA 4370
style retention), have a compliance professional set the periods.

## What counts as the record

| Artifact | Why it matters | Minimum retention |
|---|---|---|
| `run_state.json` and `orders*.csv` per scheduled live run | Source of truth for same-day allocation guards and caps | 7 years |
| `raw/` broker payload snapshots and `orders_after_place.json` | Reconciliation evidence for each order | 7 years |
| Gate 9 evidence files, alert files | Proof a slot ran and what it decided | 7 years |
| Pointers (`latest_*_run.json`) | Cross-run coordination only | Overwritten by design; keep no history |
| Whole-share accumulator ledgers (`whole_share_accumulation/<TICKER>/ledger.json`) | Durable balance, **not reproducible from history** | Forever; include in every backup ([restore_runbook.md](restore_runbook.md)) |
| Manual runs (`manual_runs/<timestamp>/`) | Debugging; excluded from scheduled metrics | 1 year |
| Demo and dry-run output, `artifacts/`, readiness reports | Reproducible from code | 90 days |

7 years is a conservative default borrowed from common brokerage recordkeeping practice. Shorten it only on
purpose and note the date here.

## Rules any prune job must follow

1. **Never touch the whole-share ledgers or a run less than 400 days old.** The 7-day kill switch and the cost
   scans read recent history; a run state that vanishes from the window reads as "nothing deployed".
2. **Archive before delete.** Compress a closed month into one tarball and write its SHA-256 beside it; delete
   originals only after the archive is verified and backed up off the machine.
3. **Fail closed.** If the archive step cannot prove it wrote and verified the file, delete nothing.
4. **Dry run by default**, listing what it would remove, the same convention as the rest of the repo.
5. **Backups come first.** Do not enable pruning before `scripts/backup_state_root.py` has run on a schedule and
   one restore has been tested ([restore_runbook.md](restore_runbook.md)).

## Open items

- Implement the prune job against rules 1 to 4 (not started).
- Decide where off-machine archives live and who can read them.
