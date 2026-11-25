# Rock 3 (#3): did the 13:30 miss cause the 14:30 cap exhaustion? (2026-11-19)

**Answer: no.** The normal-tier daily cap ($180) was already almost spent before 13:30, so the lost 13:30 buys did not
starve 14:30 (they would have competed with it for the same remaining headroom). The 14:30 slot ran out of headroom because 09:35 and 10:30 spent most of the day's cap. No cap-seeding
mismatch is needed to explain it; none was ruled out either (see "Not checked").

Source: each slot's `submitted_orders.csv` (`submitted_amount_usd`), `run_state.json` (`budget`) and `gate9_report.notes`
under the operator's `cron_tracking/` tree. No order IDs or account values are quoted.

| Slot | Run dir | Orders | Placed USD |
| --- | --- | --- | --- |
| 09:35 | `market_open_dca/20261119_0935ET` | 16 | 54.72 |
| 10:30 | `morning_dca/20261119_1030ET` | 18 | 78.49 |
| 11:30 | `intraday_dca/20261119_1130ET` | 2 | 12.00 |
| 12:30 | `midday_dca/20261119_1230ET` | 2 | 13.58 |
| 13:30 | `intraday_dca/20261119_1330ET` | 0 | 0.00 (aborted, Gate 6 history) |
| 14:30 | `intraday_dca/20261119_1430ET` | 3 | 21.13 |
| 15:30 | `ce16ba6b/scheduled_runs/20261119_1530ET` | 4 | 36.72 |

- `run_state.json` `budget` at 14:30: `budget_tier: normal`, `daily_cap_usd: 180`, `weekly_cap_usd: 900`.
- Slots 09:35 to 12:30 placed $158.79, so at most $21.21 of the $180 cap (less any 12:43 backup-period orders, which the 14:30
  `gate9_report.notes` mentions but this note does not quantify) was left when 13:30 fired.
- 14:30 placed $21.13 (VCX, AMZN, QQQ per its notes) and its notes say the normal-tier cap was exhausted after those three,
  with 63 more candidates unplaced. $158.79 + $21.13 = $179.92, consistent with the $180 cap.
- 15:30 ran at the stress tier (its notes: 78 signals >= 3x, binding ceiling buying power $37.66), which is why the day's
  total ($216.64) exceeds the normal $180 cap. That is the tier ladder, not a breach.

**Consequence.** The cap is a shared daily pool. A working 13:30 slot would have faced the same at-most-$21.21 headroom and
would have spent it on its own candidates instead of 14:30's three. Day-total spend would have been about the same; only which
tickers got bought, and when, would differ. The 13:30 failure is still a real reliability bug (Issue #1), but its dollar cost
on 2026-11-19 was close to zero. It would matter on a day the earlier slots leave more cap unspent.

**Not checked (needs the operator or a later cycle).** Whether the three entry points seed the daily cap identically
(`docs/live_wiring.md`): all seven slots here read the same shared cap (per the 14:30 note), which is consistent with
agreement but is one day of evidence, not a test.
