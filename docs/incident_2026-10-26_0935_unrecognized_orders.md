# Incident 2026-10-26: 0935 payload refused, 25 buys read as unrecognized

Status: mitigated (fix merged, Mac checkout updated). Open items listed at the end.

## Summary

The 09:35 ET slot on 2026-10-26 had no usable payload because two fetch workers failed, so no
plan-first recommendation set existed. Twenty-five agent buys were nevertheless placed between
09:48:56 and 09:50:40 ET. With no placement files and no published set, the backup classifier has
no way to recognise them, so they read as `unrecognized`.

## Facts

Verified from the Robinhood MCP account (ending 7042, read-only) and this repository:

- 25 filled buys were created 09:48:56 to 09:50:40 ET, all market orders, regular hours, day
  time-in-force, agent-initiated, dollar-based, $1.09 to $4.61 each, $52.40 in total.
- Symbols: AAPL, MA, MDLZ, SATA, RGTI, QUBT, NFLX, IBM, HSY, QBTS, FLKR, GOOG, GOOGL, AAAU, DFTX,
  CBRS, MRK, MSFT, RMD, LUNR, CL, SPCX, CAT, TSN, DGX.
- Their `ref_id` values are consecutive and time-ordered, about 4 to 5 seconds apart, which is
  consistent with one run.
- Ten more agent buys came later from other batches (about 10:56, 11:42, 12:56 and 15:00 ET), for
  $16.61. The day total for the account from 09:00 ET is 35 orders and $69.01.
- PR 1961 is merged into `main` as `5eafd4b6`. The Mac Studio checkout is at `8c219fe9`, which
  contains it.

Reported by the operator, not independently verified:

- The `codex exec` workers for chunks 4 and 10 died on both retries, leaving 16 of 91 tickers
  without data. Coverage was 75/91 (82%), below the 95% floor, so the 0935 payload was refused.
- The 10:30 fetch succeeded at 90/91, with one stale bar for FLKR.

## Cause

The classifier accepts an order as Claude's only through (see `scripts/backup_planner.py`,
`_classify_row`):

1. an order id saved in `recommendations/<day>/placements/<slot>/<TICKER>.json` on the Mac,
2. a `dca-runner-<slot>-` reference,
3. the derived native reference for that slot, day and symbol, or
4. a match on symbol and exact dollar amount in the published set (when the Mac is unreachable).

None applied. The refused payload meant no per-symbol placement files were written and no
`recommendations` branch was published. Inference: the unrecognized label follows from the missing
evidence, not from anything wrong with the orders themselves.

## Impact

- Money: none beyond the intended strategy. $52.40 for the 25 orders and $69.01 for the day,
  against the $180 Normal daily cap (38%) and a $10 per-order ceiling. The weekly cap was not
  checked precisely. About 8% of $900 if the week started Monday (assumption).
- Reporting: the 25 orders will show as unrecognized for Oct 26. The classifier is scoped to one
  slot window on one trading day, so Oct 27 is unaffected.
- Caveat: caps pool every stream, and the figures above cover only this account from 09:00 ET.

## Decision: no manual evidence

There is no sanctioned command to mark orders as recognized after the fact. Placement files are
evidence of what Claude's reader actually submitted. Writing them by hand would forge that
evidence and weaken the fail-closed check, so none were written. This note is the record instead.

## Actions taken

- PR 1961 merged to `main` as `5eafd4b6` (dead-chunk rescue for the payload fetch).
- Mac Studio checkout pulled with `git pull --ff-only origin main`. Result: already up to date at
  `8c219fe9`, which has `5eafd4b6` as an ancestor.

## Open items

1. **How were the 25 buys placed without a plan?** The operator's account says no set was handed to
   Claude, yet orders went out. The path that produced them is not identified. If a slot can buy
   with no computed plan, the plan-first fail-closed design has a gap, so find the path before the
   next incident.
2. Tomorrow (2026-10-27) after the 09:35 slot, confirm coverage reached the 95% floor,
   `placements/0935/*.json` has per-symbol files, and the `recommendations` branch has a set.
3. After the 09:55 ET backup run, confirm the classify step passed. Run
   `python3 -m scripts.backup_planner executor-ids --trading-day 2026-10-27 --slot 0935` on the Mac
   to see the recorded order ids.
4. The `#dca-alerts` Slack post failed with `not_in_channel`. Add the app to the channel to enable
   alerts.
5. Check whether the later batches (about 10:56 to 15:00 ET) were recognised.
