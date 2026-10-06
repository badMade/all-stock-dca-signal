#!/usr/bin/env python3
"""Gate 5 entry point for the 1530ET (3:30 PM final buy) slot ONLY.

STATUS: covers exactly one of the seven roster slots. See "SCOPE" below
before wiring this into any other slot's prompt.

WHAT THIS IS FOR
----------------
Every one of the seven canonical live-execution prompts
(``claude/dca-executor-prompts/live-mirror/*.txt``) has a Gate 5 step reading
"Run the repo's own signal generation for this slot ... Use the repo entry
point, never a reimplementation" -- and until now there was no such entry
point to run (the now-removed GATE5 proposal doc's section 1.1; see
``PRODUCTION_READINESS.md`` gap #1). Every fire has hit ``GATE5_DISABLED``
since 2026-07-30 and placed nothing, including on days with a broadly down
tape (formerly documented in the now-removed GATE5 incident-fix log; see
``PRODUCTION_READINESS.md``).

SCOPE -- read this before extending to another slot
-----------------------------------------------------
This module calls :func:`near_close_dca.evaluate_source_signal` directly.
That function is the REAL, native signal engine for exactly one live stream:
the 3:30 PM final buy (``run_dca.py`` / ``near_close_dca.py``). It is NOT
what drives market-open, morning, midday, or intraday today -- those run
through ``morning_dca.MorningDCAExecutor.evaluate_signal()``, a separately
implemented method with its own dataclass (``MorningSignal``, not
``SourceSignal``), its own manual-review-candidate handling, and (for the
10:30 live mode) its own same-day preflight-gate ticker narrowing.

Calling ``evaluate_source_signal`` for those other six slots would not reuse
their real logic -- it would silently be a FOURTH, non-matching
reimplementation, which is exactly what Gate 5's own instruction says never
to do. A correct entry point for 0935ET/1030ET/1130ET/1230ET/1330ET/1430ET
has to construct and call the actual ``MarketOpenDCAExecutor`` /
``MorningDCAExecutor`` / ``MiddayDCAExecutor`` / ``IntradayDCAExecutor``
instances' own ``.evaluate()`` method instead -- a materially different,
not-yet-built integration (those classes are stateful, take their own
per-stream config objects, and for 10:30 specifically need the same-day
preflight artifact). Extending this module's ``SLOT_SPECS`` to those slots
without that dispatch would ship a plausible-looking but factually wrong
signal generator for six-sevenths of the roster.

This module therefore stays scoped to 1530ET, where ``evaluate_source_signal``
genuinely is the repo entry point, not a reimplementation of one.

WHAT THIS IS NOT
----------------
It does not re-implement the signal or ladder math. Every candidate is scored
by calling :func:`near_close_dca.evaluate_source_signal` and
:func:`near_close_dca.reconcile_signals` directly -- the same functions the
five-cron path (``run_dca.py`` / ``morning_dca.py``) already calls -- so the
dynamic risk kill-switch (baked into ``graded_buy_multiplier`` itself),
per-ticker calibrated ladders, the real per-source bar intervals
(``StrategyConfig.finance_interval_minutes`` / ``robinhood_interval_minutes``),
and the dual-source (Finance + Robinhood) agreement check all apply exactly
as they do for the currently-live final-buy stream, with no separate copy of
that logic to drift.

One kill-switch nuance does NOT carry over: the five-cron path's own placement
loop (``near_close_dca._process_buy_candidates_batch``) re-checks
``kill_switch_engaged`` with a RUNNING deployed-7d total that grows as each
order in the SAME batch is placed, so a batch that crosses the threshold
mid-run caps later orders even when the starting total was still clear. This
module has no placement loop -- it scores the full universe in one pass
against a single starting ``--deployed-last-7d-usd`` snapshot, so every
candidate's multiplier reflects only that starting figure. Neither
``dca_runner.plan_orders``/``assess`` (the shared Gate 6 this module's
``planner_candidates`` feeds) implement that running re-check either, so
closing this gap is a Gate 6/Gate 7 design decision, not something this
signal-generation entry point can correctly guess at without also owning an
allocation order Gate 6 has not applied yet (Codex review: "Apply the kill
switch as the batch crosses the threshold").

A second nuance does not carry over cleanly either:
``dca_runner.plan_orders()``'s own multiplier/drop/base consistency check
(``dca_runner._candidate_multiplier_notional_mismatch``) validates against
ONLY the global ``config/sizing_ladder.json``, with no awareness of
``config/per_ticker_sizing.json`` calibration overrides that
``graded_buy_multiplier``/``select_first_allocation_base_usd`` DO respect --
and most of the active universe is calibrated. A calibrated ticker's real,
correct multiplier/drop pairing can legitimately fall outside the global
ladder's range for that multiplier, so publishing it unfiltered into
``planner_candidates`` would get ``plan_orders()`` to wrongly refuse a real
BUY as ``REFUSED_INCONSISTENT_MULTIPLIER`` (Codex review: "Keep planner rows
compatible with calibrated ladders"). This module filters such rows out of
``planner_candidates`` and lists the affected tickers in
``planner_incompatible_tickers`` instead -- their full, correct decision is
still in ``candidates`` -- rather than publishing a row ``plan_orders()``
would refuse. That means those legitimate buys do NOT auto-place via the
standard ``planner_candidates`` -> ``plan_orders()`` path; a caller must
separately notice a non-empty ``planner_incompatible_tickers`` and route
those tickers through manual handling. Making ``plan_orders()`` itself
calibration-aware would close that gap for real, but it is shared Gate 6
code relied on by all seven scheduled prompts (Codex review: "Preserve
calibrated BUYs in planner rows"), so it needs its own dedicated change and
review rather than a side effect of adding this entry point.

It does not decide the per-ticker-per-day rule, budget tiers, the same-day
top-up guard, or placement. Those stay separately owned, exactly as for the
five-cron path today: Gate 6 (this repo's shared budget-tier logic, applied
by the caller from this output), Gate 6.5 (``scripts/same_day_top_up_gate.py``,
already wired into every prompt), and Gate 7 (placement, a live broker call
this module never makes). This module's output is a candidate list, not an
order list.

It makes no network calls. Like ``scripts/same_day_top_up_gate.py``, market
data arrives as an already-fetched JSON file: the caller (an agent session
holding real Finance/Robinhood market-data access) fetches quotes and bars via
its own tools and hands them to this script. A subprocess cannot receive a
live Python callable across its boundary, only data -- the same reason
``scripts/same_day_top_up_gate.py`` takes ``--orders-pages`` rather than a
broker object, and the one architecture pattern in this repo already proven
to run inside the live prompts every trading day.

INPUT SHAPE (--market-data)
----------------------------
A JSON object::

    {
      "sources": {
        "finance":   {"<TICKER>": {"previous_close_bar": <bar>, "intraday_bars": [<bar>, ...], "quote": <quote>}, ...},
        "robinhood": {"<TICKER>": {"previous_close_bar": <bar>, "intraday_bars": [<bar>, ...], "quote": <quote>}, ...}
      },
      "source_errors": ["finance"],
      "ticker_errors": {"finance": ["AAPL"], "robinhood": []}
    }

The top-level ``sources`` key is REQUIRED (both provider snapshots are
required evidence for this entry point) -- a genuine "no data at all from
this source" is spelled ``{}`` explicitly, never by omitting the key, and a
``--market-data`` file missing ``sources`` entirely raises rather than
silently producing a schema-valid, all-``NO_DATA`` quiet-tape document
(Codex review: "Require the top-level sources payload").

A ``<bar>`` is ``{"timestamp": "<ISO 8601, tz-aware>", "open", "high", "low",
"close", "volume"}``. ``open``/``high``/``low``/``close`` (and ``quote.price``
below) must be finite AND strictly positive -- both JSON and Python's own
``float()`` accept ``NaN``/``Infinity`` without complaint (Codex review:
"Reject non-finite market prices"), and a non-finite price makes every
comparison in ``evaluate_source_signal`` evaluate False, silently
downgrading a malformed snapshot to ``NO_ACTION`` instead of failing closed;
worse, a 0/negative placeholder price (e.g. from a failed fetch) IS finite
but grades as a 100%+ drop against a normal previous close, reconciling to a
max-size 5x BUY instead (Codex review: "Reject non-positive market prices").
``volume`` only needs to be finite -- zero is a legitimate no-trades bar. One
exception to the strictly-positive rule: ``previous_close_bar.low`` may be
zero or negative to mean "unknown," matching the live Robinhood adapter's own
quote-derived previous-close bar, which has no real prior-session low and
stamps ``low=0.0`` deliberately (``select_first_allocation_base_usd`` treats
any non-positive low as missing and picks the smaller $1 base rather than
upgrading to $2) -- rejecting that live-equivalent shape would make a
live-parity payload abort, or push a caller to fabricate a positive low that
could wrongly upgrade the base (Codex review: "Preserve unknown prior-low
sentinels"). ``previous_close_bar`` is the prior completed
regular-session daily bar (its ``close`` is the previous close, its ``low`` is
the previous regular-session low, when known -- both feed
``select_first_allocation_base_usd``), and its ``timestamp`` must fall
strictly before the trading day being evaluated -- a same-day (or future)
close is not "the prior completed regular session" and would otherwise
silently corrupt BUY grading and base selection rather than fail the
malformed evidence (Codex review: "Reject stale previous-close bars").
``intraday_bars`` must cover from
09:30 ET through at least the slot's decision time, densely enough to resolve
the completed decision bar and the 60-minutes-back lookback bar at that
source's real interval -- 15 minutes for Finance, 5 minutes for Robinhood
(``StrategyConfig.finance_interval_minutes`` / ``robinhood_interval_minutes``,
read from config exactly as the live final-buy run reads them; there is no
CLI override, so this script can never silently drift from the live
interval by being invoked with a different value). For a ticker actually
present in a source's map, ALL THREE of ``previous_close_bar``, a lookback
bar (60 minutes before the decision anchor), and a decision bar within one
source interval of the decision anchor must be resolvable -- a
truncated/partial/malformed fetch missing any of the three (e.g. a lone
10:00 ET bar surviving for a 15:30 decision, or bars that start too late to
cover the lookback point) scores ``NO_DATA``/``provider_error=True`` for
that source instead of silently being treated as an ordinary "not covered"
ticker, which (since 1530ET bypasses the trend filter and
``evaluate_source_signal`` requires all three just to score at all) could
otherwise either still reconcile to a BUY or mask a genuinely broken fetch
behind the OTHER source's clean BUY (Codex review: "Reject stale intraday
bars"; "Treat incomplete attempted source payloads as errors").

``quote`` is OPTIONAL: ``{"price": <float>, "as_of": "<ISO 8601, tz-aware>"}``,
a live signal-time quote captured by the caller alongside the bars -- the same
role the live ``RobinhoodDataProvider.get_quote`` plays for the five-cron
path. When present it becomes ``target_price`` (what Gate 6.5's price
comparisons and any downstream slippage evidence read); when absent,
``evaluate_source_signal`` falls back to the completed decision-bar close,
the same fallback the live provider itself uses when it has no quote. A
caller that fetched a live quote but dropped it here would silently under-
serve the parity with the live path this whole module exists to preserve
(Codex review: "Preserve signal-time quotes in static market data").
``as_of`` is NOT required for a quote to parse -- a ``quote`` with a
``price`` but no ``as_of`` is accepted, not rejected, unlike the hard
failures elsewhere in this module. It IS required for the quote to be
HONORED: a quote's price is only used as ``target_price`` when its
``as_of`` is present AND falls within one of that source's own
interval-minutes of the slot's decision moment -- a missing, old, or
future ``as_of`` cannot be verified to reflect the decision moment at
all, so the quote is silently treated exactly like an absent one (falls
back to the completed decision-bar close) rather than trusted as-is.
Without this, a stale saved quote could become ``target_price`` -- and so
``same_day_top_up_candidates[].current_price`` -- letting Gate 6.5 compare a
same-day top-up against a stale price (Codex review: "Reject stale quote
timestamps").

A ticker missing from a source's per-ticker map (but the source itself
PRESENT as an object in ``sources``, even an empty ``{}``, and not listed in
``source_errors``) is scored ``NO_DATA`` for that source with
``provider_error=False`` -- this is the documented, expected shape for a
ticker one source simply doesn't resolve (e.g. ``GFUZ`` on Finance,
``config/tickers.json`` ``data_source_notes``), and still permits the
single-source auto-buy path exactly as it does live.

``source_errors`` is different: it means the FETCH for that whole source
failed (an exception, a timeout, an empty/malformed response), not that
individual tickers were unresolved. Listing a source there scores every
ticker ``NO_DATA`` with ``provider_error=True`` for it, regardless of
whatever might otherwise be present in ``sources`` for it -- matching
``near_close_dca._safe_signal``'s real distinction between a permanent
per-ticker "not found" (``provider_error=False``, single-source BUY still
allowed) and a genuine provider exception (``provider_error=True``, which
``reconcile_signals`` treats as unsafe to auto-buy past even when the other
source alone says BUY). Omitting ``source_errors`` when a fetch actually
failed would let the still-working source's BUY sail through with no
independent cross-check -- exactly the gap this field exists to close.

``ticker_errors`` is the per-symbol counterpart to ``source_errors``, for a
source that is otherwise working but had a fetch EXCEPTION for one specific
ticker (as opposed to that source simply not covering the ticker at all, the
benign case two paragraphs up). ``{"finance": ["AAPL"], ...}`` scores ``AAPL``
``NO_DATA``/``provider_error=True`` for Finance regardless of whether
``sources.finance`` also happens to have an ``AAPL`` entry -- an explicit
error marker always wins. Without this field a per-ticker fetch exception is
indistinguishable from "this source doesn't cover this ticker" and always
takes the benign, single-source-BUY-permitted path, silently bypassing
dual-source validation for a single-symbol outage (Codex review: "Preserve
per-ticker provider errors"). Optional; both keys default to an empty list.
Both ``source_errors`` and ``ticker_errors`` are shape- and content-validated
inside :func:`generate_candidates` itself, not only by this CLI -- that
function is independently public and importable (``__all__``), so a caller
using it directly gets the same fail-closed guarantee as one going through
``main()`` (Codex review: "Validate error markers in the public helper").

Both ``finance`` and ``robinhood`` keys are REQUIRED under ``sources``, even
when a source genuinely has no data for any ticker (send ``{}``, not a
missing key). A key entirely absent from ``sources`` is treated exactly like
listing it in ``source_errors`` -- every ticker scores ``provider_error=True``
for it -- rather than being silently read as "this source resolved zero
tickers" (which would permit the single-source auto-buy path above with no
independent cross-check). This protects a caller whose fetch failed and who
never built the object at all, not just one who remembered to also populate
``source_errors`` (Codex review: "Fail closed when a source is absent").

``yahoo_finance_yfinance`` is an OPTIONAL third key under ``sources``, in the
identical per-ticker shape as ``finance``/``robinhood`` -- the repo-wide
market-data source policy's fallback (``config/scheduled_tasks.json``'s
``market_data_source_policy``: "yfinance is a fallback used ONLY if Finance
connector data is unavailable, stale, incomplete, or unresolved"). Unlike
``finance``/``robinhood`` its absence is never itself an error; it is simply
unavailable to substitute for a failed Finance fetch. When Finance is judged
unusable for the run -- listed in ``source_errors``, entirely absent from
``sources``, carrying a ``ticker_errors`` entry for any attempted ticker, or
incomplete (missing a fresh decision/lookback bar or ``previous_close_bar``)
for any attempted ticker -- and a usable ``yahoo_finance_yfinance`` payload is
present, this module scores the ENTIRE universe's finance-side signal from
the fallback instead, exactly the whole-universe switch the same policy
already requires of the other six roster slots
(``scripts/stream_candidates.py``'s ``use_fallback``). Real Finance evidence
for an individual ticker is never mixed with fallback evidence for another
ticker in the same run, and a healthy, complete Finance payload is never
overridden by a disagreeing fallback payload. When Finance needs the
fallback but none was supplied (or it was itself listed in
``source_errors``/incomplete), this module does **not** abort -- it falls
back to exactly today's behavior, scoring Finance ``NO_DATA``/
``provider_error=True`` and letting ``reconcile_signals`` reconcile off
Robinhood alone, as it already does for a Finance outage with no fallback
evidence.

USAGE (from an executor prompt)
--------------------------------
    python3 -m scripts.generate_buy_candidates \\
        --slot 1530ET \\
        --trading-day 2026-08-05 \\
        --market-data $ROOT/market_data_1530ET.json \\
        --deployed-last-7d-usd 850.00

``$ROOT`` is the same resolved evidence directory the rest of this slot's own
gates already write under (e.g. Gate 6.5's ``dca_paths.stream_root(...)``-
derived path in ``claude/dca-executor-prompts/live-mirror/``) -- never a bare
repo-relative ``artifacts/`` directory, which would split this fetched
market-data evidence from the shared cron-tracking tree every other consumer
reads (AGENTS.md: artifact roots come from ``dca_paths.stream_root(...)``/
``cron_tracking_root()``, honouring ``$DCA_CRON_TRACKING_ROOT``).

``--deployed-last-7d-usd`` is REQUIRED: the trailing-7-day AFTERNOON-scheduled
spend, from real broker history -- NOT a sum across all seven slots.
``dca_risk.deployed_by_day``'s ``require_afternoon_evidence`` /
``is_afternoon_scheduled_evidence`` gate is what actually funds the live
final-buy kill-switch, deliberately excluding morning and midday scheduled
runs (``dca_risk.build_risk_report``: "Only the scheduled-live afternoon
stream funds the kill-switch"; this slot and 1430ET are the only two afternoon
slots that count). Passing the all-slot total here would let unrelated
morning/midday deployment cap this slot's multipliers when the real final-buy
runner would not have (Codex review: "Scope trailing spend to afternoon risk
evidence"). It feeds ``near_close_dca.kill_switch_engaged`` exactly as the
live final-buy run feeds it today. There is deliberately no default -- a
missing value here is exactly the "buying power cannot be confirmed" class of
gap every executor prompt's own Gate 6 already requires an abort for;
silently assuming zero deployment would let a real kill-switch-tripped day
emit uncapped sizing. It must also be finite and non-negative -- a NaN or
negative figure compares False against every kill-switch threshold check,
which would silently disable the guard instead of tripping it.

Exit status is always 0; a malformed ``--market-data`` file, a non-finite or
negative ``--deployed-last-7d-usd``, or bad arguments raise before that,
which the caller should treat as an abort. The candidate document is written
to stdout.

Standard library only, save for the in-repo imports below.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

# dca_runner is deliberately NOT imported at module level -- it reads
# CONFIG_DIR = get_config_dir() and eagerly loads sizing_ladder.json at ITS
# OWN import time, unconditionally, before this module's argparse has a
# chance to see --config-dir. A scheduler environment with a stale/missing
# $DCA_CONFIG_DIR then crashes on the plain `import scripts.generate_buy_
# candidates` itself -- verified this breaks even `--help`, which needs no
# config at all -- and no valid --config-dir can recover it, since the
# crash happens before main() ever runs (Codex review: "Defer dca_runner
# import until after config selection"). Each function below that actually
# needs dca_runner imports it locally instead; Python caches the module
# after the first successful import, so this costs nothing beyond the
# first call.
from dca_config import (
    get_config_dir,
    load_budgets,
    load_per_ticker_sizing,
    load_sizing_tiers,
    load_strategy_config,
    load_tickers,
)
from near_close_dca import (
    Action,
    Bar,
    ReconciledDecision,
    SourceName,
    SourceSignal,
    StrategyConfig,
    allocation_sort_key,
    completed_bar_anchor,
    evaluate_source_signal,
    kill_switch_engaged,
    latest_bar_at_or_before,
    reconcile_signals,
)

__all__ = ["SLOT_SPECS", "generate_candidates", "main"]

ET = ZoneInfo("America/New_York")

# Only 1530ET is implemented -- see the module docstring's "SCOPE" section for
# why the other six slots are deliberately absent rather than wired up to the
# wrong (non-matching) signal function. ``trend_required=False`` here matches
# near_close_dca.StrategyConfig.ignore_trend_filter as set for the afternoon
# stream and config/scheduled_tasks.json's final_buy_behavior.ignore_trend_filter
# policy: the final scheduled buy of the day deliberately ignores the trend
# filter so it still considers a ticker that dipped earlier but has since
# ticked up slightly.
SLOT_SPECS: Dict[str, Dict[str, Any]] = {
    "1530ET": {"decision_time_et": time(15, 30), "trend_required": False},
}

# dca_runner.SLOTS keys have no "ET" suffix ("1530", not "1530ET") and
# dca_runner.assess()/execute_plan() intentionally fail loud on an
# unrecognized slot_id. An executor feeding this document's "slot" value
# straight into that shared Gate 6/placement path would abort on the
# mismatch even though Gate 5 itself succeeded here; publishing the
# runner-compatible id directly avoids the caller needing its own
# translation table (Codex review: "Publish the runner slot id"). A plain
# dict lookup (not .get with a default) so adding a SLOT_SPECS entry without
# a matching entry here fails at that call site instead of silently
# emitting a wrong or missing runner_slot_id.
_RUNNER_SLOT_IDS: Dict[str, str] = {"1530ET": "1530"}

# A tuple, not a set/frozenset: membership checks against it must use `==`
# scanning (safe for any JSON value) rather than hashing, since a malformed
# `source_errors` entry could be unhashable (a nested list/object).
_VALID_SOURCE_NAMES = tuple(member.value for member in SourceName)


def _validate_source_errors(source_errors: Sequence[str]) -> None:
    """Raise if ``source_errors`` is not a list of exactly ``"finance"``/
    ``"robinhood"`` entries.

    A misspelled/mis-cased entry (e.g. ``"Finance"``) is silently NOT
    ``"finance"`` for the exact-string membership check
    ``generate_candidates``'s ``finance_unavailable``/``robinhood_unavailable``
    use -- if ``sources.finance`` is also present as ``{}`` (a legitimate "no
    data" marker), ``finance_unavailable`` stays False, finance scores a
    plain ``NO_DATA`` instead of ``provider_error=True``, and reconciliation
    can allow a Robinhood-only auto-buy instead of blocking on the provider
    outage this entry was meant to record (Codex review: "Validate
    source_errors entries"). Called from ``generate_candidates`` itself, not
    only ``main()``, since that function is an independently public,
    directly-callable API that would otherwise bypass this check entirely
    (Codex review: "Validate error markers in the public helper"). Accepts
    ``list`` (what JSON deserializes to) or ``tuple`` (``generate_candidates``'s
    own default and idiomatic for a Python caller) -- not a bare `str`, which
    is also iterable but never a valid ``source_errors`` value.
    """
    if not isinstance(source_errors, (list, tuple)):
        raise ValueError("--market-data's 'source_errors', if present, must be a list")
    # _VALID_SOURCE_NAMES is a tuple (not a set) so an unhashable malformed
    # entry (a nested list/object) fails this ``in`` check cleanly via `==`
    # instead of raising TypeError from a hash lookup.
    invalid_source_errors = [entry for entry in source_errors if entry not in _VALID_SOURCE_NAMES]
    if invalid_source_errors:
        raise ValueError(
            f"--market-data's 'source_errors' entries must be one of {_VALID_SOURCE_NAMES}, "
            f"got {invalid_source_errors!r}"
        )


def _recognized_tickers(config: StrategyConfig) -> set:
    """Every symbol this module's input validators will accept by name.

    Deliberately WIDER than the set ``generate_candidates`` scores. Its own
    loop iterates ``config.tickers``, and whole-share symbols are not in that
    list -- they are bought by an entirely different route (accumulate a
    contribution per run, fire one whole-share order once the balance reaches
    a share's price), wired into ``near_close_dca._run_once_impl`` rather than
    here. Adding them to the scoring loop would emit an ordinary $1-$10 graded
    fractional candidate for a ticker that cannot be bought fractionally at
    all, which is the exact failure the whole-share category exists to
    prevent, so that is NOT what this changes.

    What it changes is recognition. An operator fetching market data for the
    configured universe includes every symbol in ``config/tickers.json``, and
    a payload carrying one was rejected outright as "not in the configured
    universe" -- a hard failure for correctly-fetched data (Codex review,
    PR #786: "Include whole-share symbols in the 15:30 candidate generator").
    Accepting the key costs nothing: the scoring loop never reads it. The
    condition predates this PR and applies to BYDDY exactly as it does to
    RVII -- moving RVII into ``whole_share`` surfaced it rather than causing
    it.
    """
    return set(config.tickers) | set(config.excluded_tickers) | set(
        getattr(config, "whole_share_tickers", ()) or ()
    )


def _validate_ticker_errors(ticker_errors: Mapping[str, Sequence[str]], config: StrategyConfig) -> None:
    """Raise if ``ticker_errors`` is not a ``{source_name: [ticker, ...]}``
    mapping whose ticker entries are all recognized symbols.

    ``ticker_errors`` is the per-ticker counterpart to ``source_errors``, for
    a source that is otherwise working but had a per-symbol fetch exception
    for one ticker (as opposed to that ticker simply not being covered by
    this source at all -- see ``generate_candidates``'s own comment on
    ``finance_ticker_errors``/``robinhood_ticker_errors`` for why the two
    must not collapse into the same "missing from the map" shape). JSON
    object keys are always strings, so unlike ``source_errors``' list
    entries there is no unhashable-key case to guard against for the
    top-level dict.

    A non-string or unrecognized-ticker entry (e.g. ``"test"`` instead of
    ``"TEST"``, a stray int/null) would silently never match
    ``config.tickers`` in ``generate_candidates``'s
    ``ticker in finance_ticker_errors`` set-membership check -- the
    per-ticker error marker would just be dropped, with no indication
    anything went wrong, exactly the silent single-symbol bypass
    ``ticker_errors`` exists to close (Codex review: "Validate ticker_errors
    values"). Checked against the union of active and excluded tickers (not
    just ``config.tickers``) so marking a currently-excluded symbol -- a real
    ticker, just not scored this run -- is a tolerated no-op rather than a
    spurious hard error; only a genuinely unrecognized entry fails closed
    here. ``not isinstance(...)`` is checked first so an unhashable entry (a
    nested list/object) short-circuits before the set-membership check, the
    same unhashable-safety pattern ``_validate_source_errors`` relies on.
    """
    if not isinstance(ticker_errors, dict):
        raise ValueError("--market-data's 'ticker_errors', if present, must be an object")
    invalid_ticker_error_sources = [key for key in ticker_errors if key not in _VALID_SOURCE_NAMES]
    if invalid_ticker_error_sources:
        raise ValueError(
            f"--market-data's 'ticker_errors' keys must be one of {_VALID_SOURCE_NAMES}, "
            f"got {invalid_ticker_error_sources!r}"
        )
    known_tickers = _recognized_tickers(config)
    for source_name, tickers in ticker_errors.items():
        # (list, tuple), same reasoning as _validate_source_errors: JSON
        # deserializes to list, but generate_candidates' own Sequence[str]
        # type hint also makes a tuple a valid, idiomatic Python-caller value.
        if not isinstance(tickers, (list, tuple)):
            raise ValueError(f"--market-data's 'ticker_errors[{source_name!r}]' must be a list of ticker strings")
        invalid_tickers = [t for t in tickers if not isinstance(t, str) or t not in known_tickers]
        if invalid_tickers:
            raise ValueError(
                f"--market-data's 'ticker_errors[{source_name!r}]' entries must be tickers in the "
                f"configured universe, got {invalid_tickers!r}"
            )


def _validate_already_covered_tickers(already_covered_tickers: Sequence[str], config: StrategyConfig) -> None:
    """Raise if ``already_covered_tickers`` is not a list of recognized
    ticker strings.

    Gate 4's compatible-stand-down case (live-mirror prompt): an
    unrecognized entry here is far more likely a typo than a real,
    unconfigured symbol, and a typo would silently never match the
    ``ticker in already_covered_tickers`` checks below, letting a
    same-window duplicate order through uncaught -- the same failure mode
    ``_validate_ticker_errors`` closes for its own list-of-tickers inputs.
    Checked against the union of active and excluded tickers, the same
    tolerance every other ticker-list validator here uses.
    """
    if not isinstance(already_covered_tickers, (list, tuple)):
        raise ValueError("already_covered_tickers must be a list of ticker strings")
    known_tickers = _recognized_tickers(config)
    invalid = [t for t in already_covered_tickers if not isinstance(t, str) or t not in known_tickers]
    if invalid:
        raise ValueError(
            f"already_covered_tickers entries must be tickers in the configured universe, got {invalid!r}"
        )


def _validate_market_data_ticker_keys(
    market_data_by_source: Mapping[str, Mapping[str, Any]],
    config: StrategyConfig,
    source_errors: Sequence[str] = (),
) -> None:
    """Raise if a per-source payload (``sources.finance``/
    ``sources.robinhood``) isn't an object, or has a key that isn't a
    recognized symbol.

    A source already marked failed via ``source_errors`` is skipped here
    entirely: ``finance_unavailable``/``robinhood_unavailable`` force
    ``_unavailable_signal`` for every ticker on that source regardless of
    whatever raw payload it has, so a malformed leftover from the SAME
    failed fetch (``null``, a list, an unrecognized ticker key) must not
    abort Gate 5 before that marker even gets a chance to route around it
    -- the surviving provider's own evidence would otherwise never be
    scored either (Codex review: "Skip errored source payload
    validation").

    ``generate_candidates`` matches these keys against ``config.tickers``
    with plain ``in`` membership (``ticker in finance_inputs``, ...). A
    differently-cased or whitespace-padded key (``"test"`` for active
    ``TEST``) never matches that check, so the ticker reads as "not
    attempted on this source" -- the same benign shape as a source that
    genuinely doesn't cover it -- instead of the malformed evidence it
    actually is. With only one source affected, the other source's BUY can
    auto-buy with no independent cross-check; with both affected, the run
    reports a quiet NO_DATA instead of the data problem that actually
    occurred (Codex review: "Normalize market-data ticker keys before
    matching"). Checked against the union of active and excluded tickers,
    the same tolerance ``_validate_ticker_errors`` uses, so data attached to
    a currently-excluded symbol is a no-op, not a spurious hard error.

    A present-but-non-Mapping source payload (e.g. ``"finance": ["invalid"]``
    or an explicit ``"finance": null``) previously made this function
    silently skip it via an ``isinstance`` check on the value alone, deferring
    to whatever happened downstream -- but nothing downstream validates the
    shape either, so ``generate_candidates``'s ``.items()`` call on it raised
    an unhandled ``AttributeError`` instead of a descriptive ``ValueError``
    (Antigravity review: "Non-Mapping Provider Payload Validation"). Only a
    genuinely ABSENT key is tolerated here, matching
    ``finance_unavailable``/``robinhood_unavailable``'s own
    ``"finance" not in market_data_by_source`` check -- those check KEY
    presence, not value-is-``None``, so an explicit JSON ``null`` (key
    present, value ``None``) is a malformed shape same as any other
    non-Mapping value, not a tolerated absence.

    The per-source loop below only ever reads ``market_data_by_source[name]``
    for ``name in _VALID_SOURCE_NAMES`` -- it never enumerates
    ``market_data_by_source``'s OWN keys, so an extra or mis-cased top-level
    key (a typo'd ``"Finance"`` alongside a legitimate, empty ``"finance"``)
    was never looked at, let alone rejected. That stray object can carry real
    evidence -- a no-buy signal under the typo'd key -- that this function
    silently drops instead of scoring, while the surviving canonical
    ``"finance"``/``"robinhood"`` keys read as benignly uncovered and let the
    other source's BUY through with no cross-check against the dropped,
    contradictory evidence (Codex review: "Reject unknown market-data source
    keys"). Checked before the per-source loop so a stray key is caught
    regardless of whether either recognized source is itself present.
    """
    unknown_sources = [key for key in market_data_by_source if key not in _VALID_SOURCE_NAMES]
    if unknown_sources:
        raise ValueError(
            f"--market-data's 'sources' keys must be one of {_VALID_SOURCE_NAMES}, got unknown "
            f"key(s) {unknown_sources!r}"
        )
    known_tickers = _recognized_tickers(config)
    for source_name in _VALID_SOURCE_NAMES:
        if source_name in source_errors:
            continue
        if source_name not in market_data_by_source:
            continue
        per_ticker = market_data_by_source[source_name]
        if not isinstance(per_ticker, Mapping):
            raise ValueError(
                f"--market-data's 'sources[{source_name!r}]' must be an object mapping ticker to "
                f"payload, got {type(per_ticker).__name__}"
            )
        invalid_keys = [key for key in per_ticker if key not in known_tickers]
        if invalid_keys:
            raise ValueError(
                f"--market-data's 'sources[{source_name!r}]' keys must be tickers in the "
                f"configured universe, got {invalid_keys!r}"
            )


def _finite_float(raw: Any, *, field: str) -> float:
    """Parse a market-data numeric field, rejecting NaN/Infinity/booleans.

    Both Python's ``json`` module and ``float()`` itself accept the literal
    tokens/strings ``NaN``/``Infinity``/``-Infinity`` without complaint. A
    non-finite bar/quote price would make every comparison in
    ``evaluate_source_signal`` (``below_previous_close``, ``intraday_trend_up``,
    ...) evaluate False, silently downgrading a malformed snapshot to
    ``NO_ACTION``/``buy_count=0`` instead of failing closed -- and a NaN that
    survived into the output document is not valid JSON for a downstream
    parser either (Codex review: "Reject non-finite market prices"). ``bool``
    is an ``int`` subclass, so ``float(True) == 1.0`` -- a corrupted decision
    close of JSON ``true`` would otherwise silently become a finite, positive
    $1.00 and grade as a ~99% drop against a normal previous close instead of
    failing closed on malformed provider evidence (Codex review: "Reject
    boolean market prices").

    ``float()`` itself raises ``TypeError``, not ``ValueError``, for a
    non-numeric-shaped JSON value (``null``, a nested list/object) -- every
    other rejection in this module is a ``ValueError`` specifically, so a
    caller that catches that one type to handle malformed market-data
    input gracefully would miss this shape of corruption and crash
    uncaught instead (Antigravity review: "Convert TypeError to ValueError
    in _finite_float"). Caught and re-raised as ValueError to match.
    """
    if isinstance(raw, bool):
        raise ValueError(f"boolean market-data value for {field}: {raw!r}")
    try:
        value = float(raw)
    except TypeError as exc:
        raise ValueError(f"invalid market-data value for {field}: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"non-finite market-data value for {field}: {raw!r}")
    return value


def _finite_positive_float(raw: Any, *, field: str) -> float:
    """Parse a market-data PRICE field, rejecting NaN/Infinity/zero/negative.

    ``_finite_float`` alone is not enough for a price: a 0/-1 placeholder from
    a failed fetch is finite, but a non-positive decision close compared
    against a normal positive previous_close grades as a 100%+ drop in
    ``evaluate_source_signal``'s ``drop_vs_previous_close_pct`` math --
    reconciling to a max-size (5x) BUY at a non-positive ``target_price``
    instead of failing closed on malformed data (Codex review: "Reject
    non-positive market prices"). Volume legitimately can be zero (no trades
    in a bar), so it stays on the finite-only ``_finite_float`` check.
    """
    value = _finite_float(raw, field=field)
    if value <= 0:
        raise ValueError(f"non-positive market-data price for {field}: {raw!r}")
    return value


def _normalize_iso8601_timestamp(text: str) -> str:
    """Normalize a market-data timestamp so ``datetime.fromisoformat`` accepts
    it on Python 3.10, not just the broadened 3.11+ parser.

    Robinhood returns timestamps like ``2026-07-06T18:31:29.201412148Z`` (a
    trailing ``Z``, 9 fractional digits). Trims to microsecond precision and
    replaces a trailing ``Z`` with an explicit ``+00:00`` offset -- the same
    normalization ``dca_live_adapters._parse_rfc3339`` already applies for the
    identical reason -- so parsing this module's own ``timestamp``/``as_of``
    fields does not depend on which interpreter runs it.
    """
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        frac, offset = tail, ""
        for sep in ("+", "-"):
            idx = tail.find(sep)
            if idx != -1:
                frac, offset = tail[:idx], tail[idx:]
                break
        text = f"{head}.{frac[:6]}{offset}"
    return text


def _parse_bar(raw: Mapping[str, Any], *, allow_unknown_low: bool = False) -> Bar:
    """``allow_unknown_low`` relaxes ``low`` from strictly-positive to merely
    finite, for ``previous_close_bar`` only. The live Robinhood adapter's own
    quote-derived previous-close bar (``RobinhoodDataProvider.
    _previous_close_from_quote``) has no real prior-session low, only a
    close, and deliberately stamps ``low=0.0`` as an "unknown" sentinel --
    ``select_first_allocation_base_usd`` treats any ``low <= 0`` as
    missing/unknown and conservatively picks the smaller $1 base rather than
    upgrading to $2. Rejecting that same live-equivalent shape here would
    make a live-parity 1530ET payload abort, or push a caller to fabricate a
    positive low that could wrongly upgrade the base (Codex review:
    "Preserve unknown prior-low sentinels"). Intraday/decision bars have no
    such live sentinel and keep the strict positive check.

    A ``timestamp`` with no UTC offset parses fine via ``fromisoformat`` but
    yields a naive ``datetime`` -- ``_StaticMarketData``'s same-day guard
    then calls ``.astimezone(ET)`` on it, which does not raise for a naive
    value but silently reinterprets it in the HOST's local system timezone
    instead of erroring. On a UTC host a malformed same-day bar can convert
    to the prior ET date, pass the "before trading day" guard, and its
    close/low then drive an automatic BUY from an evidentially ambiguous
    timestamp. Every other naive-vs-aware mismatch in this module surfaces
    as an immediate ``TypeError`` from datetime subtraction (decision/
    lookback bar comparisons); only this ``.astimezone`` call silently
    misinterprets instead of crashing, so it is rejected here instead
    (Codex review: "Reject naive previous-close timestamps").

    A bar object missing one of the required keys entirely (as opposed to
    present-but-wrong-type, which ``_finite_float``/``_finite_positive_float``
    already reject as ``ValueError``) raises ``KeyError`` from the plain
    ``raw[...]`` lookups below -- inconsistent with every other malformed-
    input rejection in this module (Antigravity review: "Unhandled KeyError
    in Bar/Quote Parsing"). Converted to the same ``ValueError`` shape here.
    """
    low_parser = _finite_float if allow_unknown_low else _finite_positive_float
    try:
        timestamp = datetime.fromisoformat(_normalize_iso8601_timestamp(str(raw["timestamp"])))
        if timestamp.tzinfo is None:
            raise ValueError(f"bar timestamp must be timezone-aware, got {raw['timestamp']!r}")
        open_price = _finite_positive_float(raw["open"], field="open")
        high = _finite_positive_float(raw["high"], field="high")
        low = low_parser(raw["low"], field="low")
        close = _finite_positive_float(raw["close"], field="close")
        volume = _finite_float(raw.get("volume", 0.0), field="volume")
        # Each of open/high/low/close being individually positive does not
        # prove the bar is internally consistent -- a decision bar whose
        # close is below its own low (or above its own high, or an open
        # outside [low, high]) is impossible for a real bar but still
        # passes every check above. evaluate_source_signal trusts this
        # close/low pairing directly for below_previous_close and $1/$2
        # base selection, so both sources could reconcile to a max-size BUY
        # from evidence that could never have actually occurred (Codex
        # review: "Reject internally inconsistent OHLC bars"). `low > 0`
        # skips this for previous_close_bar's allow_unknown_low sentinel
        # (low=0.0 meaning "unknown", not a real low of zero) -- see this
        # function's own docstring on that sentinel; high/open/close are
        # still real values even when low is the sentinel, so the high-side
        # check always applies.
        if (low > 0 and (low > open_price or low > close)) or high < open_price or high < close:
            raise ValueError(
                f"bar has internally inconsistent OHLC values (low/high must bound open/close): "
                f"open={open_price!r}, high={high!r}, low={low!r}, close={close!r}"
            )
        return Bar(timestamp=timestamp, open=open_price, high=high, low=low, close=close, volume=volume)
    except KeyError as exc:
        raise ValueError(f"bar is missing required key: {exc}") from exc


def _parse_quote(raw: Optional[Mapping[str, Any]]) -> Optional[Tuple[float, Optional[datetime]]]:
    """Parse an optional per-ticker ``quote`` payload into the ``(price, as_of)``
    shape :func:`near_close_dca.evaluate_source_signal` reads from
    ``get_quote`` -- see the module docstring's "INPUT SHAPE" section.

    ``as_of`` with no UTC offset parses fine via ``fromisoformat`` but yields
    a naive ``datetime``; ``_StaticMarketData.get_quote`` later subtracts it
    from the (aware) decision anchor, which raises an unhandled ``TypeError``
    rather than a clean, descriptive error at the point the malformed value
    actually entered the system -- every other parsed field in this module
    fails with ``ValueError`` specifically (Antigravity review: "Unhandled
    TypeError on offset-naive quote.as_of timestamps"). Rejected here to
    match, rather than left to surface however ``get_quote`` happens to fail.

    A missing ``price`` key raises ``KeyError`` from the plain ``raw[...]``
    lookup below, the same inconsistency ``_parse_bar`` had (Antigravity
    review: "Unhandled KeyError in Bar/Quote Parsing") -- converted to
    ``ValueError`` here to match every other malformed-input rejection in
    this module.

    Only a genuinely ABSENT/``None`` quote is tolerated as "no quote" --
    the same absent-vs-malformed distinction ``_validate_market_data_ticker_
    keys`` draws for a whole source payload. ``if not raw`` previously also
    swallowed a present-but-empty ``{}`` (and any other falsy non-Mapping
    value) as if the field were never provided, silently falling back to
    the decision-bar close instead of rejecting the caller's malformed
    saved-quote evidence, even though a quote missing only ``as_of``
    (``{"as_of": ...}`` with no ``price``) already correctly raised via the
    ``KeyError`` path below (Codex review: "Reject empty quote payloads
    explicitly"). A non-Mapping-but-truthy value (a list, string, bool)
    previously reached ``raw.get(...)`` below and crashed with an unhandled
    ``AttributeError`` instead of this module's usual descriptive
    ``ValueError``.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"quote must be an object mapping, got {type(raw).__name__}")
    try:
        as_of = raw.get("as_of")
        quote_time = datetime.fromisoformat(_normalize_iso8601_timestamp(str(as_of))) if as_of else None
        if quote_time is not None and quote_time.tzinfo is None:
            raise ValueError(f"quote.as_of must be timezone-aware, got {as_of!r}")
        return (
            _finite_positive_float(raw["price"], field="quote.price"),
            quote_time,
        )
    except KeyError as exc:
        raise ValueError(f"quote is missing required key: {exc}") from exc


def _previous_trading_day(trading_day: date) -> date:
    """Return the most recent open market day strictly before ``trading_day``,
    per ``config/market_calendar.json`` (``dca_runner``'s own Gate 0 calendar).

    ``_StaticMarketData`` previously only checked that a ``previous_close_bar``
    was dated strictly before ``trading_day`` -- ANY earlier date passed,
    including a reused/cached bar from days or weeks earlier. That stale
    baseline would still be used as the BUY-grading and $1/$2 base-selection
    previous close/low exactly as if it were fresh, since
    ``evaluate_source_signal`` has no session-calendar awareness of its own
    and trusts whatever it is handed (Codex review: "Reject stale
    previous-close bars"). Using the checked-in calendar (rather than a
    fixed day-count offset, which would misfire across weekends/holidays)
    to compute the ONE correct date and requiring an exact match closes
    that gap.

    Bounded to 10 calendar days back as a defensive limit, not a realistic
    one -- the longest actual U.S. market closure is a handful of days, so
    hitting this bound means a corrupted calendar (e.g. one that marks an
    entire month closed) rather than a genuine long weekend, and raising
    here fails closed instead of looping indefinitely.
    """
    import dca_runner  # deferred -- see the module-level comment by the (absent) top import

    candidate = trading_day
    for _ in range(10):
        candidate = candidate - timedelta(days=1)
        try:
            status = dca_runner.market_day_status(candidate)
        except dca_runner.CalendarCoverageError as exc:
            raise ValueError(f"cannot determine the previous trading day before {trading_day}: {exc}") from exc
        if not status.is_closed:
            return candidate
    raise ValueError(f"no open market day found in the 10 calendar days before {trading_day} per market_calendar.json")


class _StaticMarketData:
    """Serves caller-supplied, already-fetched bars and an optional signal-time
    quote through the ``MarketDataProvider`` protocol
    :func:`evaluate_source_signal` expects.

    No network access of its own -- the caller fetches the raw bars/quote via
    its own Finance/Robinhood tools and hands them here as plain JSON; this
    class only marshals that JSON into the shape the real signal function
    reads, the same role ``scripts.broker_reconciliation``'s row-normalizers
    play for broker order JSON.
    """

    def __init__(
        self,
        source: SourceName,
        per_ticker: Mapping[str, Mapping[str, Any]],
        *,
        quote_staleness_minutes: float,
        trading_day: date,
    ) -> None:
        self.source = source
        self._quote_staleness_minutes = quote_staleness_minutes
        self._previous_close: Dict[str, Optional[Bar]] = {}
        self._intraday: Dict[str, List[Bar]] = {}
        self._quotes: Dict[str, Optional[Tuple[float, Optional[datetime]]]] = {}
        # Computed once per construction, not per ticker -- trading_day is the
        # same for every ticker in this batch, and dca_runner.market_day_status
        # reads config/market_calendar.json fresh on every call.
        previous_trading_day = _previous_trading_day(trading_day)
        for ticker, payload in per_ticker.items():
            # `per_ticker` here is generate_candidates' already-filtered
            # finance_inputs/robinhood_inputs -- a ticker already marked
            # errored via ticker_errors, excluded, or already-covered is
            # filtered out BEFORE this constructor ever sees it, so any
            # ticker reaching this loop is evidence this run actually intends
            # to score. `_validate_market_data_ticker_keys` only validates
            # that per_ticker's KEYS are known tickers -- not that each
            # ticker's VALUE is itself an object -- so a malformed payload
            # (`sources.finance.TEST: null`, or a list from a failed
            # serializer) previously reached the plain `payload.get(...)`
            # call below and crashed with an unhandled AttributeError,
            # aborting the entire run instead of failing on just this
            # ticker's malformed evidence with a clear ValueError (Codex
            # review: "Validate per-ticker payload objects before parsing").
            if not isinstance(payload, Mapping):
                raise ValueError(
                    f"--market-data's 'sources[{source.value!r}][{ticker!r}]' must be an object, "
                    f"got {type(payload).__name__}"
                )
            previous_close_bar = payload.get("previous_close_bar")
            if previous_close_bar:
                bar = _parse_bar(previous_close_bar, allow_unknown_low=True)
                # A previous_close_bar dated anything other than the immediately
                # previous market session is not "the prior completed regular
                # session" this field is documented to be -- evaluate_source_
                # signal has no session-calendar awareness of its own and would
                # use whatever close/low it's handed as the BUY-grading and
                # $1/$2 base-selection baseline. Originally this only rejected
                # a same-day-or-future bar, so a reused/cached bar from days or
                # weeks earlier (a caller bug, or a stale cached file) still
                # passed and was scored as if it were fresh (Codex review:
                # "Reject stale previous-close bars"). Same hard-ValueError
                # severity as a boolean/non-finite price above: a nonsensical
                # bar date signals a broken caller payload, not a routine
                # per-source outage the reconciler should route around.
                # Daily bars timestamped at midnight UTC (00:00:00Z) carry the session
                # date directly; converting midnight UTC to ET would subtract 4/5 hours
                # and falsely shift the bar to the prior calendar day.
                bar_date = (
                    bar.timestamp.date()
                    if bar.timestamp.time() == time(0, 0)
                    else bar.timestamp.astimezone(ET).date()
                )
                if bar_date != previous_trading_day:
                    raise ValueError(
                        f"previous_close_bar for {ticker!r} ({source.value}) is dated {bar_date}, "
                        f"not the immediately previous market session ({previous_trading_day}) "
                        f"before trading day {trading_day}"
                    )
                self._previous_close[ticker] = bar
            else:
                self._previous_close[ticker] = None
            intraday_bars = [_parse_bar(bar) for bar in payload.get("intraday_bars", [])]
            # latest_bar_at_or_before (near_close_dca.py) keeps whichever bar
            # it visits FIRST among equal timestamps: `bar.timestamp >
            # best.timestamp` is strict, so a later duplicate never replaces
            # an earlier one. A malformed merge producing two bars for the
            # same minute with different prices would then have its
            # decision/lookback bar selection depend solely on array order
            # -- silently turning a no-buy 15:15/15:25 bar into an automatic
            # BUY, or vice versa, from evidence this module never actually
            # chose (Codex review: "Reject duplicate intraday bar
            # timestamps").
            bar_timestamps = [bar.timestamp for bar in intraday_bars]
            if len(bar_timestamps) != len(set(bar_timestamps)):
                duplicate_timestamps = sorted({t for t in bar_timestamps if bar_timestamps.count(t) > 1})
                raise ValueError(
                    f"intraday_bars for {ticker!r} ({source.value}) has duplicate timestamp(s) "
                    f"{[t.isoformat() for t in duplicate_timestamps]!r}"
                )
            self._intraday[ticker] = intraday_bars
            # get_quote() below already unconditionally returns None for
            # Finance -- the live FinanceDataProvider has no quote path at
            # all -- but that suppression happens too late to help here: a
            # generic caller that serializes the same "quote" shape for
            # both providers can still have a malformed Finance quote
            # (missing price, non-numeric) reach _parse_quote and abort an
            # otherwise-valid Finance/Robinhood bar evaluation over data
            # that was never going to be used anyway (Codex review: "Skip
            # Finance quotes before parsing"). Skipped at the source
            # instead of merely ignored at read time.
            self._quotes[ticker] = None if source is SourceName.FINANCE else _parse_quote(payload.get("quote"))

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        return self._previous_close.get(ticker)

    def get_intraday_bars(
        self, ticker: str, start: datetime, end: datetime, interval_minutes: int
    ) -> List[Bar]:
        return [bar for bar in self._intraday.get(ticker, []) if start <= bar.timestamp <= end]

    def freshest_bar_at_or_before(self, ticker: str, anchor: datetime) -> Optional[Bar]:
        """The same selection :func:`evaluate_source_signal` itself makes for
        its decision bar (``near_close_dca.latest_bar_at_or_before``), exposed
        so a caller can pre-check staleness before scoring -- see
        ``generate_candidates``'s use of this for why a stale decision bar
        must not silently be scored as fresh."""
        return latest_bar_at_or_before(self._intraday.get(ticker, []), anchor)

    def get_quote(self, ticker: str, as_of: datetime) -> Optional[Tuple[float, Optional[datetime]]]:
        if self.source is SourceName.FINANCE:
            # The live FinanceDataProvider (dca_live_adapters.py) has no
            # get_quote() method at all -- a Finance-sourced target price
            # always falls back to the completed decision bar's close.
            # Surfacing a caller-supplied "quote" field for Finance here
            # would let reconciliation's equal-multiplier tie-break publish
            # a target_price/top-up current_price the live path could never
            # produce, drifting Gate 6.5 price comparisons and slippage
            # evidence from the real final-buy path (Codex review: "Ignore
            # Finance quotes to preserve live target prices").
            return None
        quote = self._quotes.get(ticker)
        if quote is None:
            return None
        _price, quote_time = quote
        # A quote with no as_of, or one further than one source interval from
        # the actual decision moment, cannot be verified to reflect the
        # decision moment at all (old, or -- just as unverifiable -- from the
        # future). evaluate_source_signal treats a None return here exactly
        # like "no quote was ever provided" and falls back to the completed
        # decision bar's own close, which this same class has already
        # validated is itself fresh (see generate_candidates' staleness gate)
        # -- so rejecting an unverifiable quote here fails closed onto a
        # value already known to be good, not onto no value at all (Codex
        # review: "Reject stale quote timestamps").
        if quote_time is None:
            return None
        # abs() alone treats a quote timestamped AFTER as_of as merely "far
        # or near," accepting one close enough in time even though it is
        # signal-time evidence that could not have existed yet -- e.g. a
        # 15:34 Robinhood quote for a 15:30 decision, within the 5-minute
        # interval by absolute distance but fetched after the decision
        # moment. That post-decision price would become target_price and
        # same_day_top_up_candidates[].current_price, drifting Gate 6.5/
        # slippage evidence from the actual decision moment (Codex review:
        # "Reject near-future quote timestamps"). Rejected outright,
        # regardless of how close, before the distance check below even
        # runs.
        if quote_time > as_of:
            return None
        if (as_of - quote_time).total_seconds() > self._quote_staleness_minutes * 60:
            return None
        return quote


def _bar_is_stale(data: "_StaticMarketData", ticker: str, anchor: datetime, interval_minutes: int) -> bool:
    """True when ``ticker``'s freshest intraday bar at-or-before ``anchor``
    is missing, or further from it than one source interval -- see
    ``generate_candidates``'s use of this for why a stale bar must not be
    scored as if it were fresh (Codex review: "Reject stale intraday
    bars"). ``anchor`` is whichever reference point staleness is being
    judged against: the decision anchor for the decision bar, or the
    lookback anchor for the trend-lookback bar (see
    ``_attempted_source_incomplete``) -- the definition of "stale" (no bar
    within one source interval) is the same at either anchor."""
    bar = data.freshest_bar_at_or_before(ticker, anchor)
    if bar is None:
        return True
    return (anchor - bar.timestamp) > timedelta(minutes=interval_minutes)


def _attempted_source_incomplete(
    data: "_StaticMarketData",
    ticker: str,
    decision_anchor: datetime,
    lookback_anchor: datetime,
    interval_minutes: int,
) -> bool:
    """True when ``ticker`` was attempted on this source but is missing a
    piece :func:`evaluate_source_signal` requires to score it at all: a
    fresh decision bar, a fresh lookback bar, or ``previous_close_bar``.
    That function's own "any of the three is None" check defaults to
    ``provider_error=False`` -- the same NO_DATA shape as a genuinely
    uncovered ticker -- so a truncated/malformed attempted payload could
    otherwise silently permit a single-source auto-buy from the other
    source instead of forcing manual review (Codex review: "Treat
    incomplete attempted source payloads as errors").

    The lookback bar is checked for STALENESS via ``_bar_is_stale``, not
    just presence: a technically-non-None bar that is far older than the
    lookback anchor is exactly as untrustworthy as a missing one for the
    trend read it is meant to support, the same gap ``_bar_is_stale``
    already closed for the decision bar alone -- checking only ``is None``
    here let a stale morning bar silently stand in for the lookback bar,
    reconciling to an automatic BUY from incomplete evidence instead of
    forcing provider-error/manual review (Codex review: "Reject stale
    lookback bars before scoring").
    """
    if data.get_previous_close_bar(ticker, decision_anchor) is None:
        return True
    if _bar_is_stale(data, ticker, lookback_anchor, interval_minutes):
        return True
    return _bar_is_stale(data, ticker, decision_anchor, interval_minutes)


def _unavailable_signal(ticker: str, source: SourceName) -> SourceSignal:
    """A whole-source fetch failure, in the same shape near_close_dca._safe_signal
    uses for a genuine provider exception: NO_DATA action, provider_error=True so
    reconcile_signals refuses to let the other source's BUY sail through alone."""
    return SourceSignal(
        ticker=ticker,
        source=source,
        action=Action.NO_DATA,
        reason=f"{source.value} source unavailable (fetch failed)",
        provider_error=True,
    )


def _allocation_sort_drop_pct(decision: ReconciledDecision) -> float:
    """The percent drop ``near_close_dca.allocation_sort_key`` actually uses for
    its equal-multiplier tie-break: the MAX across both sources, not whichever
    single source's price the reconciled decision happened to target. Calling
    that function directly, rather than recomputing the max here, keeps this
    field in lockstep with the live final-buy allocation order even if the
    tie-break logic itself ever changes (Codex review: "Emit the allocation-
    sort drop") -- reporting only the winning (smaller-amount) source's own
    drop under-reports it whenever that source is not also the larger-drop
    source, which would silently reorder a caller's capped-budget allocation
    relative to the real final-buy path.

    This is deliberately NOT the value used in ``planner_candidates`` -- see
    ``_winning_signal`` -- because it can disagree with ``buy_multiplier``
    (a real ladder value derived from the WINNING source's own drop, not this
    max), and ``dca_runner.plan_orders()`` refuses a candidate whose
    ``drop_pct`` falls outside its claimed ``multiplier``'s configured range.
    """
    _, negated_drop, _ = allocation_sort_key(decision)
    return -negated_drop


def _winning_signal(decision: ReconciledDecision) -> Optional[SourceSignal]:
    """The SourceSignal that actually produced this decision's buy_multiplier/
    suggested_amount_usd, identified by which source decision.target_price_source
    names. Only meaningful for a BUY decision -- reconcile_signals sets
    target_price_source via _target_fields(winning_signal) on every path that
    reaches Action.BUY, so this always resolves for one; a non-BUY decision has
    no "winning" source and returns None (target_price_source is empty)."""
    for signal in (decision.finance_signal, decision.robinhood_signal):
        if signal is not None and decision.target_price_source.startswith(signal.source.value):
            return signal
    return None


def _decision_to_dict(ticker: str, decision: ReconciledDecision) -> Dict[str, Any]:
    winning = _winning_signal(decision)
    return {
        "ticker": ticker,
        "action": decision.action.value,
        "reason": decision.reason,
        "buy_multiplier": decision.buy_multiplier,
        "suggested_amount_usd": round(decision.suggested_amount_usd, 2),
        "selected_base_amount_usd": decision.selected_base_amount_usd,
        "target_price": decision.target_price,
        "target_price_source": decision.target_price_source,
        # Gate 6's allocation priority (near_close_dca.allocation_sort_key) tie-breaks
        # equal multipliers by larger percent drop -- without this field a caller has
        # to re-fetch/recompute the signal inputs just to preserve that ordering. NOT
        # necessarily consistent with buy_multiplier -- see multiplier_drop_pct below
        # for the value that is.
        "drop_vs_previous_close_pct": _allocation_sort_drop_pct(decision),
        # The drop that ACTUALLY produced buy_multiplier/suggested_amount_usd (the
        # winning source's own drop) -- guaranteed self-consistent with them, unlike
        # drop_vs_previous_close_pct above. This is what planner_candidates uses:
        # dca_runner.plan_orders() rejects a candidate as inconsistent_multiplier_
        # notional when its drop_pct falls outside its claimed multiplier's own
        # configured range, so pairing the allocation-sort MAX with this decision's
        # multiplier/amount would get a real BUY wrongly refused whenever the
        # winning and max-drop sources differ (Codex review: "Keep planner drop
        # consistent with multiplier").
        "multiplier_drop_pct": winning.drop_vs_previous_close_pct if winning else None,
        "finance_action": decision.finance_signal.action.value if decision.finance_signal else None,
        "robinhood_action": decision.robinhood_signal.action.value if decision.robinhood_signal else None,
        # A provider error (whole-source outage, a per-ticker error marker, or
        # an incomplete attempted payload) does not always escalate the
        # OVERALL action to MANUAL_REVIEW -- reconcile_signals only does that
        # when the surviving source would otherwise BUY. If neither source
        # qualifies as BUY anyway, this reconciles as plain NO_DATA/NO_ACTION
        # with no trace of the error, so buy_count/manual_review_count can
        # both read 0 on a day a source genuinely failed, indistinguishable
        # from a real quiet tape with clean data (Codex review: "Surface
        # provider-error runs before zero-candidate exits").
        "provider_error": (
            (decision.finance_signal is not None and decision.finance_signal.provider_error)
            or (decision.robinhood_signal is not None and decision.robinhood_signal.provider_error)
        ),
    }


def generate_candidates(
    *,
    slot: str,
    now: datetime,
    config: StrategyConfig,
    market_data_by_source: Mapping[str, Mapping[str, Mapping[str, Any]]],
    source_errors: Sequence[str] = (),
    ticker_errors: Optional[Mapping[str, Sequence[str]]] = None,
    already_covered_tickers: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """Evaluate every active, non-excluded, not-already-covered ticker for
    ``slot`` and return reconciled candidates. This is Gate 5 and nothing
    past it -- see the module docstring's "WHAT THIS IS NOT".

    ``already_covered_tickers`` is Gate 4's compatible-stand-down case
    (live-mirror prompt: "generate this slot's candidate list EXCLUDING
    every ticker the compatible executor already placed an order for in
    THIS SAME window") -- treated exactly like ``config.excluded_tickers``:
    skipped by the main loop below and filtered out of finance_inputs/
    robinhood_inputs before _StaticMarketData ever parses their payload, so
    a stale or malformed bar for an already-covered ticker cannot abort
    scoring for the rest of the universe, or surface provider-error/manual-
    review evidence for a ticker no second order is ever allowed for
    anyway (Codex review: "Exclude same-window tickers before scoring").
    """
    # `or {}` would ALSO replace a present-but-wrong-shape value (e.g. the
    # JSON schema violation `"ticker_errors": []`) with {}, since an empty
    # list is falsy -- silently discarding it before _validate_ticker_errors
    # below ever sees it, so a malformed ticker_errors payload reads as "no
    # per-ticker error evidence" instead of failing closed on the schema
    # violation (Codex review: "Validate falsy ticker_errors payloads").
    # Only None (genuinely not provided) gets the default; anything else,
    # however falsy, is passed through so the isinstance check can reject it.
    ticker_errors = {} if ticker_errors is None else ticker_errors
    if slot not in SLOT_SPECS:
        raise ValueError(f"unknown slot {slot!r}; must be one of {sorted(SLOT_SPECS)}")
    spec = SLOT_SPECS[slot]
    config = dataclasses.replace(
        config,
        decision_time_et=spec["decision_time_et"],
        ignore_trend_filter=not spec["trend_required"],
    )
    # Validated HERE, not only in main(), because generate_candidates is
    # itself a public, exported, directly-callable API
    # (__all__/module docstring) -- a caller (this module's own tests
    # included) that invokes it directly rather than through the CLI would
    # otherwise bypass source_errors/ticker_errors validation entirely,
    # reopening the exact silent-single-source-auto-buy gap those checks
    # exist to close, just via a different call path (Codex review:
    # "Validate error markers in the public helper").
    _validate_source_errors(source_errors)
    _validate_ticker_errors(ticker_errors, config)
    _validate_market_data_ticker_keys(market_data_by_source, config, source_errors)
    _validate_deployed_last_7d_usd(config.deployed_last_7d_usd)
    _validate_risk_guard_config(config)
    _validate_already_covered_tickers(already_covered_tickers, config)
    already_covered_tickers_set = set(already_covered_tickers)
    trading_day = now.astimezone(ET).date()
    # A source key ENTIRELY ABSENT from `sources` (not merely present-but-empty) is
    # treated the same as an explicit source_errors entry: a caller whose fetch
    # failed and who simply never built a "finance"/"robinhood" object -- rather
    # than remembering to also list it in source_errors -- must not silently fall
    # through to "this source resolved zero tickers," which permits an unvalidated
    # single-source auto-buy. Fail closed on the ambiguous shape instead of trusting
    # it (Codex review: "Fail closed when a source is absent").
    finance_unavailable = "finance" in source_errors or "finance" not in market_data_by_source
    robinhood_unavailable = "robinhood" in source_errors or "robinhood" not in market_data_by_source
    # The yfinance fallback is genuinely optional -- unlike finance/robinhood,
    # its absence is never itself a failure this module reports, only a
    # signal that the fallback substitution below cannot happen. See
    # `use_yfinance_fallback` for the policy this backs (market_data_source_
    # policy in config/scheduled_tasks.json: "yfinance is a fallback used
    # ONLY if Finance connector data is unavailable, stale, incomplete, or
    # unresolved").
    yfinance_unavailable = (
        SourceName.YFINANCE_FALLBACK.value in source_errors
        or SourceName.YFINANCE_FALLBACK.value not in market_data_by_source
    )
    # A ticker absent from an otherwise-working source's per-ticker map is
    # ambiguous: near_close_dca._safe_signal distinguishes a permanent "this
    # source doesn't cover this ticker" (provider_error=False, single-source
    # BUY still allowed -- e.g. GFUZ on Finance) from a transient per-ticker
    # fetch exception (provider_error=True, forces manual review). Without
    # ticker_errors both collapse to the same "just missing from the map"
    # shape and always take the benign path, silently bypassing dual-source
    # validation for a single-symbol outage (Codex review: "Preserve
    # per-ticker provider errors").
    finance_ticker_errors = set(ticker_errors.get("finance", ()))
    robinhood_ticker_errors = set(ticker_errors.get("robinhood", ()))
    yfinance_ticker_errors = set(ticker_errors.get(SourceName.YFINANCE_FALLBACK.value, ()))
    # A ticker/source already marked errored above, excluded from trading
    # entirely, or already covered by a compatible executor in this same
    # window (Gate 4's stand-down case) is never scored via a real
    # evaluate_source_signal call in the main loop below regardless of
    # whatever raw payload it has here -- an error-marked ticker
    # unconditionally gets _unavailable_signal, and an excluded/already-
    # covered ticker's whole loop iteration is skipped via `if ticker in
    # config.excluded_tickers or ticker in already_covered_tickers_set:
    # continue` before either source is even consulted. That payload must
    # therefore never reach _parse_bar/_parse_quote (via _StaticMarketData's
    # constructor) at all. Parsing it anyway meant a genuinely malformed/
    # stale leftover -- from the SAME failed fetch that produced an error
    # marker (e.g. a partial bar with close: 0), a normal full-universe
    # fetch that still includes an excluded symbol (VCX/TRLV/RVI/BYDDY), or
    # a compatible executor's own already-covered ticker -- raised and
    # crashed candidate generation for every remaining ticker too, instead
    # of the tolerated no-op each of these is supposed to be (Codex review:
    # "Apply error markers before parsing errored payloads"; "Drop excluded
    # tickers before eager parsing"; "Exclude same-window tickers before
    # scoring"). Filtered here, before _StaticMarketData construction, not
    # just skipped in the main loop below, since that constructor parses
    # eagerly. No ticker's outcome changes either way -- an error-marked
    # ticker still hits _unavailable_signal, and an excluded/already-covered
    # ticker is still skipped by the main loop's own `continue`, regardless
    # of whether it also stayed in finance_inputs.
    # A whole-share ticker (config.whole_share_tickers, e.g. RVII/BYDDY) is
    # excluded here too, for the identical reason as excluded_tickers above.
    # _recognized_tickers now accepts it by name (see that function's
    # docstring) precisely so a full-universe fetch that legitimately
    # includes it is not rejected outright -- but "recognized" is not
    # "scored": this module's own main loop below never evaluates a
    # whole-share symbol, it is bought via the entirely separate
    # accumulate-then-fire route wired into near_close_dca._run_once_impl.
    # Before RVII moved from excluded_tickers into whole_share_tickers this
    # filter already dropped it for free; once moved, an unused stale/
    # malformed whole-share payload would otherwise reach
    # _StaticMarketData's eager parse and abort scoring for every fractional
    # ticker in the same run, for a payload nothing here ever reads (Codex
    # review, PR #786: "Filter whole-share payloads before eager parsing").
    finance_inputs = (
        {}
        if finance_unavailable
        else {
            ticker: payload
            for ticker, payload in market_data_by_source.get("finance", {}).items()
            if ticker not in finance_ticker_errors
            and ticker not in config.excluded_tickers
            and ticker not in config.whole_share_tickers
            and ticker not in already_covered_tickers_set
        }
    )
    robinhood_inputs = (
        {}
        if robinhood_unavailable
        else {
            ticker: payload
            for ticker, payload in market_data_by_source.get("robinhood", {}).items()
            if ticker not in robinhood_ticker_errors
            and ticker not in config.excluded_tickers
            and ticker not in config.whole_share_tickers
            and ticker not in already_covered_tickers_set
        }
    )
    # Filtered identically to finance_inputs -- the fallback stands in for
    # Finance evidence, so it is held to the same excluded/whole-share/
    # already-covered/ticker-error filtering before ever reaching
    # _StaticMarketData's eager parse.
    yfinance_inputs = (
        {}
        if yfinance_unavailable
        else {
            ticker: payload
            for ticker, payload in market_data_by_source.get(SourceName.YFINANCE_FALLBACK.value, {}).items()
            if ticker not in yfinance_ticker_errors
            and ticker not in config.excluded_tickers
            and ticker not in config.whole_share_tickers
            and ticker not in already_covered_tickers_set
        }
    )
    finance_data = _StaticMarketData(
        SourceName.FINANCE,
        finance_inputs,
        quote_staleness_minutes=config.finance_interval_minutes,
        trading_day=trading_day,
    )
    robinhood_data = _StaticMarketData(
        SourceName.ROBINHOOD,
        robinhood_inputs,
        quote_staleness_minutes=config.robinhood_interval_minutes,
        trading_day=trading_day,
    )
    # yfinance stands in for Finance, never for Robinhood (Robinhood is
    # execution-broker validation/context, not a policy primary with a
    # configured fallback) -- so it shares Finance's staleness window
    # (config.finance_interval_minutes), the same interval the executor
    # prompts require the fallback fetch to match.
    yfinance_data = _StaticMarketData(
        SourceName.YFINANCE_FALLBACK,
        yfinance_inputs,
        quote_staleness_minutes=config.finance_interval_minutes,
        trading_day=trading_day,
    )
    # The same decision/lookback anchors evaluate_source_signal computes
    # internally (near_close_dca._compute_signal_timestamps, not itself
    # importable -- rebuilt here from the same public pieces:
    # decision_time_et, completed_bar_anchor, and trend_lookback_minutes)
    # but per-source, since Finance and Robinhood have different bar
    # intervals.
    decision_dt = datetime.combine(trading_day, config.decision_time_et, tzinfo=ET)
    finance_decision_anchor = completed_bar_anchor(decision_dt, config.finance_interval_minutes, config)
    robinhood_decision_anchor = completed_bar_anchor(decision_dt, config.robinhood_interval_minutes, config)
    finance_lookback_anchor = finance_decision_anchor - timedelta(minutes=config.trend_lookback_minutes)
    robinhood_lookback_anchor = robinhood_decision_anchor - timedelta(minutes=config.trend_lookback_minutes)
    # Whole-universe switch, matching the checked-in market-data source
    # policy ("the generator switches the entire ordinary universe to the
    # configured fallback when even one Finance row is unresolved" --
    # claude/dca-executor-prompts/live-mirror/1230ET-*.txt's Gate 5 step,
    # applied here to the 1530ET dual-source engine): Finance is judged
    # unusable for the WHOLE run, not per-ticker, so a partial fallback
    # payload for only the tickers Finance happened to miss is never
    # sufficient and never silently mixed with real Finance evidence for
    # other tickers in the same run.
    finance_needs_fallback = (
        finance_unavailable
        or bool(finance_ticker_errors)
        or any(
            _attempted_source_incomplete(
                finance_data, ticker, finance_decision_anchor, finance_lookback_anchor, config.finance_interval_minutes
            )
            for ticker in finance_inputs
        )
    )
    # When Finance needs the fallback but no usable fallback evidence was
    # supplied, this stays False rather than raising: the 1530ET engine
    # already tolerates a down Finance source today by reconciling on
    # Robinhood alone (reconcile_signals' "Robinhood-only BUY because
    # finance source is unavailable" path) -- requiring yfinance-or-abort
    # here would regress that existing, already-live degraded-mode
    # behavior instead of only adding a new one.
    use_yfinance_fallback = finance_needs_fallback and not yfinance_unavailable

    candidates: List[Dict[str, Any]] = []
    for ticker in config.tickers:
        if ticker in config.excluded_tickers or ticker in already_covered_tickers_set:
            continue
        # A ticker present in the map but missing a piece
        # evaluate_source_signal requires to score it at all -- a fresh
        # decision bar, a lookback bar, or previous_close_bar -- means a
        # truncated/partial/malformed fetch, not "this source doesn't cover
        # this ticker": evaluate_source_signal's own "any of the three is
        # None" check defaults to provider_error=False, the SAME NO_DATA
        # shape as a genuinely uncovered ticker, so an incomplete attempted
        # payload could otherwise silently permit a single-source auto-buy
        # from the other source instead of forcing manual review (Codex
        # review: "Treat incomplete attempted source payloads as errors";
        # folds in the prior round's stale-decision-bar check, "Reject stale
        # intraday bars"). Checked only when the ticker was actually
        # attempted on this source (ticker in *_inputs) -- a ticker never
        # attempted is the existing, separate "not covered" case above.
        robinhood_incomplete = ticker in robinhood_inputs and _attempted_source_incomplete(
            robinhood_data,
            ticker,
            robinhood_decision_anchor,
            robinhood_lookback_anchor,
            config.robinhood_interval_minutes,
        )
        # use_yfinance_fallback is a whole-run decision (see its definition
        # above): when set, EVERY ticker's finance-side signal is built from
        # yfinance_data instead of finance_data, never a per-ticker mix of
        # the two. Robinhood is entirely unaffected either way.
        if use_yfinance_fallback:
            yfinance_incomplete = ticker in yfinance_inputs and _attempted_source_incomplete(
                yfinance_data,
                ticker,
                finance_decision_anchor,
                finance_lookback_anchor,
                config.finance_interval_minutes,
            )
            if ticker in yfinance_ticker_errors or yfinance_incomplete:
                finance_signal: Optional[SourceSignal] = _unavailable_signal(ticker, SourceName.YFINANCE_FALLBACK)
            elif ticker in yfinance_inputs:
                finance_signal = evaluate_source_signal(
                    ticker, now, config, yfinance_data, config.finance_interval_minutes
                )
            else:
                finance_signal = None
        else:
            finance_incomplete = ticker in finance_inputs and _attempted_source_incomplete(
                finance_data, ticker, finance_decision_anchor, finance_lookback_anchor, config.finance_interval_minutes
            )
            if finance_unavailable or ticker in finance_ticker_errors or finance_incomplete:
                finance_signal = _unavailable_signal(ticker, SourceName.FINANCE)
            elif ticker in finance_inputs:
                finance_signal = evaluate_source_signal(
                    ticker, now, config, finance_data, config.finance_interval_minutes
                )
            else:
                finance_signal = None
        if robinhood_unavailable or ticker in robinhood_ticker_errors or robinhood_incomplete:
            robinhood_signal: Optional[SourceSignal] = _unavailable_signal(ticker, SourceName.ROBINHOOD)
        elif ticker in robinhood_inputs:
            robinhood_signal = evaluate_source_signal(
                ticker, now, config, robinhood_data, config.robinhood_interval_minutes
            )
        else:
            robinhood_signal = None
        decision = reconcile_signals(ticker, finance_signal, robinhood_signal)
        candidates.append(_decision_to_dict(ticker, decision))
    return candidates


def _planner_incompatible(row: List[Any]) -> bool:
    """True when ``dca_runner.plan_orders()`` would refuse ``row`` as
    ``REFUSED_INCONSISTENT_MULTIPLIER``.

    ``dca_runner``'s consistency check validates multiplier/drop/base against
    ONLY the global ``config/sizing_ladder.json`` -- it has no awareness of
    ``config/per_ticker_sizing.json`` calibration overrides that
    ``near_close_dca.graded_buy_multiplier``/``select_first_allocation_base_usd``
    DO respect (per ``docs/strategy.md``, most of the active universe is
    calibrated). A calibrated ticker's real, correct multiplier/drop/base can
    legitimately fall outside the global ladder's range for that multiplier,
    so a ``planner_candidates`` row built from it is not automatically safe
    just because it came from a real BUY decision.

    Calling ``dca_runner``'s OWN validation function here -- rather than
    re-deriving the same check locally and risking it drifting from
    ``dca_runner``'s real logic -- is what actually determines whether
    ``plan_orders()`` would accept the row, instead of assuming every
    ``planner_candidates`` row is planner-safe when most of the active
    universe is calibrated (Codex review: "Keep planner rows compatible with
    calibrated ladders").
    """
    import dca_runner  # deferred -- see the module-level comment by the (absent) top import

    (candidate,) = dca_runner._parse_candidate_rows([row])
    return dca_runner._candidate_multiplier_notional_mismatch(candidate)


def _is_finite_non_negative(value: float) -> bool:
    """Shared predicate behind ``_finite_non_negative_float`` (CLI boundary)
    and ``_validate_deployed_last_7d_usd`` (public-helper boundary) -- see
    the latter for why both need it independently."""
    return math.isfinite(value) and value >= 0


def _validate_deployed_last_7d_usd(value: float) -> None:
    """Raise if ``config.deployed_last_7d_usd`` is not finite and non-negative.

    A NaN or negative value would reach ``near_close_dca.kill_switch_engaged``
    (``deployed_last_7d_usd > threshold``), which evaluates False for both --
    silently disabling the 7-day kill-switch instead of tripping it, and
    letting ``graded_buy_multiplier`` emit uncapped sizing on exactly the kind
    of malformed-input day the guard exists to catch. ``_finite_non_negative_float``
    already rejects this at the CLI's ``--deployed-last-7d-usd`` argparse
    boundary, but ``generate_candidates`` is itself a public, directly-callable
    API -- a caller building its own ``StrategyConfig`` (with this field set
    directly, not through the CLI) and invoking the helper bypasses that
    CLI-only check entirely (Codex review: "Validate helper trailing spend
    before scoring").
    """
    if not _is_finite_non_negative(value):
        raise ValueError(f"config.deployed_last_7d_usd must be a finite, non-negative number, got {value!r}")


def _validate_risk_guard_config(config: StrategyConfig) -> None:
    """Raise if ``config.monthly_cash_allocation_usd`` or
    ``config.kill_switch_7d_deploy_fraction`` cannot safely reach
    ``near_close_dca.kill_switch_engaged``.

    ``_validate_deployed_last_7d_usd`` above only covers the trailing-spend
    side of that function's comparison; ``kill_switch_engaged`` reads two
    more ``StrategyConfig`` fields this helper boundary never checked. A
    non-finite allocation skips that function's OWN ``allocation <= 0``
    escape hatch entirely -- ``NaN <= 0`` is False, like every NaN
    comparison -- and falls through to ``threshold = fraction * allocation``
    = ``NaN``, where ``deployed_last_7d_usd > NaN`` is always False
    regardless of how large the real trailing spend is: the guard silently
    never engages instead of failing closed. An out-of-``[0, 1]`` fraction
    (NaN-comparison-vulnerable the same way: ``0 <= NaN <= 1`` is also False)
    hits ``kill_switch_engaged``'s own range check and disables the guard
    the same way -- a deliberate escape hatch for the documented "operator
    left the allocation unset" case, not for a directly-constructed
    ``StrategyConfig`` carrying corrupted data. ``dca_config.load_budgets``
    already rejects both at the config-loading boundary (finite allocation;
    fraction in ``[0, 1]``), but ``generate_candidates`` is itself a public,
    directly-callable API -- a caller building its own ``StrategyConfig``
    (not via ``load_strategy_config``) and invoking the helper bypasses that
    entirely, the same gap ``_validate_deployed_last_7d_usd`` closes for the
    trailing-spend field (Codex review: "Validate risk-guard inputs in the
    helper").
    """
    allocation = config.monthly_cash_allocation_usd
    if not math.isfinite(allocation):
        raise ValueError(f"config.monthly_cash_allocation_usd must be finite, got {allocation!r}")
    # Finite is not the whole story. kill_switch_engaged() treats ANY
    # non-positive allocation as the documented "operator left it unset"
    # escape hatch (allocation <= 0 -> guard disabled). ZERO is that
    # legitimate, documented unset value and must keep working; NEGATIVE --
    # corrupted data, not an intentional unset -- takes the same branch and
    # disables the 7-day deployment kill switch identically, while being
    # outside schemas/budgets.schema.json's own `minimum: 0` -- so it buys the
    # escape hatch's behaviour without being the case the escape hatch
    # documents (Antigravity/Codex review: "Reject negative monthly
    # allocations in the public helper"). The check and its message are kept
    # identical to MorningDCAExecutor.apply_deployment_risk_guard, whose
    # docstring names this function as the validator it mirrors: fixing one
    # and not the other would make that claim false and leave the two
    # boundaries disagreeing about what a valid allocation is (Codex review:
    # "Reject negative monthly allocation values").
    if allocation < 0:
        raise ValueError(
            "config.monthly_cash_allocation_usd must be non-negative (0 is the documented "
            f"'allocation unset' escape hatch), got {allocation!r}; a negative allocation "
            "disables the deployment kill switch"
        )
    fraction = config.kill_switch_7d_deploy_fraction
    if not 0 <= fraction <= 1:
        raise ValueError(f"config.kill_switch_7d_deploy_fraction must be within [0, 1], got {fraction!r}")


def _finite_non_negative_float(raw: str) -> float:
    """argparse ``type=`` for ``--deployed-last-7d-usd``. See
    ``_validate_deployed_last_7d_usd`` for why this value must be finite and
    non-negative (Codex review: "Reject non-finite trailing spend"). Rejected
    here, at the CLI boundary, in addition to -- not instead of --
    ``generate_candidates``'s own check, since a Python caller of that public
    helper never goes through argparse at all.
    """
    value = float(raw)
    if not _is_finite_non_negative(value):
        raise argparse.ArgumentTypeError(
            f"--deployed-last-7d-usd must be a finite, non-negative number, got {raw!r}"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slot", required=True, choices=sorted(SLOT_SPECS), help="Which of the seven roster slots this invocation is for.")
    parser.add_argument("--trading-day", required=True, help="YYYY-MM-DD, the ET calendar day this slot fires on.")
    parser.add_argument(
        "--market-data",
        required=True,
        type=Path,
        help="Path to the pre-fetched market-data JSON. See the module docstring for the exact shape.",
    )
    parser.add_argument(
        "--deployed-last-7d-usd",
        type=_finite_non_negative_float,
        required=True,
        help=(
            "Trailing-7-day AFTERNOON-scheduled spend only (>= 2:30pm ET runs -- see "
            "dca_risk.is_afternoon_scheduled_evidence), NOT a sum across all seven slots. "
            "Feeds the kill-switch. Must be finite and non-negative. No default -- see the "
            "module docstring."
        ),
    )
    parser.add_argument("--config-dir", type=Path, default=get_config_dir())
    parser.add_argument(
        "--already-covered-tickers",
        nargs="*",
        default=(),
        metavar="TICKER",
        help=(
            "Tickers a compatible executor already placed an order for in THIS SAME window "
            "(live-mirror prompt Gate 4's compatible-stand-down case: 'generate this slot's "
            "candidate list EXCLUDING every ticker the compatible executor already placed an "
            "order for in THIS SAME window'). Excluded from planner_candidates/"
            "same_day_top_up_candidates/buy_count even when their own signal is BUY -- "
            "config/budgets.json's one_order_per_ticker_per_stream safeguard blocks a second "
            "buy for the same ticker in the same slot regardless of size, so Gate 6.5's "
            "same-day top-up guard (built for a legitimate repeat across DIFFERENT slots/"
            "sessions) must never be asked to size a same-window duplicate. Default: none."
        ),
    )
    return parser


# The subset of config/*.json load_strategy_config reads that directly
# determines live sizing/universe correctness: tickers.json (which tickers
# exist/are excluded), budgets.json (base buy amounts), sizing_ladder.json
# (the global multiplier/drop ladder), and per_ticker_sizing.json
# (calibration overrides most of the active universe depends on, per
# docs/strategy.md). trading_window.json and near_close_dca.json are
# deliberately NOT included here -- both are documented in dca_config.py as
# optional overlays where an absent file intentionally keeps the hardcoded
# default, not a live-correctness gap this entry point needs to close.
#
# market_calendar.json is a different kind of dependency: load_strategy_config
# never reads it, but _previous_trading_day (used by _StaticMarketData to
# validate previous_close_bar's date) calls dca_runner.market_day_status,
# which reads it from dca_runner.CONFIG_DIR -- the SAME directory --config-dir
# is wired to via the env-var injection in main() below. Omitting it from
# this list would still let a selected --config-dir missing the file reach
# generate_candidates(), where it fails as a raw, unhandled FileNotFoundError
# deep inside dca_runner._read_config instead of this module's usual clean,
# early "missing required file" abort.
_REQUIRED_CONFIG_FILENAMES = (
    "tickers.json",
    "budgets.json",
    "sizing_ladder.json",
    "per_ticker_sizing.json",
    "market_calendar.json",
)


def _require_populated_config(config_dir: Path) -> None:
    """Raise if any of ``_REQUIRED_CONFIG_FILENAMES`` exists but produces no
    real override, has a duplicate JSON key, or -- for
    ``per_ticker_sizing.json`` -- does not cover every active ticker.

    Existence alone (the ``_REQUIRED_CONFIG_FILENAMES`` check in ``main()``)
    is not enough: each ``load_*`` function below tolerates an empty or
    insufficiently-populated file exactly the same way it tolerates a
    missing one -- returning ``{}`` and leaving ``load_strategy_config``'s
    ``StrategyConfig()`` defaults in place -- so a ``--config-dir`` with the
    right filenames but empty/inactive contents passed the filename check
    while still silently scoring against the hardcoded universe, ladder,
    budgets, or no per-ticker calibration (Codex review: "Validate required
    config contents, not just filenames"). Calls the SAME ``load_*``
    functions ``load_strategy_config`` itself calls, rather than re-deriving
    what "populated" means, so this can never drift from what
    ``load_strategy_config`` would actually do with the file.
    """
    import dca_runner  # deferred -- see the module-level comment by the (absent) top import

    # dca_config._read_json (which every load_* call below goes through)
    # uses plain json.loads with no duplicate-key protection -- unlike
    # main()'s own --market-data read and dca_runner's own config/*.json
    # reads, NEITHER of which covers this SELECTED --config-dir's files.
    # Two "TEST" entries under per_ticker_sizing.json's "tickers", for
    # instance, would silently keep only the second calibration model, with
    # no signal the first was ever discarded (Codex review: "Reject
    # duplicate keys in selected config"). Checked independently, before
    # any of the load_* calls below -- this only proves no duplicate
    # exists, it does not replace load_strategy_config's own separate parse.
    for name in _REQUIRED_CONFIG_FILENAMES:
        path = config_dir / name
        try:
            json.loads(path.read_bytes(), object_pairs_hook=dca_runner._reject_duplicate_keys)
        except ValueError as exc:
            raise ValueError(f"{path} is malformed or has a duplicate JSON key: {exc}") from exc

    ticker_config = load_tickers(config_dir)
    tickers = ticker_config.get("tickers")
    if not tickers:
        raise ValueError(f"{config_dir / 'tickers.json'} has no non-empty 'active' ticker list")
    # load_tickers only does tuple(str(t) for t in active) -- no dedup, no
    # case/whitespace normalization. A duplicate entry (["TEST", "TEST"])
    # makes the main loop below iterate that ticker twice, independently
    # scoring and potentially publishing two BUY/top-up rows for the same
    # symbol; a lower-case or padded entry ("test") becomes part of
    # known_tickers for every validator in this module, but would never
    # match dca_runner's own canonical-uppercase whitelist if a caller's
    # --config-dir differs from dca_runner.CONFIG_DIR, silently drifting
    # from the runner's real universe (Codex review: "Validate active
    # tickers before scoring").
    if len(tickers) != len(set(tickers)):
        duplicate_tickers = sorted({t for t in tickers if tickers.count(t) > 1})
        raise ValueError(f"{config_dir / 'tickers.json'} 'active' list has duplicate ticker(s) {duplicate_tickers!r}")
    non_canonical = sorted(t for t in tickers if t != t.strip().upper())
    if non_canonical:
        raise ValueError(
            f"{config_dir / 'tickers.json'} 'active' list has non-canonical ticker(s) {non_canonical!r}; "
            "entries must be upper-case with no leading/trailing whitespace"
        )
    # load_tickers's "excluded_tickers" is a frozenset(str(t) for t in excluded) --
    # the same no case/whitespace normalization as "tickers" above, but the
    # consequence here is a silent SAFETY gap rather than a double-count.
    # config.excluded_tickers is checked everywhere with exact `in` membership
    # (generate_candidates's main loop, _validate_already_covered_tickers,
    # _validate_market_data_ticker_keys's known_tickers union), so an excluded
    # entry that merely differs in case/whitespace from its canonical active
    # counterpart (active "VCX" vs. excluded "vcx") never matches -- the ticker
    # reads as simply not excluded and is scored/bought like any other active
    # symbol, silently defeating the exclusion the config author intended
    # (Codex review: "Reject non-canonical excluded tickers").
    excluded_tickers = ticker_config.get("excluded_tickers") or set()
    non_canonical_excluded = sorted(t for t in excluded_tickers if t != t.strip().upper())
    if non_canonical_excluded:
        raise ValueError(
            f"{config_dir / 'tickers.json'} 'excluded' list has non-canonical ticker(s) "
            f"{non_canonical_excluded!r}; entries must be upper-case with no leading/trailing whitespace"
        )
    # Truthiness alone only proves SOME budget field survived -- an
    # unrelated field present while base_amount_usd/base_amount_below_
    # close_usd (the actual live sizing bases select_first_allocation_
    # base_usd reads) are both absent would still pass, silently sizing
    # every BUY off StrategyConfig()'s hardcoded defaults while reporting
    # the selected live config was accepted (Codex review: "Require the
    # actual live budget keys").
    budgets = load_budgets(config_dir)
    if "base_buy_amount_usd" not in budgets or "base_buy_amount_below_close_usd" not in budgets:
        raise ValueError(f"{config_dir / 'budgets.json'} is missing base_amount_usd/base_amount_below_close_usd")
    # kill_switch_engaged()'s own `if allocation <= 0: return False` guard
    # already safely handles a non-positive monthly_cash_allocation_usd
    # (an intentional "guard disabled, never crash" design -- see that
    # function's own docstring), so only finiteness needs checking here.
    # NaN/Infinity is NOT caught by `<= 0` (NaN compares False against
    # everything), so it falls through to `threshold = fraction *
    # allocation` = NaN, and `deployed_last_7d_usd > NaN` is always False
    # -- silently disabling the kill switch regardless of how large the
    # real trailing spend is, letting fallback-ladder BUYs publish at
    # uncapped multipliers (Codex review: "Reject non-finite risk-guard
    # allocation").
    monthly_cash_allocation_usd = budgets.get("monthly_cash_allocation_usd")
    if monthly_cash_allocation_usd is not None and not math.isfinite(monthly_cash_allocation_usd):
        raise ValueError(
            f"{config_dir / 'budgets.json'} monthly_cash_allocation_usd must be finite, "
            f"got {monthly_cash_allocation_usd!r}"
        )
    if not load_sizing_tiers(config_dir):
        raise ValueError(f"{config_dir / 'sizing_ladder.json'} has no non-empty 'tiers' list")
    # load_per_ticker_sizing's own normalization runs before any check below
    # ever sees the data: ticker.upper() silently collapses a case
    # collision ("TEST" and "test" both present) into whichever entry
    # iterates last, discarding the other with no signal anything was
    # dropped; full_base = max(1.0, float(model.get("base_amount_usd",
    # 1.0))) floors a negative/NaN base into a safe-looking 1.0 with no
    # trace of the corruption it papered over. The tier-level fields
    # (min_drop_pct/multiplier) get no such coercion -- a plain float()
    # with no floor -- so the calibration-bounds check below still sees
    # those unmodified; only the ticker keys and the two base fields need
    # checking against the RAW JSON, before load_per_ticker_sizing
    # normalizes them (Codex review: "Validate raw per-ticker sizing
    # before normalization").
    raw_per_ticker_sizing = json.loads((config_dir / "per_ticker_sizing.json").read_bytes())
    raw_tickers = raw_per_ticker_sizing.get("tickers")
    if isinstance(raw_tickers, dict):
        seen_upper: Dict[str, str] = {}
        for raw_ticker, model in raw_tickers.items():
            if not isinstance(raw_ticker, str):
                continue  # load_per_ticker_sizing's own shape check rejects this
            upper = raw_ticker.upper()
            if upper in seen_upper:
                raise ValueError(
                    f"{config_dir / 'per_ticker_sizing.json'} tickers has case-colliding keys "
                    f"{seen_upper[upper]!r} and {raw_ticker!r}, both normalizing to {upper!r}"
                )
            seen_upper[upper] = raw_ticker
            if not isinstance(model, dict):
                continue  # load_per_ticker_sizing's own shape check rejects this
            for base_field in ("base_amount_usd", "base_amount_below_close_usd"):
                if base_field not in model:
                    continue
                raw_value = model[base_field]
                if (
                    isinstance(raw_value, bool)
                    or not isinstance(raw_value, (int, float))
                    or not math.isfinite(raw_value)
                    or raw_value <= 0
                ):
                    raise ValueError(
                        f"{config_dir / 'per_ticker_sizing.json'} ticker {raw_ticker!r}'s {base_field} "
                        f"must be a finite, positive number, got {raw_value!r}"
                    )
    per_ticker_sizing = load_per_ticker_sizing(config_dir).get("per_ticker_sizing")
    if not per_ticker_sizing:
        raise ValueError(f"{config_dir / 'per_ticker_sizing.json'} is not active, or has no calibrated tickers")
    # At-least-one-calibrated-ticker alone doesn't prove COVERAGE: an
    # active ticker missing from "tickers" here silently falls back to the
    # global ladder via _ticker_sizing()'s own empty-mapping-for-fallback
    # behavior -- a real, designed path for a genuinely uncalibrated
    # symbol, but not something a caller should discover only after Gate 5
    # already reported the selected calibrated config was accepted (Codex
    # review: "Require calibration coverage for active tickers").
    uncalibrated = sorted(set(tickers) - set(per_ticker_sizing))
    if uncalibrated:
        raise ValueError(
            f"{config_dir / 'per_ticker_sizing.json'} has no calibration entry for active ticker(s) "
            f"{uncalibrated!r}"
        )
    # load_per_ticker_sizing validates SHAPE (a tiers list, min_drop_pct/
    # multiplier keys present) but not whether those numbers are usable:
    # float() on NaN/Infinity/a negative tier threshold succeeds the same
    # way it does everywhere else in this module, and full_base = max(1.0,
    # ...) only floors the per-ticker base -- there is no ceiling. A
    # calibrated base paired with its own tier's multiplier that exceeds
    # dca_runner.MAX_PER_ORDER_USD (the same $10 = $2 base x 5x live
    # ceiling dca_runner.plan_orders()/execute_plan() enforce) would still
    # reach graded_buy_multiplier/select_first_allocation_base_usd
    # unchecked, publishing a BUY/same_day_top_up_candidates row sized
    # above the live per-order limit even though the selected config is
    # malformed (Codex review: "Validate calibration numeric bounds").
    for ticker, model in per_ticker_sizing.items():
        for min_drop_pct, multiplier in model["tiers"]:
            if not math.isfinite(min_drop_pct) or min_drop_pct < 0:
                raise ValueError(
                    f"{config_dir / 'per_ticker_sizing.json'} ticker {ticker!r} has a non-finite or "
                    f"negative tier drop threshold {min_drop_pct!r}"
                )
            if not math.isfinite(multiplier) or multiplier < 0:
                raise ValueError(
                    f"{config_dir / 'per_ticker_sizing.json'} ticker {ticker!r} has a non-finite or "
                    f"negative tier multiplier {multiplier!r}"
                )
            for base_field in ("base_amount_usd", "base_amount_below_close_usd"):
                amount = model[base_field] * multiplier
                if not math.isfinite(amount) or amount > dca_runner.MAX_PER_ORDER_USD:
                    raise ValueError(
                        f"{config_dir / 'per_ticker_sizing.json'} ticker {ticker!r}'s {base_field} "
                        f"({model[base_field]!r}) x multiplier ({multiplier!r}) = {amount!r} exceeds "
                        f"MAX_PER_ORDER_USD ({dca_runner.MAX_PER_ORDER_USD!r})"
                    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    trading_day = date.fromisoformat(args.trading_day)
    spec = SLOT_SPECS[args.slot]
    now = datetime.combine(trading_day, spec["decision_time_et"], tzinfo=ET)

    # load_strategy_config only *warns* and falls back to hardcoded
    # StrategyConfig defaults when config_dir doesn't resolve, or per-file
    # when an individual config/*.json is absent -- the right behavior for a
    # dev/installed-wheel caller that may genuinely have no config/ nearby,
    # but wrong for this live entry point: a mistyped --config-dir, a
    # mounted repo missing config/ entirely, or an existing-but-incomplete
    # mount missing one of the files below would then silently score
    # buy_count/planner_candidates/same_day_top_up_candidates against
    # built-in defaults (the hardcoded universe, global ladder, or no
    # per-ticker calibration) instead of the checked-in live config, while
    # the caller believes the selected config was used, with no signal that
    # happened (Codex review: "Fail closed when the config directory is
    # unavailable"; "Require config files before scoring"). Checked here,
    # before dca_runner is even imported below, rather than changing that
    # shared loader's own tolerant behavior for its other callers.
    if not args.config_dir.is_dir():
        raise ValueError(
            f"--config-dir {args.config_dir} does not exist; refusing to score against "
            "load_strategy_config's hardcoded StrategyConfig defaults instead of the real "
            "checked-in config"
        )
    missing_config_files = [
        name for name in _REQUIRED_CONFIG_FILENAMES if not (args.config_dir / name).is_file()
    ]
    if missing_config_files:
        raise ValueError(
            f"--config-dir {args.config_dir} is missing required file(s) {missing_config_files!r}; "
            "refusing to score with load_strategy_config's hardcoded StrategyConfig defaults for "
            "the missing piece(s) instead of the real checked-in config"
        )
    # dca_runner reads CONFIG_DIR = get_config_dir() (i.e. $DCA_CONFIG_DIR,
    # or its own default search) at ITS OWN import time, with no per-call
    # override -- so merely deferring this import past parse_args (which
    # fixed a bad ambient $DCA_CONFIG_DIR breaking --help) does not, by
    # itself, let a valid --config-dir recover a run when that ambient
    # value is stale or missing: dca_runner would still import using the
    # bad ambient value regardless of what --config-dir says (Codex review:
    # "Defer dca_runner until after honoring --config-dir"). $DCA_CONFIG_DIR
    # is injected here, in the narrowest possible window immediately around
    # dca_runner's first import in this process, and restored right after
    # -- dca_runner reads the env var exactly once, at import time, so this
    # narrow window is sufficient and leaves no lasting os.environ mutation
    # behind for the rest of this process. Only takes effect if dca_runner
    # has not already been imported elsewhere in this process (Python
    # caches modules; a caller that already imported it -- e.g. this
    # module's own test suite -- keeps whatever CONFIG_DIR that earlier
    # import resolved, unaffected by this injection).
    _dca_config_dir_env_backup = os.environ.get("DCA_CONFIG_DIR")
    os.environ["DCA_CONFIG_DIR"] = str(args.config_dir)
    try:
        import dca_runner
    finally:
        if _dca_config_dir_env_backup is None:
            os.environ.pop("DCA_CONFIG_DIR", None)
        else:
            os.environ["DCA_CONFIG_DIR"] = _dca_config_dir_env_backup

    # object_pairs_hook rejects a duplicate key at any nesting level (e.g. two
    # sources.finance.TEST entries from a malformed merge), the same
    # dca_runner._reject_duplicate_keys hook config/*.json reads already use
    # -- plain json.loads silently keeps only the LAST occurrence, so
    # conflicting provider evidence could be overwritten before validation/
    # scoring ever sees the discarded row (Codex review: "Reject duplicate
    # keys in market-data JSON").
    payload = json.loads(
        args.market_data.read_bytes(), object_pairs_hook=dca_runner._reject_duplicate_keys
    )
    # "sources" missing ENTIRELY is malformed input, not "both providers returned
    # nothing" -- a bare .get("sources", {}) default made the two indistinguishable,
    # so a truncated/malformed file quietly produced a schema-valid NO_DATA/
    # buy_count=0 document (exit 0) that a caller could read as a legitimate quiet
    # tape instead of a data-evidence failure to abort on (Codex review: "Require
    # the top-level sources payload"). Both provider snapshots are required
    # evidence for this entry point; a real "no data for this source" is spelled
    # {} explicitly, not by omitting the key.
    if "sources" not in payload:
        raise ValueError("--market-data must contain a top-level 'sources' object")
    market_data_by_source = payload["sources"]
    if not isinstance(market_data_by_source, dict):
        raise ValueError("--market-data must contain a top-level 'sources' object")
    # source_errors/ticker_errors shape AND content are validated inside
    # generate_candidates itself (_validate_source_errors/
    # _validate_ticker_errors), not here -- see those functions' docstrings
    # for why validating only in main() would leave generate_candidates'
    # other, independently-public callers unprotected.
    source_errors = payload.get("source_errors", [])
    # generate_candidates() defaults ticker_errors to None (its own Python
    # kwarg default, for a direct caller that genuinely omits the
    # argument) and coerces that None to {} BEFORE _validate_ticker_errors
    # ever runs -- so an explicit JSON "ticker_errors": null (key PRESENT,
    # value None -- e.g. a failed serializer dropping the per-symbol
    # exception map) survives payload.get(..., {}) unchanged (the default
    # only applies to an ABSENT key) and then reads exactly like a
    # genuinely omitted key by the time it reaches that coercion,
    # silently treated as "no per-ticker errors" instead of the malformed
    # evidence it actually is. Checked here, at the JSON boundary, where
    # "omitted" and "explicitly null" are still distinguishable -- by the
    # time this reaches generate_candidates() that distinction is already
    # gone (Codex review: "Reject null ticker_errors payloads").
    if "ticker_errors" in payload and payload["ticker_errors"] is None:
        raise ValueError("--market-data's 'ticker_errors', if present, must be an object, got null")
    ticker_errors = payload.get("ticker_errors", {})

    _require_populated_config(args.config_dir)
    # Explicitly pass `now` (derived from --trading-day above, not the real
    # wall clock) so load_ml_sizing_adjustment's freshness check judges
    # config/ml_sizing_adjustment.json's generated_at against the candidate
    # trading day this run is actually scoring, not "today" -- omitting it
    # would apply a publish that's from the future relative to a historical/
    # replay trading day, or skip one that was genuinely fresh AT that day
    # (Codex review: "Use the candidate trading day for ML freshness"; see
    # load_strategy_config's own `now` docstring for the general contract).
    config = load_strategy_config(args.config_dir, now=now, deployed_last_7d_usd=args.deployed_last_7d_usd)

    # already_covered_tickers shape/content is validated inside
    # generate_candidates itself (_validate_already_covered_tickers), same
    # reasoning as source_errors/ticker_errors above.
    candidates = generate_candidates(
        slot=args.slot,
        now=now,
        config=config,
        market_data_by_source=market_data_by_source,
        source_errors=source_errors,
        ticker_errors=ticker_errors,
        already_covered_tickers=args.already_covered_tickers,
    )
    # kill_switch_engaged() is re-checked here (rather than only implicitly, inside
    # graded_buy_multiplier) purely for visibility: a caller building Gate 9's report
    # should be able to say *why* every multiplier capped at 1x without recomputing it.
    resolved_config = dataclasses.replace(
        config, decision_time_et=spec["decision_time_et"], ignore_trend_filter=not spec["trend_required"]
    )
    # Every active, non-excluded ticker gets a row in `candidates` regardless of
    # its action (BUY, NO_ACTION, NO_DATA, MANUAL_REVIEW) -- so evaluated_count
    # is a full-universe-scan count, not "how many candidates there are to act
    # on" (that's buy_count). A field literally named candidate_count would
    # read as Gate 5's documented "zero candidates: end silently" terminal
    # condition (claude/dca-executor-prompts/live-mirror/1530ET-...) even on an
    # ordinary day where every ticker is simply NO_ACTION, which it always is
    # unless something qualifies (Codex review: "Report zero actionable
    # candidates as zero").
    # A ticker in --already-covered-tickers (Gate 4's compatible-stand-down
    # case) never reaches `candidates` at all -- generate_candidates skips
    # it the same way it skips config.excluded_tickers, so it can never
    # appear here as a BUY row either. No separate filtering needed on this
    # side; see generate_candidates' own docstring for why (Codex review:
    # "Exclude same-window tickers before scoring").
    buy_rows = [c for c in candidates if c["action"] == Action.BUY.value]
    # A source disagreement or per-ticker provider error reconciles to
    # MANUAL_REVIEW, not BUY -- so buy_count alone reads as "zero candidates,
    # quiet day" even when a ticker urgently needs a human/agent look. The
    # native final runner tracks this explicitly (manual_review_tickers/
    # manual_recovery_required); without an equivalent summary here, a caller
    # would have to hand-scan every debug row in `candidates` to notice
    # (Codex review: "Report manual-review rows before zero-candidate exits").
    manual_review_tickers = [c["ticker"] for c in candidates if c["action"] == Action.MANUAL_REVIEW.value]
    # A provider error doesn't always escalate to MANUAL_REVIEW -- only when
    # the surviving source would otherwise BUY (see _decision_to_dict's
    # provider_error field). If neither source qualifies as BUY anyway, the
    # ticker reconciles as plain NO_DATA/NO_ACTION with buy_count and
    # manual_review_count both still 0, indistinguishable from a real quiet
    # day with clean data even though a Finance/Robinhood outage genuinely
    # happened (Codex review: "Surface provider-error runs before
    # zero-candidate exits").
    provider_error_tickers = [c["ticker"] for c in candidates if c["provider_error"]]
    # BUY-only rows in the raw (ticker, amount_usd, multiplier, drop_pct) shape
    # dca_runner.assess()/plan_orders() parse directly (dca_runner._parse_candidate_rows;
    # docs/live_wiring.md's candidates contract). `candidates` above uses different key
    # names for its fuller, debug-oriented rows and is not itself plan_orders-shaped, so
    # passing it straight through would abort those functions on a malformed-candidate
    # verdict instead of producing a plan (Codex review: "Publish planner-ready candidate
    # rows"). NO_ACTION/NO_DATA/MANUAL_REVIEW rows are excluded: they are not candidates
    # to place, and a $0 row would only earn plan_orders' REFUSED_INVALID_AMOUNT noise.
    # A calibrated ticker's row is additionally checked against dca_runner's OWN
    # validation (see _planner_incompatible) and, if it would be refused as
    # REFUSED_INCONSISTENT_MULTIPLIER there, held back into
    # planner_incompatible_tickers instead of silently shipping an unsafe row --
    # candidates above still has this ticker's full, correct decision either way.
    #
    # That validation reads dca_runner.CONFIG_DIR's sizing ladder, resolved
    # once at dca_runner's OWN import time -- it has no parameter to accept a
    # different config directory. When --config-dir points somewhere else,
    # buy_rows above were sized from a DIFFERENT ladder than the one
    # _planner_incompatible validates against, so its verdict cannot be
    # trusted either way; the same mismatch means dca_runner.plan_orders()/
    # execute_plan() downstream would ALSO size/validate these rows against
    # the wrong ladder if fed them directly. Publishing planner_candidates as
    # verified-compatible in that situation would be actively misleading, so
    # they are withheld and the mismatch is surfaced explicitly instead of
    # silently held back or silently approved (Codex review: "Use the
    # selected config for planner filtering").
    planner_config_mismatch = args.config_dir.resolve() != dca_runner.CONFIG_DIR.resolve()
    planner_candidates: List[List[Any]] = []
    planner_incompatible_tickers: List[str] = []
    if not planner_config_mismatch:
        for c in buy_rows:
            row = [c["ticker"], c["suggested_amount_usd"], c["buy_multiplier"], c["multiplier_drop_pct"]]
            if _planner_incompatible(row):
                planner_incompatible_tickers.append(c["ticker"])
            else:
                planner_candidates.append(row)
    # BUY-only {"ticker", "desired_usd", "current_price"} rows for
    # scripts/same_day_top_up_gate.py's --candidates input (that gate's own
    # --help documents this exact shape). current_price previously was omitted
    # deliberately, but that gate's evaluator treats a missing/non-positive
    # current_price as MISSING_CURRENT_PRICE and scores $0/manual review before
    # ever applying the top-up arithmetic -- so every row failed closed instead
    # of being top-up-gate ready, the opposite of the point of publishing this
    # field at all (Codex review: "Include current_price in top-up rows").
    # current_price uses this decision's own target_price as the best price
    # available here. That is still not a substitute for Gate 6.5's own
    # contract: the executor must recompute the decision price immediately
    # before placement (claude/dca-executor-prompts/live-mirror/*.txt's Gate
    # 6.5 step) and overwrite this field with a freshly re-fetched quote --
    # this value only makes the row schema-valid if a caller has nothing
    # fresher, not a stale price this script is asserting is still current.
    #
    # A ticker withheld from planner_candidates above -- either because
    # planner_config_mismatch made the whole compatibility check untrustworthy,
    # or because _planner_incompatible flagged its specific row -- is a BUY
    # Gate 6's plan_orders()/execute_plan() was explicitly unable to approve
    # for auto-placement. Building this list from every buy_rows entry
    # regardless published that same withheld ticker into Gate 6.5's own
    # advertised input unfiltered, so a caller that trusts this shape (rather
    # than separately cross-referencing planner_incompatible_tickers/
    # planner_config_mismatch first) could size and auto-place a top-up for
    # exactly the row Gate 6 refused (Codex review: "Withhold top-up rows
    # that Gate 6 cannot place"). Restricted to the same planner-approved
    # ticker set planner_candidates itself used -- a withheld ticker's full,
    # correct decision is still in candidates, same as for
    # planner_incompatible_tickers, so nothing is silently dropped, only kept
    # out of the two auto-placement-shaped outputs.
    planner_approved_tickers = {row[0] for row in planner_candidates}
    same_day_top_up_candidates = [
        {"ticker": c["ticker"], "desired_usd": c["suggested_amount_usd"], "current_price": c["target_price"]}
        for c in buy_rows
        if c["ticker"] in planner_approved_tickers
    ]
    document = {
        "slot": args.slot,
        "runner_slot_id": _RUNNER_SLOT_IDS[args.slot],
        "trading_day": args.trading_day,
        "kill_switch_engaged": kill_switch_engaged(resolved_config),
        "evaluated_count": len(candidates),
        "buy_count": len(buy_rows),
        "manual_review_count": len(manual_review_tickers),
        "manual_review_tickers": manual_review_tickers,
        "provider_error_count": len(provider_error_tickers),
        "provider_error_tickers": provider_error_tickers,
        "candidates": candidates,
        "planner_candidates": planner_candidates,
        "planner_incompatible_tickers": planner_incompatible_tickers,
        "planner_config_mismatch": planner_config_mismatch,
        "same_day_top_up_candidates": same_day_top_up_candidates,
        # Echoes the validated --already-covered-tickers input, for
        # traceability -- these tickers never reach `candidates` above at
        # all (see generate_candidates' own docstring), so this is the only
        # place in the document that shows Gate 4's compatible-stand-down
        # exclusion was actually applied this run.
        "already_covered_tickers": sorted(args.already_covered_tickers),
    }
    # allow_nan=False: belt-and-suspenders defense-in-depth alongside _finite_float
    # above -- a NaN/Infinity that reached this document from any future code path
    # would otherwise silently serialize as a bare, non-standard JSON token instead
    # of failing (Codex review: "Reject non-finite market prices").
    json.dump(document, sys.stdout, sort_keys=True, default=str, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
