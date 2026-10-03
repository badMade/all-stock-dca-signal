# Runbook: all-stock-dca iteration (dry-run)

Use for a weekly or quarterly check. It never places an order; live runs belong to the scheduled roster.

1. `cd agent-vault && claude`. The hook loads `MEMORY.md`, the latest Daily note and the latest Reflection.
2. Ask: "Execute all-stock-dca iteration". The agent follows `skills/execute-dca/SKILL.md`.
3. It reads `vault/Knowledge/all-stock-dca-rules.md`, then the source files that note points at.
4. It runs, from the repo root with a fixed time (`${TMPDIR:-/tmp}` covers sessions where `TMPDIR` is unset):
   `python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir "${TMPDIR:-/tmp}/dca-demo"`
5. Optionally it summarizes the latest live run from the pointer files under `dca_paths.cron_tracking_root()`
   (read-only; skipped if the tree is absent).
6. It logs the command, artifacts path, grades, fills and slippage under `## Sessions` in `vault/Daily/YYYY-MM-DD.md`.
7. It captures durable facts into the rules note, then closes with `reflect`.
8. The close-out commit is `chore: memory update` plus a summary that starts `[all-stock-dca] YYYY-MM-DD <outcome>`.
9. Next time, `improve` compares reflections and edits the rules note or the skill only if a lesson repeated and a
   fresh verifier could not refute it.

Stop and ask if anything suggests a live run, a changed limit, or a missing source file.
