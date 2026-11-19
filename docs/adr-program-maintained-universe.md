# ADR: Program-maintained ticker universe (supersedes item 8 of the dynamic-universe ADR)

- **Status:** Proposed (2026-11-19). Nothing in this ADR is implemented. It needs operator
  acceptance before any code is written.
- **Supersedes:** item 8 ("Suggest-only boundary") of `docs/adr-dynamic-universe.md`. Every other
  item of that ADR stays in force unless this one says otherwise.
- **Scope:** who writes `config/tickers.json`, how the executor reads it, how the 122 current names
  are ranked and phased down to 100, and which checks stop a bad write. The graded ladder, budgets
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
   VCX, VT, XLG). The pool is therefore the union of `tickers.txt` and the current `active`,
   `excluded` and `whole_share` lists, so no current name goes unranked. `tickers.txt` is
   protected operational data (`CLAUDE.md`, "Do not modify") and the program only reads it. This
   replaces item 1 of the dynamic-universe ADR as a filter on who gets ranked. Its tests (price,
   market cap, dollar volume, fractional eligibility, history length) become per-name eligibility
   flags with a reason. A name that fails a flag, or lacks the data to be scored, is still listed
   with its status and reason and is never silently dropped. Ranks 1 to 100 for buys are taken
   among names that pass every flag. A name that cannot be scored shows as `unscored`, not as a
   low rank.
3. **Cap of 100 with a phase-in.** The active set is at most 100. The 22 lowest-ranked of the
   current 122 are demoted over three quarterly reviews (8, 8, then 6), using the dynamic-universe
   ADR's 8-swap limit. Demotion means new buys stop. Shares are never sold. Until the phase-in
   completes, the cap check allows a documented transition ceiling that falls each review: 114,
   106, then 100.
4. **One gated writer.** A single function is the only code allowed to write the file. It must:
   validate against `schemas/tickers.schema.json`; reject duplicates and names in `excluded`;
   refuse a proposal whose content hash does not match an approval record; enforce the cap, the
   90-name floor and the swap limit; write to a temp file and rename it atomically; and append one
   row per change to `config/ticker_history.csv` with source (`program` or `operator`) and
   approval ID. Any failed check keeps the last approved list.
5. **Approval owner.** Operator only, as in the dynamic-universe ADR's proposed default. No
   approval means the last approved list stays in force. A program write without an approval ID is
   rejected.
6. **When a change takes effect.** The executor reads the list at process start. A write never
   lands mid-run. Runs already in progress keep the list they started with.
7. **Operator edits stay possible.** A hand edit remains allowed (urgent removals, for example a
   name the broker cannot find). It must also append a `source=operator` row to
   `ticker_history.csv`. A drift check flags any JSON change with no matching history row.
8. **Hand-held fields stay hand-held.** `excluded`, `whole_share`, `symbol_corrections` and the
   free-text notes are not written by the program in this ADR.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Does the 0935 live run read this repo's working copy or a separate checkout (`DCA_CONFIG_DIR`)? | Must be answered before implementation. If separate, the writer needs a delivery step to that copy |
| 2 | Is the phase-in pace (8 per quarter) acceptable, or should the 22 be demoted at once? | 8 per quarter, to match the existing swap limit |
| 3 | Where do ranks 101 and below live in `tickers.json`? They stay ranked but are not considered for buys | Move to a `bench` key outside `active`, so they keep a rank and history |
| 4 | Where does the rank for each ticker get stored for reporting? | `universe_proposal.json` plus the DuckDB `universe_scores` table from the dynamic-universe ADR |
| 5 | Who maintains `tickers.txt`, and how often does it refresh? The pool is only as current as that file | Read-only input. A stale or shrunken file (for example under 95% of the prior run's pool) rejects the proposal, as in the dynamic-universe ADR |
| 6 | Are ranks taken among buy-eligible names only, or among all names? | Among buy-eligible names; ineligible names are listed with a reason |

## Consequences

- Fewer hidden edits: every list change carries a source and an approval ID.
- 22 current names stop receiving new buys over three quarters. Their existing shares stay.
- Removing the hardcoded constant changes how bare `StrategyConfig()` paths get their list. It
  needs its own review and acceptance run.
- A program that can change what gets bought is a larger blast radius than a suggester. The
  approval gate, the cap, and the fail-closed checks are the controls.

## Verification

- Unit tests for the writer: duplicate, excluded name, over-cap, under-floor, over-swap-limit,
  bad hash, missing approval ID, and atomic-write interruption each leave the file unchanged.
- `tests/test_config_invariants.py` passes after the constants are derived.
- A dry run produces a proposal that ranks all 122 current names and lists the 8 first demotions,
  with no change to `config/tickers.json`.
- A report compares rank with realized performance for ranks 1 to 100 and for 101 and lower, so a
  poorly ranked name that performs well is visible.

## Rollback

Revert the writer PR. The file returns to hand maintenance with no other change. The derived
constants can stay, since they read the same JSON.
