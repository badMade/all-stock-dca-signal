# ADR: Program-maintained ticker universe (supersedes item 8 of the dynamic-universe ADR)

- **Status:** Proposed (2026-11-19). Nothing in this ADR is implemented. It needs operator
  acceptance before any code is written.
- **Supersedes:** item 8 ("Suggest-only boundary") of `docs/adr-dynamic-universe.md`. Every other
  item of that ADR stays in force unless this one says otherwise.
- **Scope:** who writes `config/tickers.json`, how the executor reads it, how the 122 current names
  are ranked and cut to the top 100 for buys, and which checks stop a bad write. The graded ladder, budgets
  and the "never sell" rule are untouched.

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
   constants are removed as an independent copy: `near_close_dca.ACTIVE_TICKERS` and
   `EXCLUDED_TICKERS` are derived from the JSON at import, as `dca_runner.py` already does for its
   whitelist. The two parity tests are rewritten to assert derivation. This ships as its own PR,
   separate from the writer, because it touches the live 3:30 PM path.
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
   limit and 75/150 buffers do not apply. The 90-to-100 size check does not apply to the first
   run only. Demotion means new buys stop. Shares are never sold. A name can leave and re-enter
   as its rank moves, so boundary churn is open question 2.
4. **One gated writer.** A single function is the only code allowed to write the file. It must:
   validate against `schemas/tickers.schema.json`; reject duplicates and names in `excluded`;
   refuse a proposal whose content hash does not match an approval record; enforce the cap, the
   90-name floor (the first run excepted, item 3) and a bound on names changed in one day (open
   question 7); write to a temp file and rename it atomically; append that day's active set to
   `config/ticker_history.csv` in the existing `date,ticker` format when it changed; and record
   the source (`program` or `operator`) and approval ID in `universe_proposal.json` and the
   `universe_membership` table. Any failed check keeps the last approved list. The dynamic-universe
   ADR's fail-closed input checks (pool coverage, stale quotes, missing factors) run on every
   daily proposal.
5. **Approval.** A daily operator approval is not workable, so the proposed default changes the
   dynamic-universe ADR's operator-only rule. The program applies a daily proposal by itself when
   every gate passes and the change is within the daily bound, and the approval ID then records
   the standing operator approval of this ADR plus the proposal hash. A proposal outside the
   bound, a failed gate, or any change to the scoring itself needs the operator. No approval means
   the last approved list stays in force. A program write with no approval ID is rejected.
6. **When a change takes effect.** The daily job runs after the prior close and finishes before
   the earliest slot (9:35 ET), then writes once per trading day. The executor reads the list at
   process start. A write never lands mid-run. Runs already in progress keep the list they
   started with.
7. **Operator edits stay possible.** A hand edit remains allowed (urgent removals, for example a
   name the broker cannot find). It must record a `source=operator` entry in `universe_membership`.
   A drift check flags any JSON change with no matching entry. The next daily run may reverse a
   hand edit unless the name is added to `excluded`.
8. **Hand-held fields stay hand-held.** `excluded`, `whole_share`, `symbol_corrections` and the
   free-text notes are not written by the program in this ADR.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Does the 0935 live run read this repo's working copy or a separate checkout (`DCA_CONFIG_DIR`)? | Must be answered before implementation. If separate, the writer needs a delivery step to that copy |
| 2 | With daily re-ranking, is membership the strict top 100 each day, or does a held name keep its place until it falls below a lower rank (a buffer)? | Strict top 100, as you stated. The report lists daily entries and exits so you can see boundary churn and add a buffer later |
| 3 | Where do ranks 101 and below live in `tickers.json`? They stay ranked but are not considered for buys | Move to a `bench` key outside `active`, so they keep a rank and history |
| 4 | Where does the rank for each ticker get stored for reporting? | `universe_proposal.json` plus the DuckDB `universe_scores` table from the dynamic-universe ADR |
| 5 | Who maintains `tickers.txt`, and how often does it refresh? The pool is only as current as that file | Read-only input. A stale or shrunken file (for example under 95% of the prior run's pool) rejects the proposal, as in the dynamic-universe ADR |
| 6 | Are ranks taken among buy-eligible names only, or among all names? | Among buy-eligible names; ineligible names are listed with a reason |
| 7 | What is the bound on names changed in one day before a proposal needs the operator, and may the program apply within-bound proposals without a per-day approval? | Bound of 8 names per day (reusing the old swap size) and yes to auto-apply, with the operator needed above the bound. Both are changes from the dynamic-universe ADR |

## Consequences

- Fewer hidden edits: every list change carries a source and an approval ID. The list can now change every trading day, so a scoring bug reaches buying within a day. The daily bound and the fail-closed checks are the controls.
- 22 current names stop receiving new buys on the first approved run. Their existing shares stay. One ranking decides all 22, so the first ranking needs review before approval.
- Removing the hardcoded constant changes how bare `StrategyConfig()` paths get their list. It
  needs its own review and acceptance run.
- A program that can change what gets bought is a larger blast radius than a suggester. The
  approval gate, the cap, and the fail-closed checks are the controls.

## Verification

- Unit tests for the writer: duplicate, excluded name, over-cap, under-floor, over-swap-limit,
  bad hash, missing approval ID, and atomic-write interruption each leave the file unchanged.
- `tests/test_config_invariants.py` passes after the constants are derived.
- A dry run produces a proposal that ranks all 122 current names and lists the 22 names that would stop receiving buys,
  with no change to `config/tickers.json`.
- A report compares rank with realized performance for ranks 1 to 100 and for 101 and lower, so a
  poorly ranked name that performs well is visible.

## Rollback

Revert the writer PR. The file returns to hand maintenance with no other change. The derived
constants can stay, since they read the same JSON.
