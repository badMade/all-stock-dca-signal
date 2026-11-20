"""Tests for the SAFETY-GATED compute path (``compute --market-data``) reading the DAY's list.

Piece 4b of docs/adr-backup-planner-daily-universe.md. Written BEFORE the implementation (TDD). Piece 4 gave
``compute --quotes`` and ``universe`` a ``--published FILE`` option; the gated path is the one the live backup slots
actually take (``GATED_SLOTS``), and it still reads the bundled list. Same table as piece 4
(``test_backup_planner_day_universe.py``): no/unchanged set -> bundled list; set with a carried list -> that list, and
the computed plan carries it; changed hash without a list, or an unreadable payload -> FAILED, nothing planned.

What is specific to the gated path: the primary's own Gate 5 generators decide the buys, and they read the strategy
config the planner hands them (and, for the 15:30 engine and the stream CLI, reload one from the config directory).
The carried list must reach every one of those readers, or a name the program dropped could still be planned. These tests
therefore run every gated slot and assert on WHICH names become orders, not on the plan's header alone.

Dollar caps and sizing are the primary's, unchanged: a carried list changes which names are considered, never how much.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from market_data_fixtures import DAY, market_data
from test_backup_planner_gate import NOW as NOW_1530
from test_backup_planner_gate import _pages, _write
from test_backup_planner_streams import NOW as STREAM_NOW

import dca_config
import dca_order_plan as dop
from dca_rec_models import Recommendation, RecommendationSet
from dca_rec_publish import build_document
from scripts import backup_planner
from scripts.validate_native_placement import native_ref_id

GATED = ["0935", "1030", "1130", "1230", "1330", "1430", "1530"]
NOW = {**STREAM_NOW, "1530": NOW_1530}
AS_OF = "2026-10-07T09:00:00-04:00"

CARRIED = ("MSFT", "VT", "ZZZ")  # ZZZ is not in the bundled list; AAPL is, and is NOT carried
# prior close -> last price. Every name drops enough to be a buy, so only the list decides who is planned.
# The payload holds exactly the names its list names (the task fetches what ``universe`` prints), so the carried tape
# has ZZZ and no AAPL, and the bundled tape has AAPL and no ZZZ: a name outside the governing list is not in the payload.
BUNDLED_TAPE = {"VT": (100.0, 97.0), "MSFT": (100.0, 98.5), "AAPL": (100.0, 98.0)}
CARRIED_TAPE = {"VT": (100.0, 97.0), "MSFT": (100.0, 98.5), "ZZZ": (100.0, 97.5)}
# A payload that ALSO holds a dropped name: it must never be planned (refusing the payload is acceptable).
STRAY_TAPE = {**CARRIED_TAPE, "AAPL": (100.0, 98.0)}


@pytest.fixture(scope="module")
def config():
    return dca_config.load_strategy_config(dca_config.get_config_dir())


def a_set(slot, universe, universe_hash=None):
    return RecommendationSet(
        date.fromisoformat(DAY), datetime.fromisoformat(AS_OF), slot, "robinhood_agent_fetch",
        dop.current_ladder_version(), universe_hash or dop.universe_hash(universe or ()),
        (Recommendation("MSFT", 4.0, native_ref_id(slot, DAY, "MSFT")),),
        sizing_guard={"max_multiplier": 1.0, "deployed_7d_usd": 0.0}, universe=universe,
    )


def published(tmp_path, slot, recommendation_set=None):
    slots = {} if recommendation_set is None else {slot: recommendation_set}
    return _write(tmp_path, "published.json", build_document(slots, generated_at=datetime.fromisoformat(AS_OF)))


def gated(tmp_path, capsys, slot, *extra, tape=None):
    argv = ["compute", "--trading-day", DAY, "--slot", slot,
            "--market-data", _write(tmp_path, "market.json", market_data(BUNDLED_TAPE if tape is None else tape, slot=slot)),
            "--orders-pages", _write(tmp_path, "pages.json", _pages()), "--now", NOW[slot], "--buying-power", "1000",
            "--deployed-last-7d-usd", "0", *extra]
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


def symbols(report):
    return {o["symbol"] for o in report["plan"]["orders"]}


def codes(report):
    return [e["code"] for e in report["errors"]]


# --- the control: without a published set nothing changes ----------------------------------------------------------


@pytest.mark.parametrize("slot", GATED)
def test_without_published_the_bundled_list_governs_and_a_bundled_name_is_planned(tmp_path, capsys, slot, config):
    code, report = gated(tmp_path, capsys, slot)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert "AAPL" in symbols(report)  # the premise of the tests below: AAPL is plannable when the list allows it
    assert "universe" not in report["plan"]
    assert report["plan"]["universe_hash"] == dop.universe_hash(config.tickers)


# --- a carried list reaches every reader ----------------------------------------------------------------------------


@pytest.mark.parametrize("slot", GATED)
def test_a_carried_list_plans_only_names_on_it(tmp_path, capsys, slot):
    code, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, a_set(slot, CARRIED)),
                         tape=CARRIED_TAPE)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert symbols(report) <= set(CARRIED)
    assert {"VT", "MSFT"} <= symbols(report)


@pytest.mark.parametrize("slot", GATED)
def test_a_bundled_name_the_day_dropped_is_never_planned_even_if_the_payload_holds_it(tmp_path, capsys, slot):
    code, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, a_set(slot, CARRIED)),
                         tape=STRAY_TAPE)
    if code == 0:
        assert "AAPL" not in symbols(report)
    else:  # refusing the whole payload is the other safe outcome
        assert (report["status"], report["plan"]) == ("FAILED", None)


@pytest.mark.parametrize("slot", GATED)
def test_a_carried_list_can_plan_a_name_the_bundled_list_lacks(tmp_path, capsys, slot, config):
    assert "ZZZ" not in config.tickers
    _, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, a_set(slot, CARRIED)),
                      tape=CARRIED_TAPE)
    assert "ZZZ" in symbols(report)


@pytest.mark.parametrize("slot", GATED)
def test_the_computed_plan_carries_the_list_and_its_hash(tmp_path, capsys, slot):
    _, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, a_set(slot, CARRIED)),
                      tape=CARRIED_TAPE)
    assert report["plan"]["universe"] == list(CARRIED)
    assert report["plan"]["universe_hash"] == dop.universe_hash(CARRIED)


@pytest.mark.parametrize("slot", ["1130", "1530"])
def test_the_computed_plan_passes_the_validator_finalize_uses(tmp_path, capsys, slot, config):
    _, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, a_set(slot, CARRIED)),
                      tape=CARRIED_TAPE)
    result = dop.validate_plan(
        report["plan"], trading_day=date.fromisoformat(DAY), slot=slot, ladder_version=dop.current_ladder_version(),
        active_tickers=tuple(config.tickers), excluded_tickers=frozenset(config.excluded_tickers),
    )
    assert result.errors == ()


@pytest.mark.parametrize("slot", ["1130", "1530"])
def test_the_dollar_cap_is_unchanged_with_a_carried_list(tmp_path, capsys, slot):
    _, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, a_set(slot, CARRIED)),
                      tape=CARRIED_TAPE)
    assert sum(o["notional_usd"] for o in report["plan"]["orders"]) <= 180.0  # the Normal-tier daily cap


# --- the other rows of the table ------------------------------------------------------------------------------------


@pytest.mark.parametrize("slot", ["1130", "1530"])
def test_a_set_whose_hash_equals_the_bundled_hash_leaves_the_bundled_list_in_force(tmp_path, capsys, slot, config):
    unchanged = a_set(slot, None, universe_hash=dop.universe_hash(config.tickers))
    code, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, unchanged))
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert "AAPL" in symbols(report) and "universe" not in report["plan"]


@pytest.mark.parametrize("slot", ["1130", "1530"])
def test_a_payload_with_no_set_for_the_slot_leaves_the_bundled_list_in_force(tmp_path, capsys, slot):
    code, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot))
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert "AAPL" in symbols(report)


@pytest.mark.parametrize("slot", ["1130", "1530"])
def test_a_changed_hash_with_no_carried_list_plans_nothing(tmp_path, capsys, slot):
    changed = a_set(slot, None, universe_hash=dop.universe_hash(CARRIED))
    code, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, changed))
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "universe_unknown_for_day" in codes(report)


@pytest.mark.parametrize("slot", ["1130", "1530"])
def test_an_unreadable_published_file_fails_closed(tmp_path, capsys, slot):
    code, report = gated(tmp_path, capsys, slot, "--published", _write(tmp_path, "published.json", "{oops"))
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)


@pytest.mark.parametrize("slot", ["1130", "1530"])
def test_a_carried_list_holding_a_bundled_excluded_name_is_refused(tmp_path, capsys, slot, config):
    names = tuple(sorted({*CARRIED, sorted(config.excluded_tickers)[0]}))
    code, report = gated(tmp_path, capsys, slot, "--published", published(tmp_path, slot, a_set(slot, names)))
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "excluded_in_universe" in codes(report)
