"""Tests for cluster_tickers.py -- the behavioral ticker grouping exporter.

The interesting property is not "does it produce clusters" but "does it recover
structure that is actually there". Every fixture below therefore builds tickers with a
KNOWN group membership -- shared driving factor, shared volatility regime -- and asserts
the grouping puts them back together.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pandas as pd
import pytest

import cluster_tickers


def _write_archive(directory: Path, ticker: str, closes, volumes=None):
    """Write a Yahoo-flavored per-ticker archive CSV of the shape features.py expects."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits"]
    days = pd.bdate_range("2022-01-03", periods=len(closes))
    for day, close, volume in zip(days, closes, volumes or [1_000_000] * len(closes), strict=True):
        lines.append(
            f"{day.date()},{close:.4f},{close * 1.01:.4f},{close * 0.99:.4f},"
            f"{close:.4f},{close:.4f},{int(volume)},0,0"
        )
    (directory / f"{ticker}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _factor_archive(tmp_path: Path, groups: dict[str, list[str]], vols: dict[str, float],
                    periods: int = 400, seed: int = 3) -> Path:
    """Build an archive whose groups share both a driving factor and a volatility regime."""
    rng = random.Random(seed)
    directory = tmp_path / "ticker_data"
    factors = {name: [rng.gauss(0, vols[name]) for _ in range(periods)] for name in groups}
    for group, tickers in groups.items():
        for ticker in tickers:
            price = 100.0
            closes = []
            for i in range(periods):
                price = max(1.0, price * (1 + factors[group][i] + rng.gauss(0, vols[group] * 0.3)))
                closes.append(price)
            _write_archive(directory, ticker, closes)
    return directory


@pytest.fixture
def two_regime_archive(tmp_path):
    return _factor_archive(
        tmp_path,
        {"calm": ["CALMA", "CALMB", "CALMC"], "wild": ["WILDA", "WILDB", "WILDC"]},
        {"calm": 0.004, "wild": 0.045},
    )


def _features(directory: Path):
    from features import build_feature_frame, discover_available_tickers

    return build_feature_frame(directory, discover_available_tickers(directory))


# ---------------------------------------------------------------------------
# Grouping quality
# ---------------------------------------------------------------------------


def test_recovers_two_known_behavioral_regimes(two_regime_archive):
    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), clusters=2)
    recovered = {frozenset(c["tickers"]) for c in grouping["clusters"]}
    assert recovered == {
        frozenset({"CALMA", "CALMB", "CALMC"}),
        frozenset({"WILDA", "WILDB", "WILDC"}),
    }


def test_seriation_keeps_each_group_contiguous(two_regime_archive):
    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), clusters=2)
    order = grouping["ticker_order"]
    for group in (["CALMA", "CALMB", "CALMC"], ["WILDA", "WILDB", "WILDC"]):
        positions = sorted(order.index(t) for t in group)
        assert positions == list(range(positions[0], positions[0] + 3)), order


@pytest.mark.parametrize("alpha", [0.0, 0.5, 1.0])
def test_both_similarity_notions_recover_the_regimes_on_their_own(two_regime_archive, alpha):
    # alpha=1.0 is pure return correlation, alpha=0.0 pure behavioral profile. The
    # fixture is separable under either, which is what makes the blend safe to tune.
    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), alpha=alpha, clusters=2)
    recovered = {frozenset(c["tickers"]) for c in grouping["clusters"]}
    assert recovered == {
        frozenset({"CALMA", "CALMB", "CALMC"}),
        frozenset({"WILDA", "WILDB", "WILDC"}),
    }


def test_labels_describe_the_volatility_band(two_regime_archive):
    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), clusters=2)
    labels = {frozenset(c["tickers"]): c["label"] for c in grouping["clusters"]}
    calm = labels[frozenset({"CALMA", "CALMB", "CALMC"})]
    wild = labels[frozenset({"WILDA", "WILDB", "WILDC"})]
    assert "vol" in calm and "vol" in wild
    assert calm != wild, "the two regimes must not receive the same label"


def test_bands_are_emitted_left_to_right_along_the_ticker_axis(two_regime_archive):
    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), clusters=2)
    order = grouping["ticker_order"]
    firsts = [min(order.index(t) for t in c["tickers"]) for c in grouping["clusters"]]
    assert firsts == sorted(firsts)
    assert [c["id"] for c in grouping["clusters"]] == list(range(len(grouping["clusters"])))


# ---------------------------------------------------------------------------
# Document shape -- must satisfy dca_market_graph.grouping_from_mapping
# ---------------------------------------------------------------------------


def test_document_is_consumable_by_the_market_graph(two_regime_archive):
    import dca_market_graph as mg

    # Local stand-in for near_close_dca.Bar rather than importing it. ml-forecaster must
    # never import the live DCA engine (near_close_dca) -- the dca-sizing-signals boundary
    # test (tests/test_architecture_boundaries.py) enforces exactly that, and this file was
    # the sole violation. mg.BarSeries reuses near_close_dca.Bar only as a plain
    # (timestamp, open, high, low, close, volume) carrier and does not type-check the atom,
    # so a same-shaped local dataclass exercises the identical consumption path.
    from dataclasses import dataclass
    from datetime import datetime, timedelta
    from typing import Any, cast

    @dataclass(frozen=True)
    class Bar:
        timestamp: datetime
        open: float
        high: float
        low: float
        close: float
        volume: float

    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), clusters=2)

    # Round-trip through JSON exactly as the CLI writes it and the viewer reads it.
    document = json.loads(json.dumps(grouping))

    series = [
        mg.BarSeries(
            ticker=ticker,
            bars=cast(
                Any,
                tuple(
                    Bar(timestamp=datetime(2024, 1, 1) + timedelta(days=i), open=10, high=11,
                        low=9, close=10 + (i % 3), volume=1000)
                    for i in range(10)
                ),
            ),
        )
        for ticker in document["ticker_order"]
    ]
    resolved = mg.grouping_from_mapping(document, series)
    assert resolved.source == "ml-forecaster"
    assert set(resolved.order) == set(document["ticker_order"])
    assert len(resolved.clusters) == len(document["clusters"])


def test_colours_come_from_the_shared_palette(two_regime_archive):
    from dca_market_graph import CLUSTER_COLORS

    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), clusters=2)
    for cluster in grouping["clusters"]:
        assert cluster["color"] in CLUSTER_COLORS


def test_params_record_how_the_grouping_was_produced(two_regime_archive):
    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), alpha=0.25, clusters=2)
    assert grouping["source"] == "ml-forecaster"
    assert grouping["params"]["alpha"] == 0.25
    assert grouping["params"]["features"]


def test_every_usable_ticker_appears_exactly_once(two_regime_archive):
    grouping = cluster_tickers.build_grouping(_features(two_regime_archive), clusters=2)
    assigned = [t for c in grouping["clusters"] for t in c["tickers"]]
    assert sorted(assigned) == sorted(grouping["ticker_order"])
    assert len(assigned) == len(set(assigned))


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


def test_tickers_with_too_little_history_are_dropped_not_clustered(tmp_path):
    directory = _factor_archive(
        tmp_path, {"a": ["AAAA", "AAAB"], "b": ["BBBA", "BBBB"]},
        {"a": 0.005, "b": 0.04},
    )
    _write_archive(directory, "TINY", [100.0 + i for i in range(12)])
    grouping = cluster_tickers.build_grouping(_features(directory), clusters=2)
    assert "TINY" not in grouping["ticker_order"]
    assert "TINY" not in [t for c in grouping["clusters"] for t in c["tickers"]]


def test_a_ticker_with_zero_surviving_feature_rows_is_reported_as_dropped(tmp_path):
    """Too little history to survive warmup is invisible to counts.index entirely.

    A ticker below MIN_ROWS_PER_TICKER but still present in `counts` (some rows survived
    warmup) was already caught by the old set-subtraction -- that's
    test_tickers_with_too_little_history_are_dropped_not_clustered above. This is the
    sharper case: a ticker so short (5 rows, below every rolling window in
    NUMERIC_FEATURE_COLUMNS) that NOTHING survives features.py's finite-value filter, so
    it is absent from counts.index itself. Without the resolved roster to reconcile
    against, the old subtraction could not name a ticker it never saw.
    """
    directory = _factor_archive(
        tmp_path, {"a": ["AAAA", "AAAB"], "b": ["BBBA", "BBBB"]},
        {"a": 0.005, "b": 0.04},
    )
    _write_archive(directory, "STUB", [100.0 + i for i in range(5)])
    features = _features(directory)
    assert "STUB" not in set(features["ticker"])   # confirms the premise: it vanished

    from features import discover_available_tickers
    resolved = discover_available_tickers(directory, None)
    assert "STUB" in resolved   # it WAS a real, resolved ticker

    grouping = cluster_tickers.build_grouping(features, clusters=2, resolved=resolved)
    assert "STUB" in grouping["dropped_tickers"]
    assert "STUB" not in grouping["ticker_order"]


def test_cli_reports_a_zero_row_ticker_as_dropped(tmp_path, capsys):
    directory = _factor_archive(
        tmp_path, {"a": ["AAAA", "AAAB"], "b": ["BBBA", "BBBB"]},
        {"a": 0.005, "b": 0.04},
    )
    _write_archive(directory, "STUB", [100.0 + i for i in range(5)])
    out = tmp_path / "groups.json"
    code = cluster_tickers.main([
        "--history-dir", str(directory), "--out", str(out),
        "--all-tickers-in-archive", "--clusters", "2",
    ])
    assert code == 0
    document = json.loads(out.read_bytes())
    assert "STUB" in document["dropped_tickers"]
    captured = capsys.readouterr()
    assert "dropped (too little history)" in captured.out
    assert "STUB" in captured.out


def test_refuses_when_fewer_than_two_tickers_are_usable(tmp_path):
    directory = tmp_path / "ticker_data"
    _write_archive(directory, "ONLY", [100.0 * (1.001 ** i) for i in range(300)])
    with pytest.raises(SystemExit, match="at least 2 tickers"):
        cluster_tickers.build_grouping(_features(directory))


def test_a_feature_identical_across_tickers_does_not_produce_nan_distances(tmp_path):
    # Constant columns have zero spread; dividing by it would poison every distance.
    directory = _factor_archive(
        tmp_path, {"a": ["AAAA", "AAAB"], "b": ["BBBA", "BBBB"]}, {"a": 0.006, "b": 0.05}
    )
    features = _features(directory)
    features = features.assign(rsi_14=50.0)  # force one feature flat everywhere
    grouping = cluster_tickers.build_grouping(features, clusters=2)
    assert len(grouping["ticker_order"]) == 4


def test_an_unsatisfiable_cluster_count_is_refused_not_silently_reduced(tmp_path):
    """"maxclust" is a MAXIMUM, not a target.

    Four tickers with the same history make every linkage height tie, and scipy then
    hands back fewer groups than asked for. The run used to succeed and record the
    smaller number as though it were the answer. Refused rather than forced: between
    identical series there is no principled place to cut, so any split would be
    invented.
    """
    directory = tmp_path / "ticker_data"
    closes = [100.0 * (1.0 + 0.004 * ((i % 7) - 3)) for i in range(300)]
    for ticker in ("SAMEA", "SAMEB", "SAMEC", "SAMED"):
        _write_archive(directory, ticker, closes)

    with pytest.raises(ValueError, match="cannot be satisfied"):
        cluster_tickers.build_grouping(_features(directory), clusters=3)


def test_an_automatic_cluster_count_accepts_whatever_the_cut_yields(tmp_path):
    """Nothing was promised, so nothing is refused -- only an explicit request is."""
    directory = tmp_path / "ticker_data"
    closes = [100.0 * (1.0 + 0.004 * ((i % 7) - 3)) for i in range(300)]
    for ticker in ("SAMEA", "SAMEB", "SAMEC", "SAMED"):
        _write_archive(directory, ticker, closes)

    grouping = cluster_tickers.build_grouping(_features(directory))
    assert len(grouping["ticker_order"]) == 4
    assert grouping["clusters"]


def test_profile_matrix_is_standardized(two_regime_archive):
    names, profile = cluster_tickers._profile_matrix(_features(two_regime_archive))
    assert len(names) == 6
    assert profile.shape[1] == 2 * len(cluster_tickers.NUMERIC_FEATURE_COLUMNS)
    assert all(math.isfinite(v) for v in profile.ravel())
    # Column means sit at zero after z-scoring; spread is 1 wherever it was non-zero.
    assert abs(profile.mean(axis=0)).max() < 1e-9


def test_correlation_distance_is_a_metric_with_a_zero_diagonal(two_regime_archive):
    features = _features(two_regime_archive)
    names = sorted(features["ticker"].unique())
    distance = cluster_tickers._correlation_distance(features, names)
    assert distance.shape == (6, 6)
    assert abs(distance.diagonal()).max() < 1e-9
    assert (distance >= 0).all()


def test_normalize_handles_an_all_zero_matrix():
    import numpy as np

    zeros = np.zeros((3, 3))
    assert cluster_tickers._normalize(zeros).max() == 0.0


def test_label_for_an_unknown_ticker_set_falls_back_to_a_count(two_regime_archive):
    features = _features(two_regime_archive)
    assert cluster_tickers._label_for(["NOT_PRESENT"], features) == "1 tickers"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_writes_a_grouping_document(two_regime_archive, tmp_path, capsys):
    out = tmp_path / "groups.json"
    code = cluster_tickers.main([
        "--history-dir", str(two_regime_archive), "--out", str(out), "--clusters", "2",
        "--all-tickers-in-archive",
    ])
    assert code == 0
    document = json.loads(out.read_bytes())
    assert document["source"] == "ml-forecaster"
    assert len(document["clusters"]) == 2
    assert "wrote" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["0", "-2", "abc", "1.5"])
def test_cli_rejects_a_nonpositive_cluster_count(bad):
    """--clusters 0 was accepted and then silently ignored.

    seriate/fcluster treat it as "no count given", so an explicit grouping request came
    back with the automatic number of groups and said nothing about the substitution.
    Shared with both market-graph CLIs via validate_positive_int. SystemExit rather
    than a return code because this CLI, unlike scripts/generate_market_graph.py, lets
    argparse's own exit propagate -- the refusal is argparse's usage error either way.
    """
    with pytest.raises(SystemExit) as exit_info:
        cluster_tickers.main(["--clusters", bad])
    assert exit_info.value.code == 2


def test_cli_rejects_an_out_of_range_alpha(two_regime_archive, tmp_path, capsys):
    code = cluster_tickers.main(["--history-dir", str(two_regime_archive), "--alpha", "1.5"])
    assert code == 2
    assert "--alpha" in capsys.readouterr().err


def test_cli_reports_a_missing_archive_without_a_traceback(tmp_path, capsys):
    code = cluster_tickers.main(["--history-dir", str(tmp_path / "absent")])
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_cli_refuses_a_single_ticker_archive(tmp_path, capsys):
    directory = tmp_path / "ticker_data"
    _write_archive(directory, "ONLY", [100.0 + i for i in range(300)])
    code = cluster_tickers.main(["--history-dir", str(directory), "--all-tickers-in-archive"])
    assert code == 1
    assert "at least 2 tickers" in capsys.readouterr().err


def test_cli_reports_a_malformed_archive_csv_without_a_traceback(tmp_path, capsys):
    """A single bad selected CSV must not end the run in a traceback.

    build_feature_frame() reads every selected archive CSV through
    features.load_history_csv(), which raises ValueError for a missing required column.
    That call used to sit between two guarded calls (universe resolution, build_grouping)
    unguarded itself, so this is the gap between them -- not a repeat of either.
    """
    directory = _factor_archive(
        tmp_path, {"a": ["AAAA", "AAAB"], "b": ["BBBA", "BBBB"]},
        {"a": 0.005, "b": 0.04},
    )
    # A CSV missing the required Close column entirely.
    (directory / "BROKEN.csv").write_text(
        "Date,Open,High,Low,Adj Close,Volume,Dividends,Stock Splits\n"
        "2022-01-03,10,11,9,10,1000,0,0\n",
        encoding="utf-8",
    )
    code = cluster_tickers.main([
        "--history-dir", str(directory), "--tickers", "AAAA", "AAAB", "BBBA", "BBBB", "BROKEN",
    ])
    assert code == 1
    assert "required column" in capsys.readouterr().err


def test_cli_defaults_to_the_configured_active_universe_and_says_so(two_regime_archive, capsys):
    # Without --tickers or --all-tickers-in-archive the universe comes from
    # config/tickers.json, which shares nothing with a synthetic archive. The refusal
    # must name where the universe came from, or this is baffling to debug.
    code = cluster_tickers.main(["--history-dir", str(two_regime_archive)])
    assert code == 1
    err = capsys.readouterr().err
    assert "active universe" in err and "--all-tickers-in-archive" in err


@pytest.mark.parametrize(
    ("contents", "why"),
    [
        (None, "missing file"),
        ("{not json", "malformed JSON"),
        ("[]", "valid JSON that is not an object"),
        ('{"excluded": []}', "missing the 'active' key"),
        ('{"active": "AAPL"}', "a non-list 'active' value"),
    ],
    ids=["missing", "malformed", "not-an-object", "no-active-key", "active-not-a-list"],
)
def test_cli_refuses_a_bad_tickers_config_without_a_traceback(
    two_regime_archive, tmp_path, capsys, contents, why
):
    """A bad --tickers-config is ordinary user input, not a crash.

    _load_active_tickers raises OSError, ValueError, or KeyError depending on how the
    config is broken, and all three used to escape main() as a traceback. The whole
    class is caught at the CLI boundary now, the same way
    scripts/generate_market_graph.py already catches universe resolution.
    """
    config = tmp_path / "tickers.json"
    if contents is not None:
        config.write_text(contents, encoding="utf-8")

    code = cluster_tickers.main([
        "--history-dir", str(two_regime_archive), "--tickers-config", str(config),
    ])
    assert code == 1, why
    assert "cannot read the active universe" in capsys.readouterr().err


@pytest.mark.parametrize("spelling", [["calma", "calmb", "wilda", "wildb"],
                                      [" CalmA ", "cALMb", "WildA", "wildB"]])
def test_cli_normalizes_the_case_of_an_explicit_ticker_list(two_regime_archive, tmp_path, spelling):
    """`--tickers aapl` must find AAPL.csv.

    discover_available_tickers filters against `^[A-Z.]{1,6}$` and then looks for an
    exact `<TICKER>.csv`, so a lowercase symbol was dropped silently and the run
    reported "no matching history" against an archive that plainly held the file. The
    market-graph CLI has always uppercased explicit symbols; this is that same rule.
    """
    out = tmp_path / "groups.json"
    code = cluster_tickers.main([
        "--history-dir", str(two_regime_archive), "--out", str(out),
        "--tickers", *spelling, "--clusters", "2",
    ])
    assert code == 0
    document = json.loads(out.read_bytes())
    assert set(document["ticker_order"]) == {"CALMA", "CALMB", "WILDA", "WILDB"}


def test_cli_refuses_an_explicitly_empty_ticker_list(two_regime_archive, capsys):
    """`--tickers` with no symbols must not widen into the configured universe.

    Realistically this is an unset shell variable expanding to nothing. argparse hands
    over [], and a truthiness test fell through to the active universe and clustered all
    of it. This was the FOURTH site of that mistake in this feature -- dca_market_graph
    .normalize_roster() exists precisely to end the class, and this file had been edited
    three times without it being applied here.
    """
    code = cluster_tickers.main(["--history-dir", str(two_regime_archive), "--tickers"])
    assert code == 1
    err = capsys.readouterr().err
    assert "at least 2 tickers" in err and "--tickers" in err


def test_cli_honours_an_explicit_ticker_list(two_regime_archive, tmp_path):
    out = tmp_path / "groups.json"
    code = cluster_tickers.main([
        "--history-dir", str(two_regime_archive), "--out", str(out),
        "--tickers", "CALMA", "CALMB", "WILDA", "WILDB", "--clusters", "2",
    ])
    assert code == 0
    document = json.loads(out.read_bytes())
    assert set(document["ticker_order"]) == {"CALMA", "CALMB", "WILDA", "WILDB"}
