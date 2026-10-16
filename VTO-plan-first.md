# VTO — Plan-first recommendations, thin buying agents

Cycle: `rocket-fuel/plan-first-recommendations` (started 2026-10-13). Build plan:
`PLAN-plan-first.md`. Same Page log: `SAME-PAGE-LOG-plan-first.md`. Backlog:
`DEFERRED-plan-first.md`. (The `PLAN.md`/`VTO.md` files at the repo root belong to the
earlier hosted-console cycle and are untouched.)

## What

A program on the operator's Mac that, every 5 minutes during market hours, pulls Robinhood
market and account data, runs the strategy's **existing** signal, ladder and cap logic, and
writes a small recommendation file: `[{ticker, usd, ref_id}]` for the slot it would serve.
The scheduled buying agents read only that file, place the buys, and save raw broker
responses. The program records the price it expected and later compares it to the fill.

## Why

- **Tokens.** The seven live slot prompts total about 250K tokens. Each one re-fetches
  bars and quotes for about 65 tickers through the model.
- **Fragility.** Prices, fractional quantities and multi-step arithmetic flowing through
  the model have caused run failures.
- **Visibility.** Today there is no live intraday data between slots. A 5-minute history
  gives one.

## Who

The operator, who owns the account and the scheduled tasks. The primary agent is a Claude
Cowork scheduled task; the backup is a Perplexity scheduled task.

## Success criteria (this cycle)

1. At each slot's decision time, the program's recommendation equals what the existing
   signal step plus `plan_from_decisions` produce on the same data (parity test).
2. The agent-facing file carries no price fields, only ticker, dollar amount and ref_id.
3. The freshness rule holds. The agent uses the newest file whose `as_of` is at or before
   now, on the same trading day, at most 15 minutes old (configurable). Otherwise the
   agent buys nothing and alerts.
4. The program can never place an order. A hard allowlist of read-only tools is enforced
   by tests.
5. Expected-vs-actual fill comparison works from saved placement responses.
6. A reader prompt draft is 2K tokens or fewer.

## Non-negotiables

- **Same rules, same math.** No changes to the ladder, caps, tickers or tiers. Reuse the
  existing functions.
- **No yfinance in this path.** The data source is headless Robinhood, falling back to a
  tiny Claude fetch task.
- **Credentials stay out of the repo.** Tokens live in the macOS Keychain and are never
  logged. The account number is still resolved through `dca_paths`.
- **Fail closed.** If the data is stale or missing, the program writes no recommendation
  and the agent buys nothing.
- **Standard library only** at runtime. New modules go into `pyproject.toml`. Paths come
  from `dca_paths`.
- **Live prompts stay as they are.** Only drafts are written; the operator repastes them
  after a shadow period of at least 5 trading days.
- **Pull requests are drafts.** Commits need the operator's approval.
