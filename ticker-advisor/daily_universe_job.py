"""The daily universe job (step 7b-2): rank, then optionally apply the passing proposal to the buy list.

Specified first by tests/test_daily_universe_job.py. It is thin glue the scheduler runs once per weekday evening. It
chains ``daily_ranking`` (propose-only) and the gated writer ``universe_apply.apply_proposal`` under the standing
approval in docs/adr-program-maintained-universe.md ("auto-apply when every safeguard passes"). There is no new
ranking or writing logic here.

* Options are ``daily_ranking``'s plus ``--history FILE`` (the ``ticker_history.csv`` to append to) and ``--apply``.
  ``--as-of`` is optional: omitted, it is the latest bar date in the price archive for the snapshot's symbols.
* WITHOUT ``--apply`` it is exactly ``daily_ranking``: the two outputs in ``--out-dir``, nothing else written.
* WITH ``--apply`` and a proposal that passed every safeguard it calls the writer with the approval id
  ``auto:adr-program-maintained-universe:<first 12 hex of the proposal hash>``. Only ``active`` changes.
* A proposal with no adds and no drops is "unchanged": the writer is not called and no history rows are appended,
  so a same-day rerun never duplicates history.
* Exit codes: 0 passed (applied, unchanged, or proposed), 1 rejected by a safeguard (live files untouched),
  2 bad input (one stderr line), 3 the writer refused a passing proposal (live files untouched, reason on stderr).
* It is a BUY-list job only. ``stopped_buying`` names are reported and are never sold. The module imports no broker,
  order, runner or network code.

The last stdout line is one JSON summary. Pure stdlib.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import daily_ranking
import universe_apply
from daily_ranking import _InputError
from price_factors import load_price_history
from price_factors import price_inputs as compute_price_inputs
from universe import PoolThresholds
from universe_inputs import load_snapshot

APPROVAL_PREFIX = "auto:adr-program-maintained-universe:"


def _build_parser():
    parser = daily_ranking._build_parser()
    parser.prog = "daily_universe_job"
    parser.description = (
        "Rank the pool and, with --apply, write a passing proposal to the buy list. Never sells anything."
    )
    for action in parser._actions:
        if "--as-of" in action.option_strings:
            action.required = False
            action.help = "ranking date, YYYY-MM-DD (default: the latest bar date in the price archive)"
    parser.add_argument("--history", help="ticker_history.csv to append the new list to (required with --apply)")
    parser.add_argument("--apply", action="store_true", help="apply a proposal that passed every safeguard")
    return parser


def _latest_bar_date(histories):
    dates = [bars[-1].date for bars in histories.values() if bars]
    if not dates:
        raise _InputError("the price archive has no bars for the snapshot's symbols, so --as-of cannot default")
    return max(dates)


def main(argv: list[str] | None = None) -> int:
    """Run the job. Returns 0 (passed), 1 (rejected), 2 (bad input) or 3 (the writer refused)."""
    parser = _build_parser()
    options = parser.parse_args(argv)
    if options.apply and not options.history:
        parser.error("--apply needs --history")
    try:
        generated_at = daily_ranking._parse_generated_at(options.generated_at)
        size = daily_ranking._parse_int(options.size, "--size", minimum=1)
        prior_pool_size = daily_ranking._parse_int(options.prior_pool_size, "--prior-pool-size", minimum=0)
        snapshot = load_snapshot(options.snapshot)
        active, pinned = daily_ranking._load_tickers_config(options.tickers_config)
        exchanges = daily_ranking._load_exchanges(options.exchanges)
        histories = load_price_history(options.prices, tickers=snapshot["symbols"])
        if options.as_of is None:
            as_of = _latest_bar_date(histories)
        else:
            as_of = daily_ranking._parse_date(options.as_of, "--as-of")
        prices = {t: compute_price_inputs(bars, as_of=as_of, min_price=options.min_price)
                  for t, bars in histories.items()}
        thresholds = PoolThresholds(
            min_price=options.min_price,
            min_market_cap=options.min_market_cap,
            min_median_dollar_volume_60d=options.min_dollar_volume,
            min_history_sessions=options.min_history,
            require_fractional_eligible=False,
        )
        ranking = daily_ranking.run_daily_ranking(
            snapshot=snapshot,
            price_inputs=prices,
            prior_active=active,
            pinned=pinned,
            exchange_by_symbol=exchanges,
            as_of=as_of,
            generated_at=generated_at,
            size=size,  # type: ignore[arg-type]
            thresholds=thresholds,
            prior_pool_size=prior_pool_size,
        )
        proposal_path, report_path = daily_ranking.write_daily_outputs(options.out_dir, ranking)
    except (_InputError, ValueError, OSError) as exc:
        print("error: " + " ".join(str(exc).split()), file=sys.stderr)
        return 2

    proposal = ranking.proposal
    summary: dict = {
        "status": proposal.status,
        "added": sorted(set(proposal.active) - set(proposal.prior_active)),
        "stopped_buying": sorted(set(proposal.prior_active) - set(proposal.active)),
        "failures": sorted({f.code for f in proposal.safeguard_failures}),
        "proposal": str(proposal_path),
        "report": str(report_path),
    }
    if proposal.status != "pending_approval":
        print(json.dumps(summary, sort_keys=True))
        return 1

    if options.apply:
        if not summary["added"] and not summary["stopped_buying"]:
            summary["status"] = "unchanged"
        else:
            document = json.loads(Path(proposal_path).read_text(encoding="utf-8"))
            approval_id = APPROVAL_PREFIX + str(document["content_sha256"])[:12]
            try:
                universe_apply.apply_proposal(
                    proposal_path, options.tickers_config, options.history,
                    approval_id=approval_id, applied_on=as_of,
                )
            except universe_apply.ApplyRefused as refused:
                print("error: " + " ".join(str(refused).split()), file=sys.stderr)
                return 3
            except OSError as exc:
                print("error: " + " ".join(str(exc).split()), file=sys.stderr)
                return 3
            summary["status"] = "applied"
            summary["approval_id"] = approval_id
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
