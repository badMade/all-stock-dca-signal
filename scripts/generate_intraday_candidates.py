#!/usr/bin/env python3
"""Gate 5 signal-generation entry point for the three intraday slots.

WHAT THIS IS FOR
----------------
Cron ``7be82b97`` fires three times a weekday -- 11:30 AM, 1:30 PM and 2:30 PM
ET -- and every one of those fires hit the same wall as 0935ET and 1230ET did:
Gate 5 had no committed signal-generation entry point to call, so the run
cleared every read-only gate and then stopped with "no signal-generation entry
point exists for this slot; nothing to buy"
(``claude/dca-executor-prompts/live-mirror/1130ET-*.txt`` and its 1330/1430
siblings, Gate 5).

WHY ONE MODULE FOR THREE SLOTS
------------------------------
Because the repo already models them as one thing. The three fires share ONE
cron id, ONE executor class (:class:`intraday_dca.IntradayDCAExecutor`), ONE
config file (``config/intraday_dca.json``), ONE artifact root
(``intraday_dca/``), ONE pointer (``latest_intraday_run.json``) and ONE pooled
Normal-tier budget -- ``INTRADAY_SLOT_PROFILES`` differ ONLY in the decision
anchor, the +/-5 minute window and the ``<slot>ET`` folder suffix. The executor
itself takes the slot as a constructor argument (``IntradayDCAExecutor(...,
slot="1330")``) rather than existing in three subclasses, and this module
mirrors that exactly: three :class:`~scripts.stream_candidates.StreamSpec`
literals selected by ``--slot``.

Three copied modules would instead be three places for a review round's
hardening to reach two of, which is the drift ``CLAUDE.md``'s "Three entry
points coexist" warning is about.

Everything below the specs lives in :mod:`scripts.stream_candidates`, shared
with the 0935ET, 1030ET and 1230ET entry points. Read that module for the
payload contract, the output document, the fail-closed input rules, and why
this calls the executor's ``evaluate()`` rather than its ``run()``.

SLOT-SPECIFIC NOTES
-------------------
* **Trend validation applies to all three**, unlike at 0935ET
  (``config/intraday_dca.json``'s ``trend_min_bars`` = 6,
  ``_build_slot_profile``'s ``validate_trend: True``): a candidate whose price
  and drop qualify is auto-buy eligible ONLY when at least 6 usable intraday
  bars resolve over the 60 minutes before its decision anchor. One whose trend
  cannot be validated is flagged ``manual_review_candidate`` and is deliberately
  excluded from ``cands``/``same_day_top_up_candidates`` while still appearing in
  ``candidates`` and ``manual_review_tickers``. **Each slot's payload must
  therefore carry intraday bars from 60 minutes before ITS OWN anchor** -- 10:30
  onward for ``--slot 1130``, 12:30 for ``1330``, 13:30 for ``1430``. A truncated
  fetch will not error; it will quietly move real buys into manual review.
* **``--slot`` has no default.** ``IntradayDCAExecutor``'s own constructor
  defaults to ``DEFAULT_INTRADAY_SLOT`` ("1130"), which is right for a library
  caller and wrong here: a 2:30 PM run that omitted the flag would silently score
  its payload against an 11:30 anchor (rejecting every bar as future-dated) and
  publish the result under the ``1130ET`` slot label. The flag is required.
* **No ``--deployed-last-7d-usd``.** Like 0935ET and unlike 1230ET, none of the
  three intraday prompts makes the trailing-7-day deployment kill switch the
  producer's responsibility -- the 1430ET prompt references the kill switch only
  as a funding input. Accepting the figure here would size DIFFERENTLY from the
  executor this mirrors; see ``scripts.stream_candidates._guarded_sizing_config``
  for why that guard is per-slot rather than uniform.
* **The three fires share one budget, and nothing here knows that.** Gate 6
  pools the Normal-tier ceiling across all three (``cap_seed_roots`` includes
  this stream's own root, so 1:30 seeds from 11:30's fills and 2:30 from both).
  This module evaluates signals only: an 11:30 fill does not reduce the amounts
  a 1:30 document proposes. That offset belongs to the executing agent's Gate 6
  and Gate 6.5, exactly where the prompt already has it -- and it is why
  ``--already-covered-tickers`` exists for the narrower Gate 4 case.
* ``config/intraday_dca.json`` ships ``live_execution_enabled=false``. That gates
  placement inside ``IntradayDCAExecutor.run()``, which this module never calls;
  see :mod:`scripts.stream_candidates`.

USAGE (from each slot's executor prompt, Gate 5)
------------------------------------------------
    python3 -m scripts.generate_intraday_candidates \\
        --slot 1330 \\
        --trading-day 2026-08-26 \\
        --market-data $ROOT/market_data_1330ET.json

``$ROOT`` is that fire's own resolved evidence directory
(``dca_paths.stream_root('intraday_dca', '<YYYYMMDD>_1330ET')``). The
document's ``cands`` list is the prompt's ``$ROOT/cands.json`` payload, already
carrying the ``prior_close``/``prior_low``/``selected_base_amount_usd``/
calibration fields that prompt requires. See ``docs/intraday_signal_entry.md``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from intraday_dca import (
    INTRADAY_LIVE_CRON_ID,
    INTRADAY_LIVE_MODE,
    INTRADAY_SLOTS,
    INTRADAY_STREAM,
    IntradayDCAExecutor,
    load_intraday_config,
)
from near_close_dca import SourceName
from scripts.stream_candidates import (
    StreamSpec,
    build_document,
    required_config_filenames,
    run_multi_slot_cli,
)

__all__ = [
    "INTRADAY_SPECS",
    "generate_intraday_candidates",
    "main",
    "spec_for_slot",
]


def _build_spec(slot: str) -> StreamSpec:
    """One slot's spec. Derived from ``INTRADAY_SLOTS`` rather than written out
    three times so a fourth intraday fire added to that tuple gets an entry point
    for free instead of a silently missing one."""
    return StreamSpec(
        slot=f"{slot}ET",
        runner_slot_id=slot,
        stream=INTRADAY_STREAM,
        cron_id=INTRADAY_LIVE_CRON_ID,
        executor_cls=IntradayDCAExecutor,
        config_loader=load_intraday_config,
        live_mode=INTRADAY_LIVE_MODE,
        stream_config_filename="intraday_dca.json",
        source=SourceName.ROBINHOOD,
        # See the module docstring: this slot's prompt does not make the kill
        # switch the producer's job the way 1230ET's does.
        requires_deployed_last_7d_usd=False,
        # The whole reason one class serves three slots: the profile (decision
        # anchor, window, folder suffix) is selected per-instance from this.
        executor_kwargs={"slot": slot},
        doc_ref="docs/intraday_signal_entry.md",
    )


#: ``--slot`` token -> that fire's spec. Keyed by the bare token (no "ET"), the
#: same spelling ``IntradayDCAExecutor(slot=...)`` and ``dca_runner.SLOTS`` use.
INTRADAY_SPECS: Mapping[str, StreamSpec] = {slot: _build_spec(slot) for slot in INTRADAY_SLOTS}

_REQUIRED_CONFIG_FILENAMES = required_config_filenames(INTRADAY_SPECS[INTRADAY_SLOTS[0]])


def spec_for_slot(slot: str) -> StreamSpec:
    """Return the spec for one intraday fire, accepting "1330" or "1330ET".

    Fails loud on anything else: an unrecognized slot must not fall back to the
    executor's own ``DEFAULT_INTRADAY_SLOT``, which would score the caller's
    payload against the 11:30 anchor.
    """
    token = slot[:-2] if slot.endswith("ET") else slot
    try:
        return INTRADAY_SPECS[token]
    except KeyError:
        raise ValueError(
            f"unknown intraday slot {slot!r}; expected one of "
            f"{sorted(INTRADAY_SPECS)!r} (with or without an 'ET' suffix)"
        ) from None


def generate_intraday_candidates(
    *,
    slot: str,
    trading_day,
    config,
    market_data: Mapping[str, Mapping[str, Any]],
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    config_dir=None,
    check_planner_compatibility: bool = True,
) -> dict:
    """Run one intraday fire's own evaluation and return the Gate 5 document."""
    return build_document(
        spec_for_slot(slot),
        trading_day=trading_day,
        config=config,
        market_data=market_data,
        source_errors=source_errors,
        ticker_errors=ticker_errors,
        already_covered_tickers=already_covered_tickers,
        deployed_last_7d_usd=None,
        config_dir=config_dir,
        check_planner_compatibility=check_planner_compatibility,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run_multi_slot_cli(
        INTRADAY_SPECS,
        argv,
        description=(
            "Gate 5 candidate generation for the intraday stream's three fires "
            f"({', '.join(f'{s}ET' for s in INTRADAY_SLOTS)}, cron {INTRADAY_LIVE_CRON_ID})."
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
