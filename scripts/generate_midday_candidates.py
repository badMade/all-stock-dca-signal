#!/usr/bin/env python3
"""Gate 5 signal-generation entry point for the 1230ET (12:30 PM midday) slot.

WHAT THIS IS FOR
----------------
Cron ``a6239dae`` (the 12:30 PM ET midday live buy) had no committed
signal-generation entry point, so its Gate 5 aborted every weekday with "no
signal-generation entry point exists for this slot; nothing to buy" -- the run
cleared every read-only gate, fetched the week's order history, and then stopped
because there was no candidate producer to call
(``claude/dca-executor-prompts/live-mirror/1230ET-*.txt``, Gate 5).

That prompt names the open question precisely: whether
``MiddayDCAExecutor.run(now, data, broker=...)`` should be the producer is a
human decision, because ``run()`` owns budget arithmetic, the same-day top-up
guard, placement and the artifact tree -- Gates 6 through 9 -- which the prompt's
own gate sequence also owns. Wiring it in would mean two implementations of those
gates racing on one account in a single fire.

This module answers that by not calling ``run()`` at all. It calls
``MiddayDCAExecutor.evaluate()``: the same real signal path, with no broker, no
credentials, no network, no orders and no artifact writes. Gates 6, 6.5, 6.7 and
7 stay exactly where the prompt already has them. Everything below the spec lives
in :mod:`scripts.stream_candidates`, shared with the 0935ET entry point.

SLOT-SPECIFIC NOTES
-------------------
* **Trend validation applies here**, unlike at 0935ET
  (``config/midday_dca.json``'s ``confidence_rule``, ``MIDDAY_PROFILES``'s
  ``validate_trend: True``): a candidate whose price and drop qualify is
  auto-buy eligible ONLY when at least ``trend_min_bars`` (6) usable intraday
  bars resolve over the 60 minutes before 12:30. One whose trend cannot be
  validated is flagged ``manual_review_candidate`` and is deliberately excluded
  from ``cands``/``same_day_top_up_candidates`` while still appearing in
  ``candidates`` and ``manual_review_tickers``. **The payload must therefore
  carry intraday bars from 11:30 ET onward**, not just the decision bar; a
  truncated fetch will not error, it will quietly move real buys into manual
  review.
* **``--deployed-last-7d-usd`` is REQUIRED.** This slot's Gate 5 producer
  contract makes the 7-day deployment kill switch the producer's job: "nothing
  downstream of Gate 5 in this prompt enforces this ... a producer that omits
  this step can carry a deep-drop 5x candidate all the way to Gate 7 uncapped"
  (Codex review: "Apply the deployment kill switch in the producer contract").
  The figure is the trailing-7-day AFTERNOON-scheduled deployment
  (``dca_risk.is_afternoon_scheduled_evidence``), not a sum across all seven
  slots. It is applied by handing the real ladder function the real inputs, so
  the cap fires inside ``near_close_dca.graded_buy_multiplier`` itself rather
  than being recomputed here -- see
  ``scripts.stream_candidates._guarded_sizing_config``, including why this makes
  the entry point's sizing conservative relative to ``MiddayDCAExecutor.run()``
  rather than merely different.
* ``config/midday_dca.json`` ships ``live_execution_enabled=true`` (midday is one
  of the currently-functioning live streams, unlike market-open). That still
  gates placement inside ``run()``, which this module never calls.

USAGE (from the executor prompt's Gate 5)
-----------------------------------------
    python3 -m scripts.generate_midday_candidates \\
        --trading-day 2026-08-26 \\
        --market-data $ROOT/market_data_1230ET.json \\
        --deployed-last-7d-usd 850.00

``$ROOT`` is this slot's own resolved evidence directory
(``dca_paths.stream_root('midday_dca', '<YYYYMMDD>_1230ET')``), the same one
Gate 4 already writes ``orders_week.json`` under. The document's ``cands`` list
is the prompt's ``$ROOT/cands.json`` payload, already carrying the
``prior_close``/``prior_low``/``selected_base_amount_usd``/calibration fields
that prompt requires. See ``docs/midday_signal_entry.md``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from midday_dca import (
    MIDDAY_LIVE_CRON_ID,
    MIDDAY_LIVE_MODE,
    MIDDAY_STREAM,
    MiddayDCAExecutor,
    load_midday_config,
)
from near_close_dca import SourceName
from scripts.stream_candidates import (
    StreamSpec,
    build_document,
    required_config_filenames,
    run_cli,
)

__all__ = ["MIDDAY_SPEC", "generate_midday_candidates", "main"]

MIDDAY_SPEC = StreamSpec(
    slot="1230ET",
    runner_slot_id="1230",
    stream=MIDDAY_STREAM,
    cron_id=MIDDAY_LIVE_CRON_ID,
    executor_cls=MiddayDCAExecutor,
    config_loader=load_midday_config,
    live_mode=MIDDAY_LIVE_MODE,
    stream_config_filename="midday_dca.json",
    source=SourceName.ROBINHOOD,
    # See the module docstring: required by this slot's own producer contract.
    requires_deployed_last_7d_usd=True,
    doc_ref="docs/midday_signal_entry.md",
)

MIDDAY_SOURCE = MIDDAY_SPEC.source
_REQUIRED_CONFIG_FILENAMES = required_config_filenames(MIDDAY_SPEC)


def generate_midday_candidates(
    *,
    trading_day,
    config,
    market_data: Mapping[str, Mapping[str, Any]],
    deployed_last_7d_usd: float,
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    config_dir=None,
    check_planner_compatibility: bool = True,
) -> dict:
    """Run the midday executor's own evaluation and return the Gate 5 document."""
    return build_document(
        MIDDAY_SPEC,
        trading_day=trading_day,
        config=config,
        market_data=market_data,
        source_errors=source_errors,
        ticker_errors=ticker_errors,
        already_covered_tickers=already_covered_tickers,
        deployed_last_7d_usd=deployed_last_7d_usd,
        config_dir=config_dir,
        check_planner_compatibility=check_planner_compatibility,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run_cli(MIDDAY_SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())
