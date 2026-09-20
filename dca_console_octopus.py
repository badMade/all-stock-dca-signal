"""Bounded, standard-library views of Wondrous Octopus research artifacts.

This adapter never loads model weights, imports research dependencies, or reads
the trading configuration. Forecasts are adjusted-dollar research observations.
The checkpoint and published daily/report files are the authority; uncommitted
ledger tails are deliberately not read.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dca_console_reader import MAX_BYTES, MAX_FILES, BoundedReader, iso_date, root_label

# Module-level copies of the shared limits: tests tighten them per module, and
# _Reader reads them at construction so the override applies to the next read.
_MAX_BYTES = MAX_BYTES
_MAX_FILES = MAX_FILES
_MAX_ROWS = 50_000
_MAX_RESULTS = 2_000
_COMPONENTS = ("open", "high", "low", "close")
_MODELS = ("octopus", "gbm_quantile", "persistence")
_PERIOD_NAMES = {
    "all": "All scored periods",
    "dotcom_bust": "Dot-com bust",
    "gfc": "Global financial crisis",
    "covid": "COVID",
    "rate_shock_2022": "2022 rate shock",
    "other": "Other periods",
}
_SYMBOL = re.compile(r"[A-Z^][A-Z0-9.^=\-]{0,24}\Z")


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


_date = iso_date


class _Reader(BoundedReader):
    """The Octopus run reader: the shared bounded reader plus the ``daily/`` lookup.

    The confinement, size and scan limits moved to ``dca_console_reader`` in
    GUI expansion Phase 0 so every later reader shares them; this subclass
    keeps the one Octopus-specific helper and the private name the module's
    own functions and tests already use.
    """

    def __init__(self, root: Path, warnings: list[str]) -> None:
        super().__init__(root, warnings, max_bytes=_MAX_BYTES, max_files=_MAX_FILES)

    def latest_daily(self, suffix: str) -> Path | None:
        return self.latest_in("daily", suffix)


def _summary_fields(raw: dict) -> dict:
    """Allow only documented research data; never echo arbitrary artifact keys."""
    clean: dict[str, Any] = {}
    if observed := _date(raw.get("date")):
        clean["date"] = observed
    days = raw.get("days_processed")
    if isinstance(days, list):
        clean["days_processed"] = [day for item in days[:_MAX_ROWS] if (day := _date(item))]
    for key in ("rebound_forecasts", "predicted_tickers"):
        if (value := _number(raw.get(key))) is not None and value >= 0:
            clean[key] = int(value)
    models = {}
    for name in _MODELS:
        block = _mapping(_mapping(raw.get("models")).get(name))
        if not block:
            continue
        models[name] = {
            key: _number(block.get(key))
            for key in ("scored_today", "close_mae_today", "in_band_today", "coverage_to_date")
        }
        beats = block.get("beats_persistence")
        models[name]["beats_persistence"] = beats if isinstance(beats, bool) else None
    if models:
        clean["models"] = models
    health = _mapping(raw.get("encoder_health"))
    if health:
        status = health.get("status")
        clean["encoder_health"] = {
            "status": status if status in ("OK", "STABLE", "HEALTHY", "DEGRADED", "N/A") else "N/A"
        }
        if health.get("model") in _MODELS:
            clean["encoder_health"]["model"] = health["model"]
    return clean


def _forecast(raw: dict, *, log_returns: bool = False, target: Any = None) -> dict | None:
    ticker = raw.get("ticker")
    model = raw.get("model")
    if not isinstance(ticker, str) or not _SYMBOL.fullmatch(ticker) or model not in _MODELS:
        return None
    # A checkpoint bucket can be a deferred *scoring* date after a ticker
    # recovers missing history. It is not authoritative for the target date.
    target_date = _date(raw.get("target_date"))
    row: dict[str, Any] = {
        "ticker": ticker,
        "model": model,
        "origin_date": _date(raw.get("origin_date")),
        "target_date": target_date,
        "target_label": target_date or "Next trading row (date unresolved)",
        "price_basis": "adjusted dollars",
        "ref_close": _number(raw.get("ref_close")),
    }
    if _date(target):
        row["scheduled_score_date"] = _date(target)
    reference = row["ref_close"]

    def price(value: Any) -> float | None:
        number = _number(value)
        if number is None:
            return None
        if not log_returns:
            return number
        if reference is None or reference <= 0:
            return None
        # Match the existing exporter's overflow bound, while keeping invalid
        # nonfinite input visibly missing instead of fabricating a flat price.
        converted = reference * math.exp(max(-50.0, min(50.0, number)))
        return converted if math.isfinite(converted) else None

    for component in _COMPONENTS:
        row[component] = price(
            _mapping(raw.get("point")).get(component) if log_returns else raw.get(f"pred_{component}")
        )
        for quantile in ("p10", "p50", "p90"):
            row[f"{component}_{quantile}"] = price(
                _mapping(_mapping(raw.get("quantiles")).get(component)).get(quantile)
                if log_returns
                else raw.get(f"{component}_{quantile}")
            )
    row["p10"] = row["close_p10"]
    row["p90"] = row["close_p90"]
    return row


def _checkpoint_forecasts(state: dict, meta: dict, reader: _Reader) -> list[dict]:
    universe = _mapping(meta.get("universe")).get("tickers")
    allowed = {item for item in universe if isinstance(item, str)} if isinstance(universe, list) else None
    current: dict[tuple[str, str], dict] = {}
    scanned = 0
    for target, bucket in _mapping(state.get("outstanding")).items():
        if not isinstance(bucket, list):
            continue
        for raw in bucket:
            scanned += 1
            if scanned > _MAX_ROWS:
                reader.warn("Forecast scanning was capped at 50,000 rows.")
                return list(current.values())
            row = _forecast(_mapping(raw), log_returns=True, target=target)
            if row is None or (allowed is not None and row["ticker"] not in allowed):
                continue
            key = row["ticker"], row["model"]
            previous = current.get(key)
            if previous is None or (row["origin_date"] or "") > (previous["origin_date"] or ""):
                current[key] = row
    return list(current.values())


def _csv_forecasts(path: Path, reader: _Reader) -> list[dict]:
    source = reader.text(path)
    if source is None:
        return []
    rows = []
    try:
        records = csv.reader(io.StringIO(source), strict=True)
        try:
            fieldnames = next(records)
        except StopIteration:
            fieldnames = []

        if not {"ticker", "model", "origin_date", "pred_close"}.issubset(fieldnames):
            reader.warn("The forecast CSV has unsupported columns and was ignored.")
            return []

        for index, raw in enumerate(records):
            if index >= _MAX_ROWS:
                reader.warn("Forecast scanning was capped at 50,000 rows.")
                break
            raw_dict = dict(zip(fieldnames, raw))  # noqa: B905
            if row := _forecast(raw_dict):
                rows.append(row)
    except (csv.Error, ValueError):
        reader.warn("The forecast CSV is corrupt; checkpoint forecasts are used when available.")
        return []
    return rows


def _periods(report: dict, selected: str) -> list[dict]:
    rows = []
    by_regime = _mapping(report.get("by_regime"))
    for regime, label in _PERIOD_NAMES.items():
        if selected != "all" and regime != selected:
            continue
        block = _mapping(by_regime.get(regime))
        for model in _MODELS:
            for component in _COMPONENTS:
                stats = _mapping(_mapping(block.get(model)).get(component))
                if not stats:
                    continue
                row = {"id": regime, "name": label, "model": model, "component": component}
                row.update({key: _number(stats.get(key)) for key in ("n", "mae", "mae_price", "coverage")})
                # The report's published skill metric applies only to the full
                # intersection. Do not mislabel it as a crisis-specific score.
                row["skill"] = (
                    _number(_mapping(_mapping(report.get("skill_vs_persistence")).get(model)).get(component))
                    if regime == "all"
                    else None
                )
                rows.append(row)
    return rows


def build_octopus_snapshot(
    run_dir: Path | None, *, ticker: str = "", period: str = "all", limit: int = 200
) -> dict:
    """Read a selected run for the console, without importing or updating models.

    ``period`` filters historical regime metrics (``all`` includes all regimes);
    ticker filters only pending forecasts, never changes aggregate metrics.
    Unknown next-trading-row dates remain null. Returned numbers are finite or
    null, and only documented research fields are exposed to the interface.
    """
    snapshot: dict[str, Any] = {
        "status": "not_configured",
        "summary": [],
        "forecasts": [],
        "periods": [],
        "latest_summary": {},
        "warnings": [],
        "forecast_count": 0,
        "truncated": False,
    }
    if run_dir is None:
        snapshot["summary"] = ["Select a Wondrous Octopus run directory to view forecasts and scored periods."]
        return snapshot
    try:
        root = Path(run_dir).expanduser().resolve()
        is_directory = root.is_dir()
    except (OSError, RuntimeError):
        is_directory = False
        root = Path(run_dir)
    # A label, never the absolute run directory: the snapshot is a browser
    # response and a saved summary (GUI expansion Phase 0 leak fix).
    snapshot["run_label"] = root_label(root)
    if not is_directory:
        snapshot["status"] = "missing"
        snapshot["summary"] = ["The selected Wondrous Octopus run directory does not exist."]
        return snapshot
    reader = _Reader(root, snapshot["warnings"])
    report = reader.json("report/report.json")
    meta = reader.json("run_meta.json")
    checkpoint_path = "checkpoint/harness_state.json"
    state = reader.json(checkpoint_path)
    if not state:
        checkpoint_path = "checkpoint.prev/harness_state.json"
        state = reader.json(checkpoint_path)
    report_modified = reader.modified_at("report/report.json") if report else None
    checkpoint_modified = reader.modified_at(checkpoint_path) if state else None
    snapshot["report_may_be_stale"] = bool(
        report_modified is not None and checkpoint_modified is not None and report_modified < checkpoint_modified
    )
    if report_modified is not None:
        snapshot["report_modified_at"] = datetime.fromtimestamp(report_modified, timezone.utc).isoformat()
    if snapshot["report_may_be_stale"]:
        reader.warn("The period report predates the checkpoint and may omit later scores; regenerate the report.")
    summary_path = reader.latest_daily("_summary.json")
    daily = _summary_fields(reader.json(summary_path)) if summary_path is not None else {}
    snapshot["latest_summary"] = daily
    cursor = _date(state.get("last_processed_date"))
    csv_path = reader.latest_daily("_next_day_forecasts.csv")
    csv_modified = reader.modified_at(csv_path) if csv_path is not None else None
    has_checkpoint_forecasts = isinstance(state.get("outstanding"), dict)
    csv_older_day = csv_path is not None and cursor is not None and csv_path.name[:10] < cursor
    csv_older_same_day = (
        csv_path is not None and cursor is not None and csv_path.name[:10] == cursor
        and csv_modified is not None and checkpoint_modified is not None and csv_modified < checkpoint_modified
    )
    rows = []
    if has_checkpoint_forecasts and (csv_path is None or csv_older_day or csv_older_same_day):
        # A same-day recovery can retire forecasts or update the universe, then
        # stop before replacing the CSV. An empty outstanding mapping is still
        # authoritative and must not resurrect retired CSV rows.
        rows = _checkpoint_forecasts(state, meta, reader)
        snapshot["forecast_source"] = "checkpoint outstanding forecasts"
    elif csv_path is not None and not csv_older_day:
        rows = _csv_forecasts(csv_path, reader)
        if rows:
            snapshot["forecast_source"] = str(csv_path)
    if "forecast_source" not in snapshot and has_checkpoint_forecasts:
        rows = _checkpoint_forecasts(state, meta, reader)
        snapshot["forecast_source"] = "checkpoint outstanding forecasts"
    if csv_older_day:
        reader.warn("The daily CSV is older than the checkpoint; checkpoint forecasts are shown.")
    elif csv_older_same_day and has_checkpoint_forecasts:
        reader.warn("The daily CSV is older than the same-day checkpoint; checkpoint forecasts are shown.")
    if daily.get("date") and cursor and daily["date"] < cursor:
        reader.warn("The daily summary is older than the checkpoint.")
    if period not in _PERIOD_NAMES:
        reader.warn("The selected historical period is unavailable.")
    snapshot["periods"] = _periods(report, period)
    snapshot["period_options"] = [
        {"id": key, "name": label} for key, label in _PERIOD_NAMES.items()
        if key in _mapping(report.get("by_regime"))
    ]
    query = ticker.strip().upper()
    filtered = sorted((row for row in rows if not query or row["ticker"] == query), key=lambda r: (r["ticker"], r["model"]))
    bound = max(1, min(_MAX_RESULTS, limit))
    snapshot["forecasts"] = filtered[:bound]
    snapshot["forecast_count"] = len(filtered)
    snapshot["truncated"] = len(filtered) > bound
    snapshot["as_of"] = cursor or daily.get("date")
    snapshot["status"] = "available" if has_checkpoint_forecasts or rows or report or daily else "empty"
    if any(row[component] is None for row in filtered for component in _COMPONENTS):
        reader.warn("Some forecast prices are missing or nonfinite and are shown as unavailable.")
    if any(row["target_date"] is None for row in filtered):
        reader.warn("Forecast targets are the next available trading row; an exact target date is not yet recorded.")
    lines = snapshot["summary"]
    if snapshot["as_of"]:
        lines.append(f"Latest processed trading day: {snapshot['as_of']}.")
    lines.append(f"{len(filtered)} pending forecast rows across {len({row['ticker'] for row in filtered})} tickers.")
    if snapshot["truncated"]:
        lines.append(f"Showing the first {bound} matching forecast rows.")
    for model, block in daily.get("models", {}).items():
        scored = block["scored_today"]
        if scored is not None:
            lines.append(f"{model}: {int(scored)} forecasts scored on {daily.get('date', 'the latest daily update')}.")
    if health := daily.get("encoder_health", {}).get("status"):
        lines.append(f"Encoder health: {health}.")
    if report:
        lines.append("Historical period metrics are measured walk-forward results across the run's scored universe.")
    else:
        lines.append("No historical period report is available yet.")
    lines.append("Forecasts are research outputs in adjusted dollars and do not change live DCA sizing.")
    return snapshot
