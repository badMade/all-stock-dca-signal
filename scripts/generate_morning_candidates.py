#!/usr/bin/env python3
"""Gate 5 signal-generation entry point for the 1030ET (10:30 AM morning) slot.

WHAT THIS IS FOR
----------------
Cron ``42173154`` (the 10:30 AM ET morning live buy) had no committed
signal-generation entry point, so its Gate 5 aborted every weekday with "no
signal-generation entry point exists for this slot; nothing to buy"
(``claude/dca-executor-prompts/live-mirror/1030ET-*.txt``, Gate 5) -- the run
cleared every read-only gate and then stopped because there was no candidate
producer to call.

This module answers that the same way the 0935ET, 1130/1330/1430ET and 1230ET
entry points do: by calling :meth:`morning_dca.MorningDCAExecutor.evaluate`, the
same real signal path, with no broker, no credentials, no network, no orders and
no artifact writes. Gates 6, 6.5, 6.7 and 7 stay exactly where the prompt already
has them. Everything below the spec lives in :mod:`scripts.stream_candidates`.

THIS IS THE BASE CLASS, NOT A SUBCLASS
--------------------------------------
The other three entry points drive a subclass of ``MorningDCAExecutor``. This one
drives ``MorningDCAExecutor`` itself, in ``MORNING_LIVE_MODE`` -- the morning
stream is where that class's default (watchlist) mode lives alongside the live
one, so two things differ from every sibling module:

* **The config loader takes ``mode`` positionally-second.**
  ``load_morning_config(config_dir, mode, *, allow_unpinned_account=...)``,
  unlike every other ``load_*_config(config_dir, *, allow_unpinned_account=...)``.
  Handing the shared CLI the bare function would have loaded the 9:45
  WATCHLIST profile's config for a 10:30 LIVE run -- silently, since both parse
  the same file. :func:`load_morning_live_config` below pins the mode.
* **The 9:45 pre-flight gate runs.** See the next section.

THE 9:45 PRE-FLIGHT GATE (the one filesystem read in this family)
-----------------------------------------------------------------
``MorningDCAExecutor.evaluate()`` calls ``_apply_preflight_gate()``, which is
active for exactly one combination -- ``MORNING_STREAM`` + ``MORNING_LIVE_MODE``
-- and therefore fires here and in none of the sibling entry points. It narrows
the universe to the tickers the same-day 9:45 pre-flight scan marked
``eligible_to_execute=true``, reading them off the ``morning_dca_preflight``
artifact tree.

That makes this the only Gate 5 command in the family that touches the artifact
tree at all, and the only one whose output is not purely a function of its
arguments. Two consequences worth knowing before you run it:

* **It is NOT fail-closed, deliberately.** Per Geoff's 2026-08-02 direction
  ("should not block any later runs"), a missing, unparseable, misconfigured or
  valid-but-empty pre-flight artifact degrades to the FULL configured universe
  with a warning -- for scheduled and manual runs alike. The 9:45 scan is a
  coarse pre-screen, never a hard gate the 10:30 slot depends on to run; the
  10:30 slot's own signal, trend, budget, spread and top-up checks are the real
  safeguards. See :func:`morning_dca.morning_preflight_eligible_tickers`.
* **The verdict is reported, not swallowed.** The output document's
  ``preflight_gate`` field carries the executor's own gate diagnostic -- whether
  an artifact was found, its run id, which tickers were admitted, which were
  gated out, and any degradation warning. A reader that sees a short candidate
  list can tell a quiet tape apart from a narrow pre-screen. It is ``null`` in
  every sibling slot's document, where the gate does not apply.
* Which tree gets read follows ``config/morning_dca.json``'s
  ``preflight_run_state_root`` and honours ``$DCA_CRON_TRACKING_ROOT``, so a
  relocated/staging run stays contained rather than reading production evidence.

SLOT-SPECIFIC NOTES
-------------------
* **Trend validation applies here**, unlike at 0935ET
  (``config/morning_dca.json``'s ``trend_min_bars`` = 6,
  ``MORNING_PROFILES[MORNING_LIVE_MODE]``'s ``validate_trend: True``): a
  candidate whose price and drop qualify is auto-buy eligible ONLY when at least
  6 usable intraday bars resolve over the 60 minutes before 10:30. One whose
  trend cannot be validated is flagged ``manual_review_candidate`` and is
  deliberately excluded from ``cands``/``same_day_top_up_candidates`` while still
  appearing in ``candidates`` and ``manual_review_tickers``. **The payload must
  therefore carry intraday bars from 09:30 ET onward** -- the full session so
  far, which at 10:30 is exactly the 60-minute lookback -- not just the decision
  bar. A truncated fetch will not error; it will quietly move real buys into
  manual review.
* **No ``--deployed-last-7d-usd``.** Like 0935ET and the intraday fires, and
  unlike 1230ET, this slot's prompt does not make the trailing-7-day deployment
  kill switch the producer's responsibility. Accepting the figure here would size
  DIFFERENTLY from the executor this mirrors; see
  ``scripts.stream_candidates._guarded_sizing_config``.
* ``config/morning_dca.json`` ships the morning stream as one of the
  currently-functioning live streams. Placement is still gated inside
  ``MorningDCAExecutor.run()``, which this module never calls.

USAGE (from the executor prompt's Gate 5)
-----------------------------------------
    python3 -m scripts.generate_morning_candidates \\
        --trading-day 2026-08-26 \\
        --market-data $ROOT/market_data_1030ET.json

``$ROOT`` is this slot's own resolved evidence directory
(``dca_paths.stream_root('morning_dca', '<YYYYMMDD>_1030ET')``), the same one
Gate 4 already writes ``orders_week.json`` under. The document's ``cands`` list
is the prompt's ``$ROOT/cands.json`` payload, already carrying the
``prior_close``/``prior_low``/``selected_base_amount_usd``/calibration fields
that prompt requires. See ``docs/morning_signal_entry.md``.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Mapping, Optional, Sequence

from morning_dca import (
    EASTERN,
    MORNING_LIVE_CRON_ID,
    MORNING_LIVE_MODE,
    MORNING_STREAM,
    MorningDCAExecutor,
    load_morning_config,
    morning_preflight_eligible_tickers,
)
from near_close_dca import SourceName
from scripts.stream_candidates import (
    StreamSpec,
    build_document,
    required_config_filenames,
    run_cli,
)

__all__ = [
    "MORNING_SPEC",
    "preflight_admitted_tickers",
    "generate_morning_candidates",
    "load_morning_live_config",
    "main",
]


def load_morning_live_config(config_dir=None, *, allow_unpinned_account: bool = False):
    """``load_morning_config`` pinned to ``MORNING_LIVE_MODE``.

    Exists because that loader alone in this family takes ``mode`` as its second
    POSITIONAL parameter (defaulting to the 9:45 watchlist mode), while
    :class:`~scripts.stream_candidates.StreamSpec` calls every loader as
    ``loader(config_dir, allow_unpinned_account=True)``. Without this wrapper the
    10:30 live slot would silently score against the 9:45 watchlist profile's
    config -- same file, different artifact roots and different
    ``target_account_number`` handling -- with nothing raising to say so.

    Kept as a named module-level function rather than a lambda or
    ``functools.partial`` so the spec's ``config_loader`` reads as something with
    a docstring a reviewer can find.
    """
    return load_morning_config(
        config_dir, MORNING_LIVE_MODE, allow_unpinned_account=allow_unpinned_account
    )


def preflight_admitted_tickers(trading_day, tickers, config):
    """The tickers the same-day 09:45 pre-flight admits, for the shared core's
    pre-parse narrowing.

    Calls :func:`morning_dca.morning_preflight_eligible_tickers` -- the repo's
    single implementation of this contract, shared with
    ``MorningDCAExecutor._apply_preflight_gate`` and
    ``run_dca.run_slot("1030ET", ...)`` -- rather than re-deriving eligibility.
    A second definition here would be free to drift from the one the executor
    actually applies, which is the whole reason that function was extracted.

    ``manual=False``: this entry point mirrors the SCHEDULED live path. That
    distinction does not change the outcome today (the gate is non-blocking for
    scheduled and manual runs alike), but passing the honest value keeps this
    correct if the contract is ever re-tightened.

    Only the admitted list is used here; the gate diagnostic this discards is
    recomputed and published by ``evaluate()`` itself, which is what the
    document's ``preflight_gate`` field reports.
    """
    admitted, _gate = morning_preflight_eligible_tickers(
        datetime.combine(trading_day, time(0, 0), tzinfo=EASTERN),
        list(tickers),
        preflight_run_state_root=config.preflight_run_state_root,
        preflight_forbidden_roots=config.preflight_forbidden_roots,
        preflight_bounded_recovery=config.preflight_bounded_recovery,
    )
    return admitted


MORNING_SPEC = StreamSpec(
    slot="1030ET",
    runner_slot_id="1030",
    stream=MORNING_STREAM,
    cron_id=MORNING_LIVE_CRON_ID,
    executor_cls=MorningDCAExecutor,
    config_loader=load_morning_live_config,
    live_mode=MORNING_LIVE_MODE,
    stream_config_filename="morning_dca.json",
    source=SourceName.ROBINHOOD,
    # See the module docstring: this slot's prompt does not make the kill switch
    # the producer's job the way 1230ET's does.
    requires_deployed_last_7d_usd=False,
    doc_ref="docs/morning_signal_entry.md",
    # See preflight_admitted_tickers: narrows the scored universe to the 09:45
    # verdict BEFORE payload validation, so a gated-out ticker's malformed
    # evidence cannot abort a run the executor would have completed.
    universe_filter=preflight_admitted_tickers,
)

MORNING_SOURCE = MORNING_SPEC.source
_REQUIRED_CONFIG_FILENAMES = required_config_filenames(MORNING_SPEC)


def generate_morning_candidates(
    *,
    trading_day,
    config,
    market_data: Mapping[str, Mapping[str, Any]],
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    config_dir=None,
    check_planner_compatibility: bool = True,
) -> dict:
    """Run the morning executor's own evaluation and return the Gate 5 document.

    Note the 9:45 pre-flight gate described in the module docstring: unlike the
    sibling generators this reads the ``morning_dca_preflight`` artifact tree, so
    the same arguments can produce different universes on different machines. The
    gate's own verdict rides along in the document's ``preflight_gate`` field.
    """
    return build_document(
        MORNING_SPEC,
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
    return run_cli(MORNING_SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())
