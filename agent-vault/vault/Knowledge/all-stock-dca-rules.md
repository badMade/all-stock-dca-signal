# all-stock-dca rules

A pointer note. The source files win; if this note and a source disagree, the source is right and this note is stale.
Last checked against the files on 2026-10-03 by reading them (nothing was run).

## Where the rules live

| Topic | Source of truth |
| --- | --- |
| Broker, budget caps, circuit breakers, execution roster | `docs/safeguards.md`, `config/budgets.json` |
| Sizing ladder and per-ticker calibration | `docs/strategy.md` ("Sizing ladder"), `config/sizing_ladder.json`, `config/per_ticker_sizing.json` |
| Scheduled vs manual runs | `docs/run_classification.md`, `scripts/run_classification.py` |
| Entry points and what each one drives | `CLAUDE.md` (repo root), `docs/live_wiring.md` |

## Facts worth keeping in view (each cites its source)

- Robinhood is the only execution broker. Alpaca, Finance and Yahoo are read-only context.
  (`docs/safeguards.md`, "Broker / account"; `config/budgets.json`, `broker_roles`)
- The base amount is exactly $2.00, or $1.00 when only below the prior close. Never fall back to $50.
  (`config/budgets.json`, `base_amount_policy`)
- Sizing is a ladder on the drop versus the prior close: below 0.1% buys nothing, 0.1% to under 2% is 1x, and
  8% or more is 5x. That table is the fallback; per-ticker calibrated tiers govern live sizing.
  (`docs/strategy.md`, "Sizing ladder (latest, active)")
- Circuit breakers: $10 maximum per fractional order, 3 consecutive placement failures stop a batch, 120 orders per
  day across all slots, whitelist only, one order per ticker per stream. (`config/budgets.json`, `circuit_breakers`)
- No sells, options, crypto, margin changes or order cancellations unless explicitly authorized.
  (`config/budgets.json`, `safety_policy`)
- The CLI is dry-run only. `run_dca.main()` refuses a live config, `--slot`, or a missing `--demo`.
  (`CLAUDE.md`, "Hard invariants")

## How a vault session runs it

`python run_dca.py --demo --report --now <ISO time> --artifacts-dir <dir outside the checkout>`, from the repo
root. Same `--now` and same code must give identical artifacts; the `execute-dca` skill checks that on request.

## Prior results

None yet. The first dry-run log lands in `Daily/`, and its durable findings come back here via `capture`.

## Open questions

- Whether the brief's "max slip 5bp, min 50bp between entry and exit" has any basis in this strategy. No repo
  document states it, so it is not adopted. See `threshold-review.md`.
