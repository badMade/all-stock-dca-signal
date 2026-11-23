"""The daily universe job (feed step 7b-2): rank, then apply the passing list to this checkout.

Specified first by tests/test_daily_universe_job.py. It is thin glue the Mac scheduler runs once per weekday
evening. It chains :mod:`daily_ranking` (propose-only) and :func:`universe_apply.apply_proposal` (the gated writer)
under the standing approval in docs/adr-program-maintained-universe.md: a proposal that passed EVERY safeguard is
applied; anything else changes nothing.

This is a BUY-list job only. A name that leaves the list stops being bought (``stopped_buying``); nothing is ever
sold because of rank, and this module imports no broker, order, runner or network code.

Without ``--apply`` it is exactly ``daily_ranking`` (outputs in ``--out-dir`` only). With ``--apply`` it also needs
``--history`` (the ``ticker_history.csv`` the writer appends to). ``--as-of`` may be omitted: the latest bar date in
the price archive is used, because a scheduler cannot compute "the last completed session".

Exit codes: 0 passed (applied, unchanged, or proposed without ``--apply``); 1 the proposal was REJECTED by a
safeguard; 2 bad input; 3 the writer REFUSED a passing proposal. On 1, 2 and 3 the live files are untouched. The last
stdout line is one JSON summary.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from datetime import date
from pathlib import Path

import daily_ranking
import universe_apply
from price_factors import load_price_history
from universe_inputs import load_snapshot

APPROVAL_ID_PREFIX = "auto:adr-program-maintained-universe:"


def _build_parser():
    parser = daily_ranking._build_parser(as_of_required=False)
    parser.prog = "daily_universe_job"
    parser.description = (
        "Rank the pool and, with --apply, write the passing buy list into this checkout's tickers.json. Buy list "
        "only: nothing is ever sold."
    )
    parser.add_argument("--history", help="ticker_history.csv to append the new list to (required with --apply)")
    parser.add_argument("--apply", action="store_true", help="apply a proposal that passed every safeguard")
    return parser


def _latest_bar_date(snapshot_path: str, prices_path: str) -> date:
    """The latest session in the price archive for the snapshot's symbols (``ValueError`` when there is none)."""
    snapshot = load_snapshot(snapshot_path)
    histories = load_price_history(prices_path, tickers=snapshot["symbols"])
    latest = max((bars[-1].date for bars in histories.values() if bars), default=None)
    if latest is None:
        raise ValueError("the price archive holds no bars for the snapshot's symbols")
    return latest


def _passthrough(argv: list[str], as_of: date) -> list[str]:
    """``daily_ranking``'s argv: ours minus the job's own flags, with the resolved ``--as-of``."""
    out: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token == "--apply":
            continue
        if token in ("--history", "--as-of"):
            skip = True
            continue
        if token.startswith(("--history=", "--as-of=")):
            continue
        out.append(token)
    return [*out, "--as-of", as_of.isoformat()]


def _summary_line(captured: str) -> dict:
    return json.loads(captured.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    """Run the job. Returns 0 (passed), 1 (rejected), 2 (bad input) or 3 (the writer refused)."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    options = parser.parse_args(arguments)
    if options.apply and not options.history:
        parser.error("--apply needs --history")
    try:
        as_of = (daily_ranking._parse_date(options.as_of, "--as-of") if options.as_of
                 else _latest_bar_date(options.snapshot, options.prices))
    except (daily_ranking._InputError, ValueError, OSError) as exc:
        print("error: " + " ".join(str(exc).split()), file=sys.stderr)
        return 2

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = daily_ranking.main(_passthrough(arguments, as_of))
    if code == 2:
        return 2
    summary = _summary_line(buffer.getvalue())
    if code != 0 or not options.apply:
        print(json.dumps(summary, sort_keys=True))
        return code

    proposal = json.loads(Path(summary["proposal"]).read_text(encoding="utf-8"))
    if set(proposal["active"]) == set(proposal["prior_active"]):
        # Nothing to change: no writer call and no history rows, so a same-day rerun never duplicates history.
        print(json.dumps({**summary, "status": "unchanged"}, sort_keys=True))
        return 0
    approval_id = APPROVAL_ID_PREFIX + proposal["content_sha256"][:12]
    try:
        result = universe_apply.apply_proposal(
            summary["proposal"], options.tickers_config, options.history, approval_id=approval_id, applied_on=as_of
        )
    except universe_apply.ApplyRefused as refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 3
    except OSError as exc:
        print("error: " + " ".join(str(exc).split()), file=sys.stderr)
        return 3
    print(json.dumps({**summary, "status": "applied", "approval_id": approval_id,
                      "added": sorted(result.added), "stopped_buying": sorted(result.stopped_buying)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
