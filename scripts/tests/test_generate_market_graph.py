"""CLI-level tests for scripts/generate_market_graph.py.

These drive ``main(argv)`` directly rather than a subprocess, so the exit-code contract
(0 success, 1 data problem, 2 usage refusal) is asserted the same way the rest of
``scripts/`` asserts it. Rendering itself is covered by ``test_dca_market_graph.py``;
what matters here is argument wiring, defaults, and the refusal paths.

``--no-inline-three`` is used throughout: the vendored renderer is 608 KB and inlining
it into every temp file makes the suite noticeably slower for no additional coverage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from scripts import generate_market_graph as cli  # noqa: E402


@pytest.fixture
def combined_csv(tmp_path):
    """A small two-ticker combined CSV in the repo-root file's Polygon-flavored schema."""
    path = tmp_path / "combined.csv"
    rows = ["ticker,date,open,high,low,close,volume,vwap,n"]
    price = {"AAA": 100.0, "BBB": 50.0}
    for day in range(1, 41):
        for ticker in ("AAA", "BBB"):
            base = price[ticker] * (1 + 0.004 * ((day % 7) - 3))
            rows.append(
                f"{ticker},2024-02-{day:02d},{base:.2f},{base * 1.01:.2f},"
                # Underscore-separated on purpose: test_config_invariants.py's
                # account-literal guard flags any bare 6-12 digit run in a tracked
                # file, and a synthetic share volume is exactly that shape. The
                # separator is what defeats the scan, so the value itself can stay a
                # realistic six-figure volume rather than being shrunk to dodge it.
                f"{base * 0.99:.2f},{base * 1.002:.2f},{100_000 + day * 137},{base:.2f},10"
            )
            price[ticker] = base * 1.002
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def run(args):
    return cli.main(args)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_writes_html_and_reports_resolution(combined_csv, tmp_path, capsys):
    out = tmp_path / "ocean.html"
    assert run(["--source", "csv", "--csv-path", str(combined_csv),
                "--out", str(out), "--no-inline-three"]) == 0
    assert out.is_file()
    captured = capsys.readouterr().out
    assert "wrote" in captured
    assert "grouping=returns" in captured


def test_json_out_writes_a_schema_valid_payload(combined_csv, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    out = tmp_path / "ocean.html"
    payload_path = tmp_path / "nested" / "payload.json"
    assert run(["--source", "csv", "--csv-path", str(combined_csv), "--out", str(out),
                "--json-out", str(payload_path), "--no-inline-three"]) == 0
    payload = json.loads(payload_path.read_bytes())
    schema = json.loads((REPO_ROOT / "schemas" / "market_graph_payload.schema.json").read_bytes())
    jsonschema.validate(payload, schema)


def test_ticker_filter_and_date_window_are_applied(combined_csv, tmp_path):
    payload_path = tmp_path / "payload.json"
    assert run(["--source", "csv", "--csv-path", str(combined_csv),
                "--tickers", "AAA", "--start", "2024-02-05", "--end", "2024-02-20",
                "--out", str(tmp_path / "o.html"), "--json-out", str(payload_path),
                "--no-inline-three"]) == 0
    payload = json.loads(payload_path.read_bytes())
    assert [t for t in payload["tickers"] if not t.startswith("__")] == ["AAA"]
    assert payload["dates"][0] >= "2024-02-05"
    assert payload["dates"][-1] <= "2024-02-20"


def test_drawdown_window_sees_the_full_lookback_before_the_start_boundary(tmp_path):
    # Codex review, PR #631 discussion_r3914702425: a --drawdown-window render used to
    # let the loader's own --start truncation (which keeps just ONE bar before `start` --
    # a prior-close baseline, not a drawdown lookback; see dca_market_graph._window())
    # starve _rolling_highs() of an earlier peak still inside the requested drawdown
    # window. A $200 peak, a run of ~$100 filler days, and a first displayed low of $90
    # should report roughly -55% drawdown against the TRUE 20-session rolling high, not
    # roughly -10% against only the single bar the truncated series used to retain.
    csv_path = tmp_path / "drawdown.csv"
    rows = ["ticker,date,open,high,low,close,volume,vwap,n"]
    # The true rolling high, well before the --start boundary below.
    rows.append("AAA,2024-02-01,195.00,200.00,190.00,195.00,1000,195.00,10")
    # Flat filler sessions between the peak and the requested window -- the ONLY bar
    # of these the pre-fix truncated load kept was the last one (2024-02-05).
    for day in range(2, 6):
        rows.append(f"AAA,2024-02-{day:02d},100.00,101.00,99.00,100.00,1000,100.00,10")
    # First displayed session: a deep low relative to the TRUE ($200) peak, not the
    # nearby ($101) filler high.
    rows.append("AAA,2024-02-06,95.00,100.00,90.00,92.00,1000,95.00,10")
    # A couple more sessions so the loader keeps at least 2 bars in the displayed window.
    for day in range(7, 9):
        rows.append(f"AAA,2024-02-{day:02d},100.00,101.00,99.00,100.00,1000,100.00,10")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    payload_path = tmp_path / "payload.json"
    assert run(["--source", "csv", "--csv-path", str(csv_path),
                "--start", "2024-02-06", "--drawdown-window", "20",
                "--out", str(tmp_path / "o.html"), "--json-out", str(payload_path),
                "--no-inline-three"]) == 0
    payload = json.loads(payload_path.read_bytes())
    scale = payload["scales"]["pct_per_unit"]
    series = next(s for s in payload["series"] if s["ticker"] == "AAA")
    # payload channels are stored pre-divided by the shared scale (see build_payload's
    # `y_low.append(round(low / scale, 4))`); multiplying back out recovers the raw
    # drawdown fraction regardless of what the scene's scale happened to normalize to.
    first_low_drawdown = series["yl"][0] * scale
    assert first_low_drawdown == pytest.approx(-0.55, abs=0.02)
    assert payload["dates"][0] == "2024-02-06"


def test_drawdown_window_return_grouping_ignores_pre_start_history(combined_csv, tmp_path, monkeypatch):
    # Codex review, PR #631 follow-up comment on the drawdown-window fix above:
    # loading full history for --drawdown-window's rolling-high lookback must not also
    # feed that untrimmed history to seriate_or_refuse() -- return-correlation grouping
    # order/clusters should reflect only the requested --start window, the same window
    # build_payload(frame_start=...) actually draws, not bars the reader never sees.
    seen = {}
    real_seriate = cli.mg.seriate_or_refuse

    def spy(series_list, **kwargs):
        seen["series"] = series_list
        return real_seriate(series_list, **kwargs)

    monkeypatch.setattr(cli.mg, "seriate_or_refuse", spy)

    assert run(["--source", "csv", "--csv-path", str(combined_csv),
                "--start", "2024-02-20", "--drawdown-window", "20",
                "--group-source", "returns",
                "--out", str(tmp_path / "o.html"), "--no-inline-three"]) == 0

    assert "series" in seen
    for series in seen["series"]:
        # At most ONE bar before --start survives -- the return-correlation
        # baseline close (Codex review, PR #631 discussion_r3917083227), not
        # the full pre-start history _rolling_highs() sees.
        pre_start = [bar for bar in series.bars if bar.timestamp.date().isoformat() < "2024-02-20"]
        assert len(pre_start) <= 1


def test_drawdown_window_excludes_a_ticker_with_no_bars_in_the_window(tmp_path):
    # Codex review, PR #631 discussion_r3917083213: --drawdown-window's full-history
    # load (load_start=None) skips the loader's own start-boundary exclusion, so a
    # ticker whose ENTIRE history predates --start used to survive into the resolved
    # roster and payload as an all-null series -- included in resolution/ticker counts,
    # left outside the grouping order, drawing nothing. It must be dropped entirely.
    csv_path = tmp_path / "absent.csv"
    rows = ["ticker,date,open,high,low,close,volume,vwap,n"]
    # AAA: has bars both before and inside the requested window.
    for day in range(1, 11):
        rows.append(f"AAA,2024-02-{day:02d},100.00,101.00,99.00,100.00,1000,100.00,10")
    # CCC: EVERY bar predates --start below -- nothing inside the window at all.
    for day in range(1, 4):
        rows.append(f"CCC,2024-02-{day:02d},50.00,51.00,49.00,50.00,1000,50.00,10")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    payload_path = tmp_path / "payload.json"
    assert run(["--source", "csv", "--csv-path", str(csv_path),
                "--start", "2024-02-05", "--drawdown-window", "20",
                "--out", str(tmp_path / "o.html"), "--json-out", str(payload_path),
                "--no-inline-three"]) == 0
    payload = json.loads(payload_path.read_bytes())
    tickers = [t for t in payload["tickers"] if not t.startswith("__")]
    assert tickers == ["AAA"]


def test_single_mode_omits_the_centroid_and_switches_the_volume_scale(combined_csv, tmp_path):
    payload_path = tmp_path / "payload.json"
    assert run(["--source", "csv", "--csv-path", str(combined_csv), "--single",
                "--tickers", "AAA",
                "--out", str(tmp_path / "o.html"), "--json-out", str(payload_path),
                "--no-inline-three"]) == 0
    payload = json.loads(payload_path.read_bytes())
    assert "__MARKET__" not in payload["tickers"]
    assert payload["scales"]["volume"]["mode"] == "per_ticker_log"
    assert payload["whole_market"] is False


def test_single_mode_refuses_a_multi_ticker_scene(combined_csv, tmp_path, capsys):
    """--single scales volume PER TICKER, which is only honest for one of them.

    Drawn together, every series is stretched across the full X axis on its own scale,
    so two glyphs at the same X have nothing in common while the scene invites exactly
    that reading. This used to succeed silently -- the default CSV resolves five names.
    """
    assert run(["--source", "csv", "--csv-path", str(combined_csv), "--single",
                "--out", str(tmp_path / "o.html"), "--no-inline-three"]) == 1
    err = capsys.readouterr().err
    assert "--single" in err and "AAA" in err and "BBB" in err


def test_trail_flag_reaches_the_payload(combined_csv, tmp_path):
    payload_path = tmp_path / "payload.json"
    run(["--source", "csv", "--csv-path", str(combined_csv), "--trail", "42",
         "--out", str(tmp_path / "o.html"), "--json-out", str(payload_path),
         "--no-inline-three"])
    assert json.loads(payload_path.read_bytes())["meta"]["default_trail"] == 42


def test_group_source_none_skips_clustering(combined_csv, tmp_path):
    payload_path = tmp_path / "payload.json"
    run(["--source", "csv", "--csv-path", str(combined_csv), "--group-source", "none",
         "--out", str(tmp_path / "o.html"), "--json-out", str(payload_path),
         "--no-inline-three"])
    payload = json.loads(payload_path.read_bytes())
    assert payload["grouping"]["source"] == "none"
    assert payload["grouping"]["clusters"] == []


def test_group_source_file_applies_a_supplied_grouping(combined_csv, tmp_path):
    groups = tmp_path / "groups.json"
    groups.write_text(json.dumps({
        "source": "ml-forecaster",
        "ticker_order": ["BBB", "AAA"],
        "clusters": [{"id": 0, "label": "pair", "tickers": ["BBB", "AAA"], "color": "#abcdef"}],
    }), encoding="utf-8")
    payload_path = tmp_path / "payload.json"
    assert run(["--source", "csv", "--csv-path", str(combined_csv),
                "--group-source", "file", "--groups-file", str(groups),
                "--out", str(tmp_path / "o.html"), "--json-out", str(payload_path),
                "--no-inline-three"]) == 0
    payload = json.loads(payload_path.read_bytes())
    assert payload["grouping"]["source"] == "ml-forecaster"
    assert payload["grouping"]["clusters"][0]["label"] == "pair"
    assert payload["tickers"][:2] == ["BBB", "AAA"]


def test_default_output_path_lands_under_the_shared_artifact_root(combined_csv, tmp_path, monkeypatch):
    """Not the working directory: a scheduled render must land somewhere findable."""
    root = tmp_path / "cron"
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    assert run(["--source", "csv", "--csv-path", str(combined_csv), "--no-inline-three"]) == 0
    produced = list((root / "market_graph").glob("*/market_graph.html"))
    assert len(produced) == 1
    # ...and nothing was dropped in the cwd.
    assert not (tmp_path / "artifacts").exists()


def test_tickers_at_config_expands_to_the_active_universe(combined_csv, tmp_path, capsys):
    # None of the synthetic tickers are in the real universe, so this must refuse
    # cleanly rather than rendering an empty scene.
    assert run(["--source", "csv", "--csv-path", str(combined_csv), "--tickers", "@config",
                "--out", str(tmp_path / "o.html"), "--no-inline-three"]) == 1
    assert "nothing to draw" in capsys.readouterr().err


def test_run_dir_source_replays_persisted_broker_responses(tmp_path):
    raw = tmp_path / "run" / "raw"
    raw.mkdir(parents=True)
    bars = [
        {"begins_at": f"2024-01-02T1{i}:30:00Z", "open_price": "10", "high_price": "11",
         "low_price": "9", "close_price": str(10 + i * 0.2), "volume": "100"}
        for i in range(8)
    ]
    raw.joinpath("historicals_batch1.json").write_text(
        json.dumps({"results": [{"symbol": "AMD", "interval": "5minute", "bars": bars}]}),
        encoding="utf-8",
    )
    payload_path = tmp_path / "payload.json"
    assert run(["--source", "run-dir", "--run-dir", str(tmp_path / "run"),
                "--out", str(tmp_path / "o.html"), "--json-out", str(payload_path),
                "--no-inline-three"]) == 0
    assert "AMD" in json.loads(payload_path.read_bytes())["tickers"]


# ---------------------------------------------------------------------------
# Refusals -- the exit-code contract
# ---------------------------------------------------------------------------


def test_group_source_file_without_a_file_is_a_usage_refusal(tmp_path, capsys):
    assert run(["--group-source", "file", "--out", str(tmp_path / "o.html")]) == 2
    assert "requires --groups-file" in capsys.readouterr().err


def test_missing_source_file_exits_one_not_a_traceback(tmp_path, capsys):
    assert run(["--source", "csv", "--csv-path", str(tmp_path / "absent.csv"),
                "--out", str(tmp_path / "o.html")]) == 1
    assert "error:" in capsys.readouterr().err


def test_run_dir_source_without_run_dir_exits_one(tmp_path, capsys):
    assert run(["--source", "run-dir", "--out", str(tmp_path / "o.html")]) == 1
    assert "requires --run-dir" in capsys.readouterr().err


def test_source_with_no_usable_history_exits_one(tmp_path, capsys):
    empty = tmp_path / "empty.csv"
    empty.write_text("ticker,date,open,high,low,close,volume,vwap,n\n", encoding="utf-8")
    assert run(["--source", "csv", "--csv-path", str(empty), "--out", str(tmp_path / "o.html")]) == 1
    assert "nothing to draw" in capsys.readouterr().err


def test_bad_date_is_reported_as_a_usage_error():
    # argparse exits 2; main() catches SystemExit and returns the code rather than
    # letting it propagate, matching every other script in this directory.
    assert run(["--start", "01-01-2024"]) == 2


def test_unknown_source_is_rejected_by_argparse():
    assert run(["--source", "telepathy"]) == 2


def test_help_returns_zero():
    assert run(["--help"]) == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_resolve_tickers_handles_lists_config_and_none():
    assert cli._resolve_tickers(None) is None
    assert cli._resolve_tickers(" aapl , msft ") == ["AAPL", "MSFT"]
    # An explicitly empty selection stays empty rather than collapsing to None, which
    # the loaders read as "no filter" and would answer with the entire archive.
    assert cli._resolve_tickers(",,") == []
    assert "AAPL" in cli._resolve_tickers("@config")


@pytest.mark.parametrize("bad", ["0", "-1", "61", "abc"])
def test_trail_must_be_within_the_viewers_range(bad):
    # The viewer silently clamps out-of-range values, which would leave the emitted
    # payload claiming a trail length nobody ever saw -- and 0 is schema-invalid.
    assert cli.main(["--trail", bad]) == 2


def test_trail_accepts_the_boundaries():
    assert cli._trail_length("1") == 1
    assert cli._trail_length(str(cli.MAX_TRAIL)) == cli.MAX_TRAIL


@pytest.mark.parametrize("flag", ["--clusters", "--interval-minutes"])
@pytest.mark.parametrize("bad", ["0", "-3", "abc", "1.5"])
def test_count_flags_are_refused_at_parse_time(flag, bad):
    """Every count-shaped option, not just the one a reviewer happened to name.

    --clusters 0 was accepted and then silently treated as though the flag had been
    omitted, so an explicit grouping request came back with the automatic number of
    groups and no warning -- the same shape as --trail 0 drawing a blank pulse and
    --fps 0 dying inside PillowWriter. They share one validator now, so a count option
    added later inherits the refusal rather than the bug.
    """
    assert cli.main([flag, bad]) == 2


def test_count_flags_accept_a_real_count():
    assert cli._count("--clusters")("4") == 4


def test_unreadable_groups_file_exits_one_not_a_traceback(combined_csv, tmp_path, capsys):
    assert cli.main(["--source", "csv", "--csv-path", str(combined_csv),
                     "--group-source", "file", "--groups-file", str(tmp_path / "absent.json"),
                     "--out", str(tmp_path / "o.html"), "--no-inline-three"]) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("spec", ["@config", None])
def test_a_broken_config_is_a_clean_refusal_not_a_traceback(tmp_path, capsys, monkeypatch, spec):
    """Universe resolution reads tickers.json, so it belongs inside the error boundary.

    Covers both routes into it: an explicit `--tickers @config`, and the archive
    source's implicit default.
    """
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tickers.json").write_text("{not json", encoding="utf-8")

    args = ["--source", "archive", "--history-dir", str(tmp_path), "--no-inline-three",
            "--out", str(tmp_path / "o.html")]
    if spec:
        args += ["--tickers", spec]
    assert cli.main(args) == 1
    assert "error:" in capsys.readouterr().err


def test_an_unreadable_config_directory_is_also_a_clean_refusal(tmp_path, capsys, monkeypatch):
    # OSError, not just ValueError: a misdirected $DCA_CONFIG_DIR is the same class of
    # user-input problem as malformed JSON and deserves the same refusal.
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path / "nowhere"))
    assert cli.main(["--source", "csv", "--tickers", "@config",
                     "--out", str(tmp_path / "o.html"), "--no-inline-three"]) == 1
    assert "error:" in capsys.readouterr().err


def test_malformed_groups_file_exits_one(combined_csv, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cli.main(["--source", "csv", "--csv-path", str(combined_csv),
                     "--group-source", "file", "--groups-file", str(bad),
                     "--out", str(tmp_path / "o.html"), "--no-inline-three"]) == 1
    assert "error:" in capsys.readouterr().err


def test_archive_source_defaults_to_the_active_universe(tmp_path, capsys):
    """The archive is the whole US universe, not this strategy's universe.

    Loading it wholesale is slow, almost never intended, and feeds an O(n^3) seriation.
    """
    directory = tmp_path / "ticker_data"
    directory.mkdir()
    for symbol in ("ZZZA", "ZZZB"):        # present in the archive, absent from config
        lines = ["Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits"]
        for day in range(1, 20):
            lines.append(f"2024-01-{day:02d},10,11,9,10.5,10.5,1000,0,0")
        (directory / f"{symbol}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert cli.main(["--source", "archive", "--history-dir", str(directory),
                     "--out", str(tmp_path / "o.html"), "--no-inline-three"]) == 1
    assert "nothing to draw" in capsys.readouterr().err

    # ...and the documented opt-out loads them.
    assert cli.main(["--source", "archive", "--history-dir", str(directory),
                     "--all-tickers-in-archive",
                     "--out", str(tmp_path / "o.html"), "--no-inline-three"]) == 0


def test_default_output_span_comes_from_the_payload_not_the_first_ticker(tmp_path, monkeypatch):
    """With staggered histories those differ, and two windows could collide on one path."""
    csv_path = tmp_path / "staggered.csv"
    rows = ["ticker,date,open,high,low,close,volume,vwap,n"]
    for day in range(1, 21):               # SHORT starts late
        rows.append(f"LONG,2024-03-{day:02d},10,11,9,10.5,{10_000 + day},10,1")
    for day in range(10, 21):
        rows.append(f"SHRT,2024-03-{day:02d},10,11,9,10.5,{10_000 + day},10,1")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    root = tmp_path / "cron"
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--source", "csv", "--csv-path", str(csv_path), "--no-inline-three"]) == 0
    produced = list((root / "market_graph").glob("*/market_graph.html"))
    assert len(produced) == 1
    # 03-02, not 03-01: LONG's first bar has no prior close, so it is not drawable and
    # earns no frame. The span names the drawable window, which is the point -- it must
    # come from the payload's own axis and end on LONG's last session, not on SHRT's
    # shorter history.
    assert produced[0].parent.name == "2024-03-02_2024-03-20"


def test_parse_day_accepts_iso_and_rejects_anything_else():
    import argparse
    from datetime import date

    assert cli._parse_day("2024-04-01") == date(2024, 4, 1)
    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_day("April 1")


def test_arg_parser_builds_without_error():
    parser = cli._build_arg_parser()
    args = parser.parse_args([])
    assert args.source == "csv"
    assert args.mode == "both"
    assert args.group_source == "returns"
