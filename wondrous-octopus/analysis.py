"""The short whole-model analysis: report.json numbers -> plain English.

Every sentence is generated from measured values -- no hand-written claims.
The output is ~15 lines a person can read in under a minute and know: what
the model is, whether it predicts well, how it sees the market, where it is
strong or weak, and whether its encoder is healthy.
"""

from __future__ import annotations

from typing import Any

from data import COMPONENTS


def _verdict(skill: float) -> str:
    if skill >= 0.10:
        return "clearly beats"
    if skill >= 0.02:
        return "edges out"
    if skill >= -0.02:
        return "roughly matches"
    return "currently loses to"


def _pct(value: float) -> str:
    return f"{100.0 * value:.1f}%"


def _primary_model(report: dict[str, Any]) -> str:
    models = report.get("models") or []
    for candidate in ("octopus", "gbm_quantile", "persistence"):
        if candidate in models:
            return candidate
    return models[0] if models else "?"


def render_analysis(
    report: dict[str, Any],
    *,
    run_meta: dict[str, Any] | None = None,
    per_ticker_skill: dict[str, float] | None = None,
    health: dict[str, Any] | None = None,
    primary: str | None = None,
) -> str:
    """The analysis.md text. Numbers come from report.json; nothing is invented.

    ``primary`` lets the caller pin the model the prose describes -- e.g.
    inspect_run.py's --model selection, which also chose the model
    per_ticker_skill/health were computed for. Falls back to the fixed
    octopus-first preference when omitted or not a model this report
    actually scored (Codex review: "Honor the selected model in generated
    analyses").
    """
    lines: list[str] = ["# Wondrous Octopus — model analysis", ""]
    # A valid run that has only pending forecasts (nothing has matured into a
    # scored day yet) reports an empty model list -- render a neutral status
    # instead of indexing into it (Codex review: "Handle reports with no
    # scored forecasts").
    if not report.get("models"):
        universe = report.get("universe") or (run_meta or {}).get("universe") or {}
        lines.append(
            "**What this is.** A walk-forward run over "
            f"{universe.get('n_tickers', '?')} tickers ({universe.get('description', 'an unlabeled universe')})."
        )
        lines.append(
            "**Status.** No forecast has matured into a scored day yet, so no accuracy analysis is "
            "available. Check back once the run advances past its first target date."
        )
        return "\n".join(lines) + "\n"
    if primary is None or primary not in report["models"]:
        primary = _primary_model(report)
    universe = report.get("universe") or (run_meta or {}).get("universe") or {}
    # `(report.get(key) or {})`, not `.get(key, {})`: the default only
    # applies when the key is ABSENT, not when it's present but explicitly
    # None (e.g. a hand-edited or older-schema report.json) -- the latter
    # would otherwise raise AttributeError on the chained .get() (Codex
    # review: "Robustness: Potential AttributeError on Explicit None
    # Values").
    n_scored = (report.get("n_scored") or {}).get(primary)

    # What it is.
    described = universe.get("description", "an unlabeled universe")
    others = ", ".join(model for model in report.get("models", []) if model != primary)
    tail = f", alongside {others} for comparison." if others else "."
    lines.append(
        f"**What this is.** Walk-forward next-day OHLC forecasts from `{primary}` over "
        f"{universe.get('n_tickers', '?')} tickers ({described}); "
        f"{n_scored if n_scored is not None else '?'} forecasts have been scored against realized days" + tail
    )

    # How good.
    all_block = ((report.get("by_regime") or {}).get("all") or {}).get(primary, {})
    close = all_block.get("close")
    if close:
        lines.append(
            f"**Accuracy.** Close forecasts miss by {_pct(close['mae'])} per day on average "
            f"(≈ ${close['mae_price']:.2f} on a typical share)."
        )
    skill = (report.get("skill_vs_persistence") or {}).get(primary)
    if skill:
        mean_skill = sum(skill.values()) / len(skill)
        per_component = ", ".join(f"{component} {_pct(skill.get(component, 0.0))}" for component in COMPONENTS)
        lines.append(
            f"**Vs. the naive baseline.** It {_verdict(mean_skill)} tomorrow-equals-today persistence "
            f"(skill by component: {per_component})."
        )
    if close:
        target = report.get("nominal_coverage", 0.8)
        lines.append(
            f"**Bands.** The p10–p90 band caught the realized close {_pct(close['coverage'])} of the time "
            f"(target {_pct(target)}; the model's own uncalibrated band managed {_pct(close['coverage_raw'])}, "
            f"so adaptive calibration contributed {_pct(close['coverage'] - close['coverage_raw'])})."
        )
    range_hit = (report.get("range_hit") or {}).get(primary)
    if range_hit:
        lines.append(
            f"**Inside the day's range.** The point close landed within the realized low–high "
            f"{_pct(range_hit['close_in_range'])} of days (open: {_pct(range_hit['open_in_range'])})."
        )

    # How it sees the market.
    market = (report.get("market") or {}).get(primary)
    if market:
        fraction = market["error_variance_market_fraction"]
        character = "mostly market-wide" if fraction > 0.5 else "mostly ticker-specific"
        lines.append(
            f"**The market as one entity.** The equal-weight universe forecast tracks the realized "
            f"aggregate within {_pct(market['aggregate_close_mae'])} per day; {_pct(fraction)} of "
            f"close-error variance is shared across the market, so mistakes are {character}."
        )

    # Strong / weak regimes.
    regime_rows = []
    for regime, block in (report.get("by_regime") or {}).items():
        if regime == "all":
            continue
        stats = block.get(primary, {}).get("close")
        if stats:
            regime_rows.append((regime, stats["mae"], stats["coverage"]))
    if len(regime_rows) >= 2:
        best = min(regime_rows, key=lambda row: row[1])
        worst = max(regime_rows, key=lambda row: row[1])
        lines.append(
            f"**Regimes.** Steadiest in `{best[0]}` (close MAE {_pct(best[1])}, coverage {_pct(best[2])}); "
            f"hardest in `{worst[0]}` (close MAE {_pct(worst[1])}, coverage {_pct(worst[2])})."
        )

    # Best / worst tickers. Slices are sized to never overlap: with fewer
    # than 6 ranked tickers, ranked[:3] and ranked[-3:] would otherwise share
    # entries and double-list a ticker as both "highest" and "lowest".
    if per_ticker_skill:
        ranked = sorted(per_ticker_skill.items(), key=lambda item: item[1], reverse=True)
        top_n = min(3, len(ranked))
        bottom_n = min(3, len(ranked) - top_n)
        if top_n:
            top = ", ".join(f"{ticker} ({_pct(value)})" for ticker, value in ranked[:top_n])
            if bottom_n:
                bottom_items = ranked[len(ranked) - bottom_n :]
                bottom = ", ".join(f"{ticker} ({_pct(value)})" for ticker, value in bottom_items)
                lines.append(
                    f"**Tickers.** Highest skill vs persistence: {top}. Lowest: {bottom}."
                )
            else:
                lines.append(f"**Tickers.** Skill vs persistence: {top}.")

    # Health.
    if health and health.get("modes"):
        pieces = []
        for mode, stats in health["modes"].items():
            trend = "degraded" if stats["degraded"] else "stable"
            pieces.append(
                f"{mode}-context reconstruction {trend} "
                f"({stats['baseline_mae_close']:.4f} → {stats['current_mae_close']:.4f} close-MAE)"
            )
        lines.append(f"**Encoder health: {health['status']}.** " + "; ".join(pieces) + ".")
        if health["status"] == "DEGRADED":
            lines.append(
                "A recovery retrain on full history is recommended "
                "(`run_walkforward.py --resume <run_dir> --retrain`)."
            )
    diagnostics = report.get("diagnostics", {})
    if not health and diagnostics:
        lines.append(f"**Diagnostics.** Reconstruction MAE by mode: {diagnostics}.")

    lines.append("")
    lines.append(
        "_Every figure above is computed from this run's scored-forecast ledger "
        "(report/report.json); nothing is hand-written._"
    )
    return "\n".join(lines) + "\n"
