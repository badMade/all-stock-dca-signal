"""State & ledgers page readers: durable state read WITHOUT any lock.

GUI expansion Phase 3. Every builder here reads the files the live system
writes -- ``strategy_state.json``, the three ledgers, run states, the
maintenance event logs -- and never the classes that own them. The owning
classes (``CumulativeCreditLedger``, ``WholeShareAccumulationLedger``,
``InverseMartingaleLedger``, ``dca_runner.state_transaction``) take a
cross-process lock even on a read, and a console that blocked or delayed a
scheduled 3:30 PM run over a browser refresh would be a live-money bug
(adversarial pass, refutation on ``dca_cumulative.pending_usd``). So this
module is read-only and lock-free by construction: a ledger ``.lock`` file is
reported as present or absent and never opened.

Three states are kept apart everywhere: ``absent`` (no file), ``corrupt``
(a file that cannot be trusted -- an alarm, because the live loaders fail
closed on it) and ``available``. ``None`` in any figure means unavailable,
never zero. Roots are named by their ``RootSpec.label``; a file is identified
by ``"run-" + sha256("<label>:<relative path>")[:12]``, the same shape
``dca_console_data._run_row`` mints, so no absolute path, order id or account
number can reach the browser.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Sequence
from datetime import date, datetime
from itertools import islice
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dca_console_jobs import redact_value
from dca_console_reader import MAX_BYTES, BoundedReader, RootSpec, iso_date, root_label

ET = ZoneInfo("America/New_York")
MAX_TEXT = 500
MAX_ROWS = 200
MAX_PURCHASES = 500
# A torn write: dca_runner.save_strategy_state writes a temp file and
# os.replace()s it, so a reader can see a half-written document for a moment.
# One retry after this pause separates that from real corruption.
TORN_WRITE_RETRY_SECONDS = 0.25
# The whole-share ledger's own symbol rule (dca_whole_share_accumulator
# _SYMBOL_PATTERN) is the strictest of the three; a symbol is a path component
# below, so nothing looser may pass.
SYMBOL = re.compile(r"^[A-Z][A-Z.]{0,5}$")
LEDGER_KINDS = ("cumulative", "whole_share", "inverse_martingale")
# Directory names as each ledger module passes them to dca_paths.stream_root;
# the primary RootSpec IS the cron-tracking root, so they are relative to it.
LEDGER_DIRS = {
    "cumulative": "cumulative_dca_credits",
    "whole_share": "whole_share_accumulation",
    "inverse_martingale": "inverse_martingale_dca",
}
CUMULATIVE_EVENT_KEYS = (
    "ticker",
    "stream",
    "recorded_at",
    "final_session",
    "pending_before_usd",
    "incremental_credit_usd",
    "order_amount_usd",
    "pending_after_usd",
    "decision",
)
WHOLE_SHARE_EVENT_KEYS = (
    "trading_day",
    "stream",
    "recorded_at",
    "pending_before_usd",
    "contribution_usd",
    "current_share_price_usd",
    "shares_to_buy",
    "order_notional_usd",
    "pending_after_usd",
    "shares_purchased",
    "submitted_notional_usd",
    "restored_shortfall_usd",
    "shares_purchased_total",
    "decision",
)
# settlement_ref_id (a broker order id) and settlement_account are deliberately
# absent from this tracker allowlist.
TRACKER_KEYS = (
    "status",
    "accumulated_usd",
    "cycle_start_trading_day",
    "cycle_reference_close",
    "lowest_drop_seen",
    "crossed_tier_thresholds",
    "trading_days_active",
    "fire_attempts",
    "last_evaluated_trading_day",
    "last_evaluated_stream",
    "last_evaluated_is_real",
    "settlement_pending",
    "settlement_trading_day",
    "settlement_stream",
    "settlement_live",
    "settlement_claimed_usd",
    "settlement_target_state",
    "stddev_reference",
    "basis",
    "fired_floor_price",
    "fired_floor_drop_metric",
    "rearm_reference_close",
    "rearm_last_seen_day",
)
TRACKER_EVENT_KEYS = (
    "status_before",
    "status_after",
    "accumulated_usd_before",
    "accumulated_usd_after",
    "newly_crossed_tiers",
    "fired",
    "fire_amount_usd",
    "reason",
)
# strategy_state.json keys forwarded verbatim (dca_runner._state_to_json). The
# order-id, placement-ref and recovered-run-id maps are never forwarded; their
# names show up under "omitted_keys" so the operator knows the allowlist hid them.
STATE_SCALAR_KEYS = ("schema_version", "weekly_rollover_reserve_usd", "reserve_week_key", "selected_tier")
# dca_runner._default_state_path() is stream_root("dca_runner") / "strategy_state.json".
STATE_DIR, STATE_FILE = "dca_runner", "strategy_state.json"
STATE_DATED_KEYS = ("submitted_notional_by_day", "submitted_order_count_by_day")
# Evidence counts the live kill switch requires before it trusts a zero
# (dca_risk._load_states_with_evidence; plan "Server contract" correction 2).
EVIDENCE_KEYS = (
    "state_root_exists",
    "run_state_files_found",
    "run_states_loaded",
    "run_states_unreadable",
    "run_states_unclassifiable",
    "afternoon_states_seen",
    "afternoon_states_undated",
    "afternoon_states_invalid_notional",
    "unusable_states_outside_window",
    "unclassifiable_states_without_deployment",
)
EVIDENCE_CHECKS = (
    ("state_root_exists", "is true"),
    ("run_states_loaded", "> 0"),
    ("run_states_unreadable", "== 0"),
    ("run_states_unclassifiable", "== 0"),
    ("afternoon_states_seen", "> 0"),
    ("afternoon_states_undated", "== 0"),
    ("afternoon_states_invalid_notional", "== 0"),
)
MAINTENANCE = {
    # (directory under the root, scalar allowlist). List-valued keys become
    # ``<key>_count``; ``history_dir`` / ``upstream_ticker_update_event`` /
    # ``output`` hold paths in the real files and are never forwarded.
    "ticker_history": (
        "ticker_history_update",
        (
            "event_type",
            "status",
            "completed_at",
            "invocations",
            "updated_total",
            "remaining",
            "execution_owner",
            "version",
        ),
    ),
    "calibration": (
        "per_ticker_calibration",
        (
            "status",
            "run_at",
            "completed_at",
            "calibrated_tickers",
            "trigger_source",
            "upstream_status",
            "execution_owner",
            "version",
        ),
    ),
}
COUNTED_LIST_KEYS = ("errors", "changed_tickers", "changed_active_tickers", "fallback_tickers", "triggering_tickers")
# Fields that must be timestamps: forwarded only when they parse as one, else
# None. Stricter than the bounded-text rule on purpose -- a stamp is the one
# free-text slot every ledger and event log shares, so it is where a path or
# an order reference seeded by a foreign writer would otherwise ride through.
TIMESTAMP_KEYS = ("recorded_at", "claimed_at", "completed_at", "run_at")


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return redact_value(str(value)[:limit])


def _finite(value: Any) -> float | None:
    """A finite number, or None; bools are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Bound and redact one artifact value into JSON-safe types (None when unsupported)."""
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if depth >= 6:
        return None
    if isinstance(value, dict):
        return {_text(key, 80): _jsonable(item, depth + 1) for key, item in islice(value.items(), MAX_ROWS)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item, depth + 1) for item in islice(value, MAX_ROWS)]
    return None


def _stamp(value: Any) -> str | None:
    """An ISO-8601 timestamp string as written, or None for anything that is not one."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _pick(obj: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: (_stamp(obj[key]) if key in TIMESTAMP_KEYS else _jsonable(obj[key])) for key in keys if key in obj}


def _identity(root: RootSpec, relative: Path | str) -> str:
    return "run-" + hashlib.sha256(f"{root.label}:{relative}".encode()).hexdigest()[:12]


def _dedup(warnings: list[str]) -> list[str]:
    return list(dict.fromkeys(warnings))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # json.loads keeps the LAST duplicate silently; the live loader refuses
    # such a document (dca_runner._reject_duplicate_keys), so this view must
    # show it as corrupt rather than as whichever value happened to win.
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _raw_bytes(path: Path) -> bytes | None:
    """Read one file's bytes up to the shared ceiling; None when absent."""
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    return None if len(data) > MAX_BYTES else data


def _parse_state(path: Path) -> tuple[str, dict[str, Any] | None]:
    try:
        data = _raw_bytes(path)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "corrupt", None
    if data is None:
        return "corrupt", None
    try:
        parsed = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, UnicodeError, RecursionError):
        return "corrupt", None
    return ("available", parsed) if isinstance(parsed, dict) else ("corrupt", None)


def _dated_numbers(mapping: Any) -> dict[str, float | None] | None:
    if not isinstance(mapping, dict):
        return None
    rows: list[tuple[str, float | None]] = []
    for raw_key, raw_val in mapping.items():
        date_key = iso_date(raw_key)
        if date_key is not None:
            rows.append((date_key, _finite(raw_val)))
    rows.sort(key=lambda r: r[0], reverse=True)
    return {key: value for key, value in rows[:MAX_ROWS]}


def _symbol_rows(mapping: dict[str, Any], value_of, ticker: str | None = None) -> tuple[dict[str, Any], int]:
    """Allowlisted, bounded symbol->value rows plus the number of keys DROPPED.

    dca_cumulative and dca_runner apply no symbol pattern on write (every site
    is ``ticker.strip().upper()``), so a stored key like ``BRK-B`` or a
    seven-character symbol is legal on disk yet unsafe as a path component
    here. Such a key is left out of the rows, but the caller must know it was
    -- a sum over the survivors is not the balance the file records
    (adversarial review round 2: silent drops turned real credit into $0.00).
    A ``ticker`` filter is not a drop: the operator asked for that subset.
    """
    rows: dict[str, Any] = {}
    dropped = 0
    for symbol, value in sorted(mapping.items(), key=lambda item: item[0]):
        if not isinstance(symbol, str) or not SYMBOL.match(symbol):
            dropped += 1
        elif ticker not in (None, symbol):
            continue
        elif len(rows) >= MAX_ROWS:
            dropped += 1
        else:
            rows[symbol] = value_of(value)
    return rows, dropped


def _state_fields(raw: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    fields: dict[str, Any] = _pick(raw, STATE_SCALAR_KEYS)
    for key in STATE_DATED_KEYS:
        fields[key] = _dated_numbers(raw.get(key))
    weeks = raw.get("submitted_notional_by_week")
    fields["submitted_notional_by_week"] = (
        {_text(key, 20): _finite(value) for key, value in islice(weeks.items(), MAX_ROWS)}
        if isinstance(weeks, dict)
        else None
    )
    days = raw.get("finalized_budget_days")
    fields["finalized_budget_days"] = (
        sorted({d for day in days for d in (iso_date(day),) if d is not None}, reverse=True)[:MAX_ROWS]
        if isinstance(days, list)
        else None
    )
    buys = raw.get("buys_by_ticker_by_day")
    fields["buys_by_ticker_by_day"] = None
    if isinstance(buys, dict):
        fields["buys_by_ticker_by_day"], dropped = _symbol_rows(buys, _dated_numbers)
        if dropped:
            warnings.append(
                f"{dropped} ticker key(s) in buys_by_ticker_by_day were hidden "
                "(not a displayable symbol, or past the row limit); the grid is incomplete."
            )
    shown = (
        set(STATE_SCALAR_KEYS)
        | set(STATE_DATED_KEYS)
        | {"submitted_notional_by_week", "finalized_budget_days", "buys_by_ticker_by_day"}
    )
    fields["omitted_keys"] = sorted(_text(key, 80) for key in raw if key not in shown)[:MAX_ROWS]
    return fields


def default_state_path(roots: Sequence[RootSpec]) -> Path:
    """Where ``strategy_state.json`` lives for these roots, without reading the environment.

    ``dca_runner.save_strategy_state`` writes ``stream_root("dca_runner") /
    strategy_state.json``, and the primary RootSpec IS that cron-tracking
    root, so the same file sits at ``<primary>/dca_runner/strategy_state.json``.
    Resolving it from the root rather than from ``$DCA_CRON_TRACKING_ROOT``
    keeps the state card on the same tree as every other panel: a demo or
    ``--root`` console used to show production's budget figures beside demo
    ledgers (adversarial review round 2). ``dca_runner._default_state_path``
    remains the fallback only when no root is configured at all.
    """
    if roots:
        return Path(roots[0].path) / STATE_DIR / STATE_FILE
    from dca_runner import _default_state_path  # call-time: reads the env through dca_paths

    return _default_state_path()


def strategy_state_view(state_path: Path | None = None, *, roots: Sequence[RootSpec] = ()) -> dict[str, Any]:
    """Read ``strategy_state.json`` raw: no lock, no ``load_strategy_state``.

    ``status`` is ``absent`` (no file: a fresh state), ``corrupt`` (unreadable,
    oversized, invalid JSON, duplicate keys or not an object -- the alarm state,
    because the live loader refuses to run on it) or ``available``. ``fields``
    holds the allowlisted budget/buy-count keys with ``None`` for any value that
    is missing or non-finite; ``lock_file_present`` reports the sibling
    ``.lock`` without ever opening it. Without ``state_path`` the file is
    ``default_state_path(roots)``: under the primary root when one is given,
    else ``dca_runner._default_state_path()`` resolved at call time.
    ``path_label`` names the file without its path.
    """
    warnings: list[str] = []
    path = Path(state_path) if state_path is not None else default_state_path(roots)
    status, raw = _parse_state(path)
    if status == "corrupt":
        # A torn write is indistinguishable from corruption on the first read;
        # only a second failure after the replace window is an alarm.
        time.sleep(TORN_WRITE_RETRY_SECONDS)
        status, raw = _parse_state(path)
    if status == "corrupt":
        warnings.append("strategy_state.json is unreadable or corrupt; the live loader will refuse to run on it.")
    elif status == "absent":
        warnings.append("strategy_state.json is absent; the live loader would start from a fresh state.")
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=ET).isoformat() if status != "absent" else None
    except OSError:
        modified = None
    try:
        lock_present = path.with_name(path.name + ".lock").exists()
    except OSError:
        lock_present = False
    return {
        "status": status,
        "path_label": root_label(path),
        "modified": modified,
        "lock_file_present": lock_present,
        "fields": _state_fields(raw, warnings) if raw is not None else {},
        "warnings": _dedup(warnings),
    }


def _read_object(reader: BoundedReader, relative: Path) -> tuple[str, dict[str, Any] | None]:
    """(status, object) with absent / corrupt / available kept apart."""
    path = reader.confined(relative)
    if path is None or not path.is_file():
        return "absent", None
    text = reader.text(relative)
    if text is None:
        return "corrupt", None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return "corrupt", None
    return ("available", parsed) if isinstance(parsed, dict) else ("corrupt", None)


def _lock_present(reader: BoundedReader, relative: Path) -> bool:
    path = reader.confined(relative)
    try:
        return path is not None and path.exists()
    except OSError:
        return False


def _subdirs(reader: BoundedReader, directory: str, accept) -> list[str]:
    names = []
    for entry in reader.entries(directory):
        if accept(entry.name) and (reader.root / entry).is_dir():
            names.append(entry.name)
    return names


def _events(state: dict[str, Any], keys: Sequence[str], keep, extra=None) -> tuple[list[dict[str, Any]], int | None]:
    """Newest-first, bounded, allowlisted events; count None when the list is malformed."""
    raw = state.get("events")
    if not isinstance(raw, list):
        return [], None
    rows = [event for event in raw if isinstance(event, dict) and keep(event)]
    return [{**_pick(event, keys), **(extra(event) if extra else {})} for event in reversed(rows[-MAX_ROWS:])], len(
        rows
    )


def _entry(root: RootSpec, reader: BoundedReader, relative: Path, filename: str) -> tuple[dict[str, Any], dict | None]:
    status, state = _read_object(reader, relative / filename)
    # ledger.json sits beside ledger.lock, tracker.json beside tracker.lock:
    # the lock is reported by existence only and never opened.
    entry = {
        "id": _identity(root, relative),
        "root_label": root.label,
        "at_risk": root.kind != "primary",
        "status": status,
        "lock_file_present": _lock_present(reader, relative / (Path(filename).stem + ".lock")),
    }
    if status == "corrupt":
        reader.warn(f"A {root.label} ledger file is corrupt; its balances are unavailable.")
    return entry, state


def _cumulative_entry(root: RootSpec, reader: BoundedReader, day: str, ticker: str | None) -> dict[str, Any]:
    relative = Path(LEDGER_DIRS["cumulative"]) / day
    entry, state = _entry(root, reader, relative, "ledger.json")
    entry.update(
        {
            "day": day,
            "pending_usd_by_ticker": None,
            "pending_total_usd": None,
            "events": [],
            "event_count": None,
            "settled_order_count": None,
        }
    )
    if state is None:
        return entry
    if state.get("trading_day") != day:
        entry["status"] = "mismatched"
        reader.warn("A cumulative ledger records a different trading day than its directory.")
    pending = state.get("pending_usd_by_ticker")
    if isinstance(pending, dict):
        rows, dropped = _symbol_rows(pending, _finite, ticker)
        entry["pending_usd_by_ticker"] = rows
        if dropped:
            # A total over a thinned map is not the day's pending balance;
            # None says "unavailable" where a partial sum would read as $0.00.
            reader.warn(
                f"{dropped} pending-credit key(s) in a cumulative ledger were hidden "
                "(not a displayable symbol, or past the row limit); its pending total is unavailable."
            )
        elif None not in rows.values():
            entry["pending_total_usd"] = round(sum(rows.values()), 2)
    entry["events"], entry["event_count"] = _events(
        state, CUMULATIVE_EVENT_KEYS, lambda event: ticker in (None, event.get("ticker"))
    )
    settled = state.get("settled_order_ids")
    entry["settled_order_count"] = len(settled) if isinstance(settled, dict) else None
    return entry


def _whole_share_entry(root: RootSpec, reader: BoundedReader, symbol: str, day: str | None) -> dict[str, Any]:
    relative = Path(LEDGER_DIRS["whole_share"]) / symbol
    entry, state = _entry(root, reader, relative, "ledger.json")
    entry.update(
        {
            "ticker": symbol,
            "pending_usd": None,
            "shares_purchased_total": None,
            "events": [],
            "event_count": None,
            "claims": [],
            "unresolved_claim_count": None,
        }
    )
    if state is None:
        return entry
    if state.get("ticker") != symbol:
        entry["status"] = "mismatched"
        reader.warn("A whole-share ledger records a different ticker than its directory.")
    entry["pending_usd"] = _finite(state.get("pending_usd"))
    entry["shares_purchased_total"] = _finite(state.get("shares_purchased_total"))
    entry["events"], entry["event_count"] = _events(
        state, WHOLE_SHARE_EVENT_KEYS, lambda event: day in (None, event.get("trading_day"))
    )
    claims = state.get("recorded_idempotency_keys")
    if isinstance(claims, dict):
        # Claim tokens and the slot keys are never forwarded; resolution state is.
        rows = [
            {
                "resolved": claim.get("resolved") is True,
                "claimed_at": _stamp(claim.get("claimed_at")),
                "shares_credited": claim.get("shares_credited") is True,
            }
            for claim in islice(claims.values(), MAX_ROWS)
            if isinstance(claim, dict)
        ]
        entry["claims"] = rows
        entry["unresolved_claim_count"] = sum(not row["resolved"] for row in rows)
    return entry


def _tracker_entry(root: RootSpec, reader: BoundedReader, symbol: str) -> dict[str, Any]:
    relative = Path(LEDGER_DIRS["inverse_martingale"]) / symbol
    entry, state = _entry(root, reader, relative, "tracker.json")
    entry.update({"ticker": symbol, "tracker": None, "events": [], "event_count": None})
    if state is None:
        # The ledger unlinks tracker.json when a ticker's state empties, so an
        # absent file under a present directory is a resolved cycle, not damage.
        return entry
    if state.get("ticker") not in (None, symbol):
        entry["status"] = "mismatched"
        reader.warn("An inverse-martingale tracker records a different ticker than its directory.")
    entry["tracker"] = _pick(state, TRACKER_KEYS)
    # recovery_metadata is a nested blob (order refs inside); only its presence
    # is forwarded, and through _events' hook so a tracker without an "events"
    # list is still "available", not a KeyError (adversarial review round 2).
    entry["events"], entry["event_count"] = _events(
        state,
        TRACKER_EVENT_KEYS,
        lambda event: True,
        extra=lambda event: {"has_recovery_metadata": "recovery_metadata" in event},
    )
    return entry


def ledger_view(
    roots: Sequence[RootSpec], kind: str, *, ticker: str | None = None, day: str | None = None
) -> dict[str, Any]:
    """Read one ledger kind's files directly through the bounded reader.

    ``kind`` is ``cumulative`` (``cumulative_dca_credits/<day>/ledger.json``),
    ``whole_share`` (``whole_share_accumulation/<TICKER>/ledger.json``) or
    ``inverse_martingale`` (``inverse_martingale_dca/<TICKER>/tracker.json``).
    Without ``day`` the cumulative view reads each root's newest day; without
    ``ticker`` the per-symbol views read every listed symbol. Each entry
    carries ``status`` (absent / corrupt / mismatched / available),
    ``lock_file_present`` (never acquired), pending amounts (None when
    unavailable) and newest-first events with decision names verbatim.
    Invalid ``kind``/``ticker``/``day`` raise ValueError so a route can 400.
    """
    if kind not in LEDGER_KINDS:
        raise ValueError(f"Unknown ledger kind: {kind!r}")
    if ticker is not None and not SYMBOL.match(ticker):
        raise ValueError("ticker must be an upper-case symbol")
    if day is not None and iso_date(day) is None:
        raise ValueError("day must be an ISO date")
    warnings: list[str] = []
    result: dict[str, Any] = {
        "kind": kind,
        "directory": LEDGER_DIRS[kind],
        "entries": [],
        "roots": [root.label for root in roots],
    }
    listed: set[str] = set()
    for root in roots:
        reader = root.reader(warnings)
        if kind == "cumulative":
            days = sorted(_subdirs(reader, LEDGER_DIRS[kind], iso_date), reverse=True)
            listed.update(days)
            for chosen in [day] if day else days[:1]:
                result["entries"].append(_cumulative_entry(root, reader, chosen, ticker))
            continue
        symbols = sorted(_subdirs(reader, LEDGER_DIRS[kind], SYMBOL.match))
        listed.update(symbols)
        for symbol in [ticker] if ticker else symbols[:MAX_ROWS]:
            result["entries"].append(
                _whole_share_entry(root, reader, symbol, day)
                if kind == "whole_share"
                else _tracker_entry(root, reader, symbol)
            )
    result["days" if kind == "cumulative" else "tickers"] = sorted(listed, reverse=kind == "cumulative")[:MAX_ROWS]
    if not roots:
        warnings.append("No artifact root is configured; ledgers are unavailable.")
    result["warnings"] = _dedup(warnings)
    return result


def _owning_root(resolved_roots: list[tuple[RootSpec, Path]], source_dir: str) -> RootSpec | None:
    try:
        resolved = Path(source_dir).resolve()
    except (OSError, RuntimeError):
        return None
    return next((root for root, path in resolved_roots if resolved.is_relative_to(path)), None)


def purchases_view(roots: Sequence[RootSpec], *, day: str | None = None) -> dict[str, Any]:
    """Confirmed fills from ``scripts.purchase_history`` over every root.

    Records are newest first, bounded to 500, and carry no order id, run id or
    run directory: only the trading day, ticker, amount, price, quantity,
    stream, origin and owning ``root_label`` (``at_risk`` for a secondary
    root). ``rollup`` is ``build_ticker_rollup`` over the same records;
    ``summary.total_usd`` is None when any record lacks an amount. The
    reader's own evidence warnings are counted, never forwarded (they name
    paths).
    """
    from scripts.purchase_history import build_ticker_rollup, collect_purchase_records

    warnings: list[str] = []
    if day is not None and iso_date(day) is None:
        raise ValueError("day must be an ISO date")
    empty = {"records": [], "rollup": {}, "summary": {"record_count": 0, "total_usd": None, "last_trading_day": None}}
    if not roots:
        return {**empty, "warnings": ["No artifact root is configured; purchases are unavailable."]}
    try:
        records, evidence = collect_purchase_records(roots[0].path, additional_roots=[r.path for r in roots[1:]])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {**empty, "warnings": ["Confirmed purchases could not be read; spend remains unverified."]}
    if evidence:
        warnings.append(
            f"The purchase-history reader reported {len(evidence)} evidence warning(s); "
            "confirmed-fill coverage may be incomplete. Run the purchase-history CLI for details."
        )
    if day is not None:
        records = [record for record in records if record.trading_day == day]
    records.sort(key=lambda record: record.trading_day or "", reverse=True)
    resolved = [(root, root.path.resolve()) for root in roots]
    rows = []
    # ⚡ Bolt Optimization: Cache resolved owning roots instead of resolving paths per record
    owner_cache = {}
    for record in records[:MAX_PURCHASES]:
        if record.source_run_dir not in owner_cache:
            owner_cache[record.source_run_dir] = _owning_root(resolved, record.source_run_dir)
        owner = owner_cache[record.source_run_dir]
        rows.append(
            {
                "trading_day": iso_date(record.trading_day),
                "ticker": _text(record.ticker, 12),
                "amount_usd": _finite(record.amount_usd),
                "price": _finite(record.avg_fill_price),
                "quantity": _finite(record.quantity),
                "stream": _jsonable(record.stream),
                "origin": _jsonable(record.run_origin),
                "root_label": owner.label if owner else None,
                "at_risk": owner.kind != "primary" if owner else None,
            }
        )
    if len(records) > MAX_PURCHASES:
        warnings.append(f"Only the newest {MAX_PURCHASES} of {len(records)} purchase records are shown.")
    valid_amounts = [a for record in records if (a := _finite(record.amount_usd)) is not None]
    total = round(sum(valid_amounts), 2) if (len(valid_amounts) == len(records) and records) else None
    return {
        "records": rows,
        "rollup": _jsonable(build_ticker_rollup(records)),
        "summary": {
            "record_count": len(records),
            "total_usd": total,
            "last_trading_day": iso_date(records[0].trading_day) if records else None,
        },
        "warnings": _dedup(warnings),
    }


def _evidence_checks(evidence: dict[str, Any], warnings: list[str]) -> bool:
    usable = True
    for name, rule in EVIDENCE_CHECKS:
        value = evidence.get(name)
        passed = (
            value is True
            if rule == "is true"
            else isinstance(value, int) and not isinstance(value, bool) and (value > 0 if rule == "> 0" else value == 0)
        )
        if not passed:
            usable = False
            warnings.append(f"Risk evidence check failed: {name} {rule} (observed {_jsonable(value)!r}).")
    return usable


def risk_view(roots: Sequence[RootSpec], day: date, *, config_dir: Path) -> dict[str, Any]:
    """Kill-switch report over the PRIMARY root only, gated by the evidence block.

    ``usable`` is True only when every check the live sizing gate applies
    passes (root exists, states loaded, none unreadable/unclassifiable,
    afternoon evidence seen, none undated or with invalid notional); each
    failing check is named in ``warnings``. ``evidence`` carries the counts
    (never the paths ``dca_risk`` lists) or None when the loader is
    unavailable. ``report`` is ``dca_risk.build_risk_report`` made JSON-safe
    plus the window lengths and the scheduled-live deployment by day inside
    the evidence window. Secondary roots are ignored on purpose: the live kill
    switch reads the cron-tracking tree alone.
    """
    from dca_risk import (
        BURN_WINDOW_DAYS,
        EVIDENCE_WINDOW_DAYS,
        KILL_SWITCH_WINDOW_DAYS,
        STREAM_SCHEDULED,
        build_risk_report,
        deployed_by_day,
    )

    warnings: list[str] = []
    result: dict[str, Any] = {
        "usable": False,
        "evidence": None,
        "report": None,
        "scope": {
            "root_label": roots[0].label if roots else None,
            "ignored_roots": [root.label for root in roots[1:]],
            "note": "Computed over the primary root only: that is the tree the live kill switch reads.",
        },
    }
    if not roots:
        return {**result, "warnings": ["No artifact root is configured; the risk report is unavailable."]}
    try:
        from dca_risk import load_states_with_evidence
    except ImportError:
        return {**result, "warnings": ["The risk evidence loader is not available in this checkout."]}
    try:
        states, evidence = load_states_with_evidence(roots[0].path, as_of=day)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {**result, "warnings": ["Run states could not be read; the risk report is unavailable."]}
    result["evidence"] = {key: _jsonable(evidence.get(key)) for key in EVIDENCE_KEYS}
    result["usable"] = _evidence_checks(evidence, warnings)
    try:
        from dca_config import load_strategy_config

        config = load_strategy_config(config_dir)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        config = None
        warnings.append("The strategy config could not be loaded; thresholds use built-in defaults.")
    try:
        report = _jsonable(build_risk_report(states, day, config))
        by_day = deployed_by_day(states, stream=STREAM_SCHEDULED)
    except (ValueError, TypeError, KeyError, OverflowError):
        return {**result, "warnings": _dedup(warnings + ["The risk report could not be computed."])}
    start = day.toordinal() - (EVIDENCE_WINDOW_DAYS - 1)
    report["window_days"] = {
        "burn": BURN_WINDOW_DAYS,
        "kill_switch": KILL_SWITCH_WINDOW_DAYS,
        "evidence": EVIDENCE_WINDOW_DAYS,
    }
    report["scheduled_deployed_by_day"] = {
        when.isoformat(): _finite(amount)
        for when, amount in sorted(by_day.items(), reverse=True)
        if start <= when.toordinal() <= day.toordinal()
    }
    result["report"] = report
    return {**result, "warnings": _dedup(warnings)}


def _maintenance_row(root: RootSpec, reader: BoundedReader, relative: Path, scalars: Sequence[str]) -> dict[str, Any]:
    status, event = _read_object(reader, relative)
    row: dict[str, Any] = {
        "id": _identity(root, relative),
        "root_label": root.label,
        "at_risk": root.kind != "primary",
        "day": relative.parent.name,
        "file": relative.name,
        "file_status": status,
    }
    if status == "corrupt":
        # _read_object is silent by design (absent is common); a maintenance
        # event that exists but cannot be parsed is not, so name it here.
        reader.warn(f"A {root.label} maintenance event file is corrupt; its status is unavailable.")
    if event is None:
        return row
    row.update(_pick(event, scalars))
    for key in COUNTED_LIST_KEYS:
        if key in event:
            row[f"{key}_count"] = len(event[key]) if isinstance(event[key], list) else None
    breadth = event.get("breadth_fetch")
    if isinstance(breadth, dict):
        row["breadth_status"] = _jsonable(breadth.get("status"))
    return row


def maintenance_events(roots: Sequence[RootSpec], *, days: int = 30) -> dict[str, Any]:
    """Summarise the ticker-history and calibration event logs under each root.

    Lists ``ticker_history_update/<day>/*.json`` and
    ``per_ticker_calibration/<day>/*.json`` for the newest ``days`` dated
    directories (1..366) and forwards only allowlisted scalars plus list
    lengths; the real files embed absolute paths, which never leave here.
    ``file_status`` is ``corrupt`` for an unreadable event.
    """
    warnings: list[str] = []
    limit = max(1, min(days, 366))
    result: dict[str, Any] = {key: [] for key in MAINTENANCE}
    for root in roots:
        reader = root.reader(warnings)
        for key, (directory, scalars) in MAINTENANCE.items():
            for day in sorted(_subdirs(reader, directory, iso_date), reverse=True)[:limit]:
                for entry in sorted(reader.entries(Path(directory) / day, limit=MAX_ROWS)):
                    if entry.suffix == ".json" and len(result[key]) < MAX_ROWS:
                        result[key].append(_maintenance_row(root, reader, entry, scalars))
    if not roots:
        warnings.append("No artifact root is configured; maintenance events are unavailable.")
    result["warnings"] = _dedup(warnings)
    return result


def build_state_snapshot(
    roots: Sequence[RootSpec], day: date, *, config_dir: Path, state_path: Path | None = None
) -> dict[str, Any]:
    """Compose the page's summary without any event list.

    ``ledgers`` holds counts per kind (entries, corrupt, lock files, pending
    total or None); ``purchases`` the record count, total and last day;
    ``strategy_state`` and ``risk`` their full views (neither carries events);
    ``maintenance`` the newest event's day, status and ``file_status`` per
    log, so a corrupt newest event is an alarm on the card rather than an
    unavailable status. Every sub-view's warnings are merged and deduplicated.
    """
    state = strategy_state_view(state_path, roots=roots)
    purchases = purchases_view(roots)
    risk = risk_view(roots, day, config_dir=config_dir)
    maintenance = maintenance_events(roots, days=7)
    warnings = list(state["warnings"]) + purchases["warnings"] + risk["warnings"] + maintenance["warnings"]
    ledgers: dict[str, Any] = {}
    for kind in LEDGER_KINDS:
        view = ledger_view(roots, kind)
        warnings.extend(view["warnings"])
        pending = [
            entry.get("pending_total_usd" if kind == "cumulative" else "pending_usd")
            if kind != "inverse_martingale"
            else (entry["tracker"] or {}).get("accumulated_usd")
            for entry in view["entries"]
            if entry["status"] == "available"
        ]
        valid_pending: list[float] = []
        for v in pending:
            if isinstance(v, (int, float)):
                try:
                    if math.isfinite(v):
                        valid_pending.append(float(v))
                except OverflowError:
                    pass
        pending_total = round(sum(valid_pending), 2) if (len(valid_pending) == len(pending) and pending) else None
        ledgers[kind] = {
            "entries": len(view["entries"]),
            "listed": len(view.get("days") or view.get("tickers") or []),
            "corrupt": sum(entry["status"] in {"corrupt", "mismatched"} for entry in view["entries"]),
            "lock_files_present": sum(entry["lock_file_present"] for entry in view["entries"]),
            "pending_total_usd": pending_total,
        }
    latest = {
        key: (
            {"day": rows[0]["day"], "status": rows[0].get("status"), "file_status": rows[0]["file_status"]}
            if rows
            else None
        )
        for key, rows in maintenance.items()
        if key != "warnings"
    }
    return {
        "date": day.isoformat(),
        "strategy_state": state,
        "ledgers": ledgers,
        "purchases": purchases["summary"],
        "risk": risk,
        "maintenance": latest,
        "warnings": _dedup(warnings),
    }
