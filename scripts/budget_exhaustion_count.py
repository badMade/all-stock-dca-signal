"""Count live-sized buys the remaining daily or weekly budget could not fund, from each slot's own documents.

Why this exists (2026-11-23): the Markov category-1 ideas in ``claude/markov-extensions.md`` (MDP cash pacing,
the absorbing tier chain) only pay off if scheduled slots often had a buy that live code sized but the shared
budget could not fund. docs/adr-budget-exhaustion-replay.md (revision 3) counts that from what each slot saved
instead of replaying sizing from config history: the Gate 5 document's ``desired_usd`` (3:30:
``suggested_amount_usd``) is already the live-sized buy, and the slot's ISO-week ``orders_pages_week_*`` envelope is
the broker history its own Gate 6 used.

Per slot: pre-slot spend is ``dca_runner.budget_snapshot`` over the envelope minus the slot's own orders; the caps
are the dollars the slot recorded in ``finalize_budget.json``; the allocation is plan_orders' own priority
(``dca_runner._allocation_priority_key``) and stop-at-first-boundary loop. ``plan_orders`` itself is NOT called: its
per-candidate refusals read today's universe, dated no-buy windows and calibration config, and refused all 82
candidates of the real 2026-11-20 3:30 document. ``scripts/tests/test_budget_exhaustion_count.py`` pins
``allocate`` equal to ``plan_orders`` on every real candidate ``plan_orders`` accepts.

Buckets, never merged: ``counted`` (direct evidence: the headline), ``derived`` (a slot without its own week
envelope, rebuilt from the earliest same-week envelope of any stream snapshotted at or after its trigger),
``aborted`` (a non-budget abort at or before Gate 6) and ``unresolved`` (with a reason). Same-day repeats are their
own bucket and never allocated (Gate 6.5 shrinks or zeroes them, so their ``desired_usd`` is not what they cost).

Read-only: never writes, never contacts the broker, never prints an account number (envelopes carry one; it is
not read). Known limits: buying power is ignored; the order-count cap is today's ``MAX_ORDERS_PER_DAY``; the count
stops at Gate 6, so a funded buy that later failed for price or spread is not a miss here.

Usage::

    python -m scripts.budget_exhaustion_count [--root <cron-tracking root>] [--json] [--slots]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

import dca_config
import dca_paths
import dca_runner
from scripts.assemble_order_pages import _order_rows
from scripts.backup_planner import _to_broker_order

ET = ZoneInfo("America/New_York")

# Stream artifact roots (CLAUDE.md "Run classification"); 3:30 lives under the legacy ce16ba6b root.
STREAM_ROOTS = {
    "market_open_dca": "market_open",
    "morning_dca": "morning",
    "midday_dca": "midday",
    "intraday_dca": "intraday",
    "ce16ba6b/scheduled_runs": "final_1530",
}
FINAL_STREAM = "final_1530"
# Both folder spellings exist in the tree: compact YYYYMMDD (what the repo's helpers write) and the older dashed form.
_SLOT_DIR = re.compile(r"^(?P<day>\d{8}|\d{4}-\d{2}-\d{2})_(?P<hhmm>\d{4})ET$")
# The prompts pin Normal for every slot but 3:30 (ADR Q5), so a missing finalize_budget.json falls back to it there.
DEFAULT_NORMAL_CAPS = (180.0, 900.0)
_BUDGET_STATUS = re.compile(r"CAP_EXHAUSTED|BUDGET_EXHAUSTED|^ABORT_GATE6_BUDGET")
_PRE_BUDGET_ABORT = re.compile(r"^ABORT(?:ED)?_GATE(?:[0-5]|6(?!_5))(?:_|$)")


class SlotUnresolved(ValueError):
    """A slot input that cannot be proven; the message is the reason code reported."""


@dataclass(frozen=True)
class Missed:
    ticker: str
    usd: float
    cap: str


@dataclass
class SlotResult:
    slot: str
    stream: str
    trading_day: Optional[str]
    outcome: str  # counted | aborted | unresolved
    reason: Optional[str] = None
    evidence: Optional[str] = None  # direct | derived
    envelope: Optional[str] = None
    caps: Optional[tuple[float, float]] = None
    caps_source: Optional[str] = None
    pre_slot_day_usd: Optional[float] = None
    pre_slot_week_usd: Optional[float] = None
    placed: list[tuple[str, float]] = field(default_factory=list)
    missed: list[Missed] = field(default_factory=list)
    repeats: list[tuple[str, float]] = field(default_factory=list)
    daily_only_missed: list[Missed] = field(default_factory=list)
    own_order_disagreements: list[str] = field(default_factory=list)
    gate9_daily_cap: Optional[int] = None
    authorized_tickers: Optional[list[str]] = None
    candidate_count: int = 0
    candidate_usd: float = 0.0
    derived: Optional["SlotResult"] = None


# ---------------------------------------------------------------- reading


def _read_json(path: Path) -> Any:
    # Duplicate keys refused, as every other evidence reader here does (dca_config fail-closed loading).
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=dca_config._reject_duplicate_keys)


def _parse_time(text: Any) -> datetime:
    value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamp is not timezone-aware")
    return value


def _stream_of(slot_dir: Path, root: Path) -> Optional[str]:
    rel = slot_dir.parent.relative_to(root).as_posix()
    return STREAM_ROOTS.get(rel)


def _slot_day_and_hhmm(slot_dir: Path) -> tuple[date, str]:
    match = _SLOT_DIR.match(slot_dir.name)
    if not match:
        raise SlotUnresolved("bad_slot_name")
    raw = match["day"].replace("-", "")
    return date(int(raw[:4]), int(raw[4:6]), int(raw[6:])), match["hhmm"]


def _run_summary(slot_dir: Path) -> dict:
    path = slot_dir / "run_summary.json"
    if not path.is_file():
        return {}
    try:
        document = _read_json(path)
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _gate5_path(slot_dir: Path) -> Path:
    day, hhmm = _slot_day_and_hhmm(slot_dir)
    exact = slot_dir / f"candidates_{day.isoformat()}_{hhmm}.json"
    if exact.is_file():
        return exact
    found = sorted(slot_dir.glob("candidates*.json"))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise SlotUnresolved("ambiguous_gate5")
    if (slot_dir / "gen_out.json").is_file():  # midday's one full Gate 5 document
        return slot_dir / "gen_out.json"
    raise SlotUnresolved("no_gate5")


def _has_gate5(slot_dir: Path) -> bool:
    try:
        _gate5_path(slot_dir)
    except SlotUnresolved as exc:
        return str(exc) == "ambiguous_gate5"  # still a slot; it reports as unresolved
    return True


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def load_gate5_candidates(slot_dir: Path) -> list[dca_runner.Candidate]:
    """The slot's live-sized buy list as ``Candidate`` rows, sizing evidence included; nothing recomputed."""
    try:
        document = _read_json(_gate5_path(slot_dir))
    except (OSError, ValueError) as exc:
        if isinstance(exc, SlotUnresolved):
            raise
        raise SlotUnresolved("gate5_unusable") from exc
    if not isinstance(document, dict):
        raise SlotUnresolved("gate5_unusable")
    if isinstance(document.get("cands"), list):
        rows = document["cands"]
    elif isinstance(document.get("candidates"), list):  # the 3:30 shape
        rows = [row for row in document["candidates"] if isinstance(row, dict) and row.get("action") == "BUY"]
    else:
        raise SlotUnresolved("gate5_unusable")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("ticker"), str):
            raise SlotUnresolved("gate5_unusable")
        amount = _number(row.get("desired_usd", row.get("suggested_amount_usd")))
        if amount is None or amount <= 0:
            continue  # Gate 9's eligible_count counts positive desired_usd only
        out.append(dca_runner.Candidate(
            ticker=row["ticker"],
            amount_usd=amount,
            multiplier=_number(row.get("multiplier", row.get("buy_multiplier"))),
            drop_pct=_number(row.get("drop_pct", row.get("drop_vs_previous_close_pct"))),
            sizing_base_usd=_number(row.get("sizing_base_usd")),
            sizing_min_drop_pct=_number(row.get("sizing_min_drop_pct")),
            sizing_max_drop_pct=_number(row.get("sizing_max_drop_pct")),
        ))
    return out


def find_slots(root: Path) -> list[Path]:
    """Scheduled slot directories holding a Gate 5 document. Manual runs never count (docs/run_classification.md)."""
    slots = []
    for rel in STREAM_ROOTS:
        base = root / rel
        if not base.is_dir():
            continue
        for slot_dir in base.iterdir():
            if not slot_dir.is_dir() or "manual" in slot_dir.name or not _SLOT_DIR.match(slot_dir.name):
                continue
            origin = (_run_summary(slot_dir).get("run_classification") or {}).get("run_origin")
            if origin is not None and origin != "scheduled":
                continue
            if _has_gate5(slot_dir):
                slots.append(slot_dir)
    return sorted(slots)


def _own_order_ids(slot_dir: Path) -> set[str]:
    ids: set[str] = set()
    try:
        for row in _read_json(slot_dir / "finalize_orders.json"):
            if isinstance(row, dict) and isinstance(row.get("order_id"), str):
                ids.add(row["order_id"])
    except (OSError, ValueError, TypeError):
        pass
    try:
        with (slot_dir / "submitted_orders.csv").open(newline="", encoding="utf-8") as handle:
            ids.update(row["order_id"] for row in csv.DictReader(handle) if row.get("order_id"))
    except (OSError, KeyError, csv.Error):
        pass
    return ids


def _caps(slot_dir: Path, stream: str) -> tuple[tuple[float, float], str]:
    try:
        document = _read_json(slot_dir / "finalize_budget.json")
        daily, weekly = _number(document.get("daily_cap_usd")), _number(document.get("weekly_cap_usd"))
        if daily is not None and weekly is not None:
            return (daily, weekly), "finalize_budget"
    except (OSError, ValueError, AttributeError):
        pass
    if stream == FINAL_STREAM:  # 3:30 selects its tier dynamically; Normal would be a guess
        raise SlotUnresolved("no_caps")
    return DEFAULT_NORMAL_CAPS, "default_normal"


def _monday(day: date) -> str:
    return (day - timedelta(days=day.weekday())).isoformat()


def _load_envelope(path: Path) -> dict:
    document = _read_json(path)
    if not isinstance(document, dict) or not isinstance(document.get("pages"), list):
        raise ValueError("not an envelope")
    return document


def _envelope_rows(document: dict) -> list[dict]:
    return [row for page in document["pages"] for row in _order_rows(page)]


# ---------------------------------------------------------------- the counting core


def pre_slot_rows(raw_rows: Sequence[dict], triggered_at: datetime, own_order_ids: set[str]):
    """Rows committed before the slot ran. ADR Q3 (c): drop a row that is the slot's own by id OR by time, and
    list every row where the two tests disagree."""
    kept, disagreements = [], []
    for row in raw_rows:
        by_id = row.get("id") in own_order_ids
        by_time = _parse_time(row.get("created_at")) >= triggered_at
        if by_id != by_time:
            disagreements.append(str(row.get("id")))
        if not (by_id or by_time):
            kept.append(row)
    return kept, disagreements


def allocate(candidates, snapshot, *, daily_cap_usd: float, weekly_cap_usd: float):
    """plan_orders' allocation order and cap-boundary loop, without its today's-config refusals.

    The boundary checks below mirror ``dca_runner.plan_orders`` line for line (order count, then daily, then
    weekly; the first boundary stops the batch and every later candidate is skipped with it; no back-fill); the
    parity test holds them equal. Returns ``(placed, skipped)``: ``[(ticker, usd)]`` and ``[(ticker, usd, reason)]``.
    """
    placed: list[tuple[str, float]] = []
    skipped: list[tuple[str, float, str]] = []
    running = 0.0
    stop: Optional[str] = None
    seen: set[str] = set()
    for cand in sorted(candidates, key=lambda c: dca_runner._allocation_priority_key(c)):
        ticker = dca_runner._normalize_ticker(cand.ticker)
        if ticker in seen:
            continue
        if stop is not None:
            skipped.append((ticker, cand.amount_usd, stop))
            continue
        seen.add(ticker)
        prospective = running + cand.amount_usd
        if snapshot.day_order_count + len(placed) + 1 > dca_runner.MAX_ORDERS_PER_DAY:
            stop = dca_runner.SKIPPED_DAILY_ORDER_COUNT_CAP
        elif snapshot.day_notional_usd + prospective > daily_cap_usd:
            stop = dca_runner.SKIPPED_DAILY_CAP
        elif snapshot.week_notional_usd + prospective > weekly_cap_usd:
            stop = dca_runner.SKIPPED_WEEKLY_CAP
        if stop is not None:
            skipped.append((ticker, cand.amount_usd, stop))
            continue
        placed.append((ticker, cand.amount_usd))
        running = prospective
    return placed, skipped


def classify_status(status: Optional[str]) -> str:
    """``aborted`` for a non-budget abort at or before Gate 6 (ADR Q2); everything else is ``counted``."""
    if not status or _BUDGET_STATUS.search(status):
        return "counted"
    return "aborted" if _PRE_BUDGET_ABORT.match(status) else "counted"


def _evaluate(result: SlotResult, cands, raw_rows, triggered_at, own_ids, day, caps) -> SlotResult:
    try:
        kept, result.own_order_disagreements = pre_slot_rows(raw_rows, triggered_at, own_ids)
        orders = [_to_broker_order(row) for row in kept]
        snapshot = dca_runner.budget_snapshot(orders, day)
    except (ValueError, TypeError, dca_runner.BudgetDataError):
        result.outcome, result.reason = "unresolved", "envelope_unusable"
        return result
    bought_today = {
        order.symbol for order in orders
        if order.side == "buy" and order.counts_toward_budget and order.created_at.astimezone(ET).date() == day
    }
    first = []
    for cand in cands:
        if dca_runner._normalize_ticker(cand.ticker) in bought_today:
            result.repeats.append((dca_runner._normalize_ticker(cand.ticker), cand.amount_usd))
        else:
            first.append(cand)
    daily, weekly = caps
    result.placed, skipped = allocate(first, snapshot, daily_cap_usd=daily, weekly_cap_usd=weekly)
    result.missed = [Missed(t, usd, reason) for t, usd, reason in skipped]
    _, daily_only = allocate(first, snapshot, daily_cap_usd=daily, weekly_cap_usd=math.inf)
    result.daily_only_missed = [Missed(t, usd, reason) for t, usd, reason in daily_only]
    result.pre_slot_day_usd = round(snapshot.day_notional_usd, 2)
    result.pre_slot_week_usd = round(snapshot.week_notional_usd, 2)
    result.outcome = "counted"
    return result


class EnvelopeIndex:
    """Every scheduled week envelope under ``root``, by Monday; loaded on demand and cached."""

    def __init__(self, root: Path):
        self.root = root
        self.by_monday: dict[str, list[Path]] = {}
        self._loaded: dict[Path, Optional[dict]] = {}
        for rel in STREAM_ROOTS:
            base = root / rel
            if not base.is_dir():
                continue
            for path in base.glob("*/orders_pages_week_*.json"):
                if "manual" in path.parent.name or not _SLOT_DIR.match(path.parent.name):
                    continue
                monday = path.name[len("orders_pages_week_"):][:10]
                self.by_monday.setdefault(monday, []).append(path)

    def load(self, path: Path) -> Optional[dict]:
        if path not in self._loaded:
            try:
                self._loaded[path] = _load_envelope(path)
            except (OSError, ValueError):
                self._loaded[path] = None
        return self._loaded[path]

    def earliest_covering(self, monday: str, triggered_at: datetime) -> Optional[Path]:
        best: Optional[tuple[datetime, Path]] = None
        for path in self.by_monday.get(monday, []):
            document = self.load(path)
            if document is None or str(document.get("trading_day")) != monday:
                continue
            try:
                snapped = _parse_time(document.get("snapshot_at"))
            except ValueError:
                continue
            if snapped >= triggered_at and (best is None or snapped < best[0]):
                best = (snapped, path)
        return best[1] if best else None


def count_slot(slot_dir: Path, root: Path, envelope_index: Optional[EnvelopeIndex] = None) -> SlotResult:
    """One slot's outcome: counted (direct), aborted, or unresolved (with a derived figure when Q4 applies)."""
    stream = _stream_of(slot_dir, root) or "unknown"
    rel = slot_dir.relative_to(root).as_posix()
    summary = _run_summary(slot_dir)
    classification = summary.get("run_classification") or {}
    result = SlotResult(slot=rel, stream=stream, trading_day=None, outcome="unresolved")
    try:
        day, _ = _slot_day_and_hhmm(slot_dir)
        trading_day = classification.get("trading_day") or summary.get("trading_day")
        day = date.fromisoformat(trading_day) if isinstance(trading_day, str) else day
        result.trading_day = day.isoformat()
        cands = load_gate5_candidates(slot_dir)
    except SlotUnresolved as exc:
        result.reason = str(exc)
        return result
    result.candidate_count = len(cands)
    result.candidate_usd = round(sum(c.amount_usd for c in cands), 2)
    gate9 = _gate9_daily_cap(slot_dir)
    result.gate9_daily_cap = gate9
    result.authorized_tickers = _authorized(slot_dir)

    status = summary.get("status")
    if classify_status(status) == "aborted":
        result.outcome, result.reason = "aborted", status
        return result
    try:
        triggered_at = _parse_time(classification["triggered_at_utc"])
    except (KeyError, TypeError, ValueError):
        result.reason = "no_trigger_time"
        return result
    try:
        result.caps, result.caps_source = _caps(slot_dir, stream)
    except SlotUnresolved as exc:
        result.reason = str(exc)
        return result
    own_ids = _own_order_ids(slot_dir)
    monday = _monday(day)

    own_envelopes = sorted(slot_dir.glob("orders_pages_week_*.json"))
    if not own_envelopes:
        result.reason = "no_week_envelope"
        index = envelope_index or EnvelopeIndex(root)
        derived = SlotResult(slot=rel, stream=stream, trading_day=result.trading_day, outcome="unresolved",
                             evidence="derived", caps=result.caps, caps_source=result.caps_source,
                             candidate_count=result.candidate_count, candidate_usd=result.candidate_usd,
                             gate9_daily_cap=gate9, authorized_tickers=result.authorized_tickers)
        neighbour = index.earliest_covering(monday, triggered_at)
        if neighbour is None:
            derived.reason = "no_neighbour_envelope"
        else:
            derived.envelope = neighbour.relative_to(root).as_posix()
            _evaluate(derived, cands, _envelope_rows(index.load(neighbour)), triggered_at, own_ids, day, result.caps)
        result.derived = derived
        return result

    try:
        loaded = [(path, _load_envelope(path)) for path in own_envelopes]
        # Several saves in one slot: the latest snapshot is the one Gate 6 acted on.
        path, document = max(loaded, key=lambda item: _parse_time(item[1].get("snapshot_at")))
    except (OSError, ValueError, TypeError):
        result.reason = "envelope_unusable"
        return result
    if str(document.get("trading_day")) != monday:
        result.reason = "week_envelope_wrong_week"
        return result
    result.evidence = "direct"
    result.envelope = path.relative_to(root).as_posix()
    try:
        rows = _envelope_rows(document)
    except (ValueError, TypeError):
        result.reason = "envelope_unusable"
        return result
    return _evaluate(result, cands, rows, triggered_at, own_ids, day, result.caps)


def _gate9_daily_cap(slot_dir: Path) -> Optional[int]:
    try:
        value = (_read_json(slot_dir / "gate9_report.json").get("dropped") or {}).get("daily_cap")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _authorized(slot_dir: Path) -> Optional[list[str]]:
    found = sorted(slot_dir.glob("budget_authorized_cands_*.json"))
    if not found:
        return None
    try:
        rows = _read_json(found[-1])
        return [row["ticker"] for row in rows]
    except (OSError, ValueError, TypeError, KeyError):
        return None


# ---------------------------------------------------------------- reporting


def _tally(items) -> dict:
    return {"count": len(items), "usd": round(sum(item[1] for item in items), 2)}


def _block(results: Sequence[SlotResult]) -> dict:
    missed = [m for r in results for m in r.missed]
    by_cap: dict[str, list] = {}
    for m in missed:
        by_cap.setdefault(m.cap, []).append((m.ticker, m.usd))
    by_stream: dict[str, dict] = {}
    for r in results:
        entry = by_stream.setdefault(r.stream, {"slots": 0, "slots_with_missed": 0, "count": 0, "usd": 0.0})
        entry["slots"] += 1
        entry["slots_with_missed"] += bool(r.missed)
        entry["count"] += len(r.missed)
        entry["usd"] = round(entry["usd"] + sum(m.usd for m in r.missed), 2)
    return {
        "slots_counted": len(results),
        "slots_with_missed": sum(bool(r.missed) for r in results),
        "sessions_observed": len({r.trading_day for r in results}),
        "sessions_with_missed": len({r.trading_day for r in results if r.missed}),
        "missed_count": len(missed),
        "missed_usd": round(sum(m.usd for m in missed), 2),
        "by_cap": {cap: _tally(items) for cap, items in sorted(by_cap.items())},
        "by_stream": dict(sorted(by_stream.items())),
        "repeats": _tally([item for r in results for item in r.repeats]),
        "daily_only": _tally([(m.ticker, m.usd) for r in results for m in r.daily_only_missed]),
        "own_order_disagreements": sum(len(r.own_order_disagreements) for r in results),
        "gate9_daily_cap_recorded": sum(r.gate9_daily_cap or 0 for r in results),
    }


def summarize(results: Sequence[SlotResult]) -> dict:
    counted = [r for r in results if r.outcome == "counted"]
    aborted = [r for r in results if r.outcome == "aborted"]
    unresolved = [r for r in results if r.outcome == "unresolved"]
    derived = [r.derived for r in unresolved if r.derived is not None]
    unresolved_reasons: dict[str, int] = {}
    for r in unresolved:
        unresolved_reasons[r.reason or "unknown"] = unresolved_reasons.get(r.reason or "unknown", 0) + 1
    derived_unresolved: dict[str, int] = {}
    for r in derived:
        if r.outcome != "counted":
            derived_unresolved[r.reason or "unknown"] = derived_unresolved.get(r.reason or "unknown", 0) + 1
    by_status: dict[str, int] = {}
    for r in aborted:
        by_status[r.reason] = by_status.get(r.reason, 0) + 1
    denominator = len(counted) + len(unresolved)
    return {
        "headline": _block(counted),
        "derived": _block([r for r in derived if r.outcome == "counted"]),
        "derived_unresolved": dict(sorted(derived_unresolved.items())),
        "aborted": {
            "slots": len(aborted),
            "candidate_count": sum(r.candidate_count for r in aborted),
            "candidate_usd": round(sum(r.candidate_usd for r in aborted), 2),
            "by_status": dict(sorted(by_status.items())),
        },
        "unresolved": dict(sorted(unresolved_reasons.items())),
        "unresolved_share": (len(unresolved) / denominator) if denominator else 0.0,
        "known_limits": [
            "buying power ignored",
            "order-count cap is today's MAX_ORDERS_PER_DAY",
            "same-day repeats are not allocated (Gate 6.5 shrinks them); see repeats",
            "counting stops at Gate 6: later price, spread or platform drops are not misses",
        ],
    }


def _format_block(name: str, block: dict) -> list[str]:
    lines = [
        f"{name}: {block['slots_counted']} slots over {block['sessions_observed']} sessions; "
        f"missed {block['missed_count']} buys ${block['missed_usd']:.2f} in {block['slots_with_missed']} slots "
        f"({block['sessions_with_missed']} sessions)",
    ]
    for cap, tally in block["by_cap"].items():
        lines.append(f"  {cap}: {tally['count']} ${tally['usd']:.2f}")
    for stream, entry in block["by_stream"].items():
        lines.append(f"  {stream}: {entry['slots']} slots, missed {entry['count']} ${entry['usd']:.2f}")
    lines.append(f"  repeats (own bucket): {block['repeats']['count']} ${block['repeats']['usd']:.2f}")
    lines.append(f"  daily cap alone (weekly lifted): {block['daily_only']['count']} ${block['daily_only']['usd']:.2f}")
    lines.append(f"  gate9 dropped.daily_cap recorded (cross-check): {block['gate9_daily_cap_recorded']}")
    return lines


def _format_text(summary: dict, results: Sequence[SlotResult], show_slots: bool) -> str:
    lines = _format_block("headline (direct evidence)", summary["headline"])
    lines += _format_block("derived (rebuilt from neighbouring envelopes)", summary["derived"])
    if summary["derived_unresolved"]:
        lines.append(f"  derived unresolved: {summary['derived_unresolved']}")
    aborted = summary["aborted"]
    lines.append(f"aborted (not budget): {aborted['slots']} slots, {aborted['candidate_count']} candidates "
                 f"${aborted['candidate_usd']:.2f}; {aborted['by_status']}")
    lines.append(f"unresolved: {summary['unresolved']} (share {summary['unresolved_share']:.1%})")
    lines.append("known limits: " + "; ".join(summary["known_limits"]))
    if show_slots:
        for r in results:
            shown = r if r.outcome != "unresolved" or r.derived is None else r.derived
            caps: dict[str, int] = {}
            for m in shown.missed:
                caps[m.cap] = caps.get(m.cap, 0) + 1
            label = r.outcome if shown is r else f"unresolved ({r.reason}); derived {shown.outcome}"
            lines.append(f"  {r.slot}: {label} {r.reason or ''} missed {len(shown.missed)} "
                         f"${sum(m.usd for m in shown.missed):.2f} {caps} gate9 {r.gate9_daily_cap}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None, help="Cron-tracking root (default: dca_paths).")
    parser.add_argument("--json", action="store_true", help="Print the summary and every slot as JSON.")
    parser.add_argument("--slots", action="store_true", help="Text mode: also list every slot.")
    args = parser.parse_args(argv)
    root = (args.root or Path(dca_paths.cron_tracking_root())).resolve()
    index = EnvelopeIndex(root)
    results = [count_slot(slot, root, index) for slot in find_slots(root)]
    summary = summarize(results)
    if args.json:
        document = dict(summary, slots=[asdict(r) for r in results])
        sys.stdout.write(json.dumps(document, indent=1, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_format_text(summary, results, args.slots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
