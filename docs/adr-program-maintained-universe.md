# ADR: Program-maintained ticker universe (supersedes item 8 of the dynamic-universe ADR)

- **Status:** In progress, operator-directed 2026-11-19. Implemented so far: the gated writer
  (`ticker-advisor/universe_apply.py`), the derived `ACTIVE_TICKERS`, and the strict daily top-100
  engine mode. Not built: the data feed for ranking, the `launchd` job on the Mac, and the call
  sites. Nothing yet applies a proposal to the live list. Open question 9 is unanswered.
- **Supersedes:** item 8 ("Suggest-only boundary") of `docs/adr-dynamic-universe.md`. Every other
  item of that ADR stays in force unless this one says otherwise.
- **Scope:** who writes `config/tickers.json`, how the executor reads it, how the 122 current names
  are ranked and cut to the top 100 for buys, and which checks stop a bad write. The graded ladder, budgets
  and the "never sell" rule are untouched. Selling is out of scope (item 8).

## Context

`config/tickers.json` is maintained by hand. Verified in the repo on 2026-11-19:

- No code writes its `active` key. `ticker-advisor/` only proposes diffs.
- Two copies of the list exist: the JSON (read by `dca_runner.py` at import) and a hardcoded
  `near_close_dca.ACTIVE_TICKERS` (with `EXCLUDED_TICKERS`). `tests/test_config_invariants.py`
  (`test_active_tickers_match_config_tickers_json`, `test_excluded_tickers_match_config_tickers_json`)
  fails if they differ, so a program that edits only the JSON breaks CI.
- The file has 122 active names. The dynamic-universe ADR caps the active set at 100 and still
  describes a 91-name starting list, so its own fail-closed rule would reject today's file.
- `config/ticker_history.csv` records the roster by date (122 rows from 2026-11-06) but not who
  or what changed it.

The operator wants the list maintained by the program, with every ticker ranked.

## Decision (proposed)

1. **Single source of truth.** `config/tickers.json` stays the one approved list. The code
   constants are removed as an independent copy: `near_close_dca.ACTIVE_TICKERS` is derived from
   the JSON at import, as `dca_runner.py` already does for its whitelist, and an unreadable or
   malformed file refuses the import (implemented 2026-11-19 in `near_close_dca._load_active_tickers`,
   tests in `tests/test_active_tickers_derivation.py`). `EXCLUDED_TICKERS` stays a hardcoded copy
   with its parity test, because it is hand-held and the program never writes it. This touches the
   live 3:30 PM path, so it needs the acceptance run listed in Consequences.
2. **Rank everything, buy only the top 100.** Every ticker in the current roster, plus the
   candidate pool and bench defined in the dynamic-universe ADR, gets a published rank in
   `universe_proposal.json` (operator-confirmed 2026-11-19: all tickers ranked, only the top 100
   considered for buys). Rank decides buy eligibility: ranks 1 to 100 are considered for buys,
   and every name ranked 101 or lower is ranked and tracked but not considered for buys.
   The tracking exists to evaluate the score. A name that ranks poorly but performs well is
   evidence the scoring needs adjusting, so every ranked name keeps a dated rank history and a
   counterfactual record (what the same buy rules would have bought, using the existing
   `universe_scores` and counterfactual tables of the dynamic-universe ADR). Each review reports
   rank against realized performance for ranks 1 to 100 and for 101 and lower. Any scoring change
   that follows is a new variant recorded in `trials` and goes through operator approval like a
   list change. Tracking never places an order.
   **The ranked pool is every symbol in `tickers.txt`, not just the 122.** `tickers.txt` holds
   5,502 well-formed symbols (checked 2026-11-19), but 18 of today's 122 active names are not in
   it (AAAU, CBRS, DFTX, DRAM, DXYZ, FLKR, GFUZ, MRSH, QQQ, QTOP, RVI, SATA, SPCX, TOPT, TRLV,
   VCX, VT, XLG). On 2026-11-19 the operator directed that these 18 be added, so this PR adds
   them to `tickers.txt` (5,502 to 5,520 symbols, sorted order kept). The pool is still defined as
   the union of `tickers.txt` and the current `active`, `excluded` and `whole_share` lists, so a
   future active addition cannot go unranked before `tickers.txt` catches up. `tickers.txt` is
   protected operational data (`CLAUDE.md`, "Do not modify"). Apart from that operator-directed
   one-time addition, the program only reads it. This
   replaces item 1 of the dynamic-universe ADR as a filter on who gets ranked. Its tests (price,
   market cap, dollar volume, fractional eligibility, history length) become per-name eligibility
   flags with a reason. A name that fails a flag, or lacks the data to be scored, is still listed
   with its status and reason and is never silently dropped. Ranks 1 to 100 for buys are taken
   among names that pass every flag. A name that cannot be scored shows as `unscored`, not as a
   low rank.
3. **Cap of 100, applied at once and recalculated daily.** The active set is at most 100. The
   program rescores and re-ranks every day (operator-directed 2026-11-19), and the buy-eligible
   set is that day's ranks 1 to 100. There is no phase-in: on the first approved run the 22
   lowest-ranked of the current 122 stop receiving new buys immediately. The daily recalculation
   replaces the quarterly review of the dynamic-universe ADR, so its quarterly cadence, 8-swap
   limit and 75/150 buffers do not apply. There is no limit on how many names change in one day
   (operator-directed 2026-11-19): as many names enter or leave as the ranking requires. Demotion
   means new buys stop. Shares are never sold. A name can leave and re-enter
   as its rank moves, so boundary churn is open question 2.
4. **One gated writer.** A single function is the only code allowed to write the file. It must:
   validate against `schemas/tickers.schema.json`; reject duplicates and names in `excluded`;
   refuse a proposal whose content hash does not match an approval record; enforce the cap of 100 and
   the 90-name floor; apply no limit on the number of names changed; write to a temp file and rename it atomically; append that day's active set to
   `config/ticker_history.csv` in the existing `date,ticker` format when it changed; and record
   the source (`program` or `operator`) and approval ID in `universe_proposal.json` and the
   `universe_membership` table. Any failed check keeps the last approved list. The dynamic-universe
   ADR's fail-closed input checks (pool coverage, stale quotes, missing factors) run on every
   daily proposal.
5. **Approval.** A daily operator approval is not workable, so this ADR changes the
   dynamic-universe ADR's operator-only rule (operator confirmed auto-apply, 2026-11-19). The
   program applies a daily proposal by itself when every gate passes, and the approval ID then
   records the standing operator approval of this ADR plus the proposal hash. A failed gate, or
   any change to the scoring itself, needs the operator. No approval means the last approved list
   stays in force. A program write with no approval ID is rejected.
6. **When a change takes effect.** The daily job runs after the prior close and finishes before
   the earliest slot (9:35 ET), then writes once per trading day. The executor reads the list at
   process start. A write never lands mid-run. Runs already in progress keep the list they
   started with.
7. **Operator edits stay possible.** A hand edit remains allowed (urgent removals, for example a
   name the broker cannot find). It must record a `source=operator` entry in `universe_membership`.
   A drift check flags any JSON change with no matching entry. The next daily run may reverse a
   hand edit unless the name is added to `excluded`.
8. **This is a buy list, never a sell list (operator-directed 2026-11-19).** The `active` list
   and the daily ranks answer one question: which names may receive new buys. Falling out of the
   top 100, being unscored, or failing an eligibility flag stops new buys and does nothing else.
   No code may read this list, a rank, or a demotion as a reason to sell, trim or close a
   position. Selling is a later, separate function with its own ADR, and the operator has said it
   will be built as a separate unit following object-oriented design rules. That function will
   not share this writer, this file or this approval path. If it ever needs the ranks it reads
   them as an input from `universe_scores` and makes its own decision. Until that ADR is
   accepted, the "shares are never sold" rule stands, with only the exception already recorded in
   `docs/adr-tax-loss-sale.md`.
9. **Hand-held fields stay hand-held.** `excluded`, `whole_share`, `symbol_corrections` and the
   free-text notes are not written by the program in this ADR.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Resolved 2026-11-19 from `claude/dca-executor-prompts/live-mirror/0935ET-935-am-dca-morning-buy.txt` (Gate 2, "PREBUILT ROBINHOOD PAYLOAD"): the 0935 run reads the repo checkout on the operator's Mac (canonical path `/Users/geoffreyplymale/Documents/Git/all-stock-dca`, exposed to the agent sandbox under `/sessions/*/mnt/`), requires exactly one checkout, and treats it as the only source of truth for what to buy. It does not read a cloud session's copy. Other Mac jobs already run under `launchd` (`deploy/launchd/`) | The daily ranking-and-apply job runs on the Mac under `launchd`, in the same pattern as `com.all-stock-dca.octopus-daily-update`, and writes `config/tickers.json` in that checkout before 9:35 ET. No delivery step is needed. A write made from a cloud session reaches the live run only through git and a pull on the Mac |
| 2 | Resolved 2026-11-19: strict top 100 each day, no buffer, a name enters on the first day it ranks in the top 100 (`UniverseConfig.strict_daily_top`) | The report lists daily entries and exits so boundary churn is visible, and a buffer can be added later if the operator wants one |
| 3 | Where do ranks 101 and below live in `tickers.json`? They stay ranked but are not considered for buys | Move to a `bench` key outside `active`, so they keep a rank and history |
| 4 | Where does the rank for each ticker get stored for reporting? | `universe_proposal.json` plus the DuckDB `universe_scores` table from the dynamic-universe ADR |
| 5 | Who maintains `tickers.txt`, and how often does it refresh? The pool is only as current as that file | Read-only input. A stale or shrunken file (for example under 95% of the prior run's pool) rejects the proposal, as in the dynamic-universe ADR |
| 6 | Are ranks taken among buy-eligible names only, or among all names? | Among buy-eligible names; ineligible names are listed with a reason |
| 7 | Resolved 2026-11-19: no limit on names changed per day. The "8 per day" figure in earlier drafts was copied from the quarterly swap limit of the dynamic-universe ADR and was never set by the operator | None. The engine's `max_swaps` safeguard must be neutralized (open question 8) |
| 8 | Resolved 2026-11-19 and implemented: `UniverseConfig.strict_daily_top(100)` in `ticker-advisor/universe.py` gives one band (entry and keep rank 100), no tenure protection, sector and industry caps of 100 (so they never block a top-100 name), and `max_swaps` of `UNLIMITED_SWAPS`. A new setting `entry_requires_prior_review` (default true, so the quarterly mode is unchanged) is false there, so a name enters on the first day it ranks in the top 100, as the operator directed. The input safeguards (pool coverage, stale data, missing factors) and the 90 floor and 100 cap still apply | None. Sector and industry classification is still required for every add; names with no SIC classification are rejected as adds |
| 9 | Nothing in the repo shows how the Mac checkout is kept in sync with `main` (no `git pull` step in the live prompt or docs). A daily in-place write also leaves `config/tickers.json` modified against git on the Mac, which can block or conflict with a later pull. How should that work? | Unanswered. Needs the operator: how the Mac checkout is updated today, and whether the program-written list should live in a gitignored file the executor reads instead of the tracked `tickers.json` |

## Consequences

- Fewer hidden edits: every list change carries a source and an approval ID. The list can now change every trading day, so a scoring bug reaches buying within a day. The cap of 100, the 90-name floor and the fail-closed input checks are the controls, since the number of names changed is not limited.
- 22 current names stop receiving new buys on the first approved run. Their existing shares stay. One ranking decides all 22, so the first ranking needs review before approval.
- Removing the hardcoded constant changes how bare `StrategyConfig()` paths get their list. It
  needs its own review and acceptance run.
- A program that can change what gets bought is a larger blast radius than a suggester. The
  approval gate, the cap, and the fail-closed checks are the controls.

## Verification

- Unit tests for the writer (implemented in `ticker-advisor/universe_apply.py`, tests in `ticker-advisor/tests/test_universe_apply.py`): duplicate, excluded name, over-cap, under-floor,
  bad hash, missing approval ID, and atomic-write interruption each leave the file unchanged.
- `tests/test_config_invariants.py` passes after the constants are derived.
- A dry run produces a proposal that ranks all 122 current names and lists the 22 names that would stop receiving buys,
  with no change to `config/tickers.json`.
- A report compares rank with realized performance for ranks 1 to 100 and for 101 and lower, so a
  poorly ranked name that performs well is visible.

## Rollback

Revert the writer PR. The file returns to hand maintenance with no other change. The derived
constants can stay, since they read the same JSON.
