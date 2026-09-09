# Gate 5 signal generation for the 10:30 AM morning slot

`scripts/generate_morning_candidates.py` is the signal-generation entry point for
cron `42173154` (the 10:30 AM ET morning live buy, `run_type=morning_dca_live`).

Before it existed, that slot's Gate 5 aborted every weekday with *"no
signal-generation entry point exists for this slot; nothing to buy"* — the run
cleared every read-only gate and then stopped because there was no candidate
producer to call.

It is the sibling of `docs/market_open_signal_entry.md` (0935ET),
`docs/intraday_signal_entry.md` (1130/1330/1430ET) and
`docs/midday_signal_entry.md` (1230ET). All four run on the shared core in
`scripts/stream_candidates.py`; this page covers only what is different about the
morning slot. Read the market-open page, or the shared module's docstring, for
the *mechanics* of the market-data payload contract, the `cands` field contract
and the fail-closed input rules — those are identical. The **source key** is not:
see the next section.

### Market data source for this slot

`MORNING_SPEC.source` is `SourceName.ROBINHOOD`, not `SourceName.FINANCE`. This
slot's `--market-data` payload must carry a populated `sources.robinhood` object;
that is the key `build_document` requires and scores.

Unlike the Finance-primary slots, **Finance and Alpaca are not accepted keys at
all here** -- `run_cli()`'s `accepted_sources` is built from `spec.source`
(robinhood) plus the yfinance fallback key only, so a `sources.finance` or
`sources.alpaca` object in the payload raises `"unsupported key(s)"`, it does
not get accepted as validation/context. `sources.yahoo_finance_yfinance` is an
accepted key but is **not a usable fallback for this slot**: the "missing
primary can be met by yfinance instead" allowance in `run_cli()` is gated on
`spec.source is SourceName.FINANCE`, so for this Robinhood-primary spec a
`robinhood` entry in `source_errors` still aborts even when yfinance data is
present (`test_cli_does_not_substitute_yfinance_for_a_robinhood_primary_spec`).
A Robinhood outage at 10:30 has no automatic data-source fallback today.

This is a deliberate, confirmed exception (2026-09-09) to the repo-wide
Finance-primary policy in `docs/safeguards.md#market-data-source-policy` and
`config/scheduled_tasks.json:market_data_source_policy`, which both carry a
`slot_overrides.1030ET` entry recording it. `docs/morning_dca.md`'s "10:30 runner
self-containment" section and every other slot's Gate 5 doc still describe
Finance-primary; do not assume this page's payload shape from those without
checking this section first.

```bash
python3 -m scripts.generate_morning_candidates \
    --trading-day 2026-08-26 \
    --market-data $ROOT/market_data_1030ET.json
```

`$ROOT` is this slot's own evidence directory,
`dca_paths.stream_root('morning_dca', '<YYYYMMDD>_1030ET')` — the same one
Gate 4 already writes `orders_week.json` under. `stream_root()` joins the text it is given verbatim -- it does not normalize the date -- so the COMPACT `YYYYMMDD` form is required: a dashed `<YYYY-MM-DD>` suffix silently creates a sibling directory that the pointer and artifact consumers never scan (Codex review: "Document the compact artifact directory suffix").

## This slot drives the base class, not a subclass

0935ET, 1130/1330/1430ET and 1230ET each drive a *subclass* of
`MorningDCAExecutor`. This one drives `MorningDCAExecutor` itself, in
`MORNING_LIVE_MODE` — the morning stream is where that class's default
(watchlist) mode lives alongside the live one. Two things follow, and they are
the whole of what is special about this page.

### 1. The config loader takes `mode` positionally-second

Every other loader in this family is
`load_*_config(config_dir, *, allow_unpinned_account=...)`. `load_morning_config`
is `load_morning_config(config_dir, mode, *, allow_unpinned_account=...)`, with
`mode` defaulting to the **9:45 watchlist** profile.

`StreamSpec` calls every loader as `loader(config_dir, allow_unpinned_account=True)`.
Handing it the bare function would therefore have loaded watchlist policy for a
live 10:30 run — same file, different artifact roots, different
`target_account_number` handling — with nothing raising to say so.
`load_morning_live_config()` in the module pins the mode; a test asserts that the
spec uses it and that the bare loader really would have bound the watchlist roots
instead.

### 2. The 9:45 pre-flight gate runs

`MorningDCAExecutor.evaluate()` calls `_apply_preflight_gate()`, which is active
for exactly one combination — `MORNING_STREAM` + `MORNING_LIVE_MODE` — and so
fires here and in none of the sibling entry points. It narrows the universe to
the tickers the same-day 9:45 pre-flight scan marked `eligible_to_execute=true`,
reading them off the `morning_dca_preflight` artifact tree.

**This makes it the only Gate 5 command in the family that touches the artifact
tree, and the only one whose output is not purely a function of its arguments.**

Three consequences worth knowing:

- **It is deliberately not fail-closed.** Per Geoff's 2026-08-02 direction
  (*"should not block any later runs"*), a missing, unparseable, misconfigured or
  valid-but-empty pre-flight artifact degrades to the **full configured
  universe** with a warning — for scheduled and manual runs alike. A pre-flight
  that flagged itself invalid (`overall_ok=false`) is treated the same as a
  missing one. The 9:45 scan is a coarse pre-screen, never a hard gate the 10:30
  slot depends on to run; the 10:30 slot's own signal, trend, budget, spread and
  top-up checks are the real safeguards. See
  `morning_dca.morning_preflight_eligible_tickers` for the enforcement, which is
  shared with `run_dca.run_slot("1030ET", ...)` so there is one implementation of
  the contract.
- **The verdict is reported, not swallowed.** The document's `preflight_gate`
  field carries the executor's own gate diagnostic — whether an artifact was
  found, its run id, `admitted_tickers`, `gated_out_tickers`, whether it was
  recovered via the bounded lookup, and any degradation warning. A reader that
  sees a short candidate list can tell a narrow pre-screen from a quiet tape.
  The field is `null` in every sibling slot's document, so "gate not applicable"
  and "gate found nothing" never look alike.
- **Which tree is read follows config.** `config/morning_dca.json`'s
  `preflight_run_state_root` selects it, `preflight_forbidden_roots` refuses a
  root that resolves under a live-output or cron-ID tree, and
  `$DCA_CRON_TRACKING_ROOT` is honoured — so a relocated/staging run stays
  contained rather than reading production evidence.

## Trend validation applies — so the payload needs the session so far

`config/morning_dca.json`'s `trend_min_bars` (6) and
`MORNING_PROFILES[MORNING_LIVE_MODE]`'s `validate_trend: True` mean a candidate
whose price and drop qualify is auto-buy eligible **only** when at least 6 usable
intraday bars resolve over the 60 minutes before 10:30. One whose trend cannot be
validated is flagged `manual_review_candidate`: it stays in `candidates` and
`manual_review_tickers`, and is deliberately excluded from `cands` and
`same_day_top_up_candidates`.

**Fetch 5-minute bars from 09:30 ET onward** — at 10:30 the 60-minute lookback is
exactly the session so far, so this is the full regular session up to the anchor,
not just bars around the decision time. A truncated fetch does not error; it
quietly moves real buys into manual review. This is the opposite of 0935ET, which
is trend-*exempt* precisely because its 60-minutes-back bar would be pre-market.

## The trend window must be *spanned*, not just sampled

`MorningDCAExecutor._trend_validated` asks two things of the 60 minutes before
the decision anchor: are there at least `trend_min_bars` (6) usable bars in it,
and is the last close below the first. It does **not** ask whether those bars
*span* the window. Six consecutive 5-minute bars covering only the final 25
minutes satisfy both tests, so a truncated payload yields
`trend_validated: true` and an `auto_buy_eligible` row at full ladder size on a
fraction of the evidence the config asks for.

That rule belongs to the live executor and is shared by every trend-validating
stream, so this entry point does not change it — tightening it would alter live
eligibility on the in-repo executor path too, which is a sizing-policy decision
with money attached. What the entry point does instead is refuse to **publish**
such a row: it is kept in `candidates` with the executor's own verdict intact,
withheld from `cands` and `same_day_top_up_candidates`, and named in
**`withheld_incomplete_trend_window_tickers`**.

- The check is per-ticker and withholds rather than aborting. It cannot
  distinguish "the caller fetched too narrow a range" from "this symbol did not
  trade early in the window", and does not try to — both mean the downtrend was
  never observed over 60 minutes — so one thinly-traded symbol must not take the
  whole universe down with it.
- One interval of tolerance applies at the window start, because a provider that
  stamps bars at interval *end* puts a complete window's first bar one interval
  late.
- These rows are **not** `manual_review_candidate`s. The fix is a wider
  re-fetch and a re-run, not a human judgement call, so they are reported
  separately and must not be routed to the same queue.
- The list is always empty on the trend-exempt 0935ET slot, where a full window
  would start pre-market and is impossible by construction.

`buy_count > 0` with an empty `cands` is exactly this case (or its sibling,
`withheld_contradictory_base_tickers`). Check both before reading a short list
as a quieter tape.

## No `--deployed-last-7d-usd`

Like 0935ET and the intraday fires, and unlike 1230ET: this slot's prompt does
not make the trailing-7-day deployment kill switch the producer's responsibility.
Accepting the figure here would size *differently* from the executor this
mirrors, so the flag does not exist and the document reports
`kill_switch_evaluated: false`.

## Wiring it into the 10:30 task

The live task's prompt text lives in Cowork, not in this repo, and
`claude/dca-executor-prompts/live-mirror/1030ET-*.txt` is a *proposed* rewrite
rather than a verified capture — see `claude/dca-executor-prompts/README.md`. Its
Gate 5 now names this command; until a human applies that body through
`update_scheduled_task` and the mirror is re-captured, the live 10:30 task still
aborts at Gate 5 exactly as before.

Gate 5 in that file now reads: fetch the universe's 5-minute bars from **09:30
ET** through the 10:30 anchor, the previous session's daily bar, and a current
quote; save them as `$ROOT/market_data_<YYYY-MM-DD>_1030.json`; run the command
above; save `cands` to `$ROOT/cands.json`. Gates 6, 6.5, 6.7 and 7 are unchanged.

## Scope

Gate 5 and nothing past it. This command calls `MorningDCAExecutor.evaluate()`,
never `run()`: `run()` owns budget arithmetic, the same-day top-up guard,
placement, drift detection and the artifact tree — Gates 6 through 9 — which the
prompt's own gate sequence also owns. `evaluate()` needs no broker, no
credentials and no network, and cannot place an order. It does read the
pre-flight artifact tree, as described above; that is the one exception to
"writes nothing, reads nothing", and it is a read, never a write. See
`docs/midday_signal_entry.md`'s "Why it calls `evaluate()`, not `run()`" for the
full argument; it applies unchanged here.
