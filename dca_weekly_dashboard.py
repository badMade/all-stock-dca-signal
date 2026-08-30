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


def _finite_ordinary_amount(value: Any, is_adjustment: bool) -> Optional[float]:
    """``_finite_float(value)``, additionally rejecting a NEGATIVE amount for
    an ordinary (non-adjustment) order.

    A whole_share_settlement_adjustment's dollar delta is legitimately
    negative (a bookkeeping correction reducing a prior order's notional),
    but an ordinary order's own fill amount never is -- the schema requires
    planned_amount_usd to be nonnegative, and this same guard already exists
    in the blocked-state amount reconciliation elsewhere in this module. The
    cross-file dedup pass's own removal-credit/keeper-merge paths read
    ``submitted_amount_usd`` without it, so a corrupted later variant
    reporting a negative amount for an ORDINARY duplicate got credited and
    merged into the keeper as-is, corrupting its retained fill and
    cumulative totals with a negative dollar figure (Codex review, PR #654:
    "Reject negative ordinary fill corrections").
    """
    amount = _finite_float(value)
    if amount is not None and amount < 0 and not is_adjustment:
        return None
    return amount


def _states_share_run_identity(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Whether two states are variants of the SAME physical scheduled run.

    trading_day alone is not precise enough: distinct scheduled runs on the
    SAME calendar day (e.g. the morning and afternoon streams) have
    different trading_day values in the common case, but nothing stops a
    later artifact-format/recovery variant from repeating an order across
    them if they ever shared one -- and more importantly, two GENUINELY
    different same-day runs must never be treated as "the same run" just
    because their trading_day happens to match. The top-level ``run_id``
    (schemas/run_state.schema.json: "Trading day plus HHMMSS, the artifact
    build id") is too precise for this purpose -- a preserved/retry variant
    of the SAME run can legitimately carry a different HHMMSS. Preferred
    instead: ``run_classification.run_id``, documented as "the origin-scoped
    id used by reporting to include/exclude runs" -- the field this exact
    kind of run identity comparison is meant for. Falls back to trading_day
    alone when either state lacks a usable run_classification.run_id (older
    artifacts), preserving prior behavior for those (Codex review, PR #654:
    "Use run identity before collapsing same-day states").
    """
    if a.get("trading_day") != b.get("trading_day"):
        return False
    _a_rc = a.get("run_classification")
    _b_rc = b.get("run_classification")
    _a_run_id = _a_rc.get("run_id") if isinstance(_a_rc, dict) else None
    _b_run_id = _b_rc.get("run_id") if isinstance(_b_rc, dict) else None
    if isinstance(_a_run_id, str) and _a_run_id and isinstance(_b_run_id, str) and _b_run_id:
        return _a_run_id == _b_run_id
    # Codex review, PR #654: "Preserve distinct same-day streams without run
    # IDs". Falling straight back to "same trading_day means same run" let
    # the husk collapse drop a genuinely different scheduled run of that day
    # -- e.g. an afternoon run whose only rows were recovery evidence for a
    # morning order -- taking its run day, coverage and status data with it.
    # ``run_classification.stream`` ("morning"/"midday"/"intraday"/
    # "afternoon"/...) is the same field near_close_dca.py and
    # morning_dca.py use to tell one slot's artifacts from another's, so
    # when BOTH sides carry one and they disagree, these are provably
    # different runs. Only ever used to REJECT: a missing or matching stream
    # falls through to the historical trading_day-only behaviour, since a
    # shared stream on one day is not by itself proof of a shared run (the
    # intraday stream fires three times a day).
    _a_stream = _a_rc.get("stream") if isinstance(_a_rc, dict) else None
    _b_stream = _b_rc.get("stream") if isinstance(_b_rc, dict) else None
    if isinstance(_a_stream, str) and _a_stream and isinstance(_b_stream, str) and _b_stream:
        if _a_stream != _b_stream:
            return False
    return True


def _states_share_explicit_run_identity(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """:func:`_states_share_run_identity` WITHOUT its trading_day-only fallback.

    Dropping a state that has been emptied of every row is safe on the looser
    evidence a shared ``trading_day`` gives: there is nothing left in it to
    lose. MERGING two states that both still hold rows is not -- it moves real
    orders out of one run and reports them under another, so it demands
    positive proof that the two really are variants of ONE physical run, not
    merely two runs that happen to fall on the same calendar day. Both sides
    must therefore carry a usable ``run_classification.run_id`` and agree on
    it (Codex review, PR #654 discussion_r3853577540: "Merge partial variants
    before counting run days", whose scenario is explicitly two variants
    "under the same ``run_classification.run_id``").
    """
    if a.get("trading_day") != b.get("trading_day"):
        return False
    _a_rc = a.get("run_classification")
    _b_rc = b.get("run_classification")
    _a_run_id = _a_rc.get("run_id") if isinstance(_a_rc, dict) else None
    _b_run_id = _b_rc.get("run_id") if isinstance(_b_rc, dict) else None
    if not (isinstance(_a_run_id, str) and _a_run_id):
        return False
    if not (isinstance(_b_run_id, str) and _b_run_id):
        return False
    return _a_run_id == _b_run_id

# Ranks a broker_outcomes entry's own placement status by how authoritative
# its amount is, highest first. Used only when two DIFFERENT outcome entries
# in the SAME state's broker_outcomes dict report the same order_id (a
# provisional "submitted" confirmation followed later by the "filled" one
# once the fill price is known) -- picking whichever entry is encountered
# first in dict iteration order can select the stale provisional figure over
# a later, more authoritative one (Codex review, PR #654: "Prefer the
# authoritative duplicate outcome amount"). A status absent from this map
# ranks below every real status.
_OUTCOME_STATUS_RANK = {"filled": 3, "submitted": 2, "pending": 1, "unconfirmed": 0}

# run_state ``generated_at`` is documented (schemas/run_state.schema.json) as an
# ISO-8601 timestamp in America/New_York, and the live writers emit it both
# naively ("2026-07-23T10:30:00") and offset-qualified depending on the path.
# Anchoring a naive value to ET rather than to the reader's local zone keeps the
# two forms mutually comparable.
_EASTERN = ZoneInfo("America/New_York")


def _is_settlement_adjustment(source: Optional[Dict[str, Any]]) -> bool:
    """Whether ``source`` is genuinely flagged as a
    ``whole_share_settlement_adjustment`` row/outcome.

    A bare truthiness check on ``source.get("whole_share_settlement_
    adjustment")`` treats ANY truthy value -- including a false-looking
    but truthy string like ``"false"`` (JSON has no way to stop a
    structurally rejected artifact from carrying one) -- as an adjustment.
    Every consumer of this flag throughout this module's reconciliation
    logic uses it to EXCLUDE a row from ``counts.buy_candidates``/
    ``counts.submitted``/the fixed-DCA baseline (an adjustment is a
    bookkeeping correction to an already-counted order, not a fresh
    candidate of its own) -- so an ordinary submitted order misclassified
    this way still contributes its dollars but vanishes from every count
    that should have included it (Codex review, PR #654: "Require a
    boolean settlement-adjustment marker"). Only the literal boolean
    ``True`` activates adjustment semantics.
    """
    if source is None:
        return False
    return source.get("whole_share_settlement_adjustment") is True


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
    if not isinstance(breakdown, list):
        return False
    for item in breakdown:
        # A nonempty list whose every entry is malformed (e.g. [1], no
        # dicts at all) still passed the old `bool(breakdown)` check, so
        # it could win a claim_sources registration over another
        # representation of the same order that carries a genuinely
        # usable breakdown -- but _current_week_claim_amount() itself
        # skips every non-dict entry (see its own `if not
        # isinstance(item, dict): continue`), so a breakdown with none
        # attributes the ENTIRE remainder to the artifact's current week
        # regardless of what the malformed entries claimed (Codex review,
        # PR #654: "Require a usable entry in claim breakdowns").
        if not isinstance(item, dict):
            continue
        # A dict entry with no usable amount_usd (e.g. {}) is not enough
        # either: _current_week_claim_amount() defaults a missing
        # amount_usd to 0.0 via `float(item.get("amount_usd") or 0.0)`,
        # so such an entry contributes $0 to `accounted_usd` while still
        # being "processed" -- the entire aggregate then falls into the
        # unaccounted-remainder term and lands on the artifact's current
        # week anyway, the exact same outcome as no breakdown at all
        # (Codex review, PR #654: "Validate claim-breakdown contents
        # before preferring them"). Require an entry with an actual
        # finite numeric amount_usd -- not defaulted via `or 0.0`.
        # _finite_float(), not a bare float(): besides the finite check,
        # it also rejects a JSON boolean (bool is an int subclass, so a
        # bare float(True) succeeds as 1.0) -- an outcome carrying
        # `amount_usd: true` was treated as usable and let $1 override a
        # genuinely valid candidate breakdown's real dollar attribution
        # (Codex review, PR #654: "Reject boolean claim-breakdown
        # amounts").
        if _finite_float(item.get("amount_usd")) is not None:
            return True
    return False


def _claim_breakdown_has_dated_entry(source: Optional[Dict[str, Any]]) -> bool:
    """True when `source` carries a usable breakdown entry with a parseable ``trading_day``.

    An entry with a missing or unparseable ``trading_day`` is attributed to
    the artifact's OWN week by :func:`_current_week_claim_amount` -- the same
    outcome as no breakdown at all for that dollar. When two representations
    of the same order (a candidate row and its matched broker outcome, or two
    outcomes) both carry a usable breakdown but only one actually NAMES a
    day, preferring the dated one over the undated one preserves a real
    claim-week split instead of collapsing it back to "the whole amount
    belongs to this week" (Codex review, PR #654: "Preserve dated claim
    metadata over undated replacements").
    """
    if source is None:
        return False
    breakdown = source.get("whole_share_claim_breakdown")
    if not isinstance(breakdown, list):
        return False
    for item in breakdown:
        if not isinstance(item, dict):
            continue
        if _finite_float(item.get("amount_usd")) is None:
            continue
        claim_day = item.get("trading_day")
        if not isinstance(claim_day, str):
            continue
        try:
            date.fromisoformat(claim_day[:10])
        except ValueError:
            continue
        return True
    return False


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
        # bool is a subclass of int in Python, so a bare float(True)
        # succeeds as 1.0 -- _has_usable_claim_breakdown() rejects a
        # boolean amount_usd when deciding whether the WHOLE breakdown is
        # usable, but this per-entry loop still accepted one when it sits
        # alongside a genuinely valid entry, miscounting it into
        # accounted_usd/current_week_usd instead of skipping it into the
        # unaccounted remainder the way every other unusable entry already
        # is (Codex review, PR #654: "Reject booleans in every
        # claim-breakdown entry"). Checked before the `or 0.0` fallback so
        # a missing amount_usd still defaults to 0.0 as before.
        raw_amount = item.get("amount_usd")
        if isinstance(raw_amount, bool):
            continue
        try:
            entry_amount = float(raw_amount or 0.0)
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
    # Codex review, PR #654: "Prevent negative current-week claim
    # remainders". The "unaccounted remainder" below is meant to attribute
    # whatever the breakdown DIDN'T claim to the artifact's own current
    # week -- but a later correction can lower aggregate_amount without
    # updating the (now stale) breakdown, leaving accounted_usd larger
    # than the corrected total. An unclamped remainder then goes negative
    # and SUBTRACTS from current_week_usd, attributing negative notional
    # to a week that received none of this order. Floored at zero: an
    # over-accounted breakdown contributes nothing further to the current
    # week, rather than a spurious negative correction.
    #
    # Codex review, PR #654: "Preserve negative unaccounted adjustment
    # remainders". That floor is only correct for the ORDINARY
    # (non-negative) aggregate case it was written for -- a settlement
    # adjustment's own aggregate can be legitimately negative, and a
    # partial breakdown that only dates PART of a negative correction
    # (e.g. a -$3 adjustment whose breakdown dates only -$2 to a prior
    # week) genuinely owes the remaining -$1 to the current week. Clamping
    # that at zero silently drops the rest of a downward correction from
    # ever applying, mirroring exactly the "ordinary vs. adjustment" split
    # the cap just below already makes.
    if aggregate_amount >= 0:
        result = current_week_usd + max(0.0, aggregate_amount - accounted_usd)
    else:
        result = current_week_usd + (aggregate_amount - accounted_usd)
    # Codex review, PR #654: "Cap over-accounted current-week breakdowns".
    # Flooring the remainder alone doesn't stop `current_week_usd` ITSELF
    # from exceeding the corrected total when the stale breakdown assigns
    # more to `week_key` than the order is now actually worth (e.g. a
    # corrected $5 aggregate whose stale breakdown still books $7 to this
    # week) -- overstating this week's notional above the order's real
    # fill. Capped at aggregate_amount for the ordinary (non-negative)
    # case; a settlement adjustment's own aggregate can be legitimately
    # negative, where this cap wouldn't make sense, so it's left alone.
    if aggregate_amount >= 0:
        result = min(result, aggregate_amount)
    return result


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


def _merge_authoritative_amount_into_keeper(
    keeper_entry: Optional[
        Tuple[Optional[Tuple[int, int]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]
    ],
    new_amount: float,
    source_row: Optional[Dict[str, Any]],
    merged_into: Optional[Dict[int, Dict[str, Any]]] = None,
) -> None:
    """Propagate a later, more authoritative fill amount onto the retained keeper row.

    Cross-file dedup (see ``load_json_files``'s own cross-file dedup pass) keeps
    the EARLIEST state's row for a duplicated order_id and discards every later
    duplicate after crediting its dollar amount toward the discarding state's
    own totals -- but a later duplicate can carry a MORE authoritative amount
    than the keeper's own (e.g. an earlier retained blocked state's provisional
    $5 estimate, corrected by a later completed state's real $7 fill for the
    same order). Simply discarding the later representation after adjusting
    only the discarding state's own aggregates never propagated that
    correction back onto the keeper, so the weekly snapshot kept reporting the
    stale $5 forever (Codex review, PR #654: "Merge later fill amounts into
    the retained order"). Called once per removed duplicate whose amount is
    itself confirmed (never for a candidate row's ``planned_amount_usd``
    fallback -- see the caller's own ``_dropped_amount_is_confirmed`` gate;
    Codex review, PR #654: "Do not merge planned fallbacks as authoritative
    fills"), from both the candidate-row and outcome-row removal branches.

    ``keeper_entry`` is whatever the claimed-order-ID map stores for this
    identity: ``(claiming_state_iso_week, keeper_row_or_None,
    keeper_outcome_or_None, keeper_state)``. ``keeper_row`` and
    ``keeper_outcome`` are the keeper state's OWN buy_candidates row and
    broker_outcomes entry for this same order_id (either may be ``None`` if
    the keeper only ever represented the order on one side) -- both are
    written so a later blocked-state reconciliation pass that re-derives a
    row's amount from its OWN broker_outcomes entry doesn't find the old,
    unmerged figure still sitting there and revert this correction (Codex
    review, PR #654: "Update the keeper outcome when merging a later fill").
    The keeper's own week-to-date total is always adjusted by the same delta,
    regardless of which ISO week the correcting duplicate happened to be
    found in: the corrected order remains attributed to the KEEPER's week,
    not the correcting state's (Codex review, PR #654: "Apply cross-week
    fill corrections to the keeper total").

    ``source_row`` is the duplicate's own row/outcome dict the amount was
    read from -- used only to propagate its ``whole_share_claim_breakdown``
    (if it has a usable one) onto the keeper, so a recovered order's claim
    dates survive even when the duplicate carrying them is fully absorbed
    and dropped (Codex review, PR #654: "Merge later claim metadata into the
    retained order"). That propagation happens unconditionally, before the
    amount-equality check below returns early -- claim metadata can arrive
    on a duplicate reporting the SAME dollar amount as the keeper already
    has, and skipping it in that case left the keeper's row still
    attributed to its own (wrong) week.

    ``merged_into`` is ``load_json_files``'s state-absorption chain (see
    ``_states_merged_into``). A claimed identity's ``_KeeperEntry`` pins the
    state that FIRST claimed it, but the partial-variant merge can later fold
    that state into an earlier sibling and drop it from ``kept`` -- taking its
    ``budget`` out of every report while its ROW dicts (still updated below,
    correctly) live on inside the survivor. Applying the dollar delta to the
    dropped state left the survivor's own totals disagreeing with the rows it
    now holds. The keeper state is therefore resolved through the chain to
    whichever state actually holds these rows now (Codex review, PR #654:
    "Retarget order claims after merging their keeper state").
    """
    if keeper_entry is None:
        return
    _keeper_iso_week, _keeper_row, _keeper_outcome, _keeper_state = keeper_entry
    if merged_into and isinstance(_keeper_state, dict):
        # Bounded by the chain length: destinations always point BACKWARD in
        # the accumulation order, so the chain is acyclic by construction and
        # the guard can never be what stops the walk.
        _chain_steps = 0
        while id(_keeper_state) in merged_into and _chain_steps <= len(merged_into):
            _keeper_state = merged_into[id(_keeper_state)]
            _chain_steps += 1
    _keeper_row_dict = _keeper_row if isinstance(_keeper_row, dict) else None
    _keeper_outcome_dict = _keeper_outcome if isinstance(_keeper_outcome, dict) else None
    if not isinstance(_keeper_state, dict) or (_keeper_row_dict is None and _keeper_outcome_dict is None):
        return
    # Codex review, PR #654: "Preserve stronger keeper fills across state
    # variants" / "Rank both keeper representations before merging". A
    # duplicate's amount is only MORE authoritative than the keeper's own
    # when its own placement status is at least as strong -- a retained
    # "filled" $7 keeper must not be rewritten by a later, stale
    # "submitted"/"pending"/"unconfirmed" variant of the SAME order (a
    # provisional confirmation that simply hasn't caught up yet). The
    # keeper's OWN effective rank is the STRONGER of its row's and its
    # outcome's own status -- unconditionally preferring the outcome (as a
    # prior version of this check did) let a duplicate outrank a "filled"
    # candidate row merely by tying a WEAKER "submitted" outcome, since
    # only the outcome's rank was ever compared. The SAME stronger
    # representation is then used as the amount baseline just below, so
    # the rank check and the amount actually being defended always agree
    # with each other.
    _keeper_row_status_rank = (
        _OUTCOME_STATUS_RANK.get(str(_keeper_row_dict.get("status") or ""), -1) if _keeper_row_dict is not None else -1
    )
    _keeper_outcome_status_rank = (
        _OUTCOME_STATUS_RANK.get(str(_keeper_outcome_dict.get("status") or ""), -1) if _keeper_outcome_dict is not None else -1
    )
    _keeper_status_rank = max(_keeper_row_status_rank, _keeper_outcome_status_rank)
    # Codex review, PR #654: "Preserve metadata from lower-ranked duplicate
    # states". This used to `return` outright, which protected the keeper's
    # stronger fill amount but ALSO skipped the claim-metadata merge below --
    # contradicting this function's own contract that metadata propagates
    # unconditionally. A `filled` $7 keeper carrying no breakdown, followed
    # by a stale `submitted` $5 variant that dates the claim to the prior
    # Friday, lost those dates when the variant was dropped and kept the $7
    # attributed to the keeper's own week. The rank now gates only the AMOUNT
    # replacement: a lower-ranked duplicate keeps the keeper's existing
    # figure (set just below, once it has been resolved) while its usable
    # claim metadata is still considered.
    _source_outranks_keeper = True
    if isinstance(source_row, dict):
        _source_status_rank = _OUTCOME_STATUS_RANK.get(str(source_row.get("status") or ""), -1)
        if _source_status_rank < _keeper_status_rank:
            _source_outranks_keeper = False
    # Codex review, PR #654: "Base keeper deltas on its confirmed outcome" /
    # "Base keeper deltas on the confirmed outcome amount" / "Rank both
    # keeper representations before merging". The keeper's own
    # broker_outcomes entry is only preferred as the amount baseline when
    # its OWN status rank is at least as strong as the row's -- otherwise a
    # weaker outcome (e.g. still "submitted") could win over an already
    # "filled" candidate row purely by being checked first, contradicting
    # the rank this same function just defended above.
    _keeper_existing_amount = None
    if _keeper_outcome_dict is not None and _keeper_outcome_status_rank >= _keeper_row_status_rank:
        _keeper_existing_amount = _finite_float(_keeper_outcome_dict.get("submitted_amount_usd"))
    if _keeper_existing_amount is None and _keeper_row_dict is not None:
        _keeper_existing_amount = _finite_float(_keeper_row_dict.get("submitted_amount_usd"))
    if _keeper_existing_amount is None and _keeper_outcome_dict is not None:
        _keeper_existing_amount = _finite_float(_keeper_outcome_dict.get("submitted_amount_usd"))
    if _keeper_existing_amount is None and _keeper_row_dict is not None:
        _keeper_existing_amount = _finite_float(_keeper_row_dict.get("planned_amount_usd"))
    if _keeper_existing_amount is None:
        _keeper_existing_amount = 0.0
    if not _source_outranks_keeper:
        # Amount pinned to what the keeper already reports, so the delta
        # below is zero and only a claim-week reattribution can still move
        # anything (see the rank note above).
        new_amount = _keeper_existing_amount
    # Codex review, PR #654: "Recompute keeper week attribution when claim
    # metadata changes". The keeper's OLD week-to-date total was built using
    # its OLD claim_breakdown's own week attribution (via
    # _current_week_claim_amount, the same split the accumulator loop below
    # applies) -- snapshotted here, before any overwrite, so the eventual
    # week-field delta reflects the reattribution even when the dollar
    # amount itself is unchanged (e.g. a later duplicate adds a dated
    # breakdown moving the SAME $5 from the keeper's own week onto a prior
    # week; the amount-equality alone must no longer be the only thing that
    # decides whether the week field moves).
    _keeper_old_breakdown = (_keeper_row_dict.get("whole_share_claim_breakdown") if _keeper_row_dict is not None else None) or (
        _keeper_outcome_dict.get("whole_share_claim_breakdown") if _keeper_outcome_dict is not None else None
    )
    _keeper_old_week_contribution = _current_week_claim_amount(
        {"whole_share_claim_breakdown": _keeper_old_breakdown}, _keeper_existing_amount, _keeper_iso_week
    )
    _keeper_new_breakdown = _keeper_old_breakdown
    if isinstance(source_row, dict) and _has_usable_claim_breakdown(source_row):
        # Codex review, PR #654: "Preserve dated keeper claim metadata". An
        # unconditional overwrite let a later duplicate's usable but UNDATED
        # breakdown replace a keeper's already-DATED one -- undoing the
        # dated claim-week attribution the keeper already had and shifting
        # those dollars onto the keeper artifact's own (wrong) week. Only
        # overwrite when the keeper doesn't already have a dated breakdown,
        # or the replacement is ALSO dated (equally or more precise).
        _keeper_already_dated = _claim_breakdown_has_dated_entry({"whole_share_claim_breakdown": _keeper_old_breakdown})
        if not _keeper_already_dated or _claim_breakdown_has_dated_entry(source_row):
            _keeper_new_breakdown = source_row.get("whole_share_claim_breakdown")
            if _keeper_row_dict is not None:
                _keeper_row_dict["whole_share_claim_breakdown"] = _keeper_new_breakdown
            if _keeper_outcome_dict is not None:
                _keeper_outcome_dict["whole_share_claim_breakdown"] = _keeper_new_breakdown
    _keeper_new_week_contribution = _current_week_claim_amount(
        {"whole_share_claim_breakdown": _keeper_new_breakdown}, new_amount, _keeper_iso_week
    )
    _week_delta = _keeper_new_week_contribution - _keeper_old_week_contribution
    _delta = new_amount - _keeper_existing_amount
    if _delta == 0 and _week_delta == 0:
        return
    # submitted_amount_usd, not planned_amount_usd: every amount-reading path
    # in this module checks submitted_amount_usd first, so writing the
    # corrected value there guarantees every later reader (including a
    # SUBSEQUENT duplicate comparing against this same keeper) sees it.
    if _keeper_row_dict is not None:
        _keeper_row_dict["submitted_amount_usd"] = new_amount
    if _keeper_outcome_dict is not None:
        _keeper_outcome_dict["submitted_amount_usd"] = new_amount
    _keeper_budget = _keeper_state.get("budget")
    if not isinstance(_keeper_budget, dict):
        return
    # day_notional_usd/hypothetical_day_spend_usd are the keeper state's own
    # row-local total -- never claim-week-split -- so they always absorb the
    # FULL dollar delta. week_notional_usd/hypothetical_week_spend_usd are
    # the keeper's own cumulative week-to-date total, which only ever
    # counted the claim-attributed PORTION of this order in the first place
    # -- they absorb the claim-attribution delta instead of the full dollar
    # delta, so a metadata-only reattribution (equal amount, different claim
    # week) still moves them correctly.
    for _budget_key in ("day_notional_usd", "hypothetical_day_spend_usd"):
        _existing_value = _finite_float(_keeper_budget.get(_budget_key))
        if _existing_value is not None:
            _keeper_budget[_budget_key] = _existing_value + _delta
    if _week_delta:
        for _budget_key in ("week_notional_usd", "hypothetical_week_spend_usd"):
            _existing_value = _finite_float(_keeper_budget.get(_budget_key))
            if _existing_value is not None:
                _keeper_budget[_budget_key] = _existing_value + _week_delta


def _order_constituent_ids(order_id: Any, source: Optional[Dict[str, Any]]) -> frozenset:
    """The full set of physical order IDs ``source`` represents.

    A whole-share aggregate row/outcome names its NEWEST constituent order as
    its own scalar ``order_id`` while the complete set of orders it combines
    lives in ``whole_share_order_ids`` -- see
    ``_resolve_cross_state_identity_for_row``'s own note. Used to compare
    what a row/outcome actually represents against what a keeper it might be
    merged into represents (Codex review, PR #654: "Do not merge aggregate
    totals into one constituent").
    """
    _ids = {order_id} if isinstance(order_id, str) and order_id else set()
    if isinstance(source, dict):
        _extra_ids = source.get("whole_share_order_ids")
        if isinstance(_extra_ids, list):
            _ids.update(_id for _id in _extra_ids if isinstance(_id, str) and _id)
    return frozenset(_ids)


def _keeper_constituent_ids(keeper_entry: Tuple[Any, Any, Any, Any]) -> frozenset:
    """The full set of physical order IDs a ``_KeeperEntry`` represents.

    Unions the keeper's own candidate row and matching outcome, each via
    :func:`_order_constituent_ids` -- either may independently carry the
    aggregate's ``whole_share_order_ids`` list.
    """
    _, _keeper_row, _keeper_outcome, _ = keeper_entry
    _ids: set = set()
    for _source in (_keeper_row, _keeper_outcome):
        if isinstance(_source, dict):
            _ids |= _order_constituent_ids(_source.get("order_id"), _source)
    return frozenset(_ids)


def _merge_outcomes_into_keeper(
    keeper_outcomes: Dict[Any, Any], source_outcomes: Dict[Any, Any]
) -> None:
    """Fold ``source_outcomes`` into ``keeper_outcomes`` without clobbering.

    ``broker_outcomes`` is keyed by TICKER in the common case, so two variants
    of one run that each retain a DIFFERENT order for the same ticker collide
    on that key. The disambiguation is the one
    ``near_close_dca._run_once_impl``'s own preserved-state merge already uses
    for exactly this situation: fall back to ``f"{ticker}:{order_id}"`` and
    carry the real ticker INSIDE the outcome, because
    ``submitted_order_rows()`` would otherwise write the synthetic key into
    the canonical order row as the symbol.
    """
    for _key, _outcome in source_outcomes.items():
        if _key not in keeper_outcomes:
            keeper_outcomes[_key] = _outcome
            continue
        _ticker = _outcome.get("ticker") if isinstance(_outcome, dict) else None
        if not isinstance(_ticker, str) or not _ticker:
            _ticker = _key if isinstance(_key, str) else str(_key)
        _order_id = _outcome.get("order_id") if isinstance(_outcome, dict) else None
        if isinstance(_order_id, str) and _order_id:
            _merged_key = f"{_ticker}:{_order_id}"
        else:
            _merged_key = _ticker
        # An ID-less outcome gives nothing to disambiguate with, and a
        # same-ticker collision can repeat even the order-id'd form when the
        # keeper already carries that exact synthetic key -- a numeric suffix
        # is the last resort that keeps the entry rather than dropping real
        # evidence on the floor.
        if _merged_key in keeper_outcomes:
            _suffix = 2
            while f"{_merged_key}#{_suffix}" in keeper_outcomes:
                _suffix += 1
            _merged_key = f"{_merged_key}#{_suffix}"
        if isinstance(_outcome, dict) and _merged_key != _ticker and _outcome.get("ticker") != _ticker:
            _outcome = {**_outcome, "ticker": _ticker}
        keeper_outcomes[_merged_key] = _outcome


def _merge_partial_state_into_keeper(source: Dict[str, Any], keeper: Dict[str, Any]) -> None:
    """Fold ``source``'s surviving rows and totals into ``keeper``.

    Both states are variants of the SAME physical run (the caller has already
    established that via :func:`_states_share_explicit_run_identity` and a
    single unambiguous keeper), so the survivor must report their UNION: every
    distinct order they hold between them, once, under one run day.

    The two families of totals are merged differently on purpose:

    * ``counts.*`` and ``budget.day_notional_usd`` /
      ``budget.hypothetical_day_spend_usd`` are ROW-LOCAL -- each state's own
      contribution -- so they ADD, exactly reproducing the sum the two
      separate states contributed to every aggregate builder before the
      merge. ``source``'s values have already had its removed duplicates
      subtracted by this point, so the sum counts each physical order once.
    * ``budget.week_notional_usd`` / ``budget.hypothetical_week_spend_usd``
      are cumulative WEEK-TO-DATE totals, not row-local. Two variants of one
      run report overlapping (not additive) weekly totals, so the survivor
      takes ``source``'s figure -- ``source`` is always the LATER variant in
      accumulation order, and a cumulative total's latest report is its
      authoritative one. This is exactly what ``_latest_week_value()`` would
      have selected from the two states before the merge, so the merge is
      week-neutral. Taking the larger of the two instead (as a first version
      of this did) silently reverted a legitimate DOWNWARD correction: a
      later variant reporting $8 after a -$2 settlement adjustment against an
      earlier variant's $10 would be restored to the stale $10, because the
      blocked-state reconciliation that produced the $8 does not rerun for
      the survivor (Codex review, PR #654: "Preserve downward corrections
      when folding weekly totals"). Compared PER KEY, so a keeper carrying
      both fields with genuinely different values does not have them
      collapsed onto one figure.

    ``build_performance_snapshot()``'s own fallbacks are respected: a keeper
    that reports no ``budget.day_notional_usd`` at all has its notional
    derived from its rows by :func:`_this_run_submitted_notional`, so the
    merged rows are already accounted for there and the field is left absent
    rather than being invented (which would discard the keeper's own rows).
    Likewise ``counts.submitted`` falls back to the top-level
    ``orders_submitted_count`` blocked/partial artifacts carry.
    """
    # Captured BEFORE the rows move, so a derived fallback reads source's own
    # contribution rather than the post-merge union.
    _source_day_notional = _state_own_day_notional(source)
    # Codex review, PR #654: "Derive merged counts from surviving source
    # rows". A partial source variant can carry real submitted rows while
    # reporting NEITHER counts.submitted NOR the top-level
    # orders_submitted_count (and likewise no counts.buy_candidates) -- the
    # rows still move into the keeper, so crediting nothing for them leaves a
    # survivor holding X and Y while reporting one of each. The blocked-state
    # normalization does not rescue this: it only recomputes a keeper whose
    # own status is "blocked", not a completed or legacy one. Derived from the
    # rows about to move whenever the source reports no figure of its own; a
    # reported figure (a genuine zero included) always wins, since it has
    # already had this state's own removed duplicates subtracted.
    _source_submitted = _state_own_submitted_count(source)
    if _source_submitted is None:
        _source_submitted = _derived_submitted_count(source)
    _source_counts = source.get("counts")
    _source_candidates = (
        _finite_float(_source_counts.get("buy_candidates")) if isinstance(_source_counts, dict) else None
    )
    if _source_candidates is None:
        _source_candidates = _derived_candidate_count(source)
    _keeper_day_notional_reported = _reported_day_notional(keeper)

    _source_rows = source.get("buy_candidates")
    # Codex review, PR #654: "Preserve completed placement semantics during
    # partial merges". Whether a STATUSLESS candidate row counts as placed is
    # a property of the state it lives in, not of the row: an ordinary
    # completed run's rows are placed by construction and are read that way
    # by build_graded_vs_fixed() and the blocked-state reconciliation, while
    # a blocked artifact carries no such guarantee and has its statusless
    # rows excluded (see those two sites' own notes). Moving a completed
    # variant's statusless row into a BLOCKED keeper therefore silently
    # demoted a real order to unplaced -- dropping it from counts.submitted,
    # day_notional_usd and the graded-versus-fixed totals, so a blocked X=$5
    # plus a completed statusless Y=$3 rendered one order and $5 executed
    # against an $8 weekly total.
    #
    # The row's own provenance is stamped onto it before it moves, rather
    # than promoting the survivor's status: the keeper is blocked because ITS
    # OWN run was, and its own statusless rows must keep being excluded --
    # promoting the state would wrongly extend the completed run's
    # construction guarantee to them. A matching broker outcome's real status
    # is preferred when this state has one (it is the actual placement
    # evidence); "submitted" is the fallback, the canonical value the
    # executors write for a placed candidate row.
    if (
        isinstance(_source_rows, list)
        and _source_rows
        and source.get("status") != "blocked"
        and keeper.get("status") == "blocked"
    ):
        _source_outcomes_for_status = source.get("broker_outcomes")
        _source_outcome_status_by_identity: Dict[str, Any] = {}
        if isinstance(_source_outcomes_for_status, dict):
            for _status_key, _status_outcome in _source_outcomes_for_status.items():
                if not isinstance(_status_outcome, dict):
                    continue
                if _status_outcome.get("status") not in _REAL_ORDER_STATUSES:
                    continue
                for _status_identity in (
                    _status_outcome.get("order_id"),
                    _status_outcome.get("ticker"),
                    _status_key,
                ):
                    if isinstance(_status_identity, str) and _status_identity:
                        _source_outcome_status_by_identity.setdefault(_status_identity, _status_outcome["status"])
        for _moving_row in _source_rows:
            if not isinstance(_moving_row, dict) or _moving_row.get("status") is not None:
                continue
            # Adjustments are exempt from the blocked-state exclusion
            # already (their dollar delta counts regardless of status), so
            # stamping one would only add a claim the artifact never made.
            if _is_settlement_adjustment(_moving_row):
                continue
            _moving_status = None
            for _moving_identity in (_moving_row.get("order_id"), _moving_row.get("ticker")):
                if isinstance(_moving_identity, str) and _moving_identity:
                    _moving_status = _source_outcome_status_by_identity.get(_moving_identity)
                    if _moving_status is not None:
                        break
            _moving_row["status"] = _moving_status or "submitted"
    if isinstance(_source_rows, list) and _source_rows:
        _keeper_rows = keeper.get("buy_candidates")
        if isinstance(_keeper_rows, list):
            _keeper_rows.extend(_source_rows)
        else:
            keeper["buy_candidates"] = list(_source_rows)
        source["buy_candidates"] = []
    _source_outcomes = source.get("broker_outcomes")
    if isinstance(_source_outcomes, dict) and _source_outcomes:
        _keeper_outcomes = keeper.get("broker_outcomes")
        if isinstance(_keeper_outcomes, dict):
            _merge_outcomes_into_keeper(_keeper_outcomes, _source_outcomes)
        else:
            keeper["broker_outcomes"] = dict(_source_outcomes)
        source["broker_outcomes"] = {}
    # Codex review, PR #654: "Merge manual-review flags before dropping
    # partial variants". manual_review_tickers is neither a row nor a total,
    # so nothing above carries it across -- yet build_coverage_missed() and
    # build_weekly_review_notes() both read it straight off the state. A
    # later variant flagging "Y" for manual review had that flag dropped on
    # the floor along with the state, and both reports then showed zero
    # manual-review flags for the run. Unioned, order-preserving (keeper's
    # own entries first), so a flag repeated by both variants is not
    # double-counted by the notes' own len() tally.
    _source_manual_review = source.get("manual_review_tickers")
    if isinstance(_source_manual_review, list) and _source_manual_review:
        _keeper_manual_review = keeper.get("manual_review_tickers")
        if not isinstance(_keeper_manual_review, list):
            _keeper_manual_review = []
            keeper["manual_review_tickers"] = _keeper_manual_review
        for _flagged in _source_manual_review:
            if _flagged not in _keeper_manual_review:
                _keeper_manual_review.append(_flagged)
        source["manual_review_tickers"] = []

    if _source_candidates:
        _keeper_counts = keeper.get("counts")
        if not isinstance(_keeper_counts, dict):
            _keeper_counts = {}
            keeper["counts"] = _keeper_counts
        _keeper_counts["buy_candidates"] = max(
            0.0, (_finite_float(_keeper_counts.get("buy_candidates")) or 0.0) + _source_candidates
        )
    if _source_submitted:
        _keeper_counts = keeper.get("counts")
        if isinstance(_keeper_counts, dict) and _finite_float(_keeper_counts.get("submitted")) is not None:
            _keeper_counts["submitted"] = max(
                0.0, (_finite_float(_keeper_counts.get("submitted")) or 0.0) + _source_submitted
            )
        elif _finite_float(keeper.get("orders_submitted_count")) is not None:
            keeper["orders_submitted_count"] = max(
                0.0, (_finite_float(keeper.get("orders_submitted_count")) or 0.0) + _source_submitted
            )
        else:
            # Neither slot present: the keeper contributed zero submissions to
            # every aggregate today, so seeding counts.submitted with source's
            # own figure preserves the pre-merge sum exactly.
            if not isinstance(_keeper_counts, dict):
                _keeper_counts = {}
                keeper["counts"] = _keeper_counts
            _keeper_counts["submitted"] = _source_submitted
    if _source_day_notional and _keeper_day_notional_reported is not None:
        # Only when the keeper REPORTS a row-local day notional of its own --
        # otherwise _this_run_submitted_notional() derives it from the rows,
        # which now include source's, and writing a value here would replace
        # that derivation with source's contribution alone.
        _keeper_budget = keeper.get("budget")
        if isinstance(_keeper_budget, dict):
            for _budget_key in ("day_notional_usd", "hypothetical_day_spend_usd"):
                _existing = _finite_float(_keeper_budget.get(_budget_key))
                if _existing is not None:
                    _keeper_budget[_budget_key] = _existing + _source_day_notional

    # Codex review, PR #654: "Carry the latest remaining budget through
    # partial merges". budget.remaining_spend_today_usd (named
    # remaining_daily_usd by the morning/midday/market_open/intraday
    # artifacts) and budget.weekly_rollover_reserve_usd are cumulative
    # AS-OF-THIS-STATE snapshots, not row-local totals: they SHRINK as the
    # run places orders. Adding the merged rows to the keeper's executed
    # total while leaving its stale headroom untouched rendered a budget
    # table reading "$8.00 spent / $95.00 remaining" after a $5 and a $3
    # order. Same rule as the cumulative weekly total above -- the later
    # variant's figure wins. Deliberately NOT extended to daily_cap_usd /
    # weekly_cap_usd / budget_tier: those are the day's policy constants,
    # identical in both variants of one run, so copying them would only add
    # a way to go wrong.
    _source_budget_snapshot = source.get("budget")
    if isinstance(_source_budget_snapshot, dict):
        _keeper_budget = keeper.get("budget")
        if isinstance(_keeper_budget, dict):
            for _snapshot_key in (
                "remaining_spend_today_usd",
                "remaining_daily_usd",
                "weekly_rollover_reserve_usd",
                "carry_forward_reserve_usd",
            ):
                _source_snapshot_value = _finite_float(_source_budget_snapshot.get(_snapshot_key))
                # Only overwrite a field the keeper actually reports: seeding
                # one it never carried would invent headroom for a state
                # whose own writer never measured it.
                if _source_snapshot_value is not None and _finite_float(_keeper_budget.get(_snapshot_key)) is not None:
                    _keeper_budget[_snapshot_key] = _source_snapshot_value

    _source_week = _state_week_notional(source)
    if _source_week is not None:
        _keeper_budget = keeper.get("budget")
        if not isinstance(_keeper_budget, dict):
            _keeper_budget = {}
            keeper["budget"] = _keeper_budget
        _source_budget = source.get("budget")
        for _week_key in ("week_notional_usd", "hypothetical_week_spend_usd"):
            if _finite_float(_keeper_budget.get(_week_key)) is None:
                # The keeper reports nothing under this key; the "neither
                # field present" seed below is what covers that case.
                continue
            _source_week_for_key = (
                _finite_float(_source_budget.get(_week_key)) if isinstance(_source_budget, dict) else None
            )
            # Falls back to source's RESOLVED weekly total (the same
            # week_notional_usd-then-hypothetical_week_spend_usd lookup
            # _state_week_notional() uses) only when source has no value of
            # its own under this exact key.
            if _source_week_for_key is None:
                _source_week_for_key = _source_week
            _keeper_budget[_week_key] = _source_week_for_key
        if (
            _finite_float(_keeper_budget.get("week_notional_usd")) is None
            and _finite_float(_keeper_budget.get("hypothetical_week_spend_usd")) is None
        ):
            _keeper_budget["week_notional_usd"] = _source_week


def _reported_day_notional(data: Dict[str, Any]) -> Optional[float]:
    """A state's OWN reported ``budget.day_notional_usd``, or ``None``.

    ``None`` means the state reports no row-local day notional at all, which
    is the exact condition under which ``build_performance_snapshot()`` falls
    back to deriving it from the state's rows.
    """
    budget = data.get("budget")
    if not isinstance(budget, dict):
        return None
    return _finite_float(budget.get("day_notional_usd"))


def _state_own_day_notional(data: Dict[str, Any]) -> float:
    """A state's own executed day notional, using the snapshot's own fallback."""
    reported = _reported_day_notional(data)
    if reported is not None:
        return reported
    return _this_run_submitted_notional(data)


def _state_own_submitted_count(data: Dict[str, Any]) -> Optional[float]:
    """A state's own submitted-order count, using the snapshot's own fallback.

    ``None`` when the state reports NEITHER field -- distinct from a genuine
    zero, so :func:`_merge_partial_state_into_keeper` can derive the figure
    from the rows it is about to move instead of crediting nothing for them.
    """
    counts = data.get("counts")
    value = counts.get("submitted") if isinstance(counts, dict) else None
    if value is None:
        value = data.get("orders_submitted_count")
    if value is None:
        return None
    return _finite_float(value)


def _derived_candidate_count(data: Dict[str, Any]) -> float:
    """Count a state's own ordinary (non-adjustment) candidate rows.

    Mirrors what ``counts.buy_candidates`` reports: settlement adjustments are
    excluded, matching the cross-file dedup pass's own
    ``_removed_candidate_count``, which only counts ordinary rows.
    """
    rows = data.get("buy_candidates")
    if not isinstance(rows, list):
        return 0.0
    return float(sum(1 for row in rows if isinstance(row, dict) and not _is_settlement_adjustment(row)))


def _derived_submitted_count(data: Dict[str, Any]) -> float:
    """Count the distinct placed ordinary orders a state's own rows evidence.

    Deduplicated by ORDER IDENTITY (``order_id`` when present, else ticker) so
    a candidate row and its matching ``broker_outcomes`` entry -- two sides of
    ONE physical order -- count once, the same identity rule
    :func:`_this_run_submitted_notional` uses for the notional it derives from
    the same rows. Settlement adjustments are excluded: no other
    reconciliation path in this module counts them toward ``counts.submitted``.
    """
    _seen: set = set()
    for _row in data.get("buy_candidates") or []:
        if not isinstance(_row, dict) or _is_settlement_adjustment(_row):
            continue
        if _row.get("status") not in _REAL_ORDER_STATUSES:
            continue
        _order_id = _row.get("order_id")
        _identity = _order_id if isinstance(_order_id, str) and _order_id else _row.get("ticker")
        if isinstance(_identity, str) and _identity:
            _seen.add(_identity)
    _outcomes = data.get("broker_outcomes")
    if isinstance(_outcomes, dict):
        for _key, _outcome in _outcomes.items():
            if not isinstance(_outcome, dict) or _is_settlement_adjustment(_outcome):
                continue
            if _outcome.get("status") not in _REAL_ORDER_STATUSES:
                continue
            _order_id = _outcome.get("order_id")
            _identity = _order_id if isinstance(_order_id, str) and _order_id else _outcome.get("ticker")
            if not (isinstance(_identity, str) and _identity):
                _identity = _key if isinstance(_key, str) else None
            if isinstance(_identity, str) and _identity:
                _seen.add(_identity)
    return float(len(_seen))


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
    # Codex review, PR #654: "Deduplicate retained orders across run-state
    # files". load_scheduled_run_states() can return MULTIPLE run_state*.json
    # variants for a single physical run (e.g. a retained blocked/preserved
    # state alongside a later completed state for the same run) -- a
    # supported artifact shape elsewhere in the repository. Every dedup pass
    # elsewhere in this function operates on ONE `data` object's own
    # buy_candidates/broker_outcomes at a time, so the same broker order ID
    # appearing in two different retained FILES was never recognized as a
    # duplicate: each state's own amount/counts computation further below
    # counted it independently, doubling the executed/day total for one
    # physical order. Removed here, before ANY of that per-state computation
    # runs, so every downstream pass -- which this session has spent a great
    # many rounds hardening for the single-state case -- sees each order_id
    # at most once across the whole retained set, with no changes needed to
    # that logic itself.
    #
    # Walked in the SAME chronologically sorted order the accumulator loop
    # just below uses (a fresh sorted() view of `kept`, mutating only the
    # dicts in place -- see that loop's own note on why `kept`'s own order
    # is never reordered), so the FIRST -- earliest -- state to carry an
    # order_id keeps it and a later duplicate loses it, matching "prefer the
    # earliest recorded evidence" the accumulator's own week-reset logic
    # already assumes elsewhere.
    #
    # Settlement adjustments are tracked in their OWN separate namespace,
    # never the ordinary one: an adjustment sharing its order_id with the
    # order it corrects (in this case, an EARLIER FILE's own order) is a
    # bookkeeping correction, not a duplicate representation of that order,
    # and must never fold onto it (or have an ordinary order fold onto it).
    # But two DIFFERENT files carrying the SAME settlement adjustment (the
    # same physical correction, preserved in one file and repeated in a
    # later one) are duplicates of EACH OTHER just like ordinary orders are
    # -- exempting adjustments from cross-file dedup entirely let both
    # survive, doubling a -$2 correction to -$4 (Codex review, PR #654:
    # "Deduplicate adjustment rows across state variants"). Mirrors the
    # same separate-namespace pattern this file already uses for the
    # single-state dedup passes (_rows_by_order_id vs.
    # _adjustment_rows_by_order_id, candidate_identity_by_order_id vs.
    # adjustment_candidate_identity_by_order_id, and so on).
    # Codex review, PR #654: "Namespace cross-file order identities by
    # broker". order_id is only unique WITHIN one broker's own order
    # history -- this repo's adapters are broker-agnostic (see
    # dca_live_adapters.py), so a run_state's own "broker" field, not the
    # bare order_id, is the real cross-file identity. Keyed as
    # ``(broker, order_id)`` throughout, with the value the claiming
    # state's own ISO week (used below to decide whether a removed row's
    # amount should also come out of a CUMULATIVE weekly total -- see
    # "Correct duplicate weekly totals across ISO-week boundaries"). A
    # state with no "broker" field (older artifacts) shares the ``None``
    # scope, matching every retained state's behavior before this fix.
    # Value is (claiming_state_iso_week, keeper_row, keeper_outcome,
    # keeper_state) -- the candidate row and/or matching broker_outcomes
    # entry (either may be None) and the state dict that first claimed this
    # identity, kept so a later duplicate's more authoritative amount can be
    # merged back onto BOTH the keeper's own candidate row and its own
    # matching outcome entry (Codex review, PR #654: "Merge later fill
    # amounts into the retained order"; "Update the keeper outcome when
    # merging a later fill") -- see _merge_authoritative_amount_into_keeper().
    _KeeperEntry = Tuple[Optional[Tuple[int, int]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]
    _order_ids_claimed_across_states: Dict[Tuple[Any, Any, str], _KeeperEntry] = {}
    _adjustment_order_ids_claimed_across_states: Dict[Tuple[Any, Any, str], _KeeperEntry] = {}
    # (broker, order_id) -> set of every DISTINCT account_scope a claim has
    # been registered under for that order_id so far. Codex review, PR
    # #654: "Deduplicate mixed legacy and account-scoped variants". An
    # artifact-format transition (or a recovery run) can produce one
    # variant of a physical order with no account_scope at all (an older
    # writer, or run_classification simply absent) and a later variant of
    # the SAME order that DOES carry one -- an exact-match-only identity
    # treats these as two different orders. When exactly one account_scope
    # has ever claimed a given (broker, order_id) -- i.e. there is no
    # genuine ambiguity from two DIFFERENT real accounts independently
    # issuing the same order_id -- a later row/outcome under a DIFFERENT
    # account_scope for that same (broker, order_id) is resolved as a
    # duplicate of that one unambiguous claim instead of starting a second,
    # spurious identity. Tracked separately for the adjustment namespace,
    # mirroring every other separate-namespace pattern in this pass.
    _broker_order_claimed_accounts: Dict[Tuple[Any, str], set] = {}
    _adjustment_broker_order_claimed_accounts: Dict[Tuple[Any, str], set] = {}
    # order_id -> set of every DISTINCT broker scope a claim has been
    # registered under for that order_id so far. Codex review, PR #654:
    # "Bridge missing broker scopes to unambiguous variants". Mirrors the
    # account-scope bridge above, but for the broker field itself: a
    # retained blocked/legacy variant of one physical run can omit
    # "broker" entirely (None) while a later variant of the SAME run
    # records it (e.g. "robinhood") -- an exact-match-only identity treats
    # these as different brokers' orders. Bridged only when exactly one
    # broker has ever claimed this order_id and one side is unscoped
    # (None), the same "same order, inconsistently recorded" standard the
    # account bridge already applies.
    _order_claimed_brokers: Dict[str, set] = {}
    _adjustment_order_claimed_brokers: Dict[str, set] = {}

    def _resolve_cross_state_identity(
        _claimed_map_arg: Dict[Tuple[Any, Any, str], _KeeperEntry],
        _claimed_accounts_arg: Dict[Tuple[Any, str], set],
        _claimed_brokers_arg: Dict[str, set],
        _broker_arg: Any,
        _account_arg: Any,
        _order_id_arg: str,
    ) -> Tuple[Any, Any, str]:
        _exact_key = (_broker_arg, _account_arg, _order_id_arg)
        if _exact_key in _claimed_map_arg:
            return _exact_key
        _claimed_accounts = _claimed_accounts_arg.get((_broker_arg, _order_id_arg))
        if _claimed_accounts and len(_claimed_accounts) == 1:
            (_only_claimed_account,) = tuple(_claimed_accounts)
            # Only bridge an UNSCOPED (None) claim with a SCOPED one -- two
            # concrete, DIFFERENT account numbers for the same order_id are
            # a genuine collision (two real accounts independently issuing
            # the same account-local order_id) and must stay separate; only
            # one side being None (a legacy/absent account field) is the
            # "same order, inconsistently recorded" case this fallback
            # exists for.
            if _only_claimed_account != _account_arg and (_only_claimed_account is None or _account_arg is None):
                _fallback_key = (_broker_arg, _only_claimed_account, _order_id_arg)
                if _fallback_key in _claimed_map_arg:
                    return _fallback_key
        _claimed_brokers = _claimed_brokers_arg.get(_order_id_arg)
        if _claimed_brokers and len(_claimed_brokers) == 1:
            (_only_claimed_broker,) = tuple(_claimed_brokers)
            if _only_claimed_broker != _broker_arg and (_only_claimed_broker is None or _broker_arg is None):
                _fallback_key = (_only_claimed_broker, _account_arg, _order_id_arg)
                if _fallback_key in _claimed_map_arg:
                    return _fallback_key
                # Codex review, PR #654: "Bridge broker and account scopes
                # together". When a legacy keeper omits BOTH "broker" and
                # "account_number" but the incoming row/outcome supplies
                # both, neither one-dimensional fallback above can find it
                # on its own: the account fallback above searched under
                # THIS row's own (new) broker, and the single-scope broker
                # fallback just above keeps THIS row's own (new) account --
                # neither ever lands on the legacy claim's actual key of
                # (None, None, order_id). Resolve the two missing scopes
                # JOINTLY instead: once the unique broker this order_id was
                # ever claimed under is known, look up the account
                # SPECIFICALLY claimed under THAT broker (not under this
                # row's own broker) -- if that, too, is unambiguous and one
                # side is unscoped, the fully-bridged legacy key is the
                # correct identity.
                _claimed_accounts_under_only_broker = _claimed_accounts_arg.get(
                    (_only_claimed_broker, _order_id_arg)
                )
                if _claimed_accounts_under_only_broker and len(_claimed_accounts_under_only_broker) == 1:
                    (_only_claimed_account_under_broker,) = tuple(_claimed_accounts_under_only_broker)
                    if _only_claimed_account_under_broker != _account_arg and (
                        _only_claimed_account_under_broker is None or _account_arg is None
                    ):
                        _fallback_key = (_only_claimed_broker, _only_claimed_account_under_broker, _order_id_arg)
                        if _fallback_key in _claimed_map_arg:
                            return _fallback_key
        return _exact_key

    def _resolve_cross_state_identity_for_row(
        _claimed_map_arg: Dict[Tuple[Any, Any, str], _KeeperEntry],
        _claimed_accounts_arg: Dict[Tuple[Any, str], set],
        _claimed_brokers_arg: Dict[str, set],
        _broker_arg: Any,
        _account_arg: Any,
        _row_arg: Dict[str, Any],
        _order_id_arg: str,
    ) -> Tuple[Tuple[Any, Any, str], bool]:
        """Like ``_resolve_cross_state_identity``, but also checks every ID
        in ``_row_arg``'s own ``whole_share_order_ids`` list.

        Codex review, PR #654: "Deduplicate every order ID in aggregated
        rows". ``near_close_dca._merge_whole_share_submission_outcomes()``
        can legitimately aggregate SEVERAL physical whole-share orders into
        one row/outcome: the scalar ``order_id`` holds only the newest of
        them, while the complete set lives in ``whole_share_order_ids``.
        Checking only the scalar ID let an aggregated row whose newest ID
        is unclaimed slip past cross-file dedup even though one of its
        OTHER constituent IDs was already claimed by an earlier state,
        double-counting that shared order. Treated as a full-row duplicate
        of whichever earlier claim is found (this module has no per-ID
        breakdown of an aggregate's own total to split more precisely).

        Returns ``(identity_key, matched_via_constituent_aggregate)``. The
        second element is True only when the returned identity came from
        the multi-constituent fallback below (this row's OWN order_id did
        NOT resolve directly) -- see the caller's own note (Codex review,
        PR #654: "Do not merge aggregate totals into one constituent") for
        why that distinction matters: a match found this way names ONE of
        several already-separately-claimed keepers, and this row's own
        amount is the AGGREGATE's total across all of them, not a
        corrected amount for that one keeper alone.
        """
        _resolved = _resolve_cross_state_identity(
            _claimed_map_arg, _claimed_accounts_arg, _claimed_brokers_arg, _broker_arg, _account_arg, _order_id_arg
        )
        if _resolved in _claimed_map_arg:
            return _resolved, False
        _extra_ids = _row_arg.get("whole_share_order_ids")
        if isinstance(_extra_ids, list):
            _all_constituent_ids = [_order_id_arg] + [
                _extra_id
                for _extra_id in _extra_ids
                if isinstance(_extra_id, str) and _extra_id and _extra_id != _order_id_arg
            ]
            if len(_all_constituent_ids) > 1:
                # Codex review, PR #654: "Preserve unclaimed orders in
                # aggregate rows". A partial overlap -- SOME constituent
                # IDs already claimed, but at least one genuinely new --
                # must NOT be folded into a full-row duplicate of whichever
                # claim is found first: this module has no per-ID
                # breakdown of the aggregate's own total, so merging it
                # away either loses the new order's dollars entirely or,
                # when the aggregate is itself the keeper, lets a later
                # smaller standalone constituent overwrite its correctly
                # combined total downward. Only treated as a full
                # cross-file duplicate when EVERY constituent ID already
                # resolves to an existing claim -- a provable complete
                # repeat, not a partial one.
                _resolved_per_constituent = [
                    _resolve_cross_state_identity(
                        _claimed_map_arg,
                        _claimed_accounts_arg,
                        _claimed_brokers_arg,
                        _broker_arg,
                        _account_arg,
                        _constituent_id,
                    )
                    for _constituent_id in _all_constituent_ids
                ]
                if all(_k in _claimed_map_arg for _k in _resolved_per_constituent):
                    return _resolved_per_constituent[0], True
        return _resolved, False
    # Codex review, PR #654: "Collapse state variants before counting run
    # days". Row-level dedup above (and below) removes a duplicate state
    # variant's own order rows, but leaves the state dict itself in `kept`
    # -- a preserved blocked state and a later completed run_state that are
    # both variants of the SAME physical run, once their duplicate rows are
    # gone, are just two empty husks representing one run, yet still count
    # as TWO separate run days via len(run_states), and still render two
    # duplicate per-run rows in every builder that emits one row per state
    # (build_graded_vs_fixed, build_budget_reserve_effectiveness,
    # build_coverage_missed, and so on). A state is dropped entirely here
    # only when it originally carried at least one candidate/outcome row
    # AND every single one of them was removed as a cross-file duplicate --
    # a state that genuinely never had any orders that day (a real "nothing
    # to buy" run) is NOT touched, since it never contributed a row to
    # dedup against in the first place.
    _states_fully_absorbed_as_duplicates: set = set()
    # Codex review, PR #654 discussion_r3853577540: "Merge partial variants
    # before counting run days". The husk collapse above only fires when a
    # state ends up with ZERO rows. When an earlier blocked variant keeps
    # order X and its later completed variant of the SAME run keeps a newly
    # placed order Y, row-level dedup correctly leaves X in the first and Y
    # in the second -- but BOTH states survive, so one physical run still
    # reports two run days (and two per-run rows in every builder that emits
    # one row per state). Maps the id() of a state that was folded into
    # another to its destination, so a THIRD state whose own duplicates were
    # claimed by the now-folded state follows the chain to the state that
    # actually holds those rows instead of merging into one that is about to
    # be dropped.
    _states_merged_into: Dict[int, Dict[str, Any]] = {}
    for data in sorted(kept, key=_accumulation_sort_key):
        _broker_scope = data.get("broker") if isinstance(data.get("broker"), str) else None
        # Codex review, PR #654: "Include the account in cross-state
        # order identities". order_id is only unique WITHIN one
        # broker AND account -- a broker-only scope still treats two
        # DIFFERENT accounts at the same broker issuing the same
        # account-local order_id as duplicates of each other. Live
        # morning states persist their account under
        # run_classification.account_number (morning_dca.py); the
        # run-state schema also defines a top-level account_number.
        # Checked in that order, falling back to the legacy None
        # scope (matching every retained state's behavior before
        # this fix) when neither is present.
        _account_scope = data.get("account_number")
        if not isinstance(_account_scope, str):
            _run_classification_for_account_scope = data.get("run_classification")
            if isinstance(_run_classification_for_account_scope, dict):
                _account_scope = _run_classification_for_account_scope.get("account_number")
        if not isinstance(_account_scope, str):
            _account_scope = None
        _state_iso_week = _iso_week_of_state(data)
        # Collected separately from the claimed-across-states maps above and
        # only merged into them once this WHOLE state has been processed --
        # a candidate row and its own matching broker outcome for the SAME
        # order_id are two sides of ONE physical order within a SINGLE
        # state/file, not a cross-file duplicate, and the rest of this
        # function (candidate_row_by_order_id, existing_order_ids, and
        # every other reconciliation pass below) depends on both sides
        # surviving together so it can reconcile them. Checking the
        # candidates loop's own additions against the outcomes loop within
        # the SAME iteration would treat the outcome as an "already
        # claimed" duplicate of its own candidate row and incorrectly
        # drop it.
        # Dicts, not sets: mapping order_id -> (row_or_outcome,
        # matching_outcome_or_None) -- the candidate/outcome dict that first
        # claimed the order_id in THIS state, paired with this SAME state's
        # own matching broker_outcomes entry for that order_id (looked up via
        # _outcome_by_order_id_and_kind below, populated before either dedup
        # loop runs). The claimed-across-states maps below remember both, so
        # a later duplicate's more authoritative amount can be merged onto
        # BOTH the keeper row and its own outcome entry (see
        # _merge_authoritative_amount_into_keeper()), not just the fact that
        # the order_id was seen.
        _order_ids_seen_this_state: Dict[str, Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = {}
        _adjustment_order_ids_seen_this_state: Dict[str, Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = {}
        # Ticker -> list of each removed duplicate candidate's own amount
        # (submitted_amount_usd, else planned_amount_usd, else None), used
        # below to match a same-state broker_outcomes entry that lacks its
        # OWN order_id back to the specific removed duplicate it belongs to
        # -- see the outcome loop's own note (Codex review, PR #654: "Fold
        # ID-less outcomes with their removed candidate"). A per-ticker LIST
        # of {"amount": ..., "credited": ...} entries, not a bare count: a
        # state can carry a genuine, still-retained SECOND candidate for the
        # same ticker (a distinct order) whose own ID-less outcome must
        # survive untouched, and a count alone can't tell the two
        # same-ticker outcomes apart when dict iteration happens to reach
        # the genuine one first -- matching by amount (dict-order-
        # independent) picks the entry that actually belongs to the removed
        # duplicate instead of whichever one is encountered first (Codex
        # review, PR #654: "Preserve unrelated ID-less same-ticker
        # outcomes"; "Match ID-less outcomes to the removed candidate").
        # "credited" records whether the removed candidate's OWN row already
        # reached a real order status (and so already credited its
        # count/amount some other way) -- when it did NOT (e.g. a `selected`
        # candidate whose ONLY placement evidence is this matching ID-less
        # outcome), the outcome loop below still needs to credit the
        # removal itself instead of silently dropping real evidence (Codex
        # review, PR #654: "Credit removed ID-less outcomes to state
        # totals"). Each removed duplicate may claim (drop) at most ONE
        # matching ID-less outcome.
        _removed_duplicate_candidate_entries_by_ticker: Dict[str, List[Dict[str, Any]]] = {}
        # Fresh evidence after the row-level dedup above: a LATER file's
        # own counts.submitted/counts.buy_candidates/budget.*_notional_usd
        # were already computed (by the executor that wrote it, or by the
        # blocked-state recompute further below) from ITS OWN copy of the
        # now-removed duplicate rows. The blocked-state recompute further
        # down overwrites them from scratch and so is unaffected, but a
        # NORMAL completed state's pre-existing aggregates are used as-is
        # and never touched by that recompute -- so a duplicate row's
        # contribution stayed baked into the state's totals even after the
        # row itself was dropped here, still doubling the executed/day
        # total and order count (Codex review, PR #654: "Recompute state
        # totals after dropping cross-file duplicates"). Tracked here and
        # subtracted from this state's own counts/budget below, once its
        # full row-removal pass is done.
        _removed_submitted_count = 0
        _removed_candidate_count = 0
        _removed_amount = 0.0
        # Codex review, PR #654: "Keep distinct run days after order
        # deduplication". A state is only a variant of the SAME physical run
        # as its keeper when they share a trading_day -- the cross-week
        # correction case this pass explicitly supports (a Friday order
        # repeated/corrected in a Monday state) removes every one of a
        # DIFFERENT day's rows, but that Monday state is still its OWN
        # distinct run, not an empty husk of Friday's. Starts True; set
        # False the moment any removed duplicate's keeper belongs to a
        # different trading_day, which blocks the final "collapse fully-
        # duplicate state" check below from dropping this state's own
        # (otherwise real) run day.
        _all_removed_duplicates_share_this_trading_day = True
        # Every DISTINCT keeper state this state's removed duplicates were
        # folded into, keyed by id() (run_state dicts are not hashable).
        # Recorded so the partial-variant merge below knows not just THAT the
        # removals share a run identity but WHICH single sibling state
        # actually holds them -- merging into "some state on the same
        # trading_day" would move rows into a state that never claimed them
        # (Codex review, PR #654 discussion_r3853577540: "Merge partial
        # variants before counting run days"). More than one entry here means
        # the destination is ambiguous and no merge is attempted.
        _removed_duplicate_keeper_states: Dict[int, Dict[str, Any]] = {}
        # The portion of _removed_amount whose claiming state is NOT in
        # this state's own ISO week -- see "Correct duplicate weekly
        # totals across ISO-week boundaries" at the budget-adjustment site
        # below for why only this portion (not the same-week portion) may
        # also be subtracted from a cumulative weekly total.
        _removed_amount_cross_week = 0.0
        # order_ids already credited toward THIS state's removed count/
        # amount -- tracked SEPARATELY (not as one combined set) so a
        # removed candidate row that's missing an amount (or a real
        # status) but whose matching outcome entry HAS one can still let
        # the outcome side credit whichever piece the candidate side
        # couldn't, instead of being unconditionally marked "already
        # handled" and skipped entirely (Codex review, PR #654: "Credit
        # missing outcome contributions after candidate removal").
        _removed_count_credited_order_ids: set = set()
        _removed_amount_credited_order_ids: set = set()
        _candidates_for_cross_state_dedup = data.get("buy_candidates")
        _outcomes_for_cross_state_dedup_original = data.get("broker_outcomes")
        # Captured before either dedup pass below mutates buy_candidates/
        # broker_outcomes in place -- the ONLY signal for whether this
        # state's evidence was entirely a duplicate of an earlier one (see
        # _states_fully_absorbed_as_duplicates' own declaration above).
        _original_candidate_count = (
            len(_candidates_for_cross_state_dedup) if isinstance(_candidates_for_cross_state_dedup, list) else 0
        )
        _original_outcome_count = (
            len(_outcomes_for_cross_state_dedup_original)
            if isinstance(_outcomes_for_cross_state_dedup_original, dict)
            else 0
        )
        # Codex review, PR #654: "Reconcile outcome amounts before
        # crediting removals". When a later state's duplicate order is
        # represented by BOTH a candidate row and a broker_outcomes entry
        # that report DIFFERENT amounts (a candidate's own
        # planned_amount_usd is a pre-placement estimate; the matching
        # outcome's submitted_amount_usd is the confirmed fill), crediting
        # from whichever side the loop happens to reach FIRST -- the
        # candidate, since that loop runs before the outcomes loop below --
        # used the estimate instead of the confirmed amount actually baked
        # into the state's own day_notional_usd, then marked the (order_id,
        # is_adjustment) key as already credited so the outcome side could
        # never correct it. Looked up here, before the candidate loop runs,
        # so the candidate-side branch can defer entirely to a same-state
        # matching outcome instead of guessing which side is authoritative.
        # Codex review, PR #654: "Prefer the authoritative duplicate outcome
        # amount". A single state's broker_outcomes can carry MORE THAN ONE
        # entry for the same order_id (a provisional "submitted" confirmation
        # written first, replaced by a "filled" entry once the fill price is
        # known) -- a first-writer-wins lookup could pick the provisional
        # entry purely because of dict iteration order, permanently losing
        # the more authoritative figure to both the removal-amount credit
        # above and the keeper merge. Each identity keeps whichever entry has
        # a usable amount and, among those, the highest-ranked status (see
        # _OUTCOME_STATUS_RANK).
        _outcome_by_order_id_and_kind: Dict[Tuple[str, bool], Dict[str, Any]] = {}
        if isinstance(_outcomes_for_cross_state_dedup_original, dict):
            for _outcome_value_for_lookup in _outcomes_for_cross_state_dedup_original.values():
                if not isinstance(_outcome_value_for_lookup, dict):
                    continue
                _lookup_order_id = _outcome_value_for_lookup.get("order_id")
                if not isinstance(_lookup_order_id, str) or not _lookup_order_id:
                    continue
                _lookup_key = (_lookup_order_id, _is_settlement_adjustment(_outcome_value_for_lookup))
                _existing_outcome_for_lookup = _outcome_by_order_id_and_kind.get(_lookup_key)
                if _existing_outcome_for_lookup is None:
                    _outcome_by_order_id_and_kind[_lookup_key] = _outcome_value_for_lookup
                    continue
                _existing_amount_for_lookup = _finite_ordinary_amount(
                    _existing_outcome_for_lookup.get("submitted_amount_usd"), _lookup_key[1]
                )
                _candidate_amount_for_lookup = _finite_ordinary_amount(
                    _outcome_value_for_lookup.get("submitted_amount_usd"), _lookup_key[1]
                )
                if _existing_amount_for_lookup is None and _candidate_amount_for_lookup is not None:
                    _outcome_by_order_id_and_kind[_lookup_key] = _outcome_value_for_lookup
                elif (
                    _candidate_amount_for_lookup is not None
                    # >=, not >: Codex review, PR #654: "Prefer the later
                    # outcome when status ranks tie". Two usable entries can
                    # legitimately share the SAME highest-ranked status (an
                    # earlier "filled" $5 confirmation superseded by a later,
                    # corrected "filled" $7 one) -- a strict `>` left the
                    # first-encountered entry selected even though the later
                    # one is the authoritative correction.
                    and _OUTCOME_STATUS_RANK.get(str(_outcome_value_for_lookup.get("status") or ""), -1)
                    >= _OUTCOME_STATUS_RANK.get(str(_existing_outcome_for_lookup.get("status") or ""), -1)
                ):
                    _outcome_by_order_id_and_kind[_lookup_key] = _outcome_value_for_lookup
        # Ticker -> count of this state's OWN broker_outcomes entries that
        # lack a usable order_id AND reached a real placement status --
        # used below to decide whether the ID-less matching pass may
        # safely ignore an amount MISMATCH (see its own note; Codex
        # review, PR #654: "Match sole ID-less fills despite amount
        # corrections"). When exactly one PLACED ID-less outcome exists
        # for a ticker, there is no competing candidate it could be
        # mistaken for, so it may match a sole removed duplicate regardless
        # of whether the two amounts agree. A non-placement outcome (e.g.
        # "failed") is excluded from this count entirely -- it isn't real
        # order evidence and must never make an otherwise-sole placed
        # outcome look ambiguous (Codex review, PR #654: "Ignore
        # non-placement outcomes when consuming ID-less matches").
        _id_less_outcome_ticker_counts: Dict[str, int] = {}
        if isinstance(_outcomes_for_cross_state_dedup_original, dict):
            for _count_key, _outcome_value_for_count in _outcomes_for_cross_state_dedup_original.items():
                if not isinstance(_outcome_value_for_count, dict):
                    continue
                if _outcome_value_for_count.get("status") not in _REAL_ORDER_STATUSES:
                    continue
                _count_order_id = _outcome_value_for_count.get("order_id")
                if isinstance(_count_order_id, str) and _count_order_id:
                    continue
                _count_ticker = _outcome_value_for_count.get("ticker")
                if not isinstance(_count_ticker, str) or not _count_ticker:
                    _count_ticker = _count_key if isinstance(_count_key, str) else None
                if not _count_ticker:
                    continue
                _id_less_outcome_ticker_counts[_count_ticker] = (
                    _id_less_outcome_ticker_counts.get(_count_ticker, 0) + 1
                )
        if isinstance(_candidates_for_cross_state_dedup, list):
            _deduped_candidates = []
            for _row in _candidates_for_cross_state_dedup:
                _row_order_id = _row.get("order_id") if isinstance(_row, dict) else None
                if isinstance(_row, dict) and isinstance(_row_order_id, str) and _row_order_id:
                    _row_is_adjustment = _is_settlement_adjustment(_row)
                    _claimed_map = (
                        _adjustment_order_ids_claimed_across_states
                        if _row_is_adjustment
                        else _order_ids_claimed_across_states
                    )
                    _identity_key, _row_matched_via_constituent_aggregate = _resolve_cross_state_identity_for_row(
                        _claimed_map,
                        _adjustment_broker_order_claimed_accounts if _row_is_adjustment else _broker_order_claimed_accounts,
                        _adjustment_order_claimed_brokers if _row_is_adjustment else _order_claimed_brokers,
                        _broker_scope,
                        _account_scope,
                        _row,
                        _row_order_id,
                    )
                    if _identity_key in _claimed_map:
                        # Codex review, PR #654: "Record scoped accounts
                        # bridged to a legacy claim". This row's OWN
                        # account_scope must be recorded as a claimant of
                        # (broker, order_id) even when it was resolved here
                        # via the unscoped-legacy fallback -- otherwise the
                        # claimed-accounts set never grows past {None}, and
                        # a LATER, genuinely different concrete account
                        # sharing this order_id would ALSO look unambiguous
                        # (still exactly one claimed account) and get
                        # wrongly bridged too.
                        (
                            _adjustment_broker_order_claimed_accounts
                            if _row_is_adjustment
                            else _broker_order_claimed_accounts
                        ).setdefault((_broker_scope, _row_order_id), set()).add(_account_scope)
                        # Codex review, PR #654: "Alias bridged account
                        # scopes in the claimed map". Recording the account
                        # above (so a later THIRD, genuinely different
                        # account is seen as ambiguous) also means the
                        # claimed-accounts set for this (broker, order_id)
                        # is no longer a singleton -- so a LATER row under
                        # this SAME bridged account_scope would find no
                        # exact _claimed_map entry AND no longer qualify
                        # for the unscoped-legacy fallback either (more
                        # than one account has now claimed it), leaving it
                        # unmatched and double-counted. Alias this
                        # account_scope directly onto the same keeper entry
                        # in _claimed_map so future exact lookups under it
                        # succeed regardless of how many accounts are on
                        # record.
                        if _account_scope != _identity_key[1]:
                            _claimed_map[(_broker_scope, _account_scope, _row_order_id)] = _claimed_map[_identity_key]
                        # Codex review, PR #654: "Bridge missing broker
                        # scopes to unambiguous variants" -- mirrors the
                        # account-scope registration/aliasing directly
                        # above, but for the broker field itself.
                        (
                            _adjustment_order_claimed_brokers if _row_is_adjustment else _order_claimed_brokers
                        ).setdefault(_row_order_id, set()).add(_broker_scope)
                        if _broker_scope != _identity_key[0]:
                            _claimed_map[(_broker_scope, _account_scope, _row_order_id)] = _claimed_map[_identity_key]
                        if not _row_is_adjustment:
                            _removed_candidate_count += 1
                        _keeper_state_for_run_check = _claimed_map[_identity_key][3]
                        if not (
                            isinstance(_keeper_state_for_run_check, dict)
                            and _states_share_run_identity(_keeper_state_for_run_check, data)
                        ):
                            _all_removed_duplicates_share_this_trading_day = False
                        elif _keeper_state_for_run_check is not data:
                            _removed_duplicate_keeper_states.setdefault(
                                id(_keeper_state_for_run_check), _keeper_state_for_run_check
                            )
                        # Recorded regardless of status/adjustment kind: this
                        # state's own broker_outcomes can carry a SEPARATE
                        # entry for the exact same physical order that omits
                        # its own order_id (a status-only confirmation
                        # payload), which the outcome loop below can only
                        # recognize as belonging to this now-removed row by
                        # ticker (Codex review, PR #654: "Fold ID-less
                        # outcomes with their removed candidate").
                        _row_ticker_for_removal = _row.get("ticker")
                        if isinstance(_row_ticker_for_removal, str) and _row_ticker_for_removal:
                            _row_amount_for_removal = _finite_ordinary_amount(
                                _row.get("submitted_amount_usd"), _row_is_adjustment
                            )
                            if _row_amount_for_removal is None:
                                _row_amount_for_removal = _finite_ordinary_amount(
                                    _row.get("planned_amount_usd"), _row_is_adjustment
                                )
                            _removed_duplicate_candidate_entries_by_ticker.setdefault(
                                _row_ticker_for_removal, []
                            ).append(
                                {
                                    "amount": _row_amount_for_removal,
                                    "credited": _row.get("status") in _REAL_ORDER_STATUSES,
                                    # Codex review, PR #654: "Match ID-less
                                    # outcomes only to the same order kind".
                                    # An ordinary order and a settlement
                                    # adjustment for the same ticker are two
                                    # different physical things; without the
                                    # kind recorded here, a lone ID-less -$2
                                    # adjustment outcome could be consumed as
                                    # the match for a removed ORDINARY $5 row
                                    # and merge -$2 onto that ordinary
                                    # keeper.
                                    "is_adjustment": _row_is_adjustment,
                                    # The removed row's own placement rank,
                                    # so an already-credited removal is only
                                    # corrected by a matched outcome whose
                                    # status is at least as authoritative --
                                    # the same rule the same-state outcome
                                    # deferral above already applies.
                                    "status_rank": _OUTCOME_STATUS_RANK.get(str(_row.get("status") or ""), -1),
                                    # The removed candidate's OWN keeper entry
                                    # (it always has one -- it was found in
                                    # _claimed_map to be removed at all) --
                                    # carried along so a matched ID-less
                                    # outcome can merge its own more
                                    # authoritative amount/claim metadata
                                    # into the SAME keeper the order-id'd
                                    # removal path would have used (Codex
                                    # review, PR #654: "Merge claim metadata
                                    # from matched ID-less outcomes").
                                    "keeper_entry": _claimed_map[_identity_key],
                                    "claiming_week": _claimed_map[_identity_key][0],
                                }
                            )
                        # Only a row that actually reached a real order
                        # status contributed to the state's own baked-in
                        # counts.submitted/day_notional_usd in the first
                        # place (a dry_run candidate's deterministic
                        # synthetic order_id can repeat across same-day
                        # variants without ever having been counted) --
                        # crediting an unplaced row's count/amount here
                        # would subtract evidence the state's own totals
                        # never actually included (Codex review, PR #654:
                        # "Subtract amounts only for placed duplicate
                        # rows"). Settlement adjustments are deliberately
                        # excluded from counts.submitted by every OTHER
                        # count computation in this module (see
                        # counted_identities above), so a removed
                        # adjustment must never increment
                        # _removed_submitted_count even though it's placed
                        # (Codex review, PR #654: "Exclude adjustment rows
                        # from removed-submission counts") -- its dollar
                        # delta still needs crediting toward
                        # _removed_amount below, just not the count.
                        if _row.get("status") in _REAL_ORDER_STATUSES:
                            if not _row_is_adjustment and _row_order_id not in _removed_count_credited_order_ids:
                                _removed_submitted_count += 1
                                _removed_count_credited_order_ids.add(_row_order_id)
                            # Keyed by (order_id, is_adjustment), not bare
                            # order_id: an ordinary order and its OWN
                            # settlement adjustment can legitimately share
                            # one order_id (the adjustment corrects that
                            # exact order), and both can be duplicated
                            # across state variants at once. Crediting the
                            # ordinary row's $100 first must not suppress
                            # crediting the DISTINCT adjustment's -$2 too --
                            # they're two separate dollar deltas on the same
                            # order_id, and skipping the second left it
                            # double-counted in the later state's own baked
                            # total (Codex review, PR #654: "Namespace
                            # removal credits by adjustment kind").
                            _amount_credit_key = (_row_order_id, _row_is_adjustment)
                            # A same-state broker_outcomes entry for this
                            # exact (order_id, is_adjustment) is the
                            # CONFIRMED fill -- the candidate row's own
                            # planned_amount_usd is only a pre-placement
                            # estimate, and the two can disagree. Crediting
                            # from the candidate here and marking the key
                            # credited would permanently block the outcome
                            # branch below from ever supplying the real,
                            # confirmed amount actually baked into this
                            # state's own day_notional_usd (Codex review, PR
                            # #654: "Reconcile outcome amounts before
                            # crediting removals"). Deferred to the outcome
                            # branch entirely whenever one exists with a real
                            # status; only fall back to the candidate's own
                            # amount when no matching outcome exists at all.
                            # Fresh evidence after the reconcile-outcome-
                            # amounts fix: a matching outcome can be
                            # PLACED without itself carrying a usable
                            # submitted_amount_usd (e.g. a confirmation
                            # that omits the fill price). Deferring to it
                            # unconditionally left NEITHER side crediting
                            # anything -- the candidate's own real amount
                            # was silently dropped instead of being used
                            # as the fallback it always was before this
                            # reconciliation existed (Codex review, PR
                            # #654: "Fall back to the candidate when the
                            # outcome lacks an amount"). Only defer when
                            # the matching outcome ALSO has a usable
                            # amount of its own.
                            _matching_outcome_for_row = _outcome_by_order_id_and_kind.get(
                                (_row_order_id, _row_is_adjustment)
                            )
                            _matching_outcome_amount = (
                                _finite_ordinary_amount(_matching_outcome_for_row.get("submitted_amount_usd"), _row_is_adjustment)
                                if _matching_outcome_for_row is not None
                                else None
                            )
                            _defer_amount_to_outcome = (
                                _matching_outcome_for_row is not None
                                and _matching_outcome_for_row.get("status") in _REAL_ORDER_STATUSES
                                and _matching_outcome_amount is not None
                                # Codex review, PR #654: "Prefer a stronger
                                # candidate fill over a stale outcome". A
                                # placed outcome isn't automatically more
                                # authoritative than the candidate row it's
                                # reconciling against -- a "filled" $8
                                # candidate must not defer to a same-state
                                # outcome still stuck at "submitted" $5.
                                # Only defer when the outcome's own status
                                # ranks at least as strong as the row's.
                                and _OUTCOME_STATUS_RANK.get(_matching_outcome_for_row.get("status"), -1)
                                >= _OUTCOME_STATUS_RANK.get(_row.get("status"), -1)
                            )
                            if not _defer_amount_to_outcome and _amount_credit_key not in _removed_amount_credited_order_ids:
                                _dropped_amount = _finite_ordinary_amount(_row.get("submitted_amount_usd"), _row_is_adjustment)
                                # Codex review, PR #654: "Do not merge planned
                                # fallbacks as authoritative fills". A dropped
                                # amount sourced from planned_amount_usd is
                                # only a pre-placement ESTIMATE, correct to
                                # subtract from this state's own totals (the
                                # estimate is exactly what got baked into
                                # them), but never authoritative enough to
                                # overwrite a keeper's own confirmed fill.
                                _dropped_amount_is_confirmed = _dropped_amount is not None
                                if _dropped_amount is None:
                                    _dropped_amount = _finite_ordinary_amount(_row.get("planned_amount_usd"), _row_is_adjustment)
                                if _dropped_amount is not None:
                                    _removed_amount += _dropped_amount
                                    if _claimed_map[_identity_key][0] != _state_iso_week:
                                        _removed_amount_cross_week += _dropped_amount
                                    _removed_amount_credited_order_ids.add(_amount_credit_key)
                                    # Codex review, PR #654: "Do not merge
                                    # aggregate totals into one constituent".
                                    # ``_dropped_amount`` is only a valid
                                    # correction for ``_identity_key``'s own
                                    # keeper when this row represents
                                    # EXACTLY the same set of physical orders
                                    # the keeper does -- checked by comparing
                                    # their full constituent-ID sets rather
                                    # than trusting a shared identity alone.
                                    # Two directions both go wrong otherwise:
                                    # an aggregate row [X, Y] whose identity
                                    # resolves via the multi-constituent
                                    # fallback (``_row_matched_via_
                                    # constituent_aggregate``) to X's own
                                    # single-order keeper would overwrite
                                    # that keeper with the WHOLE combined
                                    # total while Y's own separate keeper
                                    # still counts it too; conversely a
                                    # standalone row for constituent X ALONE,
                                    # whose bare identity was directly
                                    # aliased onto an aggregate [X, Y]
                                    # keeper by _register_seen_claim, would
                                    # overwrite that keeper's correctly
                                    # combined total down to X's own smaller
                                    # amount, silently dropping Y's share.
                                    # In both cases this row's own dollars
                                    # are already fully accounted for by its
                                    # constituents' existing keeper(s), so
                                    # the row is dropped (via ``continue``
                                    # below) and its own state-local totals
                                    # corrected (already done above) WITHOUT
                                    # also treating its amount as an
                                    # authoritative correction for a keeper
                                    # it doesn't exactly match.
                                    if (
                                        _dropped_amount_is_confirmed
                                        and not _row_matched_via_constituent_aggregate
                                        and _order_constituent_ids(_row_order_id, _row)
                                        == _keeper_constituent_ids(_claimed_map[_identity_key])
                                    ):
                                        _merge_authoritative_amount_into_keeper(
                                            _claimed_map[_identity_key], _dropped_amount, _row, _states_merged_into
                                        )
                                        # Codex review, PR #654: "Preserve
                                        # metadata from amount-less matching
                                        # outcomes". The matching outcome
                                        # was skipped as the amount SOURCE
                                        # only because it lacks a usable
                                        # submitted_amount_usd -- it can
                                        # still carry its own, more
                                        # authoritative claim_breakdown. The
                                        # outcome branch below can never
                                        # invoke the keeper merge for THIS
                                        # outcome (it has no amount of its
                                        # own to credit), so its metadata is
                                        # merged here, alongside the
                                        # candidate's own fallback amount,
                                        # or it would vanish along with the
                                        # outcome once dropped.
                                        if _matching_outcome_for_row is not None and _has_usable_claim_breakdown(
                                            _matching_outcome_for_row
                                        ):
                                            _merge_authoritative_amount_into_keeper(
                                                _claimed_map[_identity_key],
                                                _dropped_amount,
                                                _matching_outcome_for_row,
                                                _states_merged_into,
                                            )
                        continue
                    _seen_map = (
                        _adjustment_order_ids_seen_this_state if _row_is_adjustment else _order_ids_seen_this_state
                    )
                    _seen_map.setdefault(
                        _row_order_id, (_row, _outcome_by_order_id_and_kind.get((_row_order_id, _row_is_adjustment)))
                    )
                _deduped_candidates.append(_row)
            data["buy_candidates"] = _deduped_candidates
        _outcomes_for_cross_state_dedup = data.get("broker_outcomes")
        if isinstance(_outcomes_for_cross_state_dedup, dict):
            _deduped_outcomes = {}
            for _outcome_key, _outcome_value in _outcomes_for_cross_state_dedup.items():
                _outcome_order_id = _outcome_value.get("order_id") if isinstance(_outcome_value, dict) else None
                if isinstance(_outcome_value, dict) and isinstance(_outcome_order_id, str) and _outcome_order_id:
                    _outcome_is_adjustment = _is_settlement_adjustment(_outcome_value)
                    _claimed_map = (
                        _adjustment_order_ids_claimed_across_states
                        if _outcome_is_adjustment
                        else _order_ids_claimed_across_states
                    )
                    _identity_key, _outcome_matched_via_constituent_aggregate = _resolve_cross_state_identity_for_row(
                        _claimed_map,
                        _adjustment_broker_order_claimed_accounts
                        if _outcome_is_adjustment
                        else _broker_order_claimed_accounts,
                        _adjustment_order_claimed_brokers if _outcome_is_adjustment else _order_claimed_brokers,
                        _broker_scope,
                        _account_scope,
                        _outcome_value,
                        _outcome_order_id,
                    )
                    if _identity_key in _claimed_map:
                        # See the candidate-row branch's own note above
                        # (Codex review, PR #654: "Record scoped accounts
                        # bridged to a legacy claim"; "Alias bridged
                        # account scopes in the claimed map").
                        (
                            _adjustment_broker_order_claimed_accounts
                            if _outcome_is_adjustment
                            else _broker_order_claimed_accounts
                        ).setdefault((_broker_scope, _outcome_order_id), set()).add(_account_scope)
                        if _account_scope != _identity_key[1]:
                            _claimed_map[(_broker_scope, _account_scope, _outcome_order_id)] = _claimed_map[
                                _identity_key
                            ]
                        # See the candidate-row branch's own note above
                        # (Codex review, PR #654: "Bridge missing broker
                        # scopes to unambiguous variants").
                        (
                            _adjustment_order_claimed_brokers if _outcome_is_adjustment else _order_claimed_brokers
                        ).setdefault(_outcome_order_id, set()).add(_broker_scope)
                        if _broker_scope != _identity_key[0]:
                            _claimed_map[(_broker_scope, _account_scope, _outcome_order_id)] = _claimed_map[
                                _identity_key
                            ]
                        _keeper_state_for_run_check = _claimed_map[_identity_key][3]
                        if not (
                            isinstance(_keeper_state_for_run_check, dict)
                            and _states_share_run_identity(_keeper_state_for_run_check, data)
                        ):
                            _all_removed_duplicates_share_this_trading_day = False
                        elif _keeper_state_for_run_check is not data:
                            _removed_duplicate_keeper_states.setdefault(
                                id(_keeper_state_for_run_check), _keeper_state_for_run_check
                            )
                        # Fresh evidence after the candidate-row removal
                        # tracking above: a later state can represent the
                        # duplicate order ONLY in broker_outcomes, with no
                        # buy_candidates row of its own for it -- dropping
                        # it here without also crediting the removal left
                        # this state's counts.submitted/budget.
                        # day_notional_usd unchanged, so it still reported
                        # a second submission and doubled executed
                        # notional for the same physical order (Codex
                        # review, PR #654: "Adjust totals for outcome-only
                        # duplicates"). Guarded by the two credited-sets
                        # above (not one combined set) so an order_id whose
                        # candidate row already credited its COUNT but not
                        # its AMOUNT (or vice versa) still lets this outcome
                        # credit whichever piece is still missing.
                        _outcome_is_placed = _outcome_value.get("status") in _REAL_ORDER_STATUSES
                        if (
                            not _outcome_is_adjustment
                            and _outcome_is_placed
                            and _outcome_order_id not in _removed_count_credited_order_ids
                        ):
                            _removed_submitted_count += 1
                            _removed_count_credited_order_ids.add(_outcome_order_id)
                        # Same two gates as the candidate-row branch above:
                        # only a PLACED outcome ever contributed to the
                        # state's own stored day_notional_usd in the first
                        # place -- a dry_run outcome's amount was never
                        # included, so crediting it here would subtract
                        # evidence that was never actually there (Codex
                        # review, PR #654: "Gate removed outcome amounts on
                        # placement status"). And the credit key includes
                        # is_adjustment, matching the candidate-row branch's
                        # own fix, so an ordinary outcome and an adjustment
                        # outcome sharing one order_id both get credited
                        # (Codex review, PR #654: "Namespace removal credits
                        # by adjustment kind").
                        _amount_credit_key = (_outcome_order_id, _outcome_is_adjustment)
                        # A state's own broker_outcomes can carry more than
                        # one entry for this exact order_id (see
                        # _outcome_by_order_id_and_kind's own note above) --
                        # every one of them still gets dropped from
                        # _deduped_outcomes below via `continue`, but only
                        # the single strongest/latest-ranked entry may ever
                        # contribute the removed amount/keeper merge, or
                        # whichever entry dict iteration happened to reach
                        # first would win regardless of which one actually
                        # carries the authoritative figure (Codex review, PR
                        # #654: "Prefer the authoritative duplicate outcome
                        # amount").
                        _is_authoritative_outcome_entry = _outcome_value is _outcome_by_order_id_and_kind.get(
                            (_outcome_order_id, _outcome_is_adjustment)
                        )
                        if (
                            _outcome_is_placed
                            and _is_authoritative_outcome_entry
                            and _amount_credit_key not in _removed_amount_credited_order_ids
                        ):
                            _dropped_outcome_amount = _finite_ordinary_amount(
                                _outcome_value.get("submitted_amount_usd"), _outcome_is_adjustment
                            )
                            if _dropped_outcome_amount is not None:
                                _removed_amount += _dropped_outcome_amount
                                if _claimed_map[_identity_key][0] != _state_iso_week:
                                    _removed_amount_cross_week += _dropped_outcome_amount
                                _removed_amount_credited_order_ids.add(_amount_credit_key)
                                # The outcome's own submitted_amount_usd is
                                # always a confirmed fill (there is no
                                # planned-amount fallback on this side), so
                                # it's always safe to merge as authoritative
                                # -- UNLESS this outcome doesn't represent
                                # exactly the same set of physical orders as
                                # the keeper it would merge into (see the
                                # candidate-row branch's own note above,
                                # Codex review, PR #654: "Do not merge
                                # aggregate totals into one constituent").
                                if (
                                    not _outcome_matched_via_constituent_aggregate
                                    and _order_constituent_ids(_outcome_order_id, _outcome_value)
                                    == _keeper_constituent_ids(_claimed_map[_identity_key])
                                ):
                                    _merge_authoritative_amount_into_keeper(
                                        _claimed_map[_identity_key],
                                        _dropped_outcome_amount,
                                        _outcome_value,
                                        _states_merged_into,
                                    )
                        continue
                    _seen_map = (
                        _adjustment_order_ids_seen_this_state
                        if _outcome_is_adjustment
                        else _order_ids_seen_this_state
                    )
                    _seen_map.setdefault(_outcome_order_id, (_outcome_value, _outcome_value))
                elif isinstance(_outcome_value, dict) and _removed_duplicate_candidate_entries_by_ticker:
                    # Codex review, PR #654: "Fold ID-less outcomes with their
                    # removed candidate". This entry has no order_id of its
                    # own, so it can never be matched against the claimed-
                    # across-states maps above -- but it can still be the
                    # SAME physical order as a candidate row just removed as
                    # a cross-file duplicate in THIS state (a status-only
                    # confirmation payload written without the order_id the
                    # candidate row already carries). Left in place, it
                    # would survive as the row's only remaining evidence and
                    # get synthesized back into a fresh (duplicate) candidate
                    # by the reconciliation pass further down. Matched by
                    # ticker -- the same identity broker_outcomes entries use
                    # elsewhere in this module when they lack an order_id
                    # (see the ticker-recovery pass above).
                    _outcome_ticker_for_removal = _outcome_value.get("ticker")
                    if not isinstance(_outcome_ticker_for_removal, str) or not _outcome_ticker_for_removal:
                        _outcome_ticker_for_removal = _outcome_key if isinstance(_outcome_key, str) else None
                    _pending_removed_entries = (
                        _removed_duplicate_candidate_entries_by_ticker.get(_outcome_ticker_for_removal)
                        if _outcome_ticker_for_removal is not None
                        else None
                    )
                    if _pending_removed_entries:
                        # One-to-one, not "any outcome sharing the ticker": a
                        # state can carry a genuine SECOND, still-retained
                        # candidate for the same ticker whose own ID-less
                        # outcome must survive untouched (Codex review, PR
                        # #654: "Preserve unrelated ID-less same-ticker
                        # outcomes"). Matched by AMOUNT first -- dict
                        # iteration order can present the genuine outcome
                        # before the removed duplicate's own one, and a
                        # bare positional/count consumption would then claim
                        # the wrong entry (Codex review, PR #654: "Match
                        # ID-less outcomes to the removed candidate"). Only
                        # falls back to consuming the sole remaining pending
                        # entry when there is exactly one and no amount is
                        # available to disambiguate further.
                        _outcome_is_adjustment_for_match = _is_settlement_adjustment(_outcome_value)
                        _outcome_amount_for_removal = _finite_ordinary_amount(
                            _outcome_value.get("submitted_amount_usd"), _outcome_is_adjustment_for_match
                        )
                        # Codex review, PR #654: "Match ID-less outcomes only
                        # to the same order kind". Every match below -- the
                        # amount match AND the sole-pending-entry fallback --
                        # is restricted to the pending removals of the SAME
                        # kind as this outcome. A state can legitimately
                        # carry an ordinary duplicate and its own settlement
                        # adjustment for one ticker at once, and the
                        # fallback's "exactly one pending entry" test read
                        # the WHOLE per-ticker list, so a lone ID-less
                        # adjustment was matched to (and consumed) an
                        # ordinary removed row, dropping the adjustment from
                        # broker_outcomes and merging its negative delta onto
                        # the ordinary keeper.
                        _eligible_removed_indices = [
                            _pending_index
                            for _pending_index, _pending_entry in enumerate(_pending_removed_entries)
                            if bool(_pending_entry["is_adjustment"]) is _outcome_is_adjustment_for_match
                        ]
                        # Codex review, PR #654: "Disambiguate equal-amount
                        # ID-less outcomes". Two DIFFERENT removed
                        # duplicates can legitimately share the same
                        # amount (two separate $5 orders) -- matching
                        # whichever pending entry is encountered FIRST
                        # would then depend on dict iteration order again,
                        # exactly the hazard amount-matching was introduced
                        # to avoid, and could attribute one order's claim
                        # metadata to the OTHER's keeper. Only accept the
                        # amount match when it is unambiguous (exactly one
                        # pending entry shares this amount); a tie is left
                        # unmatched rather than guessed at.
                        _amount_match_indices = [
                            _pending_index
                            for _pending_index in _eligible_removed_indices
                            if _pending_removed_entries[_pending_index]["amount"] is not None
                            and _outcome_amount_for_removal is not None
                            and _pending_removed_entries[_pending_index]["amount"] == _outcome_amount_for_removal
                        ]
                        _match_index = _amount_match_indices[0] if len(_amount_match_indices) == 1 else None
                        # Fall back to the sole remaining pending entry when
                        # this outcome's own amount is unknown, OR when it's
                        # known but this is the ONLY ID-less outcome this
                        # state has for this ticker at all (no competing
                        # same-ticker outcome it could be mistaken for) --
                        # a known amount that merely CORRECTS the removed
                        # candidate's own estimate (e.g. planned $5,
                        # confirmed fill $7) must not be left unmatched just
                        # because the two disagree (Codex review, PR #654:
                        # "Match sole ID-less fills despite amount
                        # corrections"). Still requires amount-based
                        # disambiguation whenever MORE than one same-ticker
                        # ID-less outcome exists, matching "Preserve
                        # unrelated ID-less same-ticker outcomes" above.
                        # Codex review, PR #654: "Ignore non-placement
                        # outcomes when consuming ID-less matches". A
                        # non-placement outcome (e.g. "failed") isn't real
                        # order evidence and has no amount of its own to
                        # disambiguate with -- letting it consume the sole
                        # pending match via this fallback starves a LATER,
                        # genuinely placed same-ticker outcome (the actual
                        # duplicate's real fill) of anything left to match.
                        if (
                            _match_index is None
                            and _outcome_value.get("status") in _REAL_ORDER_STATUSES
                            and len(_eligible_removed_indices) == 1
                            and (
                                _outcome_amount_for_removal is None
                                or _id_less_outcome_ticker_counts.get(_outcome_ticker_for_removal, 0) == 1
                            )
                        ):
                            _match_index = _eligible_removed_indices[0]
                        if _match_index is not None:
                            _matched_entry = _pending_removed_entries.pop(_match_index)
                            # Codex review, PR #654: "Credit removed ID-less
                            # outcomes to state totals". The matched removed
                            # candidate's own row can have a NON-real status
                            # (e.g. "selected") and so never credited
                            # anything itself -- this ID-less outcome can be
                            # that same order's ONLY placement evidence. Only
                            # credit here when the candidate side did not
                            # already (avoiding a double credit when both
                            # sides independently reached a real status).
                            if (
                                not _matched_entry["credited"]
                                and _outcome_value.get("status") in _REAL_ORDER_STATUSES
                                and _outcome_amount_for_removal is not None
                            ):
                                # Codex review, PR #654: "Exclude ID-less
                                # settlement adjustments from submission
                                # credits". A placed adjustment is never
                                # counted in counts.submitted by any OTHER
                                # reconciliation path in this module -- only
                                # its dollar delta is real; the count itself
                                # must stay excluded here too.
                                if not _outcome_is_adjustment_for_match:
                                    _removed_submitted_count += 1
                                _removed_amount += _outcome_amount_for_removal
                                # Codex review, PR #654: "Credit ID-less
                                # removals against cross-week totals". Same
                                # cross-week rule the order-id'd removal
                                # paths already apply: only the portion
                                # claimed in a DIFFERENT ISO week than this
                                # state's own may ever be subtracted from a
                                # cumulative weekly total.
                                if _matched_entry["claiming_week"] != _state_iso_week:
                                    _removed_amount_cross_week += _outcome_amount_for_removal
                            elif (
                                # Codex review, PR #654: "Replace candidate
                                # removal credit with the matched fill
                                # amount". The candidate side already
                                # credited this removal, but from its OWN
                                # amount -- a pre-placement ESTIMATE when
                                # this ID-less outcome is the confirmed
                                # fill. (The order-id'd path reconciles the
                                # two via _defer_amount_to_outcome; an
                                # ID-less outcome is invisible to that
                                # lookup, so the candidate always wins
                                # there.) The state's own baked totals were
                                # built from the CONFIRMED figure, so
                                # subtracting only the estimate left the
                                # difference behind -- and the partial-
                                # variant merge below then carries that
                                # phantom remainder into the keeper, which
                                # the keeper merge just corrected to the
                                # confirmed amount. Credit the delta so the
                                # removal matches what was actually baked
                                # in.
                                _outcome_value.get("status") in _REAL_ORDER_STATUSES
                                and _outcome_amount_for_removal is not None
                                and _matched_entry["amount"] is not None
                                and _outcome_amount_for_removal != _matched_entry["amount"]
                                # Only a CONFIRMATION may correct the credit,
                                # never a staler status walking it back --
                                # the same rank rule the same-state outcome
                                # deferral applies to the order-id'd path.
                                and _OUTCOME_STATUS_RANK.get(_outcome_value.get("status"), -1)
                                >= _matched_entry["status_rank"]
                            ):
                                _removal_credit_correction = _outcome_amount_for_removal - _matched_entry["amount"]
                                _removed_amount += _removal_credit_correction
                                if _matched_entry["claiming_week"] != _state_iso_week:
                                    _removed_amount_cross_week += _removal_credit_correction
                            if _outcome_amount_for_removal is not None:
                                # Codex review, PR #654: "Merge claim
                                # metadata from matched ID-less outcomes".
                                # This outcome's amount/claim breakdown was
                                # never visible to the order-id'd merge path
                                # above (it has no order_id of its own), so
                                # it's merged into the SAME keeper its
                                # matched removed candidate already points
                                # to -- independent of whether the state-
                                # level count/amount credit above fired.
                                _merge_authoritative_amount_into_keeper(
                                    _matched_entry["keeper_entry"],
                                    _outcome_amount_for_removal,
                                    _outcome_value,
                                    _states_merged_into,
                                )
                            continue
                _deduped_outcomes[_outcome_key] = _outcome_value
            data["broker_outcomes"] = _deduped_outcomes
        # Codex review, PR #654: "Register every constituent ID from
        # aggregate keepers". When the state that FIRST claims an identity
        # is itself an aggregated whole-share row (its own scalar
        # order_id is only the newest of several physical orders folded
        # together -- see whole_share_order_ids and
        # _resolve_cross_state_identity_for_row's own note), only
        # registering that scalar ID left every OTHER constituent ID
        # unclaimed. A later standalone row for one of those other IDs
        # then found no claim at all and survived as its own "new" order,
        # double-counting the dollars the aggregate already included.
        # Every constituent ID is registered here, pointing at the SAME
        # keeper entry as the scalar ID.
        def _register_seen_claim(
            _claimed_map_to_register: Dict[Tuple[Any, Any, str], _KeeperEntry],
            _claimed_accounts_to_register: Dict[Tuple[Any, str], set],
            _claimed_brokers_to_register: Dict[str, set],
            _seen_id: str,
            _seen_row_arg: Optional[Dict[str, Any]],
            _seen_outcome_arg: Optional[Dict[str, Any]],
            _broker_scope_arg: Any,
            _account_scope_arg: Any,
            _state_iso_week_arg: Optional[Tuple[int, int]],
            _data_arg: Dict[str, Any],
        ) -> None:
            _keeper_entry_to_register = (_state_iso_week_arg, _seen_row_arg, _seen_outcome_arg, _data_arg)
            _claimed_map_to_register.setdefault(
                (_broker_scope_arg, _account_scope_arg, _seen_id), _keeper_entry_to_register
            )
            _claimed_accounts_to_register.setdefault((_broker_scope_arg, _seen_id), set()).add(_account_scope_arg)
            # Codex review, PR #654: "Bridge missing broker scopes to
            # unambiguous variants" -- mirrors the account-scope
            # registration directly above, but keyed by bare order_id.
            _claimed_brokers_to_register.setdefault(_seen_id, set()).add(_broker_scope_arg)
            for _extra_source in (_seen_row_arg, _seen_outcome_arg):
                if not isinstance(_extra_source, dict):
                    continue
                _extra_ids = _extra_source.get("whole_share_order_ids")
                if not isinstance(_extra_ids, list):
                    continue
                for _extra_id in _extra_ids:
                    if not isinstance(_extra_id, str) or not _extra_id or _extra_id == _seen_id:
                        continue
                    _claimed_map_to_register.setdefault(
                        (_broker_scope_arg, _account_scope_arg, _extra_id), _keeper_entry_to_register
                    )
                    _claimed_accounts_to_register.setdefault((_broker_scope_arg, _extra_id), set()).add(
                        _account_scope_arg
                    )
                    _claimed_brokers_to_register.setdefault(_extra_id, set()).add(_broker_scope_arg)

        for _seen_order_id, _seen_entry in _order_ids_seen_this_state.items():
            _seen_row, _seen_outcome = _seen_entry
            _register_seen_claim(
                _order_ids_claimed_across_states,
                _broker_order_claimed_accounts,
                _order_claimed_brokers,
                _seen_order_id,
                _seen_row,
                _seen_outcome,
                _broker_scope,
                _account_scope,
                _state_iso_week,
                data,
            )
        for _seen_adjustment_order_id, _seen_adjustment_entry in _adjustment_order_ids_seen_this_state.items():
            _seen_adjustment_row, _seen_adjustment_outcome = _seen_adjustment_entry
            _register_seen_claim(
                _adjustment_order_ids_claimed_across_states,
                _adjustment_broker_order_claimed_accounts,
                _adjustment_order_claimed_brokers,
                _seen_adjustment_order_id,
                _seen_adjustment_row,
                _seen_adjustment_outcome,
                _broker_scope,
                _account_scope,
                _state_iso_week,
                data,
            )
        if _removed_candidate_count or _removed_submitted_count or _removed_amount:
            _counts_for_cross_state_adjustment = data.get("counts")
            if isinstance(_counts_for_cross_state_adjustment, dict):
                _existing_buy_candidates_count = _finite_float(
                    _counts_for_cross_state_adjustment.get("buy_candidates")
                )
                if _existing_buy_candidates_count is not None:
                    _counts_for_cross_state_adjustment["buy_candidates"] = max(
                        0, _existing_buy_candidates_count - _removed_candidate_count
                    )
                _existing_submitted_count = _finite_float(_counts_for_cross_state_adjustment.get("submitted"))
                if _existing_submitted_count is not None:
                    _counts_for_cross_state_adjustment["submitted"] = max(
                        0, _existing_submitted_count - _removed_submitted_count
                    )
            # Codex review, PR #654: "Adjust the top-level submitted count
            # after deduplication". counts.submitted is the COMPLETED-run
            # field name; the morning/midday blocked/partial artifacts
            # _has_submitted_evidence() admits normally carry no counts.
            # submitted at all and record the real figure under the
            # top-level orders_submitted_count instead -- which is exactly
            # the fallback build_performance_snapshot() reads when
            # counts.submitted is absent. Decrementing only the nested
            # field left that fallback reporting the pre-dedup count, so an
            # earlier state holding X plus a later one holding duplicate X
            # and new Y still reported three submissions for the two
            # retained orders. Adjusted alongside the nested field (both
            # count the same quantity, so both stay correct when both are
            # present) rather than only when the nested one is missing --
            # consumers other than the snapshot read the top-level field
            # directly.
            _existing_top_level_submitted = _finite_float(data.get("orders_submitted_count"))
            if _existing_top_level_submitted is not None:
                data["orders_submitted_count"] = max(0, _existing_top_level_submitted - _removed_submitted_count)
            _budget_for_cross_state_adjustment = data.get("budget")
            if isinstance(_budget_for_cross_state_adjustment, dict) and _removed_amount:
                # day_notional_usd/hypothetical_day_spend_usd are THIS
                # state's own row-local total -- summed directly from ITS
                # OWN candidates -- so subtracting a removed row's full
                # amount from them is exactly correct.
                for _budget_key in ("day_notional_usd", "hypothetical_day_spend_usd"):
                    _existing_budget_value = _finite_float(_budget_for_cross_state_adjustment.get(_budget_key))
                    if _existing_budget_value is not None:
                        _budget_for_cross_state_adjustment[_budget_key] = _existing_budget_value - _removed_amount
                # week_notional_usd/hypothetical_week_spend_usd are NOT
                # row-local: they're the running week-TO-DATE total as of
                # this state's trading day. When the duplicate was claimed
                # by an EARLIER state in the SAME ISO week, that total
                # already includes this exact dollar amount via that
                # earlier state -- subtracting it again here would corrupt
                # an already-correct cumulative value (two same-day state
                # variants reporting the same $5 order and the same $5
                # week-to-date total would wrongly report $0 for the week,
                # even though the retained earlier variant still supplies
                # that real $5 -- Codex review, PR #654: "Preserve weekly
                # totals across same-day state variants"). But when the
                # duplicate was claimed by an earlier state in a DIFFERENT
                # (necessarily earlier) ISO week, THIS state's own
                # week-to-date total wrongly attributes a prior week's
                # order to its own new week -- e.g. a Friday $5 order
                # repeated in a Monday state whose "new week" total is
                # that same $5 -- and must have it removed too, or
                # _latest_week_notional() reports a nonzero week total for
                # a week whose only order was just dropped as a duplicate
                # (Codex review, PR #654: "Correct duplicate weekly totals
                # across ISO-week boundaries"). Only ``_removed_amount_
                # cross_week`` -- the portion actually claimed in a
                # DIFFERENT week -- is ever subtracted from these fields.
                if _removed_amount_cross_week:
                    for _budget_key in ("week_notional_usd", "hypothetical_week_spend_usd"):
                        _existing_budget_value = _finite_float(_budget_for_cross_state_adjustment.get(_budget_key))
                        if _existing_budget_value is not None:
                            _budget_for_cross_state_adjustment[_budget_key] = (
                                _existing_budget_value - _removed_amount_cross_week
                            )
        _final_candidates_value = data.get("buy_candidates")
        _final_candidate_count = len(_final_candidates_value) if isinstance(_final_candidates_value, list) else 0
        _final_outcomes_value = data.get("broker_outcomes")
        _final_outcome_count = len(_final_outcomes_value) if isinstance(_final_outcomes_value, dict) else 0
        if (
            (_original_candidate_count or _original_outcome_count)
            and _final_candidate_count == 0
            and _final_outcome_count == 0
            and _all_removed_duplicates_share_this_trading_day
        ):
            _states_fully_absorbed_as_duplicates.add(id(data))
        elif (
            (_final_candidate_count or _final_outcome_count)
            and _all_removed_duplicates_share_this_trading_day
            and len(_removed_duplicate_keeper_states) == 1
        ):
            # Codex review, PR #654 discussion_r3853577540: "Merge partial
            # variants before counting run days". This state still holds rows
            # of its own, so the husk collapse above cannot touch it -- but
            # every duplicate it DID shed went to one single sibling state
            # that shares its full run identity, which is only possible when
            # the two are variants of the same physical run. Fold this
            # state's remainder into that sibling so the run reports the
            # union of their distinct orders under ONE run day instead of
            # two.
            #
            # Deliberately requires EXACTLY one keeper: when a state's
            # removals funnel into several different siblings there is no
            # single defensible destination for its remaining rows, and
            # guessing one would attribute real orders to a run that never
            # placed them. Such a state is left alone, exactly as before.
            _merge_destination = next(iter(_removed_duplicate_keeper_states.values()))
            # Follow the chain: the sibling that claimed these duplicates may
            # itself already have been folded into a third state, in which
            # case IT is about to be dropped from `kept` and merging into it
            # would discard this state's rows with it.
            _chain_guard = 0
            while (
                id(_merge_destination) in _states_merged_into
                # Defensive bound only -- destinations always point BACKWARD
                # in the accumulation order, so the chain is acyclic by
                # construction and this can never spin.
                and _chain_guard <= len(kept)
            ):
                _merge_destination = _states_merged_into[id(_merge_destination)]
                _chain_guard += 1
            if (
                _merge_destination is not data
                and id(_merge_destination) not in _states_fully_absorbed_as_duplicates
                # The EXPLICIT identity check, not the trading_day-only
                # fallback the husk collapse accepts -- see
                # _states_share_explicit_run_identity() for why moving live
                # rows between states demands the stronger evidence. Re-checked
                # against the chain's final destination, not just the immediate
                # keeper recorded above.
                and _states_share_explicit_run_identity(_merge_destination, data)
            ):
                _merge_partial_state_into_keeper(data, _merge_destination)
                _states_fully_absorbed_as_duplicates.add(id(data))
                _states_merged_into[id(data)] = _merge_destination
    if _states_fully_absorbed_as_duplicates:
        kept[:] = [state for state in kept if id(state) not in _states_fully_absorbed_as_duplicates]
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
                    _is_adjustment = _is_settlement_adjustment(row)
                    _planned = _finite_float(row.get("planned_amount_usd"))
                    if _planned is not None and _planned < 0 and not _is_adjustment:
                        _planned = None
                    if _planned is None:
                        _submitted = _finite_float(row.get("submitted_amount_usd"))
                        if _submitted is not None and _submitted < 0 and not _is_adjustment:
                            _submitted = None
                        row["planned_amount_usd"] = _submitted
                    # _candidate_fixed_base_usd() already rejects a
                    # nonpositive selected_base_amount_usd (falling back
                    # to the run's flat default), but its own `float(raw)`
                    # call accepts a JSON boolean as 1.0/0.0 -- a positive
                    # `true` survived that check and rendered a $1 fixed
                    # baseline (masking the run's real default, often $2)
                    # instead of falling back to it (Codex review, PR
                    # #654: "Sanitize candidate-level fixed bases in
                    # retained rows"). _finite_float() rejects bool, so
                    # sanitizing it here reaches _candidate_fixed_base_usd()
                    # as None and correctly falls back to default_base.
                    if row.get("selected_base_amount_usd") is not None:
                        row["selected_base_amount_usd"] = _finite_float(row.get("selected_base_amount_usd"))
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
                    1 for r in candidates if isinstance(r, dict) and not _is_settlement_adjustment(r)
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
            # Status rank (see _OUTCOME_STATUS_RANK) of whichever
            # broker_outcomes entry last set amounts_by_ticker[identity] --
            # used only by the outcome-folding loop below to stop a
            # lower-authority outcome from overwriting an already-confirmed
            # amount (Codex review, PR #654: "Preserve stronger outcome
            # amounts during same-state folding").
            _amounts_by_ticker_source_rank: Dict[str, int] = {}
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
                # `not isinstance(..., str)` alone let an empty string
                # through unchanged -- a blank ticker is exactly as useless
                # as a missing one for every ticker-keyed lookup below
                # (seen_tickers, existing_tickers, sector attribution), but
                # unlike None it read as "already valid", so the recovery
                # pass just below never got a chance to backfill it from a
                # matching outcome (Codex review, PR #654: "Recover blank
                # candidate tickers from matching outcomes").
                _row_ticker = _row.get("ticker")
                if not isinstance(_row_ticker, str) or not _row_ticker:
                    _row["ticker"] = None
                if not isinstance(_row.get("order_id"), str):
                    _row["order_id"] = None
            # A candidate row can be admitted with no usable ticker of its
            # own (blank or missing, just normalized above) while its
            # order-ID-matched broker outcome carries the real one --
            # without this, the row's identity/sector attribution stayed
            # blank even after the outcome-reconciliation pass further down
            # backfills its amount and status, since that backfill only
            # ever touches those two fields. Read directly off `data`
            # rather than the `broker_outcomes` local, which isn't bound
            # until much later in this function (Codex review, PR #654:
            # "Recover blank candidate tickers from matching outcomes").
            _broker_outcomes_for_ticker_recovery = data.get("broker_outcomes")
            if isinstance(_broker_outcomes_for_ticker_recovery, dict):
                _outcome_ticker_by_order_id: Dict[Any, str] = {}
                for _outcome_key, _outcome_value in _broker_outcomes_for_ticker_recovery.items():
                    if not isinstance(_outcome_value, dict):
                        continue
                    _outcome_oid = _outcome_value.get("order_id")
                    if not isinstance(_outcome_oid, str) or not _outcome_oid:
                        continue
                    _outcome_ticker = _outcome_value.get("ticker")
                    if not isinstance(_outcome_ticker, str) or not _outcome_ticker:
                        _outcome_ticker = _outcome_key if isinstance(_outcome_key, str) and _outcome_key else None
                    if _outcome_ticker:
                        _outcome_ticker_by_order_id.setdefault(_outcome_oid, _outcome_ticker)
                for _row in _all_dict_candidate_rows:
                    if _row.get("ticker") is None and _row.get("order_id"):
                        _matched_ticker = _outcome_ticker_by_order_id.get(_row["order_id"])
                        if _matched_ticker:
                            _row["ticker"] = _matched_ticker
            _ordered_candidate_rows = [
                row for row in _candidate_rows if not _is_settlement_adjustment(row)
            ] + [row for row in _candidate_rows if _is_settlement_adjustment(row)]
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
            # The adjustment-only twin of candidate_identity_by_order_id --
            # populated whenever the row IS a settlement adjustment, instead
            # of never (candidate_identity_by_order_id's own registration
            # deliberately excludes adjustments). The outcome-reconciliation
            # pass below used to reconcile a composite-keyed adjustment
            # OUTCOME only against an EARLIER adjustment outcome
            # (adjustment_outcome_identity_by_order_id) or, failing that, a
            # ticker-based fallback that only fires once amounts_by_ticker
            # already holds an entry for the bare ticker -- an adjustment
            # candidate row with no ordinary base order alongside it never
            # populates that bare-ticker entry, so the fallback never fired
            # and the outcome's amount landed on a FRESH bare-ticker
            # identity instead of the candidate row's own composite one,
            # double-counting the correction (Codex review, PR #654:
            # "Reconcile adjustment candidates with outcomes by order ID").
            adjustment_candidate_identity_by_order_id: Dict[Any, Any] = {}
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
            # Same idea, tracked SEPARATELY for adjustment-only outcomes --
            # outcome_identity_by_order_id above deliberately excludes an
            # adjustment (its dollar delta must keep landing on the
            # identity it corrects, never be treated as a distinct
            # "order" other outcomes can fold onto), but that also meant
            # two adjustment-only outcomes representing the SAME
            # settlement correction under different mapping keys (e.g.
            # "A" and "A#recovery", both carrying the same order_id) each
            # minted their own identity and their own synthetic row,
            # doubling the correction's dollar contribution to day/week
            # notional (Codex review, PR #654: "Deduplicate
            # adjustment-only outcomes by order ID"). A separate map keeps
            # this dedup scoped to adjustment-vs-adjustment folding only,
            # never adjustment-vs-ordinary.
            adjustment_outcome_identity_by_order_id: Dict[Any, Any] = {}
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
            # Reverse of candidate_row_by_identity, keyed by id(row) rather
            # than the row itself (rows are unhashable dicts) -- lets the
            # general all-row dedup pass further below look up which
            # amounts_by_ticker identity a real-status keeper row already
            # resolved to, so it can refresh that identity's aggregate
            # amount when merging in a discarded duplicate's amount rather
            # than only mutating the row's own planned_amount_usd (Codex
            # review, PR #654: "Refresh executed totals after merging
            # stale duplicates").
            identity_by_candidate_row_id: Dict[int, Any] = {}
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
                if (
                    ticker in seen_tickers
                    and order_id
                    and order_id in candidate_identity_by_order_id
                    and not _is_settlement_adjustment(row)
                ):
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
                    # order ID"). Explicitly excludes an adjustment row
                    # sharing the base order's order_id -- that row is a
                    # bookkeeping correction to the base order, not a
                    # duplicate of it, and folding it onto the base identity
                    # let its (possibly negative) delta OVERWRITE the base
                    # amount in amounts_by_ticker instead of summing
                    # separately (a $100 order followed by a -$2 correction
                    # reporting day_notional_usd=-2 instead of $98) --
                    # mirroring the same adjustment exclusion
                    # candidate_identity_by_order_id's own registration
                    # already applies (Codex review, PR #654: "Keep
                    # settlement adjustments separate during order-ID
                    # folding"). It falls through to the composite
                    # "<ticker>:<order_id>" branch just below instead,
                    # which was already its correct pre-existing identity.
                    identity = candidate_identity_by_order_id[order_id]
                    # Codex review, PR #654: "Prefer filled candidate rows
                    # when folding duplicates". Folding here always kept
                    # whichever row happened to be FIRST in list order,
                    # discarding a later, more authoritative status (a
                    # provisional "submitted" row ahead of a corrected
                    # "filled" one for the same order_id) along with its
                    # confirmed amount. When this row's own status outranks
                    # the identity's current representative row, PROMOTE it
                    # instead: the previous representative becomes the
                    # duplicate to drop, and this row becomes
                    # candidate_row_by_identity[identity] so every
                    # downstream read in this same iteration (amt_value's
                    # amounts_by_ticker assignment just below,
                    # _backfill_planned_amount, claim_sources) already
                    # operates on the stronger row.
                    # >=, not >: Codex review, PR #654: "Prefer later
                    # candidate corrections when status ranks tie". Two
                    # rows can legitimately share the SAME highest rank (an
                    # earlier "filled" $5 confirmation superseded by a
                    # later, corrected "filled" $7 one) -- matching the
                    # same tie-break already applied to same-state OUTCOME
                    # folding.
                    _existing_identity_keeper = candidate_row_by_identity.get(identity)
                    if isinstance(_existing_identity_keeper, dict) and _OUTCOME_STATUS_RANK.get(
                        str(row.get("status") or ""), -1
                    ) >= _OUTCOME_STATUS_RANK.get(str(_existing_identity_keeper.get("status") or ""), -1):
                        _duplicate_candidate_row_ids.add(id(_existing_identity_keeper))
                        candidate_row_by_identity[identity] = row
                        identity_by_candidate_row_id[id(row)] = identity
                    else:
                        _duplicate_candidate_row_ids.add(id(row))
                elif ticker in seen_tickers and order_id:
                    identity = f"{ticker}:{order_id}"
                elif ticker in seen_tickers and _is_settlement_adjustment(row):
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
                if identity is None:
                    continue
                seen_tickers.add(ticker)
                if _is_settlement_adjustment(row):
                    adjustment_identities.add(identity)
                    adjustment_identity_by_ticker.setdefault(ticker, identity)
                    if order_id:
                        # See adjustment_candidate_identity_by_order_id's
                        # own declaration above for why this needs its own
                        # registration, separate from
                        # candidate_identity_by_order_id (Codex review, PR
                        # #654: "Reconcile adjustment candidates with
                        # outcomes by order ID").
                        adjustment_candidate_identity_by_order_id.setdefault(order_id, identity)
                if order_id and not _is_settlement_adjustment(row):
                    candidate_identity_by_order_id.setdefault(order_id, identity)
                    candidate_order_id_by_identity.setdefault(identity, order_id)
                if not order_id and not _is_settlement_adjustment(row):
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
                # Fresh evidence after the negative-planned-amount fix above:
                # submitted_amount_usd goes through this SAME finite-value
                # gate but had no equivalent sign check of its own -- a
                # finite negative submitted_amount_usd (e.g. -7) on an
                # ordinary row still won here unmodified, recording a
                # negative day/week executed notional and reducing rather
                # than growing the dashboard's totals for a real order
                # (Codex review, PR #654: "Reject negative submitted amounts
                # for ordinary orders"). Adjustment rows are exempt, same as
                # every other sign check in this normalization -- their
                # dollar delta is a real bookkeeping correction and can be
                # legitimately negative.
                _row_is_adjustment = _is_settlement_adjustment(row)
                amt_value = _finite_float(row.get("submitted_amount_usd"))
                if amt_value is not None and amt_value < 0 and not _row_is_adjustment:
                    amt_value = None
                if amt_value is None:
                    amt_value = _finite_float(row.get("planned_amount_usd"))
                    if amt_value is not None and amt_value < 0 and not _row_is_adjustment:
                        amt_value = None
                if amt_value is not None:
                    # A row folded onto an EARLIER duplicate's identity
                    # (same ticker, same order_id, marked in
                    # _duplicate_candidate_row_ids above and removed from
                    # buy_candidates after this loop) must NOT let its own
                    # amount unconditionally overwrite the aggregate here
                    # -- the retained KEEPER row's own planned_amount_usd
                    # is left untouched by _backfill_planned_amount() when
                    # it already has a valid amount, so two rows with
                    # different valid amounts ($5 kept, $7 discarded)
                    # rendered a $5 candidate but a $7 day/week notional,
                    # an inconsistency the later all-row dedup pass never
                    # gets a chance to repair since this row is already
                    # gone by then (Codex review, PR #654: "Keep
                    # deduplicated row amounts consistent"). Only refresh
                    # when the aggregate doesn't already hold a real
                    # amount, matching every other backfill's "don't
                    # clobber a valid existing value" contract.
                    # Codex review, PR #654: "Protect stronger candidate
                    # amounts from stale outcomes". The same-state OUTCOME
                    # folding pass further below only overwrites
                    # amounts_by_ticker[identity] when its own status rank
                    # is >= _amounts_by_ticker_source_rank.get(identity,
                    # -1) -- but this candidate pass, which runs FIRST and
                    # can itself establish the identity's amount, never
                    # recorded a rank here. A "filled" $7 candidate's own
                    # rank was invisible to that later check, so a stale
                    # "submitted" $5 outcome (rank 2) still passed the
                    # default -1 comparison and overwrote it. Recorded
                    # here so the outcome pass has the candidate's own
                    # rank to compare against.
                    if id(row) in _duplicate_candidate_row_ids:
                        if not _finite_float(amounts_by_ticker.get(identity)):
                            amounts_by_ticker[identity] = amt_value
                            _amounts_by_ticker_source_rank[identity] = _OUTCOME_STATUS_RANK.get(
                                str(row.get("status") or ""), -1
                            )
                    else:
                        amounts_by_ticker[identity] = amt_value
                        _amounts_by_ticker_source_rank[identity] = _OUTCOME_STATUS_RANK.get(
                            str(row.get("status") or ""), -1
                        )
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
                # Backfilled onto the row THIS identity already resolved to
                # (falling back to `row` itself the first time an identity
                # is seen), not unconditionally onto `row` -- a duplicate
                # order-ID row folded onto an EARLIER identity above is
                # itself removed from buy_candidates a few lines down, so
                # backfilling only `row` here silently discarded a usable
                # amount the retained row still lacked (e.g. the kept row
                # has no amount of its own but its just-discarded duplicate
                # carried $7) instead of ever reaching the row that
                # actually survives (Codex review, PR #654: "Backfill the
                # retained row before dropping its duplicate").
                _backfill_planned_amount(candidate_row_by_identity.get(identity, row), amt_value)
                # ...and remember which row this identity came from, so that
                # same later pass can find it. First writer wins for a
                # repeated identity, matching every other identity map here.
                candidate_row_by_identity.setdefault(identity, row)
                identity_by_candidate_row_id.setdefault(id(row), identity)
                if not _is_settlement_adjustment(row):
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
                        if isinstance(r, dict) and not _is_settlement_adjustment(r)
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
            # Second, order-ID dedup pass across EVERY retained row -- the
            # loop above only deduplicates within `_candidate_rows` (the
            # real-order-status subset), so two rows sharing an order_id
            # where NEITHER (or only one) carries a real status never
            # entered that loop and were never folded or removed. A
            # matching broker outcome below only ever backfills the FIRST
            # row it finds for an order_id (via candidate_row_by_order_id),
            # so any other duplicate for that same order_id survived
            # untouched in buy_candidates -- inflating counts.buy_candidates
            # and doubling planned/sector notional for one physical order
            # (Codex review, PR #654: "Deduplicate stale candidates before
            # outcome reconciliation"). Grouped by order_id (not a simple
            # first-row-wins scan) so a real-status row is always preferred
            # as the keeper over a stale one, regardless of list order.
            _rows_by_order_id: Dict[Any, List[Dict[str, Any]]] = {}
            # An adjustment row sharing its order_id with the base order
            # it corrects is never a duplicate of THAT order -- it's a
            # bookkeeping correction, real deployed capital in its own
            # right, not a second representation of the same candidate.
            # Grouping it into the SAME dict as ordinary rows treated it
            # as a fold candidate against the base order and REMOVED it
            # entirely, losing the correction from planned/graded/sector
            # totals (Codex review, PR #654: "Keep adjustment rows out of
            # the all-row deduplication"). But routing it out of
            # deduplication ENTIRELY also meant two adjustment-only rows
            # representing the SAME settlement correction under the same
            # order_id (no ordinary row involved at all) never folded
            # together either -- each survived as its own candidate,
            # doubling the correction's contribution to day/week notional
            # (Codex review, PR #654: "Deduplicate adjustment candidate
            # rows separately"). Grouped into a SEPARATE dict, keyed
            # independently even for the same order_id value, so an
            # adjustment can fold onto another adjustment but never onto
            # (or be folded onto by) an ordinary row.
            _adjustment_rows_by_order_id: Dict[Any, List[Dict[str, Any]]] = {}
            for row in existing_candidate_rows:
                _row_order_id = row.get("order_id")
                if not _row_order_id:
                    continue
                if _is_settlement_adjustment(row):
                    _adjustment_rows_by_order_id.setdefault(_row_order_id, []).append(row)
                else:
                    _rows_by_order_id.setdefault(_row_order_id, []).append(row)
            _stale_duplicate_row_ids: set = set()
            for _dup_rows in list(_rows_by_order_id.values()) + list(_adjustment_rows_by_order_id.values()):
                if len(_dup_rows) < 2:
                    continue
                # Codex review, PR #654: "Prefer filled candidate rows when
                # folding duplicates". The first row with ANY real status
                # isn't necessarily the most authoritative one -- a
                # provisional "submitted" row can precede a later, corrected
                # "filled" row for the same order_id, and picking the first
                # discarded the stronger representation along with its
                # confirmed amount. max() with a stable tie-break (Python's
                # max returns the first of equal-ranked elements) picks the
                # highest-ranked status, falling back to the first row when
                # none has a real status at all (every rank is -1).
                _keeper = max(_dup_rows, key=lambda r: _OUTCOME_STATUS_RANK.get(r.get("status"), -1))
                for _dup_row in _dup_rows:
                    if _dup_row is _keeper:
                        continue
                    # Merge the discarded duplicate's useful fields onto the
                    # keeper before it's dropped -- the SAME three gaps the
                    # outcome-reconciliation backfill below has for a
                    # single stale row also apply here between two
                    # candidate rows: amount, placement status, and claim-
                    # week attribution (Codex review, PR #654: "Use stale
                    # candidate notional when its outcome omits it" /
                    # "Register claim metadata from matched stale
                    # candidates" describe the outcome-side twin of this).
                    _dup_amount = _finite_float(_dup_row.get("submitted_amount_usd"))
                    if _dup_amount is not None and _dup_amount < 0 and not _is_settlement_adjustment(_dup_row):
                        _dup_amount = None
                    if _dup_amount is None:
                        _dup_amount = _finite_float(_dup_row.get("planned_amount_usd"))
                        if _dup_amount is not None and _dup_amount < 0 and not _is_settlement_adjustment(_dup_row):
                            _dup_amount = None
                    _backfill_planned_amount(_keeper, _dup_amount)
                    if _dup_amount is not None and _finite_float(_keeper.get("submitted_amount_usd")) is None:
                        _keeper["submitted_amount_usd"] = _dup_amount
                    # amounts_by_ticker[identity] -- the aggregate
                    # budget.day_notional_usd is later summed from -- was
                    # already set (possibly to 0.0) by the earlier
                    # real-order-status pass, before this general dedup
                    # pass ever ran; mutating the keeper ROW's own
                    # planned_amount_usd above doesn't reach that aggregate
                    # on its own. Without this, a submitted keeper with no
                    # amount of its own plus a stale duplicate supplying $7
                    # rendered $7 planned but $0 executed/day_notional_usd
                    # unless a LATER broker outcome happened to revisit the
                    # same identity (Codex review, PR #654: "Refresh
                    # executed totals after merging stale duplicates").
                    # Only refreshed when the aggregate doesn't already
                    # hold a real amount of its own, matching
                    # _backfill_planned_amount()'s own "don't clobber a
                    # valid existing value" contract.
                    _keeper_identity = identity_by_candidate_row_id.get(id(_keeper))
                    if _dup_amount is not None:
                        if _keeper_identity is not None and not _finite_float(
                            amounts_by_ticker.get(_keeper_identity)
                        ):
                            amounts_by_ticker[_keeper_identity] = _dup_amount
                    if _dup_row.get("status") in _REAL_ORDER_STATUSES and _keeper.get(
                        "status"
                    ) not in _REAL_ORDER_STATUSES:
                        _keeper["status"] = _dup_row.get("status")
                    if _has_usable_claim_breakdown(_dup_row) and not _has_usable_claim_breakdown(_keeper):
                        _keeper["whole_share_claim_breakdown"] = _dup_row.get("whole_share_claim_breakdown")
                        # claim_sources[identity] was already populated (or
                        # not) by the earlier real-order-status pass, before
                        # this general dedup pass ever ran -- copying the
                        # breakdown onto the keeper ROW above doesn't reach
                        # that map on its own, so build_performance_
                        # snapshot()'s week-notional split never sees it
                        # unless a LATER broker outcome happens to revisit
                        # this same identity. Register it here too, the
                        # same way amounts_by_ticker is refreshed just
                        # above (Codex review, PR #654: "Register copied
                        # claim metadata before weekly aggregation").
                        if _keeper_identity is not None:
                            claim_sources[_keeper_identity] = _keeper
                    # A discarded duplicate that went through the earlier
                    # real-order-status pass (different ticker, same
                    # order_id -- that pass's own fold only merges same-
                    # ticker repeats, so a different ticker always got its
                    # OWN separate identity there) still has its own
                    # amounts_by_ticker/counted_identities entries from
                    # that pass. Discarding the ROW here without also
                    # removing those left the aggregate totals reflecting
                    # BOTH orders' dollars/counts for a single surviving
                    # candidate row (Codex review, PR #654: "Remove
                    # discarded duplicate identities from execution
                    # totals").
                    _dup_identity = identity_by_candidate_row_id.get(id(_dup_row))
                    if _dup_identity is not None and _dup_identity != _keeper_identity:
                        amounts_by_ticker.pop(_dup_identity, None)
                        counted_identities.discard(_dup_identity)
                        candidate_row_by_identity.pop(_dup_identity, None)
                    _stale_duplicate_row_ids.add(id(_dup_row))
            if _stale_duplicate_row_ids:
                existing_candidate_rows = [
                    r for r in existing_candidate_rows if id(r) not in _stale_duplicate_row_ids
                ]
                _all_dict_candidate_rows = [
                    r for r in _all_dict_candidate_rows if id(r) not in _stale_duplicate_row_ids
                ]
                _raw_buy_candidates_after_stale_dedup = data.get("buy_candidates")
                if isinstance(_raw_buy_candidates_after_stale_dedup, list):
                    data["buy_candidates"] = [
                        r for r in _raw_buy_candidates_after_stale_dedup if id(r) not in _stale_duplicate_row_ids
                    ]
                counts = data.get("counts")
                if isinstance(counts, dict):
                    counts["buy_candidates"] = sum(
                        1
                        for r in data["buy_candidates"]
                        if isinstance(r, dict) and not _is_settlement_adjustment(r)
                    )
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
                    if _outcome.get("order_id") or _is_settlement_adjustment(_outcome):
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
                    elif _pool and (
                        _identity != _ticker
                        or candidate_order_id_by_identity.get(_identity) is not None
                    ):
                        # Codex review, PR #654: "Match composite ID-less
                        # outcomes to sequenced candidates". The original
                        # deferral only covered a BARE-keyed id-less outcome
                        # whose identity is already occupied by an
                        # ID-BEARING candidate row -- but a same-ticker
                        # retry's SECOND id-less outcome is stored under a
                        # composite key (e.g. "AAA#retry") that can never
                        # literally match a sequenced pool entry like
                        # "AAA:candidate#1", so it fell through unmatched and
                        # the main pass minted it a brand-new identity of its
                        # own instead of consuming the remaining sequenced
                        # candidate. Any composite-keyed (``_identity !=
                        # _ticker``) id-less outcome now defers here whenever
                        # a pool for this ticker remains, regardless of
                        # whether an id-bearing row occupies the bare
                        # identity -- the bare case keeps its original,
                        # narrower guard so an unoccupied bare identity still
                        # resolves directly in the main pass as before.
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
                        and not _is_settlement_adjustment(outcome)
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
                        and not _is_settlement_adjustment(outcome)
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
                        and not _is_settlement_adjustment(outcome)
                        and (
                            (claimed_order_id and claimed_order_id != outcome_order_id)
                            # An adjustment row is deliberately EXCLUDED
                            # from candidate_order_id_by_identity's own
                            # registration (its dollar delta must keep
                            # landing on the identity it corrects, not be
                            # treated as a distinct order to compare
                            # order_ids against) -- so when an adjustment
                            # already owns the bare ticker identity,
                            # `claimed_order_id` above is always None for
                            # it, and the ids-differ check just above can
                            # never fire to split an ORDINARY outcome away
                            # from that identity. `amounts_by_ticker[identity]
                            # = amt` below then overwrote the adjustment's
                            # aggregate with the ordinary order's amount
                            # entirely -- a -$2 adjustment plus a $5
                            # ordinary order reported $5 executed instead
                            # of $3 (Codex review, PR #654: "Separate
                            # ordinary outcomes from adjustment-owned
                            # identities"). Checked directly via
                            # adjustment_identities rather than order_id
                            # comparison, since an adjustment-owned
                            # identity has no order_id registered to
                            # compare against in the first place.
                            or identity in adjustment_identities
                        )
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
                    # Declared here (not at its original site just before
                    # the ticker-based fallback below) so the adjustment-
                    # candidate order-ID fold above -- which runs earlier in
                    # this identity chain and can set it True -- has
                    # somewhere to write without a later reassignment
                    # clobbering it back to False (Codex review, PR #654:
                    # "Reconcile adjustment candidates with outcomes by
                    # order ID").
                    folded_candidate_adjustment = False
                    if (
                        outcome_order_id
                        and outcome_order_id not in candidate_identity_by_order_id
                        and not _is_settlement_adjustment(outcome)
                        and outcome_order_id in outcome_identity_by_order_id
                    ):
                        identity = outcome_identity_by_order_id[outcome_order_id]
                        folded_duplicate_outcome_order_id = True
                    # Reconcile an adjustment outcome against an adjustment
                    # CANDIDATE row sharing its order_id, before falling
                    # back to an EARLIER adjustment outcome's identity (the
                    # block just below) or the ticker-based fallback
                    # further down. The ticker-based fallback only fires
                    # once amounts_by_ticker already holds a bare-ticker
                    # entry (from an ordinary base order sharing the
                    # ticker), so an adjustment candidate row with no such
                    # base order alongside it never triggered it -- the
                    # outcome's amount then landed on a FRESH bare-ticker
                    # identity instead of the candidate row's own composite
                    # one, double-counting the correction (Codex review, PR
                    # #654: "Reconcile adjustment candidates with outcomes
                    # by order ID"). ``folded_candidate_adjustment``, not
                    # ``folded_duplicate_outcome_order_id``: this reconciles
                    # onto a REAL candidate row already in buy_candidates
                    # (mirroring the ticker-based fallback's own flag,
                    # which the synthesis-suppression check below already
                    # recognizes), not onto another outcome's synthetic row.
                    if (
                        outcome_order_id
                        and _is_settlement_adjustment(outcome)
                        and outcome_order_id in adjustment_candidate_identity_by_order_id
                    ):
                        identity = adjustment_candidate_identity_by_order_id[outcome_order_id]
                        folded_candidate_adjustment = True
                    # The adjustment twin of the fold immediately above --
                    # same shape (two mapping keys, no candidate row, same
                    # order_id) but for an adjustment-only outcome, using
                    # the SEPARATE adjustment_outcome_identity_by_order_id
                    # map so this can never fold an adjustment onto an
                    # ordinary order's identity or vice versa (Codex
                    # review, PR #654: "Deduplicate adjustment-only
                    # outcomes by order ID"). Only reached when the
                    # candidate-row fold just above didn't already resolve
                    # this order_id to a real row's identity.
                    if (
                        outcome_order_id
                        and _is_settlement_adjustment(outcome)
                        and outcome_order_id not in adjustment_candidate_identity_by_order_id
                        and outcome_order_id in adjustment_outcome_identity_by_order_id
                    ):
                        identity = adjustment_outcome_identity_by_order_id[outcome_order_id]
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
                    #     delta a second time. ``folded_candidate_adjustment``
                    #     is declared earlier now (see the note above) --
                    #     not reset to False here, so the order-ID fold
                    #     above still suppresses synthesis correctly even
                    #     though this ticker-based fallback's own condition
                    #     (``identity == ticker``) no longer matches once
                    #     that earlier fold has already moved identity away
                    #     from the bare ticker.
                    if (
                        _is_settlement_adjustment(outcome)
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
                    if outcome_order_id and not _is_settlement_adjustment(outcome):
                        outcome_identity_by_order_id.setdefault(outcome_order_id, identity)
                    elif outcome_order_id and _is_settlement_adjustment(outcome):
                        adjustment_outcome_identity_by_order_id.setdefault(outcome_order_id, identity)
                    # _finite_float(), not a bare float(): besides rejecting
                    # non-numeric/non-finite values (a standard-JSON-safe
                    # "NaN"/"Infinity" string, or an already-float nan/inf,
                    # would otherwise poison amounts_by_ticker, the running
                    # weekly total, and every rendered dashboard amount
                    # derived from it with $nan -- Codex review, PR #654:
                    # "Reject non-finite retained order amounts"), it also
                    # rejects a JSON boolean -- bool is an int subclass, so
                    # a bare float(True) succeeded as a real $1.00
                    # (Codex review, PR #654: "Reject booleans before
                    # converting retained amounts").
                    amt = _finite_float(outcome.get("submitted_amount_usd"))
                    if amt is not None and amt < 0 and not _is_settlement_adjustment(outcome):
                        # Fresh evidence after the candidate-side
                        # negative-amount fix is that this separate
                        # broker-outcome path -- reached when a
                        # retained state has NO matching candidate row
                        # at all, only broker_outcomes evidence -- still
                        # accepted a finite negative submitted_amount_usd
                        # unmodified, synthesizing a negative candidate
                        # and reducing planned/executed/sector totals
                        # for an ordinary order (Codex review, PR #654:
                        # "Reject negative broker-outcome notionals").
                        # Settlement adjustments are exempt, same as
                        # every other sign check in this normalization.
                        amt = None
                    if amt is not None:
                        # Codex review, PR #654: "Preserve stronger outcome
                        # amounts during same-state folding". Multiple
                        # broker_outcomes entries can fold onto the SAME
                        # identity (see the folds above) -- an unconditional
                        # overwrite let a later, lower-authority entry (a
                        # stale "submitted" re-confirmation) replace an
                        # already-confirmed "filled" amount just because it
                        # was processed second. Only overwrite when this
                        # outcome's own status ranks at least as strong as
                        # whichever entry last set this identity's amount.
                        _this_outcome_rank = _OUTCOME_STATUS_RANK.get(outcome.get("status"), -1)
                        if _this_outcome_rank >= _amounts_by_ticker_source_rank.get(identity, -1):
                            amounts_by_ticker[identity] = amt
                            _amounts_by_ticker_source_rank[identity] = _this_outcome_rank
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
                        #
                        # That candidate-row pass, though, only derives an
                        # amount for a REAL-order-status row -- a statusless
                        # or still-non-placed candidate row matched to this
                        # outcome by order_id was never considered there, so
                        # its own valid planned_amount_usd was thrown away
                        # here too, reporting $0 instead of the real amount
                        # (Codex review, PR #654: "Use stale candidate
                        # notional when its outcome omits it"). Checked via
                        # the same candidate_row_by_order_id lookup the
                        # stale-row backfill below uses.
                        _stale_row_amount = None
                        _stale_match = candidate_row_by_order_id.get(outcome_order_id) if outcome_order_id else None
                        # Same-kind gate, mirroring _stale_row_same_kind
                        # further below: a settlement-adjustment candidate
                        # row sharing this order_id with an ORDINARY outcome
                        # (or vice versa) is a DIFFERENT physical
                        # representation of this order, not a stale copy of
                        # it. Using its amount as this outcome's fallback
                        # unconditionally let a -$2 adjustment stand in for
                        # an amount-less ordinary outcome, reporting
                        # day_notional_usd = -$4 once the adjustment's own
                        # -$2 was ALSO counted via its own identity, instead
                        # of leaving this outcome's amount at $0/unresolved
                        # (Codex review, PR #654: "Gate stale amount
                        # fallback by order kind").
                        if _stale_match is not None and _is_settlement_adjustment(_stale_match) != _is_settlement_adjustment(
                            outcome
                        ):
                            _stale_match = None
                        if _stale_match is not None:
                            # _is_settlement_adjustment(), not a bare
                            # truthiness check on the raw marker -- a
                            # false-looking-but-truthy value like "false"
                            # is classified as an ORDINARY order everywhere
                            # else in this module now, but this fallback
                            # still let it exempt a negative stale amount
                            # from rejection, producing a negative
                            # executed/day notional for a row treated as
                            # ordinary everywhere else (Codex review, PR
                            # #654: "Apply the strict adjustment check to
                            # stale amounts").
                            _stale_row_amount = _finite_float(_stale_match.get("submitted_amount_usd"))
                            if (
                                _stale_row_amount is not None
                                and _stale_row_amount < 0
                                and not _is_settlement_adjustment(_stale_match)
                            ):
                                _stale_row_amount = None
                            if _stale_row_amount is None:
                                _stale_row_amount = _finite_float(_stale_match.get("planned_amount_usd"))
                                if (
                                    _stale_row_amount is not None
                                    and _stale_row_amount < 0
                                    and not _is_settlement_adjustment(_stale_match)
                                ):
                                    _stale_row_amount = None
                        if _stale_row_amount is not None:
                            amounts_by_ticker[identity] = _stale_row_amount
                        else:
                            amounts_by_ticker.setdefault(identity, 0.0)
                    if not _is_settlement_adjustment(outcome):
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
                    # Fresh evidence after the malformed-breakdown fix above:
                    # a usable-but-UNDATED outcome breakdown (e.g.
                    # `[{"amount_usd": 15}]`, no trading_day) still won this
                    # direct assignment over an ALREADY-REGISTERED DATED
                    # candidate breakdown (set by the real-status candidate
                    # pass earlier in this same function, via the identical
                    # `claim_sources[identity] = row` assignment) -- an
                    # undated entry is attributed to the artifact's own
                    # week by _current_week_claim_amount(), so a $20 Monday
                    # recovery whose candidate names Friday for $15 reported
                    # the full $20 in Monday's week instead of the real $5
                    # once the undated outcome replaced it (Codex review, PR
                    # #654: "Preserve dated claim metadata over undated
                    # replacements"). Only skip the overwrite when the
                    # existing source actually has a date and the outcome
                    # doesn't -- an outcome that is ALSO dated (or the
                    # existing source ISN'T) still wins as before.
                    _existing_claim_source = claim_sources.get(identity)
                    if _has_usable_claim_breakdown(outcome) and not (
                        _claim_breakdown_has_dated_entry(_existing_claim_source)
                        and not _claim_breakdown_has_dated_entry(outcome)
                    ):
                        claim_sources[identity] = outcome
                    elif outcome_order_id and _has_usable_claim_breakdown(
                        candidate_row_by_order_id.get(outcome_order_id, {})
                    ):
                        # The matched candidate carries the real claim-week
                        # breakdown but the outcome that proves its
                        # submission doesn't -- e.g. a statusless/stale
                        # candidate row recording where a whole-share
                        # recovery's original claim landed, reconciled here
                        # against a broker outcome that only confirms the
                        # order without repeating that metadata. Without
                        # this, a Monday recovery whose candidate names the
                        # prior Friday's claim got attributed entirely to
                        # Monday's ISO week instead of Friday's (Codex
                        # review, PR #654: "Register claim metadata from
                        # matched stale candidates").
                        claim_sources[identity] = candidate_row_by_order_id[outcome_order_id]
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
                        # Computed once and reused below for both the
                        # backfill gate and the synthesis-suppression gate,
                        # so the two can never disagree about whether this
                        # row actually represents this outcome.
                        _stale_row_same_kind = _stale_row is not None and _is_settlement_adjustment(
                            _stale_row
                        ) == _is_settlement_adjustment(outcome)
                        if _stale_row is not None and _stale_row_same_kind:
                            # Gated on _stale_row_same_kind: an adjustment
                            # candidate row sharing this order_id with an
                            # ORDINARY outcome (or vice versa) is a
                            # DIFFERENT physical representation of this
                            # order, not a stale copy of it -- copying the
                            # outcome's amount/status onto it here
                            # unconditionally let a $100 ordinary outcome
                            # overwrite a -$2 adjustment row with $100 too,
                            # so the row-based graded/sector totals (which
                            # sum EVERY row's amount) reported $200 for a
                            # $98 net order once the ordinary outcome's own
                            # synthetic row was ALSO materialized below
                            # (Codex review, PR #654: "Backfill only
                            # candidates of the same order kind").
                            _backfill_planned_amount(_stale_row, amt)
                            _outcome_status = outcome.get("status")
                            if (
                                _stale_row.get("status") not in _REAL_ORDER_STATUSES
                                and _outcome_status in _REAL_ORDER_STATUSES
                            ):
                                _stale_row["status"] = _outcome_status
                        # Only suppress synthesis when the matched row is
                        # the SAME kind of order as this outcome (ordinary
                        # vs adjustment) -- an adjustment CANDIDATE sharing
                        # this order_id with an ORDINARY outcome (e.g. the
                        # base order's own placement never got a
                        # buy_candidates row of its own, only its later
                        # settlement correction did) is not a
                        # representation of the outcome's own placement.
                        # Unconditionally suppressing here left
                        # buy_candidates containing only the adjustment,
                        # so build_graded_vs_fixed()/build_sector_matrix()
                        # (which read buy_candidates directly, not
                        # amounts_by_ticker) reported the adjustment's
                        # negative delta and zero physical candidates
                        # instead of the real ordinary order (Codex
                        # review, PR #654: "Materialize ordinary outcomes
                        # masked by adjustment rows").
                        if _stale_row is None or _stale_row_same_kind:
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
                    if _is_settlement_adjustment(outcome):
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
                    if isinstance(r, dict) and not _is_settlement_adjustment(r)
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
                # Floor against the state's own reported week_notional_usd
                # MINUS this state's settlement_adjustment_total, not the
                # raw reported value -- a retained state whose executor
                # applies its own correction before writing the field
                # reports a cumulative that ALREADY includes it (e.g. $100
                # prior plus a +$2 correction reported as $102). Flooring
                # against that already-corrected $102 and then adding the
                # +$2 correction again below reapplied it a second time,
                # normalizing to $104 instead of the real $102 (Codex
                # review, PR #654: "Avoid reapplying positive settlement
                # adjustments"). Subtracting it here and re-adding it in the
                # unconditional step below applies the correction EXACTLY
                # once regardless of whether the floor step keeps the
                # reported value or replaces it with the raise-only
                # candidate. A state with no adjustment at all
                # (settlement_adjustment_total == 0) subtracts zero, so this
                # is a no-op there -- unchanged from before this fix.
                _pre_adjustment_week_notional_usd = _num(budget.get("week_notional_usd")) - settlement_adjustment_total
                if _pre_adjustment_week_notional_usd < candidate_week_notional_usd:
                    budget["week_notional_usd"] = candidate_week_notional_usd
                else:
                    budget["week_notional_usd"] = _pre_adjustment_week_notional_usd
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
    existing_val = row.get("planned_amount_usd")
    if existing_val is None:
        existing_planned = None
    else:
        try:
            existing_planned = float(existing_val)
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
    # bool is a subclass of int in Python, so float(True)/float(False)
    # succeed as 1.0/0.0 instead of raising -- the run_state schema
    # requires a JSON number for every amount field this helper is used
    # on, never a boolean, so a structurally rejected artifact carrying
    # e.g. "planned_amount_usd": true would otherwise be reported as a
    # real $1 deployment (Codex review, PR #654: "Reject booleans before
    # converting retained amounts"). Checked before float() so it applies
    # uniformly to every caller of this shared helper.
    if isinstance(value, bool):
        return None
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
    # dict.get()'s default argument only ever fires when the KEY is
    # absent, not when it's present with an explicit `null` -- a retained
    # artifact carrying "base_amount_usd": null (JSON has no way to
    # distinguish "field omitted" from "field explicitly nulled") made the
    # nested `.get()` chain above return that None directly, never
    # consulting base_buy_amount_usd or ladder.base_amount_usd even when
    # one of those held a perfectly usable value -- the blocked-state
    # precheck above recognized the ladder fallback and let the state
    # through, only for this to then raise and abort the whole weekly
    # report despite the usable base amount being right there (Codex
    # review, PR #654: "Fall through null primary base amounts"). Chained
    # `is None` checks (not `or`) treat an explicit null exactly like an
    # absent key, falling through to the next candidate either way --
    # but an `or` chain ALSO falls through on any other falsy-but-present
    # value (0, 0.0, "", False), silently substituting a fallback instead
    # of letting the validation below reject the genuinely invalid
    # primary the way this function's fail-fast contract requires (Codex
    # review, PR #654: "Reject falsey invalid primary base amounts"). Only
    # an explicit null is indistinguishable from "field omitted" in JSON;
    # every other falsy value is a real, present, and (for this field)
    # invalid value that must reach -- and fail -- the checks below.
    raw = budget.get("base_amount_usd")
    if raw is None:
        raw = budget.get("base_buy_amount_usd")
    if raw is None:
        raw = ladder.get("base_amount_usd")
    trading_day = rs.get("trading_day", "?")
    if raw is None:
        if isinstance(trading_day, str) and trading_day < _BASE_AMOUNT_CHANGE_DATE:
            return _PRE_CHANGE_BASE_AMOUNT_USD
        raise ValueError(
            f"run_state for trading_day={trading_day!r} is missing "
            "budget.base_amount_usd/base_buy_amount_usd; refusing to guess a "
            "base DCA amount for the fixed-baseline comparison."
        )
    # bool is a subclass of int in Python, so float(True)/float(False)
    # succeed as 1.0/0.0 instead of raising -- a retained artifact
    # carrying "budget.base_amount_usd": true would silently apply a $1
    # fixed-DCA baseline to every candidate in the run instead of failing
    # fast the way this function's contract requires for a genuinely
    # invalid value (Codex review, PR #654: "Reject boolean run-level
    # base amounts"). Checked before float() so it raises the same
    # non-numeric error a string like "bad" already does.
    if isinstance(raw, bool):
        raise ValueError(
            f"run_state for trading_day={trading_day!r} has a non-numeric "
            f"budget.base_amount_usd/base_buy_amount_usd: {raw!r}"
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
    # schemas/run_state.schema.json requires the sizing-ladder base amount
    # to be strictly positive -- a zero or negative value is finite and
    # survives the check above, but build_graded_vs_fixed()'s fixed-1x
    # baseline multiplies every candidate by this value, so a nonpositive
    # base rendered a $0.00/negative fixed baseline and an inflated
    # (or nonsensical) delta instead of being treated as the corrupted
    # artifact it is (Codex review, PR #654: "Reject nonpositive ladder
    # base amounts").
    if numeric_raw <= 0:
        raise ValueError(
            f"run_state for trading_day={trading_day!r} has a nonpositive "
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
                # Codex review, PR #654: "Prefer corrected submitted
                # amounts in executed totals". morning_dca.py's own writer
                # convention puts a real-status row's placed amount at
                # planned_amount_usd -- but the cross-file dedup pass's
                # keeper merge (_merge_authoritative_amount_into_keeper)
                # writes an authoritative LATER correction to
                # submitted_amount_usd instead, deliberately leaving the
                # row's original planned_amount_usd untouched. Reading
                # planned_amount_usd unconditionally missed that
                # correction entirely, reporting the pre-correction figure
                # here while every OTHER amount-reading path in this
                # module already prefers submitted_amount_usd.
                # Codex review, PR #654: "Reject non-finite corrected
                # submitted amounts". Falling back only on a literal None
                # let a MALFORMED correction through: float("NaN") and
                # float("Infinity") both succeed, so a row whose
                # submitted_amount_usd is "NaN" poisoned the whole executed
                # total as $nan AND marked its identity seen, blocking the
                # broker_outcomes fallback that carried the real figure --
                # even though its own planned_amount_usd was perfectly
                # usable. _finite_ordinary_amount() applies the same
                # validation (finite, non-bool, and nonnegative for an
                # ordinary order) every other amount-reading path in this
                # module already uses, so an unusable corrected value now
                # falls through to the planned amount exactly as an absent
                # one always did.
                _row_is_adjustment_for_notional = _is_settlement_adjustment(c)
                amount = _finite_ordinary_amount(c.get("submitted_amount_usd"), _row_is_adjustment_for_notional)
                if amount is None:
                    amount = _finite_ordinary_amount(c.get("planned_amount_usd"), _row_is_adjustment_for_notional)
                if amount is None:
                    continue
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
    # is malformed"). _finite_float(), not a bare float(): besides the
    # finite check, it also rejects a JSON boolean -- bool is an int
    # subclass, so float(True) succeeds as 1.0, letting a malformed
    # week_notional_usd: true silently replace a real cumulative $100 total
    # with $1 instead of being skipped like every other unusable value
    # (Codex review, PR #654: "Reject boolean weekly-notional values").
    return _finite_float(value)


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
    #
    # The `or` between the two generic values (not chained `if None`
    # checks) is deliberate and preserves this function's own documented
    # zero-falls-through-one-level behavior -- _finite_float(0.0) legitimately
    # returns 0.0, so replacing this `or` with an `is None` check would stop
    # falling through for a genuine (rather than malformed) $0 primary cap,
    # masking a valid legacy weekly_notional_cap_usd the same way the
    # malformed-value bug did (Codex review, PR #654: "Preserve zero-cap
    # fallback semantics").
    # schemas/budgets.json requires a weekly cap to be strictly positive
    # -- a malformed negative value (e.g. weekly_cap_usd=-100) is finite,
    # so _finite_float() alone accepts it, and being TRUTHY (unlike a
    # genuine 0.0) it stops the `or` chain immediately, masking a valid
    # weekly_notional_cap_usd/stream-specific fallback the same way a
    # non-finite value used to (Codex review, PR #654: "Reject negative
    # weekly caps before selecting them"). Rejected to None here for
    # every source, so it falls through the `or` chain exactly like a
    # non-finite value already does, while a genuine $0 still falls
    # through on its own (0.0 is falsy) as this function's own documented
    # zero-cap semantics require.
    def _usable_cap(raw: Any) -> Optional[float]:
        numeric = _finite_float(raw)
        if numeric is not None and numeric < 0:
            return None
        return numeric

    numeric_value = _usable_cap(budget.get("weekly_cap_usd")) or _usable_cap(budget.get("weekly_notional_cap_usd"))
    if numeric_value is None:
        stream_value = _first_present(budget, _STREAM_WEEKLY_CAP_KEYS)
        numeric_value = _usable_cap(stream_value)
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
            # dict.get()'s default only ever fires when the KEY is absent,
            # not when it's present with an explicit `null` -- the same
            # hazard _require_base_amount() itself was fixed for (Codex
            # review, PR #654: "Fall through null primary base amounts").
            # A post-change retained state with "base_amount_usd": null
            # and a valid base_buy_amount_usd fell into this `continue`
            # and was silently skipped from the graded-vs-fixed table
            # even though _require_base_amount() below now correctly
            # resolves that exact fallback (Codex review, PR #654: "Honor
            # the secondary base in the blocked-state precheck").
            _blocked_precheck_base = budget.get("base_amount_usd")
            if _blocked_precheck_base is None:
                _blocked_precheck_base = budget.get("base_buy_amount_usd")
            if (
                _blocked_precheck_base is None
                and ladder.get("base_amount_usd") is None
                and not (isinstance(trading_day, str) and trading_day < _BASE_AMOUNT_CHANGE_DATE)
            ):
                continue
        base = _require_base_amount(rs)
        # A retained blocked state's candidate rows are only ever validated
        # for real placement by their STATUS -- a statusless row there has
        # no construction guarantee of being placed the way an ordinary
        # completed run's rows do (see the loop comment below), so it needs
        # the SAME "require a real status" treatment as an explicitly
        # not_submitted row, not the ordinary-run exemption (Codex review,
        # PR #654: "Require placement evidence for statusless blocked
        # rows"). A row genuinely backed by broker evidence already has its
        # status corrected to a real one by load_json_files() (see
        # "Backfill matched stale candidate rows"), so this doesn't drop
        # real orders -- only rows that never got that evidence.
        _is_blocked_retained_state = rs.get("status") == "blocked"
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
            # construction), so a MISSING status only exempts a row from
            # this exclusion for an ordinary run -- a BLOCKED retained
            # state carries no such construction guarantee, so a statusless
            # row there is excluded exactly like an explicitly non-placed
            # one. A whole_share_settlement_adjustment row is always exempt
            # -- its dollar delta is real deployed capital regardless of
            # its own status field.
            if not _is_settlement_adjustment(c) and (
                c.get("status") not in _REAL_ORDER_STATUSES
                if _is_blocked_retained_state
                else (c.get("status") is not None and c.get("status") not in _REAL_ORDER_STATUSES)
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
            if _is_settlement_adjustment(c):
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
            if not _is_settlement_adjustment(candidate):
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
