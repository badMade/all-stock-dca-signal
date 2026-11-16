"""Tests written FIRST for the goals layer (docs/adr-goals-and-loss-hedge-shadow.md).

Every test here is skipped until ``dca_goals.py``, ``schemas/goals.schema.json`` and the console
Goals page land. They pin the behaviour the operator approved, not an implementation:

* Test 1 (unit half): a goal proposal from the console is validated before anything is written.
* Test 4: the PR the console opens changes only the edited goal and the two version numbers.
* Test 5: hedge ``mode`` can only be ``off`` or ``shadow``; a broken hedge goal resolves to off.
* Test 7: hedge execution is locked in code and no goal document can unlock it.
* Test 9: the crypto hedge instrument has the same lock plus its own bounds.

The goal documents below are CONFIGURATION inputs written for these tests, not market data.
The numbers in them (a 20% target, a 25% drawdown limit, a 5% crypto cap) are the operator's
stated goals and the ADR's proposed bounds, so nothing here is an invented market fixture.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dca_goals

REPO = Path(__file__).resolve().parents[1]
SCHEMA = REPO / "schemas" / "goals.schema.json"

# A fixed "now" so effective_from comparisons never depend on the wall clock.
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
FUTURE = "2026-12-01T00:00:00-05:00"
PAST = "2026-11-01T00:00:00-05:00"


def growth_entry(**overrides):
    entry = {"goal_id": "growth-twr-5y", "goal_version": 1, "kind": "aspirational", "metric": "twr_cagr",
             "horizon_years": 5, "target": 0.20, "benchmark": "SP500_TR", "effective_from": PAST}
    entry.update(overrides)
    return entry


def put_instrument(**overrides):
    item = {"instrument": "protective_put", "mode": "shadow", "underlying": "QQQ", "horizon_days": 45}
    item.update(overrides)
    return item


def crypto_instrument(**overrides):
    item = {"instrument": "crypto_allocation", "mode": "shadow", "assets": ["BTC"], "max_alloc_pct": 0.05,
            "hold_days": 10}
    item.update(overrides)
    return item


def hedge_entry(**overrides):
    entry = {"goal_id": "loss-hedge", "goal_version": 1, "kind": "loss_hedge", "mode": "shadow",
             "max_drawdown": 0.25, "hedge_budget_pct": 0.02,
             "instruments": [put_instrument(), crypto_instrument()], "effective_from": PAST}
    entry.update(overrides)
    return entry


def document(*goals):
    return {"$schema": "../schemas/goals.schema.json", "version": 1, "status": "active",
            "goals": list(goals) if goals else [growth_entry(), hedge_entry()]}


def write_goals(config_dir: Path, doc) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "goals.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return path


def proposal(**overrides):
    """A console proposal: one goal entry WITHOUT goal_version (the server assigns it)."""
    entry = growth_entry(target=0.15, effective_from=FUTURE)
    del entry["goal_version"]
    entry.update(overrides)
    return {"goal": entry}


def schema_validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())
    return jsonschema, schema


# --------------------------------------------------------------------------------------------
# Test 1 (unit half): proposal validation happens before any write.
# --------------------------------------------------------------------------------------------

def test_1_a_valid_proposal_is_accepted_and_returned_without_a_version():
    entry = dca_goals.validate_goal_proposal(proposal(), now=NOW)
    assert entry["goal_id"] == "growth-twr-5y"
    assert entry["target"] == pytest.approx(0.15)
    # The server, not the browser, assigns goal_version (build_goals_document bumps it).
    assert "goal_version" not in entry


@pytest.mark.parametrize("payload, reason", [
    ({"goal": proposal()["goal"], "extra": 1}, "unknown top-level key"),
    ({"goals": proposal()["goal"]}, "wrong top-level key"),
    ([], "not an object"),
    (proposal(surprise="x"), "unknown goal field"),
    (proposal(goal_version=7), "goal_version is server-assigned"),
    (proposal(target=0.0), "target at the exclusive lower bound"),
    (proposal(target=-0.1), "negative target"),
    (proposal(target=0.51), "target above the 0.50 ceiling"),
    (proposal(target=float("nan")), "NaN target"),
    (proposal(target=float("inf")), "infinite target"),
    (proposal(target=10**1000), "oversized integer target"),
    (proposal(target=True), "boolean is not a number"),
    (proposal(target="0.2"), "string is not a number"),
    (proposal(horizon_years=0), "horizon below 1 year"),
    (proposal(horizon_years=31), "horizon above 30 years"),
    (proposal(effective_from="2026-12-01T00:00:00"), "naive effective_from"),
    (proposal(effective_from=PAST), "effective_from in the past"),
    (proposal(effective_from="next tuesday"), "unparseable effective_from"),
    (proposal(metric="account_value_growth"), "raw account growth is not an allowed metric"),
    (proposal(goal_id=""), "empty goal_id"),
    (proposal(goal_id="../config/budgets"), "goal_id that could escape the proposal folder"),
])
def test_1_invalid_proposals_are_rejected(payload, reason):
    with pytest.raises(ValueError):
        dca_goals.validate_goal_proposal(payload, now=NOW)


def test_1_a_hedge_proposal_cannot_ask_for_active_mode():
    entry = hedge_entry(effective_from=FUTURE, mode="active")
    del entry["goal_version"]
    with pytest.raises(ValueError):
        dca_goals.validate_goal_proposal({"goal": entry}, now=NOW)


def test_1_validation_requires_an_aware_now():
    with pytest.raises(ValueError):
        dca_goals.validate_goal_proposal(proposal(), now=NOW.replace(tzinfo=None))


# --------------------------------------------------------------------------------------------
# Test 4: the document the PR commits differs only in the edited goal and the version bumps.
# --------------------------------------------------------------------------------------------

def test_4_editing_a_goal_changes_only_that_goal_and_the_versions():
    current = document()
    entry = dca_goals.validate_goal_proposal(proposal(), now=NOW)
    result = dca_goals.build_goals_document(current, entry)

    assert result["version"] == current["version"] + 1
    assert {k: v for k, v in result.items() if k not in {"version", "goals"}} == \
        {k: v for k, v in current.items() if k not in {"version", "goals"}}

    by_id = {g["goal_id"]: g for g in result["goals"]}
    assert set(by_id) == {"growth-twr-5y", "loss-hedge"}
    assert by_id["loss-hedge"] == current["goals"][1], "an unedited goal must be byte-for-byte identical"
    edited = by_id["growth-twr-5y"]
    assert edited["goal_version"] == 2
    assert edited["target"] == pytest.approx(0.15)
    assert edited["effective_from"] == FUTURE
    # Order of goals is preserved so the PR diff stays minimal.
    assert [g["goal_id"] for g in result["goals"]] == [g["goal_id"] for g in current["goals"]]


def test_4_a_new_goal_is_appended_at_version_1():
    current = document(growth_entry())
    entry = hedge_entry(effective_from=FUTURE)
    del entry["goal_version"]
    entry = dca_goals.validate_goal_proposal({"goal": entry}, now=NOW)
    result = dca_goals.build_goals_document(current, entry)
    assert [g["goal_id"] for g in result["goals"]] == ["growth-twr-5y", "loss-hedge"]
    assert result["goals"][1]["goal_version"] == 1
    assert result["goals"][0] == current["goals"][0]


def test_4_build_does_not_mutate_its_inputs():
    current = document()
    snapshot = copy.deepcopy(current)
    entry = dca_goals.validate_goal_proposal(proposal(), now=NOW)
    entry_snapshot = copy.deepcopy(entry)
    dca_goals.build_goals_document(current, entry)
    assert current == snapshot
    assert entry == entry_snapshot


def test_4_the_built_document_validates_against_the_schema():
    jsonschema, schema = schema_validator()
    entry = dca_goals.validate_goal_proposal(proposal(), now=NOW)
    jsonschema.validate(dca_goals.build_goals_document(document(), entry), schema)


# --------------------------------------------------------------------------------------------
# Test 5: hedge mode is off or shadow only; a broken hedge goal resolves to off.
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["active", "ACTIVE", "on", "live", "", None, True, 1])
def test_5_schema_rejects_any_hedge_mode_but_off_and_shadow(mode):
    jsonschema, schema = schema_validator()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document(growth_entry(), hedge_entry(mode=mode)), schema)


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_5_schema_accepts_off_and_shadow(mode):
    jsonschema, schema = schema_validator()
    jsonschema.validate(document(growth_entry(), hedge_entry(mode=mode)), schema)


def test_5_the_schema_enum_is_exactly_off_and_shadow():
    # Guard against "active" being added to the enum later and hidden behind a default.
    schema = json.loads(SCHEMA.read_text())
    modes = set()

    def walk(node):
        if isinstance(node, dict):
            if "mode" in node.get("properties", {}):
                modes.update(node["properties"]["mode"].get("enum", []))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert modes == {"off", "shadow"}


@pytest.mark.parametrize("broken", [
    hedge_entry(mode="active"),
    hedge_entry(mode=None),
    hedge_entry(max_drawdown=1.5),
    hedge_entry(max_drawdown=float("nan")),
    hedge_entry(max_drawdown=10**1000),
    hedge_entry(hedge_budget_pct=0.06),
    hedge_entry(surprise_field=1),
    hedge_entry(effective_from="2026-11-01T00:00:00"),
])
def test_5_a_broken_hedge_goal_resolves_to_off_and_leaves_growth_alone(tmp_path, broken):
    write_goals(tmp_path, document(growth_entry(), broken))
    goals = dca_goals.load_goals(tmp_path, now=NOW)
    assert goals.hedge.mode == "off"
    assert goals.growth is not None and goals.growth.target == pytest.approx(0.20)


@pytest.mark.parametrize("raw", [None, "", "{", "[]", '{"version": 1, "version": 2}', '"goals"'])
def test_5_a_missing_or_malformed_file_is_neutral_and_never_raises(tmp_path, raw):
    if raw is not None:
        write_goals(tmp_path, raw)
    goals = dca_goals.load_goals(tmp_path, now=NOW)
    assert goals == dca_goals.NEUTRAL_GOALS
    assert goals.hedge.mode == "off"
    assert goals.growth is None


def test_5_an_inactive_file_is_neutral(tmp_path):
    doc = document()
    doc["status"] = "inactive"
    write_goals(tmp_path, doc)
    assert dca_goals.load_goals(tmp_path, now=NOW) == dca_goals.NEUTRAL_GOALS


def test_5_oversized_int_target_neutralizes_growth_goal_fail_closed(tmp_path):
    entry = growth_entry(target=10**1000)
    write_goals(tmp_path, document(entry, hedge_entry()))
    goals = dca_goals.load_goals(tmp_path, now=NOW)
    assert goals.growth is None
    assert goals.hedge.mode == "shadow"


def test_5_a_goal_not_yet_in_effect_is_neutral_until_its_date(tmp_path):
    write_goals(tmp_path, document(growth_entry(effective_from=FUTURE), hedge_entry(effective_from=FUTURE)))
    before = dca_goals.load_goals(tmp_path, now=NOW)
    assert before.growth is None and before.hedge.mode == "off"
    after = dca_goals.load_goals(tmp_path, now=datetime(2026, 12, 1, 6, tzinfo=timezone.utc))
    assert after.growth is not None and after.hedge.mode == "shadow"


def test_5_the_loader_requires_an_aware_now(tmp_path):
    write_goals(tmp_path, document())
    with pytest.raises(ValueError):
        dca_goals.load_goals(tmp_path, now=NOW.replace(tzinfo=None))


# --------------------------------------------------------------------------------------------
# Test 7: execution is locked in code. No goal document, env var or config key can unlock it.
# --------------------------------------------------------------------------------------------

LOCK = "HEDGE_EXECUTION_ENABLED"


def test_7_the_lock_is_the_literal_false():
    assert dca_goals.HEDGE_EXECUTION_ENABLED is False
    tree = ast.parse((REPO / "dca_goals.py").read_text())
    assignments = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.Assign, ast.AnnAssign))
                   and any(isinstance(t, ast.Name) and t.id == LOCK
                           for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
    assert len(assignments) == 1, "exactly one assignment, at module level"
    value = assignments[0].value
    assert isinstance(value, ast.Constant) and value.value is False, "the lock must be a literal, not computed"


def test_7_nothing_else_in_the_repo_assigns_or_reads_the_lock_from_outside():
    # Only dca_goals.py may name the lock outside tests. A config key, an env var or another module
    # that sets it would be a second switch nobody reviewed.
    offenders = []
    for path in REPO.rglob("*.py"):
        parts = set(path.relative_to(REPO).parts)
        if parts & {".venv", "tests", ".git", "node_modules"} or path.name == "dca_goals.py":
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if LOCK in text:
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
    goals_source = (REPO / "dca_goals.py").read_text()
    assert "environ" not in goals_source and "getenv" not in goals_source
    assert LOCK not in SCHEMA.read_text()
    assert LOCK.lower() not in SCHEMA.read_text().lower()


def _random_hedge_entry(rng: random.Random) -> dict:
    modes = ["off", "shadow", "active", "ACTIVE", "on", "live", True, 1, None, ""]
    numbers = [0.0, 0.02, 0.05, 0.06, 0.25, 0.9, 1.5, -1.0, float("nan"), float("inf"), True, "0.1"]
    instruments = []
    for _ in range(rng.randint(0, 3)):
        pick = rng.choice([put_instrument, crypto_instrument])
        item = pick(mode=rng.choice(modes))
        if rng.random() < 0.3:
            item[rng.choice(["execute", "live", "enabled", LOCK])] = True
        instruments.append(item)
    entry = hedge_entry(mode=rng.choice(modes), max_drawdown=rng.choice(numbers),
                        hedge_budget_pct=rng.choice(numbers), instruments=instruments,
                        effective_from=rng.choice([PAST, FUTURE, "2026-11-01T00:00:00", "bad"]))
    if rng.random() < 0.3:
        entry[rng.choice(["execute", "live", "enabled", LOCK, "hedge_execution_enabled"])] = True
    return entry


def test_7_no_goal_document_can_unlock_execution(tmp_path):
    # Seeded, deterministic property test (stdlib random; no new test dependency).
    rng = random.Random(20_261_113)
    for i in range(2000):
        doc = document(growth_entry(), _random_hedge_entry(rng))
        if rng.random() < 0.2:
            doc[rng.choice([LOCK, "execute", "status"])] = rng.choice([True, "active", 1])
        config_dir = tmp_path / f"case{i}"
        write_goals(config_dir, json.dumps(doc, allow_nan=True))
        goals = dca_goals.load_goals(config_dir, now=NOW)
        assert dca_goals.hedge_execution_allowed(goals) is False, f"case {i} unlocked execution: {doc}"
        assert goals.hedge.mode in {"off", "shadow"}
        assert all(item.mode in {"off", "shadow"} for item in goals.hedge.instruments)


def test_7_even_a_hand_built_goal_set_cannot_unlock_execution():
    assert dca_goals.hedge_execution_allowed(dca_goals.NEUTRAL_GOALS) is False


# --------------------------------------------------------------------------------------------
# Test 9: the crypto instrument is shadow-only and bounded; a broken one resolves to off.
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("broken, reason", [
    (crypto_instrument(mode="active"), "active crypto mode"),
    (crypto_instrument(max_alloc_pct=0.06), "allocation above the 5% cap"),
    (crypto_instrument(max_alloc_pct=0.0), "zero allocation"),
    (crypto_instrument(max_alloc_pct=float("nan")), "NaN allocation"),
    (crypto_instrument(assets=["DOGE"]), "asset outside BTC and ETH"),
    (crypto_instrument(assets=[]), "no assets"),
    (crypto_instrument(assets=["BTC", "BTC"]), "duplicate asset"),
    (crypto_instrument(hold_days=0), "hold below 1 day"),
    (crypto_instrument(hold_days=61), "hold above 60 days"),
    (crypto_instrument(leverage=2), "unknown field"),
])
def test_9_schema_rejects_a_bad_crypto_instrument(broken, reason):
    jsonschema, schema = schema_validator()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document(growth_entry(), hedge_entry(instruments=[put_instrument(), broken])), schema)


@pytest.mark.parametrize("broken", [
    crypto_instrument(mode="active"),
    crypto_instrument(max_alloc_pct=0.06),
    crypto_instrument(assets=["DOGE"]),
    crypto_instrument(leverage=2),
])
def test_9_a_broken_crypto_instrument_resolves_to_off_without_dropping_the_put(tmp_path, broken):
    write_goals(tmp_path, document(growth_entry(), hedge_entry(instruments=[put_instrument(), broken])))
    goals = dca_goals.load_goals(tmp_path, now=NOW)
    shadowed = [item.instrument for item in goals.hedge.instruments if item.mode == "shadow"]
    assert shadowed == ["protective_put"]
    assert dca_goals.hedge_execution_allowed(goals) is False


def test_9_a_valid_crypto_instrument_loads_in_shadow():
    # Sanity check that the bounds above are not so tight that the operator's own goal fails.
    jsonschema, schema = schema_validator()
    jsonschema.validate(document(growth_entry(), hedge_entry(
        instruments=[crypto_instrument(assets=["BTC", "ETH"], max_alloc_pct=0.05, hold_days=60)])), schema)
    assert math.isclose(crypto_instrument()["max_alloc_pct"], 0.05)


def test_proposal_and_document_helpers_are_self_consistent():
    # Guard the guards: the fixtures above must express the ADR's example, so a failing test
    # means the implementation disagrees with the ADR, not that a helper drifted.
    doc = document()
    assert doc["goals"][0]["target"] == pytest.approx(0.20)
    assert doc["goals"][1]["instruments"][1]["instrument"] == "crypto_allocation"
    assert datetime.fromisoformat(FUTURE) > NOW > datetime.fromisoformat(PAST)
    assert datetime.fromisoformat(FUTURE) - NOW < timedelta(days=30)
