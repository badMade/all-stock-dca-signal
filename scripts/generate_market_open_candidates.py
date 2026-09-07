#!/usr/bin/env python3
"""Gate 5 signal-generation entry point for the 0935ET (9:35 AM market-open) slot.

WHAT THIS IS FOR
----------------
Every one of the seven canonical live-execution prompts
(``claude/dca-executor-prompts/live-mirror/*.txt``) has a Gate 5 step reading
"Run the repo's own signal generation for this slot ... Use the repo entry
point, never a reimplementation". Until this module existed, exactly ONE slot had
such an entry point: ``scripts/generate_buy_candidates.py``, scoped to 1530ET
(see its "SCOPE" section). The 9:35 market-open slot had none, so every fire
ended at Gate 5 with the ``GATE5_DISABLED`` verdict -- an executor session with
real Robinhood access, having cleared every safety gate, could do nothing but
place nothing and alert (``PRODUCTION_READINESS.md`` gap #1).

WHY IT COULD NOT JUST REUSE THE 1530ET MODULE
---------------------------------------------
``scripts/generate_buy_candidates.py`` calls
:func:`near_close_dca.evaluate_source_signal`, the real signal engine for exactly
one stream: the 3:30 PM final buy. Market-open runs on
:meth:`morning_dca.MorningDCAExecutor.evaluate_signal` as specialized by
:class:`market_open_dca.MarketOpenDCAExecutor` -- a different method, a different
result dataclass (``MorningSignal``, not ``SourceSignal``), a single combined
market-data provider instead of a Finance/Robinhood pair to reconcile, and this
slot's own trend-validation exemption. Pointing 0935ET at the 1530ET module would
have been a fourth, non-matching implementation of the signal logic, which is
exactly what Gate 5 forbids.

Everything below the CLI lives in :mod:`scripts.stream_candidates`, shared with
the other executor-class slots. Read that module for the payload contract, the
output document, the fail-closed input rules, and why this calls the executor's
``evaluate()`` rather than its ``run()``.

SLOT-SPECIFIC NOTES
-------------------
* **Trend validation is exempt here.** At 09:35 only five minutes of
  regular-session data exist, so the 60-minute downtrend filter can never
  validate; ``config/scheduled_tasks.json:2ba19526.trend_validation_exemption``
  exempts this slot and ``market_open_dca.MARKET_OPEN_PROFILES`` enforces that in
  code. A qualifying drop is auto-buy eligible with no trend evidence. The
  practical consequence for a caller: unlike the later slots, this payload does
  NOT need the 60-minutes-back lookback bar -- at 09:35 that bar would sit at
  08:35, pre-market, and cannot exist. Bars from 09:30 through the 09:35 decision
  anchor are enough.
* **No ``--deployed-last-7d-usd``.** The 7-day kill switch is funded by
  afternoon-scheduled evidence only (``dca_risk.is_afternoon_scheduled_evidence``)
  and is inert on this stream inside the live executor too, and this slot's
  prompt does not make the guard the producer's responsibility the way the 1230ET
  prompt does. Accepting one here would size DIFFERENTLY from the executor this
  mirrors.
* ``config/market_open_dca.json`` ships ``live_execution_enabled=false``. That
  gates placement inside ``MarketOpenDCAExecutor.run()``, which this module never
  calls; see :mod:`scripts.stream_candidates`.
* **This module writes no artifacts, and the prompt's Gate 9 must.** Because
  ``run()`` is never called, nothing here satisfies
  ``config/market_open_dca.json``'s ``state_file_guarantee``. When that
  requirement was checked against the live-mirror prompt, its Gate 9 only
  notified -- it never wrote ``run_state.json``/``run_summary.json``/
  ``market_open_signals.csv``/``submitted_orders.csv`` or refreshed
  ``latest_market_open_run.json`` -- so a run that placed real orders left them
  invisible to reconciliation, drift detection and every later slot's
  prior-allocation scan (Codex review: "Persist the required live-run artifacts
  after placement"). That gate now states the requirement explicitly. Anything
  driving this entry point end-to-end owes the same artifact set; do not read
  "Gate 5 only" as "no evidence needed".

USAGE (from the executor prompt's Gate 5)
-----------------------------------------
    python3 -m scripts.generate_market_open_candidates \\
        --trading-day 2026-08-26 \\
        --market-data $ROOT/market_data_0935ET.json

``$ROOT`` is this slot's own resolved evidence directory
(``dca_paths.stream_root('market_open_dca', '<YYYYMMDD>_0935ET')``), the same
one Gate 6.5 already writes its ``orders_pages_*.json`` under -- never a bare
repo-relative ``artifacts/`` directory (AGENTS.md: artifact roots come from
``dca_paths.stream_root(...)``/``cron_tracking_root()``, honouring
``$DCA_CRON_TRACKING_ROOT``). See ``docs/market_open_signal_entry.md``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from market_open_dca import (
    MARKET_OPEN_LIVE_CRON_ID,
    MARKET_OPEN_LIVE_MODE,
    MARKET_OPEN_STREAM,
    MarketOpenDCAExecutor,
    load_market_open_config,
)
from near_close_dca import SourceName
from scripts.stream_candidates import (
    StreamSpec,
    build_document,
    required_config_filenames,
    run_cli,
)

__all__ = ["MARKET_OPEN_SPEC", "generate_market_open_candidates", "main"]

MARKET_OPEN_SPEC = StreamSpec(
    slot="0935ET",
    runner_slot_id="0935",
    stream=MARKET_OPEN_STREAM,
    cron_id=MARKET_OPEN_LIVE_CRON_ID,
    executor_cls=MarketOpenDCAExecutor,
    config_loader=load_market_open_config,
    live_mode=MARKET_OPEN_LIVE_MODE,
    stream_config_filename="market_open_dca.json",
    # Signal policy is Finance-primary. Robinhood remains execution-only here
    # and may be supplied to the CLI as validation/context evidence.
    source=SourceName.FINANCE,
    # Finance publishes start-stamped 15-minute bars. At 09:35 its 09:30 bar
    # is still open, so the completed anchor is 09:15 and a fresh Finance quote
    # (not that incomplete bar) supplies the decision price.
    source_interval_minutes=15,
    # Gate checks may finish after 09:35. Grade the connector snapshot at the
    # configured 09:50 live-window boundary so an obtainable in-window quote is
    # not rejected merely because it arrived after the nominal schedule time.
    evaluation_delay_minutes=15,
    ticker_source_overrides={"GFUZ": SourceName.ROBINHOOD},
    # See the module docstring: deliberately off for this slot.
    requires_deployed_last_7d_usd=False,
    doc_ref="docs/market_open_signal_entry.md",
)

# Kept as module-level names so a caller (and this module's tests) can reach the
# slot's own required-config list without importing the shared core.
MARKET_OPEN_SOURCE = MARKET_OPEN_SPEC.source
_REQUIRED_CONFIG_FILENAMES = required_config_filenames(MARKET_OPEN_SPEC)


def generate_market_open_candidates(
    *,
    trading_day,
    config,
    market_data: Mapping[str, Mapping[str, Any]],
    alternate_market_data: Optional[Mapping[str, Mapping[str, Any]]] = None,
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    alternate_ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    config_dir=None,
    check_planner_compatibility: bool = True,
    decision_time_et=None,
) -> dict:
    """Run the market-open executor's own evaluation and return the Gate 5 document."""
    return build_document(
        MARKET_OPEN_SPEC,
        trading_day=trading_day,
        config=config,
        market_data=market_data,
        alternate_market_data=alternate_market_data,
        source_errors=source_errors,
        ticker_errors=ticker_errors,
        alternate_ticker_errors=alternate_ticker_errors,
        already_covered_tickers=already_covered_tickers,
        config_dir=config_dir,
        check_planner_compatibility=check_planner_compatibility,
        decision_time_et=decision_time_et,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run_cli(MARKET_OPEN_SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())
