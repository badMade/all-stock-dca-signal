"""Composite rank page reader for the console (read-only).

Shows the output of the daily composite-rank job (``analysis/composite-rank/``): the latest ranked
list, which names entered or left the above-threshold list, and how ranks tend to move. It reads only
``artifacts/composite_rank/`` below the repository root through ``BoundedReader`` and never writes,
runs a job, or touches an order path. The job itself is a scheduled task; this module only displays what
it left on disk. Standard library only.
"""

from __future__ import annotations

import csv
import io
import statistics
from pathlib import Path
from typing import Any

from dca_console_reader import BoundedReader, iso_date

DATA_RELATIVE = Path("artifacts") / "composite_rank"
MAX_TABLE_ROWS = 600
MOVER_COUNT = 10
BAND_EDGES = (10, 25, 50, 100, 200)
PERIOD_COLUMNS = ("1y", "1m", "1w", "3m", "6m", "5y")


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _csv_rows(text: str | None) -> list[dict[str, str]]:
    if not text:
        return []
    return list(csv.DictReader(io.StringIO(text)))


def _band(rank: int) -> str:
    previous = 0
    for edge in BAND_EDGES:
        if rank <= edge:
            return f"{previous + 1}-{edge}"
        previous = edge
    return f"{BAND_EDGES[-1] + 1}+"


def _band_order(label: str) -> int:
    return int(label.split("-")[0].rstrip("+"))


def _ranked_table(reader: BoundedReader, run_date: str, previous_ranks: dict[str, int]) -> list[dict[str, Any]]:
    rows = _csv_rows(reader.text(Path("runs") / run_date / "ranked_all.csv"))[:MAX_TABLE_ROWS]
    table = []
    for row in rows:
        rank = _int(row.get("rank"))
        ticker = row.get("ticker", "")
        if rank is None or not ticker:
            continue
        previous = previous_ranks.get(ticker)
        table.append({
            "rank": rank,
            "ticker": ticker,
            "company": row.get("company", ""),
            "sector": row.get("sector", ""),
            "held": row.get("held") == "1",
            "score": _float(row.get("score")),
            "vol": _float(row.get("vol")),
            "max_dd": _float(row.get("max_dd")),
            "returns": {p: _float(row.get(f"ret_{p}")) for p in PERIOD_COLUMNS},
            "rank_change": None if previous is None else previous - rank,
        })
    return table


def _movement(history: list[dict[str, str]], threshold: float) -> dict[str, Any]:
    by_ticker: dict[str, list[tuple[str, int, float]]] = {}
    for row in history:
        rank, score = _int(row.get("rank")), _float(row.get("score"))
        if rank is not None and score is not None and iso_date(row.get("date")):
            by_ticker.setdefault(row.get("ticker", ""), []).append((row["date"], rank, score))
    band_moves: dict[str, list[int]] = {}
    per_ticker: dict[str, dict[str, Any]] = {}
    for ticker, series in by_ticker.items():
        series.sort()
        ranks = [r for _, r, _ in series]
        moves = [a - b for a, b in zip(ranks, ranks[1:], strict=False)]
        for (_, prior_rank, _), move in zip(series, moves, strict=False):
            band_moves.setdefault(_band(prior_rank), []).append(move)
        per_ticker[ticker] = {
            "ticker": ticker,
            "days": len(series),
            "rank": ranks[-1],
            "avg_rank": round(statistics.fmean(ranks), 1),
            "best": min(ranks),
            "worst": max(ranks),
            "avg_abs_move": round(statistics.fmean(abs(m) for m in moves), 1) if moves else 0.0,
            "change_1d": moves[-1] if moves else 0,
            "change_5d": ranks[-6] - ranks[-1] if len(ranks) > 5 else None,
            "days_above": sum(1 for _, _, s in series if s > threshold),
        }
    bands = []
    for label in sorted(band_moves, key=_band_order):
        moves = band_moves[label]
        bands.append({
            "band": label,
            "samples": len(moves),
            "avg_abs_move": round(statistics.fmean(abs(m) for m in moves), 1),
            "median_abs_move": round(statistics.median(abs(m) for m in moves), 1),
            "pct_moved_10_plus": round(100.0 * sum(1 for m in moves if abs(m) >= 10) / len(moves), 1),
            "avg_signed_move": round(statistics.fmean(moves), 1),
        })
    movers = [t for t in per_ticker.values() if t["days"] > 1]
    return {
        "bands": bands,
        "climbers": sorted(movers, key=lambda t: (-t["change_1d"], t["ticker"]))[:MOVER_COUNT],
        "fallers": sorted(movers, key=lambda t: (t["change_1d"], t["ticker"]))[:MOVER_COUNT],
        "tickers": sorted(per_ticker.values(), key=lambda t: t["rank"])[:MAX_TABLE_ROWS],
    }


SHADOW_TOP = 15
SHADOW_DATES = 10


def _trade_views(reader: BoundedReader, held: set[str]) -> dict[str, Any]:
    """Latest daily trade list plus how its size and membership moved across recent days (review-first, read-only)."""
    latest = [
        {"rank": _int(r.get("rank")), "ticker": r.get("ticker", ""), "score": _float(r.get("score")),
         "reason": r.get("reason", ""), "held": r.get("ticker", "") in held}
        for r in _csv_rows(reader.text("latest_trade_list.csv"))[:MAX_TABLE_ROWS]
    ]
    by_date: dict[str, set[str]] = {}
    version = ""
    for r in _csv_rows(reader.text("trade_list_history.csv")):
        if iso_date(r.get("date")):
            by_date.setdefault(r["date"], set()).add(r.get("ticker", ""))
            version = r.get("params_version", "") or version
    days, previous = [], None
    for day in sorted(by_date)[-SHADOW_DATES:]:
        now = by_date[day]
        days.append({"date": day, "names": len(now),
                     "added": None if previous is None else len(now - previous),
                     "removed": None if previous is None else len(previous - now)})
        previous = now
    proposal = reader.text("latest_tickers_proposal.md") or ""
    return {"latest": latest, "history": list(reversed(days)), "params_version": version, "proposal_markdown": proposal}


def _shadow_views(reader: BoundedReader, rank_tickers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Shadow logs for the forward models. They are logged at zero weight, so this only shows coverage and the latest
    forecasts; the realised-return scorecards need prices and stay a CLI command (``shadow-scorecard``)."""
    octo_by_date: dict[str, list[dict[str, str]]] = {}
    for r in _csv_rows(reader.text("shadow_octopus_history.csv")):
        if iso_date(r.get("origin_date")):
            octo_by_date.setdefault(r["origin_date"], []).append(r)
    octo_dates = sorted(octo_by_date)
    octopus: dict[str, Any] = {"available": bool(octo_dates), "weight": 0, "dates": []}
    if octo_dates:
        for day in reversed(octo_dates[-SHADOW_DATES:]):
            rets = [v for v in (_float(r.get("exp_ret")) for r in octo_by_date[day]) if v is not None]
            octopus["dates"].append({"date": day, "tickers": len(octo_by_date[day]),
                                     "mean_exp_ret": round(statistics.fmean(rets), 5) if rets else None})
        latest_rows = octo_by_date[octo_dates[-1]]
        top = []
        for r in latest_rows:
            ticker, exp_ret = r.get("ticker", ""), _float(r.get("exp_ret"))
            if ticker in rank_tickers and exp_ret is not None:  # S&P 500 / scored names only: micro-caps are noise here
                top.append({"ticker": ticker, "exp_ret": exp_ret, "p10": _float(r.get("p10_ret")), "p90": _float(r.get("p90_ret")),
                            "target": r.get("expected_target_date", ""), "rank": rank_tickers[ticker]["rank"],
                            "held": rank_tickers[ticker]["held"]})
        top.sort(key=lambda t: -t["exp_ret"])
        octopus.update({"as_of": octo_dates[-1], "top": top[:SHADOW_TOP], "bottom": list(reversed(top[-SHADOW_TOP:]))})
    ml_by_date: dict[str, list[dict[str, str]]] = {}
    for r in _csv_rows(reader.text("shadow_ml_history.csv")):
        if iso_date(r.get("date")):
            ml_by_date.setdefault(r["date"], []).append(r)
    ml_dates = sorted(ml_by_date)
    ml: dict[str, Any] = {"available": bool(ml_dates), "weight": 0, "dates": []}
    if ml_dates:
        for day in reversed(ml_dates[-SHADOW_DATES:]):
            statuses: dict[str, int] = {}
            for r in ml_by_date[day]:
                statuses[r.get("status", "")] = statuses.get(r.get("status", ""), 0) + 1
            ml["dates"].append({"date": day, "tickers": len(ml_by_date[day]), "status": statuses})
        rows = [{"ticker": r.get("ticker", ""), "tilt": _float(r.get("tilt")), "status": r.get("status", "")}
                for r in ml_by_date[ml_dates[-1]] if _float(r.get("tilt")) is not None]
        rows.sort(key=lambda t: -t["tilt"])
        ml.update({"as_of": ml_dates[-1], "top": rows[:SHADOW_TOP], "bottom": list(reversed(rows[-SHADOW_TOP:]))})
    return {"octopus": octopus, "ml": ml}


def build_rank_snapshot(repo: Path) -> dict[str, Any]:
    """Compile the page payload from ``<repo>/artifacts/composite_rank``; never raises for missing data."""
    warnings: list[str] = []
    root = (Path(repo) / DATA_RELATIVE).resolve()
    snapshot: dict[str, Any] = {
        "available": False,
        "message": "No composite-rank runs yet. The scheduled task 'daily-composite-rank' writes them after each close.",
        "run_dates": [],
        "warnings": warnings,
    }
    if not root.is_dir():
        return snapshot
    reader = BoundedReader(root, warnings)
    run_dates = sorted(
        entry.name for entry in reader.entries("runs") if iso_date(entry.name) and (root / entry).is_dir()
    )
    if not run_dates:
        return snapshot
    latest = run_dates[-1]
    summary = reader.json(Path("runs") / latest / "summary.json")
    gaps = reader.json(Path("runs") / latest / "data_gaps.json")
    history = _csv_rows(reader.text("history.csv"))
    threshold = _float(summary.get("threshold")) or 80.0

    previous_date = run_dates[-2] if len(run_dates) > 1 else None
    previous_ranks = {
        row.get("ticker", ""): rank
        for row in history
        if row.get("date") == previous_date and (rank := _int(row.get("rank"))) is not None
    }
    table = _ranked_table(reader, latest, previous_ranks)
    above = [row for row in table if row["score"] is not None and row["score"] > threshold]
    previous_above = {
        row.get("ticker", "")
        for row in history
        if row.get("date") == previous_date and (_float(row.get("score")) or 0.0) > threshold
    }
    current_above = {row["ticker"] for row in above}
    snapshot.update({
        "available": True,
        "message": "",
        "as_of": latest,
        "previous_run": previous_date,
        "run_dates": run_dates[-60:],
        "run_count": len(run_dates),
        "universe": summary.get("universe", "sp500"),
        "scored": summary.get("scored", len(table)),
        "threshold": threshold,
        "above_threshold": len(above),
        "held_above_threshold": sum(1 for row in above if row["held"]),
        "entered": sorted(current_above - previous_above) if previous_date else [],
        "dropped": sorted(previous_above - current_above) if previous_date else [],
        "no_price_history": gaps.get("no_price_history", []),
        "not_scored_or_stale": gaps.get("not_scored_or_stale", []),
        "evaluation": above,
        "ranked": table,
        "movement": _movement(history, threshold),
        "trade": _trade_views(reader, {row["ticker"] for row in table if row["held"]}),
        "shadow": _shadow_views(reader, {row["ticker"]: row for row in table}),
    })
    return snapshot
