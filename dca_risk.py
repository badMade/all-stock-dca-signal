"""Dynamic risk management for the graded DCA strategy.

Two responsibilities, both computed from the same version-controlled run_state
artifacts the rest of the automation already writes:

  1. **Rolling 30-day cash burn rate** — how much capital the strategy has
     actually deployed over the trailing 30 calendar days, plus a per-day burn
     average, so an operator can see the funding runway at a glance.

  2. **A 7-day deployment kill-switch** — when capital deployed in the trailing
     7 days exceeds a configured fraction (default 40%) of the monthly cash
     allocation, the graded sizing ladder is capped to 1x. The strategy keeps
     buying dips at the base amount but stops scaling up until the 7-day window
     rolls back under the threshold. The cap itself lives in
     :func:`near_close_dca.graded_buy_multiplier`; this module computes the
     trailing-deployment figure and injects it into the config via
     :func:`apply_risk_guard`.

Stream hygiene (matches the run-classification contract in
``config/run_classification.json``): deployment is accounted **per stream** and
never conflated. Only *scheduled-live afternoon* runs are the trading stream, so
the kill-switch counts those and excludes manual runs, monitor/analytics runs,
and any morning run. "Afternoon scheduled evidence" is only accepted when its
timestamp is at or after 2:30 PM ET; an earlier timestamp on a run claiming to be
scheduled afternoon evidence is rejected rather than counted.

Standard-library only, no secrets, no network. Reads local run_state artifacts
and emits plain dicts / JSON. Robinhood remains the only execution broker and
Alpaca stays read-only; nothing here places or reviews orders.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from near_close_dca import StrategyConfig, kill_switch_engaged

logger = logging.getLogger("near_close_dca.risk")

EASTERN = ZoneInfo("America/New_York")

# Afternoon scheduled evidence must not be accepted with a timestamp before this
# ET wall-clock time. Mirrors the "afternoon scheduled evidence must not accept
# timestamps before 2:30 PM ET" execution constraint.
AFTERNOON_EVIDENCE_FLOOR_ET = time(14, 30)

# Rolling windows (calendar days).
BURN_WINDOW_DAYS = 30
KILL_SWITCH_WINDOW_DAYS = 7
# The widest trailing window any figure in build_risk_report() sums over, and so
# the only safe horizon for deciding that an unusable artifact cannot matter.
# Scoping the evidence counts to KILL_SWITCH_WINDOW_DAYS alone left an 8-30 day
# old corrupt state reported clean while it silently understated
# rolling_cash_burn's 30-day total and daily burn rate (Codex review: "Track
# corrupt states across the 30-day burn window"). Derived rather than written as
# 30 so adding a longer figure cannot leave this behind.
EVIDENCE_WINDOW_DAYS = max(BURN_WINDOW_DAYS, KILL_SWITCH_WINDOW_DAYS)

# Candidate statuses that carry real broker-order evidence and therefore count
# as deployed capital. ``submitted`` is what a fresh placement stamps; the other
# three are what a *restored* order looks like once recovery re-reads the broker
# (a recovered order may already be ``filled``, still ``pending`` at the venue,
# or ``unconfirmed`` when the broker has not yet echoed a terminal state) --
# matching scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES minus
# "not_submitted_confirmation_required" (which never reached the broker, per
# that module's own docstring). Defined locally rather than imported: this
# module is deliberately standard-library-only downstream of near_close_dca,
# and the set is small and stable enough not to need a shared source of truth.
# A same-slot retry that restores a prior attempt's "filled"/"pending"/
# "unconfirmed" candidate (near_close_dca.run_daily_compact's merge)
# previously left it invisible to this module's deployment accounting,
# valuing real in-flight/completed spend at $0 and letting a later run reuse
# headroom the 7-day kill switch should have reserved (Codex review: "Count
# restored in-flight spend in deployment risk"). Every deployment accounting
# path -- the run total and the per-claim-date buckets that
# feed the rolling burn and the kill switch -- must consult this ONE set: an
# accounting path that still accepted only ``submitted`` would see $0 for exactly
# those restored orders and could authorize further purchases on top of capital
# already at the broker (Codex review: "Apply restored statuses in claim-date
# accounting").
DEPLOYED_CANDIDATE_STATUSES = frozenset({"submitted", "filled", "pending", "unconfirmed"})

# Recognized deployment streams. Only SCHEDULED (afternoon live) is the trading
# stream that funds the kill-switch; the others are tracked separately so they
# can never be conflated into scheduled-live deployment.
STREAM_SCHEDULED = "scheduled"
STREAM_MANUAL = "manual"
STREAM_OTHER = "other"
# Report-only sub-buckets of STREAM_SCHEDULED for build_risk_report's
# deployed_last_7d_usd_by_stream, NOT values classify_stream() ever returns.
# The seven-slot roster's 10:30/12:30 identities (run_dca.run_slot()) validate
# as STREAM_SCHEDULED (Codex review: "Mirror roster identities in the runtime
# validator"), but their timestamp is always before the 2:30 PM afternoon-
# evidence floor, so deployed_by_day(stream=STREAM_SCHEDULED) -- which also
# requires is_afternoon_scheduled_evidence -- never counts them, and they no
# longer classify as STREAM_OTHER either now that they are valid scheduled
# evidence. Without a bucket of their own that capital vanished entirely from
# every per-stream total instead of remaining visible outside the afternoon
# kill-switch stream (Codex review: "Keep non-afternoon runs out of the
# afternoon risk stream").
STREAM_MORNING = "morning"
STREAM_MIDDAY = "midday"


# --- run_state parsing -------------------------------------------------------

def find_run_state_files(root: str | Path) -> List[Path]:
    """Return every ``run_state*.json`` at or below ``root`` (sorted)."""
    root_path = Path(root)
    if root_path.is_file():
        return [root_path]
    if not root_path.exists():
        return []
    return sorted(root_path.rglob("run_state*.json"))


def _parse_iso_et(text: object) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z') to an ET datetime.

    A naive timestamp is assumed to already be ET; a tz-aware one is converted.
    Returns None if unparseable.
    """
    if isinstance(text, datetime):
        dt = text
    else:
        raw = str(text).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(EASTERN)
    return dt


def run_timestamp_et(state: Dict[str, Any]) -> Optional[datetime]:
    """Best-effort ET timestamp for a run_state (generated_at / triggered_at_utc)."""
    for key in ("generated_at", "triggered_at_utc"):
        dt = _parse_iso_et(state.get(key))
        if dt is not None:
            return dt
    rc = state.get("run_classification")
    if isinstance(rc, dict):
        return _parse_iso_et(rc.get("triggered_at_utc"))
    return None


def classify_stream(state: Dict[str, Any]) -> str:
    """Classify a run_state into a deployment stream.

    A manual origin/flag anywhere wins (defensive OR), matching the
    run-classification contract: a state marked scheduled but also carrying
    ``manual_run=true`` is treated as manual, never scheduled. A scheduled-looking
    block must also pass ``scripts.run_classification.validate_classification``
    (required fields, run_id pattern, lineage) before counting as scheduled;
    anything that is not an unambiguous, *valid* scheduled run is ``STREAM_OTHER``.
    """
    rc = state.get("run_classification") if isinstance(state, dict) else None
    if not isinstance(rc, dict):
        return STREAM_OTHER
    origin = rc.get("run_origin")
    if origin == "manual" or rc.get("manual_run") is True:
        return STREAM_MANUAL
    if origin == "scheduled" and rc.get("manual_run") is False:
        try:
            from scripts.run_classification import validate_classification
        except ImportError:
            from run_classification import validate_classification
        if validate_classification(rc).ok:
            return STREAM_SCHEDULED
    return STREAM_OTHER


def _classification_stream(state: Dict[str, Any]) -> Optional[str]:
    """The run_classification block's own ``stream`` field, or None if absent/missing."""
    rc = state.get("run_classification") if isinstance(state, dict) else None
    return rc.get("stream") if isinstance(rc, dict) else None


def is_afternoon_scheduled_evidence(state: Dict[str, Any]) -> bool:
    """True only if this is a scheduled run whose timestamp is at/after 2:30 PM ET.

    Rejects (returns False) a scheduled run whose timestamp lands before the
    afternoon floor, so a stray morning or pre-2:30 PM artifact can never be
    counted as afternoon scheduled-live deployment. Also rejects a run whose
    own classification claims the morning/midday stream: the seven-slot
    roster's 10:30/12:30 identities are valid STREAM_SCHEDULED evidence (see
    STREAM_MORNING/STREAM_MIDDAY above) but must never be double-counted here
    just because a future slot happened to land at/after the time floor.

    Excludes ONLY the morning/midday streams, not every non-"afternoon" value:
    docs/risk_management.md documents a 2:30 PM intraday slot -- not yet
    wired into validate_classification() -- that is INTENDED to jointly fund
    this same afternoon kill-switch alongside the 3:30 PM slot. Rejecting
    every stream other than exactly "afternoon" would keep excluding that
    2:30 PM slot's evidence even after its identity is eventually accepted,
    perpetuating the documented undercount instead of only guarding against
    the two streams this function actually knows are non-afternoon (Codex
    review: "Preserve the documented 14:30 risk-stream eligibility").
    """
    if classify_stream(state) != STREAM_SCHEDULED:
        return False
    if _classification_stream(state) in (STREAM_MORNING, STREAM_MIDDAY):
        return False
    # A blocked invocation may reconcile an order that belongs to an older,
    # valid afternoon whole-share claim.  The invocation time is not the order
    # evidence time, so do not reject that explicitly marked recovery merely
    # because the retry happened before the afternoon cutoff.
    if state.get("recovered_deployment_evidence") is True:
        return True
    dt = run_timestamp_et(state)
    if dt is None:
        return False
    return dt.timetz().replace(tzinfo=None) >= AFTERNOON_EVIDENCE_FLOOR_ET


def is_morning_scheduled_evidence(state: Dict[str, Any]) -> bool:
    """True only if this is a valid STREAM_SCHEDULED run claiming the morning stream."""
    return (
        classify_stream(state) == STREAM_SCHEDULED
        and _classification_stream(state) == STREAM_MORNING
    )


def is_midday_scheduled_evidence(state: Dict[str, Any]) -> bool:
    """True only if this is a valid STREAM_SCHEDULED run claiming the midday stream."""
    return (
        classify_stream(state) == STREAM_SCHEDULED
        and _classification_stream(state) == STREAM_MIDDAY
    )


def _is_deployed_status(raw: Any) -> bool:
    """True when ``raw`` is one of the committed-capital statuses.

    The isinstance guard is load-bearing, not decorative. A run_state is
    arbitrary JSON that ``_load_states()`` admits on the strength of being a
    dict, so a corrupt artifact can carry ``"status": []`` or ``{}``. Testing
    those against a set hashes them and raises ``TypeError`` -- and
    ``deployed_by_day()`` wraps no per-artifact boundary, so a single malformed
    candidate would abort the whole risk summary and the rolling kill-switch
    calculation that depends on it. The equality comparison this replaced was
    total: any non-string status was simply "not submitted" and ignored. Keep
    that property (Codex review: "Validate statuses before set membership").

    Reads :data:`DEPLOYED_CANDIDATE_STATUSES` rather than a private copy of the
    same membership, so the set stays stated once. Every read of that constant
    goes through this predicate for the same reason -- a bare ``in`` test against
    it is the shape that raises.
    """
    return isinstance(raw, str) and raw in DEPLOYED_CANDIDATE_STATUSES


def _non_negative_finite_usd(raw: Any) -> Optional[float]:
    """``raw`` as a non-negative, finite dollar amount, or None if unusable.

    Rejects a negative number, NaN, an infinity, and any non-numeric value.
    Callers skip the row (or breakdown entry) entirely on None -- an amount
    this function cannot read is an amount whose deployment is unknown, and
    unknown must never be allowed to REDUCE a known total.

    That reduction is the whole point. Deployment is a floor on capital that
    has genuinely left the account, and every consumer treats it that way:
    :func:`total_deployed` feeds the rolling kill switch, which fires when the
    trailing window gets too LARGE. A corrupt or sign-flipped
    ``planned_amount_usd`` of ``-100`` therefore did not merely fail to count
    itself -- it cancelled $100 of real deployment recorded by other rows and
    could hold the kill switch below its threshold while the account was over
    it. NaN is the same hole by another route: it parses cleanly, poisons the
    whole sum into NaN, and makes every downstream threshold comparison
    silently False (Codex review: "Reject negative notionals for deployed
    statuses").
    """
    try:
        amount = float(raw or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(amount) or amount < 0.0:
        return None
    return amount


def deployed_usd(state: Dict[str, Any]) -> float:
    """Capital actually deployed by a run_state: submitted buy candidates' notional.

    Sums ``planned_amount_usd`` over ``buy_candidates`` whose ``status`` is in
    :data:`DEPLOYED_CANDIDATE_STATUSES` (submitted/filled/pending/unconfirmed --
    every status representing real broker-order evidence). Falls back to
    ``budget.day_notional_usd`` when no per-candidate detail is present. Never
    counts skipped/blocked/not-submitted/confirmation-required candidates.
    """
    candidates = state.get("buy_candidates")
    if isinstance(candidates, list):
        submitted = [
            c for c in candidates
            if isinstance(c, dict)
            and (
                _is_deployed_status(c.get("status"))
                or c.get("ambiguous_placement_exception") is True
            )
        ]
        if submitted:
            total = 0.0
            for c in submitted:
                amount = _non_negative_finite_usd(c.get("planned_amount_usd"))
                if amount is not None:
                    total += amount
            return round(total, 2)
        # buy_candidates is a list (possibly empty) with nothing submitted ->
        # genuinely nothing deployed this run. This must NOT fall back to
        # budget.day_notional_usd: when the top-up guard seeds cross-stream
        # (morning/midday) prior allocations into the afternoon StrategyState
        # but the afternoon run itself has no BUY candidates, buy_candidates
        # is explicitly [] while budget.day_notional_usd can still reflect
        # that seeded cross-stream capital, which would otherwise get double
        # counted as afternoon-scheduled deployment.
        return 0.0
    budget = state.get("budget")
    if isinstance(budget, dict):
        amount = _non_negative_finite_usd(budget.get("day_notional_usd"))
        return round(amount, 2) if amount is not None else 0.0
    return 0.0


def _trading_day(state: Dict[str, Any]) -> Optional[date]:
    day = state.get("trading_day")
    if not isinstance(day, str):
        return None
    try:
        return date.fromisoformat(day[:10])
    except ValueError:
        return None


def deployed_usd_by_claim_date(state: Dict[str, Any]) -> Dict[date, float]:
    """Like :func:`deployed_usd`, but buckets each deployed candidate's own
    ``planned_amount_usd`` under its TRUE execution date(s) rather than
    collapsing everything into this one artifact's own ``trading_day``.

    "Deployed" means the same thing here as it does in :func:`deployed_usd` --
    a status in :data:`DEPLOYED_CANDIDATE_STATUSES`, restored broker evidence
    included -- because this is the function the rolling burn and the kill
    switch actually aggregate through (:func:`deployed_by_day`).

    A whole-share candidate row recovered by a cross-day reconciliation pass
    (see ``near_close_dca._book_reconciled_whole_share_order``) carries
    ``whole_share_claim_breakdown`` -- a list of ``{"trading_day",
    "amount_usd"}`` entries, one per reconciled claim the row aggregates
    (a single row can combine MULTIPLE claims from DIFFERENT trading days,
    or a reconciled claim plus today's own fresh submission -- see
    ``near_close_dca._merge_whole_share_submission_outcomes``). Attributing
    the row's WHOLE amount to this artifact's own date instead (the
    pre-existing behavior) made a claim recovered well outside the trailing
    kill-switch window look like fresh deployment on the very next run, and
    even an in-window claim's effective window stretched by however many
    days separated the claim date from the artifact's own date (Codex
    review: "Attribute reconciled deployment to the claim date", "Preserve
    the claim date for in-window recoveries"). Each breakdown entry is
    bucketed under its own ``trading_day``; whatever portion of
    ``planned_amount_usd`` the breakdown does NOT account for (e.g. today's
    own fresh contribution aggregated onto the same row) is attributed to
    this artifact's own ``trading_day``, exactly like an ordinary candidate
    with no breakdown at all.

    Falls back to a single ``{trading_day: deployed_usd(state)}`` bucket
    (matching :func:`deployed_usd`'s own fallback) when ``buy_candidates``
    is absent -- there is no per-candidate detail to split by date in that
    case.
    """
    artifact_day = _trading_day(state)
    candidates = state.get("buy_candidates")
    if not isinstance(candidates, list):
        if artifact_day is None:
            return {}
        return {artifact_day: deployed_usd(state)}
    per_date: Dict[date, float] = {}
    for c in candidates:
        if not isinstance(c, dict) or not (
            _is_deployed_status(c.get("status"))
            or c.get("ambiguous_placement_exception") is True
        ):
            continue
        amount = _non_negative_finite_usd(c.get("planned_amount_usd"))
        if amount is None:
            continue
        breakdown = c.get("whole_share_claim_breakdown")
        if not isinstance(breakdown, list) or not breakdown:
            if artifact_day is None:
                # A dated sibling cannot make this otherwise-unfileable
                # deployed candidate safe evidence.  Returning a partial
                # result would understate the state's broker exposure.
                return {}
            per_date[artifact_day] = round(per_date.get(artifact_day, 0.0) + amount, 2)
            continue
        accounted_usd = 0.0
        for entry in breakdown:
            if not isinstance(entry, dict):
                continue
            raw_day = entry.get("trading_day")
            # Same floor as the row itself: a breakdown entry is a slice of
            # the row's own deployment, so a negative or unreadable one must
            # not net off its sibling entries' real amounts on their own days.
            entry_amount = _non_negative_finite_usd(entry.get("amount_usd"))
            if entry_amount is None:
                continue
            entry_day = artifact_day
            if isinstance(raw_day, str):
                try:
                    entry_day = date.fromisoformat(raw_day[:10])
                except ValueError:
                    entry_day = artifact_day
            # An entry with no bucket at all (no top-level trading_day to
            # fall back to AND its own trading_day unreadable) must not
            # consume any of the row's cap below -- it contributes nothing to
            # per_date either way, but reserving part of `amount` against it
            # anyway silently shrank a LATER, validly-bucketed entry's own
            # share: a $10 row with an unbucketable $8 entry followed by a
            # bucketed $8 entry reported only $2 for that valid date, with
            # the other $8 gone from both per_date and the final remainder
            # (which itself only lands when artifact_day is not None -- the
            # same condition that made this entry unbucketable in the first
            # place). Skipping before any cap accounting happens is what
            # keeps a later valid entry's full share intact (Codex review:
            # "Do not spend the row cap on unbucketable entries").
            if entry_day is None:
                continue
            # Capped at what is LEFT of the row, not added in full. The
            # breakdown partitions this row's own planned_amount_usd, so its
            # entries can never sum past it -- and one that does is corrupt.
            # Clamping only the leftover remainder below was too late: the
            # over-sized entries had already been bucketed, so a $10 row with
            # two $8 entries made deployed_usd() report $10 while
            # deployed_usd_by_claim_date() reported $16. deployed_by_day() sums
            # the latter, so the disagreement inflates the rolling window and
            # can fire the kill switch early (Codex review: "Cap breakdown
            # totals at the candidate notional"). Truncating here keeps the two
            # aggregators equal by construction, and drops the excess from the
            # LATER entries rather than letting it displace an earlier claim
            # date's real amount.
            entry_amount = min(entry_amount, max(0.0, amount - accounted_usd))
            if entry_amount <= 0:
                continue
            accounted_usd += entry_amount
            per_date[entry_day] = round(per_date.get(entry_day, 0.0) + entry_amount, 2)
        # accounted_usd can no longer exceed `amount`, so this is a plain
        # leftover rather than a guard; max() stays as belt and braces against
        # float drift in the running subtraction.
        remainder = round(max(0.0, amount - accounted_usd), 2)
        if remainder and artifact_day is None:
            return {}
        if remainder and artifact_day is not None:
            per_date[artifact_day] = round(per_date.get(artifact_day, 0.0) + remainder, 2)
    return per_date


def has_attributable_deployment_date(state: Dict[str, Any]) -> bool:
    """True when this state's deployment can be attributed to a trading day.

    Every route in :func:`deployed_usd_by_claim_date` needs a date to bucket
    under: a candidate with no ``whole_share_claim_breakdown`` is filed under the
    artifact's own ``trading_day``, and a breakdown entry whose own
    ``trading_day`` is missing or unparseable falls back to that same date. When
    the top-level ``trading_day`` is absent or malformed, both routes hit
    ``artifact_day is None`` and the candidate is dropped -- so the state is read
    and classified successfully while contributing exactly nothing to the figure
    it appears to be evidence for.

    A state can still be attributable without one, though: if every submitted
    candidate carries a breakdown with its own parseable claim date, the amounts
    land on those dates and never consult ``artifact_day`` at all. So this asks
    the question the way the summing code answers it -- "did anything come out
    with a date on it?" -- rather than checking ``trading_day`` alone, which
    would have excluded a fully-attributable reconciliation artifact
    (Codex review: "Reject afternoon states without a usable trading day").
    """
    if _trading_day(state) is not None:
        return True
    return bool(deployed_usd_by_claim_date(state))


def _amount_is_unusable(raw: Any) -> bool:
    """True when ``raw`` cannot be trusted as a real deployed-dollar figure.

    ABSENT (``raw is None``) and PRESENT-BUT-MALFORMED (negative,
    non-finite, nonnumeric) are both unusable. So is a non-numeric SHAPE
    -- a JSON string or boolean -- that ``_non_negative_finite_usd``'s
    ``raw or 0.0`` idiom would otherwise silently normalize to a "valid"
    0.0 right alongside a genuine numeric zero, hiding the same "known
    figure discarded" gap this module exists to surface (Codex review:
    "Reject zero notionals as unusable evidence", "Reject null/unusable
    fallback budget notionals"). A genuine JSON *number* 0 is NOT
    unusable on its own: it is real, unambiguous evidence of an
    intentional zero for a budget/breakdown figure (e.g. a day nothing
    was spent), never a type-shape corruption no legitimate writer
    produces. The one context where a genuine numeric zero is NOT enough
    on its own -- an order-bearing candidate's own planned_amount_usd,
    where live orders otherwise carry a $1 minimum -- is layered on top
    by has_unusable_deployed_notional()'s caller, not here (Codex
    review: "Restrict the numeric-zero exemption to recovery rows").
    """
    if raw is None:
        return True
    # bool is an int subclass, so _non_negative_finite_usd(True) returns a
    # "valid" 1.0 -- only the FALSY boolean (False -> 0.0) was caught by
    # the amount<=0 check below; True slipped through as if it were a real
    # $1 figure. Neither truth value is a type-shape a legitimate writer
    # produces for a dollar field, regardless of what it happens to
    # convert to (Codex review: "Reject truthy booleans in deployed
    # notionals").
    if isinstance(raw, bool):
        return True
    amount = _non_negative_finite_usd(raw)
    if amount is None:
        return True
    raw_is_genuine_number = isinstance(raw, (int, float)) and not isinstance(raw, bool)
    return amount <= 0.0 and not raw_is_genuine_number


def unusable_deployed_notional_dates(state: Dict[str, Any]) -> Set[Optional[date]]:
    """The trading day(s) unusable deployed-notional evidence in this state
    would attribute to, if any -- ``None`` standing in for "no attributable
    date available" (fail-closed: a caller scoping this to a specific window
    must treat ``None`` as potentially inside it, the same "unknown means
    possibly relevant" convention ``_load_states_with_evidence``'s
    ``_relevant()`` already uses).

    ``has_unusable_deployed_notional(state)`` is exactly ``bool(this)`` --
    kept in one place so a caller that needs to know WHICH day(s) carry
    unusable evidence (e.g. to check that day against a specific trailing
    window rather than trusting a state-wide flag, as
    ``near_close_dca._apply_deployment_history`` must: an unusable candidate
    whose ``whole_share_claim_breakdown`` attributes it entirely outside the
    kill-switch's trailing window cannot move ``deployed_last_7d_usd``, yet
    the old state-wide predicate poisoned that figure anyway just because
    SOME candidate elsewhere in the file was unusable -- Codex review:
    "Scope unusable notionals to the kill-switch window") does not re-derive
    this per-candidate/per-breakdown-entry logic separately and risk it
    drifting out of sync with the rest of this function's checks.
    """
    dates: Set[Optional[date]] = set()
    candidates = state.get("buy_candidates")
    if not isinstance(candidates, list):
        if "buy_candidates" in state:
            # PRESENT but malformed -- schema requires an array, so a JSON
            # object/scalar/null here is corrupt input, not a legacy
            # artifact that simply predates this field. Falling back to the
            # budget.day_notional_usd check (meant for a genuinely ABSENT
            # field) discarded whatever real per-candidate order detail this
            # malformed value replaced, silently trusting the fallback even
            # though it may contradict what actually deployed (Codex
            # review: "Reject malformed candidate collections as risk
            # evidence"). Fails closed unconditionally, without inspecting
            # the budget fallback.
            dates.add(_trading_day(state))
            return dates
        budget = state.get("budget")
        if isinstance(budget, dict) and "day_notional_usd" in budget:
            if _amount_is_unusable(budget.get("day_notional_usd")):
                dates.add(_trading_day(state))
        return dates
    artifact_day = _trading_day(state)
    for c in candidates:
        if not isinstance(c, dict) or not (
            _is_deployed_status(c.get("status"))
            or c.get("ambiguous_placement_exception") is True
        ):
            continue
        planned = _non_negative_finite_usd(c.get("planned_amount_usd"))
        row_notional_unusable = (
            c.get("deployed_notional_uncertain")
            or _amount_is_unusable(c.get("planned_amount_usd"))
            or (
                planned == 0.0
                and isinstance(c.get("planned_amount_usd"), (int, float))
                and not isinstance(c.get("planned_amount_usd"), bool)
                and c.get("reason") != "recovered_inverse_martingale_lease"
            )
        )
        breakdown = c.get("whole_share_claim_breakdown")
        if isinstance(breakdown, list) and breakdown:
            accounted_usd = 0.0
            row_unusable = False
            claim_dates: Set[Optional[date]] = set()
            has_unusable_claim_date = False
            for entry in breakdown:
                if not isinstance(entry, dict):
                    dates.add(artifact_day)
                    row_unusable = True
                    has_unusable_claim_date = True
                    continue
                raw_day = entry.get("trading_day")
                entry_day: Optional[date] = None
                if isinstance(raw_day, str):
                    try:
                        entry_day = date.fromisoformat(raw_day[:10])
                    except ValueError:
                        entry_day = None
                if entry_day is None:
                    has_unusable_claim_date = True
                if _amount_is_unusable(entry.get("amount_usd")):
                    dates.add(entry_day if entry_day is not None else artifact_day)
                    row_unusable = True
                    continue
                if entry_day is None and artifact_day is None:
                    dates.add(None)
                    row_unusable = True
                    continue
                amt = _non_negative_finite_usd(entry.get("amount_usd"))
                if amt is not None:
                    accounted_usd += amt
                    # A genuine numeric zero is real, VERIFIED evidence of no
                    # deployment on that date -- deployed_usd_by_claim_date()
                    # would book exactly $0 there too if it ever processed
                    # this entry, so propagating it as "possibly relevant
                    # uncertain evidence" below would flag a date whose
                    # evidence was never actually missing. Only a confirmed
                    # POSITIVE entry amount makes its date worth propagating
                    # if something elsewhere in the row turns out to be
                    # unusable (Codex review: "Exclude zero-dollar claims
                    # from sibling propagation").
                    if entry_day is not None and amt > 0:
                        claim_dates.add(entry_day)
            if row_notional_unusable:
                # A reconciliation row can be written after the replay it
                # repairs. Its own total is unusable, but its claim breakdown
                # tells us which historical day(s) that uncertainty belongs
                # to. Do not substitute the later artifact day and either
                # hide an in-window claim or poison a future-only one.
                # deployed_usd_by_claim_date() drops the WHOLE row the moment
                # the top-level total is unreadable -- regardless of whether
                # a breakdown entry is ALSO individually malformed -- so
                # every otherwise-valid claim date collected above must stay
                # uncertain evidence rather than vanishing with the rest of
                # the row (Codex review: "Retain valid claim dates when a
                # sibling entry is malformed").
                dates.update(claim_dates)
                if row_unusable or has_unusable_claim_date or not claim_dates:
                    # row_unusable here means at least one entry's own
                    # amount or shape could not be read at all, so
                    # accounted_usd is necessarily incomplete -- completeness
                    # can never be established, and the numeric remainder
                    # beyond the breakdown is filed under the artifact day by
                    # deployed_usd_by_claim_date() (on the rows where it runs
                    # at all), so it remains uncertain evidence for that day
                    # too (Codex review: "Preserve artifact-day uncertainty
                    # in malformed breakdowns").
                    dates.add(artifact_day)
                elif planned is None or accounted_usd < planned - 1e-9:
                    # planned is None means the top-level notional itself
                    # could not be parsed as a number at all (e.g. the
                    # string "NaN") -- there is then no total to compare
                    # accounted_usd against, so completeness can never be
                    # established and the artifact day must stay uncertain
                    # rather than being silently cleared (Codex review:
                    # "Preserve uncertainty for an unaccounted breakdown
                    # remainder").
                    dates.add(artifact_day)
                continue
            row_amount = _non_negative_finite_usd(c.get("planned_amount_usd"))
            if row_amount is not None and accounted_usd > row_amount + 1e-9:
                # The malformed excess belongs to the claim entries that
                # supplied it.  Using the artifact day here made a recent
                # reconciliation artifact poison today's evidence window even
                # when every overfilled claim was explicitly dated outside
                # it. Reachable even when row_unusable is also True (some
                # OTHER entry in the same breakdown is individually
                # malformed): accounted_usd only ever sums entries whose own
                # amount was independently readable, so an overfill detected
                # from those alone is real regardless of what else in the
                # breakdown is broken -- skipping this check on row_unusable
                # left the current-window entry's own inflated attribution
                # unflagged (Codex review: "Retain dates for overfilled
                # readable rows").
                claim_dates = set()
                has_unusable_claim_date = False
                for entry in breakdown:
                    if not isinstance(entry, dict):
                        has_unusable_claim_date = True
                        continue
                    raw_day = entry.get("trading_day")
                    if not isinstance(raw_day, str):
                        has_unusable_claim_date = True
                        continue
                    try:
                        overfill_entry_day = date.fromisoformat(raw_day[:10])
                    except ValueError:
                        has_unusable_claim_date = True
                        continue
                    # A dated entry's own DATE is readable here regardless of
                    # its amount -- has_unusable_claim_date must stay about
                    # date readability, not amount. But a confirmed,
                    # genuine numeric zero contributes nothing to either
                    # accounted_usd or deployed_usd_by_claim_date(), so this
                    # separate reconstruction loop (unlike the entries loop
                    # above, it rebuilds claim_dates from every breakdown
                    # entry to distrust individual per-entry attribution
                    # under an overfill) must likewise skip a zero-dollar
                    # date rather than treating it as uncertain evidence
                    # that was never actually missing (Codex review: "Filter
                    # zero claims in the newly reachable overfill path").
                    overfill_entry_amt = _non_negative_finite_usd(entry.get("amount_usd"))
                    if overfill_entry_amt is not None and overfill_entry_amt > 0:
                        claim_dates.add(overfill_entry_day)
                dates.update(claim_dates)
                # A malformed or missing claim date cannot safely be assigned
                # to one of the valid historical buckets.  Retain the
                # artifact date as uncertainty too, even when sibling entries
                # have usable dates, so a current evidence window fails closed.
                if has_unusable_claim_date or not claim_dates:
                    dates.add(artifact_day)
                continue
            if row_unusable:
                # Top-level total is usable and the breakdown is not
                # overfilled -- deployed_usd_by_claim_date() does not drop
                # this row just because ONE sibling entry is malformed, it
                # still books every valid entry under its own date and the
                # remainder under the artifact day, so nothing is actually
                # lost for those dates (Codex review: "Restrict sibling
                # claim propagation to unusable row totals"). Only the bad
                # entry's own date, already added directly above, needs to
                # stay uncertain.
                continue
            if (
                row_amount is not None
                and artifact_day is None
                and accounted_usd < row_amount - 1e-9
            ):
                dates.add(None)
                continue
        elif row_notional_unusable:
            dates.add(artifact_day)
    return dates


def has_unusable_deployed_notional(state: Dict[str, Any]) -> bool:
    """True when an order-bearing candidate's ``planned_amount_usd`` is present
    but unusable (negative, non-finite, or nonnumeric).

    :func:`deployed_usd` silently skips such a candidate -- ``_non_negative_
    finite_usd`` returns ``None`` and the loop's ``if amount is not None``
    guard just moves on -- so the state is read and classified successfully
    while a KNOWN order-bearing candidate contributes exactly nothing to the
    figure it appears to be evidence for. A tree holding only such states
    would report positive afternoon evidence (``afternoon_states_seen`` and
    ``has_attributable_deployment_date`` both pass -- the row has a real
    status and the state has a real trading_day) behind a total that
    silently omits it, disengaging the rolling burn/kill switch below its
    true threshold (Codex review: "Mark invalid deployed notionals as
    unusable evidence"). Mirrors ``has_attributable_deployment_date``'s role
    in ``_load_states_with_evidence``: a state can clear every other gate and
    still not be trustworthy evidence.

    Also covers a MISSING or JSON-``null`` ``planned_amount_usd``, not only a
    present-but-corrupt one. ``_non_negative_finite_usd(None)`` cannot be
    reused to detect that case directly: its ``raw or 0.0`` idiom treats
    ``None`` as falsy and silently returns ``0.0`` rather than ``None``, so
    ``deployed_usd()``'s own loop would add a legitimate-looking zero for a
    row that in fact carries NO amount at all -- the same "known
    order-bearing candidate contributes nothing" gap a corrupt present value
    creates (Codex review: "Treat missing deployment notionals as
    unusable"). ``planned_amount_usd`` is also required and non-nullable per
    ``run_state.schema.json``, so a missing/null value is schema-invalid on
    top of being financially unusable.

    Also True when a candidate carries ``deployed_notional_uncertain: true``
    (near_close_dca.py's duplicate-recovery path sets this when it could not
    recover the originating order's true ceiling and instead booked a
    fail-closed estimate at THIS run's own ``max_per_order_usd``). That
    figure is numerically well-formed -- it would otherwise pass every check
    above -- but is not proof of the real historical amount: the run that
    wrote it already forces its OWN in-memory weekly bucket to the worst-case
    tier ceiling, an in-memory correction that stops applying the moment that
    run ends. Without this, a LATER run reading the persisted artifact back
    (``dca_risk.deployed_usd()``, the rolling 7/30-day windows) would trust
    the understated estimate as settled fact once the current week rolls
    over (Codex review: "Poison durable deployment instead of only the
    current week").
    """
    return bool(unusable_deployed_notional_dates(state))


# --- Deployment accounting (stream-aware) ------------------------------------

def deployed_by_day(
    states: Iterable[Dict[str, Any]],
    *,
    stream: str = STREAM_SCHEDULED,
    require_afternoon_evidence: bool = True,
    evidence_predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Dict[date, float]:
    """Sum deployed capital per trading day for a single stream.

    Only run_states whose :func:`classify_stream` equals ``stream`` contribute,
    so manual and scheduled deployment are never mixed. When ``stream`` is the
    scheduled stream and ``require_afternoon_evidence`` is True, a scheduled run
    is only counted if it passes :func:`is_afternoon_scheduled_evidence` (at/after
    2:30 PM ET) — a pre-2:30 PM scheduled artifact is dropped, not counted.

    ``evidence_predicate``, when given, REPLACES that afternoon-evidence check
    (regardless of ``stream``/``require_afternoon_evidence``) with an arbitrary
    per-state predicate -- e.g. :func:`is_morning_scheduled_evidence` to bucket
    the seven-slot roster's own morning deployment separately from afternoon
    scheduled-live evidence, both of which classify as the same STREAM_SCHEDULED
    (Codex review: "Keep non-afternoon runs out of the afternoon risk stream").
    """
    per_day: Dict[date, float] = {}
    for state in states:
        if not isinstance(state, dict):
            continue
        if classify_stream(state) != stream:
            continue
        if evidence_predicate is not None:
            if not evidence_predicate(state):
                continue
        elif (
            stream == STREAM_SCHEDULED
            and require_afternoon_evidence
            and not is_afternoon_scheduled_evidence(state)
        ):
            continue
        for claim_day, amount in deployed_usd_by_claim_date(state).items():
            per_day[claim_day] = round(per_day.get(claim_day, 0.0) + amount, 2)
    return per_day


def total_deployed(deploy_by_day: Dict[date, float], as_of: date, window_days: int) -> float:
    """Sum deployment over the trailing ``window_days`` ending on ``as_of`` (inclusive)."""
    start_ordinal = as_of.toordinal() - (window_days - 1)
    total = sum(
        amount for day, amount in deploy_by_day.items()
        if start_ordinal <= day.toordinal() <= as_of.toordinal()
    )
    return round(total, 2)


def seven_day_deployed(deploy_by_day: Dict[date, float], as_of: date) -> float:
    """Capital deployed in the trailing 7 calendar days ending on ``as_of``."""
    return total_deployed(deploy_by_day, as_of, KILL_SWITCH_WINDOW_DAYS)


def rolling_cash_burn(
    deploy_by_day: Dict[date, float],
    as_of: date,
    window_days: int = BURN_WINDOW_DAYS,
) -> Dict[str, Any]:
    """Rolling cash-burn summary over the trailing ``window_days`` ending ``as_of``.

    ``daily_burn_rate_usd`` is the window total spread across every calendar day
    in the window (deployed / window_days), giving a steady runway figure that
    does not spike just because trades cluster on a few days.
    """
    total = total_deployed(deploy_by_day, as_of, window_days)
    active = {
        day: amt for day, amt in deploy_by_day.items()
        if as_of.toordinal() - (window_days - 1) <= day.toordinal() <= as_of.toordinal()
    }
    return {
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "total_deployed_usd": total,
        "daily_burn_rate_usd": round(total / window_days, 2) if window_days else 0.0,
        "active_deployment_days": len(active),
    }


# --- Kill-switch decision + config injection ---------------------------------

def kill_switch_active(
    deployed_7d_usd: float,
    monthly_cash_allocation_usd: float,
    fraction: float = 0.40,
) -> bool:
    """True when 7-day deployment exceeds ``fraction`` of the monthly allocation.

    A non-positive allocation disables the guard (returns False) so an
    unconfigured allocation can never wedge sizing to 1x.
    """
    if monthly_cash_allocation_usd <= 0:
        return False
    return deployed_7d_usd > fraction * monthly_cash_allocation_usd


def apply_risk_guard(
    config: StrategyConfig,
    deploy_by_day: Dict[date, float],
    as_of: date,
) -> StrategyConfig:
    """Return a copy of ``config`` with ``deployed_last_7d_usd`` set from history.

    The returned config drives sizing: once injected, the kill-switch cap in
    :func:`near_close_dca.graded_buy_multiplier` fires automatically whenever the
    trailing-7-day deployment crosses the configured fraction of the monthly
    allocation. Only the runtime deployment figure is changed; every other field
    (broker, dry_run, allocation, fraction, ladder) is preserved.
    """
    deployed_7d = seven_day_deployed(deploy_by_day, as_of)
    return dataclasses.replace(config, deployed_last_7d_usd=deployed_7d)


def build_risk_report(
    states: Sequence[Dict[str, Any]],
    as_of: date,
    config: Optional[StrategyConfig] = None,
) -> Dict[str, Any]:
    """Assemble a full risk report: per-stream burn + the scheduled-live kill-switch.

    Reports the 30-day burn and 7-day deployment for the scheduled-live stream
    (the trading stream), plus per-stream 7-day deployment so an operator can see
    that manual/other streams are tracked but excluded from the guard. The
    seven-slot roster's own morning/midday deployment (STREAM_MORNING/
    STREAM_MIDDAY) is reported separately from both: it validates as
    STREAM_SCHEDULED (so it is not "other") but is not afternoon evidence (so
    it does not fund the kill-switch) -- without its own bucket that capital
    would be counted in neither total and vanish from this report (Codex
    review: "Keep non-afternoon runs out of the afternoon risk stream").
    """
    config = config or StrategyConfig()
    scheduled = deployed_by_day(states, stream=STREAM_SCHEDULED)
    guarded = apply_risk_guard(config, scheduled, as_of)
    deployed_7d = guarded.deployed_last_7d_usd

    per_stream_7d = {
        STREAM_SCHEDULED: deployed_7d,
        STREAM_MANUAL: seven_day_deployed(
            deployed_by_day(states, stream=STREAM_MANUAL), as_of
        ),
        STREAM_MORNING: seven_day_deployed(
            deployed_by_day(
                states, stream=STREAM_SCHEDULED, evidence_predicate=is_morning_scheduled_evidence
            ),
            as_of,
        ),
        STREAM_MIDDAY: seven_day_deployed(
            deployed_by_day(
                states, stream=STREAM_SCHEDULED, evidence_predicate=is_midday_scheduled_evidence
            ),
            as_of,
        ),
        STREAM_OTHER: seven_day_deployed(
            deployed_by_day(states, stream=STREAM_OTHER, require_afternoon_evidence=False),
            as_of,
        ),
    }

    threshold = round(config.kill_switch_7d_deploy_fraction * config.monthly_cash_allocation_usd, 2)
    return {
        "as_of": as_of.isoformat(),
        "monthly_cash_allocation_usd": config.monthly_cash_allocation_usd,
        "kill_switch_7d_deploy_fraction": config.kill_switch_7d_deploy_fraction,
        "kill_switch_7d_threshold_usd": threshold,
        "scheduled_live": {
            "rolling_cash_burn": rolling_cash_burn(scheduled, as_of),
            "deployed_last_7d_usd": deployed_7d,
            "kill_switch_engaged": kill_switch_engaged(guarded),
        },
        "deployed_last_7d_usd_by_stream": per_stream_7d,
        "streams_note": (
            "Only the scheduled-live afternoon stream funds the kill-switch; "
            "manual, morning, midday, and other streams are reported for "
            "visibility but never counted toward scheduled-live deployment."
        ),
    }


# --- CLI ---------------------------------------------------------------------

def _run_dir_date(path: Path) -> Optional[date]:
    """The trading day encoded in a run directory name, or None.

    Every stream writes its artifacts under ``<stream>/YYYYMMDD_<suffix>/`` (see
    ``dca_paths.stream_root`` and each executor's ``run_state_dir``), so the day
    is recoverable from the PATH even when the file itself cannot be parsed --
    which is the only handle available for a state that failed to read.

    Returns None for any component that does not match, and callers treat that
    as "unknown", never as "old".
    """
    for part in reversed(path.parts):
        head = part.split("_", 1)[0]
        if len(head) == 8 and head.isdigit():
            try:
                return datetime.strptime(head, "%Y%m%d").date()
            except ValueError:
                return None
    return None


def _latest_attributed_date(state: Dict[str, Any]) -> Optional[date]:
    """The newest trading day this state can put capital on, or None.

    The union of every date ``deployed_usd_by_claim_date`` would bucket under
    and the state's own ``trading_day`` -- i.e. every date the state can reach.
    """
    if not isinstance(state, dict):
        return None
    days = list(deployed_usd_by_claim_date(state))
    own = _trading_day(state)
    if own is not None:
        days.append(own)
    return max(days) if days else None


def _load_states_with_evidence(
    root: str | Path, as_of: Optional[date] = None
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Load run states AND report what was actually read to produce them.

    Every figure in this report is a sum over the states this finds, so a zero
    is ambiguous on its own: it means "no capital deployed" and "no history was
    loaded" equally well. ``find_run_state_files`` returns an empty list for a
    root that does not exist, and the loop below deliberately skips an
    unreadable state rather than failing the whole report -- so a missing tree,
    or a corrupted recent afternoon state, yields exit 0 and
    ``deployed_last_7d_usd: 0``, indistinguishable from a genuinely quiet week.

    A consumer that treats that zero as confirmed spend leaves the kill switch
    disengaged and deep-drop candidates uncapped. The counts returned here let
    any caller tell the two apart and fail closed on the second
    (Codex review: "Reject missing risk-history evidence").

    Purely additive: ``_load_states`` still returns just the states, and the
    report gains an ``evidence`` block rather than changing any existing field.

    ``as_of`` scopes the unusable-state counts to the window the figures they
    guard actually sum -- ``EVIDENCE_WINDOW_DAYS``, the widest of them, not the
    kill switch's own 7 days: ``build_risk_report`` also publishes
    ``rolling_cash_burn`` over ``BURN_WINDOW_DAYS``. An artifact dated BEFORE
    the start of that trailing window cannot move any of them however corrupt
    it is (see ``_relevant`` for why the bound is one-sided), and counting it
    blocked a consumer on evidence that is irrelevant by construction. Worse, permanently: a legacy corrupt file never heals, so one
    of them wedged Gate 5 forever (Codex review: "Scope unusable-state counts to
    relevant risk history"). Omit ``as_of`` and the counts stay archive-wide,
    which is what ``_load_states``'s callers get.

    Scoped by DATE only, deliberately, and not also by stream. The window bound
    here is the same trailing-window arithmetic ``total_deployed`` applies, over
    the widest horizon any published figure uses, so the two cannot disagree. A stream guess would be a second,
    weaker definition of ``is_afternoon_scheduled_evidence`` -- which excludes on
    a state's own *classification*, not its path -- and it would guess in the
    EXCLUDING direction, quietly dropping evidence-loss signal from a
    fail-closed check. An unparseable date is likewise treated as in-window:
    unknown means possibly relevant.
    """
    files = find_run_state_files(root)
    states: List[Dict[str, Any]] = []
    # Parallel to `states`, appended in lockstep -- lets afternoon_invalid_
    # notional below apply the same _relevant() window predicate the
    # unreadable/unclassifiable buckets already apply, without changing
    # `states` itself (every other consumer of `states` wants the full,
    # unwindowed list).
    state_paths: List[Path] = []
    unreadable: List[str] = []
    unclassifiable: List[str] = []
    # Files parked as out-of-window, held as (path, origin bucket) until the
    # whole tree has been read -- see path_dates_contradicted below.
    deferred: List[Tuple[str, str]] = []
    # Set when some READABLE state in this tree attributes capital to a day
    # later than its own directory name claims. The path date is a naming
    # convention, not an enforced invariant (dca_paths.stream_root joins
    # whatever component its caller hands it), so a misfiled state can sit
    # under a stale directory while contributing to the current window -- and
    # the same file, once corrupt, has no readable dates left to check. The one
    # thing that can still be checked is whether this tree honours the
    # convention at all; when it demonstrably does not, the heuristic is
    # untrustworthy HERE and every deferred file is counted (Codex review:
    # "Validate path dates before discarding corrupt states").
    path_dates_contradicted = False

    def _relevant(path: Path) -> bool:
        """False only when the path's own date puts it BEFORE the figure's window.

        One-sided on purpose. An artifact dated after ``as_of`` is not
        irrelevant: ``deployed_usd_by_claim_date`` attributes a reconciled
        whole-share row to the ``trading_day`` recorded in its
        ``whole_share_claim_breakdown``, which is the day the ledger claim was
        made -- always at or before the artifact's own date, never after (see
        ``near_close_dca._book_reconciled_whole_share_order``, which stamps
        ``claim_trading_day_iso``). So a later run CAN carry in-window
        deployment, and a corrupted one would have hidden it while leaving
        ``run_states_unreadable == 0`` (Codex review: "Keep future
        reconciliation artifacts relevant to past windows").

        The same asymmetry is what makes the past-dated exclusion safe: every
        date an artifact can contribute is at or before its own, so nothing
        dated before the window start can reach into it however corrupt it is.
        """
        if as_of is None:
            return True
        day = _run_dir_date(path)
        if day is None:
            return True
        start = as_of.toordinal() - (EVIDENCE_WINDOW_DAYS - 1)
        return day.toordinal() >= start
    for path in files:
        try:
            data = json.loads(path.read_bytes())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("skipping unreadable run_state %s: %s", path, exc)
            if _relevant(path):
                unreadable.append(str(path))
            else:
                deferred.append((str(path), "unreadable"))
            continue
        # Checked for EVERY readable state, including the blocked stubs dropped
        # just below: a misfiled stub is the same evidence that this tree does
        # not name its directories after the days its states cover.
        dir_day = _run_dir_date(path)
        if dir_day is not None:
            latest = _latest_attributed_date(data)
            if latest is not None and latest > dir_day:
                path_dates_contradicted = True
        # Exclude date-gate blocked stubs, but KEEP a contract-rejected state
        # (artifact_contract_failure marker): its real submitted orders are deployed
        # capital that must still count toward the kill-switch/cap accounting, even
        # though it is invalid scheduled evidence. deployed_usd sums only
        # status=='submitted' candidates, so a partial-placement failure contributes
        # exactly its real fills.
        #
        # ALSO keep a blocked stub carrying recovered_deployment_evidence=True --
        # _persist_gated_whole_share_recovery() marks one when a blocked
        # invocation reconciles an order belonging to an older, valid afternoon
        # whole-share claim (see is_afternoon_scheduled_evidence's own identical
        # exception, which _load_deployment_history_states already honors).
        # Without this, this CLI loader silently drops that marked stub's
        # recovered notional, understating rolling burn and the reported
        # kill-switch state even though the underlying capital is real
        # (Codex review: "Load blocked recovery evidence in the risk CLI").
        if isinstance(data, dict) and (
            data.get("status") != "blocked"
            or data.get("artifact_contract_failure") is not None
            or data.get("recovered_deployment_evidence") is True
        ):
            states.append(data)
            state_paths.append(path)
            # Parsing is not the same as carrying evidence. A syntactically
            # valid but empty or truncated state ({}, or one whose
            # run_classification block is missing/not a mapping) appends
            # cleanly and counts as loaded, while classify_stream can only
            # return STREAM_OTHER for it -- so it contributes nothing to the
            # scheduled-live figure. A consumer told "1 state loaded, 0
            # unreadable" would take a zero from that as proven
            # (Codex review: "Validate loaded run states before counting
            # evidence"). Counted separately rather than dropped: it IS a file
            # that was read, and hiding it would make the found/loaded counts
            # disagree for no visible reason.
            if not isinstance(data.get("run_classification"), dict):
                if _relevant(path):
                    unclassifiable.append(str(path))
                else:
                    deferred.append((str(path), "unclassifiable"))
    if path_dates_contradicted:
        # Fail closed: this tree's directory names do not track its states'
        # dates, so nothing was proven irrelevant. Every deferred file goes
        # back to the bucket it came from.
        for deferred_path, bucket in deferred:
            (unreadable if bucket == "unreadable" else unclassifiable).append(deferred_path)
        outside_window: List[str] = []
    else:
        outside_window = [deferred_path for deferred_path, _bucket in deferred]
    root_path = Path(root)
    # How many states actually reached the evidence gate that funds the kill
    # switch. This is the direct answer to "was the relevant history loaded?",
    # which no count of well-formed states can give.
    #
    # Keyed on is_afternoon_scheduled_evidence, NOT on classify_stream ==
    # STREAM_SCHEDULED: the seven-slot roster's own 10:30/12:30 artifacts are
    # valid STREAM_SCHEDULED evidence (build_risk_report buckets them under
    # STREAM_MORNING/STREAM_MIDDAY) but is_afternoon_scheduled_evidence excludes
    # them by design, so deployed_by_day counts none of them toward the
    # scheduled-live figure. Counting the broader stream made a morning-only or
    # midday-only tree report positive evidence for a figure it contributes
    # nothing to (Codex review: "Count only afternoon states as kill-switch
    # evidence"). The predicate here is deliberately the SAME function
    # deployed_by_day applies, so the two cannot disagree about what counts.
    #
    # A state with no attributable date is NOT counted here. It passes
    # is_afternoon_scheduled_evidence (its run_classification is valid and its
    # timestamp clears the 2:30 PM floor -- neither of which reads trading_day)
    # while deployed_usd_by_claim_date drops every one of its candidates for
    # want of a date to file them under. A tree holding only such states would
    # have reported positive afternoon evidence behind a zero it guarantees,
    # which is the exact failure this whole block exists to make visible.
    afternoon = [s for s in states if is_afternoon_scheduled_evidence(s)]
    afternoon_undated = [s for s in afternoon if not has_attributable_deployment_date(s)]
    afternoon_seen = len(afternoon) - len(afternoon_undated)
    # Filtered by _relevant() too, for the REPORTED count below (not this
    # afternoon_seen subtraction, which keeps its own existing meaning) --
    # the same reasoning afternoon_invalid_notional below already gets: an
    # undated afternoon state dated before this report's widest trailing
    # window (as_of - EVIDENCE_WINDOW_DAYS) cannot move any figure the
    # report publishes, so it must not permanently trip the gated `== 0`
    # count over corruption no published figure could ever be affected by
    # (Codex review: "Scope undated evidence to the risk window").
    def _timestamp_relevant(state: Dict[str, Any]) -> bool:
        """Same window test as _relevant(), but keyed on the state's own
        run timestamp instead of its directory name.

        An UNDATED state has, by definition, no attributable deployment date
        for _latest_attributed_date() to compare against dir_day -- so a
        misfiled state (a recent scheduled run whose generated_at is well
        within the window, sitting under a stale dated directory) never
        trips path_dates_contradicted and _relevant() alone reads it as
        stale, dropping it from afternoon_states_undated even though it
        genuinely is recent evidence by timestamp (Codex review: "Retain
        recent undated evidence stored under stale paths"). generated_at is
        content, not a naming convention, so it needs no
        path_dates_contradicted-style corroboration to be trusted.
        """
        if as_of is None:
            return True
        ts = run_timestamp_et(state)
        if ts is None:
            return True
        start = as_of.toordinal() - (EVIDENCE_WINDOW_DAYS - 1)
        return ts.date().toordinal() >= start

    afternoon_undated_relevant = [
        s
        for s, path in zip(states, state_paths, strict=True)
        if is_afternoon_scheduled_evidence(s)
        and not has_attributable_deployment_date(s)
        and (_relevant(path) or path_dates_contradicted or _timestamp_relevant(s))
    ]
    # NOT subtracted from afternoon_seen, unlike afternoon_undated: an
    # invalid-notional state's OTHER candidates (if any) still land on real
    # dates and remain real evidence, so it is not the same "contributes
    # nothing at all" case undated states are. This is a distinct, gated
    # signal for a distinct failure mode -- a KNOWN order-bearing row whose
    # dollar figure could not be trusted (Codex review: "Mark invalid
    # deployed notionals as unusable evidence").
    #
    # Filtered by _relevant() too, unlike afternoon/afternoon_undated above:
    # an invalid-notional row in a state dated before this report's widest
    # trailing window (as_of - EVIDENCE_WINDOW_DAYS) cannot move any figure
    # the report publishes -- the same reasoning unreadable/unclassifiable
    # states already get via the deferred/outside_window split. Without this,
    # a single ancient corrupt artifact would trip the gated `== 0` count
    # forever, permanently blocking runs over corruption no published figure
    # could ever be affected by (Codex review: "Limit invalid-notional
    # evidence to the risk window").
    def _unusable_evidence_relevant(state: Dict[str, Any]) -> bool:
        """True when SOME unusable-notional finding in ``state`` is actually
        inside this report's widest trailing window.

        ``_relevant(path)`` alone only tests the artifact's own directory
        date -- but ``unusable_deployed_notional_dates()`` can report a
        finding attributed to a DIFFERENT (often older) date via a
        ``whole_share_claim_breakdown`` entry's own ``trading_day``. A
        recent, in-window artifact holding one healthy candidate plus one
        unusable candidate whose breakdown attributes it entirely before
        the window still passed the path-only check and tripped the
        documented `== 0` gate over uncertainty no published figure could
        ever be affected by (Codex review: "Scope invalid notionals by
        their attributed dates"). Mirrors ``unusable_evidence_dates``'s own
        None-is-in-window convention used by
        ``near_close_dca._apply_deployment_history``.
        """
        if as_of is None:
            return True
        dates = unusable_deployed_notional_dates(state)
        if not dates:
            return False
        start = as_of.toordinal() - (EVIDENCE_WINDOW_DAYS - 1)
        return any(d is None or d.toordinal() >= start for d in dates)

    afternoon_invalid_notional = [
        s
        for s, path in zip(states, state_paths, strict=True)
        if is_afternoon_scheduled_evidence(s)
        and has_unusable_deployed_notional(s)
        # Deliberately NOT `_relevant(path) or ...`: the whole point of this
        # fix is that a RECENT artifact path proves nothing about whether
        # the SPECIFIC unusable candidate inside it is actually in-window
        # (a whole_share_claim_breakdown entry can attribute it to a much
        # older date) -- the pre-fix `_relevant(path) or path_dates_
        # contradicted` OR would short-circuit True on a recent path alone
        # and never reach the precise per-finding check at all.
        # path_dates_contradicted keeps the SAME fail-closed override every
        # other check here applies when this tree's directory names cannot
        # be trusted to track its states' dates -- every candidate counts
        # then, regardless of what any date-based check says.
        and (path_dates_contradicted or _unusable_evidence_relevant(s))
    ]
    return states, {
        "state_root": str(root),
        "state_root_exists": root_path.exists(),
        "run_state_files_found": len(files),
        "run_states_loaded": len(states),
        "run_states_unreadable": len(unreadable),
        "unreadable_paths": sorted(unreadable),
        "run_states_unclassifiable": len(unclassifiable),
        "unclassifiable_paths": sorted(unclassifiable),
        "afternoon_states_seen": afternoon_seen,
        # Broken out rather than folded into run_states_unclassifiable: these
        # states ARE classifiable (that is how they reached the afternoon
        # predicate at all), they just cannot be dated. An operator chasing a
        # zero needs to see which of the two it is.
        #
        # This MUST be asserted == 0 in its own right, not merely subtracted
        # from afternoon_states_seen: one healthy afternoon state alongside an
        # undated one keeps afternoon_states_seen > 0, so the documented gate
        # passed while the undated state's real submitted notional was dropped
        # (Codex review: "Require zero undated states before sizing").
        "afternoon_states_undated": len(afternoon_undated_relevant),
        # This MUST be asserted == 0 in its own right too, for the same
        # reason afternoon_states_undated is: one healthy afternoon state
        # alongside a corrupt-notional one still leaves afternoon_states_seen
        # > 0, so a gate that checks only afternoon_states_seen passes while
        # that state's real order was silently discarded from the total
        # (Codex review: "Mark invalid deployed notionals as unusable
        # evidence").
        "afternoon_states_invalid_notional": len(afternoon_invalid_notional),
        # Reported rather than dropped: these files ARE unusable, they just
        # cannot affect any figure this report publishes. Hiding them entirely
        # would make the found/loaded counts disagree with no visible reason,
        # and an operator still wants to know the archive is rotting. Empty
        # whenever path_dates_contradicted -- see the loop above.
        "unusable_states_outside_window": len(outside_window),
        "outside_window_paths": sorted(outside_window),
    }


def _load_states(root: str | Path) -> List[Dict[str, Any]]:
    """The states alone, for callers that do not need the evidence counts."""
    states, _evidence = _load_states_with_evidence(root)
    return states


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Graded DCA rolling cash-burn / kill-switch report")
    parser.add_argument("--state-root", required=True, help="Directory containing run_state*.json artifacts")
    parser.add_argument(
        "--as-of",
        default=datetime.now(EASTERN).date().isoformat(),
        help="Report date (YYYY-MM-DD); defaults to today",
    )
    parser.add_argument(
        "--monthly-allocation-usd",
        type=float,
        default=None,
        help="Override the configured monthly cash allocation",
    )
    parser.add_argument(
        "--kill-switch-fraction",
        type=float,
        default=None,
        help="Override the 7-day deploy fraction that engages the kill-switch",
    )
    args = parser.parse_args(argv)

    from dca_config import load_strategy_config
    overrides: Dict[str, Any] = {}
    if args.monthly_allocation_usd is not None:
        overrides["monthly_cash_allocation_usd"] = args.monthly_allocation_usd
    if args.kill_switch_fraction is not None:
        overrides["kill_switch_7d_deploy_fraction"] = args.kill_switch_fraction
    config = load_strategy_config(**overrides)

    as_of = date.fromisoformat(args.as_of)
    states, evidence = _load_states_with_evidence(args.state_root, as_of=as_of)
    report = build_risk_report(states, as_of, config)
    # Additive: every existing field keeps its meaning. This says how much
    # history the numbers above were computed from, so a consumer can tell a
    # real zero from an unread tree before treating it as confirmed spend.
    report["evidence"] = evidence
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
