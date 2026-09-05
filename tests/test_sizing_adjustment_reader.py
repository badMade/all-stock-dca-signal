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
