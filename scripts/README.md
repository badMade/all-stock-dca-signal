# Strategy backup utilities

This directory holds standard-library-only utilities:

- `backup_strategy.py` — re-extract strategy config from a transcript (below).
- `run_classification.py` — enforce the scheduled-vs-manual **run-classification
  contract** (see [`../docs/run_classification.md`](../docs/run_classification.md)).
- `drift_detection.py` — reconcile a run's `signals.csv` (intended auto-buys)
  against `submitted_orders.csv` (actual order records) and alert on drift (see
  [`../docs/drift_detection.md`](../docs/drift_detection.md)).

## `drift_detection.py`

Runs immediately after each live trading task writes its artifacts and flags any
symbol with an auto-buy signal and a positive intended amount but no
submitted-order record — the Jul 16 2026 failure mode (signals written, orders
neither placed nor persisted). Emits a compact JSON + markdown diagnostic and,
optionally, an immediate Slack alert (webhook read from `DCA_DRIFT_SLACK_WEBHOOK`,
never hardcoded).

```bash
python scripts/drift_detection.py --run-dir <run_dir> --fail-on-drift
python -m pytest scripts/tests/test_drift_detection.py -q
```

## `run_classification.py`

Keeps manual runs of the live task (`10ce362c`, which supersedes the deleted
`ce16ba6b`; artifact root stays under `.../ce16ba6b` for continuity) from being
counted as scheduled runs. It builds/validates the required `run_classification` block, writes
run_state artifacts to the correct path (updating `latest_scheduled_run.json`
**only** for scheduled runs), collects and excludes manual order IDs, and
selects the scheduled state for reporting with the narrow broker time-window
fallback. The live trading code and the reporting/watchdog/weekly tasks should
route their scheduled-vs-manual decisions through this module.

```bash
python -m pytest scripts/tests/test_run_classification.py -q
# or, without pytest:
python scripts/tests/test_run_classification.py
```

## `backup_strategy.py`

`backup_strategy.py` extracts the current graded DCA strategy state from a
conversation/export transcript and writes/updates the `config/` JSON files. Use
it whenever the strategy changes so the backup stays current.

- Standard library only (Python 3.8+). **No secrets, tokens, or network calls.**
- Dry-run by default; pass `--write` to persist.
- Robust to imperfect transcripts — missing sections yield empty results, not errors.

## What it extracts

| Section | Output file (with `--write`) | Notes |
|---|---|---|
| Ticker universe | `config/tickers.json` | active, excluded, `X -> Y` corrections |
| Sizing ladder | `config/sizing_ladder.json` | drop-% tiers → multiplier; keeps the latest ladder |
| Budget rules | `config/budgets.json` | base amount, tiered daily/weekly caps, weekly rollover reserve cap |
| Schedule | `config/schedule_extracted.json` | cron → ET, task ids, weekday-only flag |

Writes are **merges**: existing keys in a target file are preserved; extracted
values overwrite only the fields they cover. Review the diff before committing.

> The schedule extractor writes to `schedule_extracted.json` (not
> `scheduled_tasks.json`) so it never clobbers the hand-curated, fully-annotated
> `config/scheduled_tasks.json`. Reconcile manually.

## Usage

```bash
# Preview what would be extracted (prints JSON, writes nothing):
python scripts/backup_strategy.py --transcript thread.md

# Extract and write all config files:
python scripts/backup_strategy.py --transcript thread.md --write --out-dir config

# Only refresh specific sections:
python scripts/backup_strategy.py --transcript thread.md --write --only tickers ladder
```

If direct conversation access isn't available in the runtime, save the thread /
export to a markdown or text file and pass its path via `--transcript`.

## Transcript format hints

The extractors key off natural phrasings, e.g.:

- `Current active ticker universe:` followed by a comma-separated list, then
  `Excluded: BYDDY` and `Corrected symbols: IRDMI -> IRDM`.
- Ladder lines like `0.1% to <1% = 1x`, `>=8% = 5x`. A `drop <0.1% = 0x` line
  sets the no-buy threshold.
- Budget lines like `Base amount $3.`, `Weekly rollover reserve max $900.`, and
  tier lines like `Normal: daily cap $180, weekly cap $900.`.
- Schedule lines like `10ce362c ... schedule 20 19 * * 1-5 UTC = 3:20 PM ET weekdays`.

## Tests

```bash
python -m pytest scripts/tests -q
# or, without pytest:
python scripts/tests/test_backup_strategy.py
```

The tests assert the 45-ticker universe, the latest ladder (max 5x, no 12x-era
tiers), the budget caps, and that the **3:20 PM ET live task** stays distinct
from the **3:35 PM ET slippage/Notion sync**.

## Notion sync

This utility does **not** call Notion. For the conceptual GitHub ↔ Notion sync
recipe and an illustrative payload (no credentials), see
[`../docs/notion_dashboard_template.md`](../docs/notion_dashboard_template.md).
