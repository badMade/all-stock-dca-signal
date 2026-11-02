"""Research-only tax-loss candidate report and wash-sale eligibility check.

Implements the PROPOSED ADR ``docs/adr-tax-loss-sale.md`` (PR #2099) on the Phase 0 ledger
(PR #2101), using the ADR's labelled proposed defaults. Nothing here is operator-approved
policy, and nothing here sells: no function in this module places, previews or cancels a
brokerage order, and it imports nothing that could (tests/ledger/test_tax_loss.py greps the
source for the broker's order-tool names). The operator places an approved sale by hand, or
tells Claude in chat to place that one sale (ADR decision 5) -- never from a scheduled run.

What it does:

* ``candidate_report`` -- lots in TAXABLE accounts with an unrealized loss of at least
  ``MIN_LOSS_PCT`` and at least ``MIN_LOSS_DOLLARS``, with the estimated loss, holding term,
  and every reason a sale today would not be eligible (ADR decision 4).
* ``eligibility`` -- one proposed sale of one ticker on one date: eligible, or not and why.
  Every rule fails closed: a missing, partial or unreadable input is "not eligible", never
  "assume clean" (ADR decision 6).
* ``pre_sale_lookback_window`` / ``post_sale_pause_window`` -- the two 31-day pauses.
* ``Approval`` + ``record_approval`` / ``approvals_known_at`` -- the operator's approval
  records in an append-only table. Only ``record_approval`` writes one, and nothing in this
  module calls it. ``eligibility`` and ``candidate_report`` read approvals FROM THE LEDGER
  (a connection plus an id or the as_of moment), never an in-memory ``Approval``, so an
  approval counts only once the operator has recorded it (review of #2104).

Inputs are plain rows a caller already fetched. No network, no broker calls, and no writes
from the two checks (they only SELECT, and do not even create the approvals table). Accounts
appear only as ``alias-<24 hex>`` (``accounts.account_alias``); broker order ids are refused
outright -- the wash-sale logic needs dates, not ids, and a labelled order id would trip the
account-number guard. No allowlist or exemption is added to that guard (project rule).

History inputs (review of #2104). Per account alias, acquisitions and option transactions
each arrive as ``{"covers_from": date, "rows": [...]}``. ``covers_from`` is the first day the
caller's read is complete for; an empty ``rows`` proves "no activity" only when it reaches
back to the start of the lookback (``sale_date - 31``). Option rows are TRANSACTIONS with
``opened_on``/``closed_on`` (``None`` = still open), not just currently open positions: a long
call bought on day -10 and sold on day -2 leaves nothing open but still blocks.

Window conventions (proposed, conservative):

* Pre-sale lookback: an acquisition of the ticker dated ``sale_date - 31`` through
  ``sale_date`` inclusive blocks. The statute's window is 30 days (IRC 1091, Pub. 550); day 31
  is a one-day margin matching the operator's "31-day buy pause". So the first clean sale
  date after a buy on day D is D + 32.
* Post-sale pause: ``sale_date`` through ``sale_date + 31`` inclusive. A long call or short
  put whose life overlaps either window blocks (ADR decision 3).
* Same-lot nuance (ADR context, independent review 2026-11-02): shares bought in the prior
  30 days do not wash a loss when those same shares are sold too, so the pre-sale pause is
  conservative rather than strictly required when every lot is sold. This module blocks
  anyway, intentionally, per the ADR -- the test suite pins that choice.
* "Substantially identical" is approximated by the exact ticker. Substitutes and spouse
  accounts are outside what the ledger can see; the operator's attestation covers accounts
  outside Robinhood (ADR open question 2 default).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .accounts import guard_account_keys, looks_like_account_identifier, require_alias
from .errors import AccountIdentifierError, LedgerValidationError

# --- proposed defaults (ADR PR #2099 open questions; NOT operator-approved) -----------------

PROPOSED_DEFAULTS_STATUS = "proposed, not operator-approved (ADR PR #2099 open questions 1-2)"

# Open question 1: a lot is a candidate at >= 15% AND >= $25 unrealized loss.
MIN_LOSS_PCT = 0.15
MIN_LOSS_DOLLARS = 25.0
# Not an ADR question: a quote older than this many calendar days (as_of date minus quote
# date) is stale and the lot is skipped, never scored on an old price. 4 covers a Friday
# quote read on the Tuesday after a Monday holiday.
MAX_QUOTE_AGE_DAYS = 4

# ADR decision 3: 31 calendar days on each side of the sale.
PAUSE_DAYS = 31

# An approval must still be valid when the sale session closes (review of #2104: an expiry
# at 00:01 ET on the sale day passed a date-only comparison). 16:00 ET is the regular close;
# on an early-close day it is merely conservative.
SESSION_CLOSE_ET = time(16, 0)

# Account kinds. A kind in neither set is unknown and its lots are skipped (fail closed).
# Robinhood-managed individual portfolios are taxable; IRAs are not (a loss there is not
# deductible, and an IRA BUY makes a taxable loss permanently disallowed -- which is why
# IRA acquisitions still count in the lookback even though IRA lots are never candidates).
TAXABLE_ACCOUNT_KINDS = frozenset({"agentic", "individual", "joint", "managed_individual"})
NON_TAXABLE_ACCOUNT_KINDS = frozenset({"traditional_ira", "roth_ira", "managed_roth_ira", "managed_traditional_ira"})

# Sessions, sale dates and approval expiry are judged on the US/Eastern calendar, like
# every slot in this repo; a 01:00Z timestamp is still the previous ET trading day.
MARKET_TZ = ZoneInfo("America/New_York")

APPROVALS_TABLE = "tax_loss_approvals"

# The only fields a history / acquisition / option row may carry. Anything else -- an order
# id above all -- is refused loudly rather than silently ignored, so it can never drift into
# a table.
_HISTORY_FIELDS = frozenset({"covers_from", "rows"})
_ACQUISITION_FIELDS = frozenset({"symbol", "acquired_date", "quantity", "kind"})
_OPTION_FIELDS = frozenset({"underlying", "option_type", "side", "quantity", "opened_on", "closed_on",
                            "expiration_date"})
_LOT_REQUIRED = frozenset({"account_alias", "symbol", "lot_key", "acquired_date", "snapshot_date", "quantity",
                           "cost_basis_per_share"})
_OPTION_KINDS = frozenset({("call", "long"), ("call", "short"), ("put", "long"), ("put", "short")})

_LOT_KEY = re.compile(r"[0-9a-f]{32}")
_APPROVAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,63}")
_LONG_DIGIT_RUN = re.compile(r"\d{6,}")
_EPS = 1e-9  # float slack for the threshold comparisons (15% of $500 is $75.0000000001 in binary)

_PROV = (
    "content_hash VARCHAR NOT NULL, source VARCHAR NOT NULL, as_of TIMESTAMP NOT NULL, "
    "ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL"
)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {APPROVALS_TABLE} (
    approval_id VARCHAR NOT NULL, ticker VARCHAR NOT NULL, sale_date DATE NOT NULL, lot_keys VARCHAR[] NOT NULL,
    max_shares DOUBLE NOT NULL, approved_at TIMESTAMP NOT NULL, expires_at TIMESTAMP NOT NULL,
    inputs_known_at TIMESTAMP NOT NULL,
    {_PROV},
    PRIMARY KEY (approval_id, content_hash)
);
"""


# --- small helpers ------------------------------------------------------------------------------


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_day(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _require_day(value: object, name: str) -> date:
    if not _is_day(value):
        raise TypeError(f"{name} must be a date (not a datetime)")
    return value  # type: ignore[return-value]


def _require_aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LedgerValidationError(f"{name} must be a timezone-aware datetime")
    return value


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)


def _market_day(moment: datetime) -> date:
    return moment.astimezone(MARKET_TZ).date()


def _session_close(day: date) -> datetime:
    return datetime.combine(day, SESSION_CLOSE_ET, tzinfo=MARKET_TZ)


def _norm_ticker(value: object) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def lot_key(lot_id: str) -> str:
    """The ledger's key for a broker lot id -- the same SHA-256 prefix ``LedgerStore.ingest_tax_lots``
    stores, so approvals and stored lots join without the raw lot id reaching any table."""
    if not isinstance(lot_id, str) or not lot_id.strip():
        raise LedgerValidationError("lot_id must be a non-empty string")
    return hashlib.sha256(f"lot:{lot_id}".encode()).hexdigest()[:32]


# --- windows ------------------------------------------------------------------------------------


def pre_sale_lookback_window(sale_date: date) -> tuple[date, date]:
    """Inclusive ``(start, end)``: an acquisition of the ticker on any of these days blocks."""
    day = _require_day(sale_date, "sale_date")
    return day - timedelta(days=PAUSE_DAYS), day


def post_sale_pause_window(sale_date: date) -> tuple[date, date]:
    """Inclusive ``(start, end)`` of the post-sale buy pause: the sale day plus 31 calendar
    days. The ticker may be bought again from ``end + 1 day``. Enforcing this in the executor
    is a later ADR (open question 4); until then the operator excludes the ticker by hand."""
    day = _require_day(sale_date, "sale_date")
    return day, day + timedelta(days=PAUSE_DAYS)


def _holding_term(acquired: date, sale_day: date) -> str:
    """Long term when held MORE than one year: sold after the first anniversary. Holding
    starts the day after acquisition, so selling ON the anniversary is still short term. A
    Feb 29 acquisition's anniversary is taken as Mar 1 (the later, more conservative day)."""
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # Feb 29 -> no such day next year
        anniversary = date(acquired.year + 1, 3, 1)
    return "long" if sale_day > anniversary else "short"


# --- approvals ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    """One operator approval to sell up to ``max_shares`` of ``ticker`` from ``lot_keys`` on
    ``sale_date`` ("that ticker, that date and that share count", ADR decision 1), valid from
    ``approved_at`` until (not including) ``expires_at``.

    The system never builds one of these on its own; an approval comes from the operator, is
    persisted only through ``record_approval``, and only counts once read back from the ledger
    (``approvals_known_at``). ``approval_id`` is the operator's label (e.g. ``tlh_zzza_dec``):
    it may not contain a run of six or more digits, so neither an account number nor a broker
    order id can be smuggled in as one.
    """

    approval_id: str
    ticker: str
    sale_date: date
    lot_keys: tuple[str, ...]
    max_shares: float
    approved_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        aid = self.approval_id
        if not isinstance(aid, str) or not _APPROVAL_ID.fullmatch(aid):
            raise LedgerValidationError("approval_id must be 3-64 characters of [A-Za-z0-9_.:-]")
        if _LONG_DIGIT_RUN.search(aid) or looks_like_account_identifier(aid):
            raise AccountIdentifierError("approval_id must not contain an account- or order-id-shaped number")
        ticker = _norm_ticker(self.ticker)
        if ticker is None:
            raise LedgerValidationError("ticker must be a non-empty string")
        _require_day(self.sale_date, "sale_date")
        keys = self.lot_keys
        if isinstance(keys, str) or not isinstance(keys, Iterable):
            raise LedgerValidationError("lot_keys must be a sequence of lot keys")
        keys = tuple(keys)
        if not keys or not all(isinstance(k, str) and _LOT_KEY.fullmatch(k) for k in keys):
            raise LedgerValidationError("lot_keys must be one or more keys from lot_key()")
        if not _finite(self.max_shares) or self.max_shares <= 0:
            raise LedgerValidationError("max_shares must be a finite positive number")
        _require_aware(self.approved_at, "approved_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.approved_at:
            raise LedgerValidationError("expires_at must be after approved_at")
        # Canonical form, so equality and the content hash do not depend on input spelling.
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "lot_keys", tuple(sorted(set(keys))))
        object.__setattr__(self, "max_shares", float(self.max_shares))
        object.__setattr__(self, "approved_at", self.approved_at.astimezone(timezone.utc))
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(timezone.utc))

    def content_hash(self) -> str:
        values = [self.approval_id, self.ticker, self.sale_date.isoformat(), list(self.lot_keys), self.max_shares,
                  self.approved_at.isoformat(), self.expires_at.isoformat()]
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def ensure_tax_loss_schema(con: Any) -> None:
    """Create this module's table on a ledger connection (``LedgerStore.con``) if missing.
    Idempotent; never alters or empties an existing table. Kept out of store.py on purpose
    so the tax-loss table ships, and can be reverted, on its own. Only the writer calls it;
    the read paths never create anything."""
    con.execute(_SCHEMA)


def _approvals_table_exists(con: Any) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'main' AND table_name = ?",
        [APPROVALS_TABLE],
    ).fetchone()
    return bool(row and row[0])


def record_approval(
    con: Any, approval: Approval, *, source: str, run_id: str, as_of: datetime,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Append one operator approval. The ONLY writer of the approvals table, and the only way
    an approval comes to exist; nothing in this package calls it. Returns rows inserted (0 when
    an identical approval was recorded before -- the content hash drops it). A changed
    approval (fewer shares, a new expiry) is a new row with a later ``approved_at``; readers
    take the version built from the newest information.

    ``as_of`` earlier than ``approved_at`` is refused: a record stamped before the decision it
    holds would be visible to a reader asking about a moment before the decision existed."""
    if not isinstance(approval, Approval):
        raise LedgerValidationError("record_approval takes an Approval")
    if not isinstance(source, str) or not source.strip() or not isinstance(run_id, str) or not run_id.strip():
        raise LedgerValidationError("source and run_id must be non-empty strings")
    stamp = _require_aware(as_of, "as_of")
    if stamp < approval.approved_at:
        raise LedgerValidationError("as_of must not be earlier than approved_at")
    ensure_tax_loss_schema(con)
    row = [
        approval.approval_id, approval.ticker, approval.sale_date, list(approval.lot_keys), approval.max_shares,
        _naive_utc(approval.approved_at), _naive_utc(approval.expires_at), _naive_utc(approval.approved_at),
        approval.content_hash(), source, _naive_utc(stamp), _naive_utc(clock()), run_id,
    ]
    con.execute("BEGIN TRANSACTION")
    try:
        before = con.execute(f"SELECT count(*) FROM {APPROVALS_TABLE}").fetchone()[0]
        con.execute(
            f"INSERT INTO {APPROVALS_TABLE} (approval_id, ticker, sale_date, lot_keys, max_shares, approved_at, "
            "expires_at, inputs_known_at, content_hash, source, as_of, ingested_at, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            row,
        )
        inserted = con.execute(f"SELECT count(*) FROM {APPROVALS_TABLE}").fetchone()[0] - before
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return int(inserted)


def approvals_known_at(con: Any, known_at: datetime) -> list[Approval]:
    """The latest version of every approval as known at ``known_at``: a row is visible only if
    it was both written (``as_of``) and decided (``inputs_known_at``) by then; among visible
    versions the most recently decided wins (same rule as the ledger's outcomes). Read-only:
    on a ledger with no approvals table yet it returns ``[]`` without creating one."""
    moment = _naive_utc(_require_aware(known_at, "known_at"))
    if not _approvals_table_exists(con):
        return []
    rows = con.execute(
        "SELECT approval_id, ticker, sale_date, lot_keys, max_shares, approved_at, expires_at FROM ("
        "SELECT *, row_number() OVER (PARTITION BY approval_id ORDER BY inputs_known_at DESC, as_of DESC, "
        f"ingested_at DESC, content_hash) AS rn FROM {APPROVALS_TABLE} "
        "WHERE as_of <= ? AND inputs_known_at <= ?) WHERE rn = 1 ORDER BY approval_id",
        [moment, moment],
    ).fetchall()
    return [
        Approval(approval_id=r[0], ticker=r[1], sale_date=r[2], lot_keys=tuple(r[3]), max_shares=r[4],
                 approved_at=_aware(r[5]), expires_at=_aware(r[6]))
        for r in rows
    ]


# --- eligibility --------------------------------------------------------------------------------


@dataclass(frozen=True)
class EligibilityResult:
    ticker: str
    sale_date: date
    eligible: bool
    reasons: tuple[str, ...]
    # Every acquisition of the ticker inside the lookback, with its account alias.
    washing_acquisitions: tuple[dict[str, Any], ...]
    # Latest in-lookback acquisition (dated no later than the check) + 32 days; None when none.
    earliest_clean_sale_date: date | None


def _history(value: Any, *, allowed: frozenset[str], kind: str) -> tuple[date | None, list[dict[str, Any]]] | None:
    """``(covers_from, rows)`` for one account's history, or None when the input is missing or
    not a readable ``{"covers_from", "rows"}`` dict (= unknown). ``covers_from`` may be None
    (= coverage unstated, judged incomplete by the caller). A row field outside ``allowed``
    raises, naming the field but never its value."""
    if not isinstance(value, Mapping):
        return None
    extra = sorted(set(value) - _HISTORY_FIELDS)
    if extra:
        raise LedgerValidationError(f"{kind}: history fields {extra} are not accepted")
    rows = value.get("rows")
    if rows is None or isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Sequence):
        return None
    covers = value.get("covers_from")
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            out.append({})  # shape-checked (and flagged malformed) by the caller
            continue
        extra = sorted(set(row) - allowed)
        if extra:
            raise LedgerValidationError(f"{kind}[{i}]: fields {extra} are not accepted (no order ids, no raw ids)")
        guard_account_keys(dict(row), where=f"{kind}[{i}]")
        out.append(dict(row))
    return (covers if _is_day(covers) else None), out


def _check_aliases(mapping: Mapping[str, Any], where: str) -> None:
    for alias in mapping:
        require_alias(alias, where=where)


def _approval_reasons(approval: Approval | None, symbol: str, day: date, keys: tuple[str, ...],
                      shares: Any, moment: datetime) -> set[str]:
    if approval is None:
        return {"no_approval"}
    reasons = set()
    if approval.approved_at > moment:  # defensive: approvals_known_at already hides these
        reasons.add("no_approval")
    if approval.expires_at <= moment:
        reasons.add("approval_expired")
    if approval.expires_at <= _session_close(day):
        reasons.add("approval_expires_before_sale")
    if approval.sale_date != day:
        reasons.add("approval_sale_date_mismatch")
    if approval.ticker != symbol:
        reasons.add("approval_ticker_mismatch")
    if not set(keys) <= set(approval.lot_keys):
        reasons.add("approval_lot_mismatch")
    if _finite(shares) and shares > approval.max_shares + _EPS:
        reasons.add("approval_shares_exceeded")
    return reasons


def _evaluate(
    symbol: str,
    day: date,
    acquisitions_by_account: Any,
    option_history_by_account: Any,
    drip_status: Any,
    approval: Approval | None,
    *,
    lot_keys: Sequence[str],
    shares: Any,
    tracked_accounts: Sequence[str],
    outside_accounts_attested: Any,
    moment: datetime,
) -> EligibilityResult:
    """The rules, on an approval already read from the ledger (or None). Pure."""
    reasons: set[str] = set()
    check_day = _market_day(moment)
    if day < check_day:
        reasons.add("sale_date_before_check")

    tracked = tuple(tracked_accounts or ())
    for alias in tracked:
        require_alias(alias, where="tracked_accounts")
    if not tracked:
        reasons.add("no_tracked_accounts")
    acquisitions = acquisitions_by_account if isinstance(acquisitions_by_account, Mapping) else {}
    options = option_history_by_account if isinstance(option_history_by_account, Mapping) else {}
    drip = drip_status if isinstance(drip_status, Mapping) else {}
    _check_aliases(acquisitions, "acquisitions_by_account")
    _check_aliases(options, "option_history_by_account")
    _check_aliases(drip, "drip_status")

    lookback_start, _ = pre_sale_lookback_window(day)
    _, pause_end = post_sale_pause_window(day)

    # 1. Acquisitions, in every account handed in -- tracked or not: a buy is evidence either
    # way. Coverage is required for tracked accounts only (an untracked one is extra evidence).
    washing: list[dict[str, Any]] = []
    latest: date | None = None
    for alias in sorted(set(tracked) | set(acquisitions)):
        hist = _history(acquisitions.get(alias), allowed=_ACQUISITION_FIELDS, kind="acquisitions")
        if hist is None:
            if alias in tracked:
                reasons.add("account_history_missing")
            continue
        covers, rows = hist
        if alias in tracked and (covers is None or covers > lookback_start):
            reasons.add("account_history_incomplete")
        for row in rows:
            row_symbol, when = _norm_ticker(row.get("symbol")), row.get("acquired_date")
            qty = row.get("quantity")
            if row_symbol is None or not _is_day(when) or (qty is not None and (not _finite(qty) or qty < 0)):
                reasons.add("malformed_acquisition")
                continue
            if row_symbol != symbol:
                continue
            if when > check_day:
                # History cannot hold a buy dated after the check: lookahead or a bad feed.
                # Fail closed, and never let it move the earliest clean date (review of #2104).
                reasons.add("acquisition_dated_after_check")
                continue
            if lookback_start <= when <= day:
                reasons.add("acquisition_in_lookback")
                latest = when if latest is None or when > latest else latest
                washing.append({"account_alias": alias, **row, "symbol": row_symbol})

    # 2. Options: "a contract or option so to acquire" (IRC 1091(a)). A long call, or a short
    # put (assignable inside the window whenever it was written; writing one inside the window
    # is treated as blocking until a tax professional says otherwise), blocks when its life
    # [opened_on, closed_on or expiration or forever] overlaps either window -- including one
    # bought and closed before the sale (review of #2104). Long puts and short calls do not
    # acquire shares.
    for alias in sorted(set(tracked) | set(options)):
        hist = _history(options.get(alias), allowed=_OPTION_FIELDS, kind="option_history")
        if hist is None:
            if alias in tracked:
                reasons.add("option_history_missing")
            continue
        covers, rows = hist
        if alias in tracked and (covers is None or covers > lookback_start):
            reasons.add("option_history_incomplete")
        for row in rows:
            underlying = _norm_ticker(row.get("underlying"))
            if underlying is None:
                reasons.add("malformed_option_history")
                continue
            if underlying != symbol:
                continue
            kind = (row.get("option_type"), row.get("side"))
            opened, closed, expires = row.get("opened_on"), row.get("closed_on"), row.get("expiration_date")
            if (
                kind not in _OPTION_KINDS
                or not _is_day(opened)
                or opened > check_day
                or (closed is not None and (not _is_day(closed) or closed < opened))
                or (expires is not None and not _is_day(expires))
            ):
                reasons.add("malformed_option_history")
                continue
            end = closed if closed is not None else (expires if expires is not None else date.max)
            if opened > pause_end or end < lookback_start:
                continue
            if kind == ("call", "long"):
                reasons.add("long_call_in_window")
            elif kind == ("put", "short"):
                reasons.add("short_put_in_window")

    # 3. DRIP must be provably off everywhere the ledger looks.
    for alias in sorted(set(tracked) | set(drip)):
        flag = drip.get(alias)
        if flag is True:
            reasons.add("drip_on")
        elif flag is not False:
            reasons.add("drip_unknown")

    # 4. The proposed sale itself and the operator's recorded approval for it.
    keys = tuple(lot_keys or ())
    if not keys:
        reasons.add("no_lots")
    if not _finite(shares) or shares <= 0:
        reasons.add("invalid_share_count")
    reasons |= _approval_reasons(approval, symbol, day, keys, shares, moment)

    # 5. Accounts the ledger cannot read (other brokers, a spouse's) need the operator's word.
    if outside_accounts_attested is not True:
        reasons.add("outside_accounts_not_attested")

    washing.sort(key=lambda r: (r["acquired_date"], r["account_alias"]))
    return EligibilityResult(
        ticker=symbol,
        sale_date=day,
        eligible=not reasons,
        reasons=tuple(sorted(reasons)),
        washing_acquisitions=tuple(washing),
        earliest_clean_sale_date=None if latest is None else latest + timedelta(days=PAUSE_DAYS + 1),
    )


def eligibility(
    ticker: str,
    sale_date: date,
    acquisitions_by_account: Mapping[str, Mapping[str, Any] | None] | None,
    option_history_by_account: Mapping[str, Mapping[str, Any] | None] | None,
    drip_status: Mapping[str, bool | None] | None,
    approval_id: str | None,
    *,
    con: Any,
    lot_keys: Sequence[str],
    shares: float | None,
    tracked_accounts: Sequence[str],
    outside_accounts_attested: bool | None,
    at: datetime,
) -> EligibilityResult:
    """Whether selling ``shares`` of ``ticker`` from ``lot_keys`` on ``sale_date`` would be an
    eligible tax-loss sale, judged at moment ``at``. Research output only: it says yes or no
    and why; it never acts. ``con`` is a ledger connection used ONLY to read approval
    ``approval_id`` as known at ``at``; an approval not recorded there is no approval.

    Blocks (every reason is reported, sorted, not just the first):

    * any acquisition of the ticker -- buy, DRIP, recurring, managed rebalance, anything -- in
      ANY account in ``acquisitions_by_account``, IRAs and managed accounts included, dated in
      the pre-sale lookback (``acquisition_in_lookback``), including the very lots being sold
      (conservative, per the ADR); one dated after the check (``acquisition_dated_after_check``);
    * a tracked account whose history is absent or unreadable (``account_history_missing``) or
      does not reach back to ``sale_date - 31`` (``account_history_incomplete``); no tracked
      accounts at all (``no_tracked_accounts``);
    * a long call or short put on the ticker whose life overlaps either window, in any account
      (``long_call_in_window`` / ``short_put_in_window``); absent or short option history for
      a tracked account (``option_history_missing`` / ``option_history_incomplete``); an
      unreadable row on the ticker (``malformed_option_history``);
    * DRIP on (``drip_on``) or anything but a literal ``False`` (``drip_unknown``);
    * no recorded approval, or one that is expired, ends before the sale session's close, is
      for another date or ticker, misses a lot, or is below the share count;
    * a sale date before the check (``sale_date_before_check``);
    * no operator attestation for accounts outside Robinhood (``outside_accounts_not_attested``).
    """
    symbol = _norm_ticker(ticker)
    if symbol is None:
        raise LedgerValidationError("ticker must be a non-empty string")
    day = _require_day(sale_date, "sale_date")
    moment = _require_aware(at, "at")
    approval = None
    if approval_id is not None:
        approval = next((a for a in approvals_known_at(con, moment) if a.approval_id == approval_id), None)
    return _evaluate(
        symbol, day, acquisitions_by_account, option_history_by_account, drip_status, approval,
        lot_keys=lot_keys, shares=shares, tracked_accounts=tracked_accounts,
        outside_accounts_attested=outside_accounts_attested, moment=moment,
    )


# --- candidate report ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LossThresholds:
    min_loss_pct: float = MIN_LOSS_PCT
    min_loss_dollars: float = MIN_LOSS_DOLLARS
    max_quote_age_days: int = MAX_QUOTE_AGE_DAYS

    def __post_init__(self) -> None:
        if not _finite(self.min_loss_pct) or not 0 < self.min_loss_pct < 1:
            raise LedgerValidationError("min_loss_pct must be in (0, 1)")
        if not _finite(self.min_loss_dollars) or self.min_loss_dollars < 0:
            raise LedgerValidationError("min_loss_dollars must be a finite non-negative number")
        age = self.max_quote_age_days
        if not isinstance(age, int) or isinstance(age, bool) or age < 0:
            raise LedgerValidationError("max_quote_age_days must be a non-negative int")


DEFAULT_THRESHOLDS = LossThresholds()


@dataclass(frozen=True)
class Candidate:
    account_alias: str
    account_kind: str
    symbol: str
    lot_key: str
    acquired_date: date
    quantity: float
    cost_basis_per_share: float
    cost_basis_total: float
    quote_price: float
    quote_date: date
    market_value: float
    estimated_loss: float  # positive dollars: cost basis minus market value
    loss_pct: float  # estimated_loss / cost_basis_total
    term: str  # "short" or "long", judged for a sale on the as_of date
    eligible: bool
    blocking_reasons: tuple[str, ...]
    washing_acquisitions: tuple[dict[str, Any], ...]
    earliest_clean_sale_date: date | None
    pause_days_needed: int  # calendar days from as_of until earliest_clean_sale_date (0 if none)


@dataclass(frozen=True)
class CandidateReport:
    as_of: datetime
    thresholds: LossThresholds
    candidates: tuple[Candidate, ...]
    # (lot_key, reason) for every lot that could not be judged: unknown account kind, no or
    # stale quote, a lot after as_of, an unreadable row. Never silently dropped.
    skipped: tuple[tuple[str, str], ...] = field(default=())


def _latest_lots(lots: Iterable[Mapping[str, Any]], as_of_day: date, skipped: list[tuple[str, str]]):
    """The lots in each account's NEWEST snapshot dated on or before ``as_of_day``, and only
    those (review of #2104: a lot sold since is absent from the newest snapshot and must not
    survive from an older one). Rows from ``LedgerStore.tax_lots()`` (provenance columns
    included) are accepted as they are; ``snapshot_date`` is required."""
    by_account: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for i, row in enumerate(lots):
        if not isinstance(row, Mapping):
            skipped.append((f"lots[{i}]", "malformed_lot"))
            continue
        guard_account_keys(dict(row), where=f"lots[{i}]")
        key = row.get("lot_key")
        valid_key = isinstance(key, str) and bool(_LOT_KEY.fullmatch(key))
        label = key if valid_key else f"lots[{i}]"
        snap = row.get("snapshot_date")
        if not valid_key or not _LOT_REQUIRED <= set(row) or not _is_day(snap):
            skipped.append((label, "malformed_lot"))
            continue
        alias = require_alias(row.get("account_alias"), where=f"lots[{i}]")
        if snap > as_of_day:
            continue  # a snapshot taken after as_of did not exist yet (lookahead): invisible
        by_account.setdefault(alias, []).append((label, row))
    chosen = []
    for entries in by_account.values():
        newest = max(r["snapshot_date"] for _, r in entries)
        per_lot: dict[str, list[Mapping[str, Any]]] = {}
        for label, r in entries:
            if r["snapshot_date"] == newest:
                per_lot.setdefault(label, []).append(r)
        for label, rows in per_lot.items():
            distinct = {json.dumps({k: str(r.get(k)) for k in sorted(_LOT_REQUIRED)}) for r in rows}
            if len(distinct) > 1:
                skipped.append((label, "ambiguous_lot_snapshot"))
                continue
            chosen.append(rows[0])
    return chosen


def candidate_report(
    lots: Iterable[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    as_of: datetime,
    thresholds: LossThresholds = DEFAULT_THRESHOLDS,
    *,
    con: Any,
    account_kinds: Mapping[str, str],
    acquisitions_by_account: Mapping[str, Mapping[str, Any] | None] | None = None,
    option_history_by_account: Mapping[str, Mapping[str, Any] | None] | None = None,
    drip_status: Mapping[str, bool | None] | None = None,
    outside_accounts_attested: bool | None = None,
) -> CandidateReport:
    """Research-only weekly report (ADR decision 4): every lot in a taxable account whose
    unrealized loss is at least ``min_loss_pct`` AND at least ``min_loss_dollars``, largest
    loss first, each with the reasons a sale on the as_of date would not be eligible. The
    wash-sale inputs are optional so the report can run on lots and quotes alone -- every one
    left out then shows up as a blocking reason, never as "clean".

    ``quotes`` maps ticker -> ``{"price", "quote_date"}``. ``account_kinds`` maps alias -> kind
    for every account the ledger tracks (``LedgerStore.accounts()``); every alias in it, and
    every alias holding a lot, must have complete acquisition, option and DRIP inputs.
    Approvals are read from ``con`` as known at ``as_of`` (read-only); one counts for a lot
    when it names the lot's ticker, the lot, and the as_of date as its sale date.
    """
    moment = _require_aware(as_of, "as_of")
    if not isinstance(thresholds, LossThresholds):
        raise LedgerValidationError("thresholds must be a LossThresholds")
    if not isinstance(account_kinds, Mapping):
        raise LedgerValidationError("account_kinds must map account aliases to kinds")
    _check_aliases(account_kinds, "account_kinds")
    as_of_day = _market_day(moment)
    approvals = approvals_known_at(con, moment)

    skipped: list[tuple[str, str]] = []
    lot_rows = _latest_lots(lots, as_of_day, skipped)
    tracked = tuple(sorted(set(account_kinds) | {r["account_alias"] for r in lot_rows}))
    candidates: list[Candidate] = []
    for row in lot_rows:
        alias, key = row["account_alias"], row["lot_key"]
        symbol = _norm_ticker(row.get("symbol"))
        acquired, qty, basis = row.get("acquired_date"), row.get("quantity"), row.get("cost_basis_per_share")
        if symbol is None or not _is_day(acquired) or not _finite(qty) or qty <= 0 or not _finite(basis) or basis < 0:
            skipped.append((key, "malformed_lot"))
            continue
        if acquired > as_of_day:
            skipped.append((key, "lot_after_as_of"))
            continue
        kind = account_kinds.get(alias)
        if kind in NON_TAXABLE_ACCOUNT_KINDS:
            continue  # taxable accounts only: an IRA loss is not deductible
        if kind not in TAXABLE_ACCOUNT_KINDS:
            skipped.append((key, "unknown_account_kind"))
            continue
        quote = quotes.get(symbol) if isinstance(quotes, Mapping) else None
        price = quote.get("price") if isinstance(quote, Mapping) else None
        qday = quote.get("quote_date") if isinstance(quote, Mapping) else None
        if not _finite(price) or price <= 0 or not _is_day(qday):
            skipped.append((key, "no_quote"))
            continue
        if qday > as_of_day:
            skipped.append((key, "quote_after_as_of"))
            continue
        if (as_of_day - qday).days > thresholds.max_quote_age_days:
            skipped.append((key, "stale_quote"))
            continue

        cost, value = qty * basis, qty * price
        loss = cost - value
        pct = loss / cost if cost > 0 else 0.0
        if loss < thresholds.min_loss_dollars - _EPS or pct < thresholds.min_loss_pct - _EPS:
            continue

        matching = [a for a in approvals if a.ticker == symbol and key in a.lot_keys and a.sale_date == as_of_day]
        live = [a for a in matching if a.expires_at > moment]
        approval = max(live or matching, key=lambda a: a.approved_at, default=None)
        check = _evaluate(
            symbol, as_of_day, acquisitions_by_account, option_history_by_account, drip_status, approval,
            lot_keys=(key,), shares=qty, tracked_accounts=tracked,
            outside_accounts_attested=outside_accounts_attested, moment=moment,
        )
        clean = check.earliest_clean_sale_date
        candidates.append(Candidate(
            account_alias=alias, account_kind=kind, symbol=symbol, lot_key=key, acquired_date=acquired,
            quantity=qty, cost_basis_per_share=basis, cost_basis_total=cost, quote_price=price, quote_date=qday,
            market_value=value, estimated_loss=loss, loss_pct=pct, term=_holding_term(acquired, as_of_day),
            eligible=check.eligible, blocking_reasons=check.reasons,
            washing_acquisitions=check.washing_acquisitions, earliest_clean_sale_date=clean,
            pause_days_needed=max(0, (clean - as_of_day).days) if clean else 0,
        ))
    candidates.sort(key=lambda c: (-c.estimated_loss, c.symbol, c.account_alias, c.lot_key))
    return CandidateReport(as_of=moment, thresholds=thresholds, candidates=tuple(candidates),
                           skipped=tuple(sorted(skipped)))


__all__ = [
    "APPROVALS_TABLE", "DEFAULT_THRESHOLDS", "MAX_QUOTE_AGE_DAYS", "MIN_LOSS_DOLLARS", "MIN_LOSS_PCT",
    "NON_TAXABLE_ACCOUNT_KINDS", "PAUSE_DAYS", "PROPOSED_DEFAULTS_STATUS", "SESSION_CLOSE_ET",
    "TAXABLE_ACCOUNT_KINDS", "Approval", "Candidate", "CandidateReport", "EligibilityResult", "LossThresholds",
    "approvals_known_at", "candidate_report", "eligibility", "ensure_tax_loss_schema", "lot_key",
    "post_sale_pause_window", "pre_sale_lookback_window", "record_approval",
]
