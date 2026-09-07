#!/usr/bin/env python3
"""CLI: export a behavioral ticker grouping for the 3D market graph ("The Ocean").

The market graph's whole-market mode orders its ticker axis so that similar-behaving
names sit adjacent -- otherwise a sector-wide move appears as spikes sprinkled through
the scene instead of one contiguous swell, which is the entire pattern the chart exists
to show.

``dca_market_graph.seriate_tickers()`` already does a usable version of this from
close-to-close return correlation alone, in pure standard library, with no dependencies
and no archive. That captures *"moves together"*. This tool produces the better
grouping: *"behaves like"*.

The difference is what it clusters on. This subproject already engineers a no-lookahead
daily feature vector per (ticker, day) for the sizing model -- realized volatility, RSI
posture, drawdown from the 52-week high, trailing returns, relative volume. Two tickers
sharing a volatility regime, drawdown character, and volume profile behave alike even
when their day-to-day correlation is unremarkable. Clustering on a blend of both
notions gives an axis ordering that survives a regime change.

**Why this is a separate CLI and not a function the viewer calls.** ``ml-forecaster/``
carries pandas, numpy, scikit-learn, and scipy. ``dca_market_graph`` is a repo-root
module and is bound by CLAUDE.md's "standard library only at runtime" invariant, so it
can never import this. The interface is therefore a file: this tool writes
``ticker_groups.json`` and the viewer reads it via ``--group-source file``. The stdlib
correlation grouping remains the default, so the graph always works without this tool.

This is a strictly read-only consumer of ``features.build_feature_frame``. It never
writes ``config/ml_sizing_adjustment.json`` and has no path to the guarded-publish
quality gate -- it cannot affect what the strategy actually sizes.

Usage::

    python cluster_tickers.py
        # Active universe from config/tickers.json, written under the artifact tree
        # at <cron root>/market_graph_groups/<date>/ticker_groups.json

    python cluster_tickers.py --out /tmp/ticker_groups.json --clusters 6
    python cluster_tickers.py --alpha 1.0     # correlation only, ignore the profile
    python cluster_tickers.py --alpha 0.0     # profile only, ignore correlation

Then::

    python -m scripts.generate_market_graph --source archive --tickers @config \\
        --group-source file --groups-file /tmp/ticker_groups.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, leaves_list, linkage, optimal_leaf_ordering
from scipy.spatial.distance import pdist, squareform

from features import (
    NUMERIC_FEATURE_COLUMNS,
    build_feature_frame,
    configured_ticker_history_dir,
    discover_available_tickers,
)

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
# Index 1, not 0 -- see features.py's identical comment on why this subproject's own
# directory must stay ahead of the repo root on sys.path.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_market_graph import CLUSTER_COLORS, normalize_roster, validate_positive_int  # noqa: E402
from dca_paths import stream_root  # noqa: E402
from train import DEFAULT_TICKERS_CONFIG_PATH, _load_active_tickers  # noqa: E402

#: Default blend between the two notions of similarity. 0.5 weights "moves together"
#: and "behaves like" equally; 1.0 is pure correlation, 0.0 is pure behavioral profile.
DEFAULT_ALPHA = 0.5

#: Minimum shared trading days before a pair's correlation is trusted. Mirrors
#: dca_market_graph.MIN_OVERLAP_DAYS so the two tiers agree about what counts as
#: evidence rather than one of them inventing structure the other refuses to.
MIN_OVERLAP_DAYS = 30

#: Minimum rows a ticker needs before it can carry a behavioral profile at all. The
#: feature frame already drops rolling-window warmup rows, so a ticker below this has
#: essentially no post-warmup history.
MIN_ROWS_PER_TICKER = 40


def _profile_matrix(features: pd.DataFrame) -> tuple[list[str], np.ndarray]:
    """Per-ticker behavioral profile: mean and std of each feature, z-scored.

    Mean captures the ticker's typical regime ("this name usually runs at 45% annualized
    vol"); std captures how much that regime itself moves ("...but it swings between 20%
    and 80%"). Both matter for whether two names *behave* alike.

    Z-scoring across tickers is not optional: ``rsi_14`` lives on 0-100 while
    ``trailing_return_5d_pct`` sits near zero, so an un-standardized Euclidean distance
    would be a pure RSI distance with the other six features as rounding error.
    """
    grouped = features.groupby("ticker")[list(NUMERIC_FEATURE_COLUMNS)]
    profile = pd.concat([grouped.mean(), grouped.std().fillna(0.0)], axis=1)
    profile = profile.sort_index()
    values = profile.to_numpy(dtype=float)
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    spread = values.std(axis=0)
    # A feature that is identical across every ticker carries no information; leaving
    # its zero spread in the denominator would produce inf/NaN distances.
    spread[spread <= 0] = 1.0
    return list(profile.index), (values - values.mean(axis=0)) / spread


def _correlation_distance(features: pd.DataFrame, tickers: list[str]) -> np.ndarray:
    """``sqrt(2 * (1 - corr))`` over close-to-close returns -- a true metric.

    Pairs sharing fewer than ``MIN_OVERLAP_DAYS`` sessions are forced to zero
    correlation. A handful of shared days can produce a 0.99 that would place two
    unrelated names adjacent and invent a sector that does not exist.
    """
    closes = features.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    closes = closes.reindex(columns=tickers)
    # fill_method=None is load-bearing. pandas' default forward-fills the price before
    # differencing, which manufactures a 0% return on every session a ticker did not
    # trade -- counting an absence as observed overlap -- and then dumps the whole
    # multi-session move onto the next real print. Both distortions land hardest on
    # exactly the halted, newly listed, and thinly traded names whose grouping is
    # already the least certain.
    returns = closes.pct_change(fill_method=None)

    correlation = returns.corr(min_periods=MIN_OVERLAP_DAYS).to_numpy(dtype=float)
    correlation = np.nan_to_num(correlation, nan=0.0)
    np.fill_diagonal(correlation, 1.0)
    correlation = np.clip(correlation, -1.0, 1.0)
    return np.sqrt(np.maximum(0.0, 2.0 * (1.0 - correlation)))


def _normalize(matrix: np.ndarray) -> np.ndarray:
    """Scale a distance matrix onto [0, 1] so the two can be blended meaningfully."""
    peak = float(matrix.max())
    return matrix / peak if peak > 0 else matrix


#: Volatility bands, as the 20-day standard deviation of DAILY returns in percent --
#: the units features.py's realized_vol_20d actually uses (`.std(ddof=0) * 100.0` over
#: `close.pct_change()`), NOT an annualized fraction. Getting this wrong is silent: every
#: band collapses to one label and the legend stops distinguishing anything.
#: For reference, ~1%/day is a megacap, ~3%/day a semi, ~6%/day a speculative name.
_VOL_BANDS: tuple[tuple[float, str], ...] = (
    (4.0, "very high vol"),
    (2.2, "high vol"),
    (1.0, "moderate vol"),
    (0.0, "low vol"),
)


def _label_for(members: list[str], features: pd.DataFrame) -> str:
    """A label that says something true about the group rather than just numbering it.

    Volatility is the single most legible axis for "what kind of names are these", and
    it is what a reader is actually trying to identify when they look at a band on the
    ticker axis. Anything more inferential -- naming sectors -- would be this tool
    asserting a fact about the world it has no data for.

    The median is quoted alongside the band so the label stays informative even when
    two adjacent groups land in the same band.
    """
    subset = features[features["ticker"].isin(members)]
    if subset.empty:
        return f"{len(members)} tickers"
    vol = float(subset["realized_vol_20d"].median())
    band = next(name for threshold, name in _VOL_BANDS if vol >= threshold)
    return f"{band} {vol:.1f}%/d ({len(members)})"


def build_grouping(
    features: pd.DataFrame,
    *,
    alpha: float = DEFAULT_ALPHA,
    clusters: int | None = None,
    resolved: list[str] | None = None,
) -> dict:
    """Cluster and seriate the feature frame into a market-graph grouping document.

    ``resolved`` is the roster ``discover_available_tickers`` produced before the
    feature frame was built -- every ticker with an archived CSV, whether or not it
    survived rolling-feature warmup. Without it, a ticker that had archived rows but
    ZERO surviving post-warmup rows never appears in ``counts`` at all (it is not merely
    a small count, it is absent), so the ``counts.index`` subtraction below could never
    have named it -- the grouping export silently dropped the symbol and the CLI never
    printed its "dropped (too little history)" warning. Optional and defaults to the old
    behavior for a caller that never had a resolved roster to hand in.
    """
    counts: Any = features.groupby("ticker").size()
    usable = sorted(counts[counts >= MIN_ROWS_PER_TICKER].index)
    dropped = sorted(set(resolved if resolved is not None else counts.index) - set(usable))
    if len(usable) < 2:
        raise SystemExit(
            f"need at least 2 tickers with >= {MIN_ROWS_PER_TICKER} post-warmup rows; "
            f"got {len(usable)}"
        )

    features = cast(pd.DataFrame, features[features["ticker"].isin(usable)])
    names, profile = _profile_matrix(features)

    profile_distance = _normalize(squareform(pdist(profile, metric="euclidean")))
    correlation_distance = _normalize(_correlation_distance(features, names))
    blended = alpha * correlation_distance + (1.0 - alpha) * profile_distance
    # Symmetry and a zero diagonal are assumptions scipy's linkage makes; floating-point
    # drift in the blend can violate both by a hair.
    blended = (blended + blended.T) / 2.0
    np.fill_diagonal(blended, 0.0)

    condensed = squareform(blended, checks=False)
    tree = linkage(condensed, method="average")
    # The real optimal-leaf-ordering, not the greedy approximation the stdlib tier has
    # to settle for -- this is the concrete payoff of running inside the ML venv.
    ordered_tree = optimal_leaf_ordering(tree, condensed)
    order = [names[i] for i in leaves_list(ordered_tree)]

    requested = clusters if clusters and clusters > 0 else None
    count = requested or max(2, min(8, round((len(names) / 2) ** 0.5)))
    assignments = fcluster(ordered_tree, t=min(count, len(names)), criterion="maxclust")

    # "maxclust" is a MAXIMUM, not a target. When linkage heights tie -- identical or
    # near-duplicate histories, which this archive genuinely contains -- scipy can hand
    # back fewer groups than asked for, and the run used to succeed while recording the
    # smaller number as though it were the answer.
    #
    # Refused rather than forced, and only when the count was asked for explicitly. With
    # truly tied distances there is no principled place to cut: any split between
    # identical series is invented, and inventing one is worse than saying so. The
    # automatic count promised nothing, so it accepts whatever the cut yields.
    #
    # Note this is specific to the scipy tier. dca_market_graph._cut_clusters splits the
    # loosest merge structurally and stops only when every cluster is a leaf, so the
    # stdlib tier always reaches the requested count -- the two tiers really do differ
    # here, and the fix does not belong on both sides.
    delivered = len(set(int(a) for a in assignments))
    if requested is not None and delivered < min(requested, len(names)):
        raise ValueError(
            f"--clusters {requested} cannot be satisfied: the dendrogram cut yields "
            f"{delivered} group(s) because linkage heights tie, which happens when "
            "tickers have identical or near-identical histories. Ask for at most "
            f"{delivered}, drop --clusters to take the automatic count, or narrow the "
            "roster with --tickers."
        )

    position = {name: i for i, name in enumerate(order)}
    by_cluster: dict[int, list[str]] = {}
    for name, cluster_id in zip(names, assignments, strict=True):
        by_cluster.setdefault(int(cluster_id), []).append(name)

    # Emit bands left-to-right along the ticker axis, so the legend reads in the same
    # order as the chart.
    bands = sorted(by_cluster.values(), key=lambda members: min(position[m] for m in members))
    payload_clusters = []
    for index, members in enumerate(bands):
        members = sorted(members, key=lambda m: position[m])
        payload_clusters.append({
            "id": index,
            "label": _label_for(members, features),
            "tickers": members,
            "color": CLUSTER_COLORS[index % len(CLUSTER_COLORS)],
        })

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "ml-forecaster",
        "params": {
            "alpha": alpha,
            "clusters": len(payload_clusters),
            "min_overlap_days": MIN_OVERLAP_DAYS,
            "min_rows_per_ticker": MIN_ROWS_PER_TICKER,
            "features": list(NUMERIC_FEATURE_COLUMNS),
        },
        "dropped_tickers": dropped,
        "ticker_order": order,
        "clusters": payload_clusters,
    }


def _count(name: str):
    """argparse adapter factory over the market graph's shared count policy.

    ``--clusters 0`` was accepted here and then silently treated as though the flag had
    been omitted, so an explicit grouping request came back with the automatic number of
    groups and no warning. Shared with both market-graph CLIs rather than reimplemented,
    since the same option exists in all three.
    """
    def parse(value: str) -> int:
        try:
            return validate_positive_int(value, name)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
    return parse


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--history-dir", type=Path, default=None,
                        help="ticker OHLCV archive (default: configured_ticker_history_dir())")
    parser.add_argument("--tickers", nargs="*", default=None,
                        help="explicit ticker list (default: config/tickers.json's active universe)")
    parser.add_argument("--tickers-config", type=Path, default=DEFAULT_TICKERS_CONFIG_PATH,
                        help="tickers.json to read the active universe from")
    parser.add_argument(
        "--all-tickers-in-archive", action="store_true",
        help="cluster every *.csv under --history-dir, ignoring config/tickers.json. Off by "
             "default for the same reason train.py defaults it off: archive maintenance does "
             "not delete a ticker's CSV when it leaves the universe, so an unfiltered glob "
             "would group stale and excluded symbols alongside live ones.",
    )
    parser.add_argument("--alpha", type=float, default=DEFAULT_ALPHA,
                        help="blend weight: 1.0 pure return correlation, 0.0 pure behavioral profile")
    parser.add_argument("--clusters", type=_count("--clusters"), default=None,
                        help="how many groups to cut (default: automatic)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output path (default: <cron root>/market_graph_groups/<date>/ticker_groups.json)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not 0.0 <= args.alpha <= 1.0:
        print(f"--alpha must be between 0.0 and 1.0, got {args.alpha}", file=sys.stderr)
        return 2

    history_dir = args.history_dir or configured_ticker_history_dir()
    if not Path(history_dir).is_dir():
        print(
            f"ticker history archive not found: {history_dir} "
            "(set $DCA_TICKER_HISTORY_DIR or pass --history-dir)",
            file=sys.stderr,
        )
        return 1

    # `is not None`, not truthiness. `--tickers` with no symbols -- an unset shell
    # variable expanding to nothing, most realistically -- gives argparse [], and the
    # truthy test fell through to the configured universe and clustered all of it. That
    # is the FOURTH site of this exact mistake in this feature; dca_market_graph
    # .normalize_roster() was written in an earlier round specifically to end the class,
    # and this file was edited three times without it being applied here.
    if args.tickers is not None:
        requested = list(args.tickers)
        origin = "--tickers"
        # Normalized below, along with every other route into `requested`.
    elif args.all_tickers_in_archive:
        requested = None
        origin = "the whole archive"
    else:
        # Inside an error boundary, like scripts/generate_market_graph.py's universe
        # resolution: a missing, unreadable, malformed, or `active`-less tickers.json is
        # ordinary bad user input for a CLI, and deserves this command's documented
        # refusal rather than a traceback. KeyError is in the tuple because
        # _load_active_tickers raises it for the missing-field case specifically.
        try:
            requested = _load_active_tickers(args.tickers_config)
        except (OSError, ValueError, KeyError) as exc:
            print(f"cannot read the active universe from {args.tickers_config}: {exc}", file=sys.stderr)
            return 1
        origin = f"the active universe in {args.tickers_config}"

    # Uppercased at the single point every route converges, not per branch.
    # discover_available_tickers filters against features._TICKER_SYMBOL_PATTERN
    # (`^[A-Z.]{1,6}$`) and then looks for an exact `<TICKER>.csv`, so `--tickers aapl`
    # was dropped silently and the run reported "no matching history" against an archive
    # that plainly holds AAPL.csv. scripts/generate_market_graph.py::_resolve_tickers has
    # always normalized explicit symbols; this is the same rule, applied here too.
    requested = normalize_roster(requested)

    tickers = discover_available_tickers(history_dir, requested)
    if len(tickers) < 2:
        print(
            f"need at least 2 tickers with archived history under {history_dir}; "
            f"found {len(tickers)} from {origin}. Pass --tickers explicitly, or "
            "--all-tickers-in-archive to cluster everything present.",
            file=sys.stderr,
        )
        return 1

    # Widened to cover build_feature_frame() too, not just build_grouping(): it reads
    # every selected archive CSV through features.load_history_csv(), which raises
    # ValueError for a missing required column or an unparseable Date column. That used
    # to sit between two guarded calls unguarded itself, so a single malformed selected
    # ticker ended the command in a traceback instead of the concise refusal every other
    # bad-input case here gets. build_grouping's own refusal of a --clusters count the
    # dendrogram cannot deliver is about the user's arguments and belongs in the same
    # boundary for the same reason.
    try:
        features = build_feature_frame(history_dir, tickers)
        if features.empty:
            print("feature frame is empty; the archive has no post-warmup rows", file=sys.stderr)
            return 1
        grouping = build_grouping(features, alpha=args.alpha, clusters=args.clusters, resolved=tickers)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    out = args.out
    if out is None:
        today = datetime.now(timezone.utc).date().isoformat()
        out = Path(stream_root("market_graph_groups", today)) / "ticker_groups.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(grouping, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"wrote {out}")
    print(f"{len(grouping['ticker_order'])} tickers · {len(grouping['clusters'])} groups · alpha={args.alpha}")
    for cluster in grouping["clusters"]:
        print(f"  {cluster['label']:>20}  {' '.join(cluster['tickers'])}")
    if grouping["dropped_tickers"]:
        print(f"dropped (too little history): {' '.join(grouping['dropped_tickers'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
