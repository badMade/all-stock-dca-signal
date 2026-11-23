"""Tests for the compute fallback and the ``universe`` command reading the DAY's list.

Piece 4 of docs/adr-backup-planner-daily-universe.md (accepted 2026-11-20; open question 2 answered "yes" by the
operator 2026-11-20). Written BEFORE the implementation (TDD).

Both commands used to read only the bundled ``config/tickers.json``. Once the list changes daily, that list is stale
and the fallback would buy names the program has dropped. They now take an optional ``--published FILE`` (the same
published recommendations payload ``plan --source published_rec`` reads) and resolve the day's list from the slot's set:

| published input                                          | day's list                    | plan carries ``universe`` |
| -------------------------------------------------------- | ----------------------------- | ------------------------- |
| no ``--published`` argument                              | bundled list (unchanged)      | no                        |
| payload with no set for the slot                         | bundled list (unchanged)      | no                        |
| set whose ``universe_hash`` equals the bundled hash      | bundled list                  | no                        |
| set that carries a list                                  | the carried list (<= 100)     | yes (and its hash)        |
| set with a different hash and NO list                    | none: FAILED                  | n/a                       |
| unreadable or malformed payload                          | none: FAILED (fail closed)    | n/a                       |

Open question 2 ("yes"): a plan built from the bundled list is accepted ONLY on a day when the set's hash shows no list
change. Whenever the list is the carried one, the computed plan carries it, so ``finalize`` (which already validates a
carried list, pieces 3 and 3d) accepts it. Dollar caps are unchanged. The carried list gets the same bounds as piece 3
(at most 100, canonical, no bundled-excluded name).
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from test_backup_planner import _common, _envelope, _run, _write

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

CARRIED = ("AAPL", "MSFT", "VT", "ZZZ")  # differs from the bundled list and holds a name it lacks


@pytest.fixture(scope="module")
def config():
    return dca_config.load_strategy_config(dca_config.get_config_dir())


def a_set(universe, universe_hash=None, items=(("MSFT", 4.0),)):
    return RecommendationSet(
        date.fromisoformat(DAY), datetime.fromisoformat(AS_OF), SLOT, "robinhood_agent_fetch",
        dop.current_ladder_version(), universe_hash or dop.universe_hash(universe or ()),
        tuple(Recommendation(s, usd, native_ref_id(SLOT, DAY, s)) for s, usd in items),
        sizing_guard={"max_multiplier": 1.0, "deployed_7d_usd": 0.0}, universe=universe,
    )


def published(tmp_path, recommendation_set=None):
    """A published payload file; ``None`` writes one with no set for any slot."""
    slots = {} if recommendation_set is None else {SLOT: recommendation_set}
    document = build_document(slots, generated_at=datetime.fromisoformat(NOW))
    return _write(tmp_path, "published.json", document)


def universe_cmd(capsys, *extra):
    code = backup_planner.main(["universe", *extra])
    return code, json.loads(capsys.readouterr().out)


def compute_cmd(tmp_path, capsys, quotes, *extra):
    q = _write(tmp_path, "quotes.json", {"quotes": quotes})
    p = _write(tmp_path, "pages.json", _envelope())
    return _run(capsys, ["compute", *_common(["--quotes", q, "--orders-pages", p, "--now", NOW,
                                              "--buying-power", "1000", *extra])])


def codes(report):
    return [e["code"] for e in report["errors"]]


def quote(symbol, drop_pct=5.0, prior_close=1000.0):
    return {"symbol": symbol, "prior_close": prior_close, "prior_low": None,
            "decision_price": round(prior_close * (1 - drop_pct / 100.0), 6)}


# --- the universe command -----------------------------------------------------------------------------------------


def test_universe_with_a_carried_list_prints_that_list_and_its_hash(tmp_path, capsys):
    path = published(tmp_path, a_set(CARRIED))
    code, report = universe_cmd(capsys, "--published", path, "--slot", SLOT)
    assert (code, report["status"]) == (0, "OK")
    assert report["symbols"] == sorted(CARRIED)
    assert report["count"] == len(CARRIED)
    assert report["universe_hash"] == dop.universe_hash(CARRIED)


def test_universe_refuses_a_carried_list_that_holds_a_bundled_excluded_name(tmp_path, capsys, config):
    excluded = sorted(config.excluded_tickers)[0]
    names = tuple(sorted({*CARRIED, excluded}))
    code, report = universe_cmd(capsys, "--published", published(tmp_path, a_set(names)), "--slot", SLOT)
    # An excluded name in the carried list is a refusal (the piece 3 bound), not a quiet filter.
    assert code == 2 and "excluded_in_universe" in codes(report)


def test_universe_without_published_is_unchanged(capsys, config):
    code, report = universe_cmd(capsys)
    assert (code, report["status"]) == (0, "OK")
    assert report["universe_hash"] == dop.universe_hash(config.tickers)


def test_universe_with_no_set_for_the_slot_uses_the_bundled_list(tmp_path, capsys, config):
    code, report = universe_cmd(capsys, "--published", published(tmp_path), "--slot", SLOT)
    assert (code, report["status"]) == (0, "OK")
    assert report["universe_hash"] == dop.universe_hash(config.tickers)


def test_universe_with_a_set_whose_hash_equals_the_bundled_hash_uses_the_bundled_list(tmp_path, capsys, config):
    unchanged = a_set(None, universe_hash=dop.universe_hash(config.tickers))
    code, report = universe_cmd(capsys, "--published", published(tmp_path, unchanged), "--slot", SLOT)
    assert (code, report["status"]) == (0, "OK")
    assert report["universe_hash"] == dop.universe_hash(config.tickers)


def test_universe_refuses_a_changed_hash_with_no_carried_list(tmp_path, capsys):
    changed = a_set(None, universe_hash=dop.universe_hash(CARRIED))
    code, report = universe_cmd(capsys, "--published", published(tmp_path, changed), "--slot", SLOT)
    assert (code, report["status"]) == (2, "FAILED")
    assert "universe_unknown_for_day" in codes(report)


@pytest.mark.parametrize("text", ["", "{oops", "[]", "null", '{"schema_version": 2, "slots": {}}'])
def test_universe_with_an_unreadable_published_file_fails_closed(tmp_path, capsys, text):
    path = _write(tmp_path, "published.json", text)
    code, report = universe_cmd(capsys, "--published", path, "--slot", SLOT)
    assert (code, report["status"]) == (2, "FAILED")


def test_universe_with_a_missing_published_file_fails_closed(tmp_path, capsys):
    code, report = universe_cmd(capsys, "--published", str(tmp_path / "no.json"), "--slot", SLOT)
    assert (code, report["status"]) == (2, "FAILED")


def test_universe_published_requires_a_slot(tmp_path):
    with pytest.raises(SystemExit):
        backup_planner.main(["universe", "--published", published(tmp_path, a_set(CARRIED))])


# --- the compute fallback (quotes path) -----------------------------------------------------------------------------


def test_compute_with_a_carried_list_plans_a_name_only_that_list_has_and_carries_the_list(tmp_path, capsys):
    path = published(tmp_path, a_set(CARRIED))
    code, report = compute_cmd(tmp_path, capsys, [quote("ZZZ")], "--published", path)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert [o["symbol"] for o in report["plan"]["orders"]] == ["ZZZ"]
    assert report["plan"]["universe"] == list(CARRIED)
    assert report["plan"]["universe_hash"] == dop.universe_hash(CARRIED)


def test_compute_with_a_carried_list_skips_a_bundled_name_the_list_dropped(tmp_path, capsys, config):
    dropped = next(t for t in sorted(config.tickers) if t not in CARRIED and t not in config.excluded_tickers)
    path = published(tmp_path, a_set(CARRIED))
    code, report = compute_cmd(tmp_path, capsys, [quote(dropped), quote("VT")], "--published", path)
    assert code == 0
    assert [o["symbol"] for o in report["plan"]["orders"]] == ["VT"]
    assert dropped in {row["symbol"] for row in report["skipped"]}


def test_compute_with_a_carried_list_keeps_every_dollar_cap(tmp_path, capsys):
    quotes = [quote(s, drop_pct=8.0) for s in CARRIED]
    code, report = compute_cmd(tmp_path, capsys, quotes, "--published", published(tmp_path, a_set(CARRIED)))
    assert code == 0
    assert sum(o["notional_usd"] for o in report["plan"]["orders"]) <= 180.0  # the Normal-tier daily cap


def test_compute_whose_carried_list_the_validator_would_refuse_is_refused_here_too(tmp_path, capsys, config):
    excluded = sorted(config.excluded_tickers)[0]
    names = tuple(sorted({*CARRIED, excluded}))
    code, report = compute_cmd(tmp_path, capsys, [quote("VT")], "--published", published(tmp_path, a_set(names)))
    assert (code, report["status"]) == (2, "FAILED")
    assert "excluded_in_universe" in codes(report)


def test_compute_without_published_is_unchanged(tmp_path, capsys, config):
    code, report = compute_cmd(tmp_path, capsys, [quote("VT")])
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert "universe" not in report["plan"]
    assert report["plan"]["universe_hash"] == dop.universe_hash(config.tickers)


def test_compute_on_a_day_with_no_list_change_uses_the_bundled_list_and_carries_nothing(tmp_path, capsys, config):
    unchanged = a_set(None, universe_hash=dop.universe_hash(config.tickers))
    code, report = compute_cmd(tmp_path, capsys, [quote("VT")], "--published", published(tmp_path, unchanged))
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert "universe" not in report["plan"]


def test_compute_refuses_a_changed_hash_with_no_carried_list(tmp_path, capsys):
    changed = a_set(None, universe_hash=dop.universe_hash(CARRIED))
    code, report = compute_cmd(tmp_path, capsys, [quote("VT")], "--published", published(tmp_path, changed))
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "universe_unknown_for_day" in codes(report)


def test_compute_with_an_unreadable_published_file_fails_closed(tmp_path, capsys):
    path = _write(tmp_path, "published.json", "{oops")
    code, report = compute_cmd(tmp_path, capsys, [quote("VT")], "--published", path)
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)


# --- the computed plan survives finalize ---------------------------------------------------------------------------


def test_a_computed_plan_that_carries_a_list_passes_the_validator_finalize_uses(tmp_path, capsys, config):
    code, report = compute_cmd(tmp_path, capsys, [quote("ZZZ")], "--published", published(tmp_path, a_set(CARRIED)))
    assert code == 0
    result = dop.validate_plan(
        report["plan"], trading_day=date.fromisoformat(DAY), slot=SLOT, ladder_version=dop.current_ladder_version(),
        active_tickers=tuple(config.tickers), excluded_tickers=frozenset(config.excluded_tickers),
    )
    assert result.errors == ()
