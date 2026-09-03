# Whole-share accumulation

Implemented in `dca_whole_share_accumulator.py`. **Wired into the canonical live path**
(`near_close_dca._run_once_impl` / `run_dca.run_slot()`/`run_pipeline()` — the 3:30 PM
final-buy stream) — see "Scope" below for exactly which stream(s) that covers and which
it does not.

## The idea

BYDDY (see `config/tickers.json`'s `whole_share_reasons.BYDDY`) is not tradable for
fractional/notional buys on Robinhood — Robinhood does not resolve it as
`fractional_tradability: "tradable"`. The normal graded-ladder buy is a $1–$10
dollar-amount ("notional") order, so for BYDDY it would always be blocked at
`near_close_dca`'s `is_tradable_for_fractional_buy` gate before it ever reached the
broker.

Rather than sitting in a do-not-buy exclusion list, BYDDY lives in its own ticker
category — `near_close_dca.WHOLE_SHARE_TICKERS`, mirrored in `config/tickers.json`'s
`"whole_share"` array — **distinct from both `active` and `excluded`**:

- `active` (`ACTIVE_TICKERS`): the normal fractional/notional ladder.
- `excluded` (`EXCLUDED_TICKERS`): no buying mechanism at all (e.g. a delisting).
- `whole_share` (`WHOLE_SHARE_TICKERS`): evaluated by the exact same graded ladder as
  `active` — it appears in `signals.csv` "along with the regular buys" — but any
  resulting BUY decision is routed through this module instead of a normal
  dollar-amount order.

This module tracks what the graded ladder *would have bought* each day for a
whole-share ticker (a "virtual" contribution — the SAME `ReconciledDecision.suggested_amount_usd`
every active ticker's signal already computes, via
`near_close_dca.select_first_allocation_base_usd` + `graded_buy_multiplier`) and
accumulates it in a durable, cross-day ledger. Once the accumulated total reaches the
price of one share, it fires a single **whole-share** purchase (a quantity-based order,
not a dollar-amount one) instead of a fractional buy.

Worked example: BYDDY trades around $10. Over several days the ladder would have bought
$3, then $4, then $5 (contributions accumulate: $3 → $7 → $12). The third day's
contribution crosses $10, so the ledger fires **1 whole share** at $10.00 and carries
the remaining $2.00 forward toward the next share.

A single large contribution (or a deep drop) can cross more than one share's price at
once — the ledger fires that many whole shares in one decision and carries any leftover
fraction of a share's price forward, mirroring `dca_cumulative.py`'s "apply once, carry
the remainder" shape rather than `dca_inverse_martingale.py`'s multi-run capped-fire
shape.

## Enrolling a ticker

Add the symbol to `config/tickers.json`'s `"whole_share"` array (and to
`near_close_dca.WHOLE_SHARE_TICKERS`, its hardcoded-default counterpart —
`test_config_invariants.py::test_whole_share_tickers_match_config_tickers_json`
enforces the two stay in lockstep, exactly like `active`/`ACTIVE_TICKERS` and
`excluded`/`EXCLUDED_TICKERS`), and a `config/per_ticker_sizing.json` entry — see
"Budget-tier planning"/`scripts/generate_per_ticker_sizing.py` below. A symbol must
never appear in more than one of `active`/`excluded`/`whole_share` —
`test_whole_share_tickers_are_disjoint_from_active_and_excluded` enforces that for the
bundled config, and `dca_config.load_tickers` itself raises `ValueError` for ANY
`$DCA_CONFIG_DIR` whose `tickers.json` violates it (Codex review: "Reject overlapping
ticker categories at config load") — `near_close_dca._process_buy_candidate`'s dispatch
(`if ticker in ctx.config.whole_share_tickers: ...`) has no defined behavior for a
ticker that is simultaneously "active" and "whole_share", and evaluating it twice in one
run can silently overwrite one copy's `candidate_outcomes` entry with the other's. The
overlap check compares symbols case-INsensitively (e.g. active `"AAPL"` and whole_share
`"aapl"` are rejected as the same collision) even though each category is otherwise
stored verbatim — every consumer downstream normalizes a ticker before use, so a
case-only mismatch is still the exact same real-money collision (Codex review:
"Normalize symbols before checking category overlap").

`dca_config.load_tickers` also rejects a present-but-non-list `"whole_share"` value
outright (e.g. `"whole_share": "BYDDY"`) rather than silently ignoring it the way
`"active"`/`"excluded"` do — silently skipping it would leave `whole_share_tickers` at
`StrategyConfig`'s empty-set default, so the afternoon run starts successfully but never
evaluates or accumulates the intended whole-share ticker, with no configuration error
explaining the omission. Mirrors the same validation the whole-share-aware maintenance
and ML loaders (`ticker-advisor/advise_tickers.py`, `ml-forecaster/train.py`,
`dca_market_graph.py`) already apply to this same field (Codex review: "Reject a
malformed whole-share collection at config load").

`dca_config.load_strategy_config` also fills in a `"whole_share_tickers"` default when a
caller passes no explicit `base` AND the config's own `load_tickers` merge never declared
its own `"whole_share_tickers"` key (an absent `"whole_share"` field, or a genuinely
missing config directory) — mirroring how `StrategyConfig()`'s own class defaults already
give `tickers`/`excluded_tickers` their real `ACTIVE_TICKERS`/`EXCLUDED_TICKERS` values.
`StrategyConfig`'s bare class default deliberately keeps `whole_share_tickers` empty (see
that field's own docstring — a non-empty class default would silently evaluate BYDDY
against every bare `StrategyConfig(tickers=(...))` construction throughout the test
suite), but that safety consideration doesn't apply to `load_strategy_config`'s own
implicit default — a caller who passed no `base` at all should get the SAME
real-membership fallback the active/excluded universes already get. Without this, the
documented installed-wheel/missing-config-dir fallback (the "config dir not found"
warning) — or simply a `tickers.json` that omits the optional `"whole_share"` key,
mirroring how an omitted `"excluded"` key already falls back this way — silently stopped
evaluating and accumulating BYDDY the moment config was unavailable or didn't declare it,
while `tickers`/`excluded_tickers` kept their real hardcoded fallback (Codex review:
"Preserve whole-share defaults when config is absent"). A caller that DOES pass an
explicit `base` (e.g. a test isolating a small ad hoc universe), or whose config
explicitly declares its own `"whole_share"` list, is unaffected — only the implicit,
config-silent default fills in.

That implicit default is computed as `WHOLE_SHARE_TICKERS` MINUS this config's own FINAL
`excluded_tickers` — checked in PRECEDENCE ORDER, exactly matching
`dataclasses.replace(base, **merged)`'s own layering: the caller's own `excluded_tickers=`
keyword override first (the highest-precedence layer, applied via `merged.update(overrides)`
AFTER this default is computed), then the JSON's `"excluded"` if it declared one, else
`base`'s own default — never applied blindly. A supported legacy/alternate config that
explicitly excludes BYDDY (`{"active": [...], "excluded": ["BYDDY"]}`, never mentioning
`"whole_share"` at all — an intentional, safer do-not-buy declaration) would otherwise
have this implicit default inject BYDDY into `whole_share_tickers` anyway, overlapping the
explicit `excluded_tickers` and failing the disjointness validation just below for a
config that never asked for BYDDY to be evaluated in ANY category (Codex review: "Honor
explicit exclusions when applying whole-share defaults"). The SAME hazard applies to a
caller-supplied `excluded_tickers=` override specifically — checking only `merged`/`base`
(the lower-precedence layers) missed it entirely: a caller passing
`load_strategy_config(dir, excluded_tickers=frozenset({"BYDDY"}))` against a `tickers.json`
that omits `"whole_share"` still got BYDDY injected into `whole_share_tickers`, which the
later `merged.update(overrides)` call cannot retroactively undo, tripping the
disjointness validation for a caller explicitly trying to exclude BYDDY via the
documented override (Codex review: "Apply exclusion overrides before deriving
whole-share defaults").

`dca_config.load_tickers` also validates every `"whole_share"` entry against the same
canonical `^[A-Z.]{1,6}$` shape `schemas/tickers.schema.json` already enforces for the
bundled config and `dca_whole_share_accumulator.py`'s own `_SYMBOL_PATTERN` validates
before ever using a ticker as a filesystem path component — a supported alternate
`$DCA_CONFIG_DIR` is not schema-validated at all, so a noncanonical entry like `"byddy"`
would otherwise pass straight through into `StrategyConfig.whole_share_tickers` verbatim,
and the afternoon evaluator would request market data using that lowercase value while
`RobinhoodDataProvider`'s own quote/bar lookups compare the broker's canonical UPPERCASE
response symbol exactly — so the configured ticker would silently receive no Robinhood
data and never reach accumulation or placement at all (Codex review: "Reject
noncanonical whole-share symbols at config load"). Since every valid `"whole_share"`
entry is now canonical-uppercase-only, `load_tickers`'s OWN case-folded-duplicate-within-
`whole_share` check just below (`["BYDDY", "byddy"]`) has become unreachable through
config JSON — a lowercase spelling is rejected by the symbol check first, before ever
reaching the duplicate comparison — but the check itself is left in place as harmless
defense-in-depth rather than removed, since `active`/`excluded` don't need an equivalent
check here: `scripts/generate_buy_candidates.py`'s own downstream non-canonical-ticker
validation already catches a lower-case entry in those two categories before it becomes
a duplicate-count concern (Codex review: "Reject case-folded duplicates within each
ticker category").

Both of `load_tickers`'s checks above only see the categories a SINGLE `tickers.json`
explicitly declares. `dca_config.load_strategy_config` re-validates BOTH after merging
`base`/JSON/explicit overrides into the final `StrategyConfig` (`dataclasses.replace(base,
**merged)`), using the final resolved `tickers`/`excluded_tickers`/`whole_share_tickers`:
a partial config declaring only `"whole_share": ["aapl"]` passes `load_tickers`'s own
overlap check (its own active/excluded sets are empty), but the merge never touches
`base.tickers`, whose default `ACTIVE_TICKERS` already contains `"AAPL"` — the final
config then has the same instrument in both categories (Codex review: "Validate the final
overlaid ticker partition"). Likewise, a case-folded duplicate supplied directly through
`base` or an explicit `whole_share_tickers=` keyword override bypasses `tickers.json`
entirely, so `load_tickers`'s own within-category check never runs against it — both
spellings survive as distinct frozenset entries until the final validation's own
case-insensitive normalization would otherwise silently collapse them (Codex review:
"Reject case-folded duplicates after applying overlays").

`dca_config.load_tickers` overlays `whole_share_tickers` onto `StrategyConfig` from
`tickers.json`'s `"whole_share"` array exactly like `tickers`/`excluded_tickers` are
overlaid from `"active"`/`"excluded"`. `StrategyConfig.whole_share_tickers` deliberately
does **not** default to `WHOLE_SHARE_TICKERS` at the dataclass level, though — see the
field's own comment in `near_close_dca.py`: `excluded_tickers` only ever narrows
whatever `tickers` a caller passes, so a test/caller building `StrategyConfig(tickers=(...))`
with a small ad hoc universe is unaffected by whatever it still defaults to.
`whole_share_tickers` *widens* the evaluated universe instead (see "Scope" below), so
defaulting it to a non-empty set would silently evaluate BYDDY (against a
`DryRunBroker`/fake-data instance the caller never provisioned for it) in hundreds of
existing tests that build a bare `StrategyConfig()`. Real live runs still get the real
membership from `config/tickers.json` via `dca_config.load_strategy_config`.

A whole-share ticker also needs a `config/per_ticker_sizing.json` entry, exactly like
every active ticker — it is evaluated by the SAME `select_first_allocation_base_usd`,
which falls back to `StrategyConfig.base_buy_amount_usd`'s **$2** default when a symbol
has no entry, while every entry in that file deliberately floors both base amounts at
**$1**. Enrolling a whole-share ticker without adding its sizing entry silently doubles
its contribution at every multiplier and reaches its whole-share threshold materially
earlier than intended (Codex review: "Give BYDDY the configured one-dollar sizing
base"). `scripts/generate_per_ticker_sizing.py`'s `build_sizing_payload` calibrates the
active universe **plus** `"whole_share"` (falling back to the same generic global tiers
any low-history active ticker gets, listed in `"fallback_tickers"`) so a routine regen
never drops it again; `test_whole_share_tickers_have_a_one_dollar_per_ticker_sizing_entry`
in `test_config_invariants.py` enforces the $1 floor stays in place.

### Adjacent tooling that also needs to know about the `whole_share` category

Several tools outside the core dispatch/ledger path independently need to treat
`whole_share` as its own category rather than silently mishandling it:

- `scripts/backup_strategy.py`'s `extract_tickers()` (the transcript-based config
  extraction used by "Refreshing config from a new thread" below) now recognizes a
  `"Whole-share: ..."` marker in a pasted transcript, applies symbol corrections to it,
  dedupes it, and excludes its symbols from the extracted `active` list — the same
  treatment `excluded` already got — so a restore/refresh from a transcript can actually
  reproduce `tickers.json`'s three-way partition instead of silently dropping the
  `whole_share` category or leaving a whole-share symbol duplicated into `active` (Codex
  review: "Teach transcript backups about whole-share membership"). `_merge_into_file`
  (the merge step `write_configs` uses to apply extracted values onto an existing config
  file) filters out empty-list/empty-dict/`None` values by default — the right behavior
  for most extracted fields, where "the extractor found nothing" should leave an existing
  value untouched — but that same filtering silently discarded a DELIBERATE `"whole_share":
  []`/`"excluded": []` (a transcript whose universe no longer has any whole-share/excluded
  tickers), leaving a stale array in `tickers.json` that could overlap the newly-written
  `active` list and violate `dca_config.load_tickers`'s disjointness invariant. The merge
  now takes a `preserve_empty_list_keys` set that opts a key out of the empty-value
  filter, since an EXPLICITLY-mentioned-but-empty ticker-category list is a deliberate
  "nothing in this category now," never "say nothing" (Codex review: "Allow transcript
  restores to clear whole-share membership"). Whether a category counts as "explicitly
  mentioned" is decided by `extract_tickers`'s own `excluded_mentioned`/
  `whole_share_mentioned` flags — a separate, looser existence check for the
  `"Excluded:"`/`"Whole-share:"` marker itself, independent of whether any valid symbol
  follows it (so `"Whole-share: (none)"` still counts as an explicit, deliberate
  clearing). This distinction matters: an EARLIER version of this fix keyed "explicit"
  off the same regex that extracts symbols, which meant a transcript that simply OMITTED
  a `"Whole-share:"` section entirely (an abbreviated/older transcript, not one that asks
  to clear the category) looked identical to one that deliberately cleared it — silently
  wiping out an existing whole-share ticker (e.g. BYDDY) the transcript never mentioned at
  all (Codex review: "Preserve unmentioned ticker categories during transcript
  restores"). `write_configs` now drops `"excluded"`/`"whole_share"` from the merge
  payload ENTIRELY when the corresponding `_mentioned` flag is `False` (leaving an
  existing tickers.json value for it completely untouched), and only routes a category
  through `preserve_empty_list_keys` when the transcript actually mentioned it.
  `excluded_mentioned`/`whole_share_mentioned` themselves are anchored to the literal
  section-header COLON (`r"[Ee]xcluded\s*:"` / `r"[Ww]hole[\s-]?share\s*:"`), not just
  any trailing whitespace: an EARLIER version of this fix matched any occurrence of the
  word followed by whitespace, which treated ordinary descriptive prose — e.g. "BYDDY is
  a whole-share ticker" (the space right after "share" matched) — as an explicit section
  marker too, setting `whole_share_mentioned=True` with an EMPTY extracted list (the
  symbol-extraction regex requires an uppercase run right after the marker, which
  "ticker" isn't, so nothing actually gets extracted) and silently clearing a real
  existing membership whenever such prose happened to appear alongside an
  active-universe block (Codex review: "Require explicit category markers during
  transcript restore"). The `*_mentioned` DETECTION regexes require the literal colon,
  but the SYMBOL-EXTRACTION regexes (`extract_tickers`'s own `re.finditer` calls for
  both `"whole_share"` and `"excluded"`) originally still accepted bare whitespace —
  `[Ww]hole[\s-]?share[:\s]*` matched a space right after "share" just as readily as a
  colon. If a transcript contains a REAL `"Whole-share:"` section elsewhere (correctly
  setting `whole_share_mentioned=True`) plus separate, unrelated prose like "the
  whole-share XYZ is special," the extraction regex matched THAT prose too and added
  `"XYZ"` to the extracted list — `write_configs` would then persist this unintended
  symbol into `tickers.json`, where it could enter live strategy evaluation. Both
  extraction regexes now require the literal colon (`\s*:\s*`), matching their own
  `*_mentioned` checks' anchor exactly (Codex review: "Require a colon when extracting
  whole-share symbols").
- `ticker-advisor/advise_tickers.py`'s `_load_current_universe` now folds
  `config/tickers.json`'s `"whole_share"` array into the "active" set it returns (a
  whole-share ticker is just as ineligible for a fresh ADD proposal as an active one) —
  without this, a whole-share symbol like BYDDY was neither active nor excluded from the
  advisor's perspective and could be scored and proposed as a duplicate addition, skewing
  every other candidate's cross-candidate z-score normalization (Codex review: "Exclude
  whole-share symbols from ticker-advisor candidates").
- `config/scheduled_tasks.json`'s `month_end_volatility_risk_policy.universe.excluded`
  (an agent-consumed policy block, not repo code — this monthly Notion report has no
  in-repo Python executor) used to still list `BYDDY` after it moved from `tickers.json`'s
  `excluded` into its `whole_share` category. Since a whole-share ticker is actively
  purchased by the graded ladder, it belongs in this report's 30-day drop/3x-event/
  recovery-risk table alongside every other active/whole-share holding, not omitted from
  it as if it were a true exclusion — this `excluded` array must mirror
  `config/tickers.json`'s own `excluded` array (true no-buying-mechanism exclusions
  only), never `whole_share` (Codex review: "Include whole-share tickers in month-end
  risk reporting"). `test_config_invariants.py`'s
  `test_whole_share_tickers_are_not_excluded_from_month_end_risk_reporting` locks this in.

- `dca_market_graph.py`'s `active_tickers_from_config()` now also folds `tickers.json`'s
  `"whole_share"` array into the ticker list it returns (deduped against `active`, still
  honoring `excluded`) — a whole-share ticker like BYDDY is a real, actively-purchased
  part of the live DCA universe, so omitting it silently dropped a real holding from The
  Ocean's 3D market graph entirely (Codex review: "Include whole-share symbols in
  market-graph config defaults").
- `ml-forecaster/train.py`'s `_load_active_tickers()` likewise now folds `"whole_share"`
  into the returned training universe (deduped against `active`, still honoring
  `excluded`, each member validated against the same ticker-symbol pattern `active`/
  `excluded` members already are) — without this, a whole-share ticker never entered the
  pooled Ridge fit and never received a published sizing adjustment, silently starving it
  of the same ML-driven sizing every other actively-purchased ticker gets (Codex review:
  "Train and publish ML sizing for whole-share symbols").

See also `docs/restore_runbook.md`'s "Not backed up" note and audit checklist: the
whole-share ledger itself (`stream_root("whole_share_accumulation")`) is gitignored run
state, not tracked config, so restoring this repo from a backup does not restore BYDDY's
accumulated balance — that tree has to be copied over separately (Codex review: "Preserve
the whole-share ledger during restores").

## Scope (read before assuming this fires on every stream)

Wired into `near_close_dca._run_once_impl` only — the function `run_dca.run_slot()`/
`run_pipeline()` calls, covering the **3:30 PM final-buy canonical stream**. It is
**not** wired into `MorningDCAExecutor` (`morning_dca.py`) or its subclasses
(`market_open_dca.py`, `midday_dca.py`, `intraday_dca.py`) — none of those call
`near_close_dca._run_once_impl`/`_process_buy_candidate` at all (see `CLAUDE.md`'s
"Three entry points" section: they are a genuinely separate code path). A whole-share
ticker's dip evaluated on one of those streams is simply not evaluated there at all —
today, only the 3:30 PM stream tracks and fires BYDDY. This mirrors
`dca_inverse_martingale.py`'s identical scope decision (see `docs/inverse_martingale.md`'s
own "Scope" section) — wiring either of the other two entry points in is a separate,
explicitly deferred follow-up (`CLAUDE.md`: "Consolidating is a decision, not a
cleanup").

The same gap applies to the manual Gate 5 / `scripts/agent_entry_points.py` final-buy
command (`scripts/generate_buy_candidates.py`, `dca_runner.py`): `generate_buy_candidates.py`'s
candidate loop iterates only `config.tickers` (the normal active universe) and knows
nothing about `config.whole_share_tickers`, and `dca_runner.py`'s gate sequence only
whitelists `tickers.json`'s `active` array and only ever places notional (dollar-amount)
orders — it has no `place_whole_share_buy`/quantity-order dispatch at all (Codex review:
"Route the final-buy Gate 5 entry point through whole-share logic", evaluated and NOT
applied). Routing either of those through the accumulator would mean adding a full
second integration of `_process_whole_share_candidate`'s dispatch/risk-gate/settle flow
into an entry point this repo's own convention (`CLAUDE.md`: "Consolidating is a
decision, not a cleanup") treats as independently evolving — the SAME reasoning that
already justifies not wiring `MorningDCAExecutor` above, just for a different entry
point. An operator invoking that manual command today gets ONLY the normal active
universe; BYDDY still only ever accumulates/fires through the automated 3:30 PM
`run_dca.run_slot()` path. Wiring it in is explicitly deferred, matching this section's
existing precedent, not an oversight.

Within `_run_once_impl`:

- `tradable_tickers` (the normal `active_universe`, `config.tickers` minus
  `config.excluded_tickers`) and `whole_share_evaluated_tickers`
  (`config.whole_share_tickers` minus `config.excluded_tickers`) are evaluated by the
  SAME call to `_evaluate_all_signals`, so a whole-share ticker's signal is computed,
  reconciled across sources, and appears in `decisions`/`signals.csv` exactly like any
  active ticker's.
- Any decision whose `action == Action.BUY` becomes a `buy_candidates` entry, exactly
  like a normal ticker — this is what "purchased along with the regular buys" means in
  practice: BYDDY flows through the same batch (`_process_buy_candidates_batch`) as
  every other candidate that run.
- A whole-share ticker whose earlier fire attempt was blocked by a transient gate
  (ceiling/cap/spread/review/data-error — anything that calls `preserve_pending`) can
  leave `pending_usd` already at or above one share's price even though TODAY's own
  fresh signal is `NO_ACTION` (e.g. the price recovered). The BUY-only filter above alone
  would never re-include such a ticker, leaving an already-funded fire stranded with no
  bound on how long until some LATER day's signal happens to be a fresh BUY again. After
  building the BUY-only list, `_run_once_impl` re-checks each whole-share ticker's
  CURRENT ledger balance against a price re-derived from its finance/Robinhood signals
  (a non-BUY `ReconciledDecision` never runs through `_target_fields`, so its
  `target_price`/`decision_bar_close` are still their bare `0.0` defaults) and splices in
  a *copy* of the decision — carrying that re-derived price, never mutating the original
  used for `signals.csv`/`decisions` — whenever the balance alone already covers a share
  (Codex review: "Re-dispatch preserved fires without requiring a new BUY"). The
  candidate then reaches `_process_whole_share_candidate` with a $0 new contribution,
  exactly like a same-day retry — `record_contribution` still fires from `pending_usd`
  alone.
  - **Restricted to a genuinely safe `NO_ACTION`, never `MANUAL_REVIEW`.** The check
    originally excluded only decisions already classified `Action.BUY`, which let a
    `MANUAL_REVIEW` decision through too — `reconcile_signals` returns `MANUAL_REVIEW`
    specifically as an explicit "do not auto-buy" verdict (finance/Robinhood disagree, or
    one provider errored while the other said BUY), and redispatching it would have
    bypassed that verdict and fired the accumulated balance anyway. The check now
    requires `decision.action == Action.NO_ACTION` exactly (Codex review: "Do not
    redispatch manual-review whole-share decisions").
  - **Eligibility compared with the SAME cent-rounded arithmetic the real fire uses.** A
    raw `pending_usd >= current_share_price` float comparison can disagree with
    `apply_contribution`'s own `Decimal`-based cent rounding at a fractional-cent quote
    (e.g. $10.00 pending vs. a $10.004 quote compares insufficient under a raw float
    `>=`, while `apply_contribution` rounds the quote to $10.00 and fires). The
    eligibility check now calls `apply_contribution(pending_usd, 0.0, current_share_price)`
    itself — the exact same pure decision function the real placement path uses — and
    checks `shares_to_buy > 0` (Codex review: "Compare redispatch thresholds using
    cent-rounded prices").
  - **A ledger read failure surfaces instead of silently vanishing.** The eligibility
    check's own `pending_usd`/`apply_contribution` call can itself raise (a corrupt/
    unreadable/locked ledger) — rather than `continue`-ing past this ticker with no
    trace, the priced decision is dispatched anyway, so `_process_whole_share_candidate`'s
    own `record_contribution` call hits the SAME failure and classifies it properly
    (`errors_count_inc`, `MISSING_FILL_DATA_ERROR`) instead of this eligibility check
    swallowing it as a quiet no-op that leaves the run looking clean (Codex review:
    "Surface ledger failures during preserved-fire redispatch"). Both of
    `_process_whole_share_candidate`'s own ledger-error outcomes (dry-run preview and
    real path) also set a `whole_share_planned_notional_usd: 0.0` marker for the next
    bullet's benefit.
  - **Every redispatch outcome is still persisted in `buy_candidates.csv`.**
    `_build_buy_candidates`'s outer filter used to retain a non-BUY decision's row only
    when its outcome status was `"submitted"`/`"dry_run"` (the pending-credit-flush
    case) — a redispatched `NO_ACTION` candidate that reached a safety gate (spread,
    buying power, cap, a ledger error) and got blocked was silently dropped from
    `buy_candidates.csv`/`run_state.json` entirely, losing the block reason and planned
    notional and potentially misclassifying a budget-only block as `no_candidates`. An
    early version of this fix retained a non-BUY row whose outcome carried ANY
    `"whole_share_"`-prefixed key (Codex review: "Persist blocked preserved-fire
    redispatch outcomes"), on the assumption that every `_process_whole_share_candidate`
    branch reachable via redispatch sets at least one such key — but several genuinely
    don't (the top-of-function same-day-duplicate SKIP, a claim-verification/lost-claim
    exit, several restoration-error outcomes), so those outcomes kept vanishing anyway
    (Codex review: "Retain every whole-share redispatch outcome"). The filter now
    identifies a redispatch from the DECISION itself instead of scanning outcome keys:
    every `priced_decision` this loop splices in carries
    `synthetic_source="whole_share_redispatch"` (see below), a field ALWAYS present
    regardless of which exit branch eventually fires, so a future
    `_process_whole_share_candidate` exit that forgets a `"whole_share_"` marker can no
    longer silently vanish the way the key-scan approach could. This required actually
    reflecting the spliced `priced_decision` back into `decisions` too — mirroring the
    inverse-Martingale fire candidate's own `decisions_by_ticker`/`decisions[:]` rebuild
    convention further below — since `_build_buy_candidates` (and `run_result.decisions`,
    assigned by reference to the same list) previously still saw the ORIGINAL, un-spliced
    `NO_ACTION` decision with `synthetic_source == ""`, which the new check would
    otherwise never match.
  - **Price reconciled across BOTH sources, not selected from one.** `reconcile_signals`'s
    `NO_ACTION` fallthrough branch (both sources independently report no fresh signal)
    performs no price-agreement check of its own — it only compares actions, not prices.
    Selecting one source's `quote_price` unconditionally (finance preferred, as this
    redispatch originally did) could size and approve an order against a price the OTHER
    source actively disagrees with (e.g. finance $9.50 vs. Robinhood $20), bypassing
    `max_per_order_usd` at the disagreeing price (Codex review: "Reconcile prices before
    redispatching preserved fires"). `_whole_share_redispatch_reconciled_price` requires
    BOTH `finance_signal`/`robinhood_signal` present, neither carrying a `provider_error`,
    and their `quote_price`s (falling back to `decision_bar_close`) to agree within
    `_INVERSE_MARTINGALE_SOURCE_AGREEMENT_TOLERANCE` (2%) — the SAME safety bar
    `_inverse_martingale_reconciled_price` already applies to its own synthetic,
    action-gate-bypassing fire, just against the live execution-time quote instead of the
    signal-time comparison price. A disagreement returns `None`, and the redispatch
    abstains for this ticker entirely (deferred to a later run, not lost). When the two
    quotes DO agree (within tolerance), the reconciled price returned is the HIGHER of
    the two, not unconditionally Finance's — `place_whole_share_buy` is Robinhood-only
    (see `RobinhoodBroker`'s class docstring), so sizing/threshold/`max_per_order_usd`
    math against the LOWER of two agreeing quotes can authorize spending beyond what the
    broker will actually fill at. With Finance quoting $9.90 and Robinhood quoting
    $10.09 (within the 2% tolerance), unconditionally using Finance's price let a $10
    pending balance and the default $10 ceiling both look sufficient/compliant even
    though the broker was already quoting above both figures (Codex review: "Size
    whole-share orders using the higher reconciled quote"). This same helper is ALSO
    called by `_process_whole_share_candidate` for an ordinary fresh `Action.BUY`
    decision with both sources present (see "Cross-source execution-price agreement" in
    "Risk gates" above and "Reconcile fresh whole-share BUY prices before sizing"/
    "Reconcile the tier-planning whole-share price" below) — the higher-quote fix applies
    there identically.
  - **The deployment kill switch caps the EXECUTABLE fire, not just today's (already-$0)
    contribution.** The existing kill-switch cap (below, "Deployment kill switch") only
    ever capped `contribution_usd` — for a redispatch that contribution is already $0
    (the fire is funded entirely by an earlier day's accumulated balance), so that cap did
    nothing and an uncapped executable order could still spend the FULL accumulated
    balance while the kill switch was engaged, unlike the normal buy path, which caps
    `order_amount` itself directly. `_whole_share_risk_gates` now accepts a
    `ceiling_override_usd` parameter — `min()`-combined with `config.max_per_order_usd` —
    that both the dry-run preview and the real placement path pass as
    `decision.selected_base_amount_usd` whenever the kill switch is engaged. A redispatched
    `NO_ACTION` decision never populates `selected_base_amount_usd` (it stays at its bare
    `0.0` default, since only a fresh `BUY` sets it), so the effective ceiling collapses to
    $0 and the fire is blocked outright — deferred until the kill switch disengages, never
    executed at any size while it's active (Codex review: "Apply the kill switch to the
    executable whole-share fire").

  A ledger read failure during this re-dispatch check is caught and logged, same as
  every other whole-share ledger read in this module, so a corrupt/unreadable BYDDY
  ledger degrades only this eligibility check, never the run as a whole.
- `whole_share_evaluated_tickers` is only added to the evaluated universe when
  `stream == "afternoon"` — `_run_once_impl` is shared by all three canonical slots
  (morning/midday/afternoon all call it, see `CLAUDE.md`'s "Three entry points"), but a
  whole-share ticker is only signaled/evaluated on the afternoon (3:30 PM) run. The
  morning and midday slots never see BYDDY in `decisions`/`signals.csv` at all, and
  `test_run_dca.py` asserts the evaluated count accordingly per slot.
- `near_close_dca._process_buy_candidate` dispatches on
  `ticker in ctx.config.whole_share_tickers` BEFORE the top-up guard, the cumulative
  sub-dollar credit ledger, or the `is_tradable_for_fractional_buy` gate — none of those
  apply to a whole-share ticker. `_process_whole_share_candidate` (the dispatch target)
  handles the SAME broad broker same-day-duplicate check
  (`ctx.broker.same_day_strategy_order_exists`) every unqualified normal candidate gets,
  then the risk gates and accumulator/placement flow described below.

### Risk gates (mirrors the normal buy path)

`_process_whole_share_candidate` applies the same safety checks
`_process_buy_candidate` applies to a normal notional order, adapted for a
quantity-based order:

- **Cross-source execution-price agreement.** For an ORDINARY, fresh `Action.BUY`
  decision with both `finance_signal` and `robinhood_signal` present,
  `reconcile_signals`'s own "finance and Robinhood agree on BUY" branch selects ONE
  winning signal's `quote_price` purely by whichever produced the smaller
  `suggested_amount_usd` — it never checks that winning quote against the OTHER
  source's own quote for agreement (this is harmless for a normal dollar-amount order,
  which spends the same number of dollars regardless of price, but a whole-share
  order's share COUNT and the `max_per_order_usd` ceiling below are both computed
  directly from this price). Finance quoting $9.50 and Robinhood quoting $20 could let
  a $10 ceiling approve one share as a $9.50 order right before Robinhood actually
  executes it near $20. `_process_whole_share_candidate` now reuses the SAME
  cross-source agreement check the preserved-fire redispatch path already applies
  (`_whole_share_redispatch_reconciled_price`, despite its name now serving both
  callers) whenever both signals are present on a real `Action.BUY` decision, blocking
  the candidate outright (no ledger write) when the sources disagree or either is
  unusable, rather than trusting `decision.target_price` directly (Codex review:
  "Reconcile fresh whole-share BUY prices before sizing"). A single-source BUY (one
  provider had no data) is unaffected — there is nothing to disagree with.
- **`max_per_order_usd` ceiling.** Computed against `current_share_price_usd` before any
  ledger write: if even one share exceeds the per-order cap, the candidate is blocked
  (`MISSING_FILL_BUDGET_CAPPED`) and the full contribution is preserved via
  `preserve_pending`. If more than one share's worth was about to fire, the quantity is
  clamped down to the largest affordable count and `settle`'s automatic shortfall
  restoration (see "Fire mechanics") carries the unclamped remainder forward. The share
  count itself is computed by `_whole_share_max_shares_by_ceiling`, which rounds
  `max_per_order_usd`/`current_share_price` to integer CENTS before dividing, rather than
  a raw `//` float floor-division — a cent-rounded price is frequently not representable
  exactly as a binary float (`10.0 // 0.10` evaluates to `99.0` in Python, not `100.0`),
  which would otherwise unnecessarily clamp a fully-affordable fire by one share (Codex
  review: "Compute the share ceiling with exact cent arithmetic"). Shared by both this
  gate and the tier-planning preview's own copy of the same calculation.
- **Tradability lookup failure.** `is_tradable_for_whole_share_buy` itself can raise (a
  broker API/network failure) — caught explicitly and treated like any other ledger/data
  error: `preserve_pending` restores the already-deducted planned notional and the
  candidate is classified a data error (`MISSING_FILL_DATA_ERROR`), never left to escape
  and abort the batch with the deduction permanently unrestored (Codex review: "Restore
  the deduction when tradability lookup raises"). The tradability check itself compares
  the requested ticker against each result's `symbol` case-INsensitively
  (`RobinhoodBroker.is_tradable_for_whole_share_buy`) — the broker's response can return a
  symbol in its own canonical case even when queried with a differently-cased one, and an
  exact-match comparison would otherwise silently treat a correctly-tradable instrument as
  not found (Codex review: "Require canonical symbols for whole-share tickers"). The
  companion config-level defense is `schemas/tickers.schema.json`'s `"whole_share"`
  property, which now carries the same `"^[A-Z.]{1,6}$"`/`uniqueItems` constraint
  `"active"` already had, so a lowercase symbol cannot even enter `config/tickers.json` in
  the first place.
- **`within_caps` (daily/weekly budget).** Checked exactly like the normal path, on the
  (possibly clamped) order notional.
- **Bid/ask spread gate.** Same `quote_spread_pct`/`spread_gate_fields`/
  `BLOCKED_BY_SPREAD_GATE` logic as the normal path — missing or too-wide spread data
  blocks a scheduled run.
- **Buying power lookup failure.** `ctx.broker.available_buying_power()` itself can raise
  (a broker API/network failure), exactly like the tradability lookup above — caught
  explicitly and treated the same way: `preserve_pending` restores the already-deducted
  planned notional, the candidate is classified `MISSING_FILL_DATA_ERROR`, and
  `errors_count_inc` is incremented, rather than letting the exception escape and abort
  the whole batch with the deduction permanently unrestored (Codex review: "Restore
  pending credit when buying-power lookup fails").
- **Buying power.** Checked against the (possibly clamped) order notional, same as the
  normal path.
- **Deployment kill switch.** `kill_switch_engaged` (rolling 7-day deployment threshold)
  caps TODAY's contribution to `decision.selected_base_amount_usd` ($1/$2, unmultiplied)
  before it ever reaches the ledger — the same cap the normal path applies to
  `order_amount`, adapted here to the pre-ledger contribution since a whole-share order
  is a quantity, not a dollar amount, and the kill switch cannot retroactively shrink an
  already-accumulated pending balance (Codex review: "Apply the deployment kill switch
  to whole-share orders").
- **Manual-run review failure.** `review_whole_share_buy` itself can raise on a manual
  (`ctx.scheduled=False`) run, exactly like the tradability/buying-power lookups above —
  caught explicitly, `preserve_pending` restores the deducted planned notional, and the
  candidate is classified `MISSING_FILL_DATA_ERROR` (Codex review: "Restore the ledger
  when manual review raises").
- **Pre-placement duplicate-lookup failure.** The SECOND `same_day_strategy_order_exists`
  recheck immediately before `place_whole_share_buy` (see "Concurrency" below) can itself
  raise. Unlike a successful duplicate MATCH there (a genuine reclaim, where preserving
  would incorrectly restore money a still-viable reclaiming attempt may yet spend), a
  raised exception here is a known pre-placement failure — no order has been placed by
  anyone — so it is always safe to `preserve_pending` and classify as
  `MISSING_FILL_DATA_ERROR` (Codex review: "Restore the fire when the final duplicate
  lookup raises").
- **Initial duplicate-lookup failure.** The FIRST `same_day_strategy_order_exists` check —
  at the very top of `_process_whole_share_candidate`, run BEFORE `record_contribution`
  ever touches the ledger — can itself raise the same way. No ledger restoration is
  needed here (nothing has been deducted yet); the candidate is simply classified
  `MISSING_FILL_DATA_ERROR` instead of letting the exception escape
  `_process_buy_candidates_batch` and abort the whole run for every other, independent
  candidate still to be processed (Codex review: "Isolate the initial duplicate-order
  lookup failure").
- **Reconciling an unresolved claim.** `_reconcile_unresolved_whole_share_claim` is a
  shared module-level helper, called from TWO places (both described below): the initial
  duplicate-lookup branch, and a cross-day recovery check that runs at the top of the
  function. Given a raw cached claim entry and the trading day it was recorded under, it
  settles that claim ONLY when the broker's own order history confirms an EXACT match on
  that claim's own deterministic `ref_id` — a broad "some same-day order exists" signal is
  NOT enough, since that broad check can be True purely because of a completely UNRELATED
  live order (a manual trade, or a different stream's own fire); settling on that alone
  would record a purchase that never happened for THIS claim and corrupt
  `shares_purchased_total` (Codex review: "Require an exact order match before
  settling"). When an exact match IS found, it settles using the matched order's REAL
  quantity — but only treats `cumulative_quantity` as authoritative once the order has
  reached a TERMINAL `"filled"` state; for any OTHER live state (`"new"`/`"queued"`/
  `"confirmed"`/`"unconfirmed"`/`"partially_filled"`), `cumulative_quantity` can be zero or
  smaller than what was actually submitted while the remainder is still outstanding, and
  settling with that partial count would restore the "unbought" shares' credit and mark
  the claim resolved even though the order could still fill for real later — letting a
  LATER run spend that freed credit before the outstanding order settles (Codex review:
  "Reserve the full live order quantity during reconciliation"). For a non-terminal order
  it instead uses the order's own SUBMITTED `quantity` field, reserving credit for the
  FULL live order until it reaches a terminal state, rather than guessing at a partial
  fill; falls back to the claim's own UNCLAMPED `shares_to_buy` only when the matched
  order carries no usable quantity field at all (a per-order-ceiling clamp at placement
  time would otherwise understate the shortfall `settle()` restores if this fallback were
  used more broadly). Critically, it passes the claim's OWN `claimed_at` as `settle()`'s
  `recorded_at`, never a fresh timestamp from the reconciling call itself — `settle()`'s
  ownership guard only reconciles (marks resolved, restores any shortfall) when
  `recorded_at` matches the cache entry's own `claimed_at`; an earlier version of this fix
  passed `ctx.now.isoformat()` instead, which on a REAL retry (run at a different time
  than the original claim) would silently fail that guard — still bumping
  `shares_purchased_total`, but leaving the claim (and any shortfall) unresolved forever,
  so every future reconciliation attempt would double-count the same purchase again (Codex
  review: "Reconcile using the original claim timestamp"). Returns `None` (leaves the
  claim exactly as-is) when `shares_to_buy` isn't positive, the trading day doesn't parse,
  or no exact match is found; otherwise returns `{"order_id", "shares_purchased",
  "order_notional_usd"}` describing what it settled.

  A settled result is NOT itself the whole story — `_book_reconciled_whole_share_order`
  books it into the SAME places an ordinary, in-line placement books into:
  `ctx.state.record_buy`/`own_orders_recorded` (against the claim's OWN trading day, not
  today's), the running 7-day deployment total (for kill-switch accuracy this run), and
  the in-flight/execution-record artifacts. Without this, a reconciled order that
  genuinely landed at the broker was recorded in the whole-share ledger but nowhere
  else — daily/weekly budget caps, the deployment kill switch, and persisted execution
  artifacts all silently omitted real spend the broker already executed (Codex review:
  "Book reconciled orders into the strategy budget state").

  Both reconciliation call sites are skipped entirely when `ctx.config.dry_run` is set —
  `ledger.settle()` durably mutates the REAL ledger, and (with the booking fix above) also
  writes into REAL `ctx.state`/execution artifacts; a demo/shadow invocation resolving a
  real claim, changing its pending balance, bumping `shares_purchased_total`, or booking
  budget state would violate the dry-run path's own non-mutation guarantee and let a demo
  alter what a LATER live run would see (Codex review: "Keep cross-day reconciliation
  read-only during dry runs" — applied to both call sites for consistency, since the
  original finding named only the cross-day one).
  - **At the initial duplicate check.** When the FIRST `same_day_strategy_order_exists`
    check finds a same-day order ALREADY exists (the ordinary "skip, another order is
    already live" path), it looks up `ledger.unresolved_claim(ticker, trading_day_iso,
    idempotency_key)` for THIS ticker's own TODAY slot and, if found, calls the shared
    helper above and books any settled result. An earlier attempt today can have left
    that exact slot deliberately unresolved — a placement that raised whose ref-id
    reconciliation lookup ALSO raised (see "Ambiguous placement failure" below)
    intentionally leaves the claim untouched rather than guessing whether the order
    landed; without reconciling here, the SAME broad check would find the SAME live
    order on every future retry too, permanently stranding that claim's reservation
    (Codex review: "Reconcile reserved claims before the duplicate early return").
    Best-effort: a failure here is logged and does not block the ordinary "skipped"
    outcome this branch already returns.
  - **Across trading days.** `ledger.unresolved_claims_any_day(ticker, idempotency_key)`
    scans EVERY `recorded_idempotency_keys` entry for this ticker's idempotency key,
    regardless of which trading day it was recorded under, and returns EVERY unresolved
    match (sorted oldest trading day first), not just the first — a single ticker can
    accumulate more than one unresolved claim over time (e.g. an older placement
    genuinely rejected, so it never gets an exact ref-id match, alongside a LATER
    ambiguous placement that was actually accepted); returning only the first would let
    the older, irreconcilable entry starve every later one from ever being checked,
    hiding the accepted claim's purchase from the audit and stranding its shortfall
    forever (Codex review: "Reconcile every unresolved claim instead of only the
    first"). `unresolved_claim` (used by the check above) only ever looks at ONE
    specific trading day — exactly the day a same-day retry passes — so an ambiguity
    that survives past midnight (a doubly-failed ambiguous placement, same as above)
    becomes permanently invisible to it: the NEXT day's run passes a brand-new
    `trading_day_iso` that can never retrieve the prior day's entry, and the broad
    same-day duplicate check only looks at orders from TODAY onward, so it would never
    even notice a stale claim whose real order landed on an EARLIER day (Codex review:
    "Reconcile unresolved claims across trading days"). `_process_whole_share_candidate`
    runs this cross-day check right after computing `idempotency_key` and BEFORE the
    initial duplicate-order check (skipped when `dry_run`, see above) — iterating every
    match and, for each, using its OWN recorded trading day (not today's) to build its
    ref_id and to scope the broker's order-history lookup, since
    `RobinhoodBroker._same_day_orders` filters `created_at_gte=trading_day.isoformat()`,
    so passing today's date would miss an order actually placed on that earlier day.
    Best-effort, like the check above: a failure here is logged and never aborts this
    candidate, and it never touches a claim for the CURRENT trading day (that's the
    initial duplicate check's own job).
- **Claim-verification failure.** The placement-time `still_claimed` recheck (see
  "Concurrency" below) can itself raise (a corrupt/unreadable ledger or a lock timeout).
  `record_contribution` has already deducted the planned fire by this point, but unlike
  every OTHER ledger-error boundary in this function, this one must NOT call
  `preserve_pending`: if a reclaiming caller actually does own the claim now, preserving
  here would incorrectly restore money that caller may yet spend. The candidate instead
  fails closed — classified `MISSING_FILL_DATA_ERROR` without touching the ledger at all —
  leaving the claim's resolution to whichever caller can actually verify it (Codex review:
  "Isolate claim-verification ledger failures").
- **Restoration failure while blocking.** `_preserve_and_block` — the shared helper every
  normal pre-placement gate above (ceiling, `within_caps`, spread, buying power,
  tradability, manual review) calls to restore a claimed fire it is about to block — has
  its own `ledger.preserve_pending` call wrapped in try/except: a lock timeout,
  unreadable ledger, or write failure there is the SAME failure-isolation problem as
  `still_claimed` above. On failure, the ORIGINAL block reason is superseded by the
  restoration failure itself (`MISSING_FILL_DATA_ERROR`), the deducted fire is left
  exactly as `record_contribution` left it (neither settled nor restored, since this
  claim's true state could not be verified), and the exception is prevented from
  escaping `_process_buy_candidates_batch` and aborting every other, unrelated candidate
  still to be processed (Codex review: "Isolate failures while restoring blocked
  fires"). That restoration-failure outcome also sets `whole_share_planned_notional_usd`
  — without SOME `"whole_share_"`-prefixed field, a NO_ACTION preserved-fire redispatch's
  (see "Preserved-fire redispatch" further below) restoration-failure outcome would be
  silently dropped from `buy_candidates.csv`/`run_state.json` by `_build_buy_candidates`'s
  non-BUY filter, even though `errors_count_inc` correctly counted the failure (Codex
  review: "Retain the whole-share marker when restoration fails").
- **Ambiguous placement failure.** `place_whole_share_buy` can raise AFTER the broker has
  already accepted the order — a response timeout or connector failure while relaying the
  accepted order back is indistinguishable, from the exception alone, from a genuine
  rejection. The FIRST thing this branch now does, before touching the ledger at all, is
  reconcile: it looks up this deterministic `ref_id` (built once, above, from
  `_graded_dca_ref_id_prefix(ctx.stream, "wholeshare")` — the SAME string on every retry
  for this idempotency slot, unlike the topup ref-id shape) against the broker's own
  same-day order history via `same_day_strategy_order_exists_with_ref_id_prefix(ticker,
  ctx.trading_day, (ref_id,))`. Blindly restoring the deducted pending credit whenever
  `place_whole_share_buy` raises — the old behavior — would let a LATER trading day spend
  the same dollars again while the broker-side order already consumed them; a same-day
  retry might independently notice the order via the duplicate-order check and skip, but
  nothing else re-examines an already-restored balance (Codex review: "Reconcile
  ambiguous placement failures before restoring credit"). When the lookup finds a matching
  live order, this branch mirrors the ordinary success path instead of the failure path:
  it calls `ledger.settle` (never `preserve_pending`) and returns a `"submitted"` outcome
  using the broker's own order id — `errors_count_inc` is NOT incremented, since the order
  genuinely succeeded. That settle() call is followed by the SAME authoritative
  `ledger.pending_usd(ticker)` re-read the ordinary success path performs (with the same
  read-failure fallback that adds the known clamp-restoration shortfall back on), rather
  than reporting the pre-settlement `wsa_decision.pending_after_usd` snapshot — a multi-
  share fire clamped by the per-order ceiling has its unsubmitted shares' credit restored
  by `settle()`, and the pre-settlement snapshot would under-report exactly that restored
  amount (e.g. a two-share $16 fire clamped to one $8 share leaves the ledger at $8 after
  settlement, but the pre-settle snapshot would report $0) (Codex review: "Report the
  restored balance after ambiguous placement recovery"). If the reconciliation lookup
  ITSELF raises, that failure establishes NEITHER outcome — treating it as "no match
  found" and falling through to the conservative restoration branch below (the original
  version of this fix) would restore credit for an order that may actually be LIVE at the
  broker, exactly the hazard this whole reconciliation step exists to close. Instead, a
  failed lookup fails the candidate closed WITHOUT mutating the ledger at all (mirroring
  the placement-time `still_claimed` verification failure's own posture, see "Concurrency"
  below), leaving the claim exactly as `record_contribution` left it — reserved, neither
  settled nor restored — for a later run (once the broker's order history is reachable
  again) or manual reconciliation to resolve (Codex review: "Keep ambiguous claims
  reserved when reconciliation fails").
- **Restoration failure after a genuinely unreconciled placement failure.** Once the
  reconciliation lookup above has ruled out the broker having accepted the order, the
  `preserve_pending` call that restores the deduction can itself raise (a lock timeout,
  corrupt ledger, or write failure) — an unguarded call there would let this SECOND
  exception replace the original broker failure and escape `_process_buy_candidates_batch`
  entirely, aborting unrelated candidates while this ticker gets no `"failed"` outcome at
  all. Wrapped in its own try/except: the returned outcome's `skip_reason` names BOTH
  failures (the original placement failure and the restoration failure), and the claim is
  left exactly as `record_contribution` left it — neither settled nor restored, since its
  true state could not be verified (Codex review: "Isolate restoration errors after
  placement failures").
- **Restoration failure in the confirmation-required path.** The `OrderConfirmationRequiredError`
  branch (see "Confirmation-required placements" below) has its OWN `preserve_pending`
  call — no order was placed, so restoration is the correct response, but that call can
  ALSO raise the same way the generic placement-failure branch's identical call can.
  Wrapped in its own try/except too: on failure, the candidate is classified a data error
  (not `"not_submitted_confirmation_required"`), the claim is left untouched, and the
  exception is prevented from escaping the batch (Codex review: "Isolate restoration
  failures in the confirmation-required path"). That branch's SUCCESSFUL
  `"not_submitted_confirmation_required"` outcome also sets
  `whole_share_planned_notional_usd` — without it, a manual, NO_ACTION-redispatched
  ticker's confirmation-required hold would be dropped by `_build_buy_candidates`'s
  non-BUY filter, and if it were the only candidate this run, the run would report
  `no_candidates` instead of `confirmation_required` (Codex review: "Preserve
  confirmation-required redispatch outcomes").
- **Initial duplicate-lookup failure outcome for a redispatch.** The FIRST, top-of-function
  `same_day_strategy_order_exists` check's own failure outcome (see "Initial
  duplicate-lookup failure" above) now also sets `whole_share_planned_notional_usd` — this
  check runs before the ledger and before every other whole-share-marking helper, so a
  NO_ACTION preserved-fire redispatch reaching it would otherwise have its error outcome
  silently dropped by `_build_buy_candidates`'s non-BUY filter, exactly like the
  restoration-failure marker fix above (Codex review: "Retain early redispatch lookup
  failures in candidate artifacts").

A block at any of these gates calls `preserve_pending` so the contribution is never lost
— it simply stays in the ledger's `pending_usd` for the next run to re-evaluate. The
ceiling/`within_caps`/spread checks themselves live in the module-level
`_whole_share_risk_gates` helper, shared by BOTH the real placement path below and the
dry-run preview path — a single implementation the two branches can never drift out of
sync on (see "Dry-run isolation" below for why that matters).

**Known limitation: fill-price slippage is not hard-capped.** `place_whole_share_buy` /
`review_whole_share_buy` submit a QUANTITY-based order (`quantity=`), not a dollar-amount
one — unlike the normal fractional path, the broker is free to fill at any price at or
above the signal-time quote used to size the order, so the `max_per_order_usd` ceiling,
`within_caps`, and buying-power checks above are only as accurate as that signal-time
price (Codex review: "Enforce dollar caps against the executable order price", evaluated
and NOT applied). A price-headroom buffer (sizing the ceiling/cap checks against a
slightly higher price than signal-time to leave room for slippage) was tried and reverted:
at the default `max_per_order_usd=$10` ceiling and the test suite's convention of pricing
BYDDY at exactly $10/share (an already razor-thin boundary — one share costs exactly the
ceiling), any headroom makes `max_shares_by_ceiling` compute to 0 and blocks every
whole-share order outright. A correct fix needs either a verified-safe headroom
calibrated to BYDDY's real price range (not the test fixture's $10 edge case) or, better,
a genuine limit-priced quantity order — which requires confirming the wired Robinhood MCP
`review_equity_order`/`place_equity_order` tools even accept a limit price alongside
`quantity=` (see "The unverified assumption" below; nothing in this repo has verified
that). Until one of those lands, treat the whole-share path's dollar ceilings as
best-effort at signal-time pricing, not a hard guarantee against slippage.

### Dry-run isolation

`ctx.config.dry_run` (the repo-wide default) is checked FIRST, before any ledger call.
A dry run computes a pure, read-only preview with `apply_contribution(ledger.pending_usd(...), ...)`
and never calls `record_contribution`, `preserve_pending`, or `settle` — mirroring the
normal buy path's rule that a dry run must never durably mutate real state
(`StrategyState.record_buy()` is likewise never called on that path). A later real
(non-dry-run) evaluation of the same ticker is therefore unaffected by however many dry
runs preceded it.

Before applying `contribution_usd`, the preview checks `ledger.cached_fire_decision` for
the CURRENT (trading_day, idempotency_key) slot: a same-day retry previewed via `--demo`
after a genuine scheduled run already recorded that exact slot must not re-apply the
contribution on top of `pending_usd`. When the slot is resolved or still within its claim
window, this returns the same zero-shares "accumulated, threshold not yet reached"
decision the identical live retry would get; when the slot's claim has instead gone stale
and is reclaimable, it returns the same positive decision the live retry would actually
fire — either way mirroring exactly what `record_contribution` would return right now
(Codex review: "Honor slot idempotency in dry-run previews", refined by "Account for
actionable stale claims in tier planning" once the same gap was found in the tier-planning
preview and fixed here too for consistency).

The dry-run preview also runs through the SAME `_whole_share_risk_gates` helper as the
real path before it is reported: a preview crossing the `max_per_order_usd` ceiling is
clamped down (or blocked outright, if even one share exceeds it), a preview that would
breach `within_caps` is blocked, and a preview with missing/too-wide spread data is
blocked on a scheduled run — never durably, since the gate check itself makes no ledger
writes. Without this, a `--demo`/shadow run could report a `dry_run` order for a
quantity the identical live run would actually clamp or block, making the artifact
unreliable for validating live behavior (Codex review: "Apply safety gates to
whole-share dry-run previews"). Once those gates pass, the spread fields they computed
(`spread_pct`/`spread_status`/etc.) are carried onto EVERY subsequent dry-run outcome —
the buying-power block, the tradability block, the review block, and the final
`"dry_run"` success — exactly like the live placement path already does. Without this,
`_build_buy_candidates` persisted `spread_status="not_evaluated"` on those outcomes even
though the gate had actually been evaluated and had passed (Codex review: "Preserve
spread verdicts in whole-share dry runs").

The preview also checks `ctx.broker.available_buying_power()` against the (possibly
ceiling-clamped) notional, same as the real placement path's own check — a demo run
must not report success for an order the SAME config in live mode would block for
insufficient funds (Codex review: "Check buying power in whole-share dry-run
previews"). That call is wrapped in try/except too: the buying-power lookup itself can
raise (a transient broker/API outage) during a dry run exactly like the real placement
path's identical call — without this boundary, the exception would escape
`_process_buy_candidates_batch` entirely, aborting the WHOLE demo run and preventing
artifacts for every other, independent candidate still to be processed (Codex review:
"Catch buying-power failures in dry-run previews").

The preview also checks `ctx.broker.is_tradable_for_whole_share_buy` unconditionally,
mirroring how `is_tradable_for_fractional_buy` gates the normal buy path regardless of
`ctx.scheduled` — a ticker that has gone halted/inactive/non-tradeable while quote data
remains available must not preview a plausible `dry_run` success the SAME config in live
mode would actually block (Codex review: "Verify whole-share tradability before scheduled
placement"). That call is wrapped in try/except too: the tradability lookup itself can
raise (a transient broker/API outage) during a dry run exactly like the real placement
path's identical call — without this boundary, the exception would escape
`_process_buy_candidates_batch` entirely, aborting the WHOLE demo run and preventing
artifacts for every other, independent candidate still to be processed (Codex review:
"Catch tradability failures in dry-run previews").

The dry-run preview also calls `ctx.broker.review_whole_share_buy` (read-only,
side-effect-free) regardless of `ctx.scheduled` before reporting success, blocking if it
fails: whole-share is Robinhood-only by design (`AlpacaBroker.place_whole_share_buy`
raises `RuntimeError`), so without this check a dry run configured with an
unsupported broker would fabricate a synthetic order ID and report a plausible
`dry_run` success that the SAME config in live mode would immediately fail (Codex
review: "Reject unsupported brokers in whole-share previews"). That call is a real broker
review/capability request even though nothing gets placed, so it increments
`order_reviews_attempted_inc` — the same counter the normal candidate path increments for
every `review_market_buy` call — so scheduled-demo cost-control telemetry sees it (Codex
review: "Count whole-share dry-run reviews in usage metrics"). That call is wrapped in
try/except: `review_whole_share_buy` itself can raise (a transient broker/API outage)
during a dry run exactly like the real manual-review branch's identical call — without
this boundary, the exception would escape `_process_buy_candidates_batch` entirely,
aborting the WHOLE demo run and preventing artifacts for every other, independent
candidate still to be processed. No ledger restoration is needed (this branch is
read-only — no `record_contribution` call ever ran) (Codex review: "Catch whole-share
dry-run review failures").

A dry run that crosses the share threshold and passes every gate above now also reports
`whole_share_pending_usd`/`whole_share_contribution_usd` on its final `"dry_run"` success
outcome, taken from the SAME `preview` decision (`preview.pending_after_usd`/
`preview.contribution_usd`) the below-threshold branch and the live success path both
already report these fields from. Without this, `_build_buy_candidates` wrote both as
`None` for a successful, threshold-crossing dry run even though `preview` already carried
the simulated carry-forward balance and contribution, leaving demo artifacts unable to
show them (Codex review: "Persist accumulator details for successful dry-run fires").
`whole_share_pending_usd` also adds back any per-order-ceiling clamp shortfall
(`preview.order_notional_usd - preview_order_notional`, after the risk-gates clamp above)
— the raw `preview.pending_after_usd` reflects the PRE-clamp remainder (often `$0.00` for
an exact multi-share preview), while the equivalent live path restores exactly that
clamped-down shortfall during `settle()`; a dry run must report the SAME simulated
carry-forward the identical live run would actually produce, not the unclamped preview's
remainder (Codex review: "Restore clamped credit in dry-run pending previews").

### Budget-tier planning

For a genuine, fresh `Action.BUY` decision with both `finance_signal`/`robinhood_signal`
present, the planned-spend loop now reconciles the execution price the SAME way
`_process_whole_share_candidate` does (see "Risk gates" above) before running the
accumulator preview, instead of using `d.target_price` — the quote from whichever source
`reconcile_signals`'s "agree on BUY" branch happened to pick as the winning signal (by
smaller `suggested_amount_usd`), unrelated to which price `_whole_share_redispatch_reconciled_price`
would actually reconcile to (the HIGHER of the two agreeing quotes; see "Risk gates"
above and "Size whole-share orders using the higher reconciled quote"). With acceptable
but non-identical quotes where the WINNING (lower-cost) signal happens to carry the
LOWER price, the OLD unreconciled preview could see a fire the reconciled, higher
execution price would actually refuse — overstating planned spend right at a budget-tier
threshold and selecting a cap the run's real plan never needed (Codex review: "Reconcile
the tier-planning whole-share price"). A synthetic redispatch decision (`decision.action`
stays `NO_ACTION`) never re-triggers this check — its `target_price` is already the
reconciled price the redispatch loop computed earlier — and a genuine disagreement (or
either source unusable) reconciles to a $0 preview contribution here, matching what
execution would actually do (block outright).

`_run_once_impl`'s planned-spend loop (feeds `select_budget_tier`/`state.select_daily_budget`,
which sets the day's daily/weekly cap) treats a whole-share candidate specially: its own
`suggested_amount_usd` is just TODAY's contribution, not what this run will actually
attempt to spend — below the share threshold it spends $0 (accumulates instead), and
once the accumulated total crosses a share's price, execution attempts the full
share-multiple `order_notional_usd`, which can be many times the day's own contribution.
The loop instead previews the SAME read-only accumulator math the dry-run branch uses
(`WholeShareAccumulationLedger.pending_usd` + `apply_contribution`) so tier selection
reflects what will actually be attempted (Codex review: "Preview accumulated notional
before selecting the budget tier"). Purely a read — makes no ledger writes, and never
affects placement itself, only which tier's daily/weekly cap the run selects. The
preview also applies the SAME kill-switch cap `_process_whole_share_candidate` applies
to the contribution when `kill_switch_engaged` (using `config`'s own pre-batch
`deployed_last_7d_usd`, matching this loop's existing top-up-guard estimate precedent) —
otherwise the preview could overstate planned spend and select a needlessly high tier
when the kill switch would have capped the real contribution (Codex review: "Preview
the kill-switch-capped contribution"). The preview is also clamped to the
`max_per_order_usd` ceiling — execution never spends more than that in one run, however
large the accumulated pending balance, so previewing the full uncapped accumulated
notional could select a needlessly high tier for spend the whole-share path will never
actually submit (Codex review: "Clamp whole-share previews to executable notional"). When
the kill switch is engaged, that ceiling clamp itself is further `min()`-combined with
`decision.selected_base_amount_usd` — the SAME `ceiling_override_usd` execution's
`_whole_share_risk_gates` call now applies (see "Risk gates" above) — not just
`max_per_order_usd` alone: without this, the preview could plan for up to the full
`max_per_order_usd` while execution actually clamps or blocks the fire at the much lower
kill-switch base, overstating planned spend enough to select a needlessly high tier near
a `$129`/`$213`/`$272` escalation threshold and authorize additional, unrelated same-run
orders the real, capped whole-share spend never justifies (Codex review: "Apply the
kill-switch ceiling in tier planning"). The ceiling clamp itself is computed against
`preview.current_share_price_usd` —
`apply_contribution`'s OWN cent-rounded price — not the raw signal price, so it agrees
with the SAME rounded price execution actually clamps with (`wsa_decision.current_share_price_usd`);
at a fractional-cent price the two can otherwise disagree on how many shares fit under the
ceiling and preview the wrong tier (Codex review: "Use the ledger-rounded price in tier
planning"). A corrupt, unreadable, or lock-timed-out BYDDY ledger during this preview is
caught and logged rather than allowed to raise: an unguarded `pending_usd()` call here
would abort tier planning for every OTHER, independent active-ticker candidate in the same
batch over one accumulator's read failure, so a preview failure degrades this ticker's own
contribution to $0 (matching what `_process_whole_share_candidate`'s own identical
ledger-error handling does for the real, authoritative decision) and lets the rest of the
roster plan normally (Codex review: "Isolate ledger preview failures to the whole-share
ticker").

On a same-day retry (e.g. a scheduler re-invocation) for the SAME `(trading_day, stream,
ticker)` slot, `WholeShareAccumulationLedger.cached_fire_decision` tells the preview
exactly what the real, authoritative `record_contribution` call would return RIGHT NOW
for this exact slot, without mutating anything — if it returns a decision, the preview
uses it directly (no `apply_contribution` call, no `planned_spend` contribution from a
fresh computation); only when it returns `None` (the slot has never been recorded at all)
does the preview fall back to `apply_contribution(pending_usd, contribution, price)`.
This replaces an earlier, cruder `already_recorded()` boolean check that always flattened
an already-recorded slot's preview to zero-shares. That was itself wrong for a STALE,
still-unresolved claim (older than `UNRESOLVED_CLAIM_STALE_SECONDS`): the real
`record_contribution` call would reclaim it and return a genuine positive decision, but
the boolean-only check had no way to distinguish that case from a RESOLVED or
still-actively-claimed slot (both of which really do return zero shares), so it
under-reported a preview that the live run would actually act on.
`cached_fire_decision` fixes this by checking the SAME `_claim_is_stale` logic
`record_contribution` itself uses: a resolved or still-fresh-claimed entry previews as
zero shares (mirroring the old, still-correct half of the behavior), while a stale,
reclaimable entry previews the real positive `shares_to_buy`/`order_notional_usd` the live
run would fire (Codex review: "Account for actionable stale claims in tier planning" —
this refines "Mirror cached ledger outcomes in tier previews", which itself refined the
earlier, simpler "Make tier previews honor resolved ledger contributions" fix). The same
`cached_fire_decision`-first, `apply_contribution`-fallback rewiring was applied to the
dry-run preview branch in `_process_whole_share_candidate` too, for the identical reason
and to keep the two previews from drifting back out of sync with each other.

### Accumulator-only run classification

A run where every whole-share BUY candidate is still below its share threshold submits
nothing, but must not be reported as `not_submitted_budget_exhausted` — nothing was
actually budget-blocked; the contribution was cleanly accumulated. This does not need
its own special case: `_apply_no_order_classification` (called right after
`_derive_order_submit_status`, using each candidate outcome's `budget_blocked` flag,
which an accumulating whole-share skip never sets) already reclassifies this to
`no_orders_submitted` with `completion_reason='blocked_before_spread_gate'` and
`is_trading_failure=False` — the same mechanism that protects every other
non-budget-blocked zero-order run. `test_accumulator_only_run_is_not_misclassified_as_budget_exhausted`
in `test_whole_share_wiring.py` locks this behavior in end-to-end (Codex review:
"Classify accumulator-only runs without a budget failure").

### Artifact persistence

A whole-share outcome's accumulator detail (`whole_share_pending_usd`,
`whole_share_contribution_usd`, `whole_share_quantity`) is carried into the persisted
`buy_candidates` row by `_build_buy_candidates`, unconditionally on every row (defaulting
to `None` for a normal ticker) — exactly like `reviewed_order`/`spread_pct` above. It
must be unconditional: `buy_candidates.csv`'s writer derives its header from the FIRST
row's keys, so a key present on only some rows would raise a `csv.DictWriter` error on
any other row, whole-share or normal, depending on row order. Without this,
`run_state.json`/the candidate CSV could report a whole-share ticker as
`"skipped"`/`"submitted"` with no way to see the accumulated pending balance or shares
actually bought (Codex review: "Persist whole-share details in candidate artifacts").

A successful, submitted fire also persists the AUTHORITATIVE post-settlement
`whole_share_pending_usd`/`whole_share_contribution_usd` — read fresh from the ledger
right after `settle()` runs, not the pre-settle `wsa_decision.pending_after_usd` snapshot
captured before it. A per-order-ceiling clamp restores a shortfall back into `pending_usd`
during `settle()` (see "Fire mechanics"), so the pre-settle snapshot can under-report what
is actually left in the ledger after a partial fire; without the fresh re-read, a
successful fire that leaves a remainder (e.g. $15 pending at a $10 share price buys one
share and carries $5 forward) recorded no pending/contribution fields at all, and
`_build_buy_candidates` wrote both as `None` even though a real purchase had just produced
a real cross-day balance (Codex review: "Persist the post-fire accumulator balance"). A
read failure here falls back to the pre-settle snapshot rather than turning an
already-successful, already-published `"submitted"` outcome into an error.

`_failure_candidate_row` — the SEPARATE builder the failure/retry-merge path uses to
synthesize a `buy_candidates` row directly from an outcome, when a LATER exception
prevents the batch loop from ever reaching `_build_buy_candidates` for the remaining
candidates — now also carries `whole_share_pending_usd`/`whole_share_contribution_usd`/
`whole_share_quantity` onto its row, mirroring `_build_buy_candidates`'s own fields.
Without this, an accepted whole-share order followed by an exception while a LATER,
unrelated candidate is processed lost its settlement quantity/pending-balance/
contribution values specifically on a failed-after-submission run, even though the
candidate itself had genuinely succeeded (Codex review: "Preserve whole-share fields in
failure candidate rows").

`planned_amount_usd` (the field every candidate row carries, used by `budget.planned_spend_usd`
downstream) also needs whole-share-specific handling: `decision.suggested_amount_usd`
is only ever TODAY's raw contribution, never what the run actually planned to spend. A
fire blocked by a safety gate (ceiling/cap/spread/review) carries the accumulated
`order_notional_usd` it was about to attempt via the outcome's
`whole_share_planned_notional_usd` field (set by `_preserve_and_block` on the real path
and directly on the dry-run preview's block outcomes); an accumulating-only skip never
sets that field, since nothing was ever planned to spend that run, so `_build_buy_candidates`
correctly falls back to `$0` for it rather than the raw contribution (Codex review:
"Persist the actual whole-share planned notional").

### The unverified assumption: quantity-based orders

`dca_live_adapters.RobinhoodBroker.place_market_buy`/`review_market_buy` only support
dollar-amount ("notional") orders. `place_whole_share_buy`/`review_whole_share_buy`
reuse the SAME injected `place_fn`/`review_fn` callables but pass `quantity=` instead of
`dollar_amount=`, on the assumption that the wired Robinhood
`place_equity_order`/`review_equity_order` MCP tools accept `quantity` as an alternative
order-sizing method — which matches how Robinhood's real order API works, but **has not
been confirmed by any session that has worked on this repo**, since none has had live
Robinhood MCP access at the same time. A live agent session with real broker access
should confirm this (and what the tool actually returns for a non-fractional
instrument) at the first live 3:30 PM run that evaluates a whole-share ticker, before
treating a submitted whole-share order as routinely trustworthy.

## Fire mechanics

- `apply_contribution` is pure: `pending_before + contribution`, divided by
  `current_share_price_usd`, floored to a whole number of shares. Any full share's
  worth is deducted; the remainder (always less than one share's price) stays pending.
- `record_contribution` optimistically applies that deduction — mirroring
  `dca_cumulative.CumulativeCreditLedger.record`'s split of concerns, a caller that
  cannot act on a positive `shares_to_buy` this run (blocked before placement) MUST
  call `preserve_pending` (with the SAME `idempotency_key`) to restore the deducted
  amount, and a caller that successfully places the order MUST call `settle` (also with
  that key) to confirm it and bump `shares_purchased_total`. Until one of those is
  called, the ledger already reflects the shares as bought.
- `idempotency_key` (`_process_whole_share_candidate` uses `f"{stream}-{ticker}"`,
  namespaced under `trading_day` internally) makes a retried `record_contribution` call
  for the same key a no-op that returns a zero-shares decision, NOT the first call's
  decision unchanged, and marks a fire's cache entry `resolved` once `settle`/
  `preserve_pending` runs (or immediately, for a below-threshold contribution that never
  needed either) — a retry AFTER resolution returns `contribution_usd=0.0`, reflecting
  the current balance, rather than re-reporting the ORIGINAL call's positive
  contribution as if THIS retry call had contributed it too. A caller (see
  `_process_whole_share_candidate`) persists this field as "what THIS call contributed"
  for run-artifact reporting, so returning the stale positive amount on a resolved-slot
  retry (e.g. after a below-threshold accumulation, or a blocked fire that was
  preserved) double-reported a contribution that never actually happened on that call
  (Codex review: "Report zero contribution for resolved slot retries";
  `cached_fire_decision`'s read-only preview mirrors the same fix).

  A cache entry currently marked `placed` — the broker has accepted the order via
  `execute_claimed` but `settle` has not yet run — is ALSO treated as unavailable for a
  normal reclaim, exactly like a `resolved` entry: `record_contribution` gates on
  `resolved OR placed`, not `resolved` alone (Codex review discussion_r3804235552,
  "Reject already-placed claims", mirrored here on the `record_contribution` side of that
  fix — see the `execute_claimed` paragraph below for its own half of the same check).
  Reclaiming or redispatching a live, in-flight broker submission here would let a retry
  act on a SECOND fire for the same slot while the first is still being settled,
  double-spending the same `idempotency_key`.

  A RESOLVED-and-NOT-`placed` slot is not always a dead end, though: when
  `preserve_pending` resolved it (a fire blocked by a transient gate, not a genuine
  below-threshold accumulation, and never actually submitted), the full deducted notional
  was already restored back into `pending_usd` — real money still sitting there,
  potentially enough to buy a share on its own. A ZERO-contribution retry for that SAME
  resolved-and-not-placed slot (the ONLY way a real caller reaches this branch with
  `contribution_usd<=0` — see the preserved-fire redispatch loop in `_run_once_impl`,
  "Preserved-fire redispatch" below) now checks whether that restored
  balance ALONE, at the CURRENT price, already covers a share via
  `apply_contribution(pending_usd, 0.0, current_share_price_usd)`; if so, this is a
  genuinely NEW, actionable fire, and the slot is re-opened (`resolved` flips back to
  `False`, a fresh `claimed_at` reservation is taken, and the cache entry is overwritten
  with this new decision) rather than flattened to the "nothing to do" zero-shares
  outcome. Without this, a preserved fire's restored balance was stranded until some
  LATER trading day's signal happened to be a fresh BUY again, with no bound on how long
  that might take — the SAME idempotency slot having already resolved once blocked every
  future zero-contribution redispatch attempt for it, forever (Codex review: "Allow
  same-day redispatch of preserved fires"). A NONZERO `contribution_usd` retry against a
  resolved slot is deliberately left OUT of this fix's scope — re-opening a resolved slot
  for genuinely NEW money risks double-counting the original contribution, a separate
  concern from this narrow redispatch case. `cached_fire_decision`'s read-only preview
  mirrors the same check via its OWN `contribution_usd` parameter (default `0.0`), which
  callers now pass explicitly — the dry-run preview branch passes its own
  `contribution_usd`, and the tier-planning preview passes its own
  `preview_contribution_usd`. An earlier version of this fix assumed the ONLY real caller
  reaching an already-resolved slot always passes zero and checked unconditionally, but a
  genuine fresh `Action.BUY` preview (positive `contribution_usd`) against a resolved slot
  also reaches this branch — e.g. an earlier same-slot BUY resolved below threshold, and
  a LATER same-day fresh BUY is previewed after the price falls enough that the existing
  balance alone would now cover a share. The real `record_contribution` call for that
  exact retry receives the positive contribution and does NOT reopen the resolved slot
  (redispatch is scoped to zero-contribution callers only, per the invariant above), so
  the unconditional preview reported an order the live retry would actually skip, and
  could inflate a tier-planning cap with phantom planned spend (Codex review:
  "Distinguish fresh BUY retries in cached previews").

  A cached fire that is still UNRESOLVED and NOT yet `placed` is reclaimed by a retry
  IMMEDIATELY — there is no wall-clock wait here any more. Earlier revisions of this
  ledger gated this branch on a 5-minute `UNRESOLVED_CLAIM_STALE_SECONDS` wall-clock
  lease (`claimed_at`, set to the caller-supplied `recorded_at`, had to age past that
  window before a second overlapping call could reclaim it); PR #515 ("Fix whole-share
  retry ownership and fill settlement") removed that lease entirely and replaced it with
  ownership enforced by `execute_claimed` (see its own paragraph below): that method holds
  the SAME per-ticker cross-process file lock (`ledger.lock`) across both its final
  ownership check and the actual irreversible broker call — and `record_contribution`
  acquires that IDENTICAL lock path for its own entire reclaim/reprice branch, not a
  separate one. A caller genuinely still live and about to place through `execute_claimed`
  either already holds that lock, in which case a reclaiming `record_contribution` call
  here BLOCKS — waits — until `execute_claimed`'s in-progress placement (the broker call
  plus its own durable `placed`/`placing` write) completes and releases the lock, or, if
  this call acquires the lock first and reclaims the metadata, the ORIGINAL caller will
  itself fail `execute_claimed`'s own ownership check once it gets there (its `claimed_at`
  no longer matches, and a fresh `claim_token` was issued) and abstains without submitting
  anything. Sharing one lock, not two independent ones, is exactly what makes this fencing
  sound: mutual exclusion on the SAME lock guarantees a reclaim and an in-flight placement
  can never interleave mid-attempt, only ever resolve strictly before or strictly after one
  another (an earlier revision of this doc incorrectly described the reclaim as racing past
  `execute_claimed`'s lock rather than blocking on it — corrected per Codex review
  discussion_r3821705226, "Correct the execution-lock reclaim description"). A genuinely
  crashed caller's claim is simply reclaimed on the very next retry (once the OS-level lock
  a crashed process held is released on exit), instead of forcing every retry to wait out a
  fixed lease — which matters because the canonical scheduler's own retry window is shorter
  than the former five-minute lease was. The reclaim decision reports `contribution_usd=0.0` if it turns
  out to defer (see the negative-overdraw guard below) for the same reason the
  resolved-entry branch above does: mirroring `cached_fire_decision`'s own read-only
  preview, which drops the wall-clock staleness gate the SAME way. Reclaiming RE-PRICES
  the fire against the reclaiming caller's OWN `current_share_price_usd`
  argument, not the original attempt's stale cached price — the total dollars already
  committed (`pending_before_usd + contribution_usd` from the ORIGINAL call) is
  unaffected by price, but how many shares that total buys, and how much remains
  pending, changes with a fresh quote. Without this, a fire claimed at $10 and reclaimed
  after the price moved to $20 would still hand back a decision sized for the stale $10
  price, and the caller passes that STALE price straight into `_whole_share_risk_gates`
  for the `max_per_order_usd` ceiling check — letting a one-share order that actually
  costs ~$20 pass a $10-per-order ceiling as if it only cost $10 (Codex review: "Reprice
  reclaimed stale fires before placement"). `cached_fire_decision` (the read-only
  preview both the tier-planning loop and the dry-run branch call) gained an optional
  `current_share_price_usd` parameter for the same reason — a caller with a live quote
  on hand sees the SAME repriced decision a real reclaim would produce for its
  stale-and-reclaimable branch; omitting it (the pre-existing contract) still returns the
  stale cached decision unchanged, for a caller with no live price context. Repricing
  itself reconciles the resulting `pending_after_usd` against the ledger's CURRENT
  balance via a delta (`repriced.pending_after_usd - cached["pending_after_usd"]`),
  mirroring `preserve_pending`'s own established convention just below, rather than
  overwriting `pending_usd` with the repriced value as an absolute snapshot — a LATER
  contribution recorded under a DIFFERENT `idempotency_key` while the first fire's claim
  sat stale must not be silently discarded when that first fire is finally reclaimed and
  repriced (Codex review: "Preserve newer ledger activity while repricing stale claims").
  A reclaim that reprices to `shares_to_buy == 0` (the price rose enough that the
  original committed total no longer buys even one share) now also marks that cache
  entry `resolved`, mirroring the ORIGINAL-decision path's own `"resolved":
  shares_to_buy == 0` rule — the whole committed amount was just folded back into
  `pending_usd` as ordinary balance, so nothing is left for any caller to place or
  settle. Left unresolved, a SECOND stale reclaim of the same key would recompute
  `apply_contribution` from the SAME frozen `pending_before_usd`/`contribution_usd`,
  treating those dollars as still owed even after a DIFFERENT key had since spent the
  restored balance down — driving `pending_usd` negative (Codex review: "Prevent old
  claims from re-spending consumed credit"). A reclaim landing on a POSITIVE
  `shares_to_buy`, in contrast, is a genuine actionable fire that still needs a caller to
  place and settle/preserve it, so it correctly stays unresolved. A DOWNWARD reprice
  (the price dropped) has its own, opposite hazard: the reclaim's total is recomputed
  from the ORIGINAL frozen `pending_before_usd`/`contribution_usd`, which assumes the
  full original remainder is still exclusively this claim's — but if a DIFFERENT key
  already spent part of that shared pool (its own fire, funded in part by this claim's
  original remainder), only what is CURRENTLY unspent is actually safe to draw further
  from. Applying the delta anyway could drive `pending_usd` negative and hand back an
  actionable decision that spends more than what remains (e.g. a $15 fire at $10 leaves
  $5 pending; a different key's $7 contribution buys another share and leaves $2;
  reclaiming the first fire at a DROPPED $5 price recomputes the frozen $15 total as 3
  shares/$0 remainder — a $-5 delta that would leave `pending_usd` at $2 + (-5) = $-3)
  (Codex review: "Prevent downward repricing from overdrawing pending credit"). The fix
  defers instead: when the delta would drive `pending_usd` negative, the reclaim
  attempt returns the same "nothing to do" decision, leaving the claim exactly as-is
  (unresolved, untouched) for a later retry once the pool holds enough balance again.
  `cached_fire_decision`'s own read-only preview of a repriced reclaim mirrors this same
  deferral, so a preview never promises an over-optimistic reclaim the real call would
  actually defer. The RETURNED decision's `pending_after_usd` is the RECONCILED
  `state["pending_usd"]` just persisted, not the isolated `repriced.pending_after_usd` --
  those two differ by exactly `pending_delta` whenever a different key changed the
  balance since this claim was recorded (e.g. the $19-reconciliation example above: the
  isolated repriced remainder alone is $15, but the persisted, reconciled balance is
  $19). A caller (see `_process_whole_share_candidate`) persists this field as
  `whole_share_pending_usd` when the reprice yields 0 shares, so returning the
  pre-reconciliation value reported a stale balance to run artifacts even though the
  ledger itself already held the correct, reconciled one (Codex review: "Return the
  reconciled pending balance after a stale reprice"). `cached_fire_decision`'s own
  reprice-preview branch had the SAME gap independently: it computed `repriced` but
  returned `repriced.pending_after_usd` directly rather than reconciling it against the
  read-only `pending_after` (this ledger's current balance, read fresh at the top of the
  call) the same way — for the exact $15-fire/$4-later-contribution/$19-reconciled
  example above, it kept reporting the stale $15 in a dry-run preview even after
  `record_contribution`'s own fix started correctly returning and persisting $19 on a
  real reclaim. Fixed the same way: `pending_after + (repriced.pending_after_usd -
  cached["pending_after_usd"])` (Codex review: "Return the reconciled balance from
  stale-claim previews").
- `preserve_pending` reconciles the restored amount against the ledger's CURRENT
  balance (read fresh under its own lock), not an absolute snapshot — a concurrent
  contribution recorded between the fire and its restoration is never clobbered.
- `settle`, given `idempotency_key`, automatically restores any shortfall when fewer
  shares were purchased than planned (e.g. a downstream clamp) — a caller does not need
  a separate `preserve_pending` call for the unfilled portion. The shortfall is
  `(planned_shares - shares_purchased) * cached_price_usd`, computed from the CACHED
  share count and the ledger's OWN cent-rounded `current_share_price_usd`, deliberately
  NOT from `submitted_notional_usd` minus the cached planned notional: a caller
  computing `submitted_notional_usd` from a raw, un-rounded share price would otherwise
  corrupt the shortfall by the rounding difference (e.g. $20 pending at a raw $0.996
  price deducts 20 shares at the ledger's rounded $1.00; clamping to 10 shares must
  restore exactly $10.00, not a raw-price-derived $10.04). `submitted_notional_usd` is
  still recorded on the audit event, just no longer used for this math (Codex review:
  "Restore clamped credit using the ledger price"). To keep every caller on the SAME
  price basis in the first place, `_process_whole_share_candidate`'s own ceiling-clamp
  math (both the real path and the dry-run preview) now clamps using the fire's cached
  `current_share_price_usd`, not the raw signal price, for the same reason.
- **A `settle()` call that itself fails is the ONLY thing standing between a clamped fire
  and a durably stranded shortfall.** `settle()` is a single atomic write; if it raises
  (a lock timeout, corrupt ledger, or unwritable disk), NOTHING it would have done
  persists — the shortfall a per-order-ceiling clamp left behind is never restored to
  `pending_usd`, `shares_purchased_total` is never bumped, and the cached claim is never
  marked resolved. Unlike the fully general "crash between `record_contribution` and
  `settle`" residual risk (see "Known, accepted residual risk" below), this failure is
  detected SYNCHRONOUSLY, in the same process, immediately after a CONFIRMED successful
  placement — but the practical outcome is nearly as bad: a same-day retry for this same
  `idempotency_key` exits at the broker-duplicate check before ever touching the claim
  again (a real order genuinely exists now), and the next trading day's differently-keyed
  `dedup_key` never revisits it either. `_process_whole_share_candidate` still reports
  `"submitted"` (a real order really was placed — the caller must never claim otherwise),
  but now also computes the shortfall directly (`planned_order_notional - order_notional`,
  mirroring `settle`'s own math) and persists it onto the outcome as
  `whole_share_settlement_reconciliation_needed`/`whole_share_settlement_shortfall_usd` —
  both carried through `_build_buy_candidates` and `_failure_candidate_row` unconditionally,
  like every other `whole_share_` field — and logs at `ERROR` (not `WARNING`) rather than
  relying on a log line alone to surface a real-money discrepancy. An operator can then
  reconcile the stranded shortfall by hand via this ledger's own tooling (Codex review:
  "Preserve clamped shortfalls when settlement fails").
- `settle`'s `quantity` argument is the SUBMITTED/requested quantity the broker
  accepted, not a broker-confirmed FILL quantity (Codex review: "Settle only the
  quantity actually filled", evaluated and NOT applied) — deliberately, to match the
  existing convention every other ledger in this codebase already uses:
  `ctx.state.record_buy()` and `ctx.credit_ledger.settle()` in the normal (fractional)
  buy path likewise record the SUBMITTED notional the instant the broker accepts the
  order, not a later-reconciled fill amount. Broker acceptance is this codebase's
  deliberate "spend" event for budget/ledger accounting; actual fill data (price,
  quantity, timing) is a SEPARATE, best-effort observability concern handled by
  `_log_execution`'s `get_order_fill` lookup feeding `orders.csv`/slippage reporting,
  not the ledger. Changing this for whole-share only would make it diverge from every
  other ledger in the codebase rather than fix a whole-share-specific bug.
- `current_share_price_usd` must be strictly positive after rounding to the cent;
  `apply_contribution` raises rather than silently stalling accumulation on a bad or
  sub-cent price.
- `place_whole_share_buy` raising `OrderConfirmationRequiredError` (review passed but the
  broker requires a separate post-review confirmation step) is caught in its own branch,
  BEFORE the generic placement-failure handler — mirroring the normal buy path's own
  identical branch. This is an expected hold, not a placement failure: it does not count
  toward `errors_count_inc`, and the candidate is classified
  `"not_submitted_confirmation_required"`, not `"failed"` (which could otherwise trigger
  failure recovery/escalation for something that isn't an error). The deducted pending
  balance is still restored via `preserve_pending`, since no order was actually placed
  (Codex review: "Handle confirmation-required whole-share placements distinctly").
- A `settle()` failure (e.g. a disk/lock error after the broker has already accepted the
  order) is caught and logged as a warning rather than propagated — losing the ledger
  update would be a bug, but re-raising after a broker-accepted order would incorrectly
  report the fire as failed/unsubmitted when money has already moved. The candidate's
  `"submitted"` outcome (and its `_log_execution` entry, feeding slippage/execution
  reporting exactly like a normal buy) is unaffected by a settlement failure.
- The accepted `"submitted"` outcome is published via `_publish_in_flight_outcome`
  BEFORE `_log_execution` runs — mirroring the normal buy path's own identical ordering
  — so a raise from `_log_execution`'s fallible `get_order_fill` lookup can never lose a
  real, already-placed order from the failure artifact (Codex review: "Publish the
  accepted order before fill lookup").
- `RunResult.active_universe` (used by `build_report`'s `missing_fills` coverage loop)
  includes `whole_share_evaluated_tickers` alongside the normal tradable universe. Without
  this, an accepted-but-unfilled whole-share order — a real, still-open submitted order,
  present in `execution_records` — would silently disappear from missing-fill coverage
  because `build_report` only ever walked the normal active universe (Codex review:
  "Include unfilled whole-share orders in coverage reporting").

## State

Persisted per ticker (not per trading day — a share's worth is usually many days'
contributions) at `stream_root("whole_share_accumulation")/<TICKER>/ledger.json`, via
`WholeShareAccumulationLedger`. Each ledger tracks `pending_usd`,
`shares_purchased_total`, and an append-only `events` history for audit.

Every mutating branch of `record_contribution` appends to that `events` history — including
the stale-claim reclaim/reprice branch (see "Concurrency" below), which mutates the cached
quantity, notional, resolution state, and the global `pending_usd` exactly like the
fresh-contribution branch does. That reclaim branch originally persisted the mutation
silently, without an event, so `events()`/the on-disk audit trail retained only the
ORIGINAL stale fire and gave an operator no record explaining the later balance adjustment
or repriced decision — incorrect precisely for the recovery path a reclaim represents
(Codex review: "Record stale-claim repricing in the ledger event history"). Its event's
`"decision"` field reads `"reclaim_reprice"` (distinct from a fresh entry's `"pending"`/
`"fire"`) so an operator scanning the history can tell a reclaim from an ordinary
contribution at a glance.

Every method resolves `ticker` to a path component (`self.root / symbol / "ledger.json"`)
after `.strip().upper()`. `StrategyConfig.whole_share_tickers` is NOT schema-validated at
every entry point — an alternate `$DCA_CONFIG_DIR`'s `tickers.json` or an explicit
`whole_share_tickers=` keyword override both bypass `schemas/tickers.schema.json`'s
`"^[A-Z.]{1,6}$"` pattern entirely — so a malformed symbol (e.g. `"../../outside"`) could
otherwise resolve OUTSIDE this ledger's own artifact tree, writing durable state at an
unintended location. Every method now also rejects a symbol that doesn't match that same
canonical pattern (`_SYMBOL_PATTERN`), widening each method's existing blank-ticker guard
rather than adding a separate check (Codex review: "Validate whole-share symbols before
using them as paths"). `execute_claimed` (added later, alongside PR #515's own execution
lock) originally only rejected an EMPTY symbol, not an invalid one — a caller passing an
unvalidated ticker (e.g. `idempotency_key=None`, which skips the dedup_key-scoped
ownership checks entirely) could still reach `self.root / symbol / "ledger.lock"` with a
path-traversal-shaped value and have `cross_process_lock` create that parent path outside
the whole-share stream tree, running `operation()` with no claim validation at all. Now
validated against `_SYMBOL_PATTERN` up front, before any path is constructed or the lock
is acquired — matching every other public method (Codex review discussion_r3821705216,
"Validate ticker before acquiring the execution lock").

## Concurrency

This ledger relies on `record_contribution`'s idempotency key plus the same
cross-process file lock `dca_cumulative.py` uses (`scripts.file_lock.cross_process_lock`),
PLUS `claimed_at`/`claim_token` ownership tracked on the cached entry itself. This is the
"Fire mechanics" section's own reclaim rule read from the ownership angle: a SECOND,
overlapping `record_contribution` call for the same idempotency key against an entry
that is `resolved` OR `placed` gets a zero-shares "nothing to do" decision rather than
the same actionable fire; otherwise (unresolved, not yet placed) it reclaims the entry
IMMEDIATELY, no wall-clock wait, replacing `claimed_at` and minting a fresh `claim_token`.

An immediate metadata reclaim, on its own, only proves a NEW caller is free to TRY —
it does not by itself stop the ORIGINAL caller from later placing an order it no longer
exclusively owns if a reclaim happened in the gap between that caller's own
`record_contribution` call and its eventual placement attempt (Codex review: "Fence the
previous owner before reclaiming stale fires" — the underlying hazard this rule guards
against predates and survives PR #515's removal of the wall-clock lease; only the
TRIGGER for a reclaim changed, from "elapsed time" to "immediate"). Two closing layers
handle this, in order:

1. **`WholeShareAccumulationLedger.still_claimed`** — `_process_whole_share_candidate`
   presents its own `claim_token` immediately before the one truly irreversible step
   (placement). If a reclaim happened before this check runs, the persisted token now
   belongs to the new caller, so the original caller's check fails and it abstains
   without touching the ledger — the reclaiming caller owns the SAME single deduction
   already made, so preserving it here would incorrectly restore money a still-viable
   attempt may yet spend. This narrows the race but cannot close it fully: both callers
   could still be mid-placement simultaneously if a reclaim happens AFTER this check runs
   but before the broker round-trip completes.
2. **`WholeShareAccumulationLedger.execute_claimed`** closes that remaining gap (PR #515,
   "Fix whole-share retry ownership and fill settlement"): it holds the SAME per-ticker
   cross-process file lock across BOTH a final ownership re-check (now ALSO rejecting an
   entry already marked `placed`, on top of the pre-existing `resolved`/`claimed_at`
   checks — Codex review discussion_r3804235552, "Reject already-placed claims inside the
   execution lock", needed because two overlapping calls sharing an identical
   caller-supplied `recorded_at` can otherwise both pass a `claimed_at == recorded_at`
   comparison) AND the actual broker placement call itself. `record_contribution`'s own
   reclaim branch acquires this SAME per-ticker `ledger.lock` path, not an independent
   one, so the two methods are mutually exclusive: a caller that is genuinely still live
   and about to place through `execute_claimed` either already holds that lock, in which
   case a concurrent reclaim attempt via `record_contribution` BLOCKS — waits for the
   entire in-progress placement (the broker call plus the durable `placed`/`placing`
   write) to finish and the lock to release — before it can even read the current
   metadata, or, if a reclaim acquires the lock first and updates the metadata, this
   caller's OWN ownership check inside `execute_claimed` fails once it gets there and it
   abstains — so two callers can never both reach `operation()` (the broker call) for the
   same claim, and a reclaim can never observe or act on a mid-flight placement (an
   earlier revision of this doc incorrectly said the reclaim does not block here; corrected
   per Codex review discussion_r3821705226, "Correct the execution-lock reclaim
   description"). Once `operation()` succeeds, `execute_claimed` marks the entry `placed`
   before releasing the lock, so a retry that was blocked on the lock sees `placed=True`
   and receives no actionable fire at all once it re-enters `record_contribution`.

   This same shared lock is also what makes the stale-claim recovery pass
   (`_reconcile_unresolved_whole_share_claim`) safe against a placement that lands in the
   gap between its broker-history lookup and its eventual `preserve_pending`/
   `preserve_pending_if_claimed` call: that lookup itself runs WITHOUT holding the lock (it
   is a network round-trip), so a live owner's `execute_claimed` call can complete an
   entire real placement — including its durable `placed` write — in that gap. Both
   `preserve_pending` and `preserve_pending_if_claimed` now re-check `cached["placed"]`
   under their own lock acquisition before restoring anything (Codex review
   discussion_r3821705222, "Preserve a placement that races stale-claim restoration"): by
   the time either call acquires the lock, any `execute_claimed` call that started is
   either fully done (`placed` durably `True`) or crashed with `placing` left `True` and
   the lock released — never mid-flight, because the lock they share rules that out. A
   `placed=True` seen under the lock means a real order landed in the race window, so the
   restoration is skipped and the claim is left for normal settlement/reconciliation
   instead of being wrongly cleared for redispatch. Neither method also gates on `placing`
   alone: an in-line caller reacting to its OWN `execute_claimed` call raising (e.g. a
   confirmation-required response) legitimately reaches `preserve_pending` with `placing`
   still durably `True` and must be able to clear it — that is a known, already-resolved
   outcome for the SAME attempt, not a racing observer's guess.

`execute_claimed` also isolates the broker call from the subsequent ledger write:
if `operation()` succeeds but persisting the `placed` marker afterward raises, it raises
a distinct `WholeShareClaimPersistenceError` (carrying the already-accepted result)
rather than letting that write failure look identical to an outright submission failure
— `_process_whole_share_candidate` catches it separately and treats the order as
accepted (keeping the order id, NOT calling `preserve_pending`) instead of falling into
the generic placement-failure branch, which would otherwise restore a deduction for an
order that is actually live at the broker (Codex review discussion_r3804235587,
"Preserve broker acceptance when persisting `placed` fails").

`preserve_pending` and `settle` enforce the SAME check themselves, not just the
placement-time `still_claimed` call: a caller passing `idempotency_key`/`claim_token`
whose cache entry is already `resolved`, or whose token no longer matches the
entry's token, gets a safe no-op (the current balance, unchanged) instead of
crediting money back — otherwise a fenced-out caller reaching one of the EARLIER blocks
(buying power, ceiling, cap, spread, review — all of which call `preserve_pending`
before the placement-time `still_claimed` check even runs) could still double-credit the
pool once the reclaiming attempt separately settles or preserves the same fire (Codex
review: "Reject resolutions from callers that lost the claim"). This check only covers
the record-to-placement window; it does NOT defend against two genuinely DIFFERENT
idempotency keys (e.g. two different streams) both
reading a pre-fire balance and both deciding to fire in the same instant, the way
`dca_inverse_martingale.py`'s `claim_fire()` compare-and-swap does for its own
single-key case. This remaining gap is an accepted one, not an oversight: only ONE
stream (the 3:30 PM final buy, see "Scope" above) ever evaluates a whole-share ticker
today, so there is no genuine caller with a DIFFERENT key that could race. Closing it
fully (a `claim`/`settle` compare-and-swap like inverse-Martingale's) is straightforward
future work if and when a second entry point is wired in.

`_process_whole_share_candidate`'s real placement branch also re-checks
`ctx.broker.same_day_strategy_order_exists` a SECOND time, immediately before
`place_whole_share_buy` (in addition to the top-of-function check and `still_claimed`) —
if a reclaiming attempt's own order has already landed at the broker by then, this
caller aborts instead of submitting a second live order (Codex review: "Hold the claim
through the broker placement", partially addressed).

### Known, accepted residual risk: the placement window itself is not lockable

Two related Codex findings on this same seam remain intentionally unresolved, because a
full fix requires either unverified broker behavior or a design change with real
operational cost, not a bug fix:

- **"Hold the claim through the broker placement."** `still_claimed()` and the
  same-day-order recheck above both narrow the reclaim race but cannot close it
  completely: two callers could still both be *mid-placement* at the same instant, and
  neither recheck can observe the other's in-flight call. The provably correct fix is
  holding a SINGLE cross-process lock across `still_claimed()` → `place_whole_share_buy`
  → `settle()`, using the same per-ticker `ledger.lock` file `record_contribution()`
  itself acquires — a genuinely reclaiming caller's own `record_contribution()` call
  would then simply block on that lock until the original's critical section releases
  it, closing the race entirely. This was deliberately NOT done: it means holding a
  cross-process file lock across a broker network round-trip, which conflicts with this
  module's own stated design ("deliberately contains only deterministic math plus a
  durable ledger... callers remain responsible for their own... broker checks" — see the
  module docstring) and would block any OTHER legitimate holder of that same lock file
  for however long the broker call takes (`scripts/file_lock.py`'s default 30s
  `cross_process_lock` timeout would then start rejecting other callers during a slow
  broker response). The alternative that avoids holding a lock across I/O — a
  broker-side idempotent `ref_id` (the deterministic
  `_graded_dca_ref_id_prefix(stream, "wholeshare")-{trading_day}-{ticker}` this module
  already generates) that the Robinhood MCP order-placement tool itself deduplicates on
  — is exactly the kind of behavior flagged "**Unverified in this repo**" below: nobody
  who has written this module has had live Robinhood MCP access to confirm it.
- **"Recover fires before the scheduled retry window closes."** A crash between
  `record_contribution()` and `settle()`/`preserve_pending()` leaves that fire's
  deduction stranded: `UNRESOLVED_CLAIM_STALE_SECONDS` (5 minutes) assumes a same-day
  retry will eventually reclaim it, but the canonical 1530ET `run_slot()` invocation
  window (see `run_dca.py`) does not retry within the same day, and the NEXT day's run
  uses a different `trading_day:idempotency_key` dedup key that never revisits the
  orphaned entry. Auto-recovering it safely requires resolving a genuine ambiguity a
  crash creates that this ledger's own local state cannot answer: did the crash happen
  BEFORE `place_whole_share_buy` (safe to restore the deduction to `pending_usd`) or
  AFTER it succeeded but before `settle()` ran (a real order exists; restoring the
  deduction would let a later run spend it a second time)? Answering that requires
  querying the broker's actual order history for that stranded day/ticker/ref_id —
  broker-aware cross-day reconciliation this module's ledger deliberately does not do
  (see the module docstring) and a real follow-up feature, not a quick fix.

Both gaps share the same practical exposure: they require either a genuine process
crash mid-placement, or an operator manually re-invoking a whole-share evaluation while
an earlier attempt is still in flight or crashed — not the automated single-daily-run
canonical flow (only one scheduled 1530ET call evaluates a whole-share ticker per day;
see "Scope" above). An abandoned unresolved cache entry is always visible for manual
investigation via `ledger.json`'s `recorded_idempotency_keys`/`events` — this is a
residual risk to be aware of during a manual/operator-driven run or an incident
postmortem, not a silent failure mode.

### Idempotent settlement, stale-claim release, and 7-day-window booking

Continued hardening of the same reconciliation seam turned up several more edge cases:

- **`settle()` is now idempotent against its own return value.** It returns `bool` —
  `True` only when THIS call actually transitioned the cache entry from unresolved to
  resolved, `False` for a no-op (fenced-out caller, already-resolved entry, or a
  `recorded_at` mismatch). `_reconcile_unresolved_whole_share_claim` checks this return
  value before treating a claim as settled and returning its `{order_id,
  shares_purchased, order_notional_usd}` result — a `False` return means some OTHER
  caller already resolved this exact entry (or fenced this one out) between the claim
  lookup and the `settle()` call, and returning a result anyway would double-book the
  SAME broker order into `ctx.state`/budget totals a second time (Codex review: "Make
  recovered-claim settlement idempotent").
- **A confirmed non-match on a STALE claim releases its reserved credit.** Previously, a
  claim whose `ref_id` lookup cleanly returns no match (as opposed to raising) was simply
  left alone forever, even once it had aged well past `UNRESOLVED_CLAIM_STALE_SECONDS` —
  a rejected/cancelled order that never landed at the broker kept its reserved pending
  credit stranded indefinitely, with no automatic path back into the ladder.
  `_reconcile_unresolved_whole_share_claim` now checks `_whole_share_claim_is_stale`
  (mirroring `dca_whole_share_accumulator`'s own module-private staleness math against
  the same `UNRESOLVED_CLAIM_STALE_SECONDS` constant) on a clean no-match, and calls
  `ledger.preserve_pending` to restore the claim's own `order_notional_usd` back into
  `pending_usd` when it is. A no-match that is NOT yet stale is left reserved, unchanged
  — the order could still be in flight at the broker and a later run's lookup may yet
  find it.
- **Reconciling one unresolved claim no longer starves the others.** The cross-day
  lookup (`ledger.unresolved_claims_any_day`) and the per-claim reconciliation call
  (`_reconcile_unresolved_whole_share_claim`) are now each wrapped in their OWN
  try/except inside `_process_whole_share_candidate`'s cross-day loop — a broker-lookup
  failure or exception on one claim (e.g. an older, permanently irreconcilable entry) no
  longer aborts the whole loop and silently skips every claim after it; the loop moves on
  to the next claim, logs a warning, and keeps going.
- **A reconciled cross-day order is only counted toward TODAY's kill-switch total when
  it falls inside the trailing 7-day deployment window.** `_book_reconciled_whole_share_order`
  now only adds the reconciled order's notional into `running_deployed_7d` when
  `0 <= (ctx.trading_day - claim_trading_day) < KILL_SWITCH_WINDOW_DAYS` — booking an
  order reconciled from well outside that window (e.g. a months-old orphaned claim
  finally resolved) into the CURRENT run's rolling 7-day deployment total would inflate
  today's kill-switch accounting for spend that happened long ago and is already outside
  the window `kill_switch_engaged` actually evaluates (Codex review: "Only count
  reconciled orders toward the current deployment window"). The order is still recorded
  against `ctx.state`/execution artifacts and the ledger regardless of window — only the
  `running_deployed_7d` contribution is gated.
- **The pre-placement duplicate check now verifies the exact order before skipping.**
  The second, pre-placement `same_day_strategy_order_exists` recheck (see "Concurrency"
  above) previously skipped placement outright on ANY same-day order match — including
  one for a completely unrelated instrument or stream sharing the same broad "some order
  exists today" signal. It now runs as a two-step check: the original broad lookup stays
  the fast-path trigger (unchanged when no duplicate exists, so the common case makes no
  extra broker call), but when it returns `True`, a second lookup via
  `same_day_strategy_order_exists_with_ref_id_prefix` verifies the match is THIS claim's
  own deterministic `ref_id` before honoring the skip. A verification-lookup failure
  fails closed via `_preserve_and_block` with its own skip reason (`"whole-share:
  duplicate ref_id verification failed: ..."`), never silently falling back to the broad
  check's original (possibly unrelated) match.
- **Preserved-fire redispatch now also fires for a ticker with no pending balance but an
  unresolved claim.** The NO_ACTION redispatch eligibility loop (see "Preserved-fire
  redispatch" above) originally only checked `pending_usd` via `apply_contribution`. It
  now falls back to `ledger.unresolved_claims_any_day(ticker, idempotency_key)` when the
  balance-only check finds nothing actionable — an unresolved claim from an earlier
  attempt (not yet reclaimed/reconciled) should still get a chance to redispatch and
  reconcile even though its own reservation already deducted `pending_usd` down to a
  non-actionable balance.
- **A reconciled claim already recorded on disk is never rebooked.** Two other branches in
  this module (the ambiguous-placement-recovery reconciliation, and the ordinary in-line
  placement path) both book `ctx.state.record_buy`/`own_orders_recorded` and write a
  `"submitted"` execution artifact for a broker order the moment it is CONFIRMED — even
  when the ledger's own `settle()` call then raises and leaves the claim itself
  unresolved. A later run's cross-day reconciliation can find that same still-unresolved
  claim, settle it successfully this time, and reach `_book_reconciled_whole_share_order`
  — rebooking the SAME already-persisted order a second time would double-count it in
  both that run's own daily/weekly caps and the persisted deployment history a future
  run's kill switch sums from disk. `_book_reconciled_whole_share_order` now checks
  `_same_day_ticker_deployment_already_recorded(ticker, claim_trading_day_date,
  ctx.deployment_history_root)` — the SAME on-disk evidence check the same-stream
  duplicate-preserved path already uses for an identical reason (see the "same-stream
  duplicate" note near `own_orders_recorded`'s own definition) — before booking, and skips
  the booking entirely (while the ledger settlement itself still stands) when a prior
  run's artifact already recorded this exact ticker/day as submitted (Codex review: "Avoid
  rebooking orders after a settlement-only retry").

### Terminal-state gating at the immediate placement failure site, and partial fills before a cancellation

A further review round found the SAME terminal-state gap at a second call
site, plus a lookup-scope gap in the stale-claim release added above:

- **The immediate placement-failure branch now also requires a terminal
  match.** `_process_whole_share_candidate`'s own `place_whole_share_buy`
  exception handler (distinct from `_reconcile_unresolved_whole_share_claim`,
  which only runs on a LATER reconciliation attempt) reconciles via the same
  ref-id lookup immediately, in-line, right after the exception. It
  previously accepted ANY truthy matched order -- including a still-live,
  working one (`"new"`/`"queued"`/`"confirmed"`/`"partially_filled"`) -- and
  settled it immediately using the full REQUESTED quantity. If the working
  remainder later cancels or is rejected, the claim was already marked
  resolved with no unresolved entry left to revisit, permanently
  overstating purchased shares while withholding the unfilled credit
  forever -- the identical hazard `_reconcile_unresolved_whole_share_claim`
  already guards against, just unguarded at this second call site (Codex
  review: "Keep nonterminal ambiguous orders reserved"). A non-terminal
  match here is now left reserved (neither settled nor restored) and
  reported as a `"blocked"` outcome, exactly mirroring the deferred-
  settlement behavior described above -- a later reconciliation attempt
  (same-day duplicate check or cross-day recovery) picks it up once the
  order actually terminates.
- **A partial fill followed by a cancellation is no longer indistinguishable
  from "never happened."** `same_day_strategy_order_exists_with_ref_id_prefix`
  is deliberately scoped to LIVE order states only (`_LIVE_ORDER_STATES`'s
  own comment: a cancelled/rejected order "never consumed capacity"). That
  is the right question for the ordinary same-day duplicate check, but the
  wrong one for reconciling an AMBIGUOUS placement: an order that partially
  filled and was THEN cancelled is real, already-executed capital, yet this
  lookup reports it as a clean "no match" -- indistinguishable from an
  order that never reached the broker at all. The stale-release logic
  above would then restore the FULL planned notional, silently discarding
  the shares that genuinely executed (Codex review: "Reconcile partial
  fills before releasing canceled claims"). A new broker adapter method,
  `same_day_order_with_ref_id_prefix_any_state` (implemented alongside
  `same_day_strategy_order_exists_with_ref_id_prefix` on every
  `BrokerAdapter` -- `DryRunBroker` returns `None`, `AlpacaBroker` fails
  closed with the same "no client-order-id wired" `RuntimeError` its
  live-only sibling already raises, `RobinhoodBroker` scans same-day orders
  WITHOUT the `_LIVE_ORDER_STATES` filter), is now checked on a clean
  live-lookup no-match, before falling through to the staleness check:
  a positive `cumulative_quantity`/`quantity` on the terminal match is
  settled for real (via a new shared `_settle_whole_share_claim` helper),
  and a confirmed terminal match with NOTHING filled is released
  immediately via `preserve_pending` -- no need to wait for staleness,
  since the terminal confirmation is itself definitive (unlike the
  "found nothing at all" case, which cannot rule out the order still being
  in flight somewhere this lookup can't see).
- **The booked notional uses the broker's own fill price, not the stale
  cached signal-time price.** `_settle_whole_share_claim` now accepts an
  optional `fill_price_usd`, sourced from the matched order's own
  `average_price` field (validated by a small `_usable_positive_float`
  helper) when present and positive. A quantity-based market order can fill
  above or below the price that was quoted when the fire was originally
  planned, so booking the reconciled notional at the stale cached price
  into `ctx.state`/deployment history/the kill-switch total would record a
  different dollar amount than what the broker actually spent (Codex
  review: "Use the recovered fill price when booking deployment"). The
  cached price remains authoritative for `settle()`'s OWN shortfall math
  (comparing planned vs. purchased SHARE counts, never dollars) -- only the
  returned/booked notional changes.

### Two more independently-flagged config/tooling gaps

- **`config/scheduled_tasks.json`'s `preserved_trading_safety_rules`** still
  said "excludes BYDDY" after BYDDY moved from `tickers.json`'s `excluded`
  into its `whole_share` category — this preserved-rules block is exactly
  the kind of manifest an external scheduler/agent consults to decide what
  the active universe is, and the SAME manifest's own
  `ticker_universe_policy.summary` already correctly says BYDDY is actively
  evaluated by the 3:30 PM whole-share path — leaving both statements in
  the same file let a consumer that only reads the preserved rule omit
  BYDDY from the newly wired flow entirely (Codex review: "Stop labeling
  BYDDY as excluded in the current safety rules"). Updated to describe the
  whole-share category explicitly rather than calling it excluded.
- **`scripts/backup_strategy.py`'s `write_configs`** required a nonempty
  extracted `active` list before merging `tickers.json` at all — a
  transcript containing ONLY a category update (e.g. `"Whole-share:
  (none)"`, with no `"Current active ticker universe"` section) is a
  supported partial-transcript shape, but that outer gate skipped the
  merge entirely, leaving the stale category untouched even though
  `whole_share_mentioned`/`excluded_mentioned` correctly identified a
  deliberate clear (Codex review: "Allow category-only transcript
  restores"). The outer condition now also merges when either `*_mentioned`
  flag is set, even with an empty extracted active list — the merge
  payload's own `active` key still goes through the ordinary empty-value
  filter (it's never in `preserve_empty_list_keys`), so an unmentioned
  active universe is still left completely untouched.

### Extending terminal-order recovery to the immediate placement-failure site, and fail-closed on a raised terminal lookup

A further review round found the SAME terminal-inclusive coverage gap at
the immediate (in-line) placement-failure branch, plus a fail-open bug in
the terminal-lookup exception handling added just above:

- **The immediate placement-failure branch now also checks the
  terminal-inclusive lookup.** `_process_whole_share_candidate`'s own
  `place_whole_share_buy` exception handler (see "Terminal-state gating at
  the immediate placement failure site" above) only checked the live-only
  ref-id lookup before falling through to `preserve_pending`. An order that
  PARTIALLY filled and was THEN cancelled/rejected before that live-only
  lookup ran is invisible to it (the same gap
  `_reconcile_unresolved_whole_share_claim`'s own no-match branch already
  closes), so the branch would restore the FULL planned notional and
  silently discard the shares that genuinely executed (Codex review: "Check
  terminal orders before restoring failed placements"). When the live-only
  lookup finds nothing, the branch now also checks
  `same_day_order_with_ref_id_prefix_any_state`: a positive filled quantity
  is settled for real (using the broker's own `average_price` when usable,
  same as the cross-day path), and a confirmed zero-fill terminal match
  falls through to the ordinary restore branch unchanged (restoring the
  full planned notional is correct when nothing executed).
- **A raised terminal-inclusive lookup no longer masquerades as a clean
  no-match.** The stale-claim-release fix's own terminal-inclusive lookup
  (see "Reconcile partial fills before releasing canceled claims" above)
  originally caught ANY exception from that lookup and set the local
  `terminal_order` to `None` -- indistinguishable, to the code right after
  it, from a lookup that genuinely SUCCEEDED and found nothing. For a
  claim old enough to be stale, that fallthrough would then release the
  ENTIRE reservation via the ordinary no-match staleness logic, even
  though the order may have genuinely partially filled -- a transient
  failure on this second history request must not be allowed to look like
  definitive proof of rejection (Codex review: "Preserve claims when
  terminal-history lookup fails"). A raised lookup now returns
  immediately, leaving the claim exactly as `record_contribution` left it,
  mirroring how the live-only lookup's own raised exception is already
  handled by this function's caller.
- Both fixes reuse the SAME `same_day_order_with_ref_id_prefix_any_state`
  broker adapter method and the SAME `_usable_positive_float`-validated
  `average_price` preference introduced for the cross-day/same-day-check
  reconciliation path -- no new broker surface was needed, only two more
  call sites wiring the existing terminal-inclusive lookup in correctly.

### Six more edge cases: shortfall pricing, ledger-audit idempotency, non-finite prices, requested-vs-filled quantity, real fill prices at the immediate site, and a batch-wide pre-pass

An eighth review round on the same reconciliation subsystem found six more
issues, spanning the whole-share ledger, the reconciliation helper, and the
batch loop itself:

- **A partial-fill shortfall is computed at the CACHED price, never the
  real fill price.** Both the immediate placement-error branch's own
  `"if matched_order:"` (live-lookup, terminal filled) success path and its
  sibling terminal-inclusive partial-fill branch compute
  `whole_share_settlement_shortfall_usd`/the read-failure fallback for
  `whole_share_pending_usd` when `ledger.settle()` itself raises or its
  post-settlement read fails. An earlier version mixed the claim's cached
  signal-time price with the broker's real, differently-priced fill
  (`planned_order_notional - filled_notional`), disagreeing with
  `settle()`'s own internal shortfall math (which always uses the CACHED
  price) even when settlement succeeds — a two-share $20 claim at a $10.00
  cached price, filled for 1 share at a real $12.00, needs the unfilled
  share's $10.00 restored, not $20 − $12 = $8 (Codex review: "Compute
  partial-fill shortfalls from the cached share price"). Both sites now
  compute `planned_order_notional - filled_shares * current_share_price`
  (all at the cached price), matching `settle()` exactly.
- **`settle()` now has a `require_claim_ownership` flag so a losing
  reconciler mutates NOTHING.** Previously, even when a caller's own
  `resolved_by_this_call` idempotency check (see above) correctly returned
  `False`, `settle()` still unconditionally bumped `shares_purchased_total`
  and appended an audit event — a genuine double-count of the SAME
  physical broker order when two reconcilers raced to discover it, even
  though the caller already (correctly) skips strategy-state booking for
  the loser (Codex review: "Update ledger totals only for the winning
  reconciliation"). `require_claim_ownership=True` — passed by every
  RECONCILIATION call site (`_settle_whole_share_claim`, and both
  immediate-placement-error settle() calls) but deliberately left `False`
  (the default) for the genuine in-line placement success path further
  below, which always wants a real, freshly-placed order counted whether
  or not it also happens to win a claim-ownership race — makes a losing
  call write nothing at all and return `False` immediately.
- **A parseable but non-finite `average_price` (e.g. `"Infinity"`/`"NaN"`)
  is now rejected.** `_usable_positive_float`'s bare `value > 0` check let
  infinity through (infinity IS greater than zero), which would compute an
  infinite booked notional written into strategy budget state, deployment
  totals, and JSON artifacts (Codex review: "Reject non-finite recovered
  fill prices"). Now requires `math.isfinite(value)` too.
- **A non-filled terminal order's own `quantity` field is never treated as
  a fill.** The terminal-inclusive lookup's fallback previously accepted
  `terminal_order.get("cumulative_quantity") or terminal_order.get("quantity")`
  — legitimate ONLY once a match is already known to be terminally
  `"filled"` (the live-lookup branch's own identical fallback, unchanged),
  but wrong for a cancelled/rejected match: that order's `"quantity"` is
  merely what was REQUESTED/submitted, not what executed, so falling back
  to it would treat a fully-rejected order as fully filled (Codex review:
  "Do not treat requested quantity as a terminal fill"). Both the
  reconciliation helper and its mirrored immediate-recovery branch now
  trust ONLY `cumulative_quantity` here; an absent value leaves
  `filled_shares` at its safe `0` default rather than falling back to the
  requested size.
- **The immediate placement-error branch's own terminal-filled success path
  now also books the real fill price.** The `"if matched_order:"` branch
  (live-lookup finds an exact-ref match already in a terminal `"filled"`
  state) previously booked the locally-computed, signal-time
  `quantity`/`order_notional` unconditionally, unlike the cross-day/
  terminal-partial-fill paths, which already prefer the broker's own
  `average_price` (Codex review: "Book the actual fill notional after
  immediate recovery"). It now derives `filled_shares`/`filled_notional`
  from the matched order's own `cumulative_quantity`/`average_price` the
  same way, falling back to the locally-planned quantity/cached price only
  when the matched order carries no usable field.
- **Cross-day reconciliation now runs as a pre-pass over the WHOLE batch,
  before any candidate places.** It previously ran only once a whole-share
  ticker itself reached `_process_whole_share_candidate` in
  `_process_buy_candidates_batch`'s sorted loop — if a recovered order
  within the trailing kill-switch window pushed `running_deployed_7d`
  across the threshold, any candidate sorted BEFORE the whole-share ticker
  had already placed using the lower, not-yet-recovered figure; only LATER
  candidates ever saw the recovered spend (Codex review: "Reconcile
  recovered spend before starting the candidate batch"). The cross-day
  reconciliation logic is now extracted into
  `_reconcile_cross_day_whole_share_claims` (per-ticker) and
  `_reconcile_cross_day_whole_share_claims_for_batch` (iterates every
  whole-share ticker in `buy_candidates`), and
  `_process_buy_candidates_batch` runs the batch-wide pre-pass BEFORE its
  main placement loop starts. The per-candidate call inside
  `_process_whole_share_candidate` itself is kept as a safety net — since
  `settle()` is idempotent, a claim the pre-pass already resolved is
  simply absent from a later `unresolved_claims_any_day` call, so this
  degrades to a no-op scan rather than double-booking anything.

### Closing the remaining pre-pass/reserve-ordering gaps, plus a config-tooling sentinel

A ninth review round found five more issues, all downstream of round eight's
new batch-wide pre-pass and reserve-rollover interaction:

- **The pre-pass now also reconciles TODAY's own unresolved claim, gated on
  the same broad existence signal the initial duplicate check itself
  uses.** It previously excluded the current trading day entirely. A
  same-day retry after an AMBIGUOUS PLACEMENT can leave an unresolved
  claim for today whose real order already landed at the broker, and
  skipping it left candidates sorted before this ticker placing before the
  eventual duplicate-check reconciliation ever added the recovered fill to
  `running_deployed_7d` (Codex review: "Include same-day claims in the
  batch pre-pass"). The fix is deliberately NARROW, though: today's claim
  is only reconciled when `ctx.broker.same_day_strategy_order_exists`
  confirms an order already exists today — a same-day claim that is
  merely STALE with no placement ever attempted is a genuinely different
  case `record_contribution()`'s own reclaim-and-reprice logic already
  owns, and reconciling it via this function's general staleness-release
  fallback instead would flatten a reclaimable, repriceable fire into a
  plain released balance, discarding that behavior entirely (confirmed by
  a regression that initially broke `test_planned_spend_preview_reflects_an_actionable_stale_claim`
  when this fix was applied unconditionally).
- **The pre-pass now scans every configured whole-share ticker directly,
  not `buy_candidates`.** `_reconcile_cross_day_whole_share_claims_for_batch`
  previously iterated only `buy_candidates`, but a whole-share ticker with
  a genuine unresolved claim can be entirely ABSENT from it — e.g. today's
  own NO_ACTION redispatch quotes are missing or disagree, so
  `_whole_share_redispatch_reconciled_price()` returns `None` and the
  ticker is never spliced in — even though reconciling the STORED claim
  against broker history needs no current quote at all (Codex review:
  "Scan unresolved tickers independently of buy candidates"). It now
  iterates `ctx.config.whole_share_tickers - ctx.config.excluded_tickers`
  directly, accepting an optional `decisions_by_ticker` map for
  execution-log fidelity when a real decision exists and falling back to a
  minimal synthetic `NO_ACTION` decision otherwise (all
  `_book_reconciled_whole_share_order`'s own `_log_execution` call
  actually needs).
- **The immediate placement-error branches now check `settle()`'s
  `require_claim_ownership` return value before booking.** Both the
  terminal-filled success path and its terminal-inclusive partial-fill
  sibling previously ignored the boolean `settle()` returns — if a
  concurrent reconciler settled and booked the SAME broker order first,
  this call cleanly returns `False` (no exception), but the code still
  unconditionally recorded the buy, incremented deployment, and published
  another `"submitted"` execution (Codex review: "Skip immediate booking
  after losing claim ownership"). Both now check the return value and
  report a `"skipped"` outcome (deliberate, no error) instead of
  double-booking when it's `False` and no exception was raised.
- **Cross-day reconciliation now runs BEFORE `state.roll_weekly_reserve()`,
  not just before the candidate batch.** `roll_weekly_reserve()` computes
  the rolled-forward reserve as `NORMAL_WEEKLY_CAP_USD -
  state.week_notional(last_week)`, then freezes it for the whole current
  week (`reserve_week_key`). If reconciliation discovers a real fill from
  the PRECEDING ISO week only AFTER that roll has already run, the
  recovered spend retroactively added to the prior week's notional can
  never reduce the ALREADY-FROZEN reserve — a stress-tier run could then
  spend rollover capacity the recovered fill had actually already consumed
  (Codex review: "Reconcile prior-week claims before rolling reserve").
  `_run_once_impl` now runs the pre-pass (via a throwaway
  `ProcessCandidateContext` built from the same primitives the real one
  further below is built from) immediately before
  `state.roll_weekly_reserve()`, discarding the returned
  `running_deployed_7d` — the real batch's own kill-switch bookkeeping is
  recomputed properly once the genuine ctx/candidates are ready. The later
  pre-pass call inside `_process_buy_candidates_batch` is kept too, as a
  redundant, idempotent safety net.
- **`scripts/backup_strategy.py` now recognizes a bare `NONE` as an empty
  category marker.** The module's existing `"(none)"` example (used
  throughout its own docstrings) happens to work only because the
  parentheses fail the uppercase ticker regex — a transcript spelling it
  as a bare `"Whole-share: NONE"` (or `"Excluded: NONE"`) uppercases to
  `"NONE"`, which DOES pass the same `[A-Z.]{1,6}` pattern a real ticker
  symbol would, so `extract_tickers()` returned it as an actual extracted
  symbol and `write_configs()` would persist the bogus ticker `NONE` into
  `tickers.json` instead of clearing the category (Codex review: "Treat
  NONE as an empty category marker"). `NONE`/`NULL`/`NA` were added to
  `_TICKER_STOPWORDS`, so the existing "stop at first non-symbol token"
  loop now treats them exactly like any other non-ticker token.

### Carrying pre-roll spend into the batch, rejecting expired placements, and closing the last same-day terminal-fill gap

A tenth review round found three more issues, all in the pre-roll/pre-pass
machinery introduced over the last two rounds:

- **The pre-roll pass's own recovered spend is now folded into
  `config.deployed_last_7d_usd` before the real batch runs.** The
  reconciliation pass added before `state.roll_weekly_reserve()` (see
  above) previously discarded its own returned `running_deployed_7d`.
  Because `settle()` is idempotent, `_process_buy_candidates_batch`'s own
  later pre-pass can never rediscover a claim the pre-roll pass already
  resolved — so a recovered fill that falls within the trailing
  kill-switch window and should have engaged it was silently omitted from
  the batch's own `running_deployed_7d` seed (Codex review: "Preserve
  pre-roll recovered spend for the candidate batch"). `_run_once_impl` now
  reassigns its local `config` (via `dataclasses.replace`) to the
  reconciled figure whenever it differs, so every later use of `config` —
  including the real batch's own seed and the run's `risk_guard` reporting
  — reflects it.
- **`RobinhoodBroker.place_whole_share_buy` now rejects an `"expired"`
  immediate response too.** Its terminal-failure state set already
  rejected `rejected`/`failed`/`cancelled`/`canceled`/`voided`, but not
  `expired` — even though `_LIVE_ORDER_STATES` (used elsewhere in this
  same module for the exact same purpose) already excludes it as
  terminal/non-live. Without rejecting it here too, an order that came
  back immediately `expired` was treated as a successful full submission
  and settled for the ENTIRE requested quantity regardless of what
  actually executed (Codex review: "Reject expired whole-share placement
  responses"). Raising here routes it through the same ambiguous-placement
  reconciliation every other raised exception already gets, which inspects
  the real cumulative fill via the terminal-inclusive lookup before
  restoring or settling credit.
- **The batch pre-pass's same-day gate now also checks terminal history,
  not just the live-only existence signal.** The narrow gate added above
  (checking `same_day_strategy_order_exists` before reconciling today's
  own claim, specifically to avoid mishandling a genuinely-never-attempted
  stale claim) had its own gap: that broad check is LIVE-only, so it also
  misses a genuinely TERMINAL order — e.g. a partial fill that was THEN
  cancelled before a retry ran today. Falling through to `continue` left
  that claim unreconciled; once it went stale, `record_contribution()`
  would reclaim it and submit the FULL planned quantity again, never
  booking the earlier partial fill — excess shares purchased and reused
  credit (Codex review: "Reconcile terminal same-day fills before
  reclaiming the claim"). The gate now also checks the exact ref-id
  terminal-inclusive lookup (`same_day_order_with_ref_id_prefix_any_state`)
  before concluding nothing was attempted today; only when BOTH checks
  come back empty does the claim genuinely fall through to
  `record_contribution()`'s own reclaim-and-reprice ownership, unchanged
  from before.

### Reconciling before tier planning, and trusting the any-state fallback only when terminal

An eleventh review round found three more issues, all edge cases in the same
pre-roll reconciliation and nonterminal-order handling this document already
covers:

- **The pre-roll reconciliation pass now runs before the `planned_spend`
  loop, not just before `state.roll_weekly_reserve()`.** That loop's own
  whole-share preview reads `kill_switch_engaged(config)` directly to decide
  whether to clamp a candidate's preview notional to its
  `selected_base_amount_usd`. Running the pre-roll pass only after the loop
  (its previous position) meant a claim it reconciled — pushing
  `deployed_last_7d_usd` over the kill-switch threshold — was invisible to
  the loop's own read: tier selection summed the candidate's UNCAPPED
  preview notional even though execution, which re-evaluates the kill switch
  against the now-updated `config`, actually caps it, letting the batch pick
  (and spend against) a tier above what the real, kill-switched plan
  warrants (Codex review: "Reconcile recovered spend before planning the
  budget tier"). The pass is unchanged in every other respect — same
  throwaway `ProcessCandidateContext`, same fold-back into
  `config.deployed_last_7d_usd` — only its position moved earlier, to
  immediately after `buy_candidates`/`decisions` are finalized.
- **The any-state fallback's matched order is now verified terminal before
  its `cumulative_quantity` is trusted, in both the reconciliation helper
  and its immediate-placement-failure sibling.** `same_day_order_with_ref_
  id_prefix_any_state` carries no state filter of its own — it exists
  specifically to see a genuinely terminal order the live-only lookup
  excludes, but that means it can just as easily surface a LIVE order that
  only became visible in the moment between the two lookups (e.g. the
  live-only call ran before the broker indexed it). That order's
  `cumulative_quantity` is not yet final: a positive value settled the
  claim while the order could still fill further, and a zero value released
  the FULL reservation while the order might still fill later — either
  outcome let the eventual real fill reuse already-spent credit or vanish
  entirely (Codex review: "Verify terminal state after the any-state
  fallback"). Both call sites now check the matched order's `state` against
  this module's canonical non-fill-terminal set (or `"filled"` itself)
  before proceeding, leaving the claim reserved otherwise — mirroring the
  live-only lookup's own adjacent `matched_state != "filled": return None`/
  `blocked` guard.
- **The ordinary (non-exception) placement success path now defers
  settlement for a still-live order too.** `place_whole_share_buy` returning
  without raising only means the broker ACCEPTED the order — not that it
  filled. A nonterminal state (`new`/`queued`/`confirmed`/
  `partially_filled`) means the final executed quantity is not yet known,
  the exact ambiguity the placement-EXCEPTION reconciliation branch already
  guarded against; this path had no equivalent check and settled the full
  REQUESTED quantity unconditionally (Codex review: "Keep ordinary
  nonterminal submissions unresolved"). It now performs the same exact-
  ref-id live lookup the exception branch uses and only proceeds to settle
  once that lookup confirms `"filled"` — a still-live match leaves the
  claim reserved for a later reconciliation attempt. (A clean "no match"
  case is covered by the next round below — see "Keep claims reserved when
  accepted orders are not yet visible".)

### Reconciling as early as possible, verifying "not found," and booking the real fill price

A twelfth review round found three more issues, all sharpening the
nonterminal-order handling this document already covers:

- **A clean "no match" from the post-placement lookup is no longer trusted
  either.** The previous round's fix (above) settled through when the
  exact-ref-id lookup found no match at all, reasoning that "not found" was
  an eventual-consistency gap, not a failure signal. Fresh review found
  that reasoning unsafe: Robinhood's own order-history endpoint IS
  eventually consistent, so the just-accepted order may simply not be
  indexed yet while still genuinely working — settling the full requested
  quantity on that clean "no match" carries the exact same
  premature-resolution hazard a still-live match already guards against
  (Codex review: "Keep claims reserved when accepted orders are not yet
  visible"). The ordinary success path now treats "not found" identically
  to "found but nonterminal": both leave the claim reserved for a later
  reconciliation attempt. `DryRunBroker` (`near_close_dca.py`) gained a
  small internal ref-id-keyed order table so its
  `same_day_strategy_order_exists_with_ref_id_prefix`/
  `same_day_order_with_ref_id_prefix_any_state` can answer this
  post-placement lookup the same way a real broker would — reporting
  `"filled"` when a reference price was supplied (this broker's existing
  immediate-fill simulation) or `"new"` otherwise — so a dry-run-broker
  placement's claim is settled exactly when this broker's own design
  intends, rather than every dry-run placement now landing "blocked".
- **The pre-roll cross-day reconciliation pass moved even earlier — before
  signal-derived decisions are spliced for whole-share redispatch, not just
  before tier planning.** The redispatch-eligibility splice (which
  re-dispatches a whole-share ticker's NO_ACTION decision when the ledger
  balance alone is actionable, including via
  `unresolved_claims_any_day`) ran BEFORE the pre-roll pass. A claim the
  pre-roll pass was about to resolve for real was still seen as
  "unresolved" by that splice, so it redispatched the ticker anyway; the
  later per-candidate pass then found the claim already settled (`settle()`
  being idempotent) and published a benign "still accumulating" outcome
  that OVERWROTE the pre-roll pass's own "submitted" outcome for that same
  ticker. Moving the pre-roll pass to run immediately after
  `run_result.decisions = decisions` — before the redispatch splice, the
  planned_spend/tier-selection loop, and `roll_weekly_reserve` — closes
  this ordering gap for good; `guard_active`/`credit_ledger` are not yet
  computed at that point, but neither field is read anywhere in the
  whole-share reconciliation call chain, so `False`/the default empty
  ledger are safe placeholders.
- **Fills recovered by the pre-roll pass now persist into
  `buy_candidates.csv`/`run_state.json`, not just `ctx.state`/
  `running_deployed_7d` for this run alone.** `_book_reconciled_whole_share_
  order`'s `_publish_in_flight_outcome` call was a no-op when invoked from
  the pre-roll pass, because `_IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]`
  is normally only installed once `_process_buy_candidates_batch` starts —
  which runs AFTER the pre-roll pass, and can never rediscover a claim the
  pre-roll pass already resolved (`settle()` is idempotent). When an
  ambiguous order was never written to its original day's own run artifact,
  the pre-roll pass could be the FIRST code ever to recover it — yet that
  recovery reached nowhere durable: `dca_risk.deployed_usd()` (which a
  FUTURE run's own kill-switch seeding reads) sums only `submitted`
  `buy_candidates` rows, so the recovered spend vanished from trailing
  deployment accounting the moment the run ended, letting the kill switch
  disengage prematurely (Codex review: "Persist fills recovered by the
  pre-roll reconciliation pass"). `_run_once_impl` now installs a fresh
  `_IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]` dict immediately before the
  pre-roll pass runs, and `_process_buy_candidates_batch` now SEEDS its own
  `candidate_outcomes` dict from whatever is already installed there
  (instead of starting from an empty dict) before re-installing the same
  object — so a pre-roll-recovered outcome survives into
  `_build_buy_candidates` (via its existing `is_pending_credit_flush`
  recognition of any non-BUY decision with a `submitted`/`dry_run`
  outcome). (A later round below fixed what happens when the SAME ticker
  is ALSO processed for a genuine decision later in the same run — see
  "Preserve recovered fills when processing today's candidate".)
- **The ordinary success path now books the broker's own VERIFIED fill
  price/quantity, not the locally-planned estimate.** Once the
  post-placement lookup confirms `"filled"`, its `cumulative_quantity`/
  `average_price` are now preferred over the locally-estimated
  `quantity`/`current_share_price` this candidate was originally sized
  against — mirroring every other reconciliation branch in this module,
  which already prefer the broker's own fill data for the same reason (a
  quantity-based market order can fill at a different price than the
  signal-time quote used to plan it) (Codex review: "Book the verified
  fill price on ordinary successful orders"). `settle()`'s own shortfall
  math (and its read-failure fallback) still use the CACHED
  `current_share_price`, never the real fill price, matching this module's
  established shortfall-computation convention.

### Aggregating recovered fills, attributing them by claim date, and reporting nonterminal orders honestly

A thirteenth review round found five more issues, all sharpening the
pre-roll-reconciliation persistence and nonterminal-order handling this
document already covers:

- **A recovered fill and today's own submission for the SAME ticker are now
  aggregated instead of one silently erasing the other.** The batch loop's
  `candidate_outcomes[decision.ticker] = c_outcome` assignment
  unconditionally overwrote whatever the pre-roll pass had already seeded
  for that ticker. When BYDDY had BOTH an older claim the pre-roll pass
  just resolved AND its own, independent decision today (often just "still
  accumulating," but sometimes a genuine fresh submission too), today's
  outcome replaced the recovered one — and since `dca_risk.deployed_usd()`
  sums exactly one row's `planned_amount_usd` per ticker, the recovered
  spend vanished from trailing deployment accounting on the next run
  (Codex review: "Preserve recovered fills when processing today's
  candidate"). The loop now captures the seeded outcome *before* calling
  `_process_buy_candidate` (reading it *after*, an earlier version of this
  fix did, would already see whatever `_process_buy_candidate` itself just
  published into the SAME aliased dict) and, when both are real
  submissions, aggregates `submitted_amount_usd`/`whole_share_quantity`
  and joins both order ids; when today's own outcome isn't a real
  placement, the seeded submission is kept exactly as-is instead of being
  replaced by a non-submission.
- **`config/scheduled_tasks.json`'s `model_routing_policy`/
  `model_usage_log` `preserved_note` fields no longer call BYDDY excluded.**
  Both still said "the GFUZ/TXN active universe with the BYDDY exclusion,"
  contradicting `ticker_universe_policy.summary` and `config/tickers.json`
  (BYDDY is actively evaluated by the afternoon whole-share path) and
  risking an agent consuming these CURRENT policy sections omitting it
  from consideration (Codex review: "Stop calling BYDDY excluded in
  current policy notes"). Both now describe the whole-share category
  instead, matching the phrasing `preserved_trading_safety_rules` already
  used.
- **A reconciled fill outside the trailing kill-switch window is no longer
  published into today's own `buy_candidates`.** `_book_reconciled_whole_
  share_order` already excluded an out-of-window claim from the in-memory
  `running_deployed_7d` total, but still unconditionally published it as a
  `submitted` outcome — and since that publication lands in TODAY's own
  run artifact, `dca_risk.deployed_by_day()` buckets it under today's
  `trading_day`, not the claim's true execution date. A three-week-old
  fill would become FRESH seven-day deployment on the very next run's
  kill-switch seeding, and even an in-window fill's effective window
  stretched by however many days separated the claim date from today
  (Codex review: "Attribute reconciled deployment to the claim date").
  Publication is now gated on the same in-window check the
  `running_deployed_7d` total already used; the ledger itself is still
  settled for real either way, and `_log_execution`'s own execution-record
  append (an audit trail, not summed by `deployed_usd()`) still runs
  unconditionally. Persisting the true claim date into `deployed_by_day`'s
  own bucketing would need a new field threaded through `dca_risk.py`'s
  on-disk contract; this narrower fix at least prevents an out-of-window
  recovery from ever appearing as today's deployment.
- **The ordinary success path now reports an accepted-but-nonterminal
  order as `"submitted"`, not `"blocked"`.** The broker DID accept the
  order — `order_id` is real and live — so reporting it as `"blocked"`
  (the prior round's fix) was itself misleading: `run_result.
  execution_records` never got an entry, and `submitted_orders.csv`/
  fill-coverage reporting said nothing was submitted even though real
  capital is genuinely committed at the broker, contradicting the
  coverage setup that expects an accepted-but-unfilled whole-share order
  to have an execution record (Codex review: "Record accepted nonterminal
  orders as submitted"). This branch now reports `"submitted"` using the
  locally-planned quantity/notional (the real fill isn't known yet), books
  it into `ctx.state`/`running_deployed_7d` exactly like a normal
  submission, and calls `_log_execution` — but the whole-share LEDGER's
  own claim stays UNRESOLVED (`settle()` is still not called), so a later
  reconciliation attempt still owns confirming the real terminal fill.
  That later settlement's own `_book_reconciled_whole_share_order` call
  already checks `_same_day_ticker_deployment_already_recorded` before
  booking again, using this exact submission (once written to today's run
  artifact) to avoid double-counting.
- **The any-state reconciliation fallback (and its immediate-placement-
  error sibling) now allow a `"quantity"` fallback for an explicitly
  `"filled"` match.** The previous round's terminal-state guard correctly
  restricted `cumulative_quantity`-only trust to non-fill terminal states
  (cancelled/rejected/expired), but applied that same restriction even
  when the any-state lookup found the order EXPLICITLY `"filled"` — exactly
  as authoritative as the live `matched_state == "filled"` branch, which
  already falls back to `"quantity"` when `cumulative_quantity` is absent.
  A filled order that only carried `"quantity"` left `filled_shares` at 0
  and silently restored the FULL claim as if nothing executed, making its
  already-spent credit reusable (Codex review: "Use requested quantity for
  any-state filled orders"). The fallback to `"quantity"` is now allowed
  specifically when the any-state match's own `state` is `"filled"`,
  mirroring the live branch exactly; a non-fill terminal state still never
  falls back to it.

### Aggregating multiple recovered claims, reconciling from the placement response, and attributing deployment to the true claim date

A fourteenth review round found six more issues in the same reconciliation
subsystem. Four were fixed; two were evaluated and declined -- see the end
of this subsection for why.

- **Multiple recovered claims for the SAME ticker are now aggregated, not
  overwritten.** `_reconcile_cross_day_whole_share_claims`'s own per-ticker
  loop can settle MULTIPLE separate unresolved claims for one ticker in a
  single call (e.g. two different earlier trading days each left one
  unresolved) -- each settled claim called `_book_reconciled_whole_share_
  order` independently, and its plain `_publish_in_flight_outcome` call
  overwrote whatever the PREVIOUS claim in the same loop had just published,
  leaving only the last claim's notional/quantity visible to
  `_build_buy_candidates` (Codex review: "Aggregate all recovered claims
  for the same ticker"). A new `_publish_or_merge_whole_share_outcome`
  helper (used by `_book_reconciled_whole_share_order` in place of the bare
  publish call) now aggregates with whatever is already published for that
  ticker via a new shared `_merge_whole_share_submission_outcomes`
  function -- the SAME helper the previous round's "aggregate today's own
  submission with a pre-roll-recovered fill" fix (see above) now also uses,
  replacing that fix's own inline merge logic.
- **Order ids no longer get joined into a synthetic composite string.** The
  previous round's aggregation joined two order ids as `"old-id,new-id"`
  for the row's `order_id` field -- not a real broker order id, so an
  exact-ID consumer such as `reconcile_foreground_orders()` could never
  match it against either real broker record, and the composite silently
  hid that TWO orders were actually submitted (Codex review: "Preserve
  each broker order ID when aggregating outcomes"). `order_id` now stays a
  single, exact broker order id (the newer of the two aggregated
  outcomes); every distinct id involved is still recorded, in a new
  `whole_share_order_ids` list field.
- **A terminal placement response's own payload is now reused directly,
  instead of being discarded before raising.** `RobinhoodBroker.place_
  whole_share_buy` used to raise a bare `RuntimeError` when the broker's
  immediate response already reported a terminal (non-live) state --
  discarding the response's own `cumulative_quantity`/`average_price` and
  forcing the caller to depend exclusively on a SEPARATE, later
  order-history lookup that can itself be eventually consistent and report
  a clean "no match" for an order the placement response already proved
  landed, silently restoring the full reserved notional and making its
  already-executed shares' credit reusable (Codex review: "Reconcile
  terminal fills from the placement response"). A new
  `WholeSharePlacementTerminalError` (raised in place of the bare
  `RuntimeError`, carrying the raw `order` payload as an attribute) lets
  `_process_whole_share_candidate`'s exception handler skip BOTH the
  live-only and terminal-inclusive reconciliation lookups entirely for
  this specific exception type, reusing `exc.order` as the authoritative
  terminal match directly.
- **Reconciled deployment is now attributed to the claim's TRUE execution
  date, not the artifact's own date, for every claim -- not just
  out-of-window ones.** The previous round's fix only omitted an
  out-of-window claim from today's own `buy_candidates` publication
  entirely (there was no way to publish one without misattributing it).
  Fresh review pointed out an in-window claim (1-6 days old) still got
  published under today's date, silently extending its effective
  kill-switch window by however many days separated the claim date from
  today (Codex review: "Preserve the claim date for in-window
  recoveries"). Every reconciled outcome now carries a
  `whole_share_claim_breakdown` list -- `[{"trading_day", "amount_usd"}, ...]`
  entries, ONE per claim the row aggregates (a single row can now combine
  claims from DIFFERENT trading days, or a reconciled claim plus today's
  own fresh submission, so a single scalar date override could never have
  survived aggregation intact) -- and a new `dca_risk.
  deployed_usd_by_claim_date()` (used by `deployed_by_day()` in place of
  the old single-bucket `deployed_usd()` call) reads it to bucket each
  claim under its own date, attributing whatever portion of
  `planned_amount_usd` the breakdown does NOT cover (e.g. today's own
  aggregated contribution) to the artifact's own `trading_day` as before.
  This makes publication itself unconditionally safe regardless of window,
  so the out-of-window omission from the previous round's fix is gone too
  -- every reconciled claim is now published, correctly dated.

Two findings from this round were evaluated and declined:

- **"Redispatch restored credit on same-day BUY retries"**
  (`dca_whole_share_accumulator.py`): `record_contribution`'s own resolved-
  slot redispatch branch deliberately requires `contribution_usd <= 0`
  before re-evaluating a restored balance -- its own inline comment
  already states why: "A NONZERO contribution_usd here... is intentionally
  left out of scope -- re-opening a resolved slot for new money risks
  double-counting the original contribution." This finding asks to lift
  that restriction for a same-day retry whose signal is still a fresh BUY.
  Doing so safely requires redispatching the RESTORED balance with a zero
  additional contribution while STILL recording the fresh BUY's own
  contribution separately without conflating the two -- a real design
  change to this ledger's core idempotency contract, not a bounded fix,
  and one an earlier round already reasoned through deliberately. Revisiting
  it deserves its own dedicated round with fresh review, not a
  same-session addendum.
- **"Defer deployment booking until terminal quantity is known"**: once
  the ordinary success path's nonterminal branch (see the previous round's
  "Record accepted nonterminal orders as submitted" fix above) books an
  ESTIMATED full notional for an order whose real fill is not yet known, a
  LATER reconciliation that discovers the order actually cancelled or
  partially filled finds `_same_day_ticker_deployment_already_recorded`
  True for that original day and skips rebooking entirely -- the estimate
  never gets corrected down to the real amount. A full fix needs a NEW
  helper to read back the EXACT previously-recorded notional for that
  ticker/day (not just the boolean "something was recorded" the current
  check answers) and book a signed correction delta via the SAME
  `whole_share_claim_breakdown` mechanism this round introduced. That
  mechanism makes the fix conceptually reachable, but implementing the
  read-back/delta/re-attribution correctly, under this session's remaining
  scope, risks introducing exactly the kind of subtle
  double-counting/under-adjustment bug this reconciliation subsystem has
  spent 40+ rounds eliminating one at a time. Left for a dedicated
  follow-up round instead of a rushed addition here.

Three findings from a later round:

- **`_failure_candidate_row` now preserves claim metadata too.** The
  failure-artifact rebuild path (`_handle_run_failure()` reconstructing
  `buy_candidates` after a mid-batch exception) is separate from
  `_build_buy_candidates` and had not been updated when
  `whole_share_order_ids`/`whole_share_claim_breakdown` were added to the
  latter. If a cross-day fill is recovered and a *later* candidate in the
  same batch raises, the resulting failure artifact dropped both fields --
  losing the extra broker order IDs and causing
  `dca_risk.deployed_usd_by_claim_date()` to attribute the recovered
  amount to the current run's date instead of its true claim date,
  extending its kill-switch window incorrectly (Codex review: "Preserve
  claim metadata in failure candidate rows").
- **`load_strategy_config` now validates ticker-symbol shape on the FINAL
  overlaid `whole_share_tickers`, not just tickers.json's own list.**
  `load_tickers`'s `_WHOLE_SHARE_SYMBOL_PATTERN` check only runs against a
  `tickers.json` file's own `"whole_share"` entries; a symbol supplied
  directly through `base` or the documented `whole_share_tickers=` keyword
  override bypassed that loader entirely. A lowercase symbol (e.g.
  `"byddy"`) collides with nothing and survived every disjointness/
  duplicate check that already ran against the final resolved sets, but
  `near_close_dca`'s signal lookup and `RobinhoodDataProvider._quote_for()`/
  `_parse_bars()` compare the broker's canonical UPPERCASE response
  verbatim -- the ticker would silently never match and never accumulate
  (Codex review: "Validate final whole-share symbols after overrides").
- **`scripts/backup_strategy.py`'s transcript restore now reconciles
  retained ticker categories when a symbol moves into a mentioned one.**
  A category-only transcript restore (e.g. `Whole-share: AAPL` with no
  active-universe section) is a supported partial-restore case, but
  merging it into an existing config whose retained `active` list still
  contains `AAPL` wrote the symbol into BOTH categories, since the
  unmentioned `active` list is filtered out of the merge payload entirely.
  The next `load_strategy_config()` call then rejected the resulting
  config as non-disjoint, preventing the strategy from starting (Codex
  review: "Reconcile retained categories during partial ticker restores").
  A new `_strip_moved_symbols_from_retained_categories` helper removes a
  moved-in symbol from every OTHER retained category in the existing file
  before the merge -- deliberately excluding `active` from the set of
  categories a symbol can be considered "moved FROM", since that list's
  extraction has no reliable "mentioned" marker and is known to
  over-extract capitalized prose words as false positives; `active` only
  participates in the correction as a category a symbol can be stripped
  OUT OF when genuinely retained-but-unwritten.

A follow-up finding on that same fix was attempted and then declined once
testing exposed it was unsafe: extending the "moved symbol" stripping to
cover a move INTO `active` too (not just excluded/whole_share), gated on
whether the "universe:" marker was explicitly present. The repo's own
existing test (`test_write_configs_preserves_whole_share_despite_
descriptive_prose_mentioning_it`) immediately demonstrated why: the
marker's *presence* being reliable does not make the *contents* of the
extracted `active` list reliable -- that test's own transcript has a
trailing prose sentence ("...is a whole-share ticker...") whose "whole-
share" text is what stops the universe-block capture, and an uppercase
token sitting just before that cutoff (in that test, "BYDDY") gets swept
into `active` as a false positive despite the marker itself being genuine
and unambiguous. Trusting `active`'s contents this way would have silently
stripped a real whole-share/excluded entry the transcript never intended
to touch. A real fix needs the `active` extraction itself to stop strictly
at the ticker list's own boundary rather than using "whole-share"/
"excluded" text as a boundary proxy -- a separate, more invasive regex
change than this round's scope, left for a dedicated follow-up.

A second, unrelated finding from the same round: a transcript explicitly
listing the same symbol in BOTH `Excluded:` and `Whole-share:` (a
genuinely contradictory restore, not the active-list overlap the earlier
fix already resolves) previously passed straight through `extract_tickers`
into `write_configs`, persisting an overlapping partition that
`load_strategy_config` would then reject on its next load. `extract_tickers`
now raises `ValueError` if the excluded/whole_share sets overlap, before
any file is written, so the operator fixes the source transcript rather
than debugging a broken live config after the fact.

One P1 finding from this round was evaluated and declined: **"Preserve the
originating deployment class during reconciliation"**
(`_reconcile_cross_day_whole_share_claims`, the batch-wide pre-pass).
Whole-share claims are keyed by `f"{stream}-{ticker}"` (see the
`whole_share_idempotency_key` construction in `_run_once_impl`), with no
concept of scheduled-vs-manual origin at all -- an unscheduled/manual
override run using the default `stream="afternoon"` produces the exact
same idempotency key as the genuinely scheduled 15:30 run, so its
reconciliation pre-pass can discover and settle a claim that actually
belongs to the scheduled run. `_book_reconciled_whole_share_order` gates
the `running_deployed_7d` credit on the CURRENT run's own `ctx.scheduled`,
and publishes into whichever artifact tree the CURRENT invocation is
writing to -- a manual run's own `manual_runs/<timestamp>/` tree, which
`dca_risk.deployed_by_day()`/`scripts.run_classification` deliberately
exclude from scheduled-live metrics (see this repo's "Run classification"
docs). Once the manual run settles the claim, it is no longer unresolved,
so a LATER scheduled run's own reconciliation can never rediscover and
correctly re-book it either -- the real scheduled fill silently
disappears from trailing deployment, which can leave the kill switch
undercounted for a purchase that genuinely happened. Fixing this properly
needs the claim's TRUE originating classification threaded through and
persisted at `record_contribution()` time (not derivable after the fact
from `stream` alone, since the idempotency key does not distinguish
scheduled from manual for the same stream name), then consulted by every
reconciliation path to either book/publish under that ORIGINAL
classification regardless of which run happens to settle it, or defer
settlement entirely to a future run whose own classification matches. That
is a new field across the ledger's on-disk schema and every
`record_contribution`/reconciliation call site -- a materially different,
larger change than a bounded fix, and one this round's remaining scope
could not safely attempt without risking exactly the kind of subtle
misattribution bug this reconciliation subsystem has spent many rounds
already eliminating. Left for a dedicated follow-up round.

A further round of four findings, all fixed:

- **`same_day_order_with_ref_id_prefix_any_state` now requires provable
  order history.** It iterated the eager `_same_day_orders`
  (`require_provable_history=False`), so an unwired `orders_fn` or an
  unrecognizable page returned a clean empty list -- indistinguishable
  from a genuinely scanned-and-empty history, but proving nothing. Every
  caller of this method is whole-share reconciliation, which reads a clean
  `None` as permission to restore a stale or placement-failure claim; an
  order that actually reached the broker could have its already-spent
  credit released and reused (Codex review: "Require provable order
  history before releasing claims"). Now iterates with
  `require_provable_history=True`, exactly like `strategy_order_with_ref_id`
  already does for the same recovery-sensitive lookup -- an unprovable
  history now raises, and every one of this method's three call sites
  already treats a raise as "leave the claim exactly as it was" (the same
  fail-closed posture `strategy_order_with_ref_id`'s callers use).
- **A terminal (cancelled/rejected/expired) order with NO `cumulative_quantity`
  field no longer releases the claim.** Both reconciliation sites
  (`_reconcile_unresolved_whole_share_claim`'s cross-day path, and
  `_process_whole_share_candidate`'s own immediate placement-failure path)
  defaulted `filled_shares` to `0` whenever the field was absent -- the
  same default used for a genuinely explicit `"0"` -- and then
  unconditionally released the full reservation either way. But an absent
  field proves nothing: a cancelled order can still have partially
  executed before it terminated (Codex review: "Keep terminal claims
  reserved when fill quantity is absent"). Both sites now distinguish
  "explicit zero" (parseable, equals zero -- releases as before) from
  "unknown" (field absent, or unparsable -- leaves the claim reserved for
  later reconciliation instead), mirroring this module's own
  `_executed_quantity_is_explicit_zero` precedent for CSV-sourced terminal
  evidence.
- **A confirmed EXPLICIT zero-fill order now counts toward
  `max_orders_per_day`.** `_process_whole_share_candidate`'s immediate
  placement-failure path previously restored the contribution for a
  confirmed zero-fill terminal order and fell through to the GENERIC
  failure outcome -- which never calls `state.record_buy()` and always
  reports `order_id=None`. `max_orders_per_day` (config/budgets.json
  `circuit_breakers`) is defined as an ATTEMPT count where a cancelled/
  rejected order still consumes one of the shared daily slots (matching
  `seed_prior_allocation`'s own identical contract); the in-memory count
  therefore stayed one too low, and the persisted artifact had no order ID
  to reconstruct the missing attempt from on a later run, letting later
  candidates place beyond the configured ceiling (Codex review: "Count
  confirmed zero-fill orders toward the attempt cap"). This one confirmed
  case now gets its own dedicated branch: `state.record_buy(ticker, day,
  0.0)` (an attempt, zero notional) plus the real broker order ID
  preserved in the outcome, before falling into the same restore/failure
  shape the generic branch already used.
- **The batch pre-pass's synthetic decision now logs execution against the
  reconciled price, not `0.0`.** `_reconcile_cross_day_whole_share_claims_
  for_batch` builds a minimal synthetic `NO_ACTION` decision for a ticker
  with no real `decisions_by_ticker` entry (e.g. this pre-pass runs before
  signal evaluation), leaving `target_price` at its dataclass default of
  `0.0`. `build_execution_record()` computes slippage as `avg_fill_price -
  target_price`; with `target_price=0.0` the ENTIRE fill price read as
  adverse slippage and the entire notional as slippage cost, misreporting
  a normal reconciled fill as catastrophically poor execution (Codex
  review: "Supply a real target for recovered execution records").
  `_book_reconciled_whole_share_order` now builds an adjusted decision
  (`dataclasses.replace`) with `target_price` set to the reconciled
  order's own effective price-per-share (`order_notional_usd /
  shares_purchased` -- the real fill price when one was used, else the
  claim's cached signal-time price) for logging purposes only, whenever
  the caller's decision left `target_price` unset.

A fifth finding from the same round, also fixed: **`place_whole_share_buy`
now preserves an inner order from an OUTER failure envelope too.** The
terminal-state check just below already handles a SUCCESSFUL outer
envelope carrying a terminally-rejected inner order (raising
`WholeSharePlacementTerminalError` with the order payload attached), but
the earlier `outer_failure is not None` branch (e.g. `{"success": false,
"data": {"order": {...}}}`) raised a bare `RuntimeError` unconditionally,
discarding the inner order entirely. If that inner order carries its own
terminal state and a positive `cumulative_quantity` -- e.g. a partially
filled order that was then cancelled, with the cancellation itself
surfacing as the outer `"success": false` -- and the separate order-
history endpoint has not yet indexed it, the caller's placement-failure
reconciliation finds nothing and restores the full reservation, making
the already-executed share's credit reusable (Codex review: "Preserve
inner orders from failure envelopes"). The outer-failure branch now
inspects the unwrapped response for an inner order with a usable id and a
terminal state (the same state set the later branch checks, plus
`"filled"`) before raising, and raises `WholeSharePlacementTerminalError`
with that order attached when found -- an outer failure with no usable
inner order still raises a plain `RuntimeError` as before.

A further round of six findings, all fixed:

- **`place_whole_share_buy` now also preserves a BARE order in an outer
  failure envelope.** The prior round's fix checked only for a nested
  `"order"` key inside the unwrapped failure data; the unwrapped data can
  itself BE the bare order object with no nesting at all (e.g. `{"success":
  false, "data": {"id": ..., "state": "cancelled", "cumulative_quantity":
  ...}}`), exactly the shape the SUCCESS path's own `data.get("order") or
  data` fallback already handles. Missing that shape still discarded the
  authoritative terminal fill and raised a bare `RuntimeError` (Codex
  review: "Preserve bare orders in outer failure envelopes").
- **A recovered terminal-response fill is now logged from its own payload,
  without re-querying broker order history.** When `place_whole_share_buy`
  raises `WholeSharePlacementTerminalError`, its own payload already
  carries authoritative `cumulative_quantity`/`average_price` -- exactly
  why the terminal-inclusive lookup is skipped for it entirely. But
  `_log_execution` still unconditionally re-queried
  `broker.get_order_fill()`, a SEPARATE, potentially eventually-consistent
  endpoint; a clean miss there recorded the proven partial fill as
  "submitted"/unfilled instead of "filled", corrupting
  `filled_orders.csv`/slippage/missing-fill coverage with data this call
  already had (Codex review: "Log terminal-response fills without
  re-querying history"). `_log_execution` now accepts an optional
  `known_fill` parameter used instead of querying, when the caller
  (`_process_whole_share_candidate`'s own terminal-order settlement
  branch) already has a usable fill price.
- **A confirmed EXPLICIT zero-fill order discovered by the cross-day/
  pre-roll reconciliation path now also counts toward
  `max_orders_per_day`.** The immediate placement-error branch got this
  fix in the prior round; `_reconcile_unresolved_whole_share_claim`'s
  shared helper (used by BOTH that branch and the cross-day/pre-roll
  reconciliation path) still returned plain `None` for this exact case,
  telling its OTHER caller "nothing to book" (Codex review: "Count
  pre-roll zero-fill orders toward the attempt cap"). It now returns a
  distinct `{"zero_fill_attempt": True, "order_id"}` shape (never
  confused with a real settlement, which always carries
  `shares_purchased`/`order_notional_usd`), and
  `_book_reconciled_whole_share_order` branches on it to book a
  zero-notional attempt (and publish a non-clobbering `"failed"` outcome)
  instead of silently dropping the attempt count.
- **A recovery discovered by `_process_whole_share_candidate`'s own
  internal, per-candidate safety-net reconciliation (a duplicate of the
  cross-day pass, kept for a ticker the batch pre-pass somehow missed, or
  for broker history that becomes consistent only a moment later) no
  longer gets silently overwritten by that SAME call's own final outcome
  for TODAY's signal.** This needed fixes at TWO levels, discovered the
  hard way after an overly clever single-level fix regressed an already-
  fixed case (see the git history for the full story):
  - *Inside* `_process_whole_share_candidate`: a new local
    `_publish_whole_share_outcome` closure captures whatever the initial
    reconciliation call published (distinguished from a merely
    pre-existing entry via an identity comparison against a snapshot taken
    BEFORE that reconciliation call), and every one of the function's ~6
    self-publishing return paths now calls it instead of the raw,
    overwriting `_publish_in_flight_outcome` -- so a path that reaches one
    of those (e.g. the main successful-fire path) merges the recovery in
    rather than clobbering it.
  - *At* `_process_buy_candidates_batch`'s own outer loop: most of that
    function's ~40 OTHER return paths (e.g. "still accumulating") never
    republish at all, so a mid-call recovery can survive internally right
    up until the batch loop's own `candidate_outcomes[ticker] = c_outcome`
    assignment -- which is exactly where it was getting silently
    overwritten. A new `mid_call_recovery` check there, using the SAME
    identity-based distinction (against both `seeded_outcome` and
    `c_outcome`), catches this case too.

  (Codex review: "Merge recoveries published during candidate
  processing".)
- **`scripts/foreground_primary.reconcile_foreground_orders` now validates
  every ID in an aggregated whole-share outcome, not just the scalar
  `order_id`.** A row aggregating two recovered claims for the same ticker
  keeps `order_id` as a single, newer exact broker id, while
  `whole_share_order_ids` carries every distinct id the row actually
  represents. Checking only the scalar field let an older aggregated id
  with no matching broker fill pass validation silently (Codex review:
  "Reconcile every ID in aggregated whole-share outcomes").
- **A whole-share claim is now reconciled on `_run_once_impl`'s
  date/holiday-gated early-return path too, not just the inverse-
  Martingale lease recovery already there.** An ambiguous whole-share
  placement claimed on the FINAL approved trading date left an unresolved
  claim that every SUBSEQUENT scheduled invocation could never reach --
  each one returns from the date gate before the ordinary reconciliation
  pass further down in `_run_once_impl` even runs, permanently stranding
  the ticker's balance until an operator manually reopens the trading
  window (Codex review: "Reconcile whole-share claims before date-gated
  returns"). A new `_book_reconciled_whole_share_order_durably` helper
  mirrors `_book_recovered_lease_durably`'s identical scope narrowing --
  only the DURABLE half of booking (`state.record_buy`/
  `own_orders_recorded`), skipping the ephemeral `running_deployed_7d` and
  the candidate-outcome/execution-record row a gated run never writes
  anyway. Restricted to the afternoon stream only, matching this module's
  one existing whole-share evaluation restriction elsewhere.
- **The ordinary success path now books an accepted order even when its
  post-placement state-verification lookup raises, instead of reporting
  it as `"blocked"`.** The broker already accepted the order -- a
  subsequent lookup outage confirming its visible state is a reporting
  concern, not proof the order never happened. Reporting `"blocked"`
  dropped the fill from `state.record_buy`, `own_orders_recorded`, and
  `running_deployed_7d`, letting the same-day budget and attempt-cap
  tracking silently undercount a real broker order (Codex review: "Book
  accepted orders when state verification raises"). The exception handler
  now mirrors the sibling "not yet visible" branch exactly: it books the
  order as settled (`record_buy`/`own_orders_recorded`/
  `running_deployed_7d` when scheduled), publishes a `"submitted"` outcome
  via `_publish_whole_share_outcome` with
  `whole_share_settlement_reconciliation_needed: True` so the normal
  reconciliation pass still confirms the fill later, and logs the
  execution via `_log_execution`.
- **The second (pre-placement) duplicate-check's exact ref-id lookup no
  longer restores credit when its own verification raises.** That branch
  only runs after the broader duplicate check already proved SOME live
  same-day order exists for this ticker right now -- unlike the broader
  check's own exception handler just above it (which is safe to treat as
  "no evidence of a duplicate, restore credit and retry"), failing to
  confirm which specific order it is does not mean no order exists.
  Calling `_preserve_and_block`'s credit-restoring sibling here let a
  transient lookup failure free up whole-share credit that a real,
  already-placed order was still consuming -- risking a second concurrent
  purchase against the same accumulated balance (Codex review: "Keep
  reclaimed claims reserved when exact lookup fails"). The handler now
  leaves the claim reserved: it reports `"blocked"` with
  `deliberate_skip: True` and does not touch the ledger, so the ticker's
  whole-share balance stays committed to the order that (as far as this
  lookup could tell) may still be live.
- **`_reconcile_unresolved_whole_share_claim` now books a still-LIVE
  matched order instead of silently returning `None`.** Its live-only
  ref_id lookup can find an exact match that has not yet reached a
  terminal state -- the real filled quantity genuinely isn't known yet, so
  the ledger claim correctly stays reserved either way, but returning
  `None` (as an earlier version of this fix did) told the caller "nothing
  to book" entirely: a batch pre-pass or the initial same-day duplicate
  check could find this exact live order, correctly leave the claim
  reserved, and then skip the ticker without ever calling `record_buy()`
  or adding its requested notional to `running_deployed_7d` -- letting
  later candidates in the same batch exceed the daily attempt count,
  daily/weekly notional caps, or the kill switch while the accepted order
  remained live and completely uncounted (Codex review: "Book live orders
  discovered during claim recovery"). Returns a THIRD distinct shape,
  `{"live_order_found": True, "order_id", "planned_notional_usd",
  "planned_quantity"}` (using the CLAIM's own planned notional/quantity,
  since the real fill isn't known), alongside the existing settlement and
  `zero_fill_attempt` shapes. Both `_book_reconciled_whole_share_order` and
  its gated-path sibling `_book_reconciled_whole_share_order_durably` grew
  a matching branch: the former books the planned notional into
  `ctx.state`/`running_deployed_7d` and publishes a `"submitted"`/unfilled
  outcome (never calling `ledger.settle()`), the latter books just the
  durable half, mirroring their existing `zero_fill_attempt` handling.
- **A confirmed EXPLICIT zero-fill order is no longer booked as an
  attempt when releasing its claim itself fails.** The zero-fill branch's
  own `ledger.preserve_pending()` call was wrapped in a best-effort
  `try/except: pass` and the function unconditionally returned the
  `zero_fill_attempt` result regardless of whether the release actually
  succeeded -- so a release failure left the claim genuinely still
  unresolved while telling the caller to record a real attempt and
  publish this order id anyway. Since this same function is called from
  both a pre-pass and the per-candidate safety net, a LATER reconciliation
  attempt encountering the SAME still-unresolved claim would then record
  the identical attempt a second (or third) time, inflating
  `max_orders_per_day` accounting for one single real order (Codex
  review: "Book zero-fill attempts only after releasing the claim").
  Returns `None` instead when the release itself raises -- exactly like a
  failed lookup elsewhere in this function -- so a later, successful
  reconciliation attempt is the one that actually books it.
- **`_merge_whole_share_submission_outcomes` now unions BOTH sides' full
  `whole_share_order_ids` lists, not just one id off each.** `new` can
  itself already be a previously-aggregated outcome (e.g. the outer batch
  loop merging a seeded pre-roll recovery with an already mid-call-merged
  recovery, before merging in today's own fresh submission) carrying its
  own multi-id `whole_share_order_ids` list. Consuming only
  `new["order_id"]` (a single, newer scalar id) silently dropped every
  OTHER id in that list; since `scripts/foreground_primary.py`'s
  `reconcile_foreground_orders` validates exactly the ids present in this
  field, the dropped broker order was never checked and drift detection
  could falsely pass (Codex review: "Merge every ID from pre-aggregated
  outcomes").
- **`dca_live_adapters.RobinhoodBroker.place_whole_share_buy` now
  preserves an inner order from an outer failure envelope regardless of
  its state, not just a terminal one.** The prior fix ("Preserve inner
  orders from failure envelopes") only raised
  `WholeSharePlacementTerminalError` when the inner order's own state fell
  in a fixed terminal set -- an inner order still LIVE/working (e.g.
  `"new"`/`"confirmed"`/`"partially_filled"`) fell through to a bare
  `RuntimeError`, discarding it entirely and forcing the caller to rely
  exclusively on its own SEPARATE live/terminal-inclusive lookups, which
  can BOTH independently miss the very order this response already proved
  was accepted (an eventually-consistent order-history endpoint) --
  restoring the full reservation while the order remains live at the
  broker (Codex review: "Preserve accepted nonterminal orders in failure
  envelopes"). Any inner order with a usable id is now preserved
  regardless of state; the caller's own `exc.order` handling already
  correctly discriminates terminal vs. still-live states (its "Verify
  terminal state after the any-state fallback" branch), so this is safe
  without any caller-side change.
- **`load_prior_same_day_allocations` no longer counts a cross-day
  whole-share recovery toward today's own attempt count.** A candidate row
  whose ENTIRE `whole_share_claim_breakdown` points at trading day(s)
  strictly before this artifact's own `trading_day` is a cross-day
  RECOVERY of an order that was actually attempted (and already counted
  toward `max_orders_per_day`) on that earlier day, not a new attempt made
  today -- `dca_risk` only reads this breakdown for the DOLLAR figure, but
  this function's own `attempt_count` has no date bucketing of its own, so
  counting it again here seeded a later same-day retry's own attempt count
  as if the historical order were a fresh same-day attempt, potentially
  blocking a legitimate new order today even though
  `ctx.state.record_buy()` originally attributed the recovered order to
  the claim date (Codex review: "Keep recovered attempts on their
  original claim dates"). A row with no breakdown, or one covering TODAY
  (alone or alongside older claims), still counts normally.
- **A still-live recovered whole-share order is no longer rebooked on
  every later run.** A `"live_order_found"` claim (see the shape added
  above) stays deliberately unresolved in the ledger while its order
  remains live, so it is reconciled -- and, before this fix, rebooked and
  republished -- again on EVERY subsequent invocation until the order
  finally terminates. The existing on-disk dedup check
  (`_same_day_ticker_deployment_already_recorded`) only matches an
  artifact whose OWN `trading_day` equals the CLAIM's original trading
  day, but the recovery is published under THAT LATER run's own
  trading_day (today), never the claim's original day -- an exact match
  anchored at the claim's day can therefore never find it, letting the
  same planned notional double- (or triple-, or quadruple-) count across
  every run the order stays live for (Codex review: "Avoid rebooking a
  recovered live order on every later run"). A new
  `_same_day_order_id_already_recorded` helper deduplicates by the
  recovered order's OWN id instead, scanning the trailing kill-switch
  window anchored at TODAY (so it actually covers a recovery artifact
  written on an earlier day this week) rather than at the claim's day.
  Both `_book_reconciled_whole_share_order` and its gated-path sibling
  `_book_reconciled_whole_share_order_durably` use this new check only
  for the `"live_order_found"` shape -- the other two shapes (a real
  settlement, or a confirmed zero-fill) actually resolve the underlying
  ledger claim, so they stop being reconciled again at all and keep their
  original, narrower dedup check.
- **A terminal recovery is now ALSO deduplicated by order id, not just an
  earlier terminal recovery for the same claim date.** The prior fix only
  taught the `"live_order_found"` shape's own booking to dedup by order
  id; the ORDINARY settlement branch (both in
  `_book_reconciled_whole_share_order` and its gated-path sibling) still
  deduplicated solely by ticker/claim-day. When an earlier run
  provisionally booked an order as `"live_order_found"` (published under
  THAT run's own trading_day) and a LATER run then finds the SAME order
  now terminally FILLED, the ticker/day check alone can never see the
  earlier provisional artifact -- the order gets settled and published a
  second time, and `dca_risk.deployed_usd_by_claim_date()` then
  attributes both rows to the claim date, doubling trailing deployment
  and potentially engaging the kill switch or caps prematurely (Codex
  review: "Deduplicate terminal recoveries by broker order ID"). Both
  booking functions now check EITHER the original ticker/day match OR
  the order-id match before booking a real settlement.
- **`_reconcile_unresolved_whole_share_claim`'s `"live_order_found"`
  shape now books the matched order's own ACTUAL submitted quantity, not
  the claim's pre-clamp planned figure.** The unresolved claim's
  `shares_to_buy`/`order_notional_usd` are what was originally PLANNED,
  before any per-order-ceiling or kill-switch clamp at placement time --
  when the real placement was clamped to fewer shares (e.g. a 3-share
  $30 fire actually submitted as a 1-share $10 order), booking the
  claim's unclamped figure overstates this run's own batch caps and
  trailing deployment enough to spuriously block unrelated candidates
  (Codex review: "Book only the executable quantity for recovered live
  orders"). Now reads the matched order's own `"quantity"` field (what
  was really submitted) when available, falling back to the claim's
  `shares_to_buy` only when the broker's response carries no usable
  quantity field at all, and recomputes the notional from that quantity
  at the claim's cached signal-time price (the order hasn't filled yet,
  so no real fill price exists to prefer).
- **`load_prior_same_day_allocations` now buckets a whole-share row's
  dollar amount by its own claim date too, not just its attempt count.**
  The prior fix ("Keep recovered attempts on their original claim
  dates") only excluded a cross-day-recovery-only row from
  `attempt_count` -- the loader still added the row's FULL
  `planned_amount_usd`/fill amount to TODAY's own bucket regardless of
  which day the underlying claim(s) actually belonged to. Since
  `_compute_earlier_week_cross_stream_notional()` reuses this SAME
  loader to seed the shared weekly cap, a Monday artifact recovering a
  claim from the previous Friday was counted as Monday/current-week
  spend even though the row's own `whole_share_claim_breakdown` proves
  it belongs to the prior week -- understating remaining weekly headroom
  and potentially blocking valid orders for the rest of the week (Codex
  review: "Bucket recovered notional by its claim date"). A row carrying
  a breakdown now sums only the entries whose OWN `trading_day` matches
  this artifact's `trading_day_iso`, mirroring
  `dca_risk.deployed_usd_by_claim_date()`'s own identical per-claim-date
  attribution -- a purely cross-day row contributes `0.0` here (its
  dollars already contributed to its own day's allocation, on the
  earlier run that recorded it), and a row mixing an older claim with
  today's own fresh contribution contributes only today's own portion.
- **The immediate placement-failure branch's terminal-inclusive fallback
  now BOOKS a still-live matched order instead of reporting it
  "blocked".** `place_whole_share_buy`'s own failure-envelope fix now
  routinely surfaces a live (not yet terminal) inner order via
  `WholeSharePlacementTerminalError`, and this branch's own broker
  terminal-inclusive lookup can independently surface the same kind of
  live order too -- but reporting "blocked" here (an earlier version of
  this fix did) never called `record_buy()`/bumped `running_deployed_7d`,
  letting later candidates in the same batch exceed the daily attempt
  count, daily/weekly notional caps, or the kill switch while this
  accepted order remained live and completely uncounted, mirroring the
  exact hazard the ordinary success path's own "not yet visible"/
  nonterminal branch was already fixed to close (Codex review: "Book live
  failure-envelope orders before continuing"). Books the LOCALLY-CLAMPED
  quantity/notional (`_whole_share_risk_gates` already narrowed these
  before the placement attempt, so they reflect what was actually
  submitted) as `"submitted"`/unfilled -- the whole-share LEDGER's own
  claim stays unresolved either way.
- **A terminal settlement now reconciles a DELTA against an earlier
  provisional recovery, instead of silently suppressing the correction
  forever.** The order-id dedup added above (to stop rebooking a
  `"live_order_found"` claim's order once it terminates) correctly
  skipped rebooking the FULL amount again, but a provisional booking used
  the claim's own submitted quantity at the CACHED signal-time price --
  not the broker's real fill price, unknown while an order is still live
  -- so the terminal settlement's real notional (using the broker's own
  `average_price` when available) can genuinely differ from what was
  provisionally recorded. Unconditionally skipping left the provisional
  artifact's dollar figure permanently wrong, silently baked into every
  future `dca_risk.deployed_usd_by_claim_date()` sum across artifacts for
  that claim date (Codex review: "Reconcile provisional deployment to the
  final fill"). A new `_find_same_day_recorded_order` helper (the shared
  scan behind the order-id dedup check, now also exposing the matched
  row itself) lets `_book_reconciled_whole_share_order` compute
  `delta = real_settlement - already_recorded_amount` and publish/book
  only that delta -- summing both artifacts' `whole_share_claim_breakdown`
  amounts then nets out to the true total. A zero delta still skips
  cleanly, matching the original dedup's intent.
- **`_merge_whole_share_submission_outcomes` now merges the settlement-
  state fields too, not just amounts/quantities/IDs/claim dates.**
  `merged = dict(new)` kept only the NEWER outcome's own
  `whole_share_settlement_reconciliation_needed`/
  `whole_share_settlement_shortfall_usd` -- if `existing` was a recovered
  live order still awaiting ledger settlement (possibly with a nonzero
  outstanding shortfall from a clamped fire whose `settle()` call itself
  failed) and the same ticker then submitted a fresh, cleanly-settled
  order in the same run, the fresh outcome's own `False`/`0.0` fields
  silently overwrote and discarded the unresolved recovery's marker and
  shortfall -- exactly the persisted evidence an operator needs when
  ledger settlement fails (Codex review: "Preserve settlement state when
  aggregating outcomes"). Now ORs the reconciliation-needed flag (true if
  EITHER side still needs it) and SUMS any outstanding shortfalls,
  mirroring how `submitted_amount_usd`/`whole_share_quantity` are already
  aggregated rather than overwritten.
- **The whole-share ledger's `_SYMBOL_PATTERN` now requires at least one
  letter.** The bare `"[A-Z.]{1,6}"` character class also accepted a
  symbol made ENTIRELY of dots (e.g. `".."` or `"."`) -- every ledger
  method still resolves the accepted symbol as a real filesystem path
  component (`self.root / symbol / "ledger.json"`), and `".."`
  specifically writes the ledger and its lock in the PARENT directory
  instead of under this ledger's own whole-share stream tree, risking
  collisions with unrelated artifacts (Codex review: "Require a letter in
  whole-share symbols"). The pattern now requires a lookahead match for
  at least one uppercase letter (`^(?=.*[A-Z])[A-Z.]{1,6}$`), rejecting
  both `"."` and `".."` outright while leaving every genuine ticker
  symbol (letters, optionally with dots) unaffected.
- **The delta-reconciliation fix above had three of its own bugs, all
  found by the very next Codex review of that commit -- all fixed
  together.** The persisted row's dollar figure is actually
  `"planned_amount_usd"`, never `"submitted_amount_usd"` (that field name
  never survives serialization to disk -- `_build_buy_candidates` renames
  it), so the delta computation always read `0.0` and published the FULL
  settlement again every time (Codex review: "Read the persisted
  provisional amount before applying the delta"). The recorded row can
  also be an AGGREGATE of several separate orders' notionals merged by
  `_merge_whole_share_submission_outcomes`, so treating the row's whole
  total as the provisional amount of just the ONE order now settling
  could misattribute an entirely different, still-live order's dollars
  into the delta (Codex review: "Associate provisional notionals with
  individual order IDs"). And calling `ctx.state.record_buy()` for the
  adjustment incremented `submitted_order_count_by_day` a SECOND time for
  one real physical order, since the provisional booking already
  incremented it once (Codex review: "Avoid counting a settlement
  adjustment as another order attempt"). Fixed together: the recorded
  provisional amount is now read from the matched row's own
  `whole_share_claim_breakdown` entry for THIS claim's specific
  `claim_trading_day_iso` (not a top-level field, and immune to
  aggregation since each claim keeps its own breakdown entry), and the
  delta-adjustment branch no longer calls `record_buy()` at all -- only
  `running_deployed_7d` and the persisted breakdown need the correction,
  since `within_caps` only ever checks `ctx.trading_day`'s own totals,
  never an arbitrary historical `claim_trading_day_date`.
- **The immediate (in-line) placement-failure path's own confirmed
  zero-fill branch had the SAME release-failure double-count hazard
  already fixed in the cross-day reconciliation helper.** It recorded
  the attempt via `record_buy()` UNCONDITIONALLY, before
  `ledger.preserve_pending()` even ran -- if that release itself raised,
  the claim stayed genuinely unresolved, but the attempt was already
  counted, and a LATER reconciliation attempt (once release finally
  succeeded) would record the identical attempt a second time for one
  real cancelled/rejected order (Codex review: "Deduplicate zero-fill
  attempts when claim release fails"). Moved the `record_buy()` call to
  run only after `preserve_pending()` succeeds, mirroring
  `_reconcile_unresolved_whole_share_claim`'s own identical fix from the
  previous round.
- **The cross-day recovery pre-pass now discovers a ticker's unresolved
  claim even after an operator removes it from
  `config.whole_share_tickers` entirely.** The pre-pass previously only
  iterated the CURRENT config's own ticker set -- if an accepted or
  ambiguous order remained unresolved when the ticker was later delisted
  or moved to `excluded_tickers`, no subsequent run's pre-pass would ever
  scan its ledger directory again, permanently stranding the real fill
  (and any clamped shortfall) unbooked while `deployed_usd_by_claim_date()`
  and the kill switch silently undercounted real broker spend (Codex
  review: "Reconcile claims for tickers removed from configuration"). A
  new `WholeShareAccumulationLedger.discover_symbols()` method lists
  every ticker with an existing ledger directory under the root
  (cheap -- directory names only, no per-symbol lock/read); the pre-pass
  now reconciles the union of the currently-enrolled set (minus
  `excluded_tickers`, as before) and any discovered symbol not currently
  in `whole_share_tickers` at all. A no-op for a symbol with nothing left
  to reconcile.
- **Two more bugs found in the settlement-delta reconciliation feature,
  both fixed by tightening its scope rather than adding more machinery.**
  A same-day settlement (provisional booking and terminal settlement both
  land on the SAME trading day) made the cross-day exclusion check's own
  breakdown-date comparison false, so a later run's
  `load_prior_same_day_allocations` still counted the adjustment as a
  fresh attempt even though the provisional booking already counted it
  once (Codex review: "Exclude same-day settlement deltas from attempt
  counts"). Fixed with an explicit `whole_share_settlement_adjustment`
  marker on the outcome, persisted through `_build_buy_candidates`/
  `_failure_candidate_row` like every other `whole_share_` field, that
  `load_prior_same_day_allocations` now excludes unconditionally,
  regardless of date. Separately, when the real settlement comes in
  BELOW the already-recorded provisional amount, the delta is negative --
  `schemas/run_state.schema.json` requires `planned_amount_usd` to be
  non-negative, and a negative `submitted_amount_usd` would also be
  silently discarded outright by this same loader's own `amount <= 0:
  continue` guard (Codex review: "Represent negative reconciliation
  deltas outside order amounts"). Rather than adding a signed-adjustment
  representation outside the constrained field (a deeper schema/loader
  change), a downward correction is now simply never published: the
  existing provisional record stands as a deliberately conservative,
  upper-bound estimate. Erring toward OVER-counting deployed capital is
  the safe direction for a risk/budget system -- it can only make this
  run's own caps/kill-switch MORE conservative, never less.
- **`load_prior_same_day_allocations`'s claim-date bucketing (added two
  rounds ago) dropped a fresh order's real dollar amount when merged with
  an older recovery.** The fix summed only the `whole_share_claim_
  breakdown` entries matching today's own `trading_day_iso` -- but a row
  can merge an OLDER recovered claim (which DOES carry a breakdown entry)
  with a genuinely FRESH order placed today (which does NOT; only a
  reconciled recovery ever gets one). Summing only date-matching entries
  then attributed `$0` to that fresh order entirely, silently dropping
  real submitted capital from same-day/weekly-cap reconstruction (Codex
  review: "Preserve fresh notional in mixed recovery rows"). Now computes
  the UNACCOUNTED remainder (the row's own total minus the breakdown's
  own grand total, regardless of date) and attributes that remainder to
  today too, alongside any breakdown entries that do match -- mirroring
  `dca_risk.deployed_usd_by_claim_date()`'s own identical handling.
- **`_build_buy_candidates` now emits a row for a whole-share ticker's
  recovered claim even when NO decision covers it at all.** The
  orphaned-ledger discovery pass (`WholeShareAccumulationLedger.
  discover_symbols()`, added above) can reconcile a ticker no longer in
  `config.whole_share_tickers` -- such a ticker is never evaluated into
  `decisions`, so its outcome was invisible to both the main
  decision-keyed loop (nothing to key off) and the inverse-Martingale
  recovery fallback (a different marker). The reconciled fill never
  reached `run_state.json`/`buy_candidates.csv`, and because the ledger
  claim was already resolved, no LATER run could ever rediscover the
  spend either -- persisted cap/kill-switch history permanently
  undercounted a real broker order this run's own in-memory
  `running_deployed_7d` only protected for its own lifetime (Codex
  review: "Persist recovered outcomes for orphaned whole-share
  tickers"). A new fallback, analogous to the existing inverse-Martingale
  one, emits a `"recovered_whole_share_claim"`-reasoned row from the
  outcome alone for any ticker with a `"submitted"` whole-share outcome
  and no covering decision.
- **The two new recovery-only row shapes (inverse-Martingale lease and
  orphaned whole-share claim) now carry identical column sets.** When a
  run has NO decisions at all but recovers both kinds in the same pass,
  `_write_csv` derives `buy_candidates.csv`'s header solely from row 0
  and uses the default `extrasaction="raise"` -- whichever row landed
  first, the other row's extra `whole_share_*` keys (or their total
  absence) would raise `ValueError` the moment the artifact was written
  (Codex review: "Keep recovery-only CSV rows on one schema"). The
  inverse-Martingale recovery row now carries the same `whole_share_*`
  keys as the whole-share recovery row, defaulted to `None`/`False` --
  a lease recovery genuinely has no whole-share detail to report.
- **The date/holiday-gated early-return path's own whole-share
  reconciliation loop now also discovers orphaned ledgers.** The normal
  (gate-passing) pre-pass's `discover_symbols()` fix (added above) only
  covered that ONE code path -- the gated path runs its OWN separate
  reconciliation loop (see `_book_reconciled_whole_share_order_durably`'s
  caller in `_run_once_impl`), which still iterated only the current
  `config.whole_share_tickers` set. A ticker removed from configuration
  whose every subsequent invocation lands beyond the final approved
  trading date returns through THIS gated branch on every future run,
  never reaching the discovery-aware normal pre-pass at all, so its
  claim would remain permanently unreconciled (Codex review: "Discover
  orphaned ledgers before the gated return"). Now unions the same
  discovered-symbols set into this loop's own iteration.
- **Declined: "Persist fills reconciled on the gated path" (Codex
  review).** The gated-path helper `_book_reconciled_whole_share_order_
  durably` books ONLY into in-memory `StrategyState` (`state.record_buy`)
  and writes no run artifact at all -- by design, mirroring
  `_book_recovered_lease_durably`'s own IDENTICAL, already-reviewed scope
  for the inverse-Martingale lease recovery on the same gated path (that
  helper's own docstring: "does NOT publish a candidate_outcomes row --
  no run artifact is written on the gated path either way"). Since
  `StrategyState` has no persistence of its own, this is a genuine,
  pre-existing limitation -- but one shared identically by both recovery
  mechanisms, not something unique to the whole-share addition. Properly
  fixing it means designing a NEW durable-artifact-writing mechanism for
  the gated early-return path, which would need to cover the
  inverse-Martingale lease recovery too for consistency; that is a
  broader architecture change out of scope for this PR. Tracked as a
  follow-up: a future change should give the gated path SOME durable
  artifact (or otherwise fold its `record_buy()` calls into a mechanism
  `_apply_deployment_history()` can see) for both recovery mechanisms
  together, rather than hardening one in isolation.

### Booking a live match found by the FIRST reconciliation lookup, passing a verified fill into execution logging, and preserving recovered zero-fill attempts through the batch

A fifteenth review round found three more issues in the same immediate
placement-failure/execution-logging paths, plus one issue in the unrelated
transcript-restore tool `scripts/backup_strategy.py`. Three were fixed; the
fourth was evaluated and declined -- see the end of this subsection for why.

- **The live-only lookup's own "still-live" branch now books the order,
  instead of reporting "blocked".** The immediate placement-failure
  handler tries TWO reconciliation lookups in sequence: a live-only lookup
  first, then (if that finds nothing) a terminal-inclusive fallback. The
  terminal-inclusive fallback's own "still-live" branch was already fixed
  (two rounds ago, "Book live failure-envelope orders before continuing")
  to book a matched live order as submitted/unfilled rather than report
  "blocked" -- but the FIRST lookup's own, structurally identical "still
  live" branch was never given the same fix, so an order the live-only
  lookup itself found (rather than only the any-state fallback) still
  left later candidates in the same batch blind to a real, uncounted
  broker order (Codex review: "Book matched live orders before
  continuing"). Now mirrors the sibling branch exactly: books the
  locally-clamped quantity/notional via `ctx.state.record_buy`, bumps
  `running_deployed_7d`, and publishes a `"submitted"` outcome, while the
  whole-share ledger claim itself stays reserved for a later
  reconciliation attempt.
- **The ordinary success path's execution log now uses the fill it already
  verified, instead of re-querying the broker.** The post-placement
  success path already reads `placed_order`'s own `cumulative_quantity`/
  `average_price` to compute `order_notional` -- the broker's own
  authoritative evidence for what actually executed -- but then called
  `_log_execution` without passing that evidence along, letting it
  re-query `broker.get_order_fill()` for the SAME order. That query is
  eventually consistent and can disagree with (or simply miss) evidence
  this function already has (Codex review: "Pass the verified fill into
  execution logging"). Now constructs an `OrderFill` from the same
  `fill_price`/`quantity` already used to compute `order_notional` and
  passes it as `known_fill=`, matching every other whole-share
  reconciliation branch's identical pattern; falls back to a query only
  when `placed_order` carried no usable price at all.
- **A recovered zero-fill attempt now survives the batch loop, instead of
  being silently overwritten by today's own outcome.** `_book_reconciled_
  whole_share_order`'s `zero_fill_attempt` branch deliberately publishes
  `"status": "failed"` (a real, countable broker attempt, but no
  purchase) -- the outer batch loop's own seeded/mid-call recovery
  preservation only ever looked for `"status" == "submitted"`, so this
  recovery was overwritten the instant today's own processing of the SAME
  ticker produced ANY outcome at all, erasing the attempt from the run
  artifact even though `ctx.state.record_buy()` already counted it toward
  `max_orders_per_day` (Codex review: "Preserve recovered zero-fill
  attempts through the batch"). The `zero_fill_attempt` branch now stamps
  an explicit `whole_share_zero_fill_attempt: True` marker on the
  published outcome (status alone is indistinguishable from an ordinary
  failed candidate), and the batch loop's own recovery-preservation
  checks now look for either `"submitted"` or this marker.
- **Declined: "Reconcile symbols explicitly moved into the active
  category" (Codex review, `scripts/backup_strategy.py`).**
  `_strip_moved_symbols_from_retained_categories` already strips a symbol
  moved INTO a mentioned `excluded`/`whole_share` category out of any
  OTHER retained category still holding it (e.g. moving BYDDY into
  `whole_share` removes it from a stale `active` list) -- but deliberately
  never treats `active` itself as a moved-FROM source, per that function's
  own docstring: the active-universe extraction has no reliable "genuinely
  mentioned" marker and is already known to over-extract capitalized words
  from ordinary prose. Codex's finding asks for the mirror-image case: a
  symbol moved INTO `active` (e.g. "BYDDY is back in the active universe")
  that is still sitting in the existing `excluded`/`whole_share` category
  when the transcript never mentions those sections. A first attempt at
  this fix -- raising when the transcript's own extracted `active` list
  overlaps a retained `excluded`/`whole_share` array -- was implemented and
  immediately broke an existing, deliberately-designed regression test,
  `test_write_configs_preserves_whole_share_despite_descriptive_prose_
  mentioning_it`: ordinary prose like "BYDDY is a whole-share ticker" is
  proven (by that test) to over-extract "BYDDY" into the ACTIVE list too
  (the universe-block capture stops at the word "whole-share", but
  everything before it on the same line, including "BYDDY is a ", is
  captured first) -- exactly the over-extraction hazard the docstring
  already warned about, now confirmed concretely rather than
  theoretically. Raising on that overlap would reject a transcript that
  never actually asked to move anything, on every future restore where
  BYDDY (or any other whole-share/excluded symbol) is merely discussed in
  prose near the active-universe block. Fixing this safely needs a
  stricter, colon-anchored active-list extraction path (or an equivalent
  fidelity improvement) distinguishing a real "moved to active" intent
  from incidental prose -- a deeper rework of `extract_tickers` out of
  scope for this PR. Tracked as a follow-up.

### Persisting orphaned zero-fill recoveries, and keeping them on their own claim day

A sixteenth review round, against commit `3816e40`, found three more issues in the
same reconciliation subsystem, all fixed.

- **The orphaned-ticker fallback in `_build_buy_candidates` now also persists a
  zero-fill recovery.** That fallback (added for `WholeShareAccumulationLedger.
  discover_symbols()`'s orphaned-ledger discovery) required `"status" ==
  "submitted"` plus one of three submission-shaped fields -- but a confirmed
  zero-fill recovery (`_book_reconciled_whole_share_order`'s own
  `zero_fill_attempt` branch) deliberately publishes `"status": "failed"` with a
  `whole_share_zero_fill_attempt` marker instead, since nothing was purchased.
  For an ORPHANED ticker (removed from `config.whole_share_tickers`) this meant
  the attempt had no persisted trace at all: `ctx.state.record_buy(..., 0.0)`
  counted it in memory for the run that recovered it, but the whole-share
  ledger claim was already released, so no LATER run's own ledger scan could
  ever rediscover the attempt either -- permanently undercounting
  `max_orders_per_day` (Codex review: "Persist orphaned zero-fill recoveries").
  The fallback's filter now also accepts the `whole_share_zero_fill_attempt`
  marker, and emits a `"failed"`-status row (matching the outcome's own
  status) instead of forcing `"submitted"`.
- **A zero-fill recovery's claim day is now recorded in
  `whole_share_claim_breakdown`, not just inside `skip_reason` prose.**
  `load_prior_same_day_allocations`'s cross-day-recovery exclusion (added for
  the earlier "Keep recovered attempts on their original claim dates" fix)
  reads ONLY the machine-readable `whole_share_claim_breakdown` field to tell
  a cross-day recovery apart from a fresh same-day attempt -- it never parses
  `skip_reason` text. The `zero_fill_attempt` branch recorded the claim's
  original trading day only in that free-text message, so the exclusion check
  always evaluated `False` for a zero-fill row: a cross-day zero-fill
  recovery reloaded by a LATER run was misattributed to that later run's own
  trading day, double-counting the attempt (once via `record_buy(...,
  claim_trading_day_date, ...)` at recovery time, again via this
  misattributed reload) against `max_orders_per_day` (Codex review: "Keep
  recovered zero-fill attempts on the claim day"). The branch now stamps
  `whole_share_claim_breakdown: [{"trading_day": claim_trading_day_iso,
  "amount_usd": 0.0}]` on the published outcome, mirroring every other
  reconciled-order branch's identical breakdown convention.
- **`dca_config.py`'s own whole-share symbol validation now matches the
  ledger's hardened pattern.** `dca_whole_share_accumulator._SYMBOL_PATTERN`
  was hardened (an earlier round) to require at least one letter, rejecting a
  dots-only value like `".."` (path-traversal-risky as a filesystem
  component) -- but `dca_config._WHOLE_SHARE_SYMBOL_PATTERN`, the loader-level
  check guarding a supported alternate `$DCA_CONFIG_DIR` (never covered by the
  bundled `schemas/tickers.schema.json` check), still accepted it. Such a
  value would pass straight through into `StrategyConfig.whole_share_tickers`
  and only fail later at ledger use, past the intended fail-closed config
  boundary (Codex review: "Reject dot-only whole-share symbols at config
  load"). Now uses the identical `^(?=.*[A-Z])[A-Z.]{1,6}$` pattern.

## Tests

- `test_dca_whole_share_accumulator.py` — pure decision-function (`apply_contribution`)
  and ledger unit tests, including the idempotent-cache-resolution, concurrent-
  contribution-preservation, settlement-shortfall-restoration, unresolved-claim
  (overlapping-caller-blocked / stale-claim-reclaimed), `still_claimed` fencing
  (current / fenced-out-after-reclaim / resolved / no-key), `preserve_pending` refusing
  to credit a fenced-out or already-resolved caller, `settle`'s shortfall math using the
  ledger's own rounded price rather than a caller-supplied submitted notional,
  `settle` refusing to resolve a fenced-out caller's stale claim (still recording the
  real shares purchased, but not crediting a shortfall or marking the entry resolved),
  `still_claimed` returning `False` (not `True`) for an already-resolved entry so a
  fenced-out original caller cannot pass this check and submit a duplicate order after
  a reclaiming retry has already settled the same fire, and sub-cent-price-rejection
  cases.
- `test_dca_config.py` — `load_tickers`'s `whole_share_tickers` overlay tests, plus the
  active/excluded/whole_share pairwise-disjointness rejection tests (including a
  case-only collision) and a case-folded duplicate WITHIN `whole_share` alone; plus
  `load_strategy_config`-level tests that an overlap hidden by a partial `tickers.json`
  (only declaring `"whole_share"`, colliding with the inherited default active universe)
  and a case-folded duplicate supplied via an explicit `whole_share_tickers=` override are
  both rejected after the base/JSON/override merge, not just by `load_tickers` itself.
- `test_dca_live_adapters.py` — `RobinhoodBroker.review_whole_share_buy`/
  `place_whole_share_buy`/`is_tradable_for_whole_share_buy` tests, mirroring the
  existing `review_market_buy`/`place_market_buy`/`is_tradable_for_fractional_buy`
  coverage (including that the whole-share tradability check does NOT require
  `fractional_tradability`, unlike the fractional one); the
  `test_broker_implementations_satisfy_the_broker_adapter_protocol` conformance test
  also covers `DryRunBroker` and `AlpacaBroker`.
- `test_refresh_active_ticker_histories.py` / `test_tickers_daily_update.py` —
  `active_symbols()`/`_active_tickers()` including `whole_share` tickers alongside
  `active` (deduped), and that a BYDDY-only history update lands in
  `changed_active_tickers` so `daily_per_ticker_calibration.py`'s event-driven rebuild
  can actually fire for it.
- `test_whole_share_wiring.py` — end-to-end tests through
  `near_close_dca._process_buy_candidate`'s dispatch to `_process_whole_share_candidate`:
  accumulating, firing, dry-run pure-preview isolation (including that a dry run never
  blocks a later real scheduled run and that a preview is clamped/blocked by the same
  risk gates), insufficient buying power, same-day duplicate skip, review block,
  placement failure, the `max_per_order_usd` ceiling (both the pure-block and the
  clamp-and-settle-shortfall cases), the daily budget cap, the spread gate (missing data
  and too-wide spread), settlement-failure-still-publishes-"submitted", the
  execution-record log entry on success, the deployment kill switch (caps/does-not-cap),
  the budget-tier planning preview (including the pending-plus-contribution-crosses-a-
  share-price scenario and the max_per_order_usd ceiling clamp), a fenced-out caller
  losing its claim to a reclaiming overlapping attempt, a dry-run preview rejecting a
  broker without whole-share support and blocking on insufficient buying power, that an
  accepted order is published before a fallible fill lookup, the accumulator-detail and
  planned-notional fields persisted onto `buy_candidates` rows, that an
  accumulator-only run classifies as `no_orders_submitted` rather than
  `not_submitted_budget_exhausted`, that the tier-planning preview clamps using the
  ledger's own rounded price (not the raw signal price), that a corrupt/unreadable
  whole-share ledger during tier planning does not abort planning for other,
  independent active tickers, that `RunResult.active_universe` (and therefore
  missing-fill coverage reporting) includes whole-share evaluated tickers alongside the
  normal tradable universe, that a below-threshold accumulation is classified
  `MISSING_FILL_ACCUMULATING` rather than falling through to `MISSING_FILL_FAILED_TRIGGER`
  (both the dry-run and real paths), that a successful dry-run fire calls
  `_log_execution` the same as a real placement, that a dry-run's own ledger-read
  failure is isolated the same way the real path's is, and that a scheduled run blocks
  on `is_tradable_for_whole_share_buy` returning `False` (checked unconditionally,
  unlike `review_whole_share_buy`, which only runs on a manual run) while preserving the
  pending balance, that a same-day broker order appearing between the top-of-function
  check and placement (standing in for a reclaiming attempt's order landing mid-flight)
  aborts this caller's own placement instead of submitting a second order, that a
  successful dry-run review counts toward `order_reviews_attempted`, that an
  `OrderConfirmationRequiredError` from placement is classified
  `not_submitted_confirmation_required` (not `failed`) and still restores the pending
  balance, that the tier-planning preview does not double-count an already-recorded
  same-day slot on a retry (including the case where that slot was resolved via
  `preserve_pending` rather than a below-threshold accumulation, whose restored balance
  alone could otherwise cover a share), that a dry-run preview honors an already-recorded
  slot the same way, that a tradability-lookup exception restores the deducted pending
  balance and counts an error, and that the per-order ceiling's share count is computed
  with exact-cent arithmetic (a $0.10 share price under a $10 ceiling permits the full 100
  shares, not 99 from a raw float floor-division); that a manual-run review-call exception
  restores the deducted pending balance and counts an error; that the second,
  pre-placement duplicate-order-lookup exception restores the deducted pending balance
  and counts an error; that a successful fire leaving a remainder persists the
  authoritative post-settlement pending/contribution fields on the `"submitted"` outcome;
  and that a whole-share ticker whose pending balance alone already covers a share is
  re-dispatched and fires even when today's own signal is `NO_ACTION`; that a
  `MANUAL_REVIEW` decision is never redispatched even with an actionable pending balance;
  that redispatch eligibility is compared with the same cent-rounded arithmetic
  `apply_contribution` uses (a $10.00 balance against a $10.004 quote still fires); that a
  ledger read failure during the redispatch eligibility check dispatches anyway so the
  authoritative `record_contribution` call classifies the SAME failure properly; and that
  a redispatched candidate blocked by a safety gate (insufficient buying power) still
  persists its block reason and planned notional in `buy_candidates.csv`/`run_state.json`;
  that a redispatch abstains entirely when finance and Robinhood disagree sharply on
  price (both individually `NO_ACTION`, but wildly different quotes); and that the
  deployment kill switch caps a redispatch's EXECUTABLE fire to `$0`
  (`decision.selected_base_amount_usd`'s bare default for a non-BUY decision), blocking it
  outright rather than letting it spend the full accumulated balance while the switch is
  engaged; that a dry-run preview's own `review_whole_share_buy` exception is isolated to
  this candidate rather than escaping and aborting the whole demo run; and that the
  tier-planning preview's own `select_budget_tier` call is fed the SAME kill-switch-capped
  notional execution actually uses (a $150 pending balance well past the $129
  tier-escalation threshold previews as ~$0, not the uncapped ~$148.50, once the kill
  switch is engaged), verified by intercepting the module-level `select_budget_tier` call
  directly since a SEPARATE, later recompute inside `build_run_artifacts` re-derives its
  own reporting-only `planned_spend_usd` from each outcome's raw pre-clamp
  `whole_share_planned_notional_usd` and is unrelated to this fix; and that a dry-run
  preview's own `is_tradable_for_whole_share_buy` exception is likewise isolated to this
  candidate rather than escaping and aborting the whole demo run; and that a dry-run
  preview's own `available_buying_power` exception is likewise isolated to this
  candidate rather than escaping and aborting the whole demo run; that the FIRST,
  top-of-function `same_day_strategy_order_exists` check's own exception is isolated the
  same way, touching no ledger state (it runs before any contribution is recorded); that
  the placement-time `still_claimed` recheck's own exception fails the candidate closed
  without mutating the ledger (unlike every other ledger-error boundary here, this one
  must never call `preserve_pending`, since a reclaiming caller may actually own the
  claim by then); and that once the dry-run preview's risk gates pass, the spread fields
  they computed are carried onto every subsequent outcome (buying-power block,
  tradability block, review block, and the final `"dry_run"` success), not left at
  `spread_status="not_evaluated"`; and that `_preserve_and_block`'s own
  `ledger.preserve_pending` exception (triggered here via the `max_per_order_usd`
  ceiling block) is isolated the same way — the outcome reports the restoration failure
  itself rather than the original block reason, and the exception never escapes to
  abort unrelated candidates; that this SAME restoration-failure outcome, reached via a
  NO_ACTION preserved-fire redispatch, still carries a `"whole_share_"`-prefixed marker
  so `_build_buy_candidates`'s non-BUY filter doesn't silently drop it; and that a
  restoration failure AFTER a placement failure (`place_whole_share_buy` itself raises,
  then the except-clause's own `preserve_pending` call also raises) is isolated the same
  way, returning a `"failed"` outcome naming both failures rather than letting the second
  exception replace the first and escape the batch; that the confirmation-required
  path's OWN `preserve_pending` restoration failure is isolated the same way (a data
  error, not `"not_submitted_confirmation_required"`); that the FIRST, top-of-function
  duplicate-lookup failure's own outcome carries a `"whole_share_"` marker for a
  NO_ACTION redispatch; and that a fresh `Action.BUY` decision with both sources present
  blocks when their execution quotes disagree (mirroring the redispatch path's own
  abstention) but still fires normally when the sources agree within tolerance; that
  `_whole_share_redispatch_reconciled_price` returns the HIGHER of two agreeing quotes
  regardless of which source (Finance or Robinhood) happens to be higher, and still
  refuses a genuine disagreement beyond tolerance
  (`test_reconciled_price_uses_the_higher_of_two_agreeing_quotes`); that a manual,
  NO_ACTION-redispatched candidate's `"not_submitted_confirmation_required"` outcome
  carries a `"whole_share_"` marker too; and that the tier-planning preview reconciles a
  fresh BUY's execution price the SAME way execution does (a hand-built
  `ReconciledDecision` where Finance wins the reconcile_signals selection with a LOWER,
  but still in-tolerance, quote than Robinhood's), so the live tier-selection call sees
  the SAME reconciled (higher-quote), execution-accurate plan rather than the
  unreconciled winning signal's own lower price; that a redispatch outcome with NO
  `"whole_share_"`-prefixed field at all (the top-of-function same-day-duplicate SKIP)
  is still persisted in `buy_candidates.csv`/`run_state.json`, verifying
  `_build_buy_candidates`'s `synthetic_source`-based identification (not an outcome-key
  scan) catches it; that a `settle()` failure after a per-order-ceiling clamp keeps the
  outcome `"submitted"` while setting `whole_share_settlement_reconciliation_needed=True`
  and the exact `whole_share_settlement_shortfall_usd` the failed `settle()` call would
  have restored; and that a SEPARATE, post-settlement `pending_usd()` read failure (with
  `settle()` itself having already succeeded) preserves the shortfall settle() actually
  restored in its fallback value, rather than reporting the bare pre-settle snapshot; that
  an ambiguous placement failure (`place_whole_share_buy` raises, but the broker's own
  same-day order history shows this deterministic `ref_id` already accepted) settles
  (never restores) the claim and reports a `"submitted"` outcome with the broker's real
  order id and zero errors counted, instead of blindly restoring credit the broker-side
  order already consumed; and that a successful, threshold-crossing dry-run fire reports
  `whole_share_pending_usd`/`whole_share_contribution_usd` from the same `preview`
  decision the below-threshold branch and the live path both already report them from,
  instead of omitting them from the `"dry_run"` success outcome; that when the
  reconciliation lookup itself raises (a response timeout combined with a transient
  order-history outage), the candidate fails closed WITHOUT mutating the ledger at all —
  neither settled nor restored — rather than falling through to the conservative
  restoration branch and freeing credit for an order that may actually be live at the
  broker; and that a multi-share fire clamped by the per-order ceiling, reconciled via
  ref_id after an ambiguous placement failure, reports the AUTHORITATIVE post-settlement
  `whole_share_pending_usd` (the restored, unsubmitted shares' credit) rather than the
  pre-settlement snapshot; that a dry-run preview clamped by the per-order ceiling
  ($16 at $4/share previewing 4 shares, clamped to 2 by a $10 ceiling) reports
  `whole_share_pending_usd=8.0` (the unsubmitted 2 shares' $8, added back onto the
  pre-clamp preview's $0 remainder), not the unclamped preview's bare remainder; and that
  a same-day retry reaching the broad, ref-id-agnostic initial duplicate-order check
  reconciles (settles) an earlier attempt's unresolved claim — left deliberately
  unresolved by a placement failure whose ref-id reconciliation lookup ALSO failed —
  before returning its ordinary `"skipped"` outcome, reading the actual submitted
  quantity from the broker's own matched order rather than the cache's unclamped count;
  and that a claim left unresolved on an EARLIER trading day (a doubly-ambiguous
  placement whose reconciliation lookup also failed) is discovered and reconciled by a
  LATER trading day's run against its ORIGINAL trading day/ref_id, even though today's
  own broad duplicate check never fires (it only looks at orders from today onward),
  with the reconciled purchase booked into `ctx.state`/`running_deployed_7d` (not just
  the whole-share ledger); and that a dry run never reconciles a cross-day unresolved
  claim at all, even when the broker could confirm an exact ref-id match for it.
- `test_near_close_dca.py` — `test_a_failure_synthesized_candidate_row_preserves_whole_share_fields`
  locks in that `_failure_candidate_row` (the failure/retry-merge path's OWN row builder,
  separate from `_build_buy_candidates`) carries the same `whole_share_pending_usd`/
  `whole_share_contribution_usd`/`whole_share_quantity`/
  `whole_share_settlement_reconciliation_needed`/`whole_share_settlement_shortfall_usd`
  fields through, so a failed-after-submission run doesn't lose a whole-share
  candidate's settlement details.
- `test_config_invariants.py` — `test_whole_share_tickers_are_not_excluded_from_month_end_risk_reporting`
  locks in that `config/scheduled_tasks.json`'s `month_end_volatility_risk_policy.universe.excluded`
  never lists a whole-share ticker.
- `test_dca_whole_share_accumulator.py` — that reclaiming a stale fire (both via
  `record_contribution` and via `cached_fire_decision`'s optional price parameter)
  re-prices it against the CURRENT quote rather than the original attempt's stale cached
  price/share-count/notional; that reclaiming a stale fire reconciles the repriced
  remainder against the ledger's CURRENT balance via a delta rather than an absolute
  overwrite, so a later contribution recorded under a different `idempotency_key` while
  the fire sat stale is preserved rather than silently discarded when the stale fire is
  finally reclaimed and repriced; that a reclaim repricing to `shares_to_buy == 0` marks
  its cache entry resolved so a LATER stale reclaim of the same key returns the
  already-resolved "nothing to do" decision instead of re-applying the same frozen
  original total against a balance a different key has since spent down (which would
  otherwise drive `pending_usd` negative); that a DOWNWARD reprice which would need to
  draw more from the shared pool than is currently unspent defers instead of applying a
  delta that would drive `pending_usd` negative (both via `record_contribution` and via
  `cached_fire_decision`'s read-only preview); that a below-threshold retry (the
  original idempotency-key test) and an explicitly-resolved cached entry both report
  `contribution_usd=0.0` on the retry/preview, not the ORIGINAL call's positive amount;
  that a reclaim's RETURNED decision reports the reconciled `state["pending_usd"]`
  rather than the isolated `repriced.pending_after_usd` when a different key changed the
  balance since the claim was recorded; that `cached_fire_decision`'s own reprice-preview
  branch reports that SAME reconciled figure (not the isolated remainder) for the
  identical $15/$4/$19 scenario, purely as a read-only preview that never mutates the
  ledger; that `record_contribution` rejects a path-traversal-shaped ticker symbol
  (e.g. `"../../outside"`) rather than resolving it onto the filesystem; and that a
  stale-claim reclaim appends its own `"reclaim_reprice"`-decision event to `events()`
  (distinct from a fresh entry's `"pending"`/`"fire"`), carrying the repriced quantity and
  the reconciled `pending_after_usd`, rather than mutating the ledger silently; and that an
  overlapping retry still within `UNRESOLVED_CLAIM_STALE_SECONDS` (both via
  `record_contribution` and via `cached_fire_decision`'s read-only preview) reports
  `contribution_usd=0.0` rather than the original cached positive amount, mirroring the
  resolved-entry branch's identical fix; that a zero-contribution retry against an
  ALREADY-RESOLVED slot (a preserve_pending-restored fire) redispatches once the restored
  balance alone covers a share (both via `record_contribution` and via
  `cached_fire_decision`'s read-only preview), re-opening the slot rather than returning
  the flat zero-shares "nothing to do" outcome forever; that a NONZERO-contribution
  retry against that SAME resolved slot deliberately does NOT redispatch (via
  `cached_fire_decision`'s own `contribution_usd` parameter), keeping the pre-existing
  "nothing new" behavior since re-opening for genuinely new money risks double-counting
  the original contribution; that `unresolved_claims_any_day` finds a claim recorded (and
  still unresolved) under an EARLIER trading day, returns `[]` once that claim resolves,
  never matches under the wrong trading day, and returns EVERY unresolved match (sorted
  oldest first) rather than only the first when a ticker has accumulated more than one;
  that `_reconcile_unresolved_whole_share_claim` (imported from `near_close_dca`) settles
  using the claim's OWN `claimed_at` (not a fresh timestamp, satisfying `settle()`'s
  ownership guard) and returns the settled `{order_id, shares_purchased,
  order_notional_usd}`; that it refuses to settle at all (returns `None`) when the
  broker's ref-id lookup returns no exact match (a broad "some order exists" signal alone
  is not proof); and that a matched order still in a LIVE, non-terminal state (e.g.
  `"queued"` with `cumulative_quantity="0"`) reserves credit for its FULL submitted
  quantity rather than the (possibly zero or partial) `cumulative_quantity`, since the
  order could still fill for real later.
- `test_dca_config.py` — `test_load_tickers_rejects_a_non_list_whole_share` locks in
  that a present-but-malformed `"whole_share"` field raises `ValueError` rather than
  silently leaving `whole_share_tickers` at its empty-set default; and that
  `load_strategy_config`'s own no-explicit-`base` default falls back to the real
  `WHOLE_SHARE_TICKERS` constant both when the config directory is entirely absent and
  when an existing `tickers.json` simply omits the optional `"whole_share"` key —
  mirroring `tickers`/`excluded_tickers`'s own real hardcoded fallback — while a caller
  passing an explicit `base` is left untouched; that this SAME implicit default honors an
  explicit `"excluded": ["BYDDY"]` (never mentioning `"whole_share"` at all) by omitting
  BYDDY from the fallback `whole_share_tickers` rather than injecting it and triggering a
  spurious disjointness failure; that this SAME implicit default ALSO honors an explicit
  `excluded_tickers=` keyword OVERRIDE (the highest-precedence layer, applied after this
  default is computed) the same way, not just a JSON-file-level `"excluded"` entry; and
  that `load_tickers` rejects a noncanonical (e.g. lowercase) `"whole_share"` symbol
  outright, mirroring the same `^[A-Z.]{1,6}$` pattern
  `schemas/tickers.schema.json`/the ledger's own path-safety check already enforce.
- `test_dca_market_graph.py` — that `active_tickers_from_config()` folds `tickers.json`'s
  `"whole_share"` array into its returned ticker list (deduped against `active`, still
  honoring `excluded`), and rejects a non-list `"whole_share"` field the same way it
  already rejects a non-list `"active"`/`"excluded"` field.
- `ml-forecaster/tests/test_train.py` — that `_load_active_tickers()` folds
  `"whole_share"` into the returned training universe (deduped against `active`, still
  honoring `excluded`), defaults it to empty when absent, and raises `ValueError` (not an
  uncaught exception) on a malformed member — mirroring the existing `"excluded"`
  coverage.
- `scripts/tests/test_backup_strategy.py` — that `write_configs` can CLEAR a stale
  `tickers.json` `whole_share`/`excluded` array via `_merge_into_file`'s
  `preserve_empty_list_keys` when a transcript EXPLICITLY mentions the category as empty
  (e.g. `"Whole-share: (none)"`), but leaves an existing value completely untouched when
  the transcript never mentions the category at all — the `excluded_mentioned`/
  `whole_share_mentioned` distinction `extract_tickers` tracks separately from the
  extracted lists themselves — and that ordinary descriptive prose mentioning
  "whole-share"/"excluded" without the literal marker colon (e.g. "BYDDY is a
  whole-share ticker") never counts as an explicit mention either; and
  `test_extract_tickers_requires_a_colon_for_whole_share_and_excluded_extraction` locks
  in that the SYMBOL-EXTRACTION regexes themselves (not just the `*_mentioned` checks)
  require the literal colon too — a transcript with a REAL `"Whole-share:"` section
  elsewhere plus unrelated prose like "the whole-share XYZ is special" must extract only
  the real section's symbols, never `"XYZ"` from the prose.
- `test_run_dca.py` — `run_slot()`-level assertions that the evaluated-ticker count
  includes `config.whole_share_tickers` on the afternoon (1530ET) slot only, and excludes
  it on the morning (1030ET) and midday (1230ET) slots.
