# Plan-first recommendations

> **Deprecation (2026-10-26).** The 5-minute Claude courier (`claude/dca-executor-prompts/drafts/plan-first/courier.draft.txt`, `scripts/snapshot_cycle.py fetch-plan`) is deprecated and must not be scheduled. Bars and quotes come from the payload launcher (`scripts/robinhood_payload.py`, [robinhood_payload.md](robinhood_payload.md)). The courier also saved `get_equity_orders` pages and the `get_portfolio` buying power, which the payload does not fetch; `snapshot_cycle ingest` stays as the reference for that raw-file contract. The recommender, store, reader and fill-comparison below are unchanged.

Plan-first separates computation from the buying agent. The engine computes the recommendation and private expected-price evidence; the reader receives only ticker, dollar amount and reference ID. The buying agent does no sizing math and fetches no prices.

## Data flow

Courier → ingest → recommendation engine → store/publish → slot reader → placement → fill comparison. The courier saves raw read-tool responses; ingest validates and normalizes them. The engine writes an agent set and a private expected-price set. The reader selects a fresh set, the operator-run prompt places its listed dollar orders, and `dca_fill_compare.py` later compares saved placement responses with fills.

## Freshness and slot tiers

`RecommendationStore.select_for` chooses the newest valid file for today's slot, stamped at or before current time and no older than configured `max_age_minutes` (15 minutes). At 12:07, a 12:00 recommendation is eligible; a stale or invalid newest file fails closed. The six stream slots use Normal tier; 15:30 escalates by the 3x-signal count (PLAN Rev 4).

## Files and commands

Recommendations live under `<cron root>/recommendations/<day>/`: `rec_*.json` is agent-facing, `expected_*.json` is program-only, and `placements/<slot>/` holds handed-set evidence, raw order responses and prior IDs. Never publish expected-price data.

- `python3 -m scripts.plan_first_slot begin --slot S [--now ISO]` selects and records the handed set.
- `python3 -m scripts.plan_first_slot filter --slot S --pages '<save_to>/orders_*.json' [--now ISO]` validates complete order history and applies the executor/ticker guards.
- `python3 -m scripts.plan_first_slot finish --slot S --started-at ISO [--now ISO]` hands placement evidence to Gate 9 finalization.
- `python3 -m scripts.snapshot_cycle fetch-plan` and `ingest --run-dir ...` support the courier.
- `fetch-plan` includes the configured account number in the arguments for the existing
  `get_equity_orders` and `get_portfolio` read tools. This is the same account exposure the live
  prompts already have when they call those tools. The program prints it only as part of those
  exact tool arguments; it is never written to disk by the fetch-plan command.

## Placement-time safeguards

- **Clock.** `plan_first_slot begin`, `filter` and `clock` each require the real clock to be
  inside BOTH the slot's placement window and the regular session; the session comes from
  Gate 0's calendar, so early closes count. The reader runs `clock` before every order, so a
  delayed task never trades after its window or after the close.
- **Tradability.** The reader calls `get_equity_tradability` before each order and skips a
  ticker that is not tradable for fractional buys. It never substitutes a quantity.
- **Evidence.** The reader saves `validate_native_placement check`'s verdict as
  `<TICKER>.check.json`. `finish` counts an order for Gate 9 only when that saved verdict is
  `ok` and names the same order id as the strictly parsed response.
- **Kill switch (backup poll 2).** The recommender stamps each set with `sizing_guard`
  (`max_multiplier`, `deployed_7d_usd`), the kill-switch state it was sized under. The payload
  is price-blind and carries no per-order multiplier, so the stamp stands in for it.
  `backup_planner finalize` re-derives the trailing-7-day figure from the fresh order history,
  minus the set's own orders (matched by native `ref_id`, so a partly placed batch cannot trip
  the switch on its own fills), and refuses a set stamped above 1x once the switch is engaged
  (`kill_switch_engaged_since_plan`). A set with no stamp, or a history that cannot prove the
  figure, fails closed (`sizing_guard_missing`, `history_unusable`). The stamp is optional on
  the wire, so an older payload still parses and is then refused at placement.
- **Run freshness.** `snapshot_cycle fetch-plan` writes `fetch_plan.json` (`planned_at`) into
  each run directory. `ingest` refuses a run with no marker, a marker from another day or
  the future, or one older than 10 minutes, and stamps everything with `planned_at` rather
  than the ingest clock. A late ingest therefore cannot make old data look fresh.

## Publishing to the `recommendations` branch (Mac launchd)

The Perplexity backups' cloud fallback reads the `recommendations` branch (`plan --source published_rec`,
`classify --published`). On 2026-10-26 that branch was a 404. Its only writer was the courier's own `ingest`, and the
courier runs in a scheduled Claude session, which holds no GitHub credentials (`docs/schedule_verification.md`,
confirmed 2026-10-21). The push failed every cycle, and the only trace was one `"ok": false` row in an exit-0 report.

The push now runs on the Mac under your own git login: `python -m scripts.snapshot_cycle publish --notify`, every
5 minutes from `deploy/launchd/com.all-stock-dca.recommendations-publish.plist.template`. It publishes today's newest
set per slot, whatever its age. `classify` needs today's set to recognise an order Claude placed from an older file,
and `published_rec` applies its own freshness rule. `ingest` no longer pushes the branch, so it is never written by
two jobs with different content.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | `OK` | Every enabled channel published |
| 0 | `UNCHANGED` | The same sets were already published; nothing pushed |
| 3 | `NONE` | No set for today yet, or no channel enabled in `config/recommendations.json` |
| 2 | `ERROR` | A channel failed. `--notify` raises a macOS notification; the next run retries |

Install it with `deploy/launchd/install_recommendations_publish.sh` from the checkout on the Mac. It resolves the
cron root the way every stream does (`$DCA_CRON_TRACKING_ROOT`, else `.env`, else `<checkout>/cron_tracking`;
`--cron-root DIR` overrides), refuses a root with no `recommendations/` store, checks that git can push from your
login (`git push --dry-run`), runs one `publish` by hand, then validates, unloads any old copy and loads the agent.
Re-run it to change the root; `--uninstall` removes it; `--dry-run` renders and checks without loading anything.

The same steps by hand, with the same root the courier writes the store under:

```bash
sed -e "s|REPLACE_WITH_REPO_PATH|$PWD|g" -e "s|REPLACE_WITH_CRON_TRACKING_ROOT|$DCA_CRON_TRACKING_ROOT|g" \
  deploy/launchd/com.all-stock-dca.recommendations-publish.plist.template \
  > ~/Library/LaunchAgents/com.all-stock-dca.recommendations-publish.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.recommendations-publish.plist
tail -n 5 /tmp/dca-recommendations-publish.log      # expect OK, UNCHANGED or NONE; never ERROR
git ls-remote origin refs/heads/recommendations     # one line once the first set of the day is published
```

## Rollout and limits

Run in shadow mode for at least five trading days, comparing recommendations with live slot buys. Then the operator repastes the seven reader prompts. Spread is checked at compute time, not placement time; decision time may move up to 15 minutes; rollover reserve is not read (treated as 0, the conservative choice). Gate 9's budget record comes from the program-only `ExpectedPriceSet.budget` (the tier and caps the engine actually used), which `finish` forwards as `--budget-json`. `filter` re-checks the daily, weekly and order-count caps against the fresh order history the agent just saved and places nothing if the set no longer fits. Gate 6.5 binds logged decision prices only for buys made through this path; a repeat after a legacy-slot buy gets the gate's conservative $0 until the legacy slots are retired.
