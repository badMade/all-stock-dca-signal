# Confirmed bug patterns

Recurring failure shapes that have been **confirmed** in this repo (reproduced or traced to a root
cause), kept so the next change checks for them before they recur. This is the checked-in home for
them; do not scatter them across PR descriptions or agent memory.

Related journals, kept separate on purpose:

- `.jules/sentinel.md` — security learnings (credential logging, XSS, unsanitized responses).
- `.jules/bolt.md` — performance learnings.
- `docs/dead_code_report.md` — dead-code candidates, not bugs.

## What belongs here

Add an entry only when the pattern is **confirmed** (a real incident, a reproduced failure, or a
root cause found in code) *and* generalizes beyond the one fix. A one-off typo does not belong;
"every fix to a cap must be applied three times" does. A regression test for the specific bug
still goes in `tests/` per CLAUDE.md; this file records the *shape*.

## Entry template

```markdown
### <Short name>

- **Confirmed:** YYYY-MM-DD — <incident, PR, or test that proved it>
- **Symptom:** what was observed.
- **Root cause:** why it happens.
- **Check for it:** a concrete grep, test, or review question.
- **Guard:** the test or code that now catches it (path), or "none yet".
```

## Patterns

### Duplicated logic drifts across the three entry points

- **Confirmed:** documented in `docs/live_wiring.md`; see CLAUDE.md "Three entry points coexist".
- **Symptom:** a cap or sizing fix works on one path and not the others.
- **Root cause:** `run_dca.run_slot()`, the `MorningDCAExecutor` family, and `dca_runner.py` seed
  daily/weekly caps from three different sources (broker order history, same-day run artifacts,
  `StrategyState` plus artifact seeding).
- **Check for it:** before changing cap, budget, or sizing logic, list every entry point that
  computes the same quantity (`python -m scripts.generate_module_graph --impact <module>`).
- **Guard:** none yet beyond per-path tests.

### Stale squash-merge reverts `main`

- **Confirmed:** 2026-09-03 — PR #658 reverted ~150k lines of `main`.
- **Symptom:** a merge silently removes large amounts of already-landed work.
- **Root cause:** a long-lived branch was squash-merged from a stale base, so its older copy of
  `near_close_dca.py` won.
- **Check for it:** inspect `near_close_dca.py` line history before treating `main` as the current
  side of a merge; compare diffstat against the branch's expected size.
- **Guard:** none yet.

### Drive-mirrored checkout hard-links every write

- **Confirmed:** observed on the operator's Mac under `~/Documents`.
- **Symptom:** any `st_nlink == 1` check fails for 7–11 seconds after a file is written.
- **Root cause:** the Google Drive mirror hard-links files written in the checkout.
- **Check for it:** reproduce under `/private/tmp`; if the failure disappears, it is the mirror,
  not the code.
- **Guard:** none; avoid `st_nlink == 1` as a safety check on paths under `~/Documents`.

### Out-of-range dates in the ticker archive

- **Confirmed:** `CBU.csv` holds a yearless `04-27` that parses to year 0001.
- **Symptom:** `build_panel` exits 139 (segfault) with no traceback, which looks like OOM.
- **Root cause:** a bad date row, not memory pressure.
- **Check for it:** on an unexplained exit 139 while loading the archive, scan for dates outside a
  sane range before blaming memory.
- **Guard:** none yet.
