"""Tests for inspect_run.py against a real (demo) run's artifacts."""

from __future__ import annotations

import json
import math
import shutil

import pytest

import inspect_run
import run_walkforward


def _write_bare_checkpoint(run_dir, ledger) -> None:
    """The minimal harness_state.json _open_ledger() needs to bound reads.

    Several tests below hand-build a Ledger directly (bypassing the full
    harness/checkpoint machinery) to exercise inspect_run.py's view logic
    in isolation; since _open_ledger() now refuses when no checkpoint
    exists at all (Codex review: "Refuse inspection when no checkpoint
    bounds the ledger"), those tests need a real -- if minimal -- checkpoint
    to read through.
    """
    checkpoint_dir = run_dir / "checkpoint"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / "harness_state.json").write_text(
        json.dumps({"ledger": ledger.fingerprints()}), encoding="utf-8"
    )


def test_dollar_clamps_non_finite_log_returns_instead_of_raising():
    # Regression (Antigravity review: "Uncaught OverflowError risk in
    # _dollar()"), applied here too since inspect_run.py duplicates
    # daily_update.py's _dollar() helper.
    assert inspect_run._dollar(float("inf"), 100.0) == pytest.approx(100.0 * math.exp(50.0))
    assert inspect_run._dollar(float("-inf"), 100.0) == pytest.approx(100.0 * math.exp(-50.0))
    assert inspect_run._dollar(float("nan"), 100.0) == pytest.approx(100.0)
    assert inspect_run._dollar(0.01, 100.0) == pytest.approx(100.0 * math.exp(0.01))


def test_dollar_cell_marks_nan_forecasts_invalid_instead_of_ref_close():
    """Codex review ("Mark NaN forecasts invalid in inspection views"):
    _dollar()'s NaN->0.0 clamp exists only to keep math.exp() from raising,
    not to make a genuinely invalid forecast look like a valid "unchanged
    from ref_close" prediction -- the scoring path already treats NaN as
    invalid rather than neutral. _dollar_cell() must publish the explicit
    marker instead, mirroring daily_update.py's identical split."""
    assert inspect_run._dollar_cell(float("nan"), 100.0) == inspect_run._INVALID_FORECAST_MARKER
    assert inspect_run._dollar_cell(0.01, 100.0) == pytest.approx(100.0 * math.exp(0.01))


def test_fmt_dollar_renders_the_invalid_marker_without_crashing():
    """The invalid marker is a string, not a float -- formatting it with
    the same ``.2f`` spec used for a real price must not raise, and it
    must right-justify to the same width the text table uses for prices."""
    assert inspect_run._fmt_dollar(inspect_run._INVALID_FORECAST_MARKER, 9) == f"{'N/A':>9}"
    assert inspect_run._fmt_dollar(100.0, 9) == f"{100.0:>9.2f}"


def test_tomorrow_rows_marks_a_nan_component_invalid_not_the_ref_close():
    band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
    point = {c: 0.0 for c in ("open", "high", "low", "close")}
    point["close"] = float("nan")
    entry = {
        "ticker": "NANC",
        "model": "persistence",
        "origin_date": "2021-06-01",
        "ref_close": 100.0,
        "point": point,
        "quantiles": band,
    }
    state = {"outstanding": {"": [entry]}}
    rows = inspect_run._tomorrow_rows(state, "persistence", None)
    assert rows[0]["pred_close"] == inspect_run._INVALID_FORECAST_MARKER
    # A component that wasn't NaN still converts normally.
    assert rows[0]["pred_open"] == pytest.approx(100.0)


def test_tomorrow_rows_keeps_each_tickers_current_forecast_despite_mixed_origins():
    # A single market-wide "latest origin date" filter used to drop a
    # recovered or newly warmed-up ticker's forecast whenever its origin
    # predates the rest of the market -- even though it's still that
    # ticker's current, still-unresolved next-day forecast, and the daily
    # CSV path (_write_next_day_forecasts) already preserves it on the
    # same principle (Codex review: "Keep each ticker's current forecast
    # in --tomorrow").
    band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
    point = {c: 0.0 for c in ("open", "high", "low", "close")}
    fresh_entry = {
        "ticker": "AAA",
        "model": "persistence",
        "origin_date": "2021-06-01",
        "ref_close": 100.0,
        "point": point,
        "quantiles": band,
    }
    laggy_entry = {
        "ticker": "LAGGY",
        "model": "persistence",
        "origin_date": "2021-05-28",  # predates the rest of the market
        "ref_close": 50.0,
        "point": point,
        "quantiles": band,
    }
    state = {"outstanding": {"": [fresh_entry, laggy_entry]}}
    rows = inspect_run._tomorrow_rows(state, "persistence", None)
    assert {row["ticker"] for row in rows} == {"AAA", "LAGGY"}


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    run_dir = tmp_path_factory.mktemp("demo") / "run"
    code = run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"])
    assert code == 0
    return run_dir


class TestTomorrow:
    def test_table_shows_all_tickers_with_bands(self, demo_run, capsys):
        assert inspect_run.main(["--run-dir", str(demo_run), "--tomorrow"]) == 0
        output = capsys.readouterr().out
        assert "Tomorrow's forecast" in output
        for ticker in ("AAA", "BBB", "CCC"):
            assert ticker in output
        assert "close band [p10 – p90]" in output

    def test_csv_roundtrip_and_dollar_conversion(self, demo_run, tmp_path, capsys):
        csv_path = tmp_path / "tomorrow.csv"
        code = inspect_run.main(["--run-dir", str(demo_run), "--tomorrow", "--csv", str(csv_path)])
        assert code == 0
        capsys.readouterr()
        import csv as csv_module
        import math

        with csv_path.open() as handle:
            rows = list(csv_module.DictReader(handle))
        assert {row["ticker"] for row in rows} == {"AAA", "BBB", "CCC"}
        # Verify the dollar conversion against the checkpoint's raw entry.
        state = json.loads((demo_run / "checkpoint" / "harness_state.json").read_text())
        first = rows[0]
        entry = next(
            e
            for bucket in state["outstanding"].values()
            for e in bucket
            if e["ticker"] == first["ticker"]
            and e["model"] == first["model"]
            and e["origin_date"] == first["origin_date"]
        )
        expected = entry["ref_close"] * math.exp(entry["point"]["close"])
        assert float(first["pred_close"]) == pytest.approx(expected, rel=1e-9)
        # Band ordering survives conversion.
        assert float(first["close_p10"]) <= float(first["close_p50"]) <= float(first["close_p90"])

    def test_sort_by_band_width(self, demo_run, capsys):
        assert inspect_run.main(["--run-dir", str(demo_run), "--tomorrow", "--sort", "band_width"]) == 0
        assert "Tomorrow's forecast" in capsys.readouterr().out

    def test_csv_export_creates_missing_parent_directories(self, demo_run, tmp_path, capsys):
        # --csv naming a file under a directory that doesn't exist yet
        # used to raise an uncaught FileNotFoundError instead of
        # producing the requested export -- --html already creates its
        # parent directory (Codex review regression: "Create the CSV
        # output directory before exporting").
        csv_path = tmp_path / "nested" / "does" / "not" / "exist" / "tomorrow.csv"
        assert not csv_path.parent.exists()
        code = inspect_run.main(["--run-dir", str(demo_run), "--tomorrow", "--csv", str(csv_path)])
        assert code == 0
        assert csv_path.exists()


class TestLatestAndTicker:
    def test_latest_table_has_checkmarks_and_total(self, demo_run, capsys):
        assert inspect_run.main(["--run-dir", str(demo_run), "--latest"]) == 0
        output = capsys.readouterr().out
        assert "Most recent scored day" in output
        assert "TOTAL" in output
        assert ("✓" in output) or ("✗" in output)

    def test_ticker_view_shows_rolling_hit_rate(self, demo_run, capsys):
        assert inspect_run.main(["--run-dir", str(demo_run), "--ticker", "AAA", "--last", "10"]) == 0
        output = capsys.readouterr().out
        assert "AAA — last 10 scored days" in output
        assert output.count("\n") >= 11

    def test_latest_with_unmatched_ticker_filter_is_a_normal_empty_result(self, demo_run, capsys):
        code = inspect_run.main(["--run-dir", str(demo_run), "--latest", "--tickers", "NOPE"])
        assert code == 0
        assert "match the requested tickers" in capsys.readouterr().out

    def test_unknown_model_is_refused(self, demo_run, capsys):
        assert inspect_run.main(["--run-dir", str(demo_run), "--latest", "--model", "nonsense"]) == 1
        assert "REFUSED" in capsys.readouterr().err


class TestSummary:
    def test_summary_prose_contains_report_numbers(self, demo_run, capsys):
        assert inspect_run.main(["--run-dir", str(demo_run), "--summary"]) == 0
        output = capsys.readouterr().out
        report = json.loads((demo_run / "report" / "report.json").read_text())
        close = report["by_regime"]["all"]["gbm_quantile"]["close"]
        assert f"{100 * close['coverage']:.1f}%" in output
        assert "persistence" in output
        assert (demo_run / "report" / "analysis.md").exists()
        assert "nothing is hand-written" in (demo_run / "report" / "analysis.md").read_text()

    def test_analysis_and_html_are_written_with_explicit_utf8(self, demo_run, tmp_path, capsys, monkeypatch):
        # Path.write_text() without an explicit encoding defers to the
        # platform's preferred encoding, which is not always UTF-8; the
        # generated analysis text contains non-ASCII characters (≈, →, –)
        # that would raise UnicodeEncodeError there. Assert the encoding is
        # passed explicitly for these two artifacts rather than relying on
        # this test environment's locale happening to already be UTF-8
        # (Codex review regression: "Write inspection artifacts with UTF-8
        # explicitly").
        import pathlib

        out_html = tmp_path / "dash.html"
        seen_encodings = []
        original_write_text = pathlib.Path.write_text

        def spy_write_text(self, data, *args, **kwargs):
            if self.name == "analysis.md" or self == out_html:
                seen_encodings.append(kwargs.get("encoding"))
            return original_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "write_text", spy_write_text)
        assert inspect_run.main(["--run-dir", str(demo_run), "--summary"]) == 0
        capsys.readouterr()
        assert inspect_run.main(["--run-dir", str(demo_run), "--html", str(out_html)]) == 0
        capsys.readouterr()
        assert len(seen_encodings) == 2
        assert all(encoding == "utf-8" for encoding in seen_encodings)


class TestPerTickerSkill:
    def test_intersects_by_ticker_and_origin_date(self, tmp_path):
        # Summing each model's own abs_err independently compares
        # DIFFERENT scored-date sets whenever one model has extra
        # observations the other lacks (joined the run late, a regime
        # change, a history gap) -- fabricating spurious skill instead of
        # comparing the SAME forecasts, the exact comparability rule
        # build_report()'s own aggregate-report intersection already
        # enforces (Codex review: "Intersect per-ticker skill observations
        # by forecast key").
        from ledger import Ledger

        def _row(*, model, ticker, origin, abs_err):
            return {
                "schema_version": 1,
                "record_type": "score",
                "model": model,
                "ticker": ticker,
                "origin_date": origin,
                "target_date": origin,
                "seed": 0,
                "score": {"abs_err": {"close": abs_err}},
            }

        run_dir = tmp_path / "run"
        ledger = Ledger.create(run_dir)
        records = []
        # 25 dates scored by BOTH models -- a genuine, small skill edge.
        for day in range(1, 26):
            origin = f"2021-01-{day:02d}"
            records.append(_row(model="gbm_quantile", ticker="AAA", origin=origin, abs_err=0.01))
            records.append(_row(model="persistence", ticker="AAA", origin=origin, abs_err=0.02))
        # 25 MORE dates scored only by gbm_quantile (persistence has no
        # matching observation) with a huge error -- must NOT enter the
        # comparison, since persistence was never scored against them.
        for day in range(1, 26):
            origin = f"2021-02-{day:02d}"
            records.append(_row(model="gbm_quantile", ticker="AAA", origin=origin, abs_err=100.0))
        ledger.append(records)
        _write_bare_checkpoint(run_dir, ledger)
        skill = inspect_run._per_ticker_skill(run_dir, "gbm_quantile")
        assert skill["AAA"] == pytest.approx(1.0 - 0.01 / 0.02)


class TestPercentageError:
    def test_error_pct_is_computed_in_price_space(self, tmp_path, capsys):
        # point/actual are log-returns vs ref_close; the displayed err%
        # must be the true relative price error (exp(diff) - 1), not the
        # raw log-space difference, which is only a small-difference
        # approximation -- a 0.2 log-return difference is a ~22.1% price
        # error, not 20% (Codex review regression: "Compute displayed
        # percentage error in price space").
        import math

        from data import COMPONENTS
        from ledger import Ledger
        import metrics

        run_dir = tmp_path / "run"
        ledger = Ledger.create(run_dir)
        point = {c: 0.0 for c in COMPONENTS}
        point["close"] = 0.2
        actual = {c: 0.0 for c in COMPONENTS}
        quantiles = {c: {"p10": point[c] - 0.05, "p50": point[c], "p90": point[c] + 0.05} for c in COMPONENTS}
        score = metrics.score_forecast(
            point=point, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=100.0
        )
        record = {
            "schema_version": 1,
            "record_type": "score",
            "model": "persistence",
            "ticker": "ZDIFF",
            "origin_date": "2021-06-01",
            "target_date": "2021-06-02",
            "seed": 0,
            "point": point,
            "quantiles": quantiles,
            "quantiles_raw": quantiles,
            "actual": actual,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
            "score": score,
        }
        ledger.append([record])
        _write_bare_checkpoint(run_dir, ledger)
        assert inspect_run.main(["--run-dir", str(run_dir), "--latest"]) == 0
        output = capsys.readouterr().out
        expected_pct = 100.0 * (math.exp(0.2) - 1.0)
        naive_pct = 100.0 * 0.2
        assert f"{expected_pct:.2f}%" in output
        assert f"{naive_pct:.2f}%" not in output

    def test_latest_survives_an_extreme_ledgered_point_value(self, tmp_path, capsys):
        # A ledgered record's raw "point" value (stored separately from
        # score_forecast()'s own sanitized outputs) can still be extreme --
        # e.g. an older ledger predating the scoring-side fix, or any other
        # path that ledgers a forecast without going through
        # score_forecast(). --latest's error_pct used a SEPARATE, unguarded
        # math.exp() call on that raw value, crashing inspection for
        # exactly the divergent run it would be most useful to look at
        # (Codex review regression: "Bound the latest-view percentage
        # conversion").
        from data import COMPONENTS
        from ledger import Ledger
        import metrics

        run_dir = tmp_path / "run"
        ledger = Ledger.create(run_dir)
        point = {c: 0.0 for c in COMPONENTS}
        point["close"] = 1000.0  # raw, unsanitized -- math.exp(1000) overflows
        actual = {c: 0.0 for c in COMPONENTS}
        quantiles = {c: {"p10": -0.05, "p50": 0.0, "p90": 0.05} for c in COMPONENTS}
        score = metrics.score_forecast(
            point=point, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=100.0
        )
        record = {
            "schema_version": 1,
            "record_type": "score",
            "model": "persistence",
            "ticker": "ZEXTREME",
            "origin_date": "2021-06-01",
            "target_date": "2021-06-02",
            "seed": 0,
            "point": point,
            "quantiles": quantiles,
            "quantiles_raw": quantiles,
            "actual": actual,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
            "score": score,
        }
        ledger.append([record])
        _write_bare_checkpoint(run_dir, ledger)
        assert inspect_run.main(["--run-dir", str(run_dir), "--latest"]) == 0


class TestStreamingViews:
    """--latest/--ticker no longer materialize the full model ledger into a
    list before filtering (Codex review: "Stream inspection views instead
    of loading the full ledger"); these exercise the bounded streaming
    replacements directly against a hand-built, multi-day ledger."""

    @staticmethod
    def _record(ticker, origin_date, target_date, ref_close=100.0):
        from data import COMPONENTS
        import metrics

        point = {c: 0.0 for c in COMPONENTS}
        actual = {c: 0.0 for c in COMPONENTS}
        quantiles = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in COMPONENTS}
        score = metrics.score_forecast(
            point=point, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=ref_close
        )
        return {
            "schema_version": 1,
            "record_type": "score",
            "model": "persistence",
            "ticker": ticker,
            "origin_date": origin_date,
            "target_date": target_date,
            "seed": 0,
            "point": point,
            "quantiles": quantiles,
            "quantiles_raw": quantiles,
            "actual": actual,
            "ref_close": ref_close,
            "adj_ratio": 1.0,
            "score": score,
        }

    def test_latest_picks_the_single_most_recent_day_across_many(self, tmp_path, capsys):
        from ledger import Ledger

        run_dir = tmp_path / "run"
        ledger = Ledger.create(run_dir)
        ledger.append(
            [
                self._record("BBB", "2021-06-05", "2021-06-06"),
                self._record("AAA", "2021-06-01", "2021-06-02"),
                self._record("CCC", "2021-06-05", "2021-06-06"),
                self._record("DDD", "2021-06-03", "2021-06-04"),
            ]
        )
        _write_bare_checkpoint(run_dir, ledger)
        assert inspect_run.main(["--run-dir", str(run_dir), "--latest"]) == 0
        output = capsys.readouterr().out
        assert "2021-06-06" in output
        assert "BBB" in output and "CCC" in output
        assert "AAA" not in output and "DDD" not in output

    def test_ticker_view_keeps_a_sorted_tail_despite_out_of_order_arrival(self, tmp_path, capsys):
        # A late-rebound score matures behind the cursor and can land in
        # the ledger after later-dated records; the bounded tail must stay
        # correctly sorted rather than assuming append order.
        from ledger import Ledger

        run_dir = tmp_path / "run"
        ledger = Ledger.create(run_dir)
        pairs = [
            ("2021-06-02", "2021-06-03"),
            ("2021-06-01", "2021-06-02"),
            ("2021-06-04", "2021-06-05"),
            ("2021-06-03", "2021-06-04"),
            ("2021-06-05", "2021-06-06"),
        ]
        ledger.append([self._record("AAA", origin, target) for origin, target in pairs])
        _write_bare_checkpoint(run_dir, ledger)
        assert inspect_run.main(["--run-dir", str(run_dir), "--ticker", "AAA", "--last", "3"]) == 0
        output = capsys.readouterr().out
        lines = [line for line in output.splitlines() if line.startswith("2021-")]
        shown_dates = [line.split()[0] for line in lines]
        assert shown_dates == ["2021-06-04", "2021-06-05", "2021-06-06"]


class TestBoundedReads:
    def test_latest_ignores_bytes_past_the_checkpoint_fingerprint(self, tmp_path, capsys):
        # A runtime failure between a ledger append and the next checkpoint
        # can leave a complete (or partial) gzip member past the durable
        # boundary; --latest (and every other view) must not show it, the
        # same protection write_report() already has (Codex review
        # regression: "Bound inspection reads to the checkpoint
        # fingerprints"). Uses its own run, not the shared module-scoped
        # demo_run fixture, since this test mutates ledger bytes on disk.
        import gzip

        run_dir = tmp_path / "run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        capsys.readouterr()
        assert inspect_run.main(["--run-dir", str(run_dir), "--latest"]) == 0
        before = capsys.readouterr().out

        from ledger import Ledger

        shard = next((run_dir / "ledger" / "persistence").glob("score_*.jsonl.gz"))
        template = next(Ledger(run_dir).iter_records(model="persistence", kind="score"))
        fake = dict(template, ticker="ZZZFAKE", origin_date="2099-01-01", target_date="2099-01-02")
        with shard.open("ab") as handle:
            handle.write(gzip.compress((json.dumps(fake) + "\n").encode(), mtime=0))

        assert inspect_run.main(["--run-dir", str(run_dir), "--latest"]) == 0
        after = capsys.readouterr().out
        assert "ZZZFAKE" not in after
        assert after == before

    def test_latest_uses_checkpoint_prev_during_swap_recovery(self, tmp_path, capsys):
        # If the process dies after checkpoint/ is renamed to
        # checkpoint.prev/ but before checkpoint.tmp/ is installed,
        # checkpoint/ is briefly absent even though a complete,
        # authoritative recovery checkpoint exists in checkpoint.prev/ --
        # inspection views must still bound reads using THAT checkpoint's
        # fingerprints, not read unbounded (Codex review regression:
        # "Bound ledger reads using checkpoint.prev during swap
        # recovery").
        import gzip

        run_dir = tmp_path / "run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        capsys.readouterr()
        assert inspect_run.main(["--run-dir", str(run_dir), "--latest"]) == 0
        before = capsys.readouterr().out

        from ledger import Ledger

        shard = next((run_dir / "ledger" / "persistence").glob("score_*.jsonl.gz"))
        template = next(Ledger(run_dir).iter_records(model="persistence", kind="score"))
        fake = dict(template, ticker="ZZZFAKE", origin_date="2099-01-01", target_date="2099-01-02")
        with shard.open("ab") as handle:
            handle.write(gzip.compress((json.dumps(fake) + "\n").encode(), mtime=0))

        # Simulate the crash-mid-swap disk state directly.
        (run_dir / "checkpoint").rename(run_dir / "checkpoint.prev")

        assert inspect_run.main(["--run-dir", str(run_dir), "--latest"]) == 0
        after = capsys.readouterr().out
        assert "ZZZFAKE" not in after
        assert after == before


    def test_latest_refuses_when_no_checkpoint_bounds_the_ledger(self, tmp_path, capsys):
        # Ledger shards can exist with zero checkpoint fingerprints for them
        # (both checkpoint/ and checkpoint.prev/ lost or corrupted, or a run
        # crashed before its first checkpoint) -- reading them unrestricted
        # would accept bytes with zero durability authority instead of
        # refusing, the same protection write_report() already has (Codex
        # review regression: "Refuse inspection when no checkpoint bounds
        # the ledger").
        import shutil

        run_dir = tmp_path / "run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        capsys.readouterr()
        shutil.rmtree(run_dir / "checkpoint", ignore_errors=True)
        shutil.rmtree(run_dir / "checkpoint.prev", ignore_errors=True)
        code = inspect_run.main(["--run-dir", str(run_dir), "--latest"])
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_latest_reports_refused_not_a_traceback_on_a_shortened_shard(self, tmp_path, capsys):
        # iter_records() raises LedgerError (not Refused) when a
        # checkpointed shard is shorter than what the checkpoint recorded;
        # main() only caught Refused, so --latest (and every other view)
        # surfaced an uncaught traceback instead of the documented clean
        # REFUSED result (Codex review regression: "Handle ledger integrity
        # refusals in inspect_run").
        run_dir = tmp_path / "run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        capsys.readouterr()
        shard = next((run_dir / "ledger" / "persistence").glob("score_*.jsonl.gz"))
        relpath = str(shard.relative_to(run_dir / "ledger"))
        state = json.loads((run_dir / "checkpoint" / "harness_state.json").read_text())
        expected_size = state["ledger"][relpath]["size"]
        original = shard.read_bytes()
        assert len(original) >= expected_size  # sanity: the checkpoint did fingerprint real bytes
        # Strictly below what the checkpoint recorded -- not merely below the
        # CURRENT total size, which can include un-checkpointed tail bytes
        # from steps after the last checkpoint_every boundary that
        # iter_records() legitimately ignores.
        shard.write_bytes(original[: expected_size // 2])

        # --model persistence: --latest otherwise auto-prefers octopus/
        # gbm_quantile (_MODEL_PREFERENCE), which would read a DIFFERENT
        # shard than the one just truncated and never touch the corruption.
        code = inspect_run.main(["--run-dir", str(run_dir), "--latest", "--model", "persistence"])
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err


class TestHtml:
    def test_dashboard_is_self_contained_and_complete(self, demo_run, tmp_path, capsys):
        out = tmp_path / "dash.html"
        assert inspect_run.main(["--run-dir", str(demo_run), "--html", str(out)]) == 0
        page = out.read_text()
        assert "<svg" in page and "Tomorrow" in page and "Model analysis" in page
        assert "Regimes" in page
        # Self-contained: no external fetches of any kind.
        for marker in ("http://", "https://", "src=", "@import"):
            assert marker not in page

    def test_explicit_tickers_pick_the_ribbon_tickers(self, demo_run, tmp_path, capsys):
        # _view_html() now fetches each ribbon ticker's tail via its own
        # bounded streaming pass instead of filtering a fully-materialized
        # list of every scored record (Codex review: "Stream inspection
        # views instead of loading the full ledger"); verify --tickers
        # still selects exactly the requested ribbon.
        out = tmp_path / "dash.html"
        assert inspect_run.main(["--run-dir", str(demo_run), "--html", str(out), "--tickers", "BBB"]) == 0
        page = out.read_text()
        assert "Band vs actual" in page

    def test_dashboard_marks_a_recovered_tickers_older_origin_date(self, demo_run, tmp_path):
        # Rows can carry DIFFERENT origin dates once a recovered or
        # lagging ticker's forecast is included -- the dashboard used to
        # header every row with the alphabetically-first ticker's date
        # and never show a row's own origin, misrepresenting every OTHER
        # forecast as coming from that one date (Codex review: "Preserve
        # per-ticker origin dates in the HTML forecast table").
        run_dir = tmp_path / "run_copy"
        shutil.copytree(demo_run, run_dir)
        state_path = run_dir / "checkpoint" / "harness_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        unresolved = state["outstanding"][""]
        assert unresolved  # sanity: the demo run has pending forecasts
        # Match the model _view_html() will actually pick (same
        # preference order as _pick_model()), so the injected entry
        # isn't silently filtered out by the model filter.
        picked_model = next(m for m in inspect_run._MODEL_PREFERENCE if m in {e["model"] for e in unresolved})
        source_entry = next(e for e in unresolved if e["model"] == picked_model)
        laggy_entry = dict(source_entry)
        laggy_entry["ticker"] = "ZZZLAGGY"
        laggy_entry["origin_date"] = "1999-01-01"  # deliberately ancient
        unresolved.append(laggy_entry)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        # A lagging ticker is still a CURRENT universe member (just behind
        # on data) -- unlike a ticker the universe has actually dropped, so
        # the current-universe filter (Codex review: "Filter inspection
        # forecasts to the current universe") must not exclude it here.
        meta_path = run_dir / "run_meta.json"
        run_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        run_meta["universe"]["tickers"].append("ZZZLAGGY")
        meta_path.write_text(json.dumps(run_meta), encoding="utf-8")

        out = tmp_path / "dash.html"
        assert inspect_run.main(["--run-dir", str(run_dir), "--html", str(out)]) == 0
        page = out.read_text()
        assert "ZZZLAGGY" in page
        assert "(from 1999-01-01)" in page
