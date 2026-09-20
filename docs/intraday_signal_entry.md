# Gate 5 signal generation for the 11:30 AM, 1:30 PM and 2:30 PM intraday slots

`scripts/generate_intraday_candidates.py` is the signal-generation entry point
for cron `7be82b97` — all three of its weekday fires
(`run_type=intraday_dca_live`).

Before it existed, each of those fires aborted at Gate 5 with *"no
signal-generation entry point exists for this slot; nothing to buy"* — the run
cleared every read-only gate and then stopped because there was no candidate
producer to call.

It is the sibling of `docs/market_open_signal_entry.md` (0935ET),
`docs/morning_signal_entry.md` (1030ET) and `docs/midday_signal_entry.md`
(1230ET). All four run on the shared core in `scripts/stream_candidates.py`;
this page covers only what is different about the intraday stream. Read the
market-open page, or the shared module's docstring, for the market-data payload
contract, the `cands` field contract and the fail-closed input rules — they are
identical.

```bash
python3 -m scripts.generate_intraday_candidates \
    --slot 1330 \
    --trading-day 2026-08-26 \
    --market-data $ROOT/market_data_1330ET.json
```

`$ROOT` is that fire's own evidence directory,
`dca_paths.stream_root('intraday_dca', '<YYYYMMDD>_1330ET')` — the same one
Gate 4 already writes `orders_week.json` under. `stream_root()` joins the text it is given verbatim -- it does not normalize the date -- so the COMPACT `YYYYMMDD` form is required: a dashed `<YYYY-MM-DD>` suffix silently creates a sibling directory that the pointer and artifact consumers never scan (Codex review: "Document the compact artifact directory suffix").

## Why one command for three slots

Because the repo already models them as one thing. The three fires share:

| | |
| --- | --- |
| Cron | `7be82b97` (one scheduled task, three fires) |
| Executor | `IntradayDCAExecutor` — **one class**, with `slot=` as a constructor argument |
| Config | `config/intraday_dca.json` |
| Artifact root | `intraday_dca/` |
| Pointer | `latest_intraday_run.json` |
| Budget | one pooled Normal-tier ceiling, not one each |

`INTRADAY_SLOT_PROFILES` differ in exactly three things: the decision anchor
(11:30 / 13:30 / 14:30), the ±5-minute window, and the `<slot>ET` folder suffix.
So this module is three `StreamSpec` literals selected by `--slot`, derived from
`intraday_dca.INTRADAY_SLOTS` rather than written out three times — a fourth fire
added to that tuple gets an entry point instead of a `KeyError` at 2:30.

Three copied modules would instead be three places for a review round's
hardening to reach two of, which is the drift `CLAUDE.md`'s "Three entry points
coexist" warning is about.

## `--slot` is required and has no default

`IntradayDCAExecutor` defaults `slot` to `DEFAULT_INTRADAY_SLOT` (`"1130"`),
which is right for a library caller and wrong for this command. A 2:30 PM run
that omitted the flag would silently score its own fresh payload against an
11:30 anchor — rejecting every bar after 11:30 as future-dated — and publish the
result under the `1130ET` slot label, into the wrong evidence folder.

So the flag is required, `argparse` restricts it to the three real tokens, and
`spec_for_slot()` (the programmatic entry) raises on anything else rather than
falling back. `1230` and `1530` are refused explicitly: they are real slots of
*other* streams, which is the mistake most likely to be made here.

The document echoes both spellings — `slot` (`"1330ET"`, the label the prompts
and folder names use) and `runner_slot_id` (`"1330"`, the key
`dca_runner.SLOTS` uses).

## Trend validation applies to all three

Unlike 0935ET — which is trend-*exempt* because its 60-minutes-back bar would be
pre-market and cannot exist — every intraday fire has a full 60-minute session
window available. `config/intraday_dca.json`'s `trend_min_bars` (6) and the
profiles' `validate_trend: True` mean a candidate whose price and drop qualify
is auto-buy eligible **only** when at least 6 usable intraday bars resolve over
the 60 minutes before its own anchor. One whose trend cannot be validated is
flagged `manual_review_candidate`: it stays in `candidates` and
`manual_review_tickers`, and is deliberately excluded from `cands` and
`same_day_top_up_candidates`.

**Each fire's payload must therefore carry 5-minute bars from 60 minutes before
its own anchor:**

| `--slot` | Decision anchor | Fetch bars from |
| --- | --- | --- |
| `1130` | 11:30 ET | 10:30 ET |
| `1330` | 13:30 ET | 12:30 ET |
| `1430` | 14:30 ET | 13:30 ET |

A truncated fetch does not error — it quietly moves real buys into manual
review.

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

Like 0935ET and 1030ET, and unlike 1230ET: none of the three intraday prompts
makes the trailing-7-day deployment kill switch the producer's responsibility.
The 1430ET prompt references the kill switch only as a funding input. Accepting
the figure here would size *differently* from the executor this mirrors, so the
flag does not exist and the document reports `kill_switch_evaluated: false`.

## The shared budget is Gate 6's, not this command's

All three fires draw on **one** pooled Normal-tier ceiling — `cap_seed_roots`
includes this stream's own root, so 1:30 seeds from 11:30's fills and 2:30 from
both. This command knows nothing about that: it evaluates signals only, so an
11:30 fill does not reduce the amounts a 1:30 document proposes.

That offset belongs to the executing agent's Gate 6 and Gate 6.5, exactly where
each prompt already has it. Do not "fix" it here by having a Gate 5 producer scan
the artifact tree — that would put two independent implementations of the pooling
arithmetic on one account in one day.

The narrower Gate 4 case *is* handled: `--already-covered-tickers` names symbols
a compatible executor already placed an order for in **this same window**, and
they are never scored and never published.

## Wiring it into the three tasks

The live task's prompt text lives in Cowork, not in this repo, and
`claude/dca-executor-prompts/live-mirror/1130ET-*.txt` (and its 1330/1430
siblings) is a *proposed* rewrite rather than a verified capture — see
`claude/dca-executor-prompts/README.md`. Each Gate 5 now names this command with
its own `--slot`; until a human applies those bodies through
`update_scheduled_task` and the mirrors are re-captured, the live fires still
abort at Gate 5 exactly as before.

`config/intraday_dca.json` ships `live_execution_enabled=false`. That gates
placement *inside* `IntradayDCAExecutor.run()`, which this command never calls,
so it neither enables nor suppresses anything here — its value is reported in the
document so a reader can see it.

## Scope

Gate 5 and nothing past it. This command calls `IntradayDCAExecutor.evaluate()`,
never `run()`: `run()` owns budget arithmetic, the same-day top-up guard,
placement, drift detection and the artifact tree — Gates 6 through 9 — which each
prompt's own gate sequence also owns. `evaluate()` needs no broker, no
credentials and no network, writes nothing, and cannot place an order. See
`docs/midday_signal_entry.md`'s "Why it calls `evaluate()`, not `run()`" for the
full argument; it applies unchanged here.
