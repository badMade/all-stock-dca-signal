"""Tests for the plan-first recommendation store, its freshness rule and the reader CLI."""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_rec_store
from dca_rec_models import ExpectedPrice, ExpectedPriceSet, Recommendation, RecommendationSet
from dca_rec_store import RecommendationStore, load_settings
from scripts import read_recommendation
from scripts.validate_native_placement import native_ref_id

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 13)
MAX_AGE = timedelta(minutes=15)
REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


def _at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=ET)


def _pair(slot: str, as_of: datetime, orders=(("AAPL", 3.0),)):
    day = as_of.astimezone(ET).date()
    rec = RecommendationSet(
        day, as_of, slot, "robinhood_agent_fetch", "ladder-v1", "hash-v1",
        tuple(Recommendation(t, usd, native_ref_id(slot, day.isoformat(), t)) for t, usd in orders),
    )
    expected = ExpectedPriceSet(
        day, as_of, slot, "run_state_artifacts",
        tuple(ExpectedPrice(t, 100.0, 102.0, 99.0, 1.5, 1.96, usd) for t, usd in orders),
        {"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900},
    )
    return rec, expected


@pytest.fixture
def store(tmp_path):
    return RecommendationStore(tmp_path / "recommendations")


def test_operator_example_1207_uses_the_1200_file(store):
    store.write(*_pair("1230", _at(12, 0)))
    selection = store.select_for("1230", _at(12, 7), MAX_AGE)
    assert selection.status == "OK"
    assert selection.recommendation.as_of == _at(12, 0)
    assert selection.age_seconds == 420


def test_newest_fresh_file_wins(store):
    store.write(*_pair("1230", _at(12, 15), (("AAPL", 3.0),)))
    store.write(*_pair("1230", _at(12, 25), (("MSFT", 4.0),)))
    selection = store.select_for("1230", _at(12, 31), MAX_AGE)
    assert [item.ticker for item in selection.recommendation.items] == ["MSFT"]


def test_file_older_than_max_age_is_refused(store):
    store.write(*_pair("1230", _at(12, 0)))
    selection = store.select_for("1230", _at(12, 16), MAX_AGE)
    assert (selection.status, selection.reason) == ("NONE", "stale")
    assert selection.recommendation is None


def test_exactly_max_age_is_still_fresh(store):
    store.write(*_pair("1230", _at(12, 0)))
    assert store.select_for("1230", _at(12, 15), MAX_AGE).status == "OK"


def test_prior_day_file_is_never_used(store):
    store.write(*_pair("1530", _at(15, 55, day=date(2026, 10, 12))))
    selection = store.select_for("1530", _at(9, 40), MAX_AGE)
    assert (selection.status, selection.reason) == ("NONE", "no_file_for_slot_today")


def test_future_stamped_file_is_skipped_for_an_older_fresh_one(store):
    store.write(*_pair("1230", _at(12, 25)))
    store.write(*_pair("1230", _at(12, 31)))
    selection = store.select_for("1230", _at(12, 30, 30), MAX_AGE)
    assert selection.recommendation.as_of == _at(12, 25)


def test_only_future_stamped_files_means_none(store):
    store.write(*_pair("1230", _at(12, 31)))
    assert store.select_for("1230", _at(12, 30), MAX_AGE).reason == "only_future_stamped_files"


def test_another_slots_file_is_not_used(store):
    # The 12:25 cycle's 11:30 file must not satisfy the 12:30 agent.
    store.write(*_pair("1130", _at(12, 25)))
    assert store.select_for("1230", _at(12, 30), MAX_AGE).reason == "no_file_for_slot_today"


def test_unknown_slot_is_refused(store):
    assert store.select_for("1200", _at(12, 0), MAX_AGE).reason == "unknown_slot"


def test_corrupt_newest_file_fails_closed_instead_of_falling_back(store):
    store.write(*_pair("1230", _at(12, 20)))
    newest = store.write(*_pair("1230", _at(12, 25)))
    newest.write_text("{not json", encoding="utf-8")
    assert store.select_for("1230", _at(12, 30), MAX_AGE).reason == "newest_file_invalid"


def test_duplicate_keys_in_newest_file_fail_closed(store):
    path = store.write(*_pair("1230", _at(12, 25)))
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('{"as_of"', '{"slot":"1230","as_of"', 1), encoding="utf-8")
    assert store.select_for("1230", _at(12, 30), MAX_AGE).reason == "newest_file_invalid"


def test_file_whose_content_disagrees_with_its_name_is_refused(store):
    path = store.write(*_pair("1130", _at(12, 25)))
    renamed = path.with_name("rec_1230_1225.json")
    shutil.copy(path, renamed)
    assert store.select_for("1230", _at(12, 30), MAX_AGE).reason == "file_identity_mismatch"


def test_unrelated_names_in_the_day_dir_are_ignored(store):
    store.write(*_pair("1230", _at(12, 25)))
    (store.day_dir(DAY) / "rec_1230_latest.json").write_text("{}", encoding="utf-8")
    assert store.select_for("1230", _at(12, 30), MAX_AGE).status == "OK"


def test_agent_file_has_no_price_fields_and_expected_twin_has_them(store):
    rec, expected = _pair("1230", _at(12, 25))
    path = store.write(rec, expected)
    agent_text = path.read_text(encoding="utf-8")
    for word in ("price", "close", "low", "quote", "bar"):
        assert word not in agent_text
    assert store.expected_for(rec) == expected
    assert (store.day_dir(DAY) / "expected_1230_1225.json").exists()


def test_writes_are_canonical_and_repeatable(store):
    rec, expected = _pair("1230", _at(12, 25))
    first = store.write(rec, expected).read_bytes()
    second = store.write(rec, expected).read_bytes()
    assert first == second
    assert json.loads(first) == rec.to_json()


@pytest.mark.parametrize("change", ["slot", "tickers"])
def test_mismatched_pair_is_refused_and_nothing_is_written(store, change):
    rec, expected = _pair("1230", _at(12, 25))
    if change == "slot":
        _, expected = _pair("1130", _at(12, 25))
    else:
        _, expected = _pair("1230", _at(12, 25), (("MSFT", 4.0),))
    with pytest.raises(ValueError):
        store.write(rec, expected)
    assert not store.day_dir(DAY).exists()


def test_naive_now_is_rejected(store):
    with pytest.raises(ValueError):
        store.select_for("1230", datetime(2026, 10, 13, 12, 0), MAX_AGE)


def test_default_root_follows_cron_tracking_root(monkeypatch, tmp_path):
    monkeypatch.setattr(dca_rec_store.dca_paths, "stream_root", lambda *parts: str(tmp_path.joinpath(*parts)))
    assert RecommendationStore().root == tmp_path / "recommendations"


# --- settings ------------------------------------------------------------------------------


def test_repo_settings_load_with_the_operator_rule():
    settings = load_settings(REPO_CONFIG)
    assert settings.max_age == timedelta(minutes=15)
    assert settings.lookahead == timedelta(minutes=15)


def _settings_dir(tmp_path, **overrides):
    data = json.loads((REPO_CONFIG / "recommendations.json").read_text(encoding="utf-8"))
    data.update(overrides)
    (tmp_path / "recommendations.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "overrides",
    [{"max_age_minutes": 0}, {"max_age_minutes": 61}, {"max_age_minutes": True}, {"max_age_minutes": 15.0},
     {"lookahead_minutes": -1}, {"publish": []}],
)
def test_out_of_range_settings_fail_closed(tmp_path, overrides):
    with pytest.raises(ValueError):
        load_settings(_settings_dir(tmp_path, **overrides))


def test_missing_settings_file_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        load_settings(tmp_path)


def test_default_settings_dir_is_dca_configs(monkeypatch):
    monkeypatch.setattr(dca_rec_store.dca_config, "get_config_dir", lambda: REPO_CONFIG)
    assert load_settings().max_age == MAX_AGE


# --- reader CLI ----------------------------------------------------------------------------


def _run(capsys, root, *args):
    code = read_recommendation.main(["--root", str(root), "--config-dir", str(REPO_CONFIG), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_prints_only_ticker_usd_ref_id(store, capsys):
    store.write(*_pair("1230", _at(12, 0), (("AAPL", 3.0), ("MSFT", 1.5))))
    code, out, _ = _run(capsys, store.root, "--slot", "1230", "--now", _at(12, 7).isoformat())
    document = json.loads(out)
    assert code == 0
    assert document["status"] == "OK" and document["age_s"] == 420
    assert [set(order) for order in document["orders"]] == [{"ticker", "usd", "ref_id"}] * 2
    for word in ("price", "close", "quote"):
        assert word not in out


def test_cli_stale_exits_3_with_reason(store, capsys):
    store.write(*_pair("1230", _at(12, 0)))
    code, out, _ = _run(capsys, store.root, "--slot", "1230", "--now", _at(12, 20).isoformat())
    assert code == 3
    assert json.loads(out) == {"status": "NONE", "slot": "1230", "reason": "stale"}


def test_cli_empty_buy_list_is_ok(store, capsys):
    store.write(*_pair("1230", _at(12, 0), ()))
    code, out, _ = _run(capsys, store.root, "--slot", "1230", "--now", _at(12, 1).isoformat())
    assert code == 0 and json.loads(out)["orders"] == []


def test_cli_naive_now_is_a_usage_error(store, capsys):
    code, out, err = _run(capsys, store.root, "--slot", "1230", "--now", "2026-10-13T12:00:00")
    assert code == 2 and out == "" and "UTC offset" in err


def test_cli_bad_settings_is_a_usage_error(tmp_path, capsys):
    code = read_recommendation.main(["--slot", "1230", "--config-dir", str(tmp_path), "--root", str(tmp_path)])
    assert code == 2
    assert "missing" in capsys.readouterr().err


def test_cli_default_now_uses_the_clock(store, capsys, monkeypatch):
    monkeypatch.setattr(read_recommendation, "_now", lambda text: _at(12, 5))
    store.write(*_pair("1230", _at(12, 0)))
    code, out, _ = _run(capsys, store.root, "--slot", "1230")
    assert code == 0


def test_cli_now_parser_defaults_to_an_aware_clock():
    assert read_recommendation._now(None).utcoffset() is not None


def test_cli_module_runs_as_main(store, tmp_path):
    store.write(*_pair("1230", _at(12, 0)))
    import subprocess
    import sys

    env = {**os.environ, "DCA_DISABLE_DOTENV": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.read_recommendation", "--slot", "1230", "--now", _at(12, 1).isoformat(),
         "--root", str(store.root), "--config-dir", str(REPO_CONFIG)],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], env=env, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "OK"


# --- PR #1667 review follow-ups -----------------------------------------------------------------


@pytest.mark.parametrize("channel", ["notion", "github_branch"])
@pytest.mark.parametrize("enabled", ["false", 1, None])
def test_publish_enabled_must_be_a_real_boolean(tmp_path, channel, enabled):
    data = json.loads((REPO_CONFIG / "recommendations.json").read_text(encoding="utf-8"))
    data["publish"][channel]["enabled"] = enabled
    (tmp_path / "recommendations.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=f"publish.{channel}.enabled"):
        load_settings(tmp_path)


def test_missing_publish_channel_fails_closed(tmp_path):
    data = json.loads((REPO_CONFIG / "recommendations.json").read_text(encoding="utf-8"))
    del data["publish"]["notion"]
    (tmp_path / "recommendations.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="publish.notion.enabled"):
        load_settings(tmp_path)


def _place(store, slot, ticker, order_id, created="2026-10-13T16:31:00Z"):
    path = store.placements_dir(DAY, slot) / f"{ticker}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data": {"order": {"id": order_id, "created_at": created}}}), encoding="utf-8")


def test_handed_record_may_be_replaced_before_any_placement(store):
    first, second = _pair("1230", _at(12, 20))[0], _pair("1230", _at(12, 25), (("MSFT", 6.0),))[0]
    store.record_handed(first)
    store.record_handed(second)
    assert store.handed(DAY, "1230") == second


def test_handed_record_cannot_be_swapped_once_placements_exist(store):
    first = _pair("1230", _at(12, 20))[0]
    store.record_handed(first)
    _place(store, "1230", "AAPL", "oid-1")
    with pytest.raises(ValueError, match="placements_exist_for_an_earlier_recommendation"):
        store.record_handed(_pair("1230", _at(12, 25), (("AAPL", 6.0),))[0])
    store.record_handed(first)  # the same set may be re-recorded
    assert store.handed(DAY, "1230") == first


def test_prior_ids_file_is_not_placement_evidence(store):
    first = _pair("1230", _at(12, 20))[0]
    store.record_handed(first)
    (store.placements_dir(DAY, "1230") / "prior_ids.json").write_text("[]", encoding="utf-8")
    store.record_handed(_pair("1230", _at(12, 25))[0])  # no <TICKER>.json yet: replacement allowed


def test_handed_is_none_when_nothing_was_handed(store):
    assert store.handed(DAY, "1230") is None


def test_logged_references_bind_latest_placement_to_its_decision_price(store):
    rec_1030, expected_1030 = _pair("1030", _at(10, 30))
    store.write(rec_1030, expected_1030)
    store.record_handed(rec_1030)
    _place(store, "1030", "AAPL", "oid-early", created="2026-10-13T14:31:00Z")
    rec_1230, expected_1230 = _pair("1230", _at(12, 25))
    store.write(rec_1230, expected_1230)
    store.record_handed(rec_1230)
    _place(store, "1230", "AAPL", "oid-late", created="2026-10-13T16:31:00Z")
    assert store.logged_references(DAY) == {"AAPL": ("oid-late", 100.0)}


def test_logged_references_skip_unusable_evidence(store):
    rec, expected = _pair("1230", _at(12, 25), (("AAPL", 3.0), ("MSFT", 2.0)))
    store.write(rec, expected)
    store.record_handed(rec)
    (store.placements_dir(DAY, "1230") / "AAPL.json").write_text("{broken", encoding="utf-8")
    _place(store, "1230", "MSFT", "")                      # no order id
    _place(store, "1230", "NVDA", "oid-nvda")              # not in the handed set
    assert store.logged_references(DAY) == {}


def test_logged_references_skip_a_slot_whose_expected_twin_is_missing(store):
    rec, _expected = _pair("1230", _at(12, 25))
    store.record_handed(rec)  # handed but never written: no expected twin on disk
    _place(store, "1230", "AAPL", "oid-1")
    assert store.logged_references(DAY) == {}
