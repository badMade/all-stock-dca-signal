#!/usr/bin/env python3
"""Offline matplotlib renderer for the 3D market graph ("The Ocean").

A sibling of the browser viewer, not a replacement for it. The emitted HTML is the
better artifact for exploring -- you can orbit it, scrub it, and hover a glyph. This
renderer exists for the two things a browser page cannot do: drop a still into a
document, and hand someone a video file they can scrub frame by frame.

**It deliberately does not reimplement any of the maths.** Loading, normalization,
seriation, and clustering all come from the repo-root ``dca_market_graph`` module, which
is standard-library only and therefore importable from here without dragging pandas into
the runtime tree. Two renderers computing their own normalization would drift, and the
whole point of the shared payload is that the still and the web page agree.

Lives under ``analysis/`` because it needs matplotlib. That tree is excluded from the
core repo's ruff, coverage, and pytest gates and carries its own requirements.txt.

Usage::

    pip install -r requirements.txt

    # Straight from a data source, same flags as the HTML CLI.
    python render_market_graph.py --source csv --tickers AAPL,MSFT,GOOGL,AMZN

    # Or from a payload the HTML CLI already wrote -- guaranteed identical geometry.
    python render_market_graph.py --payload /tmp/payload.json --out-dir /tmp/ocean

    # Animated pulse. MP4 needs ffmpeg on PATH; gif needs only Pillow.
    python render_market_graph.py --source archive --tickers @config --format gif
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")  # headless: this script only ever writes files
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import animation  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

import dca_market_graph as mg  # noqa: E402

# Matches viz/market_graph.css so a still and the web page are recognisably the
# same chart rather than two different-looking things claiming to be one.
BG = "#0b0e14"
INK = "#dbe2ee"
INK_DIM = "#8a93a5"
UP = "#4fbf88"
DOWN = "#e2688f"
FLAT = "#7d879b"

DIVERGING = LinearSegmentedColormap.from_list("ocean", [DOWN, FLAT, UP])

#: Clip extents, mirroring the viewer's pulseClamp/waveClamp. Without this the
#: highest-volatility names draw glyphs several times larger than everything else and
#: swallow the scene -- see viz/market_graph.js's comment at the same constant.
CLAMP = 2.0


def _trail_length(value: str) -> int:
    """argparse adapter over ``mg.validate_trail``.

    Without this, ``--trail 0`` reached ``_draw_pulse`` unchanged, its age loop ran zero
    iterations, and the command exited 0 having written a blank Pulse -- a silent
    success that produced nothing.
    """
    try:
        return mg.validate_trail(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _count(name: str):
    """argparse adapter factory over ``mg.validate_positive_int``.

    ``--fps 0`` used to travel all the way through rendering before ``PillowWriter``
    divided ``1000 / fps`` and died on a traceback, after the expensive part was already
    done; a negative rate is meaningless to every animation writer, and ``--stride 0``
    raises from ``range``. The policy is shared with the HTML CLI rather than copied,
    for the same reason ``_trail_length`` delegates.
    """
    def parse(value: str) -> int:
        try:
            return mg.validate_positive_int(value, name)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
    return parse


def _span(payload: dict) -> str:
    """Filesystem-safe span for output filenames, via the shared policy helper.

    ``mg.artifact_span`` takes the dataclass; this renderer works from the plain dict
    form, so hand it the one field that matters. Doing it through the shared helper
    rather than inline is the point: intraday labels carry "HH:MM", and a colon is
    invalid in a Windows filename.
    """
    return mg.artifact_span(
        mg.MarketGraphPayload(
            title="", dates=tuple(payload["dates"]), tickers=(), series=(),
            scales={}, grouping=mg.TickerGrouping(order=()),
        )
    )


def _style(axes, title: str) -> None:
    axes.set_facecolor(BG)
    axes.figure.set_facecolor(BG)
    for pane in (axes.xaxis, axes.yaxis, axes.zaxis):
        pane.set_pane_color((0.04, 0.05, 0.08, 1.0))
        pane.label.set_color(INK_DIM)
    axes.tick_params(colors=INK_DIM, labelsize=7)
    axes.set_title(title, color=INK, fontsize=13, pad=14)
    axes.grid(color="#1a2130", linewidth=0.4)


def _series_array(payload: dict, key: str) -> np.ndarray:
    """(ticker, frame) float array with NaN where the ticker did not trade.

    NaN rather than zero is deliberate and matches the viewer: a substituted zero is a
    fabricated flat session, which in a chart about market structure is a lie.
    """
    rows = [
        [np.nan if value is None else float(value) for value in entry[key]]
        for entry in payload["series"]
    ]
    return np.asarray(rows, dtype=float)


def _real_tickers(payload: dict) -> list[int]:
    """Indices of the genuine tickers, excluding the synthetic market centroid.

    Refuses an empty result rather than returning one. Every caller slices a numpy array
    with this list, and an empty slice yields a ``(0, N)`` array that reaches
    ``plot_surface`` and fails with a shape error naming neither the payload nor the
    cause. build_payload() cannot produce a centroid-only payload -- it appends the
    centroid only alongside real series -- but ``--payload`` is an external document and
    can say anything.
    """
    keep = [i for i, entry in enumerate(payload["series"]) if entry["ticker"] != mg.MARKET_SERIES]
    if not keep:
        raise ValueError(
            "payload contains no real ticker series to render -- only the synthetic "
            f"{mg.MARKET_SERIES} centroid"
        )
    return keep


def render_wave(payload: dict, out_path: Path, *, elev: float = 34, azim: float = -62) -> Path:
    """Time-as-axis terrain: X time, Z ticker (seriated), Y the high/low envelope."""
    keep = _real_tickers(payload)
    highs = np.clip(_series_array(payload, "yh")[keep], -CLAMP, CLAMP)
    lows = np.clip(_series_array(payload, "yl")[keep], -CLAMP, CLAMP)
    opens = _series_array(payload, "zo")[keep]
    closes = _series_array(payload, "zc")[keep]

    # Carry-forward is bounded at BOTH ends and applies only to interior gaps, matching
    # viz/market_graph.js's validity mask exactly. Before a ticker's first observation
    # there is nothing to carry; after its last, carrying on would make a delisted or
    # stale series appear to keep trading flat to the end of the axis. Both ends keep
    # their NaN, and matplotlib omits any quad touching one -- so the surface begins and
    # ends where the data does.
    for grid in (highs, lows, opens, closes):
        for row in grid:
            real = np.flatnonzero(~np.isnan(row))
            if real.size == 0:
                continue
            last_real = real[-1]
            last = np.nan
            for i, value in enumerate(row[: last_real + 1]):
                if np.isnan(value):
                    row[i] = last
                else:
                    last = value

    # One ticker means one mesh row, and plot_surface needs two adjacent rows to form
    # any polygon at all -- it would write a PNG containing axes and no market. Extrude
    # the lone row into a narrow ribbon, mirroring the browser renderer's `ribbon`
    # handling in viz/market_graph.js::buildWave.
    if highs.shape[0] == 1:
        highs, lows, opens, closes = (np.repeat(g, 2, axis=0) for g in (highs, lows, opens, closes))
    # The identical argument on the time axis, which the row fix above originally missed.
    # A one-session window (`--start`/`--end` on the same day) is a supported request --
    # build_payload deliberately emits that single frame -- and one column forms no quad
    # either, so it becomes a narrow strip. Both renderers now extrude a degenerate
    # dimension rather than silently writing an empty picture; see the matching `strip`
    # flag in viz/market_graph.js::buildWave.
    if highs.shape[1] == 1:
        highs, lows, opens, closes = (np.repeat(g, 2, axis=1) for g in (highs, lows, opens, closes))

    rows, cols = highs.shape
    time_axis, ticker_axis = np.meshgrid(np.arange(cols), np.arange(rows))
    body = np.clip((closes - opens) * 2.2, -1, 1)
    colors = DIVERGING(np.nan_to_num((body + 1) / 2, nan=0.5))

    # Axis-letter note: matplotlib's third argument is its VERTICAL axis (Z), whereas
    # three.js is Y-up. Passing (time, ticker, high/low) therefore produces the same
    # picture as the browser's Wave mode -- height is the high/low band, the ground
    # plane is time x ticker -- even though the letters differ between the two
    # conventions. The axis LABELS below describe what is drawn, which is what matters
    # to a reader; do not "fix" the argument order to match three.js's letters.

    figure = plt.figure(figsize=(15, 9), facecolor=BG)
    axes = figure.add_subplot(111, projection="3d")
    for surface in (highs, lows):
        axes.plot_surface(
            time_axis, ticker_axis, surface, facecolors=colors,
            rstride=1, cstride=max(1, cols // 260), linewidth=0, antialiased=False,
            shade=True, alpha=0.9,
        )

    axes.set_xlabel("time →", labelpad=10)
    axes.set_ylabel("ticker (grouped by behaviour) →", labelpad=10)
    axes.set_zlabel("high / low", labelpad=6)
    axes.set_box_aspect((2.5, 1.1, 0.55))
    axes.view_init(elev=elev, azim=azim)
    _style(axes, f"{payload['title']} — Wave · {payload['dates'][0]} to {payload['dates'][-1]}")

    figure.text(0.5, 0.03, _caption(payload), ha="center", color=INK_DIM, fontsize=9)
    figure.savefig(out_path, dpi=140, facecolor=BG, bbox_inches="tight")
    plt.close(figure)
    return out_path


def _pulse_figure(payload: dict):
    figure = plt.figure(figsize=(13, 9), facecolor=BG)
    axes = figure.add_subplot(111, projection="3d")
    axes.set_xlabel("volume →", labelpad=10)
    axes.set_ylabel("high / low", labelpad=10)
    axes.set_zlabel("open / close", labelpad=6)
    axes.set_xlim(0, 1)
    axes.set_ylim(-CLAMP, CLAMP)
    axes.set_zlim(-CLAMP, CLAMP)
    axes.set_box_aspect((1.5, 1, 1))
    axes.view_init(elev=22, azim=-58)
    return figure, axes


def render_pulse_frame(payload: dict, frame: int, out_path: Path, *, trail: int = 20) -> Path:
    """One still of the animated glyph cloud, at a given session index."""
    figure, axes = _pulse_figure(payload)
    _draw_pulse(payload, axes, frame, trail)
    _style(axes, f"{payload['title']} — Pulse · {payload['dates'][frame]}")
    figure.text(0.5, 0.03, _caption(payload), ha="center", color=INK_DIM, fontsize=9)
    figure.savefig(out_path, dpi=140, facecolor=BG, bbox_inches="tight")
    plt.close(figure)
    return out_path


def _draw_pulse(payload: dict, axes, frame: int, trail: int) -> None:
    """Scatter the trail. Marker size carries the day's range, colour the body.

    matplotlib's 3D backend has no efficient instanced-box primitive, so where the
    browser viewer draws a true box per (ticker, session) this draws a sized marker.
    The channels are identical; only the glyph differs.
    """
    keep = _real_tickers(payload)
    xs = _series_array(payload, "x")[keep]
    highs = np.clip(_series_array(payload, "yh")[keep], -CLAMP, CLAMP)
    lows = np.clip(_series_array(payload, "yl")[keep], -CLAMP, CLAMP)
    opens = np.clip(_series_array(payload, "zo")[keep], -CLAMP, CLAMP)
    closes = np.clip(_series_array(payload, "zc")[keep], -CLAMP, CLAMP)

    for age in range(min(trail, frame + 1) - 1, -1, -1):
        index = frame - age
        fade = age / max(1, trail)
        mask = ~np.isnan(xs[:, index])
        if not mask.any():
            continue
        body = np.clip((closes[mask, index] - opens[mask, index]) * 2.2, -1, 1)
        axes.scatter(
            xs[mask, index],
            (lows[mask, index] + highs[mask, index]) / 2,
            (opens[mask, index] + closes[mask, index]) / 2,
            s=18 + 220 * np.abs(highs[mask, index] - lows[mask, index]),
            c=DIVERGING((body + 1) / 2),
            alpha=max(0.06, 0.92 * (1 - fade)),
            edgecolors="none",
            depthshade=False,
        )


def render_pulse_animation(
    payload: dict, out_path: Path, *, trail: int = 20, fps: int = 20, stride: int = 1
) -> Path:
    """Animate the glyph cloud across every session."""
    figure, axes = _pulse_figure(payload)
    last = len(payload["dates"]) - 1
    frames = list(range(0, len(payload["dates"]), max(1, stride)))
    # The stride sequence lands on the final session only when (n-1) is a multiple of
    # it: 12 sessions at --stride 5 give frames 0, 5, 10 and stop one short. The file is
    # still NAMED with the payload's full first-to-last span, so dropping the last frame
    # makes the filename a claim the animation does not honour -- and the final market
    # state is the one a reader is most likely to have come for.
    if last >= 0 and frames[-1] != last:
        frames.append(last)
    caption = figure.text(0.5, 0.03, _caption(payload), ha="center", color=INK_DIM, fontsize=9)

    def draw(frame: int):
        axes.clear()
        axes.set_xlim(0, 1)
        axes.set_ylim(-CLAMP, CLAMP)
        axes.set_zlim(-CLAMP, CLAMP)
        axes.set_xlabel("volume →", labelpad=10)
        axes.set_ylabel("high / low", labelpad=10)
        axes.set_zlabel("open / close", labelpad=6)
        _draw_pulse(payload, axes, frame, trail)
        _style(axes, f"{payload['title']} — Pulse · {payload['dates'][frame]}")
        return [caption]

    writer = _writer_for(out_path, fps)
    anim = animation.FuncAnimation(figure, draw, frames=frames, blit=False)
    anim.save(str(out_path), writer=writer, savefig_kwargs={"facecolor": BG})
    plt.close(figure)
    return out_path


def _writer_for(out_path: Path, fps: int):
    """Pick a writer, and fail with a fixable message rather than a stack trace."""
    if out_path.suffix == ".gif":
        return animation.PillowWriter(fps=fps)
    if animation.FFMpegWriter.isAvailable():
        return animation.FFMpegWriter(fps=fps, bitrate=2600)
    raise SystemExit(
        "MP4 output needs an ffmpeg binary on PATH, and none was found. "
        "Install ffmpeg, or re-run with --format gif (Pillow only)."
    )


def _caption(payload: dict) -> str:
    # Count real symbols only: meta.ticker_count includes the synthetic __MARKET__
    # centroid, and captioning a 42-name universe as 43 tickers is simply wrong.
    real = len([t for t in payload["tickers"] if t != mg.MARKET_SERIES])
    return (
        f"{real} tickers · {len(payload['dates'])} sessions · "
        # .get(), not a subscript: `source` is OPTIONAL in the payload schema, and a
        # --payload document that omits it used to raise KeyError here on every render.
        f"source {payload.get('source') or 'unknown'} · "
        f"grouping {payload['grouping']['source']} · "
        f"1 unit ≈ {payload['scales']['pct_per_unit'] * 100:.2f}% vs prior close"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_argument_group("input (pick one)")
    source.add_argument("--payload", type=Path, default=None,
                        help="a payload JSON already written by scripts/generate_market_graph.py")
    source.add_argument("--source", default=None, choices=("csv", "archive", "intraday", "run-dir"),
                        help="build a payload directly from an OHLCV store")
    source.add_argument("--csv-path", type=Path, default=None)
    source.add_argument("--history-dir", type=Path, default=None)
    source.add_argument("--archive-dir", type=Path, default=None)
    source.add_argument("--run-dir", type=Path, default=None)
    source.add_argument("--tickers", default=None, help="comma-separated symbols, or '@config'")
    source.add_argument("--groups-file", type=Path, default=None,
                        help="grouping JSON (e.g. the ml-forecaster/cluster_tickers.py export)")
    source.add_argument("--all-tickers-in-archive", action="store_true",
                        help="with --source archive, load every *.csv instead of the active universe")

    parser.add_argument("--mode", default="both", choices=("pulse", "wave", "both"))
    parser.add_argument("--format", default="png", choices=("png", "gif", "mp4"),
                        help="pulse output format; wave is always a PNG still (default: png)")
    source.add_argument("--interval-minutes", type=_count("--interval-minutes"), default=5,
                        help="bar interval for the provider and run-dir sources (default: 5)")

    parser.add_argument("--frame", type=int, default=-1,
                        help="session index for a pulse still; negative counts back from "
                             "the end (default: -1, the last session)")
    parser.add_argument("--trail", type=_trail_length, default=20,
                        help=f"pulse trail length, in sessions (1-{mg.MAX_TRAIL})")
    parser.add_argument("--fps", type=_count("--fps"), default=20, help="animation frame rate (>= 1)")
    parser.add_argument("--stride", type=_count("--stride"), default=1,
                        help="render every Nth session when animating (>= 1)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where to write the images (default: the shared artifact root, "
                             "matching the HTML CLI)")
    parser.add_argument("--title", default=mg.DEFAULT_TITLE)
    return parser


def _payload_from_args(args) -> dict:
    if args.payload is not None:
        # Validated, not trusted. --payload is the one input to this renderer that came
        # from somewhere else, and the array semantics are versioned: a document from a
        # future schema would not fail here, it would draw a plausible and wrong
        # picture. The browser viewer already refuses an unknown schema_version for
        # exactly this reason, and both consumers now share the one check.
        return mg.validate_payload_document(json.loads(args.payload.read_bytes()))

    # Ticker resolution and the archive default come from the shared module, not from
    # a second copy here: the HTML CLI defaults --source archive to the active universe
    # precisely so a ~6000-symbol load never reaches the O(n^3) seriation, and this
    # renderer used to bypass that and appear to hang.
    requested = None
    if args.tickers == "@config":
        requested = mg.active_tickers_from_config()
    elif args.tickers is not None:
        requested = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    tickers = mg.resolve_universe(
        args.source, requested, all_tickers_in_archive=args.all_tickers_in_archive
    )

    loader = mg.resolve_loader(
        args.source, csv_path=args.csv_path, history_dir=args.history_dir,
        archive_dir=args.archive_dir, run_dir=args.run_dir, tickers=tickers,
        # Forwarded, not left at the default: the frozen replay provider filters
        # archived entries by EXACT interval, so a run recorded at 10- or 30-minute bars
        # returned nothing and this documented direct-render path refused a run
        # directory holding perfectly usable history.
        interval_minutes=args.interval_minutes,
    )
    series = loader.load(tickers)
    if not series:
        raise SystemExit("no ticker resolved to usable history; nothing to draw")
    print(mg.summarize_resolution(tickers, series))

    if args.groups_file is not None:
        grouping = mg.grouping_from_mapping(mg.load_groups_file(args.groups_file), series)
    else:
        grouping = mg.seriate_or_refuse(series)

    return mg.payload_to_dict(
        mg.build_payload(series, whole_market=True, grouping=grouping,
                         title=args.title, source=args.source)
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.payload is None and args.source is None:
        print("pass either --payload or --source", file=sys.stderr)
        return 2

    # The boundary the HTML CLI has had for two rounds, which this renderer never got.
    # It covers RENDERING as well as loading, not just the read: the renderers refuse a
    # payload they cannot draw (no real ticker series, say), and a refusal that reaches
    # the operator as a traceback is no better than the cryptic numpy shape error it
    # replaced. Everything from here on consumes external input -- the payload document,
    # tickers.json, a grouping file, the archive -- so it all deserves the documented
    # exit 1.
    try:
        return _render(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _render(args) -> int:
    payload = _payload_from_args(args)
    # Same default as the HTML CLI: the shared artifact root rather than the process's
    # working directory, so a scheduled render lands somewhere findable.
    out_dir = args.out_dir if args.out_dir is not None else mg.default_output_dir(
        mg.MarketGraphPayload(
            title="", dates=tuple(payload["dates"]), tickers=(), series=(),
            scales={}, grouping=mg.TickerGrouping(order=()),
        )
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir = out_dir
    span = _span(payload)

    if args.mode in ("wave", "both"):
        path = render_wave(payload, args.out_dir / f"wave_{span}.png")
        print(f"wrote {path}")

    if args.mode in ("pulse", "both"):
        # Negative indexes count back from the end, Python-style: -1 the last session,
        # -2 the penultimate. Previously EVERY negative value collapsed to the last one,
        # which made --frame -2 silently render something other than what was asked for.
        # Worth noting how that survived: dca_market_graph.validate_positive_int's
        # docstring cites exactly these semantics as the reason --frame is exempt from
        # positive-int validation -- a justification written for behaviour the code had
        # never implemented.
        frame = args.frame if args.frame >= 0 else len(payload["dates"]) + args.frame
        frame = max(0, min(frame, len(payload["dates"]) - 1))
        if args.format == "png":
            path = render_pulse_frame(payload, frame, args.out_dir / f"pulse_{span}.png", trail=args.trail)
        else:
            path = render_pulse_animation(
                payload, args.out_dir / f"pulse_{span}.{args.format}",
                trail=args.trail, fps=args.fps, stride=args.stride,
            )
        print(f"wrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
