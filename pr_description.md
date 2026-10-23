## Scope

This branch started as a one-line performance fix and accumulated related schedule-evidence and backup-pipeline work. It is documented here in full rather than as "just a regex optimisation".

**Performance (the original change)**
- `near_close_dca._artifact_day_matches_scheduled_provenance` uses a module-level precompiled `re` object instead of an inline `re.match` (about 2.3x faster in `python3 -m timeit`; see `.jules/bolt.md`). No other change to `near_close_dca.py`.

**Schedule evidence and monitoring**
- `scripts/finalize_run_evidence.py`, `scripts/backfill_afternoon_evidence.py`: deployment accounting, operator-attributed order backfill, separate invocation vs write timestamps.
- `scripts/verify_schedule.py`: failed-start classification over the finalizer's acceptance window (so an early crash is a crash, not `slot_did_not_fire`); undecodable attempt logs degrade to missing evidence.
- `scripts/schedule_watchdog.py`, `scripts/planner_stamp_check.py`, `deploy/launchd/*.plist.template`, `scripts/agent_entry_points.py`: weekday post-close audit and planner-freshness notifications; `osascript` failures propagate.
- `scripts/drift_detection.py`, `deploy/run-drift-check.sh`, `deploy/systemd/dca-drift-*.service`: header-only signal files, sanitized Slack failures, `REPO_ROOT` parameterized with `$$` deferral and quoting.

**Backup pipeline and prompts**
- `scripts/backup_procedure.py`, `scripts/assemble_market_data.py`, `claude/dca-executor-prompts/**`: published-set discovery, interpreter guard, bridge install diagnostics, dry-run switch, regenerated Perplexity prompts.
- `backup_planner.pyz`: rebuilt from the merged sources (`scripts/build_backup_planner.py`). `scripts/backup_planner.py` itself is identical to `main` (decimal-string quotes are still accepted).
- `config/recommendations.json` is identical to `main` (GitHub publisher stays enabled).

**Not in this PR:** any change to sizing, budgets, or order placement in `near_close_dca.py`, `morning_dca.py`, or the console state/config modules (restored to `main`).

✅ **Verification:**
```bash
ruff check .
python -m pytest -q
python -m pytest scripts/tests -q
python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo
python -m scripts.build_backup_planner --out backup_planner.pyz --check
```
🔒 **Secrets Confirmation:** Confirmed no secrets were added.
🧭 **Entry-Point Coverage:** `run_dca.run_slot()` (regex only); prompt-driven slots and backup execution via the evidence/finalizer/prompt changes above.
