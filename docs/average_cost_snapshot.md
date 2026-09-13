# Weekly average-cost-basis snapshot

`dca_average_cost_snapshot.py` builds one consolidated view of every stock
held across **all** Robinhood accounts on the account — per-account average
cost basis, plus a combined, quantity-weighted average cost per ticker for any
stock held in more than one account.

## Why this exists

Robinhood tracks average cost per position, per account, but there is no
single place that answers "what's my average cost on this ticker across
every account I hold it in?" This module builds that place, from data an
external caller already fetched.

## Why it takes data in rather than fetching it

Per `CLAUDE.md`'s "Standard library only at runtime" / "library driven from
outside" rules, this repo makes no network calls and holds no credentials.
Average cost basis is Robinhood-only data, reachable only through the
Robinhood MCP tools an external agent session holds — this module has no way
to call them itself. Like `dca_weekly_dashboard.py`, it is **connector-
agnostic**: an external agent session does the fetching
(`get_accounts` for the account list, `get_equity_positions(account_number)`
per account for that account's holdings) and calls this module's two
functions with the results. There is no in-repo caller and no CLI entry
point — see "Three entry points coexist" in `CLAUDE.md` for why that is
normal for a module in this category, alongside `dca_cost_tracker.py` and
`dca_weekly_dashboard.py`.

## Usage (from the calling agent session)

```python
import dca_paths
from dca_average_cost_snapshot import build_average_cost_snapshot, write_average_cost_snapshot

accounts = ...              # raw list from get_accounts()["data"]["accounts"]
positions_by_account = {     # account_number -> raw get_equity_positions() rows
    "999000111": [...],
    "123456789": [...],
    ...
}

snapshot = build_average_cost_snapshot(
    accounts, positions_by_account, generated_at="2026-08-18T09:00:00-04:00"
)
write_average_cost_snapshot(snapshot, output_dir=dca_paths.stream_root("average_cost_snapshot"))
```

The account numbers above are the repository's approved synthetic fixture
values; callers supply real identifiers only at runtime and must never commit
them to source, tests, documentation, or configuration.

`generated_at` is supplied by the caller (not read from the system clock
inside this module), matching this repo's usual "now is an explicit input"
convention (`run_dca --now`, etc.) so the output is deterministic and
testable.

## Output

One file, `average_cost_snapshot.json`, written atomically
(temp file + `os.replace`, via `dca_fileio.atomic_open_w`) to the given
`output_dir`:

```json
{
  "generated_at": "2026-08-18T09:00:00-04:00",
  "account_count": 2,
  "accounts": {
    "999000111": {
      "nickname": null,
      "brokerage_account_type": "individual",
      "type": "margin",
      "is_default": true,
      "positions": [
        {"ticker": "AAPL", "quantity": 2.0, "average_cost_usd": 100.0}
      ]
    },
    "123456789": {
      "nickname": "Agentic",
      "brokerage_account_type": "individual",
      "type": "cash",
      "is_default": false,
      "positions": [
        {"ticker": "AAPL", "quantity": 1.0, "average_cost_usd": 130.0}
      ]
    }
  },
  "combined_by_ticker": {
    "AAPL": {"total_quantity": 3.0, "average_cost_usd": 110.0, "account_count": 2}
  }
}
```

`combined_by_ticker`'s `average_cost_usd` is quantity-weighted across
accounts (`sum(quantity * average_cost) / sum(quantity)`), matching
`scripts/purchase_history.py`'s `build_ticker_rollup` convention — this is an
independent cross-check of that ledger's own bought-quantity average cost,
not the same computation: this one reads Robinhood's own live average cost
basis, the other derives it from this repo's own recorded fills.

## Handling of edge cases

* An account with no entry in `positions_by_account` (never fetched, or the
  fetch failed) is omitted from `accounts` entirely — never recorded as zero
  holdings, which would be indistinguishable from a genuinely empty account.
* A position with an unresolvable or non-positive quantity (a fully-closed
  position some responses still list) is skipped — it contributes to neither
  the per-account listing nor the combined rollup.
* A position with a resolvable quantity but no resolvable average cost is
  still listed per-account (so the holding itself is visible) and still
  counted in `combined_by_ticker`'s `total_quantity`/`account_count` (it is
  real, held quantity), but excluded from the numerator/denominator behind
  `average_cost_usd` — an unpriced holding's quantity would otherwise deflate
  the weighted average cost for that ticker, the same "exclude unpaired
  amounts" rule `purchase_history.py` applies to its own rollup.
  `average_cost_usd` is `null` (never a fabricated `0.0`) when NO account's
  holding of that ticker resolved a price.
* A non-finite parsed number (`"NaN"`, `"Infinity"`, `"-Infinity"` — all
  valid input to Python's `float()`) is treated as unresolvable, the same as
  an unparseable one: a NaN quantity would otherwise slip past the `<= 0`
  check, and an infinite cost would poison the weighted-average sum and make
  the JSON writer emit non-standard `NaN`/`Infinity` tokens.

## Weekly cadence

This module itself has no scheduler — it is a pure function of the data it's
handed. The weekly cadence is implemented by whatever external agent session
holds the Robinhood MCP connection: fetch `get_accounts` + `get_equity_positions`
per account, call `build_average_cost_snapshot` / `write_average_cost_snapshot`,
once a week.

## Limitations

* This is a point-in-time snapshot of Robinhood's own reported average cost
  basis — it does not independently verify it, and it is not a substitute
  for the broker's tax-lot records (`get_equity_tax_lots`) when lot-level
  detail (acquisition date, long/short-term status) is needed.
* Only equity positions are covered — no options, no crypto.
* `write_average_cost_snapshot` does a full replace every call, not an
  incremental merge; a weekly cadence makes a full rebuild cheap and this
  can never drift from a partially-applied update.
