#!/usr/bin/env python3
"""Render the 3D market graph ("The Ocean") from any of this repo's OHLCV stores.

Emits a single self-contained HTML file -- three.js inlined, payload embedded, zero
network requests -- plus the payload JSON on its own so the matplotlib renderer under
``analysis/market-graph-3d/`` can reuse the identical normalized grid.

Deliberately a standalone script rather than a flag on ``run_dca.main()``: that CLI is
the trading pipeline, refuses to run without ``--demo``, and returns 2 for anything it
does not expect. A visualization has no business inside those gates.

Examples::

    # The one source that works in a fresh checkout, no environment needed.
    python -m scripts.generate_market_graph --source csv --out /tmp/ocean.html

    # The full active universe against the real per-ticker archive.
    python -m scripts.generate_market_graph --source archive --tickers @config

    # Replay a completed scheduled run from its persisted broker responses.
    python -m scripts.generate_market_graph --source run-dir \\
        --run-dir "$DCA_CRON_TRACKING_ROOT/midday_dca/20260803_1230ET"

    # Order the ticker axis with the richer ml-forecaster behavioral grouping.
    python -m scripts.generate_market_graph --source archive --tickers @config \\
        --group-source file --groups-file /tmp/ticker_groups.json
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

# Allow both `python scripts/generate_market_graph.py` and `python -m
# scripts.generate_market_graph` to find the top-level modules. Index 1 rather than 0
# for the same reason ticker-advisor/pricing.py documents: position 0 is the script's
# own directory, and shadowing it would break sibling imports inside scripts/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(_REPO_ROOT))

import dca_market_graph as mg  # noqa: E402

# The trail bound and the seriation cap are POLICY, and policy lives in
# dca_market_graph so this CLI and analysis/market-graph-3d/render_market_graph.py
# cannot drift apart on it. Re-exported here only so --help can interpolate the number.
MAX_TRAIL = mg.MAX_TRAIL


def _parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:  # argparse renders this as a clean usage error
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def _trail_length(value: str) -> int:
    """argparse adapter over mg.validate_trail, preserving argparse's error style."""
    try:
        return mg.validate_trail(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _count(name: str):
    """argparse adapter factory over mg.validate_positive_int.

    Every count-shaped option goes through this rather than bare ``int``. ``--clusters
    0`` was accepted and then silently treated as though the flag were omitted, which
    answers an explicit grouping request with a different grouping and says nothing --
    the same shape as ``--trail 0`` rendering a blank pulse. The policy lives in
    dca_market_graph so this CLI, the matplotlib renderer, and cluster_tickers.py cannot
    drift apart on it.
    """
    def parse(value: str) -> int:
        try:
            return mg.validate_positive_int(value, name)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
    return parse


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.generate_market_graph",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", default="csv", choices=("csv", "archive", "intraday", "run-dir"),
        help="which OHLCV store to read (default: csv, the only one committed to git)",
    )
    parser.add_argument("--csv-path", type=Path, default=None, help="override for --source csv")
    parser.add_argument("--history-dir", type=Path, default=None, help="override for --source archive")
    parser.add_argument("--archive-dir", type=Path, default=None, help="override for --source intraday")
    parser.add_argument("--run-dir", type=Path, default=None, help="run directory for --source run-dir")

    parser.add_argument(
        "--tickers", default=None,
        help="comma-separated symbols, or '@config' for the active universe in "
             "config/tickers.json. Defaults to '@config' for --source archive and to "
             "everything the source offers otherwise",
    )
    parser.add_argument(
        "--all-tickers-in-archive", action="store_true",
        help="with --source archive, load every *.csv instead of the active universe. "
             "Off by default because the archive holds thousands of symbols, including "
             "ones long since excluded from the universe -- the same reasoning "
             "ml-forecaster/train.py applies to the identical choice.",
    )
    parser.add_argument("--start", type=_parse_day, default=None, help="first trading day (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_day, default=None, help="last trading day (YYYY-MM-DD)")
    parser.add_argument("--interval-minutes", type=_count("--interval-minutes"), default=5,
                        help="bar interval for provider sources")

    parser.add_argument("--mode", default="both", choices=("pulse", "wave", "both"),
                        help="which render mode the viewer opens in (default: both)")
    parser.add_argument("--title", default=mg.DEFAULT_TITLE, help=f"page title (default: {mg.DEFAULT_TITLE!r})")
    parser.add_argument(
        "--trail", type=_trail_length, default=20,
        help=f"pulse-mode trail length, in sessions (1-{MAX_TRAIL})",
    )
    parser.add_argument(
        "--single", action="store_true",
        help="single-stock framing: scale volume per ticker rather than as relative "
             "volume, and omit the market centroid",
    )
    parser.add_argument(
        "--drawdown-window", type=int, choices=(20, 252), default=None,
        help="show distance below each ticker's rolling high over 20 sessions or 252 sessions",
    )

    parser.add_argument("--group-source", default="returns", choices=("returns", "file", "none"),
                        help="how to order and band the ticker axis (default: returns)")
    parser.add_argument("--groups-file", type=Path, default=None,
                        help="grouping JSON for --group-source file (e.g. the ml-forecaster export)")
    parser.add_argument("--clusters", type=_count("--clusters"), default=None,
                        help="how many groups to cut (default: automatic)")

    parser.add_argument("--out", type=Path, default=None,
                        help="HTML output path (default: <$DCA_CRON_TRACKING_ROOT>/market_graph/"
                             "<range>/market_graph.html)")
    parser.add_argument("--json-out", type=Path, default=None, help="also write the payload JSON here")
    parser.add_argument("--no-inline-three", action="store_true",
                        help="omit the inlined three.js (testing only -- the page will not render)")
    return parser


def _resolve_tickers(spec: Optional[str]) -> Optional[List[str]]:
    """``None`` when unfiltered; otherwise the requested list, EMPTY included.

    An explicitly empty selection must not collapse to ``None``: that would turn "the
    caller asked for nothing" into "the caller did not filter", and quietly render the
    entire archive instead of refusing.
    """
    if spec is None:
        return None
    if spec.strip() == "@config":
        return mg.active_tickers_from_config()
    return [part.strip().upper() for part in spec.split(",") if part.strip()]


def _drop_tickers_absent_from_window(series: List[mg.BarSeries], start: Optional[date]) -> List[mg.BarSeries]:
    """Drop a ticker whose ENTIRE history predates ``start`` from the resolved roster.

    Only meaningful when the loader was called with ``start=None`` (the
    ``--drawdown-window`` full-history load above): the loader's own ``_window()``
    would otherwise have already excluded such a ticker at load time. Left in,
    ``build_payload(frame_start=start)`` still emits it as an all-null series --
    included in the ticker/resolution counts and left outside the grouping order
    for having nothing to correlate -- silently misreporting resolution and
    padding the axis with a name that draws nothing (Codex review, PR #631
    discussion_r3917083213: "Exclude tickers with no bars in the requested
    window"). ``start=None`` is a no-op: no window was requested, so nothing to
    drop by. A ticker WITH at least one in-window bar keeps its full history
    (untouched) for _rolling_highs()'s lookback -- this only removes tickers
    with none.
    """
    if start is None:
        return series
    return [s for s in series if any(bar.timestamp.date() >= start for bar in s.bars)]


def _window_for_grouping(series: List[mg.BarSeries], start: Optional[date]) -> List[mg.BarSeries]:
    """Trim each series to ``start`` for return-grouping input only, baseline kept.

    Mirrors ``dca_market_graph._window()`` (used by the loaders): keeps ONE prior
    bar before ``start`` as a calculation-only baseline. Return-correlation
    grouping computes each session's return against the PRECEDING close, so
    without that baseline the first displayed session has no return at all --
    for exactly ``MIN_OVERLAP_DAYS`` displayed sessions this produces one fewer
    dated return than the overlap-evidence threshold requires, silently zeroing
    every pairwise correlation (Codex review, PR #631 discussion_r3917083227:
    "Retain the baseline bar for return grouping"). ``start=None`` (no
    ``--start`` requested, or ``load_start`` above stayed ``None`` because no
    ``--drawdown-window`` was requested either) is a no-op: ``series`` already
    covers exactly what ``seriate_or_refuse`` should see. Callers are expected
    to run :func:`_drop_tickers_absent_from_window` on ``series`` first, so
    every series here has at least one in-window bar and this never returns an
    empty one.
    """
    if start is None:
        return series
    windowed = []
    for s in series:
        inside = tuple(bar for bar in s.bars if bar.timestamp.date() >= start)
        before = tuple(bar for bar in s.bars if bar.timestamp.date() < start)
        trimmed = (before[-1],) + inside if before else inside
        windowed.append(replace(s, bars=trimmed))
    return windowed


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # keep the house convention: return, never propagate
        return int(exc.code or 0)

    if args.group_source == "file" and args.groups_file is None:
        print("--group-source file requires --groups-file", file=sys.stderr)
        return 2

    # Universe resolution sits INSIDE the boundary: it reads and parses tickers.json
    # (for `@config` and for the archive default), so a bad $DCA_CONFIG_DIR or a
    # malformed config is ordinary bad user input and deserves the documented exit 1
    # rather than a traceback. OSError rather than FileNotFoundError so an unreadable
    # or misdirected config path is caught too -- same class of problem, same refusal.
    try:
        tickers = mg.resolve_universe(
            args.source,
            _resolve_tickers(args.tickers),
            all_tickers_in_archive=args.all_tickers_in_archive,
        )
        loader = mg.resolve_loader(
            args.source,
            csv_path=args.csv_path,
            history_dir=args.history_dir,
            archive_dir=args.archive_dir,
            run_dir=args.run_dir,
            tickers=tickers,
            interval_minutes=args.interval_minutes,
        )
        # A `--drawdown-window` render measures each bar against its rolling high
        # over the PRECEDING `window` sessions, computed by dca_market_graph._rolling_
        # highs() directly on the loaded series -- but _window() (inside loader.load())
        # keeps only ONE bar before `--start` (a prior-close baseline, not a drawdown
        # lookback), so the first `window - 1` displayed sessions could only see
        # whatever handful of bars happened to precede `start`, silently understating
        # every rolling high/drawdown for them (Codex review, PR #631 discussion_
        # r3914702425: a $200 peak followed by a pre-start $100 bar and a $90 first
        # displayed close should report ~55% drawdown but reported ~10%). Loading the
        # FULL available history when a drawdown window is requested gives
        # _rolling_highs() the real lookback; build_payload()'s own `frame_start=
        # args.start` below still trims the emitted axis back down to the requested
        # window afterward, so only the rolling-high/drawdown COMPUTATION sees the
        # extra history -- nothing extra is drawn.
        load_start = None if args.drawdown_window else args.start
        series = loader.load(tickers, start=load_start, end=args.end)
        # Drop any ticker with nothing inside the requested window before it
        # reaches summarize_resolution/build_payload -- see
        # _drop_tickers_absent_from_window's docstring. A no-op when
        # load_start == args.start (the loader already excluded such tickers
        # at load time in that case).
        series = _drop_tickers_absent_from_window(series, args.start)
        # The `load_start = None` above hands seriate_or_refuse() the same
        # unrequested pre-`--start` history it hands _rolling_highs() -- but
        # return-correlation grouping order and cluster bands SHOULD reflect
        # only the requested window, not bars build_payload(frame_start=...)
        # is about to hide from the reader entirely (Codex review, PR #631
        # follow-up comment on the drawdown-window fix above: "Keep return
        # grouping within the requested date window"). window_for_grouping()
        # below trims a COPY for that purpose only; `series` itself stays
        # full so _rolling_highs() keeps its real lookback.
        grouping_series = _window_for_grouping(series, args.start)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not series:
        print("error: no ticker resolved to usable history; nothing to draw", file=sys.stderr)
        return 1

    # Always report resolution. The committed CSV covers a handful of symbols, so a
    # whole-market render against it is thin -- a reader who is not told that will
    # conclude the market was quiet rather than that the data was missing.
    print(mg.summarize_resolution(tickers, series))

    # --single means single-stock FRAMING, and the framing is only honest for one name.
    # It switches the volume channel to a per-ticker log min-max, so every series is
    # stretched across the full X axis on its OWN scale. Drawn together that is actively
    # misleading: two glyphs at the same X have nothing in common, though the scene
    # invites exactly that reading. The default CSV resolves five tickers, so plain
    # `--single` produced such a scene today.
    if args.single and len(series) != 1:
        print(
            f"error: --single is single-stock framing and scales volume per ticker, so it "
            f"needs exactly one resolved series; got {len(series)} "
            f"({', '.join(s.ticker for s in series[:6])}"
            f"{', ...' if len(series) > 6 else ''}). Narrow it with --tickers, or drop "
            "--single for a comparable whole-market scale.",
            file=sys.stderr,
        )
        return 1

    # Grouping resolution shares the loaders' error boundary: a missing or malformed
    # --groups-file is ordinary bad user input and deserves the documented exit 1, not
    # a traceback.
    try:
        if args.group_source == "file":
            grouping = mg.grouping_from_mapping(mg.load_groups_file(args.groups_file), series)
        elif args.group_source == "returns":
            grouping = mg.seriate_or_refuse(grouping_series, clusters=args.clusters)
        else:
            grouping = None
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        payload = mg.build_payload(
            series,
            mode=args.mode,
            whole_market=not args.single,
            grouping=grouping,
            title=args.title,
            source=args.source,
            frame_start=args.start,
            drawdown_window=args.drawdown_window,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload.meta["default_trail"] = args.trail

    out_html = args.out
    if out_html is None:
        out_html = mg.default_output_dir(payload) / "market_graph.html"

    from dca_fileio import atomic_write_text

    out_html.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        out_html,
        mg.build_viewer_html(payload, title=args.title, self_contained=not args.no_inline_three),
    )
    print(f"wrote {out_html}")

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(args.json_out, mg.build_payload_json(payload, indent=2) + "\n")
        print(f"wrote {args.json_out}")

    print(
        f"{len(payload.tickers)} series · {len(payload.dates)} sessions · "
        f"grouping={payload.grouping.source} · groups={len(payload.grouping.clusters)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
