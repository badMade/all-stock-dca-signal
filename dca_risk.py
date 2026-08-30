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
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
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

    Reads :data:`DEPLOYED_CANDIDATE_STATUSES` -- the ONE set every deployment
    accounting path must consult, restored broker evidence included -- rather
    than a second local copy that could drift from it.
    """
    return isinstance(raw, str) and raw in DEPLOYED_CANDIDATE_STATUSES


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
                try:
                    total += float(c.get("planned_amount_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
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
        try:
            return round(float(budget.get("day_notional_usd") or 0.0), 2)
        except (TypeError, ValueError):
            return 0.0
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
        try:
            amount = float(c.get("planned_amount_usd") or 0.0)
        except (TypeError, ValueError):
            continue
        breakdown = c.get("whole_share_claim_breakdown")
        if not isinstance(breakdown, list) or not breakdown:
            if artifact_day is None:
                # A state is usable as deployment evidence only when every
                # deployed candidate can be filed under a date.  Returning a
                # partial bucket here would count a dated sibling while
                # silently dropping this candidate's real broker exposure.
                # That restores unproven risk headroom, so fail closed for the
                # whole state instead.
                return {}
            per_date[artifact_day] = round(per_date.get(artifact_day, 0.0) + amount, 2)
            continue
        accounted_usd = 0.0
        for entry in breakdown:
            if not isinstance(entry, dict):
                continue
            raw_day = entry.get("trading_day")
            try:
                entry_amount = float(entry.get("amount_usd") or 0.0)
            except (TypeError, ValueError):
                continue
            entry_day = artifact_day
            if isinstance(raw_day, str):
                try:
                    entry_day = date.fromisoformat(raw_day[:10])
                except ValueError:
                    entry_day = artifact_day
            accounted_usd += entry_amount
            if entry_day is None:
                continue
            per_date[entry_day] = round(per_date.get(entry_day, 0.0) + entry_amount, 2)
        remainder = round(amount - accounted_usd, 2)
        if remainder:
            if artifact_day is None:
                # The breakdown did not cover the entire placed amount and
                # there is no artifact date for the balance.  Do not retain
                # the attributable portion as if it proved the state complete.
                return {}
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
        "afternoon_states_undated": len(afternoon_undated),
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
