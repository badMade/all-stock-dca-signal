"""Dated copies of the 1..H horizon forecast written by daily_update."""

from __future__ import annotations

import daily_update

HEADER = "ticker,origin_date,horizon,ref_close,close_point\n"


def test_no_horizons_dir_is_a_noop(tmp_path):
    assert daily_update._write_horizon_forecasts(tmp_path) == []
    assert not (tmp_path / "daily").exists()


def test_copy_is_named_for_origin_date_and_model(tmp_path):
    horizons = tmp_path / "horizons"
    horizons.mkdir()
    body = HEADER + "AA,2026-11-09,1,10.0,0.01\nAA,2026-11-09,2,10.0,0.02\n"
    (horizons / "octopus.csv").write_text(body, encoding="utf-8")
    (horizons / "persistence.csv").write_text(body, encoding="utf-8")

    written = daily_update._write_horizon_forecasts(tmp_path)

    names = sorted(path.name for path in written)
    assert names == [
        "2026-11-09_horizon_forecasts_octopus.csv",
        "2026-11-09_horizon_forecasts_persistence.csv",
    ]
    assert (tmp_path / "daily" / names[0]).read_text(encoding="utf-8") == body


def test_next_day_does_not_overwrite_yesterdays_copy(tmp_path):
    horizons = tmp_path / "horizons"
    horizons.mkdir()
    (horizons / "octopus.csv").write_text(HEADER + "AA,2026-11-09,1,10.0,0.01\n", encoding="utf-8")
    daily_update._write_horizon_forecasts(tmp_path)
    (horizons / "octopus.csv").write_text(HEADER + "AA,2026-11-10,1,10.1,0.03\n", encoding="utf-8")
    daily_update._write_horizon_forecasts(tmp_path)

    daily = tmp_path / "daily"
    assert (daily / "2026-11-09_horizon_forecasts_octopus.csv").read_text(encoding="utf-8").count("0.01") == 1
    assert (daily / "2026-11-10_horizon_forecasts_octopus.csv").exists()


def test_header_only_file_is_skipped(tmp_path):
    horizons = tmp_path / "horizons"
    horizons.mkdir()
    (horizons / "octopus.csv").write_text(HEADER, encoding="utf-8")
    assert daily_update._write_horizon_forecasts(tmp_path) == []
