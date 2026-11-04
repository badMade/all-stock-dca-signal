# Sizing suggestion history

The graded sizing ladder is still being *determined*. Several independent suggesters --
`ml-forecaster/`, `wondrous-octopus/` (via `scripts/publish_octopus_sizing.py`), and any future
ones -- each propose per-ticker multipliers. The operator compares them over time (minimum six
months of human review) to decide which, or which blend, earns a place in live sizing.

That comparison is only meaningful if:

1. no suggester overwrites live sizing (`config/ml_sizing_adjustment.json`, which
   `dca_config.load_ml_sizing_adjustment` feeds to `near_close_dca.graded_buy_multiplier`);
2. no suggester overwrites another; and
3. every recommendation is kept as history, not just the latest.

## Where it lives

`sizing_suggestion_history.py` (stdlib only). One JSON line per suggester run is appended to
`<cron root>/sizing_suggestions/<suggester>/<YYYY>.jsonl` (`$DCA_CRON_TRACKING_ROOT`; gitignored
with the rest of the run output). Streams today: `ml_forecaster`, `octopus`.

| Field | Meaning |
| --- | --- |
| `suggester`, `run_id` | stream name and a unique id for the run |
| `generated_at` | when the suggester's source data was produced |
| `recorded_at` | wall clock at append time (a stale forecast republished later shows up as `recorded_at` far after `generated_at`/`source.origin_date`) |
| `status` | the suggester's own outcome: `published` or a `blocked_*` reason. Blocked runs are recorded too |
| `bounds`, `adjustments` | the suggester's multiplier bounds and `{TICKER: multiplier}` |
| `model_version`, `source`, `metrics` | provenance and eval numbers each suggester has |

Appends are locked and fsynced; existing lines are never rewritten. A torn final line is skipped
by `iter_history`/`read_history`. The files are plain JSONL, so DuckDB can query them directly:

```sql
SELECT suggester, recorded_at, unnest(map_entries(adjustments)) FROM read_json_auto('sizing_suggestions/*/*.jsonl');
```

## What changed from before

- `scripts/publish_octopus_sizing.py` used to write `config/ml_sizing_adjustment.json` (with a
  1.4 cap, bypassing ml-forecaster's gate). It now only appends to the `octopus` stream; the
  `--output`/`--status` flags are gone.
- `ml-forecaster/publish.py`'s `DEFAULT_CONFIG_PATH` is now its own incumbent file under the
  history root, not the live config. The regression gate reads that file, so gate behaviour is
  unchanged. `guarded_publish` also appends every run (published or `blocked_regression`).
  `train.py --output` can still point at the live file for a deliberate promotion.
- `config/ml_sizing_adjustment.json` stays `inactive`: the live factor is 1.0.

## Comparing the suggesters

`sizing_suggestion_compare.py` (stdlib only, read-only) scores the streams against what the market did next:

```bash
python -m sizing_suggestion_compare --horizon 5            # print the scorecard
python -m sizing_suggestion_compare --horizon 5 --write    # also write <history root>/comparisons/<date>_h5.{json,md}
```

- **Tilt** = `multiplier - 1`; a ticker a run does not mention has tilt 0.
- **Outcome** = forward return of the adjusted close `--horizon` trading days after the last close on or
  before the run's date (`--as-of recorded_at|generated_at`). Runs whose horizon has not elapsed are
  *pending* and never scored. Prices come from the ticker archive; bad dates and non-positive prices are dropped.
- **Baseline** = the mean forward return of the active universe that day, so an up-only suggester must beat the
  average ticker, not zero.
- Per suggester: `tilt_weighted_excess` (sum of tilt x excess return over sum of |tilt|), `hit_rate` (share of
  non-zero tilts whose sign matched the sign of excess return) and `mean_ic` (per-run correlation of the tilt
  vector with excess return across the universe). Two runs in one day count once (the latest wins).
- `blend_equal` is the equal-weight mean tilt of the real suggesters, with no tuning, so it can't overfit. It
  appears only when at least two streams have data.

Read the run counts next to every number: a handful of matured runs is noise. The comparison never writes live
sizing; promoting any suggester or blend stays a manual edit after the review period.

## Octopus freshness gate

`scripts/publish_octopus_sizing.py` refuses to record (status `stale_source`, exit 1, nothing written, also on
`--dry-run`) when the forecast CSV's newest `origin_date` is missing, unparseable, more than one day in the future, or
older than `--max-origin-age-days` (default 5: a weekend plus one market holiday). Without it a stale
`all_shards_tomorrow.csv` re-run later would be stamped with a fresh `recorded_at` and read as a new forecast.

## Viewing the history in DuckDB or Notion

The JSONL stays the source of truth. `python -m sizing_suggestion_history --export-csv PATH [--suggester NAME]`
writes a derived, rebuildable CSV with one row per ticker per run (`suggester, run_id, recorded_at, generated_at,
model_version, status, ticker, multiplier, tilt, min_multiplier, max_multiplier, origin_date`). It is read-only over
the history and adds no dependency; rerun it to refresh.

- **DuckDB:** `SELECT suggester, ticker, avg(tilt) FROM read_csv_auto('history.csv') GROUP BY ALL;` or skip the CSV
  and query the files directly with `read_json_auto('<cron root>/sizing_suggestions/*/*.jsonl')`.
- **Notion:** import the CSV into a database (Notion, `...` menu, Merge with CSV). Nothing in this repo calls the
  Notion API for it, so a refresh is a manual re-import.

## Not built yet

An automated push of the CSV to Notion (it is a manual import today).
