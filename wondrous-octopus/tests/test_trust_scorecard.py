"""Rock 3 of the band-calibration cycle: the trust scorecard over time.

Written BEFORE the implementation (TDD-first; see
``claude/band-calibration/PLAN.md``). ``trust_scorecard`` does not exist yet.

The question this answers is the operator's own: "how well is this working,
and can I judge that over time?" One row per (as-of date, model, horizon),
appended to a history file and never rewritten. Each row is a rolling-window
reading of the p10-p90 band -- coverage, breach side, interval score, skill
against persistence -- with the Rock 2 date-block interval around it, and a
status that is one of:

- ``ok``       the coverage interval contains the 80% target;
- ``flagged``  the interval excludes it (band too narrow OR too wide);
- ``unknown``  there is not enough history to say.

``unknown`` is its own status on purpose. A scorecard that reads "ok" for a
90-day horizon on six weeks of data is the failure this cycle exists to
remove.
"""

from __future__ import annotations

import json

import band_uncertainty as bu
import metrics
import numpy as np
import pytest
import trust_scorecard as ts
from data import COMPONENTS
from ledger import Ledger


def _dates(count: int, start: str = "2020-01-01") -> list[str]:
    first = np.datetime64(start)
    return [str(first + np.timedelta64(offset, "D")) for offset in range(count)]


def _series(dates, *, in_band, n=100.0, below=None, above=None, interval_score=None, sided=True):
    """A DailyBandSeries with ``n`` forecasts per date and the given hits."""
    count = len(dates)
    hits = np.broadcast_to(np.asarray(in_band, dtype=float), (count,)).copy()
    totals = np.full(count, float(n))
    misses = totals - hits
    below_arr = misses / 2 if below is None else np.broadcast_to(np.asarray(below, dtype=float), (count,)).copy()
    above_arr = misses / 2 if above is None else np.broadcast_to(np.asarray(above, dtype=float), (count,)).copy()
    scores = (
        np.full(count, 4.0)
        if interval_score is None
        else np.broadcast_to(np.asarray(interval_score, dtype=float), (count,)).copy()
    )
    return bu.DailyBandSeries(
        dates=list(dates),
        n=totals,
        n_sided=totals.copy() if sided else np.zeros(count),
        in_band=hits,
        below=below_arr if sided else np.zeros(count),
        above=above_arr if sided else np.zeros(count),
        interval_score=scores,
    )


def _alternating(count: int, low: float, high: float) -> np.ndarray:
    return np.array([low if index % 2 == 0 else high for index in range(count)])


# --------------------------------------------------------------------------
# Window policy
# --------------------------------------------------------------------------


class TestWindowPolicy:
    @pytest.mark.parametrize(("horizon", "expected"), [(1, 5), (5, 5), (21, 21), (63, 63), (90, 90)])
    def test_block_is_at_least_the_horizon_and_never_under_a_week(self, horizon, expected):
        # Horizon-h targets from consecutive days overlap by h-1 days, so a
        # block shorter than h splits one dependent run across blocks. The
        # floor of 5 covers day-to-day volatility clustering at h = 1.
        assert ts.block_length(horizon) == expected

    @pytest.mark.parametrize(("horizon", "expected"), [(1, 63), (5, 63), (21, 84), (63, 252), (90, 360)])
    def test_window_is_the_signed_off_63_days_or_four_blocks(self, horizon, expected):
        # The operator signed off a 63-trading-day window. At horizon 63
        # that is ONE block and at horizon 90 less than one, from which no
        # interval can be estimated, so long horizons widen to four blocks.
        assert ts.window_length(horizon) == expected


# --------------------------------------------------------------------------
# One row
# --------------------------------------------------------------------------


class TestScorecardRow:
    def test_a_band_on_target_is_ok(self):
        dates = _dates(300)
        series = _series(dates, in_band=_alternating(300, 78.0, 82.0))
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert row["status"] == "ok"
        assert row["reasons"] == []
        assert row["coverage"]["point"] == pytest.approx(0.8, abs=0.005)
        assert row["coverage"]["low"] <= 0.8 <= row["coverage"]["high"]
        assert row["target_coverage"] == pytest.approx(0.8)

    def test_row_identity_and_window_bounds(self):
        dates = _dates(300)
        series = _series(dates, in_band=80.0)
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert (row["as_of"], row["model"], row["horizon"]) == (dates[-1], "octopus", 1)
        assert row["window_dates"] == 63
        assert row["window_start"] == dates[-63]
        assert row["window_end"] == dates[-1]
        assert row["n"] == 6300
        assert row["block_length"] == 5

    def test_a_band_that_is_too_narrow_is_flagged(self):
        dates = _dates(300)
        series = _series(dates, in_band=_alternating(300, 58.0, 62.0))
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert row["status"] == "flagged"
        assert row["reasons"] == ["coverage_below_target"]

    def test_a_band_that_is_too_wide_is_also_flagged(self):
        # 95% coverage is not "better than 80%": the band is wider than it
        # claims to be, and a sizing suggestion read off its edge is wrong.
        dates = _dates(300)
        series = _series(dates, in_band=_alternating(300, 94.0, 96.0))
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert row["status"] == "flagged"
        assert row["reasons"] == ["coverage_above_target"]

    def test_nothing_after_the_as_of_date_is_read(self):
        # The lookahead guard. The last 100 dates are a coverage collapse;
        # a row dated before them must not know.
        dates = _dates(300)
        in_band = np.concatenate([_alternating(200, 78.0, 82.0), np.zeros(100)])
        series = _series(dates, in_band=in_band)
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[199])
        assert row["status"] == "ok"
        assert row["window_end"] == dates[199]
        assert row["coverage"]["point"] == pytest.approx(0.8, abs=0.005)

    def test_only_the_rolling_window_is_read(self):
        # Old history must age out, or a model that degraded last quarter
        # would hide behind twenty good years.
        dates = _dates(300)
        in_band = np.concatenate([np.full(237, 100.0), _alternating(63, 58.0, 62.0)])
        series = _series(dates, in_band=in_band)
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert row["coverage"]["point"] == pytest.approx(0.6, abs=0.005)
        assert row["status"] == "flagged"

    def test_too_little_history_is_unknown_never_ok(self):
        # Horizon 21 needs two 21-day blocks at the very least; 30 dates is
        # not enough. The point estimate is still shown.
        dates = _dates(30)
        series = _series(dates, in_band=80.0)
        row = ts.scorecard_row(series, model="octopus", horizon=21, as_of=dates[-1])
        assert row["status"] == "unknown"
        assert row["reasons"] == ["too_few_blocks"]
        assert row["coverage"]["point"] == pytest.approx(0.8)
        assert row["coverage"]["low"] is None and row["coverage"]["high"] is None

    def test_no_observations_at_all_is_unknown(self):
        dates = _dates(100, start="2021-01-01")
        series = _series(dates, in_band=80.0)
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of="2020-06-01")
        assert row["status"] == "unknown"
        assert row["reasons"] == ["no_observations"]
        assert row["coverage"]["point"] is None
        assert row["n"] == 0

    def test_a_missing_series_is_unknown(self):
        row = ts.scorecard_row(None, model="octopus", horizon=90, as_of="2021-06-01")
        assert row["status"] == "unknown"
        assert row["reasons"] == ["no_observations"]

    def test_breach_side_rates(self):
        dates = _dates(300)
        series = _series(dates, in_band=80.0, below=15.0, above=5.0)
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert row["below_rate"] == pytest.approx(0.15)
        assert row["above_rate"] == pytest.approx(0.05)

    def test_breach_side_is_none_when_the_ledger_never_recorded_it(self):
        dates = _dates(300)
        series = _series(dates, in_band=80.0, sided=False)
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert row["below_rate"] is None and row["above_rate"] is None
        # Coverage does not need the side, so the row is still judged.
        assert row["status"] == "ok"

    def test_interval_score_is_the_mean_per_forecast(self):
        dates = _dates(300)
        series = _series(dates, in_band=80.0, interval_score=4.0)
        row = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert row["interval_score"]["point"] == pytest.approx(0.04)

    def test_skill_against_the_baseline_on_interval_score(self):
        dates = _dates(300)
        model = _series(dates, in_band=80.0, interval_score=3.0)
        baseline = _series(dates, in_band=80.0, interval_score=4.0)
        row = ts.scorecard_row(model, model="octopus", horizon=1, as_of=dates[-1], baseline=baseline)
        assert row["skill_vs_persistence"]["point"] == pytest.approx(0.25)
        assert row["skill_vs_persistence"]["low"] == pytest.approx(0.25)

    def test_skill_uses_only_dates_both_models_were_scored_on(self):
        # The baseline is missing the last 10 dates. Comparing the model's
        # full window against a shorter baseline window would compare two
        # different sets of days.
        dates = _dates(300)
        model = _series(dates, in_band=80.0, interval_score=np.concatenate([np.full(290, 3.0), np.full(10, 300.0)]))
        baseline = _series(dates[:290], in_band=80.0, interval_score=4.0)
        row = ts.scorecard_row(model, model="octopus", horizon=1, as_of=dates[-1], baseline=baseline)
        assert row["skill_vs_persistence"]["point"] == pytest.approx(0.25)

    def test_no_baseline_means_no_skill(self):
        dates = _dates(300)
        series = _series(dates, in_band=80.0)
        row = ts.scorecard_row(series, model="persistence", horizon=1, as_of=dates[-1])
        assert row["skill_vs_persistence"] is None

    def test_same_inputs_same_row(self):
        dates = _dates(300)
        rng = np.random.default_rng(5)
        series = _series(dates, in_band=rng.binomial(100, 0.8, size=300).astype(float))
        first = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        second = ts.scorecard_row(series, model="octopus", horizon=1, as_of=dates[-1])
        assert first == second

    def test_row_is_plain_json(self):
        dates = _dates(300)
        row = ts.scorecard_row(_series(dates, in_band=80.0), model="octopus", horizon=1, as_of=dates[-1])
        assert json.loads(json.dumps(row)) == row


# --------------------------------------------------------------------------
# Per-ticker coverage tests, summarized as rejection shares
# --------------------------------------------------------------------------


class TestCoverageTestShares:
    def test_well_behaved_tickers_reject_near_the_nominal_rate(self):
        # 200 tickers, each 250 independent days at the promised 20% miss
        # rate. About 5% of tests reject by chance; far more would mean the
        # tests (or their wiring) are wrong.
        rng = np.random.default_rng(31)
        breaches = {f"T{index:03d}": (rng.random(250) < 0.2).tolist() for index in range(200)}
        shares = ts.coverage_test_shares(breaches)
        assert shares["tickers_tested"] == 200
        assert shares["kupiec_reject_share"] < 0.15
        assert shares["independence_reject_share"] < 0.15

    def test_tickers_missing_too_often_are_caught_by_the_rate_test(self):
        rng = np.random.default_rng(32)
        breaches = {f"T{index:03d}": (rng.random(250) < 0.5).tolist() for index in range(50)}
        shares = ts.coverage_test_shares(breaches)
        assert shares["kupiec_reject_share"] > 0.9

    def test_clustered_misses_are_caught_by_the_independence_test(self):
        # The right number of misses, all in one run.
        breaches = {f"T{index:03d}": [False] * 200 + [True] * 50 for index in range(50)}
        shares = ts.coverage_test_shares(breaches)
        assert shares["kupiec_reject_share"] == pytest.approx(0.0)
        assert shares["independence_reject_share"] == pytest.approx(1.0)

    def test_short_sequences_are_not_tested(self):
        breaches = {"LONG": [False, False, False, False, True] * 20, "SHORT": [True] * 10}
        shares = ts.coverage_test_shares(breaches, min_observations=30)
        assert shares["tickers_tested"] == 1

    def test_nothing_to_test_reports_none(self):
        shares = ts.coverage_test_shares({})
        assert shares == {"tickers_tested": 0, "kupiec_reject_share": None, "independence_reject_share": None}


# --------------------------------------------------------------------------
# The append-only history file
# --------------------------------------------------------------------------


def _row(as_of="2021-06-01", model="octopus", horizon=1, status="ok"):
    return {"as_of": as_of, "model": model, "horizon": horizon, "status": status, "reasons": []}


class TestHistoryFile:
    def test_append_then_load(self, tmp_path):
        path = tmp_path / "trust" / "trust_scorecard.jsonl"
        rows = [_row(horizon=1), _row(horizon=5)]
        assert ts.append_rows(path, rows) == 2
        assert ts.load_history(path) == rows

    def test_one_json_object_per_line(self, tmp_path):
        path = tmp_path / "trust_scorecard.jsonl"
        ts.append_rows(path, [_row(horizon=1), _row(horizon=5)])
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert all(isinstance(json.loads(line), dict) for line in lines)

    def test_appending_the_same_row_again_is_a_no_op(self, tmp_path):
        # A scheduled job that retries must not double-count a day.
        path = tmp_path / "trust_scorecard.jsonl"
        ts.append_rows(path, [_row()])
        before = path.read_bytes()
        assert ts.append_rows(path, [_row()]) == 0
        assert path.read_bytes() == before

    def test_an_existing_row_is_never_rewritten(self, tmp_path):
        # History is evidence. A later run that would now judge the same
        # day differently must not edit what was recorded at the time.
        path = tmp_path / "trust_scorecard.jsonl"
        ts.append_rows(path, [_row(status="ok")])
        assert ts.append_rows(path, [_row(status="flagged")]) == 0
        assert ts.load_history(path)[0]["status"] == "ok"

    def test_new_rows_go_after_the_old_ones_which_stay_byte_identical(self, tmp_path):
        path = tmp_path / "trust_scorecard.jsonl"
        ts.append_rows(path, [_row(as_of="2021-06-01")])
        before = path.read_bytes()
        assert ts.append_rows(path, [_row(as_of="2021-06-02")]) == 1
        assert path.read_bytes().startswith(before)
        assert [row["as_of"] for row in ts.load_history(path)] == ["2021-06-01", "2021-06-02"]

    def test_no_temp_file_is_left_behind(self, tmp_path):
        path = tmp_path / "trust_scorecard.jsonl"
        ts.append_rows(path, [_row()])
        assert [entry.name for entry in tmp_path.iterdir()] == ["trust_scorecard.jsonl"]

    def test_a_missing_file_loads_as_empty(self, tmp_path):
        assert ts.load_history(tmp_path / "absent.jsonl") == []

    def test_a_corrupt_history_is_refused_not_overwritten(self, tmp_path):
        # Fail closed: appending past a line that does not parse would bury
        # the damage under fresh rows.
        path = tmp_path / "trust_scorecard.jsonl"
        path.write_text('{"as_of": "2021-06-01", "model": "octopus", "horizon": 1}\n{not json\n', encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(ts.ScorecardError):
            ts.append_rows(path, [_row(as_of="2021-06-02")])
        assert path.read_bytes() == before


# --------------------------------------------------------------------------
# From score records to rows
# --------------------------------------------------------------------------


def _score(actual_close):
    point = {c: 0.0 for c in COMPONENTS}
    quantiles = {c: {"p10": -0.02, "p50": 0.0, "p90": 0.02} for c in COMPONENTS}
    actual = {"open": 0.0, "high": max(0.0, actual_close), "low": min(0.0, actual_close), "close": actual_close}
    return metrics.score_forecast(
        point=point, quantiles=quantiles, quantiles_raw=quantiles, actual=actual, ref_close=100.0
    )


def _records(n_dates=150, *, models=("octopus", "persistence"), horizons=(1, 5), miss_every=5):
    """Five tickers per date; one in ``miss_every`` ticker-days misses below."""
    dates = _dates(n_dates, start="2021-01-01")
    records = []
    for day, date in enumerate(dates):
        for index in range(5):
            miss = (day + index) % miss_every == 0
            score = _score(-0.05 if miss else 0.0)
            for model in models:
                for horizon in horizons:
                    record = {
                        "record_type": "score" if horizon == 1 else "horizon_score",
                        "model": model,
                        "ticker": f"T{index}",
                        "origin_date": date,
                        "target_date": date,
                        "score": score,
                    }
                    if horizon != 1:
                        record["horizon"] = horizon
                    records.append(record)
    return dates, records


class TestBuildRows:
    def test_one_row_per_model_and_requested_horizon(self):
        dates, records = _records()
        rows = ts.build_rows(records, as_of=dates[-1], horizons=(1, 5))
        assert {(row["model"], row["horizon"]) for row in rows} == {
            ("octopus", 1),
            ("octopus", 5),
            ("persistence", 1),
            ("persistence", 5),
        }

    def test_a_requested_horizon_with_no_data_is_an_explicit_unknown_row(self):
        # Silence would read as "nothing to worry about".
        dates, records = _records(horizons=(1,))
        rows = ts.build_rows(records, as_of=dates[-1], horizons=(1, 90))
        row = next(row for row in rows if row["model"] == "octopus" and row["horizon"] == 90)
        assert row["status"] == "unknown"
        assert row["reasons"] == ["no_observations"]

    def test_coverage_comes_through_from_the_records(self):
        dates, records = _records()
        rows = ts.build_rows(records, as_of=dates[-1], horizons=(1,))
        row = next(row for row in rows if row["model"] == "octopus")
        assert row["coverage"]["point"] == pytest.approx(0.8)
        assert row["below_rate"] == pytest.approx(0.2)
        assert row["above_rate"] == pytest.approx(0.0)
        assert row["status"] == "ok"

    def test_persistence_has_no_skill_row_and_others_are_measured_against_it(self):
        dates, records = _records()
        rows = {(row["model"], row["horizon"]): row for row in ts.build_rows(records, as_of=dates[-1], horizons=(1,))}
        assert rows[("persistence", 1)]["skill_vs_persistence"] is None
        # Identical scores for both models here, so skill is exactly zero.
        assert rows[("octopus", 1)]["skill_vs_persistence"]["point"] == pytest.approx(0.0)

    def test_per_ticker_tests_are_reported_at_horizon_one_only(self):
        dates, records = _records()
        rows = {(row["model"], row["horizon"]): row for row in ts.build_rows(records, as_of=dates[-1], horizons=(1, 5))}
        assert rows[("octopus", 1)]["coverage_tests"]["tickers_tested"] == 5
        # At longer horizons breaches are serially dependent by
        # construction, so the independence test would reject a sound band.
        assert rows[("octopus", 5)]["coverage_tests"] is None

    def test_records_after_the_as_of_date_are_ignored(self):
        dates, records = _records()
        early = ts.build_rows(records, as_of=dates[99], horizons=(1,))
        truncated = ts.build_rows(
            [record for record in records if record["target_date"] <= dates[99]], as_of=dates[99], horizons=(1,)
        )
        assert early == truncated


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _run_dir_with_ledger(tmp_path, records, *, checkpoint=True):
    run_dir = tmp_path / "run"
    ledger = Ledger.create(run_dir)
    ledger.append(records)
    if checkpoint:
        checkpoint_dir = run_dir / "checkpoint"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "harness_state.json").write_text(
            json.dumps({"ledger": ledger.fingerprints()}), encoding="utf-8"
        )
    return run_dir


class TestCommandLine:
    def test_writes_the_history_under_the_run_directory(self, tmp_path, capsys):
        dates, records = _records()
        run_dir = _run_dir_with_ledger(tmp_path, records)
        assert ts.main(["--run-dir", str(run_dir), "--horizons", "1", "5"]) == 0
        history = ts.load_history(run_dir / "trust" / "trust_scorecard.jsonl")
        assert len(history) == 4
        assert {row["as_of"] for row in history} == {dates[-1]}
        assert "octopus" in capsys.readouterr().out

    def test_as_of_defaults_to_the_latest_scored_date(self, tmp_path):
        dates, records = _records()
        run_dir = _run_dir_with_ledger(tmp_path, records)
        ts.main(["--run-dir", str(run_dir), "--horizons", "1"])
        history = ts.load_history(run_dir / "trust" / "trust_scorecard.jsonl")
        assert history[0]["as_of"] == dates[-1]

    def test_explicit_as_of(self, tmp_path):
        dates, records = _records()
        run_dir = _run_dir_with_ledger(tmp_path, records)
        ts.main(["--run-dir", str(run_dir), "--horizons", "1", "--as-of", dates[99]])
        history = ts.load_history(run_dir / "trust" / "trust_scorecard.jsonl")
        assert {row["as_of"] for row in history} == {dates[99]}

    def test_second_run_for_the_same_date_adds_nothing(self, tmp_path):
        _dates_, records = _records()
        run_dir = _run_dir_with_ledger(tmp_path, records)
        ts.main(["--run-dir", str(run_dir), "--horizons", "1"])
        path = run_dir / "trust" / "trust_scorecard.jsonl"
        before = path.read_bytes()
        assert ts.main(["--run-dir", str(run_dir), "--horizons", "1"]) == 0
        assert path.read_bytes() == before

    def test_dry_run_prints_and_writes_nothing(self, tmp_path, capsys):
        _dates_, records = _records()
        run_dir = _run_dir_with_ledger(tmp_path, records)
        assert ts.main(["--run-dir", str(run_dir), "--horizons", "1", "--dry-run"]) == 0
        assert not (run_dir / "trust").exists()
        assert "octopus" in capsys.readouterr().out

    def test_refuses_a_run_with_no_checkpoint(self, tmp_path, capsys):
        # Ledger bytes without a checkpoint are not proven durable; the
        # other Octopus readers refuse them and so must this one.
        _dates_, records = _records()
        run_dir = _run_dir_with_ledger(tmp_path, records, checkpoint=False)
        assert ts.main(["--run-dir", str(run_dir), "--horizons", "1"]) == 1
        assert not (run_dir / "trust").exists()
        assert "REFUSED" in capsys.readouterr().err

    def test_a_flagged_band_is_reported_in_the_output_and_still_exits_zero(self, tmp_path, capsys):
        # The flag is a label on research output, not a failure of the
        # tool: a scheduled job must not read "band is off" as "job broke".
        _dates_, records = _records(miss_every=2)
        run_dir = _run_dir_with_ledger(tmp_path, records)
        assert ts.main(["--run-dir", str(run_dir), "--horizons", "1"]) == 0
        assert "flagged" in capsys.readouterr().out
