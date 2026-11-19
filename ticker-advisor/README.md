# ticker-advisor

Scores candidate tickers for the `all-stock-dca` universe against three independent signals, and
writes a ranked report plus a *proposed* diff for `config/tickers.json` / `near_close_dca.
ACTIVE_TICKERS` / `config/scheduled_tasks.json`'s `canonical_first_buy_and_topup_rule.
active_universe.active` list (that field mirrors `config/tickers.json`'s `active` array per its
own `source_of_truth` note, but nothing keeps the two in sync automatically -- this repo already
hit that exact drift once, see `config/scheduled_tasks.json`'s own `ticker_universe_update_2026_
08_04` note). It never edits any of the three files itself, never places an order, and never calls
Robinhood -- see "What this does NOT do" below.

This is a sibling of `production-safe-agent-loop/` and `trading-strategies/`: a self-contained,
heavy-dependency subproject with its own `pyproject.toml`/`requirements.txt`/tests, excluded from
the core repo's stdlib-only ruff and coverage gates (see the root `CLAUDE.md`).

## Install

```bash
cd ticker-advisor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`transformers`/`torch` (the sentiment signal) are a multi-GB install. Pass `--skip-sentiment` to
skip that dependency entirely and run on strategy-health + backtest signals only.

`python -m pytest tests/` never downloads the FinBERT model, even with `transformers`/`torch`
installed -- the one test that constructs a real `FinbertSentimentAnalyzer` is gated behind
`TICKER_ADVISOR_RUN_MODEL_DOWNLOAD_TESTS=1`, so the default suite stays runnable offline.

## Usage

```bash
python advise_tickers.py --candidates COIN PLTR SHOP
python advise_tickers.py --candidates-file candidates.txt --skip-sentiment
python advise_tickers.py --candidates COIN --weights health=2,backtest=1,sentiment=1
export NEWS_API_KEY=...  # optional -- without it, sentiment falls back to SEC filings only
export SEC_EDGAR_USER_AGENT="Your Org contact@your-domain.example"  # recommended -- see below
python advise_tickers.py --candidates COIN
```

`--candidates-file` is one symbol per line, `#`-prefixed lines ignored. `--weights` keys must be
exactly `health`/`backtest`/`sentiment` -- an unrecognized key (a typo) is a hard error rather
than a silently-ignored no-op.

SEC EDGAR requires a contact-identifying `User-Agent` header and can reject requests that don't
supply one; set `SEC_EDGAR_USER_AGENT` to your own `"Org Name contact@domain"` for reliable
access to the SEC-filing half of the sentiment signal (see
[sec.gov/os/webmaster-faq#developers](https://www.sec.gov/os/webmaster-faq#developers)).

Filing-document downloads retain at most **2 MiB of decompressed bytes**, read in 64 KiB
chunks; an oversized declared `Content-Length` is refused before reading. Missing or
incorrect length headers do not bypass the streamed byte limit. Redirects are refused
before Requests can buffer their intermediate bodies, and responses close on success or
failure. The existing SEC request throttle and request timeout still apply.

Excerpt preprocessing accepts at most **2 Mi characters** of raw document text, including
injected/offline document fetchers, and uses forward scans to remove markup and hidden
head/script/style/inline-XBRL content before selecting the usual 4,000-character excerpt.
These input limits allow over 500 times the excerpt budget for filing markup. Oversized
documents and redirects exclude only that filing; the advisory report records the fetch
failure while other filings and news remain usable. HTTP-declared text encodings are
respected, with UTF-8 as the fallback when no usable encoding is supplied.

Output lands in `analysis/<today>-ticker-advisory/<HHMMSS>/` (override with `--output-dir`) --
the time-of-day component keeps multiple same-day runs (different candidate batches, retried
with different `--weights`, etc.) from silently overwriting each other:

- `scores.json` -- every candidate's raw per-signal metrics, composite score, and classification.
  Non-finite values (NaN/inf, e.g. a metric that couldn't be computed) are serialized as `null`,
  not a bare `NaN` token, so strict JSON parsers can read the file.
- `scores.md` -- the same, as a ranked human-readable table.
- `proposed_tickers_diff.json` -- for ADD-classified candidates only, **after** filtering out any
  symbol already in `config/tickers.json`'s current `active` or `excluded` lists (reported
  separately as `skipped_already_active` / `skipped_explicitly_excluded`, since proposing either
  would violate the repo's own duplicate/disjointness invariants once applied): the literal
  snippets to splice into **all three** of `config/tickers.json`'s `active` array,
  `near_close_dca.ACTIVE_TICKERS`, and `config/scheduled_tasks.json`'s
  `canonical_first_buy_and_topup_rule.active_universe.active` list, in matching order across all
  three -- skipping the `config/scheduled_tasks.json` edit leaves the scheduled execution universe
  stale even after the other two files are correct, plus a broker-tradability reminder and a
  post-apply checklist.
  **Update 2026-11-19:** `near_close_dca.ACTIVE_TICKERS` is now derived from `config/tickers.json`
  at import, so it is no longer a splice target; only `config/tickers.json` and `config/scheduled_tasks.json`
  need the edit. The advisor's printed snippets and checklist still name all three until `advise_tickers.py`
  is updated (follow-up).

## The three signals

| Signal | Module | Dependency | What it measures |
| --- | --- | --- | --- |
| Strategy health | `signals.strategy_health_score` | pandas/numpy/scipy | Sharpe, Sortino, max drawdown, CAGR, Calmar, beta, alpha on the candidate's own price series |
| Backtested returns | `signals.backtest_score` | vectorbt | MA-crossover and RSI-reversion backtest performance |
| Sentiment | `signals.sentiment_score` | transformers/torch | FinBERT score over recent NewsAPI headlines (if `NEWS_API_KEY` is set) + SEC EDGAR filings (always available, no key needed) |

Each candidate's raw signals are combined in `scoring.py`: every individual raw metric (health's
Sharpe and Calmar; backtest's MA-crossover and RSI-reversion Sharpe; sentiment's score) is
z-scored **independently** across the current candidate batch -- so a metric with a naturally
larger scale (Calmar routinely reads 5-20+, Sharpe usually 0-2) can't silently dominate a blend of
raw values -- and only then averaged by weight into one composite score. A metric that failed,
wasn't run (e.g. `--skip-sentiment`), came back non-finite (NaN/inf), or has fewer than 2 usable
values across the whole batch to normalize against is **excluded** from that candidate's
composite, not defaulted to a neutral 0 or fed to the ADD/WATCH/REJECT bands as an un-normalized
raw number -- the gap is recorded in `TickerScore.rationale` instead.

One consequence: **scoring a single candidate at a time never classifies as ADD or REJECT** --
there's no batch to normalize against, so every signal gets excluded and the candidate reports as
WATCH with `composite: null`. Run `advise_tickers.py` against at least 2-3 candidates together for
a meaningful classification; `scores.json`'s raw per-signal metrics are still fully populated for
a single-candidate run, only the composite/classification are affected.

## What this does NOT do

- Does not touch `near_close_dca.py`, `graded_buy_multiplier()`, `select_first_allocation_base_usd()`,
  or any live sizing/budget math.
- Does not add or remove any ticker from `config/tickers.json` / `ACTIVE_TICKERS` -- it only
  writes a *proposed* diff for a human/agent to review and apply by hand.
- Does not verify broker tradability. This repo has no Robinhood/broker access; that check stays
  a manual/external-agent step, exactly as documented in `docs/strategy.md`'s "TICKER UNIVERSE
  UPDATE" changelog entries.
- Does not discover candidates on its own (no S&P 500 / sector-ETF scan). You supply the list.

## Research-only: dynamic 100-ticker universe selection (`universe.py`)

`universe.py` is a pure-stdlib engine that turns a scored candidate pool into a *proposed*
quarterly membership change. It is research-only: no network calls, no broker calls, no file
I/O except the explicit `write_proposal(path, proposal)` helper, and `universe.py` itself **never
edits `config/tickers.json`**, `near_close_dca.ACTIVE_TICKERS`, or `config/scheduled_tasks.json`.
The only code allowed to change the buy list is `universe_apply.apply_proposal` (see below). Nothing
in the repo calls either module yet, and neither is wired into live sizing or any scheduled task.

| Step | Function | Rule (spec default, NOT operator-approved) |
| --- | --- | --- |
| Pool | `filter_pool` | NMS-listed common stock, price > $5, cap > $2B, 60d median $vol > $20M, fractional-eligible, >= 252 sessions; every failing reason recorded |
| Hard fails | `detect_hard_fails` | delisted, fractional-ineligible, price < $5 for 20 sessions, pending acquisition |
| Scoring | `sector_zscores`, `winsorize`, `composite_scores` | momentum (6m/12m), gross profitability, low volatility, value; z-scored within sector, winsorized at +/-3, equal 0.25 weights; missing >= 2 factors = unscored |
| Selection | `select_universe` | enter in the top 75 at this AND the prior review; stay while in the top 150; 25/sector, 10/industry caps; max 100; 2-quarter tenure before a rank-based exit; max 8 rank-based swaps (hard-fail exits excluded); bench = ranks 101-250 |
| Safeguards | `validate_proposal`, `apply_safeguards` | reject the whole proposal and keep the last approved list on: pool coverage < 95% of prior, > 8 swaps, active < 90 or > 100, quotes > 3 sessions old, EDGAR > 7 days old, any factor > 10% missing, or absent evidence of any of these |
| Output | `proposal_to_dict`, `write_proposal` | `universe_proposal.json` with `review_id`, `as_of_date`, `generated_at`, per-ticker reasons, safeguard results, and a `content_sha256` that excludes `generated_at` |

Every threshold is a `UniverseConfig` / `PoolThresholds` field defaulting to the spec value. One
ticker with missing data is **held**, never demoted; an incumbent that genuinely fails a pool floor
(pass `ineligible=ineligible_tickers(pool)`) exits, using the swap budget. Hard-failed names are
removed before ranking, and stay removed even when a proposal is rejected. Ties break by ticker symbol, so output is
deterministic. A passing proposal is `pending_approval`, not approved; operator approval and any
executor wiring (reading an approved list instead of `config/tickers.json`) need their own ADR.
Tests: `python -m pytest tests/test_universe.py -q` (synthetic, hand-computed inputs only).

See `docs/ticker_advisory.md` for how this fits into the existing manual ticker-add workflow, and
its closing section on the (not-yet-built) idea of eventually letting a composite score influence
sizing, not just universe membership.

### Strict daily top-N mode

`UniverseConfig.strict_daily_top(100)` is the operator's daily rule (see the ADR): the buy-eligible set
is each day's ranks 1 to 100. One band (no 75/150 hysteresis), a name enters on the first day it ranks
inside it (`entry_requires_prior_review=False`) and leaves the day it falls out, nothing is
tenure-protected, the sector and industry caps are set to 100 so they never block a top-100 name, and
the number of names changed per day is unlimited (`UNLIMITED_SWAPS`). The input safeguards and the
90-name floor and 100-name cap are unchanged. The default `UniverseConfig()` keeps the quarterly
behaviour exactly as before.

### Pinned names

`config/tickers.json`'s optional `pinned` array lists hand-held names (ETFs, funds, filers with no us-gaap
facts) that cannot be scored. They stay in `active`, count toward the cap of 100, and are never ranked, added
or dropped by the program. Run `select_universe` on the non-pinned pool with
`UniverseConfig.strict_daily_top(100 - len(pinned))`, then `universe.apply_pinned(proposal, pinned)` BEFORE
`apply_safeguards`. `dca_config.load_tickers` validates `pinned` (a subset of `active`, canonical symbols,
no repeats, never excluded) and `universe_apply` refuses a proposal that drops a pinned name.

### Applying a proposal (`universe_apply.py`)

`universe_apply.apply_proposal(proposal_path, tickers_path, history_path, approval_id=..., applied_on=...)`
is the one gated writer for the **buy list**. It applies a proposal only when the proposal is
`pending_approval`, its `content_sha256` still matches its content, it was built on the `active`
list `tickers.json` holds right now, and an approval id is given. It also refuses a symbol that fails
the schema pattern, a duplicate, an `excluded` or `whole_share` name, a list over 100 or under 90.
There is no limit on how many names change in one day. Any refusal raises `ApplyRefused` (with a
stable `code`) and leaves both files byte-identical. Only the `active` array is rewritten, the rest of
the file is untouched, the write is atomic, and the new active set is appended to the history file.
A name that leaves `active` is reported as `stopped_buying`: it stops receiving new buys, and nothing
here sells. Tests: `tests/test_universe_apply.py`. Design: `docs/adr-program-maintained-universe.md`.
