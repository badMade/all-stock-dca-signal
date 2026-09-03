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
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from near_close_dca import StrategyConfig, kill_switch_engaged

logger = logging.getLogger("near_close_dca.risk")

EASTERN = ZoneInfo("America/New_York")

# Afternoon scheduled evidence must not be accepted with a timestamp before this
# ET wall-clock time. Mirrors the "afternoon scheduled evidence must not accept
# timestamps before 2:30 PM ET" execution constraint.
AFTERNOON_EVIDENCE_FLOOR_ET = time(14, 30)

# A buy_candidates row carrying real broker-order evidence, matching
# scripts.scheduled_artifacts.ORDER_ATTEMPT_STATUSES minus
# "not_submitted_confirmation_required" (which never reached the broker, per
# that module's own docstring). A same-slot retry that restores a prior
# attempt's "filled"/"pending"/"unconfirmed" candidate (near_close_dca.
# run_daily_compact's merge) previously left it invisible to this module's
# deployment accounting, valuing real in-flight/completed spend at $0 and
# letting a later run reuse headroom the 7-day kill switch should have
# reserved (Codex review: "Count restored in-flight spend in deployment
# risk"). Defined locally rather than imported: this module is deliberately
# standard-library-only downstream of near_close_dca, and the set is small
# and stable enough not to need a shared source of truth.
_DEPLOYED_ORDER_STATUSES = frozenset({"submitted", "filled", "pending", "unconfirmed"})

# Rolling windows (calendar days).
BURN_WINDOW_DAYS = 30
KILL_SWITCH_WINDOW_DAYS = 7

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


def deployed_usd(state: Dict[str, Any]) -> float:
    """Capital actually deployed by a run_state: submitted buy candidates' notional.

    Sums ``planned_amount_usd`` over ``buy_candidates`` whose ``status`` is in
    :data:`_DEPLOYED_ORDER_STATUSES` (submitted/filled/pending/unconfirmed --
    every status representing real broker-order evidence). Falls back to
    ``budget.day_notional_usd`` when no per-candidate detail is present. Never
    counts skipped/blocked/not-submitted/confirmation-required candidates.
    """
    candidates = state.get("buy_candidates")
    if isinstance(candidates, list):
        submitted = [
            c for c in candidates
            if isinstance(c, dict) and c.get("status") in _DEPLOYED_ORDER_STATUSES
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
    """Like :func:`deployed_usd`, but buckets each submitted candidate's own
    ``planned_amount_usd`` under its TRUE execution date(s) rather than
    collapsing everything into this one artifact's own ``trading_day``.

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
        if not isinstance(c, dict) or c.get("status") not in _DEPLOYED_ORDER_STATUSES:
            continue
        try:
            amount = float(c.get("planned_amount_usd") or 0.0)
        except (TypeError, ValueError):
            continue
        breakdown = c.get("whole_share_claim_breakdown")
        if not isinstance(breakdown, list) or not breakdown:
            if artifact_day is None:
                continue
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
        if remainder and artifact_day is not None:
            per_date[artifact_day] = round(per_date.get(artifact_day, 0.0) + remainder, 2)
    return per_date


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

def _load_states(root: str | Path) -> List[Dict[str, Any]]:
    states: List[Dict[str, Any]] = []
    for path in find_run_state_files(root):
        try:
            data = json.loads(path.read_bytes())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("skipping unreadable run_state %s: %s", path, exc)
            continue
        # Exclude date-gate blocked stubs, but KEEP a contract-rejected state
        # (artifact_contract_failure marker): its real submitted orders are deployed
        # capital that must still count toward the kill-switch/cap accounting, even
        # though it is invalid scheduled evidence. deployed_usd sums only
        # status=='submitted' candidates, so a partial-placement failure contributes
        # exactly its real fills.
        if isinstance(data, dict) and (
            data.get("status") != "blocked"
            or data.get("artifact_contract_failure") is not None
        ):
            states.append(data)
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
    states = _load_states(args.state_root)
    report = build_risk_report(states, as_of, config)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
