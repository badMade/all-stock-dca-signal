# Emergency stop: halting all live trading

**Status: written 2026-11-13 from what the repo and its docs establish. It has not been rehearsed.** Rehearse
it once (steps below, with a dry run) and put the date here. Nothing in this page changes runtime behaviour.

Read this when an order might be wrong, a cap might have been exceeded, or the data feed looks poisoned. The
goal is to stop new orders within minutes, from the outermost layer inwards, then investigate.

## Why there are several layers

Live execution authority is in scheduled agent sessions outside this repo
([`PRODUCTION_READINESS.md`](../PRODUCTION_READINESS.md) gap 1; [live_wiring.md](live_wiring.md)). So no single
code switch stops everything. Use the first layer that is available; do more than one if you are unsure.

## Layer 1: pause the scheduled tasks (stops the next fire)

1. In the scheduler that holds the live tasks, pause (do not delete) every task whose type is `trading`. The
   roster, their IDs and cron times are in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and
   [schedule_manifest.md](schedule_manifest.md).
2. Include the **backup** fires, not only the primaries. The backup pass can place orders on its own plan
   ([incident_2026-10-26_0935_unrecognized_orders.md](incident_2026-10-26_0935_unrecognized_orders.md)).
3. Pausing, not deleting, keeps the prompts so re-enabling is a status change
   ([schedule_manifest.md](schedule_manifest.md) records the 2026-10-31 pause done this way).

## Layer 2: make a live run fail closed (stops a fire that is already scheduled)

- **Unset `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`** in the environment the executor sessions inherit. A missing
  account number is a hard error, not an empty substitution ([`CLAUDE.md`](../CLAUDE.md) "Fail closed, always").
  Blank it and confirm with `python run_dca.py --demo` that a demo still works but a live run refuses.
- **Revoke the brokerage agent / MCP access** at Robinhood. This is the only layer that also stops a session that
  is already holding credentials.

## Layer 3: what the code already does on its own

- The 7-day deployment kill switch (`dca_risk.py`, `near_close_dca.kill_switch_engaged`) reduces or stops buying
  when the trailing deployed fraction crosses its threshold. It reads the `evidence` block in the risk report; a
  zero with no history loaded is not "nothing deployed" ([`PRODUCTION_READINESS.md`](../PRODUCTION_READINESS.md)).
- Live placement needs **two** switches, `StrategyConfig.dry_run=False` and `RobinhoodBroker.enable_live_orders=True`,
  and config files cannot set either ([live_wiring.md](live_wiring.md)). Any caller that leaves either at its
  default places nothing.
- The CLI (`run_dca.py`) refuses to run live at all.

## After the stop

1. Pull the broker's order history and compare it with the run artifacts for the last two trading days
   (`scripts/broker_reconciliation.py`).
2. Cancel any still-open orders in the brokerage app. This repo never places or cancels orders from the CLI.
3. Write an incident note in `docs/` (follow the `incident_*` files for the shape) before restarting.
4. Restart in reverse order: restore credentials, unpause one slot, watch it in a dry run first, then the rest.

## Rehearsal checklist (record the date and who ran it)

- [ ] Layer 1 on the real scheduler, then re-enable one task.
- [ ] Layer 2 on a scratch shell: confirm a live path refuses without the account number.
- [ ] Confirm the operator can reach the broker's revoke control without help.

Last rehearsed: **never** (as of 2026-11-13).
