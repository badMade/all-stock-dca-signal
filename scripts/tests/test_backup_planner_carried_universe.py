"""Tests for the backup planner accepting a recommendation set that CARRIES the day's universe.

Piece 3 of docs/adr-backup-planner-daily-universe.md (accepted 2026-11-20). Written BEFORE the implementation (TDD).

The planner bundles ``config/tickers.json``, which is stale almost every day once the buy list changes daily. A
recommendation set may now carry the day's sorted list (``RecommendationSet.universe``). When a set does, the
plan-first plan carries it too, and the planner validates against the CARRIED list instead of the bundled one:

* the plan's ``universe_hash`` must be the hash of the carried list (the bundled hash is no longer compared);
* every order symbol must be in the carried list, which may hold names the bundled list lacks;
* the planner keeps bounds of its own because it can no longer rely on a bundled list: at most 100 symbols, canonical,
  sorted and unique, and NO symbol in the bundled ``excluded`` list (that list stays hand-held);
* every dollar cap is unchanged: a carried list changes WHICH names may be bought, never how much.

A set that carries no list is validated exactly as before, against the bundled hash. Unchanged here: the compute
fallback and the ``universe`` command still use the bundled list (docs/adr-backup-planner-daily-universe.md, open
question 2), so this piece is additive and touches no plan that carries no list.

Expected values come from the ADR and the hash rule (``dca_order_plan.universe_hash``), not from the planner's output.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

import dca_config
import dca_order_plan as dop
from dca_rec_models import Recommendation, RecommendationSet
from dca_rec_publish import build_document
from scripts import backup_planner
from scripts.validate_native_placement import native_ref_id

DAY = "2026-10-07"
SLOT = "1130"
NOW = "2026-10-07T11:49:30-04:00"
AS_OF = "2026-10-07T11:45:00-04:00"


@pytest.fixture(scope="module")
def config():
    return dca_config.load_strategy_config(dca_config.get_config_dir())


def carried_set(items, universe, *, universe_hash=None):
    """A set built the way dca_recommender builds one, carrying ``universe`` (native ref_ids, current ladder)."""
    return RecommendationSet(
        date.fromisoformat(DAY), datetime.fromisoformat(AS_OF), SLOT, "robinhood_agent_fetch",
        dop.current_ladder_version(), universe_hash or dop.universe_hash(universe),
        tuple(Recommendation(symbol, usd, native_ref_id(SLOT, DAY, symbol)) for symbol, usd in items),
        sizing_guard={"max_multiplier": 1.0, "deployed_7d_usd": 0.0}, universe=universe,
    )


def run_plan(tmp_path, capsys, recommendation_set):
    document = build_document({SLOT: recommendation_set}, generated_at=datetime.fromisoformat(NOW))
    path = tmp_path / "published.json"
    path.write_text(json.dumps(document))
    code = backup_planner.main(["plan", "--source", "published_rec", "--published", str(path), "--now", NOW,
                                "--trading-day", DAY, "--slot", SLOT])
    return code, json.loads(capsys.readouterr().out)


def errors(report):
    return [e["code"] for e in report["errors"]]


def validate(plan, config):
    return backup_planner._validate_plan_first(
        plan, config=config, ladder_version=dop.current_ladder_version(),
        trading_day=date.fromisoformat(DAY), slot=SLOT,
    )


def plan_of(recommendation_set):
    return backup_planner._plan_first_document(recommendation_set, "published_rec")


# A short list that differs from the bundled one, and holds a name the bundled list does not.
CARRIED = ("AAPL", "MSFT", "VT", "ZZZ")


# --- the happy path ---------------------------------------------------------------------------------------------


def test_a_set_carrying_a_universe_that_differs_from_the_bundled_one_becomes_a_valid_plan(tmp_path, capsys, config):
    assert dop.universe_hash(CARRIED) != dop.universe_hash(config.tickers)  # the premise of the whole piece
    code, report = run_plan(tmp_path, capsys, carried_set([("MSFT", 4.0)], CARRIED))
    assert (code, report["status"], report["errors"]) == (0, "VALID_PLAN", [])
    assert report["plan"]["universe"] == list(CARRIED)
    assert report["plan"]["universe_hash"] == dop.universe_hash(CARRIED)


def test_a_name_in_the_carried_universe_but_not_in_the_bundled_list_can_be_ordered(tmp_path, capsys, config):
    assert "ZZZ" not in config.tickers
    code, report = run_plan(tmp_path, capsys, carried_set([("ZZZ", 3.0)], CARRIED))
    assert (code, report["status"]) == (0, "VALID_PLAN")


def test_a_carried_universe_equal_to_the_bundled_list_is_also_valid(tmp_path, capsys, config):
    bundled = tuple(sorted(set(config.tickers)))
    if len(bundled) > 100:
        bundled = bundled[:100]  # the model bound; the live list is cut to 100 before sets carry it
    code, report = run_plan(tmp_path, capsys, carried_set([("MSFT", 4.0)], bundled))
    assert (code, report["status"]) == (0, "VALID_PLAN")


def test_a_set_without_a_universe_still_yields_the_old_plan_shape(tmp_path, capsys):
    plain = RecommendationSet(
        date.fromisoformat(DAY), datetime.fromisoformat(AS_OF), SLOT, "robinhood_agent_fetch",
        dop.current_ladder_version(), dop.universe_hash(dca_config.load_strategy_config(
            dca_config.get_config_dir()).tickers),
        (Recommendation("MSFT", 4.0, native_ref_id(SLOT, DAY, "MSFT")),),
        sizing_guard={"max_multiplier": 1.0, "deployed_7d_usd": 0.0},
    )
    code, report = run_plan(tmp_path, capsys, plain)
    assert (code, report["status"]) == (0, "VALID_PLAN") and "universe" not in report["plan"]


# --- what the planner still refuses ---------------------------------------------------------------------------------


def test_a_symbol_in_the_bundled_excluded_list_is_refused_even_if_the_carried_list_holds_it(tmp_path, capsys, config):
    assert "QBUT" in config.excluded_tickers
    code, report = run_plan(tmp_path, capsys, carried_set([("MSFT", 4.0)], ("MSFT", "QBUT")))
    assert (code, report["status"], report["plan"]) == (2, "NO_VALID_PLAN", None)
    assert "excluded_in_universe" in errors(report)


def test_dollar_caps_are_unchanged_by_a_carried_list(tmp_path, capsys):
    code, report = run_plan(tmp_path, capsys, carried_set([("MSFT", 999.0)], CARRIED))
    assert (code, report["status"]) == (2, "NO_VALID_PLAN") and "notional_over_cap" in errors(report)


def test_a_stale_ladder_is_still_refused_with_a_carried_list(tmp_path, capsys):
    stale = carried_set([("MSFT", 4.0)], CARRIED)
    stale = RecommendationSet(stale.trading_day, stale.as_of, stale.slot, stale.source, "old-ladder",
                              stale.universe_hash, stale.items, sizing_guard=stale.sizing_guard, universe=CARRIED)
    code, report = run_plan(tmp_path, capsys, stale)
    assert (code, report["status"]) == (2, "NO_VALID_PLAN") and "stale_ladder_version" in errors(report)


def test_a_published_copy_whose_universe_omits_an_ordered_symbol_is_refused(tmp_path, capsys):
    document = build_document({SLOT: carried_set([("MSFT", 4.0)], CARRIED)}, generated_at=datetime.fromisoformat(NOW))
    document["slots"][SLOT]["universe"] = ["AAPL", "VT", "ZZZ"]  # MSFT removed, hash left as it was
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(document))
    code = backup_planner.main(["plan", "--source", "published_rec", "--published", str(path), "--now", NOW,
                                "--trading-day", DAY, "--slot", SLOT])
    report = json.loads(capsys.readouterr().out)
    assert (code, report["status"], report["plan"]) == (2, "NO_VALID_PLAN", None)


# --- the validator itself, with plans the model would never let a set produce --------------------------------------------


def valid_plan():
    return plan_of(carried_set([("MSFT", 4.0)], CARRIED))


def test_the_validator_accepts_a_well_formed_carried_plan(config):
    assert validate(valid_plan(), config).errors == ()


def test_a_universe_that_does_not_hash_to_the_plans_hash_is_refused(config):
    plan = {**valid_plan(), "universe_hash": dop.universe_hash(("AAPL", "MSFT"))}
    assert "universe_hash_mismatch" in [e.code for e in validate(plan, config).errors]


@pytest.mark.parametrize("universe", [
    pytest.param(["MSFT", "AAPL"], id="unsorted"),
    pytest.param(["AAPL", "AAPL", "MSFT"], id="duplicate"),
    pytest.param(["aapl", "MSFT"], id="lowercase"),
    pytest.param(["AAPL", "MSFT", 7], id="not_a_string"),
    pytest.param("AAPL,MSFT", id="not_a_list"),
    pytest.param([], id="empty"),
])
def test_a_malformed_carried_universe_is_a_schema_violation(config, universe):
    plan = {**valid_plan(), "universe": universe}
    assert "schema_violation" in [e.code for e in validate(plan, config).errors]


def test_more_than_100_symbols_is_refused_and_exactly_100_is_allowed(config):
    names = [f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(101)]
    over = {**valid_plan(), "universe": names, "universe_hash": dop.universe_hash(names), "orders": []}
    assert "universe_too_large" in [e.code for e in validate(over, config).errors]
    at = {**over, "universe": names[:100], "universe_hash": dop.universe_hash(names[:100])}
    assert validate(at, config).errors == ()


def test_an_order_outside_the_carried_universe_is_an_unknown_symbol(config):
    plan = valid_plan()
    plan["orders"] = [{"symbol": "AMZN", "notional_usd": 4.0, "ref_id": native_ref_id(SLOT, DAY, "AMZN")}]
    assert "unknown_symbol" in [e.code for e in validate(plan, config).errors]


def test_a_plan_with_no_universe_key_is_still_checked_against_the_bundled_hash(config):
    plan = {k: v for k, v in valid_plan().items() if k != "universe"}  # hash is the carried list's, not the bundled one
    assert "universe_hash_mismatch" in [e.code for e in validate(plan, config).errors]


def test_the_local_readers_config_check_accepts_a_carried_set_and_refuses_an_excluded_one():
    good = backup_planner.current_config_reason(carried_set([("MSFT", 4.0)], CARRIED), trading_day=date.fromisoformat(DAY),
                                                slot=SLOT)
    bad = backup_planner.current_config_reason(carried_set([("MSFT", 4.0)], ("MSFT", "QBUT")),
                                               trading_day=date.fromisoformat(DAY), slot=SLOT)
    assert (good, bad) == (None, "excluded_in_universe")

