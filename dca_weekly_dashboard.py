"""Weekly performance dashboard builder for the near-close graded DCA strategy.

Reads accumulated per-run artifacts (``run_state.json`` files) and, when
available, the per-run ``latest_run_summary.json`` files, then renders the full
set of Notion dashboard sections as markdown:

    - Performance snapshot
    - Graded DCA vs fixed daily DCA
    - Sector performance matrix
    - Budget and reserve effectiveness
    - Execution quality
    - Coverage and missed opportunities
    - Weekly review notes

Connector-agnostic and secret-free: the scheduled task feeds the loaded artifacts
in and forwards the rendered markdown to whatever Notion integration it owns.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from dca_fileio import atomic_write_text

# Placed-or-in-flight order statuses per
# scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES, minus
# "not_submitted_confirmation_required" (which never reached placement --
# it is pending a separate pre-authorization step, not real capital).
_REAL_ORDER_STATUSES = ("submitted", "filled", "pending", "unconfirmed")

# run_state ``generated_at`` is documented (schemas/run_state.schema.json) as an
# ISO-8601 timestamp in America/New_York, and the live writers emit it both
# naively ("2026-07-23T10:30:00") and offset-qualified depending on the path.
# Anchoring a naive value to ET rather than to the reader's local zone keeps the
# two forms mutually comparable.
_EASTERN = ZoneInfo("America/New_York")


def _has_usable_claim_breakdown(source: Optional[Dict[str, Any]]) -> bool:
    """Whether ``source`` carries a ``whole_share_claim_breakdown``
    :func:`_current_week_claim_amount` can actually use.

    A truthy-but-malformed breakdown (e.g. a non-empty string or dict, not
    the expected list of ``{"trading_day": ..., "amount_usd": ...}``
    entries) reads as "has a breakdown" to a bare truthiness check, so a
    candidate/outcome carrying one could reserve ``claim_sources``'
    ``setdefault()`` slot for that identity before the OTHER side's valid
    breakdown ever gets a chance -- silently disabling week attribution for
    a real claim (Codex review, PR #654: "Prefer usable claim breakdowns
    during reconciliation"). Matches the same ``isinstance(..., list) and
    ...`` usability check :func:`_current_week_claim_amount` itself applies.
    """
    if source is None:
        return False
    breakdown = source.get("whole_share_claim_breakdown")
    return isinstance(breakdown, list) and bool(breakdown)


def _current_week_claim_amount(
    source: Optional[Dict[str, Any]],
    aggregate_amount: float,
    week_key: Optional[Tuple[int, int]],
) -> float:
    """The portion of a whole-share amount attributable to ``week_key``.

    Applied to whole-share settlement ADJUSTMENTS and to ordinary recovered
    whole-share ORDERS alike (Codex review, PR #593: "Attribute recovered
    orders by their claim week") -- both shapes carry the same
    ``whole_share_claim_breakdown``, and both can surface in a later ISO week
    than the claim day they book against. Only the weekly attribution is
    split; a recovery is still a real physical order and still counts in
    ``counts.submitted`` and ``budget.day_notional_usd``, unlike an
    adjustment.

    A whole-share settlement adjustment is a dollar correction against a
    provisional order booked on an EARLIER trading day, and the day being
    corrected travels on the row as
    ``whole_share_claim_breakdown[].trading_day`` (written by near_close_dca's
    whole-share reconciliation paths) -- not as the artifact's own
    ``trading_day``, which is merely the day the correction was reconciled.
    ``load_json_files()`` uses this to keep a correction belonging to a week
    already past out of THIS week's running notional.

    The membership test this replaces was all-or-nothing: if ANY breakdown
    entry named the current week, the WHOLE aggregate landed in the current
    week. ``near_close_dca._merge_whole_share_submission_outcomes`` explicitly
    concatenates breakdowns, so one adjustment row can aggregate claims from
    several trading days -- and therefore several ISO weeks -- into a single
    summed ``submitted_amount_usd``. A $2 Friday correction merged with a $1
    Monday correction then reported $3 against Monday's week instead of $1
    (Codex review, PR #593: "Split aggregated adjustments by claim week").

    Apportion by each entry's own ``amount_usd`` instead. The bucketing rules
    deliberately mirror :func:`dca_risk.deployed_usd_by_claim_date`, which
    splits the very same field for the risk window, so the two never disagree
    about which day a dollar belongs to:

    * an entry whose ``trading_day`` is missing or unparseable is attributed
      to the artifact's own week, exactly like an ordinary candidate row;
    * an entry with a non-numeric ``amount_usd`` is skipped, so its dollars
      fall into the unaccounted remainder below rather than being guessed at;
    * whatever portion of the aggregate the breakdown does NOT account for --
      the two sums disagreeing is the normal case for a row that merged a
      reconciled claim with this artifact's own fresh contribution, which
      contributes no breakdown entry of its own -- is attributed to the
      artifact's own week.

    A source with no usable breakdown at all (and the no-week-key case) keeps
    the whole aggregate, so behavior is unchanged for every state that never
    carried one.

    ``week_key`` is the week the running weekly total this amount is about to
    move actually belongs to -- normally the artifact's own ISO week, but for
    an artifact that has none (neither ``iso_week`` nor a parseable
    ``trading_day``) the accumulator's current running week, which is the week
    the dollars would land in. Passing ``None`` for such a state instead let
    the whole aggregate through, so a dated claim naming an already-passed
    week was applied to the running week anyway -- the very attribution the
    dated form of the same row correctly declines (Codex review, PR #593:
    "Preserve claim-week attribution for undated states"). ``None`` remains
    the honest answer only when no week is known at all, on either side.
    """
    if source is None or week_key is None:
        return aggregate_amount
    breakdown = source.get("whole_share_claim_breakdown")
    if not isinstance(breakdown, list) or not breakdown:
        return aggregate_amount
    current_week_usd = 0.0
    accounted_usd = 0.0
    for item in breakdown:
        if not isinstance(item, dict):
            continue
        try:
            entry_amount = float(item.get("amount_usd") or 0.0)
        except (TypeError, ValueError):
            continue
        # A NaN/inf amount would poison the whole week's notional rather than
        # just its own entry; drop it into the remainder instead.
        if not math.isfinite(entry_amount):
            continue
        entry_week = week_key
        claim_day = item.get("trading_day")
        if isinstance(claim_day, str):
            try:
                iso = date.fromisoformat(claim_day[:10]).isocalendar()
            except ValueError:
                entry_week = week_key
            else:
                entry_week = (iso.year, iso.week)
        accounted_usd += entry_amount
        if entry_week == week_key:
            current_week_usd += entry_amount
    return current_week_usd + (aggregate_amount - accounted_usd)


def _is_manual_run_state(data: Dict[str, Any]) -> bool:
    """True if a run_state is a manual run per its run_classification block.

    The weekly Friday dashboard is scheduled-live only, so manual runs must be
    dropped before aggregation (run-classification contract). Defensive OR:
    either run_origin='manual' or manual_run=true marks it manual.
    """
    rc = data.get("run_classification") if isinstance(data, dict) else None
    if not isinstance(rc, dict):
        return False
    return rc.get("run_origin") == "manual" or rc.get("manual_run") is True


def load_scheduled_run_states(base: str | Path) -> List[Dict[str, Any]]:
    """Task 041d9d90 (weekly) entry point: load only SCHEDULED run_states.

    Globs ``<base>/scheduled_runs/**/run_state*.json`` — the ``manual_runs/``
    tree is never traversed — then routes the results through
    :func:`load_json_files`, which additionally drops any blocked stub or
    defensively manual-classified state. The Friday performance dashboard is
    scheduled-live only, so this keeps manual candidates/notional out of the
    cost-basis, reliability, and order-count metrics.
    """
    root = Path(base) / "scheduled_runs"
    paths = sorted(root.rglob("run_state*.json")) if root.exists() else []
    return load_json_files(paths)


# Each morning/midday/market_open/intraday executor writes its OWN cap keys
# (MorningDCAExecutor.budget_daily_key/budget_weekly_key, overridden per
# stream subclass -- see morning_dca.py, midday_dca.py, market_open_dca.py,
# intraday_dca.py) rather than the generic daily_cap_usd/weekly_cap_usd an
# ordinary afternoon run_state carries. Without checking these, a retained
# blocked/partial-submission artifact from one of those streams (see
# _has_submitted_evidence) rendered a real partial run with a $0.00 daily/
# weekly cap in the budget and performance tables (Codex review, PR #538:
# "Normalize morning and midday cap fields in retained runs").
_STREAM_DAILY_CAP_KEYS = (
    "morning_daily_cap_usd",
    "midday_daily_cap_usd",
    "market_open_daily_cap_usd",
    "intraday_daily_cap_usd",
)
_STREAM_WEEKLY_CAP_KEYS = (
    "morning_weekly_cap_usd",
    "midday_weekly_cap_usd",
    "market_open_weekly_cap_usd",
    "intraday_weekly_cap_usd",
)


def _first_present(mapping: Dict[str, Any], keys: tuple) -> Any:
    """First value in `mapping` for a key in `keys`, in order; None if none present."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _has_submitted_evidence(data: Dict[str, Any]) -> bool:
    """True when a run_state carries at least one real submitted order.

    Morning/midday's ``_blocked_live_result()`` can mark a run
    ``status: "blocked"`` (a later candidate's raw persistence failed) while
    still carrying a genuine earlier submission in ``buy_candidates``/
    ``broker_outcomes`` -- that submission must not disappear from weekly
    order/notional metrics just because the run as a whole is flagged blocked.
    ``"filled"``/``"pending"``/``"unconfirmed"`` count too: they are all
    placed-or-in-flight order statuses per
    ``scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES``, which already
    produce a canonical submitted_orders.csv row (Codex review: "Retain
    confirmed fills in blocked reporting artifacts" / "Retain pending and
    unconfirmed placement evidence"). ``"not_submitted_confirmation_
    required"`` is deliberately excluded: it never reached placement at all
    -- it is pending a separate pre-authorization step.

    An ``artifact_contract_failure`` (from
    ``near_close_dca._write_and_validate_scheduled_contract``) -- including one
    whose ``compute_only`` flag is true, e.g. a partial run that placed one
    candidate but left another positive-planned candidate unplaced -- does NOT
    by itself disqualify a row that genuinely reached the broker.
    ``compute_only`` describes whether the run is valid COMPLETION evidence
    (``run_classification.valid_scheduled_evidence`` is denied for it
    separately), not whether any individual row's placement is trustworthy: a
    real order id on the placed row is real capital deployment regardless of
    what a DIFFERENT candidate in the same run failed to do. Disqualifying the
    whole artifact here dropped that real order from the weekly dashboard and
    monthly cost tracker entirely (Codex review, PR #538: "Retain submitted
    rows in compute-only partial failures" -- the same principle the earlier
    "Retain real orders when only the artifact contract failed" finding
    established for a companion-artifact-only failure, extended to the
    partial-placement compute-only case).
    """
    buy_candidates = data.get("buy_candidates")
    if isinstance(buy_candidates, list) and any(
        isinstance(row, dict) and row.get("status") in _REAL_ORDER_STATUSES for row in buy_candidates
    ):
        return True
    broker_outcomes = data.get("broker_outcomes")
    if isinstance(broker_outcomes, dict) and any(
        isinstance(outcome, dict) and outcome.get("status") in _REAL_ORDER_STATUSES
        for outcome in broker_outcomes.values()
    ):
        return True
    return False


def _generated_at_sort_value(data: Dict[str, Any]) -> Optional[float]:
    """``generated_at`` as a POSIX timestamp, or ``None`` when unusable.

    The attempt-level tiebreaker for :func:`_accumulation_sort_key`. A naive
    value is anchored to ET (see ``_EASTERN``) so it compares correctly against
    an offset-qualified one; anything missing, non-string, or unparseable
    yields ``None`` and the caller sorts that state last within its own day.
    """
    raw = data.get("generated_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_EASTERN)
    return parsed.timestamp()


def _accumulation_sort_key(data: Dict[str, Any]) -> Tuple[int, str, int, float]:
    """Chronological sort key for ``load_json_files()``'s running weekly-notional
    accumulation pass (Codex review, PR #593: "Sort states before accumulating
    weekly notional").

    Derived primarily from the SAME field the ISO-week reset already keys off
    -- ``"trading_day"``, an ISO ``"YYYY-MM-DD"`` string, which compares
    correctly as a plain string. ``"iso_week"`` is never used for a state that
    HAS a usable ``trading_day``: it is week-granular, so it cannot order two
    states inside one week, and mixing the two granularities there would order
    some pairs by week and others by day.

    ``"trading_day"`` alone is DAY-granular, so every state from one trading
    day compared equal and the stable sort simply preserved caller order --
    which is the very thing this key exists to stop relying on. A later state
    already reporting $100 week-to-date, handed in before that same day's
    earlier retained $5 partial, still normalized the partial to $105 (Codex
    review, PR #593: "Include intra-day ordering in the accumulation key").
    ``"generated_at"`` -- the required, schema-documented per-artifact
    timestamp (see :func:`_generated_at_sort_value`) -- is therefore the
    secondary key, so same-day retries and recovery artifacts accumulate in
    the order they were actually produced.

    A state with no usable ``trading_day`` but a valid ``"iso_week"`` -- a
    retained legacy shape :func:`_iso_week_of_state` explicitly accepts -- is
    NOT undated: its week IS known, and tailing it after every dated state
    sorted an OLDER week's state after the current week's, so the accumulator
    reset back to the older week last and :func:`_latest_week_value` then
    selected that older week's cap and notional as "latest" (Codex review, PR
    #593: "Keep ISO-week-only states in chronological week order"). Such a
    state is therefore placed on the timeline at its week's MONDAY
    (``date.fromisocalendar(year, week, 1)``) and, within that week, sorts
    BEFORE every dated state of the same week.

    Monday-and-first is a deliberate choice, not an approximation of the
    truth: a week-granular value simply cannot be ordered within its week, so
    the only question is which end is safe. Both figures this ordering feeds
    are CUMULATIVE week-to-date, so a same-week dated state's value already
    subsumes anything an earlier state of that week contributed. Sorting the
    week-only state first therefore lets the running accumulator finish on --
    and lets ``_latest_week_value`` select -- real dated evidence, whereas
    imputing the week's END would let a week-granular state of unknown
    position override a state whose place in the week is actually known. It
    also keeps the week-only state ahead of the NEXT week's states, which is
    the ordering the finding is about.

    The documented rule for a genuinely undated state -- no usable
    ``trading_day`` AND no usable ``iso_week`` -- is unchanged: it sorts AFTER
    every dated state, keeping its input-order position relative to the other
    undated ones (``sorted`` is stable). Such a state cannot be placed on the
    timeline at all, so the honest choice is to keep it from wedging itself
    BETWEEN two dated states and disturbing their true chronological order --
    and it must never be dropped, because it can still carry real submitted
    evidence the rest of this normalization has to see.

    The same rule extends, one level down, to a state with a usable
    ``trading_day`` but no usable ``generated_at``: it sorts after every
    timestamped state OF ITS OWN DAY, stably among the other untimestamped
    ones, and never leaves that day. An unplaceable attempt must not wedge
    itself between two attempts whose true order IS known, and an undated
    state is given no secondary ordering at all -- there is no day to order
    it within, so the existing stable-and-last rule stands unchanged.
    """
    trading_day = data.get("trading_day")
    if isinstance(trading_day, str):
        try:
            date.fromisoformat(trading_day)
        except ValueError:
            pass
        else:
            generated_at = _generated_at_sort_value(data)
            if generated_at is None:
                return (0, trading_day, 1, 0.0)
            return (0, trading_day, 0, generated_at)
    # No usable trading_day -- but the week may still be known. The third
    # slot's NEGATIVE ranks are what place a week-only state before every
    # dated state of the same imputed Monday (dated states use 0 for
    # timestamped and 1 for untimestamped); -2/-1 keep the same
    # timestamped-before-untimestamped order among the week-only states
    # themselves, so an unplaceable attempt still never wedges itself between
    # two whose true order IS known.
    week = _iso_week_of_state(data)
    if week is not None:
        try:
            week_monday = date.fromisocalendar(week[0], week[1], 1).isoformat()
        except (ValueError, OverflowError):
            # Defence in depth only: _iso_week_of_state() now validates a
            # two-int iso_week with this same call before returning it, so a
            # malformed value (week 60, year 0, ...) never reaches here --
            # it has already fallen back to trading_day or None upstream.
            # This used to be the ONLY such check, which is precisely how the
            # sort key and the accumulator came to disagree about the same
            # field. Kept so a future caller-side change cannot silently
            # reintroduce a crash on a fictitious week; unreachable today.
            week_monday = None
        if week_monday is not None:
            generated_at = _generated_at_sort_value(data)
            if generated_at is None:
                return (0, week_monday, -1, 0.0)
            return (0, week_monday, -2, generated_at)
    return (1, "", 1, 0.0)


def load_json_files(paths: Sequence[str | Path]) -> List[Dict[str, Any]]:
    """Load artifacts, skipping the minimal ``status: "blocked"`` run_state stubs
    written for date-gated days — they are not run days and would otherwise be
    counted into the weekly snapshot/table denominators — and skipping any
    manual run_states so manual candidates/notional never enter the
    scheduled-live weekly cost basis, reliability, or order-count metrics.

    A blocked run that nonetheless carries a real submitted order (a later
    candidate's raw persistence failed after an earlier one was placed) is
    NOT skipped -- see :func:`_has_submitted_evidence`. This holds even when
    the artifact also carries an ``artifact_contract_failure`` marker: that
    marker can be stamped for a companion-file reason (a missing/mismatched
    summary, CSV, or pointer) unrelated to whether the submitted evidence
    itself is real, and ``dca_risk._load_states()`` already keeps such
    contract-rejected states for the same reason -- real submitted capital
    must still count toward reporting even when the run as a whole failed
    the scheduled-artifact contract.
    """
    loaded = [json.loads(Path(p).read_bytes()) for p in paths]
    kept = [
        data
        for data in loaded
        if (data.get("status") != "blocked" or _has_submitted_evidence(data))
        and not _is_manual_run_state(data)
    ]
    # A structurally rejected blocked artifact admitted above for its real
    # broker submission evidence can carry a list-/object-valued
    # trading_day (JSON has no schema to stop it) -- build_coverage_missed()
    # later uses this same field as a dict lookup key
    # (summary_by_day.get(day)) and raises TypeError: unhashable type,
    # aborting the entire weekly report despite the valid evidence just
    # retained. Sanitized once here, for every consumer downstream, rather
    # than at each dict-key use site (Codex review, PR #654: "Sanitize
    # trading days on newly retained blocked states").
    for data in kept:
        trading_day = data.get("trading_day")
        if not isinstance(trading_day, str):
            data["trading_day"] = None
        else:
            # A type-valid but unparsable string (""; "2025-not-a-date")
            # survives the isinstance check above, and both this module's
            # own pre-change base-amount fallback and
            # _require_base_amount() compare trading_day LEXICALLY against
            # _BASE_AMOUNT_CHANGE_DATE ("YYYY-MM-DD" strings sort
            # correctly as plain strings, but only when the value actually
            # IS one) -- an unparsable string that happens to sort below
            # that date silently got treated as known pre-change history
            # and assigned the $3 historical base, corrupting the
            # fixed-DCA comparison instead of being skipped as the
            # unsupported row it is (Codex review, PR #654: "Validate
            # retained trading-day strings before historical fallback").
            try:
                date.fromisoformat(trading_day)
            except ValueError:
                data["trading_day"] = None
        # Same unhashable/malformed-container hazard as buy_candidates
        # itself: a truthy non-list manual_review_tickers (e.g. a bare
        # integer) reaches build_coverage_missed()'s/
        # build_weekly_review_notes()'s unconditional len() call and
        # raises TypeError, aborting the entire weekly report; a string
        # would silently pass len() but report its CHARACTER count as the
        # number of manual-review flags, which is equally wrong (Codex
        # review, PR #654: "Normalize manual-review containers on
        # retained states").
        if not isinstance(data.get("manual_review_tickers"), list):
            data["manual_review_tickers"] = []
    # Tracks the highest true week-to-date notional seen so far while walking
    # the retained states in order -- used below as the "prior cumulative
    # weekly total" a retained partial day's own newly-derived spend gets
    # ADDED to, rather than compared against in isolation (Codex review, PR
    # #593 discussion_r3837437494: "Add retained partial spend to the prior
    # weekly total").
    #
    # Codex review, PR #593: "Sort states before accumulating weekly
    # notional". This accumulator (and the ISO-week reset just below it) is
    # stateful and ORDER-DEPENDENT, but only load_scheduled_run_states()
    # feeds it a chronologically sorted path glob -- load_json_files() is a
    # public entry point that accepts arbitrary paths in arbitrary order. A
    # caller passing, say, Wednesday's completed state (already reporting
    # $100 week-to-date) BEFORE Monday's retained $5 submission normalized
    # Monday to $105 and overstated the week; out-of-order week boundaries
    # could likewise reset the accumulator and then rebuild it from the
    # wrong side of the boundary.
    #
    # Walk a chronologically sorted WORKING COPY rather than reordering
    # `kept` itself: the returned list's order is part of this function's
    # contract (build_graded_vs_fixed/build_budget_reserve_effectiveness/
    # build_coverage_missed render one table row per state in the order they
    # are handed, and load_scheduled_run_states' callers already rely on
    # that being the sorted-path order). The loop only MUTATES the state
    # dicts in place, so sorting a shallow copy normalizes the accumulation
    # order while every caller still sees exactly the list it would have
    # before.
    running_week_notional_usd = 0.0
    running_week_key: Optional[Tuple[int, int]] = None
    for data in sorted(kept, key=_accumulation_sort_key):
        # Set (below) to this state's corrected cumulative weekly total
        # whenever a settlement adjustment LOWERED (or raised) it, so the
        # accumulator update at the bottom of this iteration can carry that
        # correction forward instead of max()-ing it away. None means "no
        # correction applied -- use the ordinary raise-only max()".
        corrected_week_notional_usd: Optional[float] = None
        # Codex review, PR #593 discussion_r3840206566: "Reset accumulated
        # notional at ISO-week boundaries" -- load_scheduled_run_states()
        # recursively loads the COMPLETE historical scheduled_runs/** tree,
        # so `kept` can span many weeks, not just the current one. Without
        # resetting here, a prior week's total (e.g. $100) bleeds into the
        # next week's own retained partial state (e.g. a $5 blocked
        # submission), reporting $105 for a week that only ever saw $5.
        # Reset whenever the state's own ISO week differs from the running
        # tracker's -- a state with no determinable week (neither a valid
        # "iso_week" field nor a parseable "trading_day") leaves the tracker
        # untouched rather than guessing.
        week_key = _iso_week_of_state(data)
        if week_key is not None:
            if running_week_key is not None and week_key != running_week_key:
                running_week_notional_usd = 0.0
            running_week_key = week_key
        # Codex review, PR #593: "Preserve claim-week attribution for undated
        # states". The whole-share claim split below asks "does this claim
        # belong to the week whose total I am about to move?" -- and for an
        # UNDATED state the answer is not "unknown" just because the artifact
        # itself carries no week. The state contributes to (and is reported
        # under) whatever week the accumulator is currently in, which the
        # block above deliberately leaves untouched for it, so that running
        # week is the one a dated claim must be measured against. Handing the
        # split a bare None instead let the WHOLE aggregate through: a $100
        # current-week total followed by an undated +$2 adjustment whose
        # breakdown names the prior Friday normalized to $102 for the current
        # week, even though the dated version of that same adjustment is
        # correctly excluded. Still None when no week is known on either side
        # -- then nothing can be compared and the aggregate keeps applying, as
        # it always did.
        claim_week_key = week_key if week_key is not None else running_week_key
        if data.get("status") == "blocked":
            # A retained partial run can carry later blocked/skipped/
            # unevaluated candidates alongside the real submission that
            # earned it a place here. Deleting those non-placed rows here
            # (the original fix) also deleted them from
            # build_performance_snapshot()'s "Planned notional"/"Buy
            # candidates" and build_sector_matrix()'s planned-coverage view
            # -- both of which WANT every candidate, placed or not, to show
            # a missed opportunity like B alongside a placed A (Codex
            # review, PR #654: "Preserve unplaced candidates in retained
            # partial runs"). Only build_graded_vs_fixed()'s graded/fixed
            # comparison is actually about EXECUTED capital and must still
            # exclude them -- it does so directly (see its own status
            # check) rather than via a deletion here that would also starve
            # the planned-coverage consumers. The reconciliation loop just
            # below (submission totals: amounts_by_ticker/counted_
            # identities/counts.submitted) also still filters to real-order-
            # status rows on its own, via `_candidate_rows`.
            candidates = data.get("buy_candidates")
            if isinstance(candidates, list):
                # A non-dict element (e.g. a bare `1`) can appear in an
                # otherwise-list buy_candidates from a structurally
                # rejected artifact -- every downstream dashboard builder
                # calls `.get()` on each row unconditionally, so it must be
                # dropped here rather than merely left for the FINAL
                # existing_candidates + synthetic_candidate_rows
                # concatenation to filter (that branch only runs when
                # there's outcome-only evidence to synthesize a row for;
                # this path must also protect the "no synthetic rows
                # needed" case) (Codex review, PR #654: "Remove malformed
                # rows from retained candidate lists").
                candidates = [row for row in candidates if isinstance(row, dict)]
                # Every retained row, not just the real-order-status ones
                # the reconciliation loop below (via `_candidate_rows`)
                # sanitizes on its own -- an UNPLACED candidate never
                # enters that loop at all now that this normalization
                # preserves it (see the comment above), so its own
                # `ticker`/`planned_amount_usd` were never validated.
                # `build_sector_matrix()` calls `sector_map.get(ticker)`
                # unconditionally on every retained row and raises
                # TypeError for an unhashable ticker (Codex review, PR
                # #654: "Sanitize unplaced candidate tickers too"); `_num()`
                # -- used by build_performance_snapshot()'s "Planned
                # notional" and build_sector_matrix()'s own notional sum --
                # has no finite check of its own and would happily
                # propagate a non-finite planned_amount_usd as $nan/$inf
                # when no finite submitted/outcome amount is available to
                # replace it (Codex review, PR #654: "Sanitize retained
                # planned amounts before aggregation").
                for row in candidates:
                    if not isinstance(row.get("ticker"), str):
                        row["ticker"] = None
                    # schemas/run_state.schema.json requires planned_amount_usd
                    # to be nonnegative for an ordinary order -- _finite_float()
                    # accepts a negative value because it IS finite, so a
                    # structurally rejected artifact with e.g.
                    # planned_amount_usd=-5 on an ordinary submitted row
                    # survived retention untouched and reported
                    # "Planned notional | $-5.00" / "Executed notional |
                    # $-5.00", reducing rather than growing the dashboard's
                    # financial totals for a real submission (Codex review,
                    # PR #654: "Reject negative ordinary-order amounts
                    # during retention"). A whole_share_settlement_adjustment
                    # row is exempt -- its dollar delta is a real bookkeeping
                    # correction and is legitimately negative.
                    _is_adjustment = bool(row.get("whole_share_settlement_adjustment"))
                    _planned = _finite_float(row.get("planned_amount_usd"))
                    if _planned is not None and _planned < 0 and not _is_adjustment:
                        _planned = None
                    if _planned is None:
                        _submitted = _finite_float(row.get("submitted_amount_usd"))
                        if _submitted is not None and _submitted < 0 and not _is_adjustment:
                            _submitted = None
                        row["planned_amount_usd"] = _submitted
                data["buy_candidates"] = candidates
                # build_performance_snapshot() reads counts.buy_candidates
                # (the stored count), not len(buy_candidates) -- recomputed
                # from the FULL retained list (not just placed rows) so
                # "Buy candidates" reflects true coverage, not just
                # submissions.
                counts = data.get("counts")
                if not isinstance(counts, dict):
                    counts = {}
                    data["counts"] = counts
                # Codex review, PR #593 discussion_r3840229906 ("Exclude
                # settlement adjustments from normalized order counts"): a
                # whole_share_settlement_adjustment row is a bookkeeping-only
                # dollar correction for an order ALREADY counted elsewhere
                # (near_close_dca.py applies this same exclusion to its own
                # counts["buy_candidates"]/counts["submitted"]), not a fresh
                # distinct placement -- it must not inflate this retained
                # run's candidate count even though it is a real, retained
                # row.
                counts["buy_candidates"] = sum(
                    1 for r in candidates if isinstance(r, dict) and not r.get("whole_share_settlement_adjustment")
                )
            # build_performance_snapshot() also reads counts.submitted and
            # budget.day_notional_usd directly off the artifact -- a
            # pending/unconfirmed-only retained state, or one whose sole
            # evidence lives in broker_outcomes rather than buy_candidates,
            # can leave those fields at zero/absent even though
            # _REAL_ORDER_STATUSES now deliberately keeps that evidence
            # (Codex review: "Recompute submitted metrics for retained
            # partial runs"). Derive both directly from the retained
            # candidate/outcome evidence, deduped by ticker (a strategy
            # places at most one order per ticker per stream per day).
            amounts_by_ticker: Dict[str, float] = {}
            # Identities (tickers, or a composite outcome key -- see the
            # broker_outcomes loop below) with at least one NON-adjustment
            # placed row/outcome -- i.e. a fresh distinct order, not just a
            # whole_share_settlement_adjustment correction. counts["submitted"]
            # is derived from this set below, not from amounts_by_ticker's own
            # keys, so a settlement-only identity contributes its dollar delta
            # to day/week notional (via amounts_by_ticker) without being
            # double-counted as an extra submitted order (Codex review, PR #593
            # discussion_r3840229906: "Exclude settlement adjustments from
            # normalized order counts").
            counted_identities: set = set()
            # Two placed/in-flight buy_candidates rows can share the same
            # ticker -- a same-day retry's second order, retained alongside
            # the first (Codex review, PR #593 discussion_r3840290875:
            # "Preserve amounts for multiple same-ticker candidates"). Keying
            # amounts_by_ticker by the bare ticker for every row let the
            # second row's amount silently overwrite the first's, losing that
            # candidate's notional entirely whenever neither had a matching
            # broker_outcomes entry to re-derive it (e.g. both outcomes omit
            # submitted_amount_usd). Mirror the exact identity scheme
            # broker_outcomes already uses below: the first row seen for a
            # ticker keeps the bare ticker as its identity (so it still
            # reconciles with a same-keyed broker_outcomes entry), and any
            # later row for that same ticker gets a composite
            # "<ticker>:<order_id>" identity -- matching the composite keys
            # submitted_order_rows()/the afternoon merge already write for a
            # retry's extra order -- so both amounts survive and both sum
            # into day/week notional instead of the second clobbering the
            # first.
            seen_tickers: set = set()
            # Codex review, PR #593 discussion_r3840429731: "Keep id-less
            # settlement adjustments as separate amounts". The composite
            # identity above requires an order_id, but a retained artifact can
            # legitimately carry BOTH an id-less base row and an id-less
            # whole_share_settlement_adjustment row for the SAME ticker -- the
            # exact shape near_close_dca.load_prior_same_day_allocations()'s
            # id-less fallback (its `not _cand_oid` ticker-level branch)
            # explicitly supports. Without a distinct identity the adjustment
            # row fell back to the bare ticker and OVERWROTE the base row's
            # amount, so a $100 provisional row followed by a -$2 settlement
            # correction reported day_notional_usd = -2 instead of $98.
            #
            # Two parts to the fix. First, non-adjustment rows are processed
            # BEFORE adjustment rows, so a real order always claims the bare
            # ticker identity (which is what reconciles with a same-keyed
            # broker_outcomes entry below) regardless of the order the rows
            # happen to sit in the artifact -- a settlement row physically
            # written first can no longer take that identity out from under
            # the order it corrects. Second, an id-less adjustment row for an
            # already-seen ticker gets its own synthetic
            # "<ticker>:whole_share_settlement_adjustment#<n>" identity, so
            # its (possibly negative) delta SUMS with the base amount instead
            # of clobbering it. That identity is never added to
            # counted_identities (the `whole_share_settlement_adjustment`
            # check at the bottom of this loop), so it still cannot inflate
            # counts.submitted/counts.buy_candidates (Codex review, PR #593
            # discussion_r3840229906), and it lands in the
            # settlement_adjustment_total half of the day/week split below
            # rather than the order_amounts_total half.
            #
            # An adjustment row that is the ONLY row for its ticker still
            # keeps the bare ticker identity, preserving reconciliation with a
            # matching adjustment outcome in broker_outcomes (which is keyed
            # by that same bare ticker) instead of double-counting the delta.
            # Not `data.get("buy_candidates") or []`: that only substitutes
            # for an absent/falsy value -- a truthy non-iterable
            # buy_candidates (e.g. an integer, from a structurally rejected
            # artifact this path deliberately admits for its broker_
            # outcomes evidence) survives it and raises TypeError on
            # `for row in ...`, aborting the whole weekly report before
            # the later synthetic-row append site's own container guard
            # ever runs (Codex review, PR #654: "Coerce candidate
            # containers before normalization").
            _raw_candidates = data.get("buy_candidates")
            if not isinstance(_raw_candidates, list):
                _raw_candidates = []
            # Now that data["buy_candidates"] retains every row (placed or
            # not -- see the comment above), this reconciliation pass must
            # filter to real-order-status rows itself: it feeds
            # amounts_by_ticker/counted_identities/counts.submitted (the
            # SUBMISSION totals), which a non-placed row must not inflate,
            # exactly the exclusion the deleted-at-load filter used to
            # provide (Codex review, PR #654: "Preserve unplaced candidates
            # in retained partial runs").
            _candidate_rows = [
                row for row in _raw_candidates if isinstance(row, dict) and row.get("status") in _REAL_ORDER_STATUSES
            ]
            # Every retained dict row, placed or not -- used below only to
            # recognize a row a broker_outcomes entry already has, never to
            # feed submission totals (those stay derived from _candidate_rows
            # alone). A candidate whose status is missing or still
            # non-placed but whose matching broker outcome proves submission
            # (a statusless row awaiting its status update, or one whose
            # status lags the outcome) is invisible to _candidate_rows'
            # real-order-status filter, so the outcome pass below couldn't
            # see it already had a representative row and materialized a
            # second synthetic candidate for the same order (Codex review,
            # PR #654: "Reconcile outcome-backed candidates despite stale
            # statuses").
            _all_dict_candidate_rows = [row for row in _raw_candidates if isinstance(row, dict)]
            # Sanitize ticker/order_id on EVERY retained row here, before
            # anything below reads them -- not just the real-order-status
            # rows the loop further down sanitizes as it processes them. An
            # unplaced or statusless row never reaches that loop (it only
            # iterates `_candidate_rows`), but its raw order_id/ticker is
            # still read by the `existing_order_ids`/`existing_tickers` set
            # comprehensions a few lines below, now that those draw on
            # `_all_dict_candidate_rows` -- a truthy but unhashable order_id
            # (e.g. a JSON array) on such a row raised TypeError there,
            # aborting the report before the valid broker evidence that
            # triggered admission could ever be shown (Codex review, PR
            # #654: "Sanitize order IDs on unplaced candidate rows").
            for _row in _all_dict_candidate_rows:
                if not isinstance(_row.get("ticker"), str):
                    _row["ticker"] = None
                if not isinstance(_row.get("order_id"), str):
                    _row["order_id"] = None
            _ordered_candidate_rows = [
                row for row in _candidate_rows if not row.get("whole_share_settlement_adjustment")
            ] + [row for row in _candidate_rows if row.get("whole_share_settlement_adjustment")]
            settlement_sequence: Dict[Any, int] = {}
            # Codex review, PR #593: "Assign unique identities to repeated
            # id-less candidates". The composite "<ticker>:<order_id>"
            # identity above is only minted when the repeated row HAS an
            # order_id, and the settlement branch only when it is an
            # adjustment -- so two placed rows for the SAME ticker that are
            # both id-less and both ordinary orders (the shape a
            # pending/unconfirmed same-day retry legitimately produces before
            # either order id is known) both fell through to the bare ticker.
            # The second amount then OVERWROTE the first in
            # amounts_by_ticker, and counted_identities gained one member
            # instead of two, so counts.submitted reported one order and
            # day/week notional lost the other row's dollars entirely.
            # Mirrors settlement_sequence exactly, with its own namespace so
            # a synthetic order identity can never collide with a synthetic
            # adjustment identity for the same ticker.
            candidate_sequence: Dict[Any, int] = {}
            # Identities the candidate pass assigned to a
            # whole_share_settlement_adjustment row, and the first such
            # identity per ticker. Both feed the broker_outcomes pass's own
            # adjustment split below (Codex review, PR #593: "Separate
            # adjustment outcomes from the base order identity") -- one to
            # recognize an identity that is ALREADY an adjustment's (so a
            # matching outcome reconciles onto it rather than being counted
            # twice), the other to find the identity to reconcile onto.
            adjustment_identities: set = set()
            adjustment_identity_by_ticker: Dict[Any, Any] = {}
            # Codex review, PR #593: "Reconcile synthetic recovery outcomes by
            # order ID". Identity is assigned from the MAPPING KEY for
            # broker_outcomes but from the TICKER for buy_candidates, and those
            # two disagree for an inverse-Martingale recovery: near_close_dca
            # stores the recovered order under a synthetic
            # "<ticker>#imart-recovery" key (so it cannot collide with this
            # run's own ticker-keyed outcome) while _build_buy_candidates emits
            # the very same order as an ordinary candidate row carrying the
            # REAL ticker and the REAL order id. One $5 recovery therefore
            # aggregated as TWO identities -- "AAA" and "AAA#imart-recovery" --
            # i.e. two submitted orders and $10 of day/week notional for a
            # single $5 placement. The synthetic-row guard added earlier only
            # suppressed the duplicate CANDIDATE ROW; the duplicate entries in
            # amounts_by_ticker/counted_identities survived it untouched.
            #
            # Record which identity each candidate order id already claimed so
            # the broker_outcomes loop below can fold a matching outcome into
            # that SAME identity instead of minting a second one. Only
            # non-adjustment rows are recorded, and only the first row per id
            # wins: a whole_share_settlement_adjustment row deliberately gets
            # its own distinct identity so its (possibly negative) delta sums
            # with the order it corrects rather than clobbering it, and folding
            # by order id must never undo that split.
            candidate_identity_by_order_id: Dict[Any, Any] = {}
            # The exact inverse of candidate_identity_by_order_id, and the
            # reason it cannot simply be read backwards: "which order id has
            # this identity already been claimed by?" (Codex review, PR #593:
            # "Match ticker-only outcomes only to candidates without IDs").
            # A broker outcome keyed by a bare ticker whose candidate row
            # already carries a DIFFERENT order id is not that row's outcome
            # -- the two ids PROVE they are separate orders -- so it must not
            # reuse the bare-ticker identity and overwrite that row's amount.
            # Populated in lockstep with candidate_identity_by_order_id
            # (non-adjustment rows only, first id per identity wins) so a
            # settlement adjustment's deliberately distinct identity is
            # never treated as an order id conflict.
            candidate_order_id_by_identity: Dict[Any, Any] = {}
            # Codex review, PR #593: "Match outcomes to repeated id-less
            # candidates". The mirror image of candidate_sequence above, and
            # the case that sequencing repeated id-less rows created. A
            # blocked retry can retain a candidate row for AAA with order id
            # A alongside a later pending/unconfirmed row with NO id, whose
            # broker outcome carries id B under a composite key. Nothing tied
            # the two together: the id-less row now holds the SEQUENCED
            # identity "AAA:candidate#1" rather than the bare ticker, so the
            # synthetic-row fallback (which only fires for identity == ticker)
            # could not suppress B's synthetic row, and B was aggregated as a
            # third identity while the id-less row kept its own -- a $5 order
            # plus a $7 order reported THREE submissions and $19 instead of
            # two and $12.
            #
            # Track every id-less non-adjustment candidate identity per
            # ticker, in row order, and let the outcome pass CONSUME one when
            # an id-bearing outcome for that ticker matches nothing else. Only
            # ordinary orders join the pool: an id-less settlement adjustment
            # deliberately keeps its own identity so its delta sums with the
            # order it corrects, and it already reconciles with a matching
            # adjustment outcome through adjustment_identity_by_ticker.
            id_less_candidate_identities: Dict[Any, List[Any]] = {}
            # The identity an EARLIER broker_outcomes entry in this same
            # pass resolved to for a given order_id, so a LATER entry
            # describing the same physical order under a different mapping
            # key (a synthetic recovery key like "Y#recovery" alongside the
            # bare-ticker key "Y", both carrying order_id "B", with no
            # buy_candidates row at all) folds onto it instead of getting
            # its own fresh identity -- every candidate-matching fold above
            # only ever looks for the id among CANDIDATE identities
            # (candidate_identity_by_order_id), which stays empty when
            # there is no candidate row, so two outcomes for the same order
            # previously summed as two submissions instead of one (Codex
            # review, PR #654: "Deduplicate outcome-only submissions by
            # order ID"). Mirrors _submitted_keys()'s own order-id
            # deduplication in morning_dca.py.
            outcome_identity_by_order_id: Dict[Any, Any] = {}
            # Codex review, PR #593: "Backfill matched candidates from outcome
            # amounts". The candidate ROW behind each identity, so the
            # broker_outcomes pass can write an amount back onto it. A matched
            # candidate row SUPPRESSES the synthetic row that would otherwise
            # have carried this order's notional into build_graded_vs_fixed()/
            # build_sector_matrix()/the planned-notional roll-up -- all of
            # which read planned_amount_usd off the rows themselves -- so when
            # the row carries neither amount field and only the matching
            # outcome has one (an id-less pending candidate matched to an
            # ID-bearing $7 outcome), updating amounts_by_ticker alone left the
            # snapshot reporting $7 executed against $0 planned, graded and
            # sector spend.
            candidate_row_by_identity: Dict[Any, Dict[str, Any]] = {}
            # Codex review, PR #593: "Attribute settlement deltas to the
            # claim's ISO week". A settlement adjustment reconciled in a LATER
            # week than the provisional order it corrects -- a Friday order
            # given a +$2 adjustment on the following Monday -- carries the
            # corrected day in whole_share_claim_breakdown[].trading_day, while
            # this normalization keys everything off the ARTIFACT's own
            # trading_day. The delta was therefore added to Monday's
            # week_notional_usd, making the new week over-report deployed
            # capital that belongs to the previous one.
            #
            # The running accumulator walks states in order and zeroes at each
            # week boundary, and build_performance_snapshot() reduces
            # week_notional_usd with a max() across states -- so a week already
            # walked past cannot be re-opened and rewritten from here, and
            # there is no later state in the OLD week for the delta to land on.
            # The correct behavior is therefore exclusion, not retroactive
            # rewriting: a delta belonging to an already-passed week is simply
            # not applicable to the current week's running total, so it is
            # dropped from the weekly figure entirely. The prior week's own
            # artifacts already reported that week's total, and this
            # correction's cash movement is still visible on the day it
            # settled via day_notional_usd -- only the WEEKLY attribution
            # changes.
            #
            # A same-week adjustment (including the same-day case) is
            # unaffected, and so is any row whose claim week cannot be
            # determined -- those keep applying to the artifact's own week
            # exactly as before.
            #
            # Codex review, PR #593: "Split aggregated adjustments by claim
            # week" -- a follow-up on the exclusion above. That exclusion was
            # all-or-nothing per identity: one adjustment row can aggregate
            # claims from SEVERAL trading days (see
            # near_close_dca._merge_whole_share_submission_outcomes, which
            # concatenates breakdowns) into a single summed
            # submitted_amount_usd, and if ANY breakdown entry named the
            # current week the ENTIRE aggregate -- prior-week dollars
            # included -- landed in it. Record the row/outcome that carries
            # the breakdown instead of a bare "prior week" flag, so the
            # weekly total below can apportion the aggregate by claim week
            # via _current_week_claim_amount() and take only this week's
            # share. The first source seen for an identity wins: the
            # candidate pass and the broker_outcomes pass describe the SAME
            # physical order, so their breakdowns are duplicates rather
            # than additional claims, and summing both would double-count.
            #
            # Codex review, PR #593: "Attribute recovered orders by their
            # claim week". This map used to be collected for settlement
            # ADJUSTMENT identities only, but an ordinary, non-adjustment
            # whole-share RECOVERY carries the very same
            # whole_share_claim_breakdown naming its original claim day --
            # see near_close_dca._book_reconciled_whole_share_order(), whose
            # live_order_found and settled branches both stamp
            # [{"trading_day": claim_trading_day_iso, ...}] onto a plain
            # "submitted" outcome. A Friday claim first surfaced on Monday
            # therefore dropped its whole amount into MONDAY's
            # week_notional_usd even though risk accounting (dca_risk's
            # deployed_usd_by_claim_date, which splits this same field)
            # books it to Friday. Collect the breakdown source for EVERY
            # identity that carries one, adjustment or not, so the weekly
            # split below applies uniformly.
            #
            # Deliberately NOT a change to the physical-order bookkeeping: a
            # recovery is a real broker order, unlike an adjustment, so it
            # still joins counted_identities (counts.submitted) and still
            # contributes its full amount to day_notional_usd. Only the
            # WEEKLY attribution is apportioned.
            claim_sources: Dict[Any, Dict[str, Any]] = {}
            # A duplicate candidate row folded onto an existing order_id's
            # identity (see the fold branch below) still physically sits in
            # data["buy_candidates"] alongside the row it duplicates --
            # folding its aggregation IDENTITY alone doesn't stop a
            # downstream consumer that iterates buy_candidates rows
            # DIRECTLY (_this_run_submitted_notional(),
            # build_graded_vs_fixed(), build_sector_matrix() all sum
            # planned/submitted amounts straight off each row, not off
            # amounts_by_ticker) from still summing both rows' amounts.
            # Tracked by id() so removal only ever targets the exact
            # duplicate object, never an unrelated row that merely compares
            # equal (Codex review, PR #654: "Fold duplicate candidate rows
            # by order ID").
            _duplicate_candidate_row_ids: set = set()
            for row in _ordered_candidate_rows:
                ticker = row.get("ticker")
                # Same unhashable-value hazard as order_id below, reached
                # one line earlier: a truthy but unhashable ticker (e.g. a
                # JSON array) hits `ticker in seen_tickers` immediately
                # after this and raises TypeError, aborting the entire
                # weekly report before the order-id guard even runs.
                # Normalized the same way -- a non-string ticker becomes
                # None, written back onto the row so every later
                # `row.get("ticker")` read (existing_tickers,
                # tickers_without_candidate_order_id, etc.) sees the
                # sanitized value too (Codex review, PR #654: "Normalize
                # malformed candidate tickers before set lookup").
                if not isinstance(ticker, str):
                    ticker = None
                    row["ticker"] = None
                order_id = row.get("order_id")
                # load_json_files() doesn't schema-validate retained states,
                # and this normalization exists specifically to admit
                # structurally rejected artifacts -- a truthy but unhashable
                # order_id (e.g. a JSON array) reaches candidate_identity_
                # by_order_id.setdefault(order_id, ...) below and raises
                # TypeError, aborting the entire weekly report. Only a
                # non-empty string is a usable order identity here; anything
                # else is treated the same as a missing one (Codex review,
                # PR #654: "Validate order IDs before using them as mapping
                # keys").
                if not isinstance(order_id, str):
                    order_id = None
                    # Written back onto the row itself, not just the local
                    # variable -- every OTHER `row.get("order_id")` read
                    # elsewhere in this normalization pass (e.g. the
                    # existing_order_ids set comprehension below) would
                    # otherwise still see the raw unhashable value and hit
                    # the exact same TypeError this guard exists to avoid.
                    row["order_id"] = None
                if ticker in seen_tickers and order_id and order_id in candidate_identity_by_order_id:
                    # A second retained row for the SAME physical order (the
                    # same order_id, not just the same ticker) -- a genuine
                    # duplicate, not a same-day retry's distinct second
                    # order. Minting the usual composite "<ticker>:<order_id>"
                    # identity below would have created a second identity
                    # for this one order_id, so both rows' amounts summed
                    # into $14 for a single $7 order across executed,
                    # planned, graded, and sector totals -- the
                    # broker-outcome side's own order-ID dedup
                    # (outcome_identity_by_order_id) has no visibility into
                    # candidate-side duplication like this. Fold onto the
                    # identity this order_id already claimed instead (Codex
                    # review, PR #654: "Fold duplicate candidate rows by
                    # order ID").
                    identity = candidate_identity_by_order_id[order_id]
                    _duplicate_candidate_row_ids.add(id(row))
                elif ticker in seen_tickers and order_id:
                    identity = f"{ticker}:{order_id}"
                elif ticker in seen_tickers and row.get("whole_share_settlement_adjustment"):
                    settlement_sequence[ticker] = settlement_sequence.get(ticker, 0) + 1
                    identity = f"{ticker}:whole_share_settlement_adjustment#{settlement_sequence[ticker]}"
                elif ticker in seen_tickers:
                    # A repeated, id-less, non-adjustment row -- see
                    # candidate_sequence above. Sequence-numbered exactly the
                    # way an id-less adjustment row is, so both amounts sum
                    # into day/week notional and both count as distinct
                    # submissions, instead of the second clobbering the first.
                    candidate_sequence[ticker] = candidate_sequence.get(ticker, 0) + 1
                    identity = f"{ticker}:candidate#{candidate_sequence[ticker]}"
                else:
                    identity = ticker
                seen_tickers.add(ticker)
                if row.get("whole_share_settlement_adjustment"):
                    adjustment_identities.add(identity)
                    adjustment_identity_by_ticker.setdefault(ticker, identity)
                if order_id and not row.get("whole_share_settlement_adjustment"):
                    candidate_identity_by_order_id.setdefault(order_id, identity)
                    candidate_order_id_by_identity.setdefault(identity, order_id)
                if not order_id and not row.get("whole_share_settlement_adjustment"):
                    # The reconciliation pool the broker_outcomes pass draws on
                    # (see id_less_candidate_identities above). Row order is
                    # preserved so the first unclaimed row of a ticker is the
                    # first one an outcome adopts.
                    id_less_candidate_identities.setdefault(ticker, []).append(identity)
                if _has_usable_claim_breakdown(row):
                    claim_sources[identity] = row
                # _finite_float(), not a bare float(): a malformed (or
                # non-finite, e.g. "NaN"/"Infinity" -- Codex review, PR
                # #654: "Reject non-finite candidate amounts before
                # aggregation") submitted_amount_usd converts cleanly via
                # a plain float() call and was indistinguishable from a
                # genuinely usable one, so the planned_amount_usd fallback
                # below only ever fired when submitted_amount_usd was
                # entirely ABSENT -- a present-but-unusable value (e.g.
                # submitted_amount_usd="bad" alongside a perfectly valid
                # planned_amount_usd=5) recorded the order as $0 day/week
                # executed notional instead of falling back to the equally
                # real planned amount (Codex review, PR #654: "Fall back
                # when the submitted amount is unusable").
                amt_value = _finite_float(row.get("submitted_amount_usd"))
                if amt_value is None:
                    amt_value = _finite_float(row.get("planned_amount_usd"))
                if amt_value is not None:
                    amounts_by_ticker[identity] = amt_value
                else:
                    amounts_by_ticker.setdefault(identity, 0.0)
                # Codex review, PR #593 discussion_r3840229902 ("Propagate
                # submitted amounts into retained candidate rows"):
                # build_graded_vs_fixed()/build_sector_matrix() read
                # planned_amount_usd directly off each candidate row -- not
                # off this function's own amounts_by_ticker aggregate -- so a
                # retained row that only ever recorded submitted_amount_usd
                # (never planned_amount_usd) rendered as $0 graded/sector
                # spend even though the performance snapshot (fed by
                # amounts_by_ticker) shows this same row's full amount.
                # Backfill planned_amount_usd from the just-derived amount
                # whenever the row doesn't already carry a valid one of its
                # own. Lifted into _backfill_planned_amount so the
                # broker_outcomes pass below can reuse it for a row this pass
                # could derive nothing for (Codex review, PR #593: "Backfill
                # matched candidates from outcome amounts").
                _backfill_planned_amount(row, amt_value)
                # ...and remember which row this identity came from, so that
                # same later pass can find it. First writer wins for a
                # repeated identity, matching every other identity map here.
                candidate_row_by_identity.setdefault(identity, row)
                if not row.get("whole_share_settlement_adjustment"):
                    counted_identities.add(identity)
            if _duplicate_candidate_row_ids:
                # Drop the folded duplicate row objects themselves from
                # every list that still holds them (by identity, via id() --
                # see the note on _duplicate_candidate_row_ids above), so a
                # direct-row consumer downstream sees exactly one row per
                # physical order, matching the aggregation identity already
                # folded above.
                _all_dict_candidate_rows = [
                    r for r in _all_dict_candidate_rows if id(r) not in _duplicate_candidate_row_ids
                ]
                _raw_buy_candidates = data.get("buy_candidates")
                if isinstance(_raw_buy_candidates, list):
                    data["buy_candidates"] = [
                        r for r in _raw_buy_candidates if id(r) not in _duplicate_candidate_row_ids
                    ]
                # counts["buy_candidates"] was already computed above (from
                # the FULL pre-dedup list) by the time this removal runs, so
                # it still counted the just-removed duplicate row --
                # build_performance_snapshot() reads this stored count, not
                # len(buy_candidates), so a retained state with one folded
                # duplicate still rendered "Buy candidates | 2" for a single
                # real order (Codex review, PR #654: "Recompute the
                # candidate count after folding duplicates"). Recomputed
                # the same way as the original computation: excluding
                # settlement-adjustment rows.
                counts = data.get("counts")
                if isinstance(counts, dict):
                    counts["buy_candidates"] = sum(
                        1
                        for r in data["buy_candidates"]
                        if isinstance(r, dict) and not r.get("whole_share_settlement_adjustment")
                    )
            # build_graded_vs_fixed()/build_sector_matrix() consume only
            # buy_candidates (they never look at broker_outcomes), so an
            # outcome whose only evidence lives there -- the exact case this
            # normalization exists to retain -- would report its executed
            # notional in the performance snapshot while showing zero graded
            # spend and no sector allocation for that same real order (Codex
            # review: "Materialize outcome-only submissions for dashboard
            # sections"). Track which tickers/order_ids already have a real
            # buy_candidates row so a synthetic row is added only for
            # evidence not already represented there.
            # Uses `_all_dict_candidate_rows` (every retained dict row,
            # placed or not) rather than the status-filtered
            # `_candidate_rows` -- a statusless or still-non-placed row
            # whose matching broker outcome proves submission must still
            # count as "already represented" here, or the outcome pass
            # below materializes a duplicate synthetic row for it (see the
            # comment on `_all_dict_candidate_rows` above). Also avoids
            # re-reading data.get("buy_candidates") directly, which carries
            # the same non-iterable hazard `_candidate_rows` was already
            # built to guard against (Codex review, PR #654: "Coerce
            # candidate containers before normalization").
            existing_candidate_rows = _all_dict_candidate_rows
            existing_tickers = {row.get("ticker") for row in existing_candidate_rows}
            # A retained retry's recovery merge can append its own
            # _failure_candidate_row (near_close_dca.py) for a same-ticker
            # composite outcome BEFORE this normalization runs, so the
            # composite order's order_id already has a real buy_candidates
            # row -- matching on ticker alone can't see that, since the
            # composite row shares its ticker with the primary order's row.
            existing_order_ids = {row.get("order_id") for row in existing_candidate_rows if row.get("order_id")}
            # First retained row seen for a given order_id, across EVERY
            # retained row (not just the real-order-status ones
            # `candidate_row_by_identity` covers) -- lets a matching
            # broker_outcomes entry backfill a statusless/still-non-placed
            # row's amount and status below instead of just skipping past
            # it once `existing_order_ids` says it's already represented
            # (Codex review, PR #654: "Backfill matched stale candidate
            # rows").
            candidate_row_by_order_id: Dict[Any, Dict[str, Any]] = {}
            for row in existing_candidate_rows:
                _row_order_id = row.get("order_id")
                if _row_order_id:
                    candidate_row_by_order_id.setdefault(_row_order_id, row)
            # Tickers with at least one candidate row that carries NO order
            # id -- the only rows a bare-ticker match may stand in for once
            # the outcome itself has an id to compare (Codex review, PR #593:
            # "Match ticker-only outcomes only to candidates without IDs").
            tickers_without_candidate_order_id = {
                row.get("ticker") for row in existing_candidate_rows if not row.get("order_id")
            }
            synthetic_candidate_rows: List[Dict[str, Any]] = []
            # The synthetic row already created for a given identity, so a
            # LATER duplicate outcome for the same order_id (folded via
            # outcome_identity_by_order_id, see
            # folded_duplicate_outcome_order_id below) that supplies a
            # usable amount the EARLIER duplicate lacked can backfill it
            # onto that existing row instead of leaving it stuck at
            # planned_amount_usd=None while amounts_by_ticker/the
            # performance snapshot already report the later amount as
            # executed (Codex review, PR #654: "Backfill the synthetic row
            # from later duplicate outcomes").
            synthetic_row_by_identity: Dict[Any, Dict[str, Any]] = {}
            broker_outcomes = data.get("broker_outcomes")
            if isinstance(broker_outcomes, dict):
                # An id-less outcome reconciles onto a candidate identity by
                # ticker/key alone, without ever consulting the pool below --
                # so retire the entry it already claims BEFORE any id-bearing
                # outcome can consume it. Without this pre-pass an id-less
                # outcome for AAA and a composite id-bearing outcome for AAA
                # would both land on the single id-less candidate row, and the
                # second, genuinely distinct order would be swallowed instead
                # of aggregating (and getting a synthetic row) of its own.
                #
                # Codex review, PR #593: "Match bare id-less outcomes to
                # sequenced candidates". The SAME arithmetic the id-bearing
                # fold below fixes, reached from the other side. A retained
                # blocked state can hold an ID-BEARING $5 AAA row (which takes
                # the bare "AAA" identity, being first) plus a later id-less
                # $7 pending row (sequenced to "AAA:candidate#1"), with the
                # matching broker outcome ALSO id-less and keyed bare "AAA".
                # The pool for AAA then holds only the sequenced identity
                # while this pre-pass's ``_identity`` is the bare ticker, so
                # nothing was consumed, and the outcome pass fell through to
                # the bare identity -- OVERWRITING the ID-bearing row's $5
                # with $7 while the id-less row kept its own $7: $14 of
                # day/week notional for two orders worth $12.
                #
                # So when the bare identity this outcome would take is
                # already claimed by an ID-BEARING candidate row, the outcome
                # cannot be that row's (that row has an id and this outcome
                # has none) -- it is the id-less row's, and it consumes a pool
                # entry exactly as an id-bearing outcome does below. Resolved
                # in a SECOND sub-pass so the direct claims above always win:
                # an outcome that already owns its pool entry retires it
                # before any other outcome, id-less or id-bearing, can take
                # it, which is precisely the guarantee the pre-pass exists
                # for. Narrow by construction: only bare-key outcomes (a
                # composite key is distinct by construction and may coincide
                # with an id-bearing candidate's own composite identity), only
                # non-adjustments, and only while an UNCLAIMED id-less row
                # remains -- so surplus in either direction still behaves, and
                # two genuinely distinct id-less rows with no matching outcome
                # are still two orders.
                id_less_outcome_identity: Dict[Any, Any] = {}
                _bare_id_less_outcomes: List[Tuple[Any, Any]] = []
                for _key, _outcome in broker_outcomes.items():
                    if not isinstance(_outcome, dict) or _outcome.get("status") not in _REAL_ORDER_STATUSES:
                        continue
                    if _outcome.get("order_id") or _outcome.get("whole_share_settlement_adjustment"):
                        continue
                    # ``or _key``, matching the main outcome pass and the
                    # canonical submitted_order_rows() writer. The
                    # default-argument form applies only when "ticker" is
                    # ABSENT, so an explicit None/"" reached the pool lookup as
                    # a blank -- the pre-pass then failed to consume the
                    # sequenced id-less identity and the main pass overwrote
                    # the ID-bearing row's bare identity instead ($5 + $7
                    # reported as $14 rather than $12). Third and last of the
                    # paths resolving an outcome symbol; all three now agree
                    # (Codex review, PR #593: "Use the mapping key for blank
                    # tickers in the pre-pass").
                    _ticker = _outcome.get("ticker")
                    # A truthy but unhashable ticker (e.g. a JSON array)
                    # reaches id_less_candidate_identities.get(_ticker)
                    # below and raises TypeError -- the same unhashable-
                    # value hazard already guarded on the candidate-row
                    # side; the JSON mapping key (`_key`, always a real
                    # string) is the correct fallback here too, matching
                    # the "ticker absent" case this line already handled
                    # (Codex review, PR #654: "Normalize broker-outcome
                    # tickers before map lookups").
                    if not isinstance(_ticker, str) or not _ticker:
                        _ticker = _key
                    _identity = _key if _key != _ticker else _ticker
                    _pool = id_less_candidate_identities.get(_ticker)
                    if _pool and _identity in _pool:
                        _pool.remove(_identity)
                    elif _identity == _ticker and candidate_order_id_by_identity.get(_identity) is not None:
                        _bare_id_less_outcomes.append((_key, _ticker))
                for _key, _ticker in _bare_id_less_outcomes:
                    _pool = id_less_candidate_identities.get(_ticker)
                    if _pool:
                        id_less_outcome_identity[_key] = _pool.pop(0)
                for key, outcome in broker_outcomes.items():
                    if not isinstance(outcome, dict) or outcome.get("status") not in _REAL_ORDER_STATUSES:
                        continue
                    # submitted_order_rows() (scripts/scheduled_artifacts.py)
                    # supports a composite mapping key (e.g. "AAPL:<order-id>")
                    # for a same-ticker retry's second order, with the real
                    # ticker carried in outcome["ticker"]. Using the bare
                    # ticker as the identity for every outcome would collapse
                    # two distinct orders for the same ticker into one
                    # (Codex review: "Preserve multiple same-ticker orders in
                    # retained metrics"), but using the raw dict key for the
                    # PRIMARY outcome (whose key already equals the ticker)
                    # would double-count against a buy_candidates row keyed by
                    # that same bare ticker (Codex review: "Reconcile
                    # composite outcome keys before summing metrics"). Use the
                    # composite key itself only when it differs from the
                    # ticker -- i.e. only for the retry's extra order -- so
                    # the primary order still reconciles with its candidate
                    # row while a retry's second order sums as distinct.
                    # ``or key``, not ``get("ticker", key)``: a placed outcome
                    # can carry an EXPLICIT ``None``/``""`` ticker, which the
                    # default-argument form hands straight through -- emitting
                    # the synthetic candidate with a blank symbol, so graded
                    # spend and the sector matrix attribute a real order to an
                    # unknown ticker. The canonical writer
                    # scripts.scheduled_artifacts.submitted_order_rows() uses
                    # the truthy fallback for exactly this shape; matching it
                    # keeps the dashboard and the canonical order record
                    # naming the same symbol (Codex review, PR #593: "Fall
                    # back from blank outcome tickers during normalization").
                    ticker = outcome.get("ticker")
                    # Same unhashable-value hazard as the pre-pass's
                    # _ticker above: a truthy but unhashable ticker reaches
                    # dict keys/lookups throughout the rest of this pass
                    # (amounts_by_ticker[identity], etc.) and raises
                    # TypeError. `key` (the JSON mapping key, always a real
                    # string) is the correct fallback, matching the
                    # existing "ticker absent" behavior (Codex review, PR
                    # #654: "Normalize broker-outcome tickers before map
                    # lookups").
                    if not isinstance(ticker, str) or not ticker:
                        ticker = key
                    identity = key if key != ticker else ticker
                    outcome_order_id = outcome.get("order_id")
                    # Same unhashable-value hazard the candidate-row pass
                    # above already guards against: a truthy but unhashable
                    # order_id (e.g. a JSON array) reaches `outcome_
                    # order_id in candidate_identity_by_order_id` below and
                    # raises TypeError, aborting the entire weekly report.
                    # This pass doesn't write back onto `outcome` itself --
                    # unlike the candidate row above, nothing later re-reads
                    # outcome.get("order_id") in this loop iteration; every
                    # remaining use goes through this local variable (Codex
                    # review, PR #654: "Validate broker outcome IDs before
                    # reconciliation").
                    if not isinstance(outcome_order_id, str):
                        outcome_order_id = None
                    # The bare id-less outcome the pre-pass matched to a
                    # SEQUENCED id-less candidate row (Codex review, PR #593:
                    # "Match bare id-less outcomes to sequenced candidates").
                    # Applied here, before every id-keyed rule below -- all of
                    # which are gated on the outcome HAVING an id and so
                    # cannot fire for this shape -- so the outcome lands on
                    # the row that is this order instead of overwriting the
                    # ID-bearing row that merely holds the bare identity.
                    # ``folded_id_less_candidate`` is initialized here rather
                    # than at the id-bearing fold below (the two are mutually
                    # exclusive by their order-id guards) so both reach the
                    # synthetic-row suppression through the same flag.
                    folded_id_less_candidate = False
                    if not outcome_order_id and key in id_less_outcome_identity:
                        identity = id_less_outcome_identity[key]
                        folded_id_less_candidate = True
                    # Codex review, PR #593 ("Reconcile synthetic recovery
                    # outcomes by order ID"). The key-vs-ticker rule above is
                    # right for a same-ticker RETRY, whose composite key really
                    # does denote a second distinct order, but wrong for an
                    # inverse-Martingale recovery, whose "<ticker>#imart-recovery"
                    # key denotes the SAME order the candidate pass already
                    # aggregated under the bare ticker. Order id is the only
                    # thing that tells the two apart, so when this outcome's id
                    # matches an id a candidate row already claimed, adopt that
                    # row's identity: the amount reconciles into one entry and
                    # counted_identities gains no second member, instead of the
                    # run reporting two orders and double the notional.
                    #
                    # Deliberately narrow, so it composes with -- rather than
                    # overrides -- every identity rule already established here:
                    #   * skipped when `identity` was already aggregated by a
                    #     candidate row that claims an order id OF ITS OWN
                    #     (the primary/composite cases the existing rules
                    #     resolve correctly on their own -- see the
                    #     occupancy note below);
                    #   * skipped for a whole_share_settlement_adjustment
                    #     outcome, whose delta must stay a separate identity
                    #     from the order it corrects (folding it in would
                    #     overwrite that order's amount with the correction).
                    #
                    # Codex review, PR #593: "Match the bare-key outcome by
                    # its order ID". The occupancy guard used to be a flat
                    # ``identity not in amounts_by_ticker``, which also
                    # skipped the fold when the occupant was an ID-LESS
                    # candidate row that merely happens to share the bare
                    # ticker key -- a shape _merge_placements_into_state()
                    # really does produce, by appending an ID-bearing
                    # placement row beside a preserved id-less one. A
                    # retained blocked state holding an id-less $5 AAA row
                    # (identity "AAA") plus a $7 AAA row with order id B
                    # (identity "AAA:B"), whose outcome arrives under the
                    # BARE key "AAA" carrying id B, skipped the fold, kept
                    # the bare identity, and OVERWROTE the id-less row's $5
                    # with $7 while B stayed aggregated separately -- $14 of
                    # day/week notional for two orders worth $12.
                    #
                    # So the guard is loosened by exactly the difference in
                    # evidence: a candidate row explicitly claiming this
                    # outcome's order id is positive proof of which row this
                    # outcome IS, and outranks a bare-key coincidence with a
                    # row that claims no id at all. When the occupant DOES
                    # claim an id the guard still holds, so the case it was
                    # added for is untouched: an occupant claiming this same
                    # id makes the fold a no-op anyway, and an occupant
                    # claiming a DIFFERENT id is the "Match ticker-only
                    # outcomes only to candidates without IDs" case, left to
                    # the composite-minting rule further down.
                    #
                    # The id-less reconciliation pool is deliberately NOT
                    # touched here: that fold is already gated on the
                    # outcome's id matching NO candidate row, so an outcome
                    # reconciled by order id can never also consume a pool
                    # entry, and the id-less row this outcome stepped off
                    # stays available for a genuinely unmatched outcome.
                    if (
                        outcome_order_id
                        and (
                            identity not in amounts_by_ticker
                            or candidate_order_id_by_identity.get(identity) is None
                        )
                        and not outcome.get("whole_share_settlement_adjustment")
                        and outcome_order_id in candidate_identity_by_order_id
                    ):
                        identity = candidate_identity_by_order_id[outcome_order_id]
                    # Codex review, PR #593: "Match outcomes to repeated
                    # id-less candidates". The fold above reconciles an
                    # outcome with the candidate row that shares its order id;
                    # this reconciles one with the candidate row that has NO
                    # id to share -- the row that IS this order, recorded
                    # before its id was known. See
                    # id_less_candidate_identities for the arithmetic that
                    # went wrong without it.
                    #
                    # Deliberately narrow, composing with the rules around it:
                    #   * only for an outcome that HAS an id (an id-less
                    #     outcome already reconciles by ticker, and the
                    #     pre-pass above retired the entry it claims);
                    #   * only when no candidate row claims that id -- if one
                    #     does, the fold above already adopted its identity
                    #     and this outcome is that row's, not an id-less
                    #     row's;
                    #   * never for a settlement adjustment, whose delta must
                    #     keep its own identity;
                    #   * only while an UNCLAIMED id-less row remains, each
                    #     consumed at most once. More id-bearing outcomes than
                    #     id-less rows therefore leaves the surplus outcomes
                    #     on their own identities (with their own synthetic
                    #     rows), and more id-less rows than outcomes leaves
                    #     the surplus rows sequenced exactly as the previous
                    #     round made them -- two genuinely distinct id-less
                    #     rows with no matching outcome are still two orders.
                    if (
                        outcome_order_id
                        and outcome_order_id not in candidate_identity_by_order_id
                        and not outcome.get("whole_share_settlement_adjustment")
                        and id_less_candidate_identities.get(ticker)
                    ):
                        identity = id_less_candidate_identities[ticker].pop(0)
                        folded_id_less_candidate = True
                    # Codex review, PR #593: "Match ticker-only outcomes only
                    # to candidates without IDs". The fold above deliberately
                    # reconciles an outcome with the candidate row that shares
                    # its order id; this is the opposite case, and the one the
                    # identity rules had no answer for. A retained blocked
                    # state can hold a candidate row for AAA with order id A
                    # alongside an OUTCOME-ONLY placement for AAA carrying a
                    # different order id B (a same-ticker retry whose second
                    # order never got a candidate row of its own). Both sides
                    # carry ids and the ids differ, which is positive proof
                    # they are two distinct orders -- yet the outcome fell
                    # through to the bare-ticker identity A already owns, so a
                    # $5 candidate plus a $7 outcome collapsed into ONE
                    # submitted order worth $7 instead of two worth $12
                    # (amounts_by_ticker[identity] = amt below OVERWRITES,
                    # and the ticker fallback further down then suppressed the
                    # synthetic row that would have carried B).
                    #
                    # Mint the composite "<ticker>:<order_id>" identity the
                    # same-ticker-retry rules already use for exactly this
                    # shape, so B aggregates separately and gets its own
                    # synthetic candidate row. Deliberately narrow, composing
                    # with -- not overriding -- the rules above it:
                    #   * only when BOTH sides carry ids and they differ (an
                    #     id-less candidate row, or an id-less outcome, still
                    #     reconciles by ticker exactly as before);
                    #   * only for a non-adjustment outcome, whose delta must
                    #     keep landing on the identity it corrects;
                    #   * a no-op when the fold above already adopted the
                    #     matching candidate's identity (the ids agree there
                    #     by construction).
                    claimed_order_id = candidate_order_id_by_identity.get(identity)
                    if (
                        outcome_order_id
                        and claimed_order_id
                        and claimed_order_id != outcome_order_id
                        and not outcome.get("whole_share_settlement_adjustment")
                    ):
                        identity = f"{ticker}:{outcome_order_id}"
                    # Codex review, PR #654: "Deduplicate outcome-only
                    # submissions by order ID". None of the folds above can
                    # see this case: they all key off
                    # candidate_identity_by_order_id/candidate_order_id_by_
                    # identity, which stay empty when the same order
                    # appears in broker_outcomes under two different
                    # mapping keys and no buy_candidates row exists at all.
                    # Fold onto whatever identity an EARLIER outcome for
                    # this same order_id already resolved to, so the second
                    # mapping-key entry aggregates into the same submission
                    # instead of becoming a distinct one.
                    folded_duplicate_outcome_order_id = False
                    if (
                        outcome_order_id
                        and outcome_order_id not in candidate_identity_by_order_id
                        and not outcome.get("whole_share_settlement_adjustment")
                        and outcome_order_id in outcome_identity_by_order_id
                    ):
                        identity = outcome_identity_by_order_id[outcome_order_id]
                        folded_duplicate_outcome_order_id = True
                    # Codex review, PR #593: "Separate adjustment outcomes
                    # from the base order identity". The candidate pass splits
                    # an id-less settlement adjustment onto its own identity
                    # so its (possibly negative) delta SUMS with the order it
                    # corrects -- but the outcome pass had no such split, and
                    # every rule above deliberately skips adjustments. So a
                    # retained artifact holding a physical candidate under
                    # "AAA" whose settlement correction exists ONLY in
                    # broker_outcomes["AAA"] gave both the bare-ticker
                    # identity, and ``amounts_by_ticker[identity] = amt``
                    # OVERWROTE the base amount: a $100 order plus a -$2
                    # correction reported day_notional_usd = -2 instead of
                    # $98.
                    #
                    # Split it the same way the candidate pass does, sharing
                    # settlement_sequence so the two namespaces cannot
                    # collide. Deliberately narrow, composing with the rules
                    # above rather than overriding them:
                    #   * only for the PRIMARY (bare-ticker) identity -- a
                    #     composite-keyed adjustment outcome is already
                    #     distinct by construction;
                    #   * only when that identity is already claimed, i.e.
                    #     some base order really would be overwritten -- an
                    #     adjustment that is the only evidence for its ticker
                    #     still keeps the bare identity, exactly as before;
                    #   * never when the claiming entry is itself an
                    #     adjustment: the candidate pass's adjustment row and
                    #     this outcome describe the SAME physical correction,
                    #     so this folds onto that row's identity (overwriting,
                    #     which is the existing dedupe) instead of adding the
                    #     delta a second time.
                    folded_candidate_adjustment = False
                    if (
                        outcome.get("whole_share_settlement_adjustment")
                        and identity == ticker
                        and identity in amounts_by_ticker
                        and identity not in adjustment_identities
                    ):
                        existing_adjustment_identity = adjustment_identity_by_ticker.get(ticker)
                        if existing_adjustment_identity is not None:
                            identity = existing_adjustment_identity
                            folded_candidate_adjustment = True
                        else:
                            settlement_sequence[ticker] = settlement_sequence.get(ticker, 0) + 1
                            identity = (
                                f"{ticker}:whole_share_settlement_adjustment#{settlement_sequence[ticker]}"
                            )
                    # Registered AFTER every fold above has had its say, so
                    # this records the FINAL identity this order_id
                    # resolved to -- first writer wins, matching every
                    # other identity map in this pass (a settlement
                    # adjustment is deliberately excluded, same as the
                    # fold that reads this map).
                    if outcome_order_id and not outcome.get("whole_share_settlement_adjustment"):
                        outcome_identity_by_order_id.setdefault(outcome_order_id, identity)
                    try:
                        amt = float(outcome.get("submitted_amount_usd"))
                    except (TypeError, ValueError):
                        amt = None
                    else:
                        # A standard-JSON-safe "NaN"/"Infinity" string (or an
                        # already-float nan/inf) converts cleanly here and
                        # would otherwise poison amounts_by_ticker, the
                        # running weekly total, and every rendered dashboard
                        # amount derived from it with $nan -- treat a
                        # non-finite amount the same as a non-numeric one:
                        # absent, not a real $0 (Codex review, PR #654:
                        # "Reject non-finite retained order amounts").
                        if not math.isfinite(amt):
                            amt = None
                    if amt is not None:
                        amounts_by_ticker[identity] = amt
                        # ...and onto the matched candidate ROW itself, which
                        # the sections below read directly (Codex review, PR
                        # #593: "Backfill matched candidates from outcome
                        # amounts"). A no-op when the row already has a valid
                        # planned amount, or when this outcome matched no
                        # retained row at all and gets a synthetic one below.
                        _backfill_planned_amount(candidate_row_by_identity.get(identity), amt)
                    else:
                        # Codex review, PR #593 discussion_r3840206570:
                        # "Preserve candidate notional when an outcome omits
                        # it" -- the candidate-row pass above already derived
                        # a real amount for this identity from
                        # submitted_amount_usd/planned_amount_usd on the
                        # matching buy_candidates row when one exists.
                        # Unconditionally overwriting with 0.0 here whenever
                        # the broker outcome itself omits (or has a
                        # non-numeric) submitted_amount_usd threw that
                        # correctly-derived amount away -- e.g. a real $5
                        # pending candidate with a matching pending outcome
                        # that never recorded submitted_amount_usd got
                        # normalized to $0 day/week notional instead of $5.
                        # Only fill in 0.0 when nothing was derived at all.
                        amounts_by_ticker.setdefault(identity, 0.0)
                    if not outcome.get("whole_share_settlement_adjustment"):
                        counted_identities.add(identity)
                    # Same claim-week attribution as the candidate pass: the
                    # outcome carries its own whole_share_claim_breakdown, and
                    # an outcome-only retained state (no matching candidate
                    # row) is exactly the shape this normalization exists to
                    # support, so the claim-week split cannot live in one pass
                    # only. Collected for ordinary recovered orders too, not
                    # just settlement adjustments (Codex review, PR #593:
                    # "Attribute recovered orders by their claim week") --
                    # note this runs OUTSIDE the adjustment branch now, and
                    # after the counted_identities add above precisely because
                    # a recovery must keep counting as a physical order.
                    # _has_usable_claim_breakdown(), not bare truthiness: a
                    # duplicate broker outcome for the same order (folded
                    # onto an earlier one via outcome_identity_by_order_id
                    # above) can itself carry a malformed breakdown while a
                    # LATER duplicate for that same identity carries a
                    # valid one -- setdefault() on bare truthiness let the
                    # malformed one reserve claim_sources first, so the
                    # order's full amount was attributed entirely to the
                    # artifact's current week instead of the real claim
                    # week the valid duplicate names (Codex review, PR
                    # #654: "Prefer usable broker-outcome claim
                    # breakdowns"). Direct assignment (not setdefault),
                    # matching the candidate-row registration this mirrors.
                    if _has_usable_claim_breakdown(outcome):
                        claim_sources[identity] = outcome
                    # Matching on ticker alone let a composite-keyed outcome
                    # (identity != ticker) always fall through to a synthetic
                    # row, even when the recovery merge already appended a
                    # real buy_candidates row for that exact order -- the
                    # ticker-only check above never covers the composite
                    # case, so that real second order got a THIRD synthetic
                    # row, inflating counts.buy_candidates, graded/fixed
                    # spend, and sector notional (Codex review, PR #593
                    # discussion_r3837437492: "Match synthetic candidates by
                    # order ID"). Check the outcome's own order_id against
                    # every existing candidate row first -- covering both the
                    # primary and composite cases uniformly -- and fall back
                    # to the ticker-only match only when the outcome carries
                    # no order_id to compare. (``outcome_order_id`` is read
                    # once at the top of this iteration -- the identity
                    # reconciliation above needs it too.)
                    if outcome_order_id and outcome_order_id in existing_order_ids:
                        # candidate_row_by_identity (used above for the
                        # ordinary matched-candidate backfill) only ever
                        # gets entries from _ordered_candidate_rows, i.e.
                        # real-order-status rows -- so a statusless or
                        # still-non-placed row whose order_id nonetheless
                        # matches this outcome (the exact case
                        # `existing_order_ids` was widened to recognize)
                        # never received that backfill and was left at
                        # planned_amount_usd=None with its stale status,
                        # even though its order is proven submitted here.
                        # Without this, such an order reported real
                        # executed notional in the performance snapshot
                        # while contributing $0 planned/graded/sector spend,
                        # and a "not_submitted" status kept it excluded from
                        # build_graded_vs_fixed() entirely (Codex review, PR
                        # #654: "Backfill matched stale candidate rows").
                        _stale_row = candidate_row_by_order_id.get(outcome_order_id)
                        if _stale_row is not None:
                            _backfill_planned_amount(_stale_row, amt)
                            _outcome_status = outcome.get("status")
                            if (
                                _stale_row.get("status") not in _REAL_ORDER_STATUSES
                                and _outcome_status in _REAL_ORDER_STATUSES
                            ):
                                _stale_row["status"] = _outcome_status
                        continue
                    # An outcome folded onto an EARLIER outcome's identity
                    # for the same order_id (no candidate row involved at
                    # all -- see outcome_identity_by_order_id above) has
                    # already had its notional/count merged into that
                    # earlier outcome; that earlier outcome already
                    # produced (or will produce) the one synthetic row this
                    # physical order needs. A second synthetic row here
                    # would inflate counts.buy_candidates and double the
                    # graded/sector spend for one order (Codex review, PR
                    # #654: "Deduplicate outcome-only submissions by order
                    # ID").
                    if folded_duplicate_outcome_order_id:
                        # This LATER duplicate can still supply a usable
                        # amount the EARLIER duplicate lacked (e.g. the
                        # first entry omitted submitted_amount_usd) --
                        # amounts_by_ticker/the performance snapshot
                        # already reflect it via `identity` above, but the
                        # synthetic row created for the earlier duplicate
                        # would otherwise stay stuck at
                        # planned_amount_usd=None, reporting $0.00 in
                        # graded/sector/planned totals while the
                        # performance snapshot shows the real amount as
                        # executed (Codex review, PR #654: "Backfill the
                        # synthetic row from later duplicate outcomes").
                        _backfill_planned_amount(synthetic_row_by_identity.get(identity), amt)
                        existing_synthetic_row = synthetic_row_by_identity.get(identity)
                        if (
                            amt is not None
                            and isinstance(existing_synthetic_row, dict)
                            and existing_synthetic_row.get("submitted_amount_usd") is None
                        ):
                            existing_synthetic_row["submitted_amount_usd"] = amt
                        continue
                    # Codex review, PR #593 discussion_r3840206567: "Fall back
                    # to ticker when candidate order IDs are absent" -- the
                    # order-id check above only matches when SOME existing
                    # candidate row carries a comparable order_id. A retained
                    # partial artifact's submitted candidate row can lack
                    # order_id entirely (written before order-id tracking, or
                    # by a code path that never records it) while its
                    # matching PRIMARY broker outcome carries one -- in that
                    # case existing_order_ids never contains the outcome's
                    # id, so without this fallback the outcome always looked
                    # unmatched and got a duplicate synthetic row appended,
                    # even though the primary candidate row for that exact
                    # ticker already covers it. Symmetric with the
                    # ticker-only fallback the order-id fix above was layered
                    # on top of: only applies to the PRIMARY identity (never
                    # a composite retry key), which is exactly the case a
                    # bare ticker match can safely stand in for.
                    #
                    # Codex review, PR #593: "Match ticker-only outcomes only
                    # to candidates without IDs" -- the fallback stays scoped
                    # to the case it was introduced for. When this outcome
                    # carries an order id, a bare-ticker match is only
                    # trustworthy if some candidate row for that ticker LACKS
                    # an id: an id-bearing candidate row whose id is not this
                    # one's was already ruled out by the existing_order_ids
                    # check above, and the two ids prove the rows are
                    # different orders, so suppressing the synthetic row
                    # would silently drop a real placement. An outcome with
                    # no id to compare keeps matching on ticker alone, as
                    # before.
                    #
                    # ``folded_candidate_adjustment`` keeps this suppression
                    # reachable for an adjustment outcome that was just folded
                    # onto the candidate pass's adjustment identity: that row
                    # already exists in buy_candidates, so the pre-split
                    # behavior (match by bare ticker, skip the synthetic row)
                    # must survive the identity change (Codex review, PR #593:
                    # "Separate adjustment outcomes from the base order
                    # identity").
                    #
                    # ``folded_id_less_candidate`` extends the same
                    # reachability to an outcome that just adopted an id-less
                    # candidate row's identity (Codex review, PR #593: "Match
                    # outcomes to repeated id-less candidates"): that row is
                    # already in buy_candidates and already carries this
                    # order's notional, so synthesizing a second row for the
                    # same physical order would inflate counts.buy_candidates
                    # and double its graded/sector spend -- precisely the
                    # duplication the identity reconciliation above removes
                    # from counts.submitted and the notional.
                    if (
                        (identity == ticker or folded_candidate_adjustment or folded_id_less_candidate)
                        and ticker in existing_tickers
                        and (not outcome_order_id or ticker in tickers_without_candidate_order_id)
                    ):
                        continue
                    synthetic_row = {
                        "ticker": ticker,
                        "status": outcome.get("status"),
                        "submitted_amount_usd": amt,
                        "planned_amount_usd": amt,
                    }
                    if outcome.get("whole_share_settlement_adjustment"):
                        # Carry the marker forward onto the synthesized row
                        # too, so a LATER normalization pass over this same
                        # retained state (or build_graded_vs_fixed()/
                        # build_sector_matrix(), which already special-case
                        # this exact field) still recognizes it as a
                        # bookkeeping-only correction rather than a fresh
                        # candidate.
                        synthetic_row["whole_share_settlement_adjustment"] = True
                    if outcome_order_id:
                        # Only stamped when present -- carrying it forward
                        # lets a LATER retry's own normalization pass match
                        # this synthetic row by order_id too (see the check
                        # above), rather than only ever matching by ticker.
                        synthetic_row["order_id"] = outcome_order_id
                    synthetic_candidate_rows.append(synthetic_row)
                    synthetic_row_by_identity[identity] = synthetic_row
            if synthetic_candidate_rows:
                # `or []` only substitutes when buy_candidates is ABSENT or
                # falsy -- a present but wrong-typed value (e.g. a truthy
                # dict from a structurally rejected artifact) survives that
                # check and then can't be concatenated with a list, raising
                # TypeError and aborting the entire weekly report instead of
                # retaining the real broker_outcomes evidence this
                # normalization pass exists to surface (Codex review, PR
                # #654: "Coerce malformed candidate containers before
                # appending").
                existing_candidates = data.get("buy_candidates")
                if not isinstance(existing_candidates, list):
                    existing_candidates = []
                # Already dict-only at this point -- the blocked-state
                # normalization above (Codex review, PR #654: "Remove
                # malformed rows from retained candidate lists") filters
                # data["buy_candidates"] to dict rows unconditionally, not
                # just when synthetic rows get appended here.
                data["buy_candidates"] = existing_candidates + synthetic_candidate_rows
                # Keep counts.buy_candidates (read directly by
                # build_performance_snapshot(), not len(buy_candidates)) in
                # sync with the rows just appended -- otherwise it would
                # still report the pre-append total even though the "Buy
                # candidates" section below now includes the synthesized
                # rows too.
                counts = data.get("counts")
                if not isinstance(counts, dict):
                    counts = {}
                    data["counts"] = counts
                # Same settlement-adjustment exclusion as the filtered-count
                # assignment above -- a synthesized settlement-adjustment row
                # is still a bookkeeping-only correction, not a fresh
                # candidate.
                counts["buy_candidates"] = sum(
                    1
                    for r in data["buy_candidates"]
                    if isinstance(r, dict) and not r.get("whole_share_settlement_adjustment")
                )
            if amounts_by_ticker:
                # The minimal retained state this normalization is meant to
                # support (a submitted candidate row with no counts/budget
                # object at all) has neither container -- creating them here
                # rather than only updating an existing one is required for
                # build_performance_snapshot() to see anything but zero
                # (Codex review: "Create metric containers for retained
                # partial states").
                counts = data.get("counts")
                if not isinstance(counts, dict):
                    counts = {}
                    data["counts"] = counts
                # counted_identities (not amounts_by_ticker's own keys)
                # excludes any identity whose ONLY evidence is a
                # whole_share_settlement_adjustment row/outcome -- a
                # bookkeeping-only dollar correction for an order already
                # counted elsewhere, not a fresh distinct placement (Codex
                # review, PR #593 discussion_r3840229906: "Exclude settlement
                # adjustments from normalized order counts"). Its dollar
                # delta still lands in day_notional_usd/week_notional_usd
                # below via amounts_by_ticker, which is untouched by this
                # exclusion.
                counts["submitted"] = len(counted_identities)
                budget = data.get("budget")
                if not isinstance(budget, dict):
                    budget = {}
                    data["budget"] = budget
                day_total = sum(amounts_by_ticker.values())
                budget["day_notional_usd"] = day_total
                # build_performance_snapshot() reports the MAX week_notional_usd
                # seen across the week's artifacts as the week-to-date executed
                # total. The minimal retained partial state this normalization
                # supports can leave that field absent or stale (unrelated to
                # today's real execution), understating -- or zeroing -- the
                # week total even though real capital was just deployed (Codex
                # review: "Include retained submissions in weekly notional").
                # Flooring at just this day's own total (the original fix)
                # under-reports the week once EARLIER days already
                # contributed real spend of their own: if the week-to-date
                # total going into today was $100 and today's retained
                # partial run only contributes $5 more, flooring at $5
                # reports $100 (via the max() in build_performance_snapshot,
                # since $5 < $100) instead of the correct $105. The correct
                # week-to-date total is today's own spend ADDED to the prior
                # cumulative total, not compared against it in isolation.
                # Codex review, PR #593 discussion_r3840403547: "Apply
                # negative settlement corrections to weekly notional". A
                # whole_share_settlement_adjustment identity's dollar amount
                # (already excluded from counted_identities above) is a
                # CORRECTION delta against an order already counted in an
                # earlier day's total -- not a fresh independent order total
                # of its own -- and that delta can be NEGATIVE (a settlement
                # correcting an earlier overcount downward). Splitting
                # amounts_by_ticker by counted_identities separates the two:
                # ordinary order amounts still only ever RAISE the running
                # weekly total (the max-based floor above/below, preserving
                # every prior fix in this area), while a settlement-
                # adjustment delta is applied ADDITIVELY -- including when
                # negative -- directly to whatever that raise-only step
                # produced. A one-way max() on the settlement-adjustment
                # component would refuse to ever LOWER a stale weekly total
                # to reflect a downward correction (e.g. a prior $100
                # week-to-date plus a -$2 correction must net to $98, not
                # stay at $100), overstating deployed capital.
                # Codex review, PR #593: "Attribute recovered orders by their
                # claim week" -- ordinary order amounts are apportioned by
                # claim week too, not just settlement deltas. A recovered
                # whole-share order published in a LATER ISO week than the
                # claim day it books against (a Friday claim surfaced Monday)
                # carries that day in whole_share_claim_breakdown, and
                # dropping its full amount into Monday's running weekly total
                # over-reported the new week's deployed capital exactly the
                # way an adjustment did. Same semantics as the settlement
                # split below -- exclusion from the current week, never a
                # retroactive rewrite of the closed week, because this
                # accumulator is a single forward pass -- and the same
                # bucketing rules for a row with no breakdown, an unparseable
                # trading_day, a missing amount_usd, or an unaccounted
                # remainder. day_notional_usd above is deliberately untouched:
                # the physical order settled today and its cash movement
                # belongs on today.
                order_amounts_total = sum(
                    _current_week_claim_amount(claim_sources.get(identity), amt, claim_week_key)
                    for identity, amt in amounts_by_ticker.items()
                    if identity in counted_identities
                )
                # ...and only for the PORTION of a correction that belongs to
                # THIS ISO week. A delta whose whole_share_claim_breakdown
                # names a day in an already-passed week corrects a total this
                # pass can no longer reach (see claim_sources' own
                # comment above), so applying it here would move money OUT of,
                # or INTO, the wrong week rather than fixing anything -- and an
                # aggregate spanning both weeks must contribute only its
                # current-week share, not all of itself and not none of itself.
                settlement_adjustment_total = sum(
                    _current_week_claim_amount(claim_sources.get(identity), amt, claim_week_key)
                    for identity, amt in amounts_by_ticker.items()
                    if identity not in counted_identities
                )
                # Codex review, PR #593: "Preserve the reported cumulative week
                # total when normalizing". The running total only knows about
                # the states this caller actually loaded, but a retained
                # morning-family state can REPORT its own cumulative
                # week-to-date under budget.hypothetical_week_spend_usd -- the
                # very key build_performance_snapshot() falls back to, and the
                # accumulator below already honours for the next state. Left
                # out of this floor, a lone blocked state reporting $100
                # hypothetical week spend with a $5 placed row normalized to
                # $5, and because week_notional_usd is written here it then
                # SHADOWED the $100 fallback in both the accumulator and the
                # snapshot: a known-larger cumulative silently became a
                # smaller one. Flooring at it keeps this a raise-only step,
                # exactly like the comparison against the state's own
                # week_notional_usd immediately below.
                candidate_week_notional_usd = max(
                    running_week_notional_usd + order_amounts_total,
                    _num(budget.get("hypothetical_week_spend_usd")),
                )
                if _num(budget.get("week_notional_usd")) < candidate_week_notional_usd:
                    budget["week_notional_usd"] = candidate_week_notional_usd
                if settlement_adjustment_total:
                    budget["week_notional_usd"] = _num(budget.get("week_notional_usd")) + settlement_adjustment_total
                    # Codex review, PR #593 discussion_r3840429733: "Carry
                    # settlement reductions into the running week total". The
                    # accumulator update at the bottom of this loop is a
                    # one-way max() against the state's own field, which is
                    # correct for ordinary raise-only order totals but
                    # DISCARDS a downward settlement correction: a $100
                    # week-to-date corrected to $98 here still left the
                    # accumulator at $100, so the NEXT retained partial state
                    # in the same ISO week (say $5, with no cumulative weekly
                    # field of its own) normalized to $105 instead of the
                    # correct $103. Remember the corrected cumulative so that
                    # final step ASSIGNS it rather than max()-ing against the
                    # pre-correction figure.
                    corrected_week_notional_usd = _num(budget.get("week_notional_usd"))
        # Feed this state's own true week-to-date total into the running
        # tracker for the NEXT state processed, whether that field was just
        # derived above or was already present on a normal completed run
        # (whose own week_notional_usd already reflects everything through
        # that day). morning/midday/market_open/intraday retained states can
        # instead carry the cumulative under hypothetical_week_spend_usd
        # (see build_performance_snapshot's own fallback for the same key).
        #
        # A state whose weekly total was just CORRECTED downward by a
        # settlement adjustment is the one case where max() is wrong: the
        # corrected figure is by construction the true cumulative
        # week-to-date, and taking max() against the stale pre-correction
        # accumulator would silently restore the money the correction just
        # removed for every subsequent state in the week (Codex review, PR
        # #593 discussion_r3840429733). Assign it instead.
        if corrected_week_notional_usd is not None:
            running_week_notional_usd = corrected_week_notional_usd
        else:
            _state_budget = data.get("budget") or {}
            _state_week_notional_usd = _state_budget.get("week_notional_usd")
            if _state_week_notional_usd is None:
                _state_week_notional_usd = _state_budget.get("hypothetical_week_spend_usd")
            running_week_notional_usd = max(running_week_notional_usd, _num(_state_week_notional_usd))
    return kept


def _backfill_planned_amount(row: Optional[Dict[str, Any]], amount: Optional[float]) -> None:
    """Give a retained candidate row a ``planned_amount_usd`` when it has none.

    build_graded_vs_fixed()/build_sector_matrix()/the planned-notional roll-up
    read ``planned_amount_usd`` directly off each candidate row -- never off
    ``load_json_files()``'s own ``amounts_by_ticker`` aggregate -- so a row that
    never recorded one renders as $0 graded/sector/planned spend while the
    performance snapshot shows that same order's full executed amount (Codex
    review, PR #593 discussion_r3840229902: "Propagate submitted amounts into
    retained candidate rows").

    Never overwrites a valid amount the row already carries; a non-numeric one
    counts as absent, since that is what those consumers make of it.

    Used from BOTH normalization passes (Codex review, PR #593: "Backfill
    matched candidates from outcome amounts"). The candidate pass fills a row
    from its own ``submitted_amount_usd``; the broker_outcomes pass fills a
    MATCHED row -- one whose existence suppresses the synthetic row that would
    otherwise have carried the notional -- from the matching outcome's amount,
    the only place an id-less candidate matched to an ID-bearing outcome has
    one at all.
    """
    if not isinstance(row, dict) or amount is None:
        return
    try:
        existing_planned = float(row.get("planned_amount_usd"))
    except (TypeError, ValueError):
        existing_planned = None
    else:
        # A non-finite existing value (a standard-JSON "NaN"/"Infinity"
        # string, or an already-float nan/inf) converts cleanly via
        # float() and was treated as "already has a valid amount," so a
        # finite `amount` derived here was never backfilled over it --
        # downstream _num() then preserves the non-finite value, rendering
        # performance/graded-vs-fixed/sector totals as $nan/$inf. Absent
        # is the correct read for a value those consumers can't use
        # either way (Codex review, PR #654: "Replace non-finite planned
        # amounts during backfill").
        if not math.isfinite(existing_planned):
            existing_planned = None
    if existing_planned is None:
        row["planned_amount_usd"] = amount


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _finite_float(value: Any) -> Optional[float]:
    """``float(value)`` when that succeeds AND is finite, else ``None``.

    Unlike :func:`_num`, never silently substitutes ``0.0`` for a bad
    value -- ``None`` lets a caller distinguish "unusable" from "a real
    zero" and fall back to another field, the same "absent is not zero"
    contract several other normalization helpers in this module already
    follow.
    """
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric_value):
        return None
    return numeric_value


def _iso_week_of_state(data: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """Best-effort ISO ``(year, week)`` for a run_state, used by
    ``load_json_files()`` to reset its running weekly-notional accumulator at
    week boundaries. Prefers the state's own ``"iso_week"`` field (written as
    ``list(near_close_dca.iso_week_key(trading_day))`` -- a two-int
    ``[year, week]`` list -- by the live strategy); falls back to deriving it
    from ``"trading_day"`` (``"YYYY-MM-DD"``) via the same ``date.isocalendar()``
    ISO-week definition when ``iso_week`` is absent or malformed, since
    lighter-weight/older retained states can omit ``iso_week`` entirely.
    Returns ``None`` when neither field yields a usable week -- callers treat
    that as "week unknown" and leave the running accumulator untouched rather
    than guessing.

    A two-int ``iso_week`` is additionally validated with
    ``date.fromisocalendar()`` before being accepted. Shape alone is not
    enough: ``[2026, 60]`` is two ints and no real week, and this helper used
    to hand it back as though it were one. ``_accumulation_sort_key()``
    already rejected such a value and tailed the state, but the accumulation
    loop and ``_latest_week_value()`` call THIS helper directly -- so the two
    disagreed about the same field, and a tailed malformed state could still
    reset the running total to a fictitious week and then win as the
    dashboard's "latest" week (a valid $100 current-week state followed by a
    retained $5 partial carrying that value reported $5). Validating here
    makes every caller agree by construction rather than each re-deriving the
    rule (Codex review, PR #593: "Validate ISO-week bounds before accepting
    the tuple"). An invalid tuple falls through to ``trading_day``, exactly
    as a malformed-shape one already did.
    """
    iso_week = data.get("iso_week")
    trading_day = data.get("trading_day")
    trading_day_week: Optional[Tuple[int, int]] = None
    if isinstance(trading_day, str):
        try:
            _trading_day_iso = date.fromisoformat(trading_day).isocalendar()
        except ValueError:
            trading_day_week = None
        else:
            trading_day_week = (_trading_day_iso.year, _trading_day_iso.week)
    if (
        isinstance(iso_week, (list, tuple))
        and len(iso_week) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) for part in iso_week)
    ):
        try:
            date.fromisocalendar(iso_week[0], iso_week[1], 1)
        except (ValueError, OverflowError):
            pass
        else:
            # A syntactically valid iso_week can still be semantically
            # WRONG for this state's own trading_day -- a retained
            # artifact's iso_week field can be stale or simply mistyped
            # without failing the shape/calendar-validity check above. The
            # accumulator sorts states by trading_day but resets/selects
            # the latest week from THIS return value, so an inconsistent
            # tuple could reset to a fictitious earlier week and let a
            # later, smaller retained partial win as "the latest week"
            # over the real current week's total (a valid $100 current-
            # week state followed by a mislabeled $5 partial reporting
            # $5). When trading_day itself parses to a usable week, it is
            # the ground truth this field must agree with; a disagreement
            # falls through to deriving the week from trading_day instead
            # of trusting iso_week (Codex review, PR #654: "Validate ISO
            # weeks against the trading day").
            if trading_day_week is None or (iso_week[0], iso_week[1]) == trading_day_week:
                return (iso_week[0], iso_week[1])
    return trading_day_week



# The base amount changed from $3 to $2 on this date (see the "Base-amount
# change (Jul 19 2026)" note in docs/strategy.md and config/scheduled_tasks.json).
# ISO date strings ("YYYY-MM-DD") compare correctly as plain strings.
_BASE_AMOUNT_CHANGE_DATE = "2026-07-19"
_PRE_CHANGE_BASE_AMOUNT_USD = 3.0


def _require_base_amount(rs: Dict[str, Any]) -> float:
    """Read the base DCA amount used for a run_state's budget.

    This is a financial config value driving the fixed-baseline comparison, so a
    missing or non-numeric key fails fast rather than silently assuming today's
    base amount. The one documented exception: a run_state dated before the
    Jul 19 2026 base-amount change that omits the field predates this field
    being tracked at all, and every such historical artifact used $3 -- that is
    a known fact, not a guess, so it is used instead of failing the report.

    A retained blocked/partial-submission morning/midday artifact (see
    ``_has_submitted_evidence``) never populates ``budget.base_amount_usd`` --
    ``build_artifacts()`` records the ladder's base amount at
    ``ladder.base_amount_usd`` instead. Without this fallback, admitting one of
    these artifacts into ``build_graded_vs_fixed()`` raised here and aborted the
    entire weekly dashboard build (Codex review, PR #538 discussion_r3837514831:
    "Normalize the base amount for retained partial runs").
    """
    budget = rs.get("budget") or {}
    # Not `or {}`: that only substitutes for an ABSENT/falsy ladder -- a
    # truthy non-dict value (e.g. a JSON array, from a structurally
    # rejected retained artifact this path deliberately admits) survives
    # it and reaches `ladder.get(...)` on the next line, raising
    # AttributeError. And because dict.get()'s default argument is
    # evaluated eagerly regardless of whether the primary key is found,
    # that AttributeError fires even when budget.base_amount_usd is
    # already present and valid -- the malformed ladder never needed to be
    # consulted at all (Codex review, PR #654: "Coerce malformed ladder
    # containers before base lookup").
    ladder = rs.get("ladder")
    if not isinstance(ladder, dict):
        ladder = {}
    raw = budget.get("base_amount_usd", budget.get("base_buy_amount_usd", ladder.get("base_amount_usd")))
    trading_day = rs.get("trading_day", "?")
    if raw is None:
        if isinstance(trading_day, str) and trading_day < _BASE_AMOUNT_CHANGE_DATE:
            return _PRE_CHANGE_BASE_AMOUNT_USD
        raise ValueError(
            f"run_state for trading_day={trading_day!r} is missing "
            "budget.base_amount_usd/base_buy_amount_usd; refusing to guess a "
            "base DCA amount for the fixed-baseline comparison."
        )
    try:
        numeric_raw = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"run_state for trading_day={trading_day!r} has a non-numeric "
            f"budget.base_amount_usd/base_buy_amount_usd: {raw!r}"
        ) from None
    # float("NaN")/float("Infinity") succeed, so a non-finite base amount
    # -- a standard-JSON-safe string this path's own precheck (see the
    # "blocked" branch above) admits as "present" -- silently reached the
    # fixed-baseline comparison and rendered $nan/$inf totals instead of
    # being treated as the genuinely corrupted artifact it is, exactly
    # like a non-numeric value already is (Codex review, PR #654: "Reject
    # non-finite bases in retained blocked states").
    if not math.isfinite(numeric_raw):
        raise ValueError(
            f"run_state for trading_day={trading_day!r} has a non-finite "
            f"budget.base_amount_usd/base_buy_amount_usd: {raw!r}"
        )
    return numeric_raw


def _this_run_submitted_notional(rs: Dict[str, Any]) -> float:
    """Sum the notional this run's OWN rows actually submitted.

    ``budget.hypothetical_day_spend_usd`` on a retained blocked/partial
    morning/midday artifact is ``ledger.day_planned(trading_day)`` -- a
    CUMULATIVE running total for the whole trading day, seeded from any prior
    same-day allocations by ``_compute_top_up()`` (see ``build_artifacts()`` in
    morning_dca.py). Using it as "this run's executed notional" double-counts:
    an earlier same-day run's own submitted amount is both baked into that
    cumulative figure AND separately contributed by that earlier run's own
    run_state artifact, which is also present in the dashboard's artifact set.

    Instead, sum only the rows THIS run actually submitted: ``buy_candidates``
    entries whose status is in ``_REAL_ORDER_STATUSES`` carry their real
    placed amount at ``planned_amount_usd`` (morning_dca.py's
    ``build_artifacts()`` writes ``oc["submitted_amount_usd"]`` there for a
    submitted outcome). Fall back to ``broker_outcomes`` for a ticker with a
    placed outcome but no matching ``buy_candidates`` row (defensive; normal
    artifacts always pair the two).

    Checks the SAME ``_REAL_ORDER_STATUSES`` set as ``_has_submitted_evidence``
    rather than the exact ``"submitted"`` string: a restored same-slot retry row
    carrying "filled"/"pending"/"unconfirmed" is admitted by that function as
    real submission evidence, but summing only literal "submitted" rows here
    reported $0 executed/spent for that same real order (Codex review, PR #538:
    "Include every placed status in submitted notional").

    Deduplicates the ``broker_outcomes`` fallback by ORDER IDENTITY
    (``order_id`` when present, else ticker), not by ticker alone: a retry can
    carry a bare-ticker-keyed ``broker_outcomes`` entry for a DIFFERENT order on
    the same ticker already counted from ``buy_candidates`` (the afternoon merge
    keys a second same-ticker order ``"AAA:<order-id>"`` instead -- see
    ``_submitted_keys()`` in morning_dca.py for the same convention). Skipping
    by ticker alone either dropped that distinct order's notional entirely (bare
    ``"AAA"`` key, same ticker already seen) or double-counted an order already
    summed from buy_candidates (a synthetic ``"AAA:<order-id>"`` recovery key,
    which never matches a bare ticker in ``seen_tickers``) (Codex review, PR
    #654: "Reconcile same-ticker outcomes before skipping them").
    """
    total = 0.0
    seen_order_identities: set[str] = set()
    for c in rs.get("buy_candidates") or []:
        if isinstance(c, dict) and c.get("status") in _REAL_ORDER_STATUSES:
            # A missing/non-numeric planned_amount_usd must NOT mark this
            # order "seen" -- _num()'s 0.0 fallback is indistinguishable
            # from a genuine $0 amount, and marking it seen here skipped the
            # broker_outcomes fallback below even when THAT entry carries
            # the real submitted_amount_usd, silently reporting a real
            # order as zero executed notional (Codex review, PR #538:
            # "Fall back when the candidate notional is absent").
            try:
                raw_amount = c.get("planned_amount_usd")
                if raw_amount is None:
                    continue
                amount = float(raw_amount)
            except (TypeError, ValueError):
                continue
            total += amount
            order_id = c.get("order_id")
            ticker = c.get("ticker")
            identity = order_id if isinstance(order_id, str) and order_id else ticker
            if isinstance(identity, str):
                seen_order_identities.add(identity)
    broker_outcomes = rs.get("broker_outcomes")
    if isinstance(broker_outcomes, dict):
        for key, outcome in broker_outcomes.items():
            if not (isinstance(outcome, dict) and outcome.get("status") in _REAL_ORDER_STATUSES):
                continue
            order_id = outcome.get("order_id")
            identity = order_id if isinstance(order_id, str) and order_id else key
            if identity in seen_order_identities:
                continue
            total += _num(outcome.get("submitted_amount_usd"))
            # Two broker_outcomes entries for the SAME physical order (a
            # synthetic recovery key alongside the bare-ticker key, same
            # order_id, neither represented by a candidate row) both pass
            # the membership check above unless this loop's own summed
            # identities are tracked too -- seen_order_identities was only
            # ever populated by the buy_candidates pass, so a second
            # outcome-only representation of one order was never
            # recognized as already counted (Codex review, PR #654:
            # "Record broker-only identities after summing them").
            if isinstance(identity, str):
                seen_order_identities.add(identity)
    return total


def _state_week_notional(data: Dict[str, Any]) -> Optional[float]:
    """A state's own cumulative week-to-date executed notional, or ``None``.

    ``budget.week_notional_usd`` first; morning/midday/market_open/intraday
    carry the same cumulative under ``budget.hypothetical_week_spend_usd``
    (Codex review, PR #538: "Normalize morning and midday cap fields in
    retained runs"). ``None`` means "this state reports no weekly total at
    all" -- distinct from a genuine $0, so the caller can skip it and keep
    looking rather than treating an absent field as a correction to zero.
    """
    budget = data.get("budget")
    if not isinstance(budget, dict):
        return None
    value = budget.get("week_notional_usd")
    if value is None:
        value = budget.get("hypothetical_week_spend_usd")
    if value is None:
        return None
    # Not _num(): its 0.0 fallback for a non-numeric/non-finite value is
    # indistinguishable from a genuine $0 report, and _latest_week_value()
    # treats ANY non-None return as "this state reports a value, and it's
    # the latest" -- a malformed Tuesday value would silently overwrite
    # Monday's valid $100 total with $0 instead of being skipped so the
    # reducer keeps looking for the last actually-usable total (Codex
    # review, PR #654: "Preserve prior totals when the latest weekly value
    # is malformed").
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric_value):
        return None
    return numeric_value


def _state_weekly_cap(data: Dict[str, Any]) -> Optional[float]:
    """A state's own weekly notional cap, or ``None`` when it reports none.

    The same three-way lookup ``build_performance_snapshot()`` has always used:
    the current ``budget.weekly_cap_usd``, the pre-policy
    ``weekly_notional_cap_usd`` historical artifacts carry, then the
    stream-specific ``*_weekly_cap_usd`` key a retained morning/midday/
    market_open/intraday partial writes instead (see
    ``_STREAM_WEEKLY_CAP_KEYS``). ``None`` means "this state reports no weekly
    cap at all", kept distinct from a genuine $0 so :func:`_latest_week_value`
    can skip it and keep looking rather than reading an absent cap as a cap of
    zero -- the same absent-is-not-zero rule :func:`_state_week_notional`
    follows.

    The ``or`` chain between the two generic keys is deliberate and predates
    this helper: a literal ``0.0`` under ``weekly_cap_usd`` still falls through
    to ``weekly_notional_cap_usd``.
    """
    budget = data.get("budget")
    if not isinstance(budget, dict):
        return None
    # Each candidate is checked for finiteness with _finite_float() BEFORE
    # falling through to the next one -- a truthy but malformed
    # weekly_cap_usd (e.g. "bad"/"NaN") used to short-circuit the `or`
    # chain and mask a perfectly usable weekly_notional_cap_usd or
    # stream-specific fallback (e.g. morning_weekly_cap_usd), reporting no
    # cap at all for a state that actually had one available (Codex
    # review, PR #654: "Fall through malformed generic weekly caps"). Not
    # _num(): see _state_week_notional()'s identical guard -- its 0.0
    # fallback for a non-numeric/non-finite cap is indistinguishable from a
    # genuine $0 report, and _latest_week_value() would treat that as the
    # latest reported cap, overwriting an earlier valid one.
    numeric_value = _finite_float(budget.get("weekly_cap_usd"))
    if numeric_value is None:
        numeric_value = _finite_float(budget.get("weekly_notional_cap_usd"))
    if numeric_value is None:
        stream_value = _first_present(budget, _STREAM_WEEKLY_CAP_KEYS)
        numeric_value = _finite_float(stream_value)
    return numeric_value


def _latest_week_value(
    run_states: Sequence[Dict[str, Any]],
    value_of: Callable[[Dict[str, Any]], Optional[float]],
) -> float:
    """Reduce ``run_states`` to the latest ISO week's last reported ``value_of``.

    The shared engine behind :func:`_latest_week_notional` and
    :func:`_latest_week_weekly_cap`. Both figures are rendered side by side in
    one "Week notional / cap" cell, so they MUST be selected by the same week
    and the same chronological ordering -- reducing the notional to the latest
    week while still taking the cap's maximum across the whole historical tree
    rendered the current week's spend against an older, larger week's cap and
    understated utilization (Codex review, PR #593: "Match the weekly cap to
    the selected week").

    Ordering is ``_accumulation_sort_key``, the same key
    ``load_json_files()``'s accumulation pass walks, so the reduction and the
    normalization that produced these values cannot disagree about which state
    came last.

    Which states belong to "the latest week" follows that pass exactly, rather
    than inventing a second rule. The pass tracks a running ISO week and leaves
    it UNTOUCHED for a state whose week cannot be determined, so such a state
    accumulates into the last known week; ``_accumulation_sort_key``
    additionally sorts every undated state after every dated one, stably, and
    never drops it. Both rules are reproduced here by carrying a running week
    forward across undated states -- so a retained blocked submission with no
    usable ``trading_day``/``iso_week``, whose cumulative total
    ``load_json_files()`` has already normalized into the latest week
    ($100 + a $5 blocked submission = $105), is the state that wins, instead of
    being discarded merely because some other state happens to be dated
    (Codex review, PR #593: "Include undated retained states in the latest
    week").

    Two deliberate edges, unchanged from the notional-only version:

    * A state reporting NO value at all is skipped rather than read as $0 --
      the last state of the week that actually reports one wins.
    * When not a single state can be placed in an ISO week, there is no
      "latest week" to select and the historic raise-only ``max()`` is used
      unchanged, so an undated corpus reports exactly what it always did.
    """
    ordered = sorted(run_states, key=_accumulation_sort_key)
    latest_week: Optional[Tuple[int, int]] = None
    for rs in ordered:
        week = _iso_week_of_state(rs)
        if week is not None:
            latest_week = week
    if latest_week is None:
        # Nothing is placeable in a week at all -- keep the historic
        # raise-only reduction rather than picking an arbitrary state.
        values = [v for v in (value_of(rs) for rs in run_states) if v is not None]
        return max(values, default=0.0)
    running_week: Optional[Tuple[int, int]] = None
    latest_value: Optional[float] = None
    for rs in ordered:
        week = _iso_week_of_state(rs)
        if week is not None:
            running_week = week
        if running_week != latest_week:
            continue
        value = value_of(rs)
        if value is not None:
            latest_value = value
    return latest_value if latest_value is not None else 0.0


def _latest_week_weekly_cap(run_states: Sequence[Dict[str, Any]]) -> float:
    """The weekly cap to render beside :func:`_latest_week_notional`'s total.

    See :func:`_latest_week_value` for why this is a per-week selection and no
    longer the maximum cap across every historical state: the cap policy
    changes over time (an older $1,200-cap week followed by the current
    $900-cap one), and ``load_scheduled_run_states()`` loads the COMPLETE
    historical tree.
    """
    return _latest_week_value(run_states, _state_weekly_cap)


def _latest_week_notional(run_states: Sequence[Dict[str, Any]]) -> float:
    """The week-to-date executed notional to report in the snapshot.

    Codex review, PR #593: "Use the corrected latest weekly total in the
    snapshot". This reduction used to be a plain ``max()`` across every state,
    which is correct only while the figure moves in one direction.
    ``load_json_files()`` now LOWERS a state's ``week_notional_usd`` when a
    whole-share settlement adjustment corrects the week downward (Tuesday's
    $100 becoming $98 after a -$2 correction) -- and ``max()`` then let
    Monday's stale pre-correction $100 win, so the corrected figure never
    reached the user-visible total.

    The rule is therefore "the LATEST cumulative value for the latest week
    present", not "the largest anywhere". ``week_notional_usd`` is cumulative
    week-to-date by construction -- both on an ordinary completed run and on a
    retained partial normalized by ``load_json_files()``'s running accumulator
    -- so the chronologically last state of a week already carries everything
    earlier states of that week contributed, and taking it (rather than the
    maximum) is what lets a downward correction survive.

    Week selection, ordering, the absent-is-not-$0 rule, and the
    no-placeable-week ``max()`` fallback all live in
    :func:`_latest_week_value`, shared with :func:`_latest_week_weekly_cap` so
    the total and the cap it is rendered against can never be selected from
    different weeks.

    Only the CURRENT week's correction is in scope here. A delta belonging to
    an already-passed week is still excluded upstream by
    ``_current_week_claim_amount``; nothing about that decision changes.
    """
    return _latest_week_value(run_states, _state_week_notional)


def build_performance_snapshot(run_states: Sequence[Dict[str, Any]]) -> str:
    days = len(run_states)
    # ⚡ Bolt Optimization: Replace multiple O(N) generator/comprehension scans with a
    # single pass over run_states to compute all aggregates simultaneously.
    total_candidates = 0.0
    total_submitted = 0.0
    total_notional = 0.0
    planned_notional = 0.0
    week_notional = 0.0  # assigned by _latest_week_notional() after the pass below
    weekly_cap = 0.0  # assigned by _latest_week_weekly_cap() after the pass below

    for rs in run_states:
        counts = rs.get("counts") or {}
        budget = rs.get("budget") or {}

        total_candidates += _num(counts.get("buy_candidates"))
        # Prefer the ordinary completed-run field names (counts.submitted /
        # budget.day_notional_usd), but the blocked/partial-submission
        # artifacts _has_submitted_evidence() lets through above (a later
        # candidate's raw persistence failed after an earlier one was
        # placed) never populate those -- morning/midday's
        # _blocked_live_result()/_apply_blocked_before_placement_fields()
        # record the real values under the newer top-level
        # orders_submitted_count and (cumulatively, see
        # _this_run_submitted_notional) budget.hypothetical_day_spend_usd
        # instead. Without this fallback, a partial run with a genuine
        # submitted order still counted toward run days/candidates above
        # but was aggregated as zero orders and zero notional here (Codex
        # review, PR #538 discussion_r3837489854: "Normalize retained
        # partial runs before dashboard aggregation").
        submitted_count = counts.get("submitted")
        if submitted_count is None:
            submitted_count = rs.get("orders_submitted_count")
        total_submitted += _num(submitted_count)
        day_notional = budget.get("day_notional_usd")
        if day_notional is None:
            # budget.hypothetical_day_spend_usd is the day's CUMULATIVE planned
            # spend (ledger.day_planned), not this run's own contribution -- a
            # same-day prior run's amount is baked into it and would be
            # double-counted against that prior run's own artifact, also
            # present in run_states. Derive this run's own submitted notional
            # from its own rows instead (Codex review, PR #538 discussion
            # r3837514830: "Derive partial-run notional from submitted rows").
            day_notional = _this_run_submitted_notional(rs)
        total_notional += _num(day_notional)

        for c in rs.get("buy_candidates") or []:
            planned_notional += _num(c.get("planned_amount_usd"))

    # Both weekly figures are reduced the SAME way -- latest reported value of
    # the latest ISO week -- so the "Week notional / cap" cell can never pair
    # this week's spend with a different week's cap (Codex review, PR #593:
    # "Match the weekly cap to the selected week"). The per-state cap lookup
    # itself (weekly_cap_usd / weekly_notional_cap_usd / the stream-specific
    # key) moved verbatim into _state_weekly_cap(); the daily cap rendered by
    # build_budget_reserve_effectiveness() is per-ROW, never reduced across
    # states, so it has no equivalent mismatch.
    week_notional = _latest_week_notional(run_states)
    weekly_cap = _latest_week_weekly_cap(run_states)

    lines = ["## Performance snapshot", ""]
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Run days | {days} |")
    lines.append(f"| Buy candidates | {int(total_candidates)} |")
    lines.append(f"| Orders submitted | {int(total_submitted)} |")
    lines.append(f"| Planned notional | ${planned_notional:.2f} |")
    lines.append(f"| Executed notional | ${total_notional:.2f} |")
    lines.append(f"| Week notional / cap | ${week_notional:.2f} / ${weekly_cap:.2f} |")
    lines.append("")
    return "\n".join(lines)


def _candidate_fixed_base_usd(candidate: Dict[str, Any], default_base: float) -> float:
    """Return one candidate's 1x fixed-baseline dollar amount.

    Prefers the candidate's own dynamic first-allocation base
    (``selected_base_amount_usd``, $1 or $2 -- see
    ``near_close_dca.select_first_allocation_base_usd``) so the fixed baseline
    reflects the SAME per-candidate base the graded ladder actually used (Codex
    review: "Apply the dynamic base to the analytics baseline"). Falls back to
    the run's flat ``default_base`` for candidates/artifacts that predate this
    field.
    """
    raw = candidate.get("selected_base_amount_usd")
    if raw is None:
        return default_base
    try:
        base = float(raw)
    except (TypeError, ValueError):
        return default_base
    if not math.isfinite(base) or base <= 0:
        return default_base
    return base


def build_graded_vs_fixed(run_states: Sequence[Dict[str, Any]]) -> str:
    """Compare graded-ladder spend to a flat 1x fixed daily DCA baseline.

    The fixed baseline buys every candidate at its own 1x base amount (the
    dynamic $1/$2 first-allocation base when the candidate recorded one,
    otherwise the run's flat base amount); graded applies the ladder
    multiplier. The delta shows the extra capital the ladder deploys into
    deeper drops relative to a naive fixed-size DCA.
    """
    lines = ["## Graded DCA vs fixed daily DCA", ""]
    lines.append("| Day | Candidates | Graded $ | Fixed 1x $ | Delta $ |")
    lines.append("| --- | --- | --- | --- | --- |")
    graded_total = 0.0
    fixed_total = 0.0
    for rs in run_states:
        candidates = rs.get("buy_candidates", [])
        # A retained partial-submission state (status=="blocked", kept by
        # load_json_files/_has_submitted_evidence for its real order
        # evidence) can be structurally partial -- _existing_submitted_
        # run_state() only validates its buy_candidates/broker_outcomes
        # containers, so it may genuinely LACK budget.base_amount_usd
        # entirely. Isolate exactly that missing-field case from THIS
        # section (whose fixed-baseline comparison needs the field) rather
        # than aborting the whole weekly report; every other section still
        # counts its real order (Codex review: "Normalize retained partial
        # states before dashboard rendering"). A blocked state whose
        # base_amount_usd is PRESENT but non-numeric is a genuinely
        # corrupted artifact, not the supported missing-field case -- that
        # must still fail fast via _require_base_amount() below rather than
        # silently disappearing from this comparison (Codex review: "Keep
        # rejecting malformed base amounts in blocked states"). A morning/
        # midday retained state instead records the ladder's base amount at
        # ladder.base_amount_usd (Codex review, PR #538 discussion_
        # r3837514831: "Normalize the base amount for retained partial
        # runs") -- checked here too so that fallback isn't short-circuited
        # by this precheck before _require_base_amount() ever runs.
        if rs.get("status") == "blocked":
            budget = rs.get("budget") or {}
            # Not `rs.get("ladder") or {}`: same hazard as the identical
            # precheck inside _require_base_amount() itself -- a truthy
            # non-dict ladder survives `or {}` and this call site runs
            # BEFORE _require_base_amount() ever gets a chance to apply
            # its own guard, so that fix alone couldn't cover this
            # earlier ladder.get() (Codex review, PR #654: "Sanitize
            # ladder before the blocked-state precheck").
            ladder = rs.get("ladder")
            if not isinstance(ladder, dict):
                ladder = {}
            trading_day = rs.get("trading_day")
            # A pre-change (before _BASE_AMOUNT_CHANGE_DATE) blocked state
            # missing BOTH fields is exactly the case _require_base_amount()
            # itself resolves via the documented $3 historical fallback,
            # not the genuinely-unsupported "missing field" case this
            # precheck exists to skip without raising -- unconditionally
            # `continue`-ing here dropped that state's real submitted rows
            # from both graded and fixed totals instead of ever reaching
            # that fallback (Codex review, PR #654: "Preserve the known
            # pre-change base for blocked states").
            if (
                budget.get("base_amount_usd", budget.get("base_buy_amount_usd")) is None
                and ladder.get("base_amount_usd") is None
                and not (isinstance(trading_day, str) and trading_day < _BASE_AMOUNT_CHANGE_DATE)
            ):
                continue
        base = _require_base_amount(rs)
        # ⚡ Bolt Optimization: Replace multiple O(N) generator passes with a
        # single manual loop over candidates to aggregate graded and fixed amounts.
        graded = 0.0
        fixed = 0.0
        candidate_count = 0
        for c in candidates:
            # A retained blocked artifact's buy_candidates now keeps every
            # row -- placed or not (see load_json_files' own comment on
            # this: "Preserve unplaced candidates in retained partial
            # runs") -- so THIS section, whose graded/fixed columns
            # represent capital actually deployed via the ladder, must
            # exclude a row that explicitly carries a non-placed status
            # itself, or its planned_amount_usd would count as executed
            # spend that never happened (Codex review, PR #654). An ORDINARY
            # completed run's candidates never carry a "status" field at
            # all (every row there already IS a placed candidate by
            # construction), so this only ever excludes an explicitly-
            # tagged not-placed row from a retained artifact, never a
            # normal run's rows. A whole_share_settlement_adjustment row is
            # exempt -- its dollar delta is real deployed capital regardless
            # of its own status field.
            if (
                c.get("status") is not None
                and c.get("status") not in _REAL_ORDER_STATUSES
                and not c.get("whole_share_settlement_adjustment")
            ):
                continue
            # A whole_share_settlement_adjustment row is a bookkeeping
            # correction to an ALREADY-counted physical candidate, not a
            # second DCA candidate of its own -- counting it here (and
            # giving it its own fixed-baseline 1x allocation) inflates both
            # the candidate count and the fixed baseline for one physical
            # order (Codex review: "Exclude adjustment rows from candidate
            # analytics"). Its dollar delta is still real deployed capital,
            # so it stays in the graded total.
            graded += _num(c.get("planned_amount_usd"))
            if c.get("whole_share_settlement_adjustment"):
                continue
            candidate_count += 1
            fixed += _candidate_fixed_base_usd(c, base)
        graded_total += graded
        fixed_total += fixed
        lines.append(
            f"| {rs.get('trading_day', '?')} | {candidate_count} | "
            f"${graded:.2f} | ${fixed:.2f} | ${graded - fixed:+.2f} |"
        )
    lines.append(
        f"| **Total** | | **${graded_total:.2f}** | **${fixed_total:.2f}** | "
        f"**${graded_total - fixed_total:+.2f}** |"
    )
    lines.append("")
    return "\n".join(lines)


def build_sector_matrix(
    run_states: Sequence[Dict[str, Any]],
    sector_map: Optional[Dict[str, str]] = None,
) -> str:
    """Aggregate planned notional and candidate counts by sector.

    ``sector_map`` maps ticker -> sector; unmapped tickers fall into 'Unclassified'.
    """
    sector_map = sector_map or {}
    by_sector: Dict[str, Dict[str, float]] = {}
    for rs in run_states:
        for candidate in rs.get("buy_candidates", []):
            ticker = candidate.get("ticker", "?")
            sector = sector_map.get(ticker, "Unclassified")
            bucket = by_sector.setdefault(sector, {"candidates": 0.0, "notional": 0.0})
            # See build_graded_vs_fixed's identical exclusion (Codex review:
            # "Exclude adjustment rows from candidate analytics") -- a
            # settlement-adjustment row corrects an already-counted
            # candidate, not a second one, but its dollar delta is still
            # real deployed capital worth keeping in the notional total.
            if not candidate.get("whole_share_settlement_adjustment"):
                bucket["candidates"] += 1
            bucket["notional"] += _num(candidate.get("planned_amount_usd"))

    lines = ["## Sector performance matrix", ""]
    lines.append("| Sector | Candidates | Planned $ |")
    lines.append("| --- | --- | --- |")
    if by_sector:
        for sector in sorted(by_sector):
            bucket = by_sector[sector]
            lines.append(f"| {sector} | {int(bucket['candidates'])} | ${bucket['notional']:.2f} |")
    else:
        lines.append("| _no candidates_ | 0 | $0.00 |")
    lines.append("")
    return "\n".join(lines)


def build_budget_reserve_effectiveness(run_states: Sequence[Dict[str, Any]]) -> str:
    lines = ["## Budget and reserve effectiveness", ""]
    lines.append("| Day | Tier | Spent $ | Daily cap $ | Rollover reserve $ | Remaining $ |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for rs in run_states:
        budget = rs.get("budget", {})
        # Fall back to the pre-policy keys so historical run_state artifacts (which
        # named these ``daily_base_cap_usd`` / ``carry_forward_reserve_usd``) still
        # render populated caps and reserve figures.
        daily_cap = budget.get("daily_cap_usd")
        if daily_cap is None:
            daily_cap = budget.get("daily_base_cap_usd")
        if daily_cap is None:
            # A morning/midday/market_open/intraday artifact carries its cap
            # under its OWN stream-specific key instead (Codex review, PR
            # #538: "Normalize morning and midday cap fields in retained
            # runs").
            daily_cap = _first_present(budget, _STREAM_DAILY_CAP_KEYS)
        reserve = budget.get("weekly_rollover_reserve_usd")
        if reserve is None:
            reserve = budget.get("carry_forward_reserve_usd")
        # A retained blocked/partial-submission morning/midday artifact (see
        # _has_submitted_evidence) never populates budget.day_notional_usd --
        # its real submitted spend lives only in its own submitted
        # buy_candidates/broker_outcomes rows. Without this fallback the
        # budget table reported $0.00 spent for a day that had a genuine
        # submitted order, even though build_performance_snapshot()'s own
        # notional total (fixed by the same normalization) reported it
        # correctly (Codex review, PR #538 discussion_r3837630719:
        # "Normalize partial runs for the budget table"). Reuses
        # _this_run_submitted_notional() rather than re-deriving the same
        # submitted-rows sum a second time.
        day_notional = budget.get("day_notional_usd")
        if day_notional is None:
            day_notional = _this_run_submitted_notional(rs)
        remaining = budget.get("remaining_spend_today_usd")
        if remaining is None:
            # morning/midday/market_open/intraday name this remaining_daily_usd
            # instead (Codex review, PR #538: "Normalize morning and midday cap
            # fields in retained runs").
            remaining = budget.get("remaining_daily_usd")
        # A retained state's budget fields are as untrusted as its
        # buy_candidates -- _num()'s float() conversion accepts "NaN"/
        # "Infinity" strings without complaint, so without this guard a
        # non-finite value here rendered as literal $nan/$inf in the table
        # while every other retained financial field in this module was
        # already sanitized (Codex review, PR #654: "Reject non-finite
        # retained budget fields"). _finite_float() maps a non-finite value
        # to None first so _num() falls back to its normal $0.00 for a
        # genuinely missing field.
        lines.append(
            f"| {rs.get('trading_day', '?')} | {budget.get('budget_tier') or 'n/a'} | "
            f"${_num(_finite_float(day_notional)):.2f} | "
            f"${_num(_finite_float(daily_cap)):.2f} | "
            f"${_num(_finite_float(reserve)):.2f} | "
            f"${_num(_finite_float(remaining)):.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_execution_quality(summaries: Sequence[Dict[str, Any]]) -> str:
    lines = ["## Execution quality", ""]
    lines.append("| Day | Priced fills | Missing target | Avg slip % | Max slip % | Worst |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    all_slips: List[float] = []
    for summary in summaries:
        exq = summary.get("execution_quality") or {}
        avg = exq.get("avg_slippage_pct")
        if avg is not None:
            all_slips.append(avg)
        avg_str = f"{avg}%" if avg is not None else "n/a"
        max_slip = exq.get("max_slippage_pct")
        max_slip_str = f"{max_slip}%" if max_slip is not None else "n/a"
        worst = exq.get("worst_ticker")
        worst_str = worst if worst is not None else "n/a"
        lines.append(
            f"| {summary.get('trading_day', '?')} | {exq.get('priced_fills', 0)} | "
            f"{exq.get('target_price_missing', 0)} | {avg_str} | "
            f"{max_slip_str} | {worst_str} |"
        )
    week_avg = round(sum(all_slips) / len(all_slips), 4) if all_slips else None
    week_avg_str = f"{week_avg}%" if week_avg is not None else "n/a"
    lines.append(f"| **Week avg** | | | **{week_avg_str}** | | |")
    lines.append("")
    return "\n".join(lines)


def build_coverage_missed(
    run_states: Sequence[Dict[str, Any]],
    summaries: Sequence[Dict[str, Any]],
) -> str:
    lines = ["## Coverage and missed opportunities", ""]
    lines.append("| Day | Candidates | Fills | Coverage % | Missing | Manual review |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    summary_by_day = {s.get("trading_day"): s for s in summaries}
    for rs in run_states:
        day = rs.get("trading_day")
        summary = summary_by_day.get(day) or {}
        cov = summary.get("coverage") or {}
        manual = rs.get("manual_review_tickers") or []

        candidates = cov.get("buy_candidates", (rs.get("counts") or {}).get("buy_candidates", 0))
        fills = cov.get("fills")
        fills_str = str(fills) if fills is not None else "n/a"
        coverage = cov.get("coverage_pct")
        coverage_str = f"{coverage}%" if coverage is not None else "n/a"
        missing = cov.get("missing_fills")
        missing_str = str(missing) if missing is not None else "n/a"

        lines.append(
            f"| {day or '?'} | {candidates} | "
            f"{fills_str} | {coverage_str} | "
            f"{missing_str} | {len(manual)} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_weekly_review_notes(
    run_states: Sequence[Dict[str, Any]],
    summaries: Sequence[Dict[str, Any]],
) -> str:
    """Auto-generate review prompts from the week's aggregates."""
    lines = ["## Weekly review notes", ""]
    # ⚡ Bolt Optimization: Use a single pass over summaries and run_states
    # instead of multiple O(N) generators to reduce iteration overhead.
    total_missing = 0.0
    missing_targets = 0.0
    for s in summaries:
        cov = s.get("coverage") or {}
        exq = s.get("execution_quality") or {}
        total_missing += _num(cov.get("missing_fills"))
        missing_targets += _num(exq.get("target_price_missing"))

    total_manual = 0
    for rs in run_states:
        total_manual += len(rs.get("manual_review_tickers") or [])

    lines.append(f"- Runs analyzed: {len(run_states)} day(s).")
    if total_missing:
        lines.append(f"- {int(total_missing)} buy candidate(s) had no fill — review broker rejects / caps.")
    else:
        lines.append("- All buy candidates were covered by fills.")
    if missing_targets:
        lines.append(
            f"- {int(missing_targets)} fill(s) lacked a target price; slippage excluded — check signal capture."
        )
    if total_manual:
        lines.append(f"- {total_manual} manual-review flag(s) across the week — reconcile finance vs Robinhood.")
    lines.append("- Add qualitative notes here before the Notion dashboard refresh.")
    lines.append("")
    return "\n".join(lines)


def build_weekly_dashboard(
    run_states: Sequence[Dict[str, Any]],
    summaries: Optional[Sequence[Dict[str, Any]]] = None,
    sector_map: Optional[Dict[str, str]] = None,
    week_label: Optional[str] = None,
) -> str:
    """Render the full weekly Notion dashboard markdown from accumulated artifacts."""
    summaries = list(summaries or [])
    title = f"# Weekly Performance Dashboard{f' — {week_label}' if week_label else ''}"
    sections = [
        title,
        "",
        build_performance_snapshot(run_states),
        build_graded_vs_fixed(run_states),
        build_sector_matrix(run_states, sector_map),
        build_budget_reserve_effectiveness(run_states),
        build_execution_quality(summaries),
        build_coverage_missed(run_states, summaries),
        build_weekly_review_notes(run_states, summaries),
    ]
    return "\n".join(sections)


def write_weekly_dashboard(
    run_states: Sequence[Dict[str, Any]],
    output_dir: str | Path,
    summaries: Optional[Sequence[Dict[str, Any]]] = None,
    sector_map: Optional[Dict[str, str]] = None,
    week_label: Optional[str] = None,
) -> str:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "weekly_performance_dashboard.md"
    atomic_write_text(path, build_weekly_dashboard(run_states, summaries, sector_map, week_label))
    return str(path)
