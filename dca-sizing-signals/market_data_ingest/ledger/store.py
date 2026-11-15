"""LedgerStore: Phase 0 outcome ledger, tax lots, trial registry and universe tables.

Implements the PROPOSED ADR docs/adr-outcome-ledger-pit-harness.md (PR #2096) on the same
DuckDB file as ``market_data_ingest.prices.store.PriceStore``, with the same rules
(tests/ledger/test_ledger_schema.py):

* Append-only. There is no row-mutating SQL anywhere in this package. Every primary key
  includes a ``content_hash`` of the row's business values, so re-ingesting identical rows
  is dropped by ``ON CONFLICT DO NOTHING``; a correction has a new hash and lands as a new
  row with a later ``as_of``. Readers take the latest version known at ``known_at``.
* Every table carries ``source, as_of, ingested_at, run_id``. Timestamps are stored as
  naive UTC and converted to aware UTC at this module's boundary (as in the price store).
* One batch is validated in full before anything is written, and inserted in one
  transaction: one bad row writes nothing.
* No network and no broker calls. Callers pass already-fetched plain dicts; read-only
  broker access, where needed, is injected by the caller, never imported here.

Point-in-time reads are DuckDB table macros stored in the database file, so the DuckDB CLI
sees the same logic: ``outcome_candidates(known_at, horizons, tolerance)`` scores each fill and each
baseline row at fixed SESSION horizons with an ``ASOF LEFT JOIN`` on ``quote_date``
(``horizon_session >= quote_date``), so no quote dated after the horizon session -- or
ingested after ``known_at`` -- can leak in. ``scheduled_outcomes(known_at)`` drops manual
runs and every manual order id (docs/run_classification.md).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from ..prices.store import FAR_FUTURE, PriceStore, to_utc_naive
from .accounts import guard_account_keys, guard_text_fields, require_alias
from .defaults import (
    ALIAS_SCHEME,
    BASELINE_MULTIPLIER,
    BASELINE_RULE,
    OUTCOME_HORIZONS_SESSIONS,
    OUTCOME_QUOTE_TOLERANCE_SESSIONS,
    PROMOTION_DSR_THRESHOLD,
    RUN_KINDS,
)
from .errors import LedgerValidationError

FILL_SOURCES = frozenset({"run_artifact", "broker_order_history"})
PROPOSAL_STATUSES = frozenset({"proposed", "rejected_safeguard"})
OUTCOME_SOURCE = "ledger_outcomes"
_SLOT = re.compile(r"\d{2}:\d{2}")
_PROV = "content_hash VARCHAR NOT NULL, source VARCHAR NOT NULL, as_of TIMESTAMP NOT NULL, " \
    "ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS market_sessions (
    session DATE NOT NULL, {_PROV},
    PRIMARY KEY (session, content_hash)
);
CREATE TABLE IF NOT EXISTS fills (
    order_id VARCHAR NOT NULL, dca_run_id VARCHAR NOT NULL,
    run_kind VARCHAR NOT NULL CHECK (run_kind IN ('scheduled', 'manual')),
    slot VARCHAR NOT NULL, symbol VARCHAR NOT NULL, session DATE NOT NULL, filled_at TIMESTAMP NOT NULL,
    price DOUBLE NOT NULL, quantity DOUBLE NOT NULL, notional DOUBLE NOT NULL, multiplier DOUBLE NOT NULL,
    {_PROV},
    PRIMARY KEY (order_id, content_hash)
);
CREATE TABLE IF NOT EXISTS baseline_fills (
    decision_id VARCHAR NOT NULL, dca_run_id VARCHAR NOT NULL,
    run_kind VARCHAR NOT NULL CHECK (run_kind IN ('scheduled', 'manual')),
    slot VARCHAR NOT NULL, symbol VARCHAR NOT NULL, session DATE NOT NULL, decided_at TIMESTAMP NOT NULL,
    price DOUBLE NOT NULL, base_notional DOUBLE NOT NULL, baseline_multiplier DOUBLE NOT NULL,
    notional DOUBLE NOT NULL, quantity DOUBLE NOT NULL, baseline_rule VARCHAR NOT NULL,
    {_PROV},
    PRIMARY KEY (decision_id, content_hash)
);
CREATE TABLE IF NOT EXISTS outcomes (
    subject_kind VARCHAR NOT NULL CHECK (subject_kind IN ('fill', 'baseline')),
    subject_id VARCHAR NOT NULL, symbol VARCHAR NOT NULL, session DATE NOT NULL,
    entry_price DOUBLE NOT NULL, notional DOUBLE NOT NULL,
    horizon_sessions INTEGER NOT NULL, horizon_session DATE, quote_tolerance_sessions INTEGER NOT NULL,
    quote_date DATE,
    close_raw DOUBLE, price_factor DOUBLE, close_fill_basis DOUBLE, return_pct DOUBLE,
    status VARCHAR NOT NULL CHECK (status IN ('scored', 'unscored')), unscored_reason VARCHAR,
    inputs_known_at TIMESTAMP NOT NULL,
    {_PROV},
    PRIMARY KEY (subject_kind, subject_id, horizon_sessions, content_hash)
);
CREATE TABLE IF NOT EXISTS accounts (
    account_alias VARCHAR NOT NULL, account_kind VARCHAR NOT NULL, label VARCHAR,
    alias_scheme VARCHAR NOT NULL,
    {_PROV},
    PRIMARY KEY (account_alias, content_hash)
);
CREATE TABLE IF NOT EXISTS tax_lots (
    account_alias VARCHAR NOT NULL, symbol VARCHAR NOT NULL, lot_key VARCHAR NOT NULL,
    acquired_date DATE NOT NULL, snapshot_date DATE NOT NULL,
    quantity DOUBLE NOT NULL, cost_basis_per_share DOUBLE NOT NULL,
    {_PROV},
    PRIMARY KEY (account_alias, lot_key, snapshot_date, content_hash)
);
CREATE TABLE IF NOT EXISTS trials (
    trial_id VARCHAR NOT NULL, experiment VARCHAR NOT NULL,
    config_json VARCHAR NOT NULL, config_hash VARCHAR NOT NULL, metrics_json VARCHAR NOT NULL,
    sharpe DOUBLE, n_observations BIGINT NOT NULL, evaluated_at TIMESTAMP NOT NULL,
    promotion_threshold DOUBLE NOT NULL,
    {_PROV},
    PRIMARY KEY (trial_id, content_hash)
);
CREATE TABLE IF NOT EXISTS universe_scores (
    as_of_date DATE NOT NULL, ticker VARCHAR NOT NULL, sector_sic VARCHAR,
    momentum DOUBLE, profitability DOUBLE, low_vol DOUBLE, "value" DOUBLE, composite DOUBLE, "rank" INTEGER,
    source_quote_date DATE NOT NULL, source_filed_date DATE,
    {_PROV},
    PRIMARY KEY (as_of_date, ticker, content_hash)
);
CREATE TABLE IF NOT EXISTS universe_proposals (
    review_id VARCHAR NOT NULL, as_of_date DATE NOT NULL,
    adds VARCHAR[] NOT NULL, drops VARCHAR[] NOT NULL,
    reasons_json VARCHAR NOT NULL, safeguard_results_json VARCHAR NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('proposed', 'rejected_safeguard')),
    {_PROV},
    PRIMARY KEY (review_id, content_hash)
);
CREATE TABLE IF NOT EXISTS universe_membership (
    ticker VARCHAR NOT NULL, effective_from DATE NOT NULL, effective_to DATE, approval_id VARCHAR NOT NULL,
    {_PROV},
    PRIMARY KEY (ticker, effective_from, content_hash)
);
CREATE TABLE IF NOT EXISTS goal_versions (
    goal_id VARCHAR NOT NULL, goal_version INTEGER NOT NULL, kind VARCHAR NOT NULL,
    metric VARCHAR, horizon_years INTEGER, target DOUBLE,
    effective_from TIMESTAMP NOT NULL, goal_json VARCHAR NOT NULL,
    {_PROV},
    PRIMARY KEY (goal_id, goal_version, content_hash)
);
-- Goal progress Phase 2 (docs/adr-goals-and-loss-hedge-shadow.md). A correction usually lands
-- with the SAME as_of as the row it corrects (it restates the same close) and, under a fixed
-- test clock, the same ingested_at too, so content_hash alone would pick an arbitrary winner.
-- ingest_seq is a monotonic insertion order the readers use as the final tie-break: the row
-- ingested last wins. It is outside content_hash and the primary key, so an identical
-- re-ingest is still dropped by ON CONFLICT DO NOTHING (it only burns a sequence value).
CREATE SEQUENCE IF NOT EXISTS goal_progress_ingest_seq;
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_day DATE NOT NULL, symbol VARCHAR NOT NULL, quantity DOUBLE NOT NULL,
    close_day DATE NOT NULL, prior_close DOUBLE NOT NULL, market_value DOUBLE NOT NULL,
    ingest_seq BIGINT NOT NULL DEFAULT nextval('goal_progress_ingest_seq'),
    {_PROV},
    PRIMARY KEY (snapshot_day, symbol, content_hash)
);
CREATE TABLE IF NOT EXISTS goal_progress (
    "day" DATE NOT NULL, goal_id VARCHAR NOT NULL, goal_version INTEGER NOT NULL,
    window_days INTEGER NOT NULL, period_return DOUBLE NOT NULL,
    annualized_return DOUBLE, mwr_annualized DOUBLE,
    ingest_seq BIGINT NOT NULL DEFAULT nextval('goal_progress_ingest_seq'),
    {_PROV},
    PRIMARY KEY ("day", goal_id, goal_version, content_hash)
);
"""


def _latest(table: str, key: str) -> str:
    """Latest version of each ``key`` among rows known at ``known_at`` (a macro parameter)."""
    return (
        f"SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY {key} "
        f"ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn FROM {table} WHERE as_of <= known_at) "
        "WHERE rn = 1"
    )


_SPLIT_TYPES = "('forward_split', 'reverse_split', 'stock_dividend')"

_MACROS = f"""
CREATE OR REPLACE MACRO fills_known_at(known_at) AS TABLE {_latest("fills", "order_id")};
CREATE OR REPLACE MACRO baseline_fills_known_at(known_at) AS TABLE {_latest("baseline_fills", "decision_id")};
-- Outcomes need two filters and a different order than the other tables (review of #2101):
-- a row is visible only if it was both WRITTEN (as_of) and BUILT FROM data known (known_at)
-- (inputs_known_at) by the reader's moment, and among visible rows the one built from the newest information
-- wins -- a later re-score run with an older known_at must not displace a better-informed one.
-- The column is inputs_known_at, not known_at, so it cannot collide with the macro parameter
-- when another macro passes its own known_at through.
CREATE OR REPLACE MACRO outcomes_known_at(known_at) AS TABLE
    SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (
        PARTITION BY subject_kind, subject_id, horizon_sessions
        ORDER BY inputs_known_at DESC, as_of DESC, ingested_at DESC, content_hash) AS rn
        FROM outcomes WHERE as_of <= known_at AND inputs_known_at <= known_at)
    WHERE rn = 1;

-- One candidate outcome per (subject, horizon). Horizons count TRADING SESSIONS strictly after
-- the subject's session, from the market_sessions calendar known at known_at. The quote is the
-- latest bar ON OR BEFORE the horizon session (ASOF), so a quote dated after it can never be
-- used; a bar ingested after known_at is invisible through bars_known_at.
--
-- ASOF alone would accept a quote of ANY age (review of #2101: a lone t+1 bar was scored as
-- the t+20 outcome). So the matched quote must itself be a calendar session no more than
-- ``tolerance`` sessions before the horizon session (default 0: the horizon session exactly);
-- otherwise the row is unscored as stale_quote. A subject whose own session is missing from
-- the calendar is unscored (session_not_in_calendar) instead of having every horizon silently
-- counted one session early. Reasons are checked in that order of precedence.
CREATE OR REPLACE MACRO outcome_candidates(known_at, horizons, tolerance) AS TABLE
    WITH cal AS (
        SELECT session, row_number() OVER (ORDER BY session) AS n
        FROM (SELECT DISTINCT session FROM market_sessions WHERE as_of <= known_at)),
    subj AS (
        SELECT 'fill' AS subject_kind, order_id AS subject_id, symbol, session, price AS entry_price, notional
        FROM fills_known_at(known_at)
        UNION ALL
        SELECT 'baseline', decision_id, symbol, session, price, notional FROM baseline_fills_known_at(known_at)),
    based AS (
        SELECT s.*, (SELECT count(*) FROM cal AS p WHERE p.session <= s.session) AS base_n,
            EXISTS (SELECT 1 FROM cal AS p WHERE p.session = s.session) AS in_calendar
        FROM subj AS s),
    hz AS (SELECT unnest(horizons) AS horizon_sessions),
    targeted AS (
        SELECT b.*, hz.horizon_sessions, c.session AS horizon_session, c.n AS horizon_n
        FROM based AS b CROSS JOIN hz LEFT JOIN cal AS c ON c.n = b.base_n + hz.horizon_sessions),
    quotes AS (SELECT symbol AS q_symbol, session AS quote_date, close AS q_close FROM bars_known_at(known_at)),
    matched AS (
        SELECT t.*, q.quote_date, q.q_close, qc.n AS quote_n
        FROM targeted AS t ASOF LEFT JOIN quotes AS q
            ON t.symbol = q.q_symbol AND t.horizon_session >= q.quote_date
        LEFT JOIN cal AS qc ON qc.session = q.quote_date),
    factors AS (
        SELECT m.subject_kind, m.subject_id, m.horizon_sessions, product(a.split_from / a.split_to) AS pf
        FROM matched AS m JOIN actions_known_at(known_at) AS a
            ON a.symbol = m.symbol AND a.ex_date > m.session AND a.ex_date <= m.quote_date
            AND a.action_type IN {_SPLIT_TYPES}
        GROUP BY m.subject_kind, m.subject_id, m.horizon_sessions),
    graded AS (
        SELECT m.*, coalesce(f.pf, 1.0) AS pf,
            CASE WHEN NOT m.in_calendar THEN 'session_not_in_calendar'
                 WHEN m.horizon_session IS NULL THEN 'horizon_not_reached'
                 WHEN m.quote_date IS NULL OR m.quote_date <= m.session THEN 'no_quote'
                 WHEN m.quote_n IS NULL OR m.quote_n < m.horizon_n - tolerance THEN 'stale_quote'
            END AS unscored_reason
        FROM matched AS m LEFT JOIN factors AS f USING (subject_kind, subject_id, horizon_sessions))
    SELECT subject_kind, subject_id, symbol, session, entry_price, notional,
        CAST(horizon_sessions AS INTEGER) AS horizon_sessions,
        CASE WHEN in_calendar THEN horizon_session END AS horizon_session,
        CAST(tolerance AS INTEGER) AS quote_tolerance_sessions,
        CASE WHEN unscored_reason IS NULL THEN quote_date END AS quote_date,
        CASE WHEN unscored_reason IS NULL THEN q_close END AS close_raw,
        CASE WHEN unscored_reason IS NULL THEN pf END AS price_factor,
        CASE WHEN unscored_reason IS NULL THEN q_close / pf END AS close_fill_basis,
        CASE WHEN unscored_reason IS NULL THEN q_close / pf / entry_price - 1 END AS return_pct,
        CASE WHEN unscored_reason IS NULL THEN 'scored' ELSE 'unscored' END AS status,
        unscored_reason
    FROM graded;

-- Scheduled-live metrics only: manual runs AND every order id ever tagged manual are excluded
-- (docs/run_classification.md). An id tagged both ways is ambiguous, so it is excluded too.
CREATE OR REPLACE MACRO scheduled_outcomes(known_at) AS TABLE
    WITH subj AS (
        SELECT 'fill' AS kind, order_id AS id, run_kind FROM fills_known_at(known_at)
        UNION ALL
        SELECT 'baseline', decision_id, run_kind FROM baseline_fills_known_at(known_at)),
    manual AS (
        SELECT DISTINCT 'fill' AS kind, order_id AS id FROM fills WHERE run_kind = 'manual' AND as_of <= known_at
        UNION
        SELECT DISTINCT 'baseline', decision_id FROM baseline_fills WHERE run_kind = 'manual' AND as_of <= known_at)
    SELECT o.* FROM outcomes_known_at(known_at) AS o
    JOIN subj ON subj.kind = o.subject_kind AND subj.id = o.subject_id
    WHERE subj.run_kind = 'scheduled'
      AND NOT EXISTS (SELECT 1 FROM manual AS m WHERE m.kind = o.subject_kind AND m.id = o.subject_id);

-- Every trial ever recorded counts, including re-evaluations of a known config: a deflated
-- Sharpe bar needs the honest trial count.
CREATE OR REPLACE MACRO trial_counts(known_at) AS TABLE
    SELECT experiment, count(DISTINCT trial_id) AS n_trials, count(DISTINCT config_hash) AS n_configs
    FROM trials WHERE as_of <= known_at GROUP BY experiment;

-- Half-open intervals: a member on as_of_date when effective_from <= as_of_date < effective_to.
CREATE OR REPLACE MACRO universe_members_as_of(as_of_date, known_at) AS TABLE
    SELECT ticker, effective_from, effective_to, approval_id
    FROM ({_latest("universe_membership", "ticker, effective_from")})
    WHERE effective_from <= as_of_date AND (effective_to IS NULL OR as_of_date < effective_to);
"""


# --- small helpers ----------------------------------------------------------------------------


def _hash(values: Sequence[Any]) -> str:
    return hashlib.sha256(json.dumps(list(values), default=str, separators=(",", ":")).encode()).hexdigest()


def _canonical_json(value: Any, *, where: str, field: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError(f"{where}: {field} is not finite, JSON-serialisable data") from exc


def _from_db(value: Any) -> Any:
    return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) else value


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Row:
    """Validation of one input dict. Collects problems; ``LedgerStore`` raises them as a batch."""

    def __init__(self, row: Any, *, kind: str, index: int, required: set[str], optional: frozenset[str] = frozenset()):
        self.where = f"{kind}[{index}]"
        self.problems: list[str] = []
        if not isinstance(row, dict):
            raise LedgerValidationError(f"{self.where}: expected a dict")
        self.row = row
        missing = sorted(required - row.keys())
        unknown = sorted(row.keys() - required - optional)
        if missing:
            self.problems.append(f"{self.where}: missing {missing}")
        if unknown:
            self.problems.append(f"{self.where}: unknown fields {unknown}")

    def text(self, key: str, *, optional: bool = False) -> str | None:
        value = self.row.get(key)
        if value is None and optional:
            return None
        if not isinstance(value, str) or not value.strip():
            self.problems.append(f"{self.where}: {key} must be a non-empty string")
            return None
        return value

    def number(self, key: str, *, positive: bool = False, optional: bool = False) -> float | None:
        value = self.row.get(key)
        if value is None and optional:
            return None
        if not _finite(value) or value < 0 or (positive and value == 0):
            kind = "positive" if positive else "non-negative"
            self.problems.append(f"{self.where}: {key} must be a finite {kind} number")
            return None
        return float(value)

    def signed(self, key: str) -> float | None:
        value = self.row.get(key)
        if value is None:
            return None
        if not _finite(value):
            self.problems.append(f"{self.where}: {key} must be a finite number or None")
            return None
        return float(value)

    def day(self, key: str, *, optional: bool = False) -> date | None:
        value = self.row.get(key)
        if value is None and optional:
            return None
        if not isinstance(value, date) or isinstance(value, datetime):
            self.problems.append(f"{self.where}: {key} must be a date")
            return None
        return value

    def moment(self, key: str) -> datetime | None:
        value = self.row.get(key)
        if not isinstance(value, datetime) or value.tzinfo is None:
            self.problems.append(f"{self.where}: {key} must be a timezone-aware datetime")
            return None
        return to_utc_naive(value)

    def choice(self, key: str, allowed: Iterable[str]) -> str | None:
        value = self.row.get(key)
        allowed = tuple(allowed)
        if value not in allowed:
            self.problems.append(f"{self.where}: {key} must be one of {list(allowed)}")
            return None
        return value


def _raise_if(problems: list[str], kind: str) -> None:
    if problems:
        shown = "; ".join(problems[:20])
        more = f" (+{len(problems) - 20} more)" if len(problems) > 20 else ""
        raise LedgerValidationError(f"{len(problems)} invalid {kind} row problem(s): {shown}{more}")


# --- the store ----------------------------------------------------------------------------------


class LedgerStore:
    """Owns one DuckDB connection (through a ``PriceStore`` on the same file)."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = _utc_now) -> None:
        # The PriceStore creates daily_bars_raw / corporate_actions and their macros, which
        # outcome_candidates reads. Sharing its connection keeps one writer per process.
        self.prices = PriceStore(path, clock=clock)
        self._clock = clock
        self.con.execute(_SCHEMA)
        self.con.execute(_MACROS)

    def __enter__(self) -> LedgerStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.prices.close()

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        return self.prices.con

    # --- generic -----------------------------------------------------------------------------

    def tables(self) -> list[str]:
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        return [r[0] for r in rows]

    def _check_table(self, table: str) -> str:
        if table not in self.tables():
            raise ValueError(f"unknown table {table!r}")
        return table

    def table_columns(self, table: str) -> list[str]:
        rows = self.con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
            [self._check_table(table)],
        ).fetchall()
        return [r[0] for r in rows]

    def row_count(self, table: str) -> int:
        row = self.con.execute(f"SELECT count(*) FROM {self._check_table(table)}").fetchone()
        return int(row[0]) if row else 0

    def _rows(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        cursor = self.con.execute(sql, params if isinstance(params, dict) else list(params))
        names = [d[0] for d in cursor.description]
        return [{k: _from_db(v) for k, v in zip(names, row, strict=True)} for row in cursor.fetchall()]

    @staticmethod
    def _known(known_at: datetime | None) -> datetime:
        return FAR_FUTURE if known_at is None else to_utc_naive(known_at)

    def _insert(self, table: str, rows: list[dict[str, Any]], *, source: str, run_id: str, as_of: datetime) -> int:
        """Insert validated rows (each already carrying ``content_hash``) in one transaction."""
        if not isinstance(source, str) or not source.strip() or not isinstance(run_id, str) or not run_id.strip():
            raise LedgerValidationError(f"{table}: source and run_id must be non-empty strings")
        stamp, ingested_at = to_utc_naive(as_of), to_utc_naive(self._clock())
        if not rows:
            return 0
        prov = {"source": source, "as_of": stamp, "ingested_at": ingested_at, "run_id": run_id}
        full = [{**r, **prov} for r in rows]
        cols = list(full[0])
        quoted = ", ".join(f'"{c}"' for c in cols)
        marks = ", ".join("?" for _ in cols)
        self.con.execute("BEGIN TRANSACTION")
        try:
            before = self.row_count(table)
            self.con.executemany(
                f"INSERT INTO {table} ({quoted}) VALUES ({marks}) ON CONFLICT DO NOTHING",
                [[r[c] for c in cols] for r in full],
            )
            inserted = self.row_count(table) - before
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return inserted

    # --- calendar ------------------------------------------------------------------------------

    def ingest_market_sessions(self, sessions: Iterable[date], *, source: str, run_id: str, as_of: datetime) -> int:
        """Open trading sessions (from a calendar the caller already fetched). Horizons count these."""
        days = list(sessions)
        bad = [i for i, d in enumerate(days) if not isinstance(d, date) or isinstance(d, datetime)]
        _raise_if([f"market_sessions[{i}]: session must be a date" for i in bad], "market_sessions")
        rows = [{"session": d, "content_hash": _hash([d.isoformat()])} for d in sorted(set(days))]
        return self._insert("market_sessions", rows, source=source, run_id=run_id, as_of=as_of)

    # --- fills and baseline ------------------------------------------------------------------

    _FILL_KEYS = {
        "order_id", "dca_run_id", "run_kind", "slot", "symbol", "session", "filled_at",
        "price", "quantity", "notional", "multiplier",
    }

    def ingest_fills(self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime) -> int:
        """Live fills from run artifacts or broker order history. ``run_kind`` is required."""
        to_utc_naive(as_of)  # refuse naive timestamps before any validation noise
        problems: list[str] = []
        if source not in FILL_SOURCES:
            problems.append(f"fills: source must be one of {sorted(FILL_SOURCES)}")
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="fills", index=i, required=self._FILL_KEYS)
            # Keys only: broker order ids are UUIDs whose first segment can be all digits,
            # which the value scan would (wrongly) call account-shaped. Fills carry no
            # account field at all, and any account-named key is refused here.
            guard_account_keys(raw, where=r.where)
            out = {
                "order_id": r.text("order_id"),
                "dca_run_id": r.text("dca_run_id"),
                "run_kind": r.choice("run_kind", RUN_KINDS),
                "slot": r.text("slot"),
                "symbol": r.text("symbol"),
                "session": r.day("session"),
                "filled_at": r.moment("filled_at"),
                "price": r.number("price", positive=True),
                "quantity": r.number("quantity", positive=True),
                "notional": r.number("notional", positive=True),
                "multiplier": r.number("multiplier"),
            }
            if out["slot"] and not _SLOT.fullmatch(out["slot"]):
                r.problems.append(f"{r.where}: slot must be HH:MM")
            problems += r.problems
            out["content_hash"] = _hash([out[k] for k in sorted(self._FILL_KEYS)])
            prepared.append(out)
        _raise_if(problems, "fills")
        return self._insert("fills", prepared, source=source, run_id=run_id, as_of=as_of)

    _BASELINE_KEYS = {"decision_id", "dca_run_id", "run_kind", "slot", "symbol", "session", "decided_at",
                      "price", "base_notional"}

    def ingest_baseline_fills(
        self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime
    ) -> int:
        """One counterfactual baseline buy per slot decision: BASELINE_MULTIPLIER (1x) of the
        decision's base amount, at the same slot timestamp and price (proposed default)."""
        to_utc_naive(as_of)
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="baseline_fills", index=i, required=self._BASELINE_KEYS)
            guard_account_keys(raw, where=r.where)
            out: dict[str, Any] = {
                "decision_id": r.text("decision_id"),
                "dca_run_id": r.text("dca_run_id"),
                "run_kind": r.choice("run_kind", RUN_KINDS),
                "slot": r.text("slot"),
                "symbol": r.text("symbol"),
                "session": r.day("session"),
                "decided_at": r.moment("decided_at"),
                "price": r.number("price", positive=True),
                "base_notional": r.number("base_notional", positive=True),
            }
            problems += r.problems
            if not r.problems:
                out["baseline_multiplier"] = BASELINE_MULTIPLIER
                out["notional"] = out["base_notional"] * BASELINE_MULTIPLIER
                out["quantity"] = out["notional"] / out["price"]
                out["baseline_rule"] = BASELINE_RULE
                out["content_hash"] = _hash([out[k] for k in sorted(out)])
            prepared.append(out)
        _raise_if(problems, "baseline_fills")
        return self._insert("baseline_fills", prepared, source=source, run_id=run_id, as_of=as_of)

    def fills(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM fills_known_at(?) ORDER BY order_id", [self._known(known_at)])

    def baseline_fills(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM baseline_fills_known_at(?) ORDER BY decision_id", [self._known(known_at)])

    # --- outcomes ----------------------------------------------------------------------------

    def record_outcomes(
        self,
        *,
        known_at: datetime,
        run_id: str,
        as_of: datetime,
        horizons: Sequence[int] = OUTCOME_HORIZONS_SESSIONS,
        quote_tolerance_sessions: int = OUTCOME_QUOTE_TOLERANCE_SESSIONS,
    ) -> int:
        """Score every fill and baseline row at ``horizons`` as known at ``known_at`` and append
        the results. Unscorable rows are appended as ``unscored`` with a reason, never as zero.
        Identical results to an earlier scoring are dropped by their content hash."""
        if not horizons or not all(isinstance(h, int) and not isinstance(h, bool) and h > 0 for h in horizons):
            raise LedgerValidationError("horizons must be positive integers")
        tol = quote_tolerance_sessions
        if not isinstance(tol, int) or isinstance(tol, bool) or tol < 0:
            raise LedgerValidationError("quote_tolerance_sessions must be a non-negative int")
        # A score stamped earlier than the information it used would be visible to readers
        # asking about a moment before that information existed (review of #2101). Fail closed.
        if to_utc_naive(as_of) < to_utc_naive(known_at):
            raise LedgerValidationError("as_of must not be earlier than known_at")
        hashed = (
            "sha256(concat_ws('|', subject_kind, subject_id, CAST(horizon_sessions AS VARCHAR), "
            "CAST(quote_tolerance_sessions AS VARCHAR), "
            + ", ".join(
                f"coalesce(CAST({c} AS VARCHAR), '-')"
                for c in ("entry_price", "notional", "horizon_session", "quote_date", "close_raw", "price_factor",
                          "status", "unscored_reason")
            )
            + "))"
        )
        params = {
            "known_at": self._known(known_at),
            "horizons": list(horizons),
            "tolerance": tol,
            "source": OUTCOME_SOURCE,
            "as_of": to_utc_naive(as_of),
            "ingested_at": to_utc_naive(self._clock()),
            "run_id": run_id,
        }
        self.con.execute("BEGIN TRANSACTION")
        try:
            before = self.row_count("outcomes")
            self.con.execute(
                "INSERT INTO outcomes SELECT subject_kind, subject_id, symbol, session, entry_price, notional, "
                "horizon_sessions, horizon_session, quote_tolerance_sessions, quote_date, close_raw, price_factor, "
                "close_fill_basis, "
                "return_pct, status, unscored_reason, $known_at, "
                f"{hashed}, $source, $as_of, $ingested_at, $run_id "
                "FROM outcome_candidates($known_at, $horizons::INTEGER[], $tolerance::INTEGER) ON CONFLICT DO NOTHING",
                params,
            )
            inserted = self.row_count("outcomes") - before
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        return inserted

    def outcomes(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM outcomes_known_at(?) ORDER BY subject_kind, subject_id, horizon_sessions",
            [self._known(known_at)],
        )

    def scheduled_outcomes(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM scheduled_outcomes(?) ORDER BY subject_kind, subject_id, horizon_sessions",
            [self._known(known_at)],
        )

    def scheduled_outcome_summary(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        """Per (subject_kind, horizon): subject count, scored count, notional, dollar P&L and the
        notional-weighted return. Unscored rows count as subjects, never as a 0% return; with
        nothing scored, P&L and return are None."""
        return self._rows(
            "SELECT subject_kind, horizon_sessions, count(*) AS n_subjects, "
            "count(*) FILTER (WHERE status = 'scored') AS n_scored, sum(notional) AS notional, "
            "sum(notional) FILTER (WHERE status = 'scored') AS scored_notional, "
            "sum(notional * return_pct) FILTER (WHERE status = 'scored') AS dollar_pnl, "
            "sum(notional * return_pct) FILTER (WHERE status = 'scored') "
            "  / sum(notional) FILTER (WHERE status = 'scored') AS weighted_return "
            "FROM scheduled_outcomes(?) GROUP BY subject_kind, horizon_sessions "
            "ORDER BY subject_kind, horizon_sessions",
            [self._known(known_at)],
        )

    # --- accounts and tax lots -----------------------------------------------------------------

    def ingest_accounts(self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime) -> int:
        """Accounts by ALIAS only (``accounts.account_alias``). Any raw identifier -- as the alias,
        under another key, or inside any text -- rejects the whole batch."""
        to_utc_naive(as_of)
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="accounts", index=i, required={"account_alias", "account_kind"},
                     optional=frozenset({"label"}))
            guard_text_fields(raw, where=r.where)
            out = {
                "account_alias": require_alias(raw.get("account_alias"), where=r.where),
                "account_kind": r.text("account_kind"),
                "label": r.text("label", optional=True),
                "alias_scheme": ALIAS_SCHEME,
            }
            problems += r.problems
            out["content_hash"] = _hash([out[k] for k in sorted(out)])
            prepared.append(out)
        _raise_if(problems, "accounts")
        return self._insert("accounts", prepared, source=source, run_id=run_id, as_of=as_of)

    def accounts(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT account_alias, account_kind, label, alias_scheme FROM ("
            "SELECT *, row_number() OVER (PARTITION BY account_alias ORDER BY as_of DESC, ingested_at DESC, "
            "content_hash) AS rn FROM accounts WHERE as_of <= ?) WHERE rn = 1 ORDER BY account_alias",
            [self._known(known_at)],
        )

    _LOT_KEYS = {"account_alias", "symbol", "lot_id", "acquired_date", "snapshot_date", "quantity",
                 "cost_basis_per_share"}

    def ingest_tax_lots(self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime) -> int:
        """Read-only tax-lot snapshots across accounts, keyed by alias. The broker's lot id is
        stored only as ``lot_key`` (a SHA-256 prefix), so a lot that moves between accounts
        stays traceable without the raw id reaching the table."""
        to_utc_naive(as_of)
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="tax_lots", index=i, required=self._LOT_KEYS)
            guard_text_fields(raw, where=r.where)
            lot_id = r.text("lot_id")
            out = {
                "account_alias": require_alias(raw.get("account_alias"), where=r.where),
                "symbol": r.text("symbol"),
                "lot_key": hashlib.sha256(f"lot:{lot_id}".encode()).hexdigest()[:32] if lot_id else None,
                "acquired_date": r.day("acquired_date"),
                "snapshot_date": r.day("snapshot_date"),
                "quantity": r.number("quantity", positive=True),
                "cost_basis_per_share": r.number("cost_basis_per_share"),
            }
            problems += r.problems
            out["content_hash"] = _hash([out[k] for k in sorted(out)])
            prepared.append(out)
        _raise_if(problems, "tax_lots")
        return self._insert("tax_lots", prepared, source=source, run_id=run_id, as_of=as_of)

    def tax_lots(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY account_alias, lot_key, "
            "snapshot_date ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn FROM tax_lots "
            "WHERE as_of <= ?) WHERE rn = 1 ORDER BY snapshot_date, account_alias, lot_key",
            [self._known(known_at)],
        )

    # --- trials ------------------------------------------------------------------------------

    _TRIAL_KEYS = {"trial_id", "experiment", "config", "metrics", "n_observations", "evaluated_at"}

    def ingest_trials(self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime) -> int:
        """Every evaluated configuration with its metrics. ``promotion_threshold`` is recorded
        from the proposed default; nothing here promotes anything."""
        to_utc_naive(as_of)
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="trials", index=i, required=self._TRIAL_KEYS)
            config, metrics = raw.get("config"), raw.get("metrics")
            if not isinstance(config, dict):
                r.problems.append(f"{r.where}: config must be a dict")
            if not isinstance(metrics, dict) or not all(
                isinstance(k, str) and _finite(v) for k, v in (metrics or {}).items()
            ):
                r.problems.append(f"{r.where}: metrics must map names to finite numbers")
            n_obs = raw.get("n_observations")
            if not isinstance(n_obs, int) or isinstance(n_obs, bool) or n_obs < 0:
                r.problems.append(f"{r.where}: n_observations must be a non-negative int")
            out: dict[str, Any] = {
                "trial_id": r.text("trial_id"),
                "experiment": r.text("experiment"),
                "n_observations": n_obs,
                "evaluated_at": r.moment("evaluated_at"),
                "promotion_threshold": PROMOTION_DSR_THRESHOLD,
            }
            if not r.problems:
                out["config_json"] = _canonical_json(config, where=r.where, field="config")
                out["config_hash"] = hashlib.sha256(out["config_json"].encode()).hexdigest()
                out["metrics_json"] = _canonical_json(metrics, where=r.where, field="metrics")
                out["sharpe"] = float(metrics["sharpe"]) if "sharpe" in metrics else None
                out["content_hash"] = _hash([out[k] for k in sorted(out)])
            problems += r.problems
            prepared.append(out)
        _raise_if(problems, "trials")
        return self._insert("trials", prepared, source=source, run_id=run_id, as_of=as_of)

    def trials(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY trial_id ORDER BY as_of DESC, "
            "ingested_at DESC, content_hash) AS rn FROM trials WHERE as_of <= ?) WHERE rn = 1 ORDER BY trial_id",
            [self._known(known_at)],
        )
        for row in rows:
            row["config"] = json.loads(row.pop("config_json"))
            row["metrics"] = json.loads(row.pop("metrics_json"))
        return rows

    def trial_count(self, *, experiment: str | None = None, known_at: datetime | None = None) -> int:
        """Trials ever recorded (distinct trial ids), optionally for one experiment."""
        row = self.con.execute(
            "SELECT count(DISTINCT trial_id) FROM trials WHERE as_of <= ? AND (? IS NULL OR experiment = ?)",
            [self._known(known_at), experiment, experiment],
        ).fetchone()
        return int(row[0]) if row else 0

    def trial_counts(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM trial_counts(?) ORDER BY experiment", [self._known(known_at)])

    # --- universe ----------------------------------------------------------------------------

    _SCORE_KEYS = {"as_of_date", "ticker", "sector_sic", "momentum", "profitability", "low_vol", "value",
                   "composite", "rank", "source_quote_date", "source_filed_date"}

    def ingest_universe_scores(
        self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime
    ) -> int:
        """Per-ticker factor scores as of a date. Refuses any input dated after ``as_of_date``
        (a quote or filing from the future is lookahead). A None composite means unscored."""
        to_utc_naive(as_of)
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="universe_scores", index=i, required=self._SCORE_KEYS)
            out: dict[str, Any] = {
                "as_of_date": r.day("as_of_date"),
                "ticker": r.text("ticker"),
                "sector_sic": r.text("sector_sic", optional=True),
                **{k: r.signed(k) for k in ("momentum", "profitability", "low_vol", "value", "composite")},
                "rank": raw.get("rank"),
                "source_quote_date": r.day("source_quote_date"),
                "source_filed_date": r.day("source_filed_date", optional=True),
            }
            if out["rank"] is not None and (not isinstance(out["rank"], int) or isinstance(out["rank"], bool)
                                            or out["rank"] < 1):
                r.problems.append(f"{r.where}: rank must be a positive int or None")
            for field in ("source_quote_date", "source_filed_date"):
                if out["as_of_date"] and out[field] and out[field] > out["as_of_date"]:
                    r.problems.append(f"{r.where}: {field} is after as_of_date (lookahead)")
            problems += r.problems
            out["content_hash"] = _hash([out[k] for k in sorted(out)])
            prepared.append(out)
        _raise_if(problems, "universe_scores")
        return self._insert("universe_scores", prepared, source=source, run_id=run_id, as_of=as_of)

    def universe_scores(self, *, as_of_date: date, known_at: datetime | None = None) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY as_of_date, ticker "
            "ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn FROM universe_scores WHERE as_of <= ?) "
            'WHERE rn = 1 AND as_of_date = ? ORDER BY "rank" NULLS LAST, ticker',
            [self._known(known_at), as_of_date],
        )

    _PROPOSAL_KEYS = {"review_id", "as_of_date", "adds", "drops", "reasons", "safeguard_results", "status"}

    def ingest_universe_proposals(
        self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime
    ) -> int:
        """Membership proposals. Approval is the operator's act, so 'approved' is not a status."""
        to_utc_naive(as_of)
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="universe_proposals", index=i, required=self._PROPOSAL_KEYS)
            out: dict[str, Any] = {
                "review_id": r.text("review_id"),
                "as_of_date": r.day("as_of_date"),
                "status": r.choice("status", sorted(PROPOSAL_STATUSES)),
            }
            for field in ("adds", "drops"):
                value = raw.get(field)
                if not isinstance(value, list) or not all(isinstance(t, str) and t.strip() for t in value):
                    r.problems.append(f"{r.where}: {field} must be a list of tickers")
                out[field] = value
            for field in ("reasons", "safeguard_results"):
                if not isinstance(raw.get(field), dict):
                    r.problems.append(f"{r.where}: {field} must be a dict")
            if not r.problems:
                out["reasons_json"] = _canonical_json(raw["reasons"], where=r.where, field="reasons")
                out["safeguard_results_json"] = _canonical_json(
                    raw["safeguard_results"], where=r.where, field="safeguard_results"
                )
                out["content_hash"] = _hash([out[k] for k in sorted(out)])
            problems += r.problems
            prepared.append(out)
        _raise_if(problems, "universe_proposals")
        return self._insert("universe_proposals", prepared, source=source, run_id=run_id, as_of=as_of)

    def universe_proposals(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY review_id ORDER BY as_of DESC, "
            "ingested_at DESC, content_hash) AS rn FROM universe_proposals WHERE as_of <= ?) WHERE rn = 1 "
            "ORDER BY as_of_date, review_id",
            [self._known(known_at)],
        )
        for row in rows:
            row["reasons"] = json.loads(row.pop("reasons_json"))
            row["safeguard_results"] = json.loads(row.pop("safeguard_results_json"))
        return rows

    _MEMBER_KEYS = {"ticker", "effective_from", "effective_to", "approval_id"}

    def ingest_universe_membership(
        self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime
    ) -> int:
        """Dated membership intervals ``[effective_from, effective_to)``. Ending a membership
        appends a new version of the same (ticker, effective_from) row with ``effective_to``."""
        to_utc_naive(as_of)
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="universe_membership", index=i, required=self._MEMBER_KEYS)
            out = {
                "ticker": r.text("ticker"),
                "effective_from": r.day("effective_from"),
                "effective_to": r.day("effective_to", optional=True),
                "approval_id": r.text("approval_id"),
            }
            if out["effective_from"] and out["effective_to"] and out["effective_to"] <= out["effective_from"]:
                r.problems.append(f"{r.where}: effective_to must be after effective_from")
            problems += r.problems
            out["content_hash"] = _hash([out[k] for k in sorted(out)])
            prepared.append(out)
        _raise_if(problems, "universe_membership")
        return self._insert("universe_membership", prepared, source=source, run_id=run_id, as_of=as_of)

    def members_as_of(self, as_of_date: date, *, known_at: datetime | None = None) -> list[str]:
        """Tickers that were members on ``as_of_date``, as known at ``known_at``."""
        rows = self.con.execute(
            "SELECT DISTINCT ticker FROM universe_members_as_of(?, ?) ORDER BY ticker",
            [as_of_date, self._known(known_at)],
        ).fetchall()
        return [r[0] for r in rows]

    # --- goal versions (docs/adr-goals-and-loss-hedge-shadow.md, Phase 2) ---------------------
    # Bitemporal on purpose: ``as_of`` is when the goal version took effect in the config, and
    # ``ingested_at`` is when this ledger learned it. A read at ``known_at`` only sees rows that
    # were both asserted and ingested by then, so a later correction never rewrites history.

    _GOAL_KEYS = {"goal_id", "goal_version", "effective_from"}
    _GOAL_OPTIONAL = frozenset({
        "kind", "metric", "horizon_years", "target", "benchmark",
        "mode", "max_drawdown", "hedge_budget_pct", "instruments",
    })
    _GOAL_KINDS = ("aspirational", "loss_hedge")

    def ingest_goal_versions(self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime) -> int:
        """One row per goal version. ``kind`` defaults to ``aspirational``; the full entry is kept
        as canonical JSON so a hedge goal's fields survive even though only the key columns are typed."""
        to_utc_naive(as_of)
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="goal_versions", index=i, required=self._GOAL_KEYS, optional=self._GOAL_OPTIONAL)
            version = raw.get("goal_version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                r.problems.append(f"{r.where}: goal_version must be a positive int")
            kind = raw.get("kind", "aspirational")
            if kind not in self._GOAL_KINDS:
                r.problems.append(f"{r.where}: kind must be one of {list(self._GOAL_KINDS)}")
            target = raw.get("target")
            if target is not None and not _finite(target):
                r.problems.append(f"{r.where}: target must be a finite number or absent")
            effective = r.moment("effective_from")
            out: dict[str, Any] = {
                "goal_id": r.text("goal_id"),
                "goal_version": version,
                "kind": kind,
                "metric": raw.get("metric"),
                "horizon_years": raw.get("horizon_years"),
                "target": target,
                "effective_from": effective,  # r.moment() already returns naive UTC
            }
            if not r.problems:
                entry = {**raw, "effective_from": effective.isoformat()}
                out["goal_json"] = _canonical_json(entry, where=r.where, field="goal")
                out["content_hash"] = _hash([out[k] for k in sorted(out)])
            problems += r.problems
            prepared.append(out)
        _raise_if(problems, "goal_versions")
        return self._insert("goal_versions", prepared, source=source, run_id=run_id, as_of=as_of)

    def goal_versions(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        """Latest version of each (goal_id, goal_version) asserted AND ingested by ``known_at``."""
        known = self._known(known_at)
        return self._rows(
            "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY goal_id, goal_version "
            "ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn FROM goal_versions "
            "WHERE as_of <= ? AND ingested_at <= ?) WHERE rn = 1 ORDER BY goal_id, goal_version",
            [known, known],
        )

    def goal_version_on(
        self, day: date, *, kind: str = "aspirational", known_at: datetime | None = None
    ) -> dict[str, Any] | None:
        """The version of ``kind`` in effect on ``day`` (ET calendar day), as known at ``known_at``.
        Latest ``effective_from`` wins; None when nothing has taken effect yet."""
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo("America/New_York")
        in_effect = []
        for row in self.goal_versions(known_at=known_at):
            if row["kind"] != kind:
                continue
            effective_day = row["effective_from"].replace(tzinfo=timezone.utc).astimezone(eastern).date()
            if effective_day <= day:
                in_effect.append(row)
        return max(in_effect, key=lambda row: row["effective_from"], default=None)

    # --- portfolio snapshots and goal progress (ADR Phase 2, tests 19-21) ---------------------
    # Same store rules as every other ledger table. Two differences, both deliberate:
    # * Day fields accept a date OR an ISO "YYYY-MM-DD" string, because the 07:31 capture
    #   writes them straight from a JSON payload; anything else is refused.
    # * Readers are bitemporal like goal_versions (as_of AND ingested_at <= known_at) and break
    #   a full as_of/ingested_at tie by ingest_seq, so a same-close correction always wins.

    _SNAPSHOT_KEYS = {"snapshot_day", "symbol", "quantity", "close_day", "prior_close", "market_value"}
    # market_value is the broker's own figure; it must agree with quantity x prior_close to
    # within this many dollars, or the row is a mis-keyed snapshot (wrong close or quantity).
    _SNAPSHOT_VALUE_TOLERANCE = 0.01

    @staticmethod
    def _aware_as_of(as_of: object, table: str) -> None:
        """Refuse a naive ``as_of`` as a validation failure (to_utc_naive raises a bare ValueError)."""
        if not isinstance(as_of, datetime) or as_of.tzinfo is None:
            raise LedgerValidationError(f"{table}: as_of must be a timezone-aware datetime")

    @staticmethod
    def _iso_day(r: _Row, key: str) -> date | None:
        value = r.row.get(key)
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value) if len(value) == 10 else None
            except ValueError:
                value = None
        if not isinstance(value, date) or isinstance(value, datetime):
            r.problems.append(f"{r.where}: {key} must be a date or an ISO YYYY-MM-DD string")
            return None
        return value

    def ingest_portfolio_snapshots(
        self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime
    ) -> int:
        """Read-only per-symbol holdings at a prior close. No account field of any kind: the
        snapshot is of the one strategy account, and an account-named key is refused outright."""
        self._aware_as_of(as_of, "portfolio_snapshots")
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="portfolio_snapshots", index=i, required=self._SNAPSHOT_KEYS)
            guard_account_keys(raw, where=r.where)
            out: dict[str, Any] = {
                "snapshot_day": self._iso_day(r, "snapshot_day"),
                "symbol": r.text("symbol"),
                "quantity": r.number("quantity", positive=True),
                "close_day": self._iso_day(r, "close_day"),
                "prior_close": r.number("prior_close", positive=True),
                "market_value": r.number("market_value"),
            }
            if out["snapshot_day"] and out["close_day"] and out["close_day"] > out["snapshot_day"]:
                r.problems.append(f"{r.where}: close_day is after snapshot_day (lookahead)")
            if None not in (out["quantity"], out["prior_close"], out["market_value"]) and abs(
                out["quantity"] * out["prior_close"] - out["market_value"]
            ) > self._SNAPSHOT_VALUE_TOLERANCE:
                r.problems.append(f"{r.where}: market_value disagrees with quantity x prior_close")
            problems += r.problems
            out["content_hash"] = _hash([out[k] for k in sorted(out)])
            prepared.append(out)
        _raise_if(problems, "portfolio_snapshots")
        return self._insert("portfolio_snapshots", prepared, source=source, run_id=run_id, as_of=as_of)

    def portfolio_snapshots(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        """Latest version of each (snapshot_day, symbol) asserted AND ingested by ``known_at``."""
        known = self._known(known_at)
        return self._rows(
            "SELECT * EXCLUDE (rn, ingest_seq) FROM (SELECT *, row_number() OVER (PARTITION BY snapshot_day, "
            "symbol ORDER BY as_of DESC, ingested_at DESC, ingest_seq DESC) AS rn FROM portfolio_snapshots "
            "WHERE as_of <= ? AND ingested_at <= ?) WHERE rn = 1 ORDER BY snapshot_day, symbol",
            [known, known],
        )

    _PROGRESS_KEYS = {"day", "goal_id", "goal_version", "window_days", "period_return",
                      "annualized_return", "mwr_annualized"}
    # Annualizing a return over less than a year extrapolates noise (a 10% fortnight reads as
    # several thousand percent a year), so under this window both annualized fields must be None.
    _ANNUALIZE_MIN_DAYS = 365

    def ingest_goal_progress(
        self, rows: Iterable[dict[str, Any]], *, source: str, run_id: str, as_of: datetime
    ) -> int:
        """Daily progress against one goal version. ``period_return`` is a fraction (0.10 = 10%)."""
        self._aware_as_of(as_of, "goal_progress")
        problems: list[str] = []
        prepared = []
        for i, raw in enumerate(rows):
            r = _Row(raw, kind="goal_progress", index=i, required=self._PROGRESS_KEYS)
            guard_account_keys(raw, where=r.where)
            for key in ("goal_version", "window_days"):
                value = raw.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    r.problems.append(f"{r.where}: {key} must be a positive int")
            period = raw.get("period_return")
            if not _finite(period) or period < -1:
                r.problems.append(f"{r.where}: period_return must be a finite fraction >= -1")
            out: dict[str, Any] = {
                "day": self._iso_day(r, "day"),
                "goal_id": r.text("goal_id"),
                "goal_version": raw.get("goal_version"),
                "window_days": raw.get("window_days"),
                "period_return": float(period) if _finite(period) else None,
                "annualized_return": r.signed("annualized_return"),
                "mwr_annualized": r.signed("mwr_annualized"),
            }
            window = out["window_days"]
            if isinstance(window, int) and window < self._ANNUALIZE_MIN_DAYS:
                for key in ("annualized_return", "mwr_annualized"):
                    if raw.get(key) is not None:
                        r.problems.append(
                            f"{r.where}: {key} must be None under a {self._ANNUALIZE_MIN_DAYS}-day window"
                        )
            problems += r.problems
            out["content_hash"] = _hash([out[k] for k in sorted(out)])
            prepared.append(out)
        _raise_if(problems, "goal_progress")
        return self._insert("goal_progress", prepared, source=source, run_id=run_id, as_of=as_of)

    def goal_progress(self, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
        """Latest version of each (day, goal_id, goal_version) asserted AND ingested by ``known_at``."""
        known = self._known(known_at)
        return self._rows(
            'SELECT * EXCLUDE (rn, ingest_seq) FROM (SELECT *, row_number() OVER (PARTITION BY "day", goal_id, '
            "goal_version ORDER BY as_of DESC, ingested_at DESC, ingest_seq DESC) AS rn FROM goal_progress "
            'WHERE as_of <= ? AND ingested_at <= ?) WHERE rn = 1 ORDER BY "day", goal_id, goal_version',
            [known, known],
        )
