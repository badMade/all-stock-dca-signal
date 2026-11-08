import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sizing_adjustment_reader import (
    CombinedAdjustment,
    _read_if_fresh,
    load_combined_adjustment,
)


def _write_adjustment(
    path: Path,
    ticker: str = "AAPL",
    pct: float = 0.15,
    hours_ago: float = 1.0,
) -> None:
    generated_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    path.write_text(
        json.dumps(
            {
                "ticker": ticker,
                "multiplier_adjustment_pct": pct,
                "generated_at": generated_at,
            }
        ),
        encoding="utf-8",
    )


def test_combined_adjustment_dataclass():
    adj = CombinedAdjustment(0.12, ("source1", "source2"))
    assert adj.multiplier_adjustment_pct == 0.12
    assert adj.sources_used == ("source1", "source2")


def test_read_if_fresh_missing_file(tmp_path: Path):
    missing = tmp_path / "missing.json"
    assert _read_if_fresh(missing, "AAPL", 24.0) is None


def test_read_if_fresh_corrupt_json(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not-valid-json", encoding="utf-8")
    assert _read_if_fresh(bad, "AAPL", 24.0) is None


def test_read_if_fresh_missing_required_keys(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"ticker": "AAPL"}), encoding="utf-8")
    assert _read_if_fresh(bad, "AAPL", 24.0) is None


def test_read_if_fresh_wrong_ticker(tmp_path: Path):
    path = tmp_path / "adj.json"
    _write_adjustment(path, ticker="MSFT")
    assert _read_if_fresh(path, "AAPL", 24.0) is None


def test_read_if_fresh_stale(tmp_path: Path):
    path = tmp_path / "adj.json"
    _write_adjustment(path, ticker="AAPL", hours_ago=25.0)
    assert _read_if_fresh(path, "AAPL", 24.0) is None


def test_read_if_fresh_valid(tmp_path: Path):
    path = tmp_path / "adj.json"
    _write_adjustment(path, ticker="AAPL", pct=0.20, hours_ago=2.0)
    res = _read_if_fresh(path, "AAPL", 24.0)
    assert res == pytest.approx(0.20)


def test_load_combined_adjustment_no_sources(tmp_path: Path):
    res = load_combined_adjustment("AAPL", {}, 24.0)
    assert res is None


def test_load_combined_adjustment_all_stale(tmp_path: Path):
    f1 = tmp_path / "f1.json"
    _write_adjustment(f1, "AAPL", 0.10, hours_ago=30.0)
    res = load_combined_adjustment("AAPL", {"s1": f1}, 24.0)
    assert res is None


def test_load_combined_adjustment_most_conservative(tmp_path: Path):
    f1 = tmp_path / "f1.json"
    f2 = tmp_path / "f2.json"
    f3 = tmp_path / "f3.json"
    _write_adjustment(f1, "AAPL", 0.30, hours_ago=1.0)
    _write_adjustment(f2, "AAPL", -0.10, hours_ago=1.0)
    _write_adjustment(f3, "AAPL", 0.15, hours_ago=1.0)

    res = load_combined_adjustment("AAPL", {"s1": f1, "s2": f2, "s3": f3}, 24.0, combine="most_conservative")
    assert res is not None
    assert res.multiplier_adjustment_pct == pytest.approx(-0.10)
    assert res.sources_used == ("s2",)


def test_load_combined_adjustment_average(tmp_path: Path):
    f1 = tmp_path / "f1.json"
    f2 = tmp_path / "f2.json"
    _write_adjustment(f1, "AAPL", 0.10, hours_ago=1.0)
    _write_adjustment(f2, "AAPL", 0.20, hours_ago=1.0)

    res = load_combined_adjustment("AAPL", {"s1": f1, "s2": f2}, 24.0, combine="average")
    assert res is not None
    assert res.multiplier_adjustment_pct == pytest.approx(0.15)
    assert set(res.sources_used) == {"s1", "s2"}


def test_load_combined_adjustment_require_all_agree_missing_source(tmp_path: Path):
    f1 = tmp_path / "f1.json"
    f2 = tmp_path / "f2.json"
    _write_adjustment(f1, "AAPL", 0.10, hours_ago=1.0)
    # f2 does not exist
    res = load_combined_adjustment("AAPL", {"s1": f1, "s2": f2}, 24.0, combine="require_all_agree")
    assert res is None


def test_load_combined_adjustment_require_all_agree_disagree_signs(tmp_path: Path):
    f1 = tmp_path / "f1.json"
    f2 = tmp_path / "f2.json"
    _write_adjustment(f1, "AAPL", 0.10, hours_ago=1.0)
    _write_adjustment(f2, "AAPL", -0.10, hours_ago=1.0)

    res = load_combined_adjustment("AAPL", {"s1": f1, "s2": f2}, 24.0, combine="require_all_agree")
    assert res is None


def test_load_combined_adjustment_require_all_agree_agree_positive(tmp_path: Path):
    f1 = tmp_path / "f1.json"
    f2 = tmp_path / "f2.json"
    _write_adjustment(f1, "AAPL", 0.25, hours_ago=1.0)
    _write_adjustment(f2, "AAPL", 0.15, hours_ago=1.0)

    res = load_combined_adjustment("AAPL", {"s1": f1, "s2": f2}, 24.0, combine="require_all_agree")
    assert res is not None
    assert res.multiplier_adjustment_pct == pytest.approx(0.15)
    assert set(res.sources_used) == {"s1", "s2"}


def test_load_combined_adjustment_require_all_agree_agree_negative(tmp_path: Path):
    f1 = tmp_path / "f1.json"
    f2 = tmp_path / "f2.json"
    _write_adjustment(f1, "AAPL", -0.25, hours_ago=1.0)
    _write_adjustment(f2, "AAPL", -0.15, hours_ago=1.0)

    res = load_combined_adjustment("AAPL", {"s1": f1, "s2": f2}, 24.0, combine="require_all_agree")
    assert res is not None
    assert res.multiplier_adjustment_pct == pytest.approx(-0.15)
    assert set(res.sources_used) == {"s1", "s2"}


def test_load_combined_adjustment_unknown_policy(tmp_path: Path):
    f1 = tmp_path / "f1.json"
    _write_adjustment(f1, "AAPL", 0.10, hours_ago=1.0)
    with pytest.raises(ValueError, match="unknown combine policy"):
        load_combined_adjustment("AAPL", {"s1": f1}, 24.0, combine="invalid_policy")


def test_read_if_fresh_rejects_boolean_multiplier(tmp_path: Path):
    path = tmp_path / "bad_bool.json"
    generated_at = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps({"ticker": "AAPL", "multiplier_adjustment_pct": True, "generated_at": generated_at}))
    assert _read_if_fresh(path, "AAPL", 24.0) is None


@pytest.mark.parametrize("bad_val", ["nan", "inf", "-inf"])
def test_read_if_fresh_rejects_non_finite_multiplier(tmp_path: Path, bad_val: str):
    path = tmp_path / "bad_float.json"
    generated_at = datetime.now(timezone.utc).isoformat()
    # Write literal JSON token for NaN / Infinity
    path.write_text(f'{{"ticker": "AAPL", "multiplier_adjustment_pct": {bad_val}, "generated_at": "{generated_at}"}}')
    assert _read_if_fresh(path, "AAPL", 24.0) is None


def test_read_if_fresh_rejects_duplicate_json_keys(tmp_path: Path):
    path = tmp_path / "duplicate.json"
    generated_at = datetime.now(timezone.utc).isoformat()
    path.write_text(
        f'{{"ticker": "AAPL", "multiplier_adjustment_pct": 0.10, "multiplier_adjustment_pct": 0.20, "generated_at": "{generated_at}"}}'
    )
    assert _read_if_fresh(path, "AAPL", 24.0) is None


def test_read_if_fresh_rejects_non_dict_json(tmp_path: Path):
    path = tmp_path / "list.json"
    path.write_text(json.dumps(["AAPL", 0.15]))
    assert _read_if_fresh(path, "AAPL", 24.0) is None


def test_read_if_fresh_rejects_naive_timestamp(tmp_path: Path):
    path = tmp_path / "naive.json"
    path.write_text(
        json.dumps({"ticker": "AAPL", "multiplier_adjustment_pct": 0.15, "generated_at": "2026-11-08T12:00:00"})
    )
    assert _read_if_fresh(path, "AAPL", 24.0) is None


def test_read_if_fresh_rejects_future_timestamp(tmp_path: Path):
    path = tmp_path / "future.json"
    future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    path.write_text(json.dumps({"ticker": "AAPL", "multiplier_adjustment_pct": 0.15, "generated_at": future_time}))
    assert _read_if_fresh(path, "AAPL", 24.0) is None


@pytest.mark.parametrize("bad_hours", [0, -1, float("nan"), float("inf"), True])
def test_read_if_fresh_rejects_invalid_staleness_hours(tmp_path: Path, bad_hours):
    path = tmp_path / "valid.json"
    _write_adjustment(path, "AAPL", 0.15, hours_ago=1.0)
    assert _read_if_fresh(path, "AAPL", bad_hours) is None


def test_read_if_fresh_rejects_oversized_integer_overflow(tmp_path: Path):
    path = tmp_path / "overflow.json"
    generated_at = datetime.now(timezone.utc).isoformat()
    # 400-digit integer exceeds float max (~1e308)
    huge = "9" * 400
    path.write_text(f'{{"ticker": "AAPL", "multiplier_adjustment_pct": {huge}, "generated_at": "{generated_at}"}}')
    assert _read_if_fresh(path, "AAPL", 24.0) is None
