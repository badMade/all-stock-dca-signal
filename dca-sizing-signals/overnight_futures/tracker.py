"""Overnight futures move for one session (docs/adr-overnight-futures-tracker.md, D4, D9).

Pure computation over a row the caller passes in, the same shape as comovement_signals:
no store, no vendor, no clock. Ingestion lives in market_data_ingest/futures/ (phase 3).

Definitions (D4, from research section 1.2):

* Window: 16:00 ET on the prior NYSE session (13:00 ET after a half-day) to 09:29 ET on
  the session date. 09:29 rather than 09:30 so no print at or after the cash open is read.
* Raw move: ln(F_pre / F_anchor), on ONE contract for the whole window.
* Fair-value-adjusted move: raw move - ln(FV_pre / FV_anchor). Log form so the two moves
  are directly comparable; it removes the carry-minus-dividends sawtooth that a raw
  "futures minus prior close" picks up around rolls and ex-dates.
* Implied open gap: exp(adjusted move) - 1, relative to the prior index close.

Fail closed (D9): every refusal is a status code on the result. compute_overnight_session
never raises, whatever it is handed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from overnight_futures.fair_value import _finite

#: NYSE's zone. Always derived through the tz database, never a fixed offset (D4): a
#: fixed -5 is wrong for about eight months of the year, and silently so.
NEW_YORK = ZoneInfo("America/New_York")
CASH_CLOSE_ET = time(16, 0)
PRE_OPEN_ET = time(9, 29)
CASH_OPEN_ET = time(9, 30)

STATUS_OK = "ok"
STATUS_SPLICE = "rejected_contract_splice"
STATUS_PRICE = "rejected_invalid_price"
STATUS_LOOKAHEAD = "rejected_lookahead"
STATUS_WINDOW = "rejected_window"


@dataclass(frozen=True, slots=True)
class OvernightInputs:
    """One night's raw observations, as the store will hand them over."""

    root: str
    session_date: date
    anchor_contract_id: str
    pre_open_contract_id: str
    anchor_close_utc: datetime
    pre_open_utc: datetime
    f_anchor: float | None
    f_pre_open: float | None
    fair_value_anchor: float | None
    fair_value_pre_open: float | None
    is_half_day_anchor: bool


@dataclass(frozen=True, slots=True)
class OvernightSession:
    """The descriptive result for one night. Moves are None whenever status is not "ok",
    and the adjusted move and implied gap are also None when fair value is missing."""

    root: str
    session_date: date
    status: str
    fv_quality: str
    overnight_ret_raw: float | None
    overnight_ret_fv_adj: float | None
    implied_gap: float | None
    hours_in_window: float | None
    exclude_from_evaluation: bool


def session_window_utc(
    prior_session: date,
    session: date,
    anchor_close_et: time = CASH_CLOSE_ET,
    pre_open_et: time = PRE_OPEN_ET,
) -> tuple[datetime, datetime]:
    """Return (anchor, pre_open) in UTC for the night between two NYSE sessions.

    Callers pass dates they control (from an exchange calendar), so a reversed pair is a
    programming error and raises ValueError, unlike compute_overnight_session.
    """
    if session <= prior_session:
        raise ValueError(f"session {session} must be after prior_session {prior_session}")
    anchor = datetime.combine(prior_session, anchor_close_et, tzinfo=NEW_YORK)
    pre_open = datetime.combine(session, pre_open_et, tzinfo=NEW_YORK)
    return anchor.astimezone(timezone.utc), pre_open.astimezone(timezone.utc)


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _positive(value: object) -> bool:
    return _finite(value) and value > 0


def _rejected(inputs: OvernightInputs, status: str, hours: float | None = None) -> OvernightSession:
    return OvernightSession(
        root=inputs.root,
        session_date=inputs.session_date,
        status=status,
        fv_quality="missing",
        overnight_ret_raw=None,
        overnight_ret_fv_adj=None,
        implied_gap=None,
        hours_in_window=hours,
        exclude_from_evaluation=True,
    )


def _window_status(inputs: OvernightInputs) -> str | None:
    """None when the window is usable, else the rejection status."""
    if not _aware(inputs.anchor_close_utc) or not _aware(inputs.pre_open_utc):
        return STATUS_WINDOW
    if inputs.anchor_close_utc >= inputs.pre_open_utc:
        return STATUS_WINDOW
    if not isinstance(inputs.session_date, date):
        return STATUS_WINDOW
    cash_open = datetime.combine(inputs.session_date, CASH_OPEN_ET, tzinfo=NEW_YORK)
    if inputs.pre_open_utc >= cash_open:
        return STATUS_LOOKAHEAD
    return None


def compute_overnight_session(inputs: OvernightInputs) -> OvernightSession:
    """Compute one night's raw and fair-value-adjusted move. Never raises (D9)."""
    try:
        return _compute(inputs)
    except Exception:  # noqa: BLE001 -- D9: the boundary answers with a status, never an exception
        # Reaching here means a check above missed a garbage type; refuse the whole row.
        return _rejected(inputs, STATUS_WINDOW)


def _compute(inputs: OvernightInputs) -> OvernightSession:
    window_status = _window_status(inputs)
    if window_status is not None:
        return _rejected(inputs, window_status)
    hours = (inputs.pre_open_utc - inputs.anchor_close_utc) / timedelta(hours=1)

    # Never splice (D4): a roll inside the window turns the front-to-next spread, which is
    # carry and dividends, into a fake overnight move. An empty or missing id cannot prove
    # a single contract either, so it is refused the same way.
    anchor_id, pre_open_id = inputs.anchor_contract_id, inputs.pre_open_contract_id
    if not (isinstance(anchor_id, str) and anchor_id and anchor_id == pre_open_id):
        return _rejected(inputs, STATUS_SPLICE, hours)

    if not (_positive(inputs.f_anchor) and _positive(inputs.f_pre_open)):
        return _rejected(inputs, STATUS_PRICE, hours)

    raw = math.log(inputs.f_pre_open / inputs.f_anchor)
    has_fv = _positive(inputs.fair_value_anchor) and _positive(inputs.fair_value_pre_open)
    adjusted = raw - math.log(inputs.fair_value_pre_open / inputs.fair_value_anchor) if has_fv else None
    return OvernightSession(
        root=inputs.root,
        session_date=inputs.session_date,
        status=STATUS_OK,
        fv_quality="ok" if has_fv else "missing",
        overnight_ret_raw=raw,
        overnight_ret_fv_adj=adjusted,
        implied_gap=math.expm1(adjusted) if adjusted is not None else None,
        hours_in_window=hours,
        # D9: a half-day anchor is not a full session; keep the row, keep it out of scoring.
        exclude_from_evaluation=bool(inputs.is_half_day_anchor),
    )
