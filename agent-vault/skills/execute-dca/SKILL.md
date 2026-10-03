---
name: execute-dca
description: Run one all-stock-dca iteration in dry-run and log it. Use when the user says "execute all-stock-dca iteration", "run the DCA check", or wants the deterministic demo run, a determinism diff, or a summary of the latest live run's artifacts. Never places orders.
---

# Execute DCA

One job: run the repo's dry-run pipeline (or read a finished live run's artifacts),
record grades, fills and slippage, and leave a trail the next session can use.

STATUS: dry-run only. Live runs belong to the external scheduled sessions that hold
broker access (see `../CLAUDE.md`). This skill never calls an order tool, never edits
`config/` or run state, and never passes a live flag.

> `vault/…` paths resolve against the vault base printed in the SessionStart header.

## Steps

1. **Read the rules.** Open `vault/Knowledge/all-stock-dca-rules.md`. Pull limits from
   the files it points at (`docs/safeguards.md`, `config/budgets.json`); never quote a
   number from memory.
2. **Run the dry run** from the repo root, with an explicit `--now` so the input is
   fixed, and an artifacts dir outside the checkout:

   ```bash
   base="$(mktemp -d "${TMPDIR:-/tmp}/dca.XXXXXX")"; out="$base/dca-demo"
   python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir "$out"
   ```

   Use the date the user names; record the exact command you ran.
3. **Optional, read-only: summarize the latest live run.** Resolve the tree with
   `dca_paths.cron_tracking_root()` (it honours `$DCA_CRON_TRACKING_ROOT`). Read the
   pointer files only (`latest_scheduled_run.json` and the per-stream pointers). If the
   tree is absent, say so and skip; never guess fills. Log aggregates only (counts,
   totals, the artifact path): never order IDs, per-order amounts or account data.
   Daily notes are committed, while live run output stays in the gitignored cron tree.
4. **Determinism check, when asked.** Run step 2, move the result aside
   (`mv "$out" "$base/first"`), run step 2 again at the same path, then
   `diff -r "$base/first" "$out"`. The run records its own output directory, so two
   different directory names differ by construction. Report every differing file and
   whether it is a timestamp or run id. Identical trees are the pass condition.
5. **Log** under today's `## Sessions` in `vault/Daily/YYYY-MM-DD.md`: the command, the
   artifacts path, grades and fills, slippage if the report has it, and anything that
   broke a limit in the rules note.
6. **Capture** durable facts (a rule that changed, a recurring skip reason) with the
   `capture` skill into `all-stock-dca-rules.md`. Do not touch `MEMORY.md` yet.
7. **Close** with the `reflect` skill. Start its commit summary with
   `[all-stock-dca] YYYY-MM-DD <outcome>`.

## Rules

- Dry-run only. If the user asks for a live run, stop and point them at the scheduled
  roster; do not improvise one.
- No account numbers, tokens or absolute machine paths in notes or commands.
- A limit you cannot find in the source files is `UNVERIFIED`; say so, do not fill it in.
- Propose rule changes through `reflect`; `improve` applies them after a repeat.
