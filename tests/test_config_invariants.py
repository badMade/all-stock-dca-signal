"""Invariants between the Python constants and the version-controlled config.

Several money-and-identity facts are stated in two places at once, by design:

  * ``near_close_dca.BUDGET_TIERS`` is the ladder the runtime actually applies,
    while ``config/budgets.json`` -> ``budget_tiers`` documents the same policy
    for operators and for ``scripts/backup_strategy.py``. ``dca_config`` reads
    the JSON block for a handful of scalars but deliberately does NOT apply
    ``budget_tiers``, so nothing forced the two to agree.
  * ``near_close_dca.ACTIVE_TICKERS`` / ``EXCLUDED_TICKERS`` are the hardcoded
    ``StrategyConfig`` defaults; ``config/tickers.json`` is overlaid on top of
    them by ``dca_config.load_tickers``. Paths that build a bare
    ``StrategyConfig()`` (e.g. ``dca_risk.build_risk_report``) never see the
    JSON, so a drift would change behavior on some paths and not others.

Both pairs happened to match when this file was written. Nothing enforced it.
These tests are that enforcement: the caps and the universe that decide how much
real money is deployed cannot silently diverge from the config that documents
them.

The account-number test is the standing guard for the secrets policy in
AGENTS.md: the live account number is supplied by
``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` and referenced in config as
``"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"``, and must never be re-inlined.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import FIXTURE_ACCOUNT_NUMBER
from near_close_dca import (
    ACTIVE_TICKERS,
    BUDGET_TIERS,
    EASTERN,
    EXCLUDED_TICKERS,
    TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS,
    TIER_SPEND_THRESHOLDS_USD,
    WHOLE_SHARE_TICKERS,
    StrategyConfig,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
SCHEMAS_DIR = REPO_ROOT / "schemas"
SCHEDULE_MANIFEST = REPO_ROOT / "docs" / "schedule_manifest.md"


def _load(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_bytes())


# --- config/*.json is well-formed --------------------------------------------


def test_every_config_file_parses_as_a_json_object():
    files = sorted(CONFIG_DIR.glob("*.json"))
    assert files, "config/ must not be empty"
    for path in files:
        data = json.loads(path.read_bytes())
        assert isinstance(data, dict), f"{path.name} must contain a JSON object"


def test_aug_31_intraday_override_is_gated_to_the_1130_fired_slot():
    """The shared intraday task must not turn its afternoon fires into authorized buys."""
    override = _load("scheduled_tasks.json")["temporary_live_scheduler_overrides"][
        "authorize_2026_08_31_morning_perplexity_buy_slots_today_only"
    ]

    assert override["authorized_task_slots_et"]["7be82b97"] == ["11:30"]
    required_text = " ".join(override["task_text_must_include"])
    assert "actual fired slot is exactly 11:30 ET" in required_text
    assert "13:30 or 14:30 ET, end silently" in required_text


def test_aug_28_one_time_catch_up_task_is_registered_in_the_manifest():
    """codex review discussion_r3894271690 (P2): the Aug 28 override authorizes
    task 2c1d3792 as an active one-time buy task via
    ``one_time_catch_up_task_ids``, but a task ID that appears only in override
    prose -- with no entry in the ``tasks`` array -- has no status, fire
    window, or broker/safety contract that other consumers (reporting,
    reconciliation, run classification) can discover or validate. Every ID an
    override names as an authorized *buy* task must have a corresponding
    ``tasks`` entry so it is discoverable the same way canonical tasks are.
    """
    data = _load("scheduled_tasks.json")
    override = data["temporary_live_scheduler_overrides"]["authorize_2026_08_28_all_perplexity_buy_slots_today_only"]
    task_ids = {task["id"] for task in data["tasks"]}

    for one_time_id in override["one_time_catch_up_task_ids"]:
        assert one_time_id in task_ids, (
            f"{one_time_id} is authorized as a one-time catch-up buy task but has no tasks[] entry"
        )


# --- Dated overrides must not outlive their own authorization window ----------
#
# The two August 2026 authorizations each granted a non-Claude executor the
# right to place real buy orders for exactly one trading day, and each declared
# its own expiry (`expires_after_trading_day`). Nothing connected that expiry to
# how docs/schedule_manifest.md presented it, so both stayed written in the
# present tense, in tables shaped exactly like the canonical schedule table and
# positioned ABOVE it, for days after they lapsed. A reader scanning the
# "Active schedule" section top-down met three conflicting schedule tables
# before the authoritative one. Nothing in the repo was wrong -- the JSON knew
# the windows had closed and 2c1d3792 was already disabled -- but the document
# an operator or agent actually reads still said "authorized".
#
# These pin the reconciliation. They are deliberately clock-dependent: a
# self-expiring override that has passed its trading day MUST be marked expired
# in the manifest, so the day after any future override lapses, the suite says
# so. A failure here is not a bug to work around; it is the reminder to add the
# override's `(EXPIRED)` heading and status line, which is a two-line edit.


def _expired_authorization_overrides(today: date) -> dict[str, dict]:
    """Overrides whose own recorded authorization window has already closed.

    Only the self-expiring shape is date-checkable: an override carrying
    ``authorization_window_et.expires_after_trading_day`` has stated in its own
    record that it lapses at the end of ``trading_day``. An override without
    that block (the Aug 17 2026 pre-validation pause) ends by operator action
    instead, which no clock can decide, so its status stays prose in the
    manifest rather than an assertion here.
    """
    expired: dict[str, dict] = {}
    overrides = _load("scheduled_tasks.json")["temporary_live_scheduler_overrides"]
    for key, override in overrides.items():
        window = override.get("authorization_window_et")
        if not isinstance(window, dict) or not window.get("expires_after_trading_day"):
            continue
        if datetime.strptime(window["trading_day"], "%Y-%m-%d").date() < today:
            expired[key] = override
    return expired


def _manifest_block_citing(key: str) -> tuple[str, str]:
    """The one ``####`` heading and body in the manifest that cites ``key``."""
    blocks = SCHEDULE_MANIFEST.read_text(encoding="utf-8").split("\n#### ")
    matches = [block for block in blocks[1:] if key in block]
    assert len(matches) == 1, (
        f"expected exactly one '#### ' block in docs/schedule_manifest.md citing {key}, found {len(matches)}"
    )
    heading, _, body = matches[0].partition("\n")
    return heading, body


def test_an_expired_authorization_override_is_marked_expired_in_the_manifest():
    """A lapsed same-day trading authorization must read as lapsed."""
    for key, override in _expired_authorization_overrides(datetime.now(EASTERN).date()).items():
        trading_day = override["authorization_window_et"]["trading_day"]
        heading, body = _manifest_block_citing(key)
        assert heading.rstrip().endswith("(EXPIRED)"), (
            f"{key} lapsed after {trading_day}, but its docs/schedule_manifest.md heading "
            f"does not end with '(EXPIRED)': {heading!r}"
        )
        assert "expired" in body.lower(), f"{key} lapsed after {trading_day}, but its manifest block never says so"
        assert trading_day in body, f"{key}'s manifest block must name the trading day {trading_day} it expired after"


def test_a_one_time_buy_task_from_an_expired_override_is_disabled():
    """``2c1d3792`` exists in ``tasks`` only so an override-named buy task is
    discoverable (see the test above it). Discoverable also means its status has
    to stay honest: once the window that authorized it closed, the one thing
    that entry must never say is ``active``.
    """
    tasks = {task["id"]: task for task in _load("scheduled_tasks.json")["tasks"]}
    for key, override in _expired_authorization_overrides(datetime.now(EASTERN).date()).items():
        for one_time_id in override.get("one_time_catch_up_task_ids", []):
            task = tasks.get(one_time_id)
            assert task is not None, f"{one_time_id} (from {key}) has no tasks[] entry"
            assert task["status"] == "disabled", (
                f"{one_time_id} was authorized only for {key}'s expired window but is status={task['status']!r}"
            )


def test_the_canonical_schedule_table_precedes_the_expired_override_records():
    """Ordering is the whole point of the fix: the authoritative table has to be
    the first schedule table in the section that calls itself authoritative.
    """
    text = SCHEDULE_MANIFEST.read_text(encoding="utf-8")
    canonical = text.index("| ET | Task | ID | Cadence | Cron (UTC) | Trades | Status |")
    expired_records = text.index("### Expired temporary live scheduler overrides")
    assert canonical < expired_records, (
        "the canonical schedule table must appear before the expired override records, "
        "or a reader meets an expired authorization table first"
    )


# --- ...and the ordering fix has to survive the next merge -------------------
#
# It did not. The three checks above were satisfied while the manifest carried a
# SECOND, uncorrected copy of the Aug 28 and Aug 31 blocks sitting ABOVE the
# canonical table, plus the Sep 8-11 block, all three written in the present
# tense with no `(EXPIRED)` marker -- the exact shape the section's own preamble
# says was removed because "that is exactly how an expired authorization gets
# mistaken for a live one". Two gaps let it back in:
#
#   * `_manifest_block_citing` splits on "\n#### " and searches `blocks[1:]`, so
#     a duplicate introduced by a PARAGRAPH heading ("Temporary live scheduler
#     override, Aug 28, 2026:") lands in `blocks[0]` and is never counted. Its
#     "exactly one" assertion stayed green with two copies in the file.
#   * The ordering test above compares the canonical table against the `###
#     Expired ...` heading, not against the first schedule table in the section.
#     Three override tables preceded it and the assertion still passed.
#
# This was not cosmetic. The duplicates contradicted the reconciled copies on
# which task IDs an override authorized: the stale Aug 28 table listed
# `4412c3c4`, `e028361e`, `e3590042`, `0c3cde00`, `44b3975d` as "date-gated
# active", while the reconciled block names those same IDs as
# `disabled_parallel_task_ids` -- IDs that specifically must NOT run, so two
# executors cannot race for one slot. An operator reading top-down met the
# wrong answer first, in the document that governs live buy authorization.
#
# These two pin the structural property directly, so a future merge that
# reintroduces a block above the table fails here rather than passing three
# checks that were written to prevent exactly it.


def _active_schedule_preamble() -> str:
    """Everything in the Active schedule section before its canonical table."""
    text = SCHEDULE_MANIFEST.read_text(encoding="utf-8")
    section = text.index("## Active schedule (canonical, verified 2026-07-27)")
    canonical = text.index("| ET | Task | ID | Cadence | Cron (UTC) | Trades | Status |")
    assert section < canonical, "the canonical table must live inside the Active schedule section"
    return text[section:canonical]


def test_no_dated_override_is_documented_above_the_canonical_schedule_table():
    """A dated override cited before the authoritative table is read as live.

    Keyed off ``temporary_live_scheduler_overrides`` rather than off heading
    text, so an override added later is covered the day it lands: whatever the
    block calls itself, citing its JSON key above the canonical table fails.
    The standing backup policy is deliberately not caught -- it is not a dated
    override, it does not expire, and its own key lives under a different
    top-level name (``standing_perplexity_backup_policy``).
    """
    preamble = _active_schedule_preamble()
    for key in _load("scheduled_tasks.json")["temporary_live_scheduler_overrides"]:
        assert key not in preamble, (
            f"{key} is cited above the canonical schedule table. Every dated override "
            f"belongs under '### Expired temporary live scheduler overrides', after the "
            f"table, or a reader meets an expired authorization first"
        )
    assert "| ET | Live scheduler task | ID |" not in preamble, (
        "an override-scope table appears above the canonical schedule table; the "
        "canonical table must be the first schedule table in this section"
    )


def test_every_override_block_is_introduced_by_a_markdown_heading():
    """The duplicates used paragraph headings, which is how they hid.

    ``_manifest_block_citing`` can only see blocks introduced by ``#### ``. A
    block introduced by a bare paragraph line is invisible to it *and* to the
    ``(EXPIRED)``-suffix check that reads its heading, so it can say anything.
    Requiring the ``#### `` form keeps every override block inside the reach of
    the three tests above.
    """
    offenders = [
        line
        for line in SCHEDULE_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.startswith("Temporary live scheduler")
    ]
    assert not offenders, (
        "override blocks must be introduced by a '#### ' heading, not a paragraph, or "
        f"the expiry and uniqueness checks cannot see them: {offenders}"
    )


def test_every_config_file_validates_against_its_referenced_schema():
    """A schema requirement tightened for the active ladder silently broke a
    DIFFERENT config file that shares the same schema via its own ``$schema``
    pointer: ``schemas/sizing_ladder.schema.json`` unconditionally required
    ``base_amount_below_close_usd``, which ``config/backtest_sizing_ladder.json``
    (an analytics-only ladder, ``status: "backtest_analytics"``, never loaded
    by the live runner) does not define at all. Any editor, packaging check,
    or consumer that validates a config file against its own declared schema
    would reject it (Codex review: "Preserve validity of the backtest sizing
    ladder"). Every config file naming a local schemas/*.json via ``$schema``
    must actually validate against it, not just the one file a given change
    happened to have in mind.
    """
    jsonschema = pytest.importorskip("jsonschema")
    for path in sorted(CONFIG_DIR.glob("*.json")):
        data = json.loads(path.read_bytes())
        schema_ref = data.get("$schema")
        if not schema_ref or not schema_ref.endswith(".json") or "://" in schema_ref:
            continue  # no local schema reference to check
        schema_path = (path.parent / schema_ref).resolve()
        if SCHEMAS_DIR not in schema_path.parents:
            continue
        schema = json.loads(schema_path.read_bytes())
        jsonschema.validate(data, schema)


def test_sizing_ladder_schema_requires_status_before_making_the_reduced_base_conditional():
    """The conditional added to fix the prior finding had its own gap: the
    ``if`` block itself required ``status`` (``required: ["status"]``), so a
    document omitting BOTH ``status`` and ``base_amount_below_close_usd``
    made the ``if`` evaluate false (its own required-property check failed),
    skipping the ``then`` requirement entirely and validating successfully --
    even though ``dca_runner._validated_sizing_ladder()`` unconditionally
    reads ``ladder["base_amount_below_close_usd"]`` and would crash at import
    on exactly this schema-valid document (Codex review: "Require status
    before making the live base conditional"). ``status`` is now required at
    the top level, so this document fails validation outright instead of
    silently passing.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "sizing_ladder.schema.json").read_bytes())
    document = {
        "base_amount_usd": 2.0,
        "tiers": [{"min_drop_pct": 0.1, "max_drop_pct": None, "multiplier": 1.0}],
        # deliberately omits both "status" and "base_amount_below_close_usd"
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


# --- schema rejection tests ---------------------------------------------------
#
# test_every_config_file_validates_against_its_referenced_schema (above) only
# ever proves the shipped documents pass their own schema -- it says nothing
# about whether the schema would catch a REAL regression. A schema that
# accidentally dropped a `required` entry, loosened a `pattern`, or lost an
# `enum` would keep passing that test forever. Each of these pins one
# operationally meaningful invariant the schema is supposed to enforce, by
# mutating a copy of the real shipped config (so everything else stays
# realistic) and asserting the mutation is rejected.


def test_budgets_schema_rejects_a_stress_tier_that_does_not_require_a_rollover_reserve():
    """budget_tiers' if/then conditional requires requires_rollover_reserve to
    be literally true for the "stress" tier -- the only tier authorized to
    exceed Normal's weekly cap using the earned rollover reserve
    (dca_runner.execute_plan's tier.uses_rollover_reserve gate). A schema
    that silently stopped enforcing this would let a stress tier entry claim
    it does NOT use the reserve while dca_runner's own BUDGET_TIERS constant
    (matched against this same document by test_budget_tiers_match_config_budgets_json)
    still trusts it, decoupling the two the same way the sizing-ladder gap did."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "budgets.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("budgets.json")))  # deep copy
    stress = next(t for t in document["budget_tiers"] if t["name"] == "stress")
    stress["requires_rollover_reserve"] = False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_scheduled_tasks_schema_rejects_an_unrecognized_task_type():
    """`type` is constrained to the four documented task kinds
    (trading/monitoring/analytics/reminder); scripts/run_classification.py
    and the executor prompts both branch on this field, so an unrecognized
    value silently passing schema validation would only surface as a runtime
    KeyError/behavioral gap far from where the config was actually edited."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "scheduled_tasks.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("scheduled_tasks.json")))  # deep copy
    document["tasks"][0]["type"] = "bogus-task-type"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_inverse_martingale_schema_rejects_a_missing_basis():
    """Each per-ticker entry requires ``basis`` (and, via the if/then/else
    branch, the matching recovery-offset field for that basis) -- a document
    that drops it should be rejected outright rather than silently defaulting
    to one basis at load time."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "inverse_martingale.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("inverse_martingale.json")))  # deep copy
    document["tickers"]["AAPL"] = {
        "enabled": True,
        "tiers": [{"drop_pct": 1.0, "units": 1.0}],
        "recovery_offset_pct": 1.0,
        # deliberately omits "basis"
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_inverse_martingale_schema_rejects_pct_basis_missing_recovery_offset():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "inverse_martingale.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("inverse_martingale.json")))  # deep copy
    document["tickers"]["AAPL"] = {
        "enabled": True,
        "basis": "pct",
        "tiers": [{"drop_pct": 1.0, "units": 1.0}],
        # deliberately omits "recovery_offset_pct"
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_inverse_martingale_schema_rejects_a_pct_tier_missing_drop_pct():
    """Regression test for "Require the basis-specific tier threshold in the
    schema". Previously each tier item only required ``units``, so a
    ``basis: "pct"`` entry with no ``drop_pct`` on a tier passed schema
    validation even though load_inverse_martingale_config() immediately
    raises on it -- letting an operator or automated config check approve a
    document that would abort strategy startup."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "inverse_martingale.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("inverse_martingale.json")))  # deep copy
    document["tickers"]["AAPL"] = {
        "enabled": True,
        "basis": "pct",
        "tiers": [{"units": 1.0}],  # deliberately omits "drop_pct"
        "recovery_offset_pct": 1.0,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_inverse_martingale_schema_rejects_a_stddev_tier_missing_sigma_multiple():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "inverse_martingale.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("inverse_martingale.json")))  # deep copy
    document["tickers"]["AAPL"] = {
        "enabled": True,
        "basis": "stddev",
        "tiers": [{"units": 1.0}],  # deliberately omits "sigma_multiple"
        "recovery_offset_sigma": 0.5,
        "stddev_window_days": 20,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_inverse_martingale_schema_accepts_a_well_formed_pct_entry():
    """Sanity check that the new tier-level if/then/else doesn't over-reject
    a genuinely valid document."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "inverse_martingale.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("inverse_martingale.json")))  # deep copy
    document["tickers"]["AAPL"] = {
        "enabled": True,
        "basis": "pct",
        "tiers": [
            {"drop_pct": 1.0, "units": 1.0},
            {"drop_pct": 2.0, "units": 2.0},
        ],
        "recovery_offset_pct": 1.0,
    }
    jsonschema.validate(document, schema)  # must not raise


def test_inverse_martingale_schema_allows_a_ticker_entry_to_omit_enabled():
    """Regression test for "Allow ticker entries to inherit enabled_default".

    dca_config.load_inverse_martingale_config() explicitly supports a ticker
    entry that omits `enabled`, falling back to the top-level
    `enabled_default` -- the schema previously required `enabled` on every
    entry, rejecting exactly the configuration runtime code and tests treat
    as supported.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "inverse_martingale.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("inverse_martingale.json")))  # deep copy
    document["enabled_default"] = True
    document["tickers"]["AAPL"] = {
        # deliberately omits "enabled" -- must fall back to enabled_default
        "basis": "pct",
        "tiers": [{"drop_pct": 1.0, "units": 1.0}],
        "recovery_offset_pct": 1.0,
    }
    jsonschema.validate(document, schema)  # must not raise


def test_ml_sizing_adjustment_schema_rejects_an_out_of_bounds_adjustment():
    """``adjustments`` values are constrained to [0.5, 1.5] at the schema
    level too -- a corrupted publish should be rejected by structure alone,
    not rely solely on dca_config.load_ml_sizing_adjustment's own runtime
    bounds check."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "ml_sizing_adjustment.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("ml_sizing_adjustment.json")))  # deep copy
    document["adjustments"]["AAPL"] = 3.0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_ml_sizing_adjustment_schema_rejects_missing_bounds():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "ml_sizing_adjustment.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("ml_sizing_adjustment.json")))  # deep copy
    del document["bounds"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_ml_sizing_adjustment_schema_rejects_active_status_with_null_generated_at():
    """Regression test for "Require a timestamp for active ML adjustments".

    An active document with a missing/null generated_at previously passed
    schema validation while dca_config.load_ml_sizing_adjustment() silently
    treats it as a no-op -- config validation would approve a document the
    runtime actually disables, letting a malformed "active" publish look
    schema-valid.
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "ml_sizing_adjustment.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("ml_sizing_adjustment.json")))  # deep copy
    document["status"] = "active"
    document["generated_at"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_ml_sizing_adjustment_schema_still_allows_null_generated_at_when_inactive():
    """The inactive template shape (status "inactive", generated_at null) must
    remain valid -- the new constraint only applies when active. Built as its
    own synthetic document (mirroring the active-status test right below)
    rather than asserting on config/ml_sizing_adjustment.json's live content,
    whose status reflects whatever scripts/publish_octopus_sizing.py last
    published and is not always "inactive"."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "ml_sizing_adjustment.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("ml_sizing_adjustment.json")))  # deep copy
    document["status"] = "inactive"
    document["generated_at"] = None
    jsonschema.validate(document, schema)  # must not raise


def test_ml_sizing_adjustment_schema_accepts_active_status_with_a_real_generated_at():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "ml_sizing_adjustment.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("ml_sizing_adjustment.json")))  # deep copy
    document["status"] = "active"
    document["generated_at"] = "2026-08-12T00:00:00+00:00"
    jsonschema.validate(document, schema)  # must not raise


def test_tickers_schema_rejects_a_malformed_ticker_symbol():
    """`active` entries are constrained to ^[A-Z.]{1,6}$ -- uppercase letters
    and dots only, at most 6 characters. A lowercase or otherwise malformed
    entry here would still be accepted by the runtime's plain `in` checks
    but silently never match a real broker symbol, and
    test_active_tickers_have_no_duplicates/test_active_tickers_match_config_tickers_json
    only check membership and duplication, not shape."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "tickers.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("tickers.json")))  # deep copy
    document["active"].append("not-a-real-ticker")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


@pytest.mark.parametrize("placeholder", ["TODO", "TBD", "N/A", "null", ""])
def test_tickers_schema_rejects_a_placeholder_data_source_note(placeholder):
    """`data_source_notes` values are documented as "non-empty, non-placeholder
    text" and enforced by a `not: {enum: [...]}` blocklist introduced with the
    GFUZ addition (#75, a review finding). PR #1321 ("Clean up TODO markers")
    dropped "TODO" from that blocklist -- a repo-wide TODO sweep that mistook
    a validation rule for a stale marker -- which LOOSENED the schema: a
    literal "TODO" note would then validate, exactly the placeholder the rule
    exists to reject. The change was reverted; this test pins every entry in
    the blocklist so a future sweep cannot quietly weaken it again. The
    positive control first proves the same document validates with real
    text, so the rejection below is attributable to the value alone."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "tickers.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("tickers.json")))  # deep copy
    first_ticker = document["active"][0]
    document["data_source_notes"][first_ticker] = "Resolved on Robinhood; finance connector unverified."
    jsonschema.validate(document, schema)  # positive control: must not raise
    document["data_source_notes"][first_ticker] = placeholder
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_trading_window_schema_rejects_a_malformed_decision_time():
    """decision_time_et is constrained to HH:MM (00-29 hour digit family
    aside, the real constraint the pattern enforces is two-digit,
    zero-padded hour and a valid minute); a single-digit-hour spelling like
    "9:35" -- exactly the kind of manual-edit slip a config like this
    invites -- must be rejected rather than silently pass through to
    whatever parses it downstream."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "trading_window.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("trading_window.json")))  # deep copy
    document["decision_time_et"] = "9:35"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_trading_window_schema_rejects_a_document_missing_start_date():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "trading_window.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("trading_window.json")))  # deep copy
    del document["start_date"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_notifications_schema_rejects_a_non_boolean_channel_enabled_flag():
    """Every channel's `enabled` flag gates whether dca_alerts actually sends
    to it; a string like "yes" is truthy in Python but is not the documented
    boolean contract, and a schema that accepted it would let a manual edit
    silently change what "enabled" means for that one channel."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "notifications.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("notifications.json")))  # deep copy
    first_channel = next(iter(document["channels"]))
    document["channels"][first_channel]["enabled"] = "yes"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_email_channel_declares_operator_address_via_env_placeholder():
    """Every executor prompt (claude/dca-executor-prompts/*.txt and
    live-mirror/*.txt) tells the agent to send alerts "to the operator email
    configured for alerts (do not hardcode a personal address)" -- but
    nothing in config/notifications.json ever named that address, so a
    compliant executor has nowhere to send to and falls back to a draft
    (see docs/alert_digest.md's EMAIL_DRAFT_NOT_DELIVERED case). The address
    must be the expandable placeholder, resolved the same way
    dca_config.expand_config_env resolves ${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER},
    never a literal address committed to git."""
    document = _load("notifications.json")
    assert document["channels"]["email"]["address"] == "${DCA_ALERT_EMAIL_ADDRESS}", (
        "config/notifications.json's channels.email must declare 'address': "
        '"${DCA_ALERT_EMAIL_ADDRESS}" so a live executor has a destination to send to'
    )


def test_notifications_schema_rejects_a_hardcoded_email_address():
    """The schema must enforce the same "never hardcode a personal address"
    rule the executor prompts state in prose -- a manual edit that pastes a
    literal address into channels.email.address should fail validation, not
    silently reach an executor as config it can send to verbatim."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "notifications.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("notifications.json")))  # deep copy
    document["channels"]["email"]["address"] = "geoffplymale@gmail.com"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_reporting_schema_rejects_a_non_object_notion_block():
    """`notion` is declared as an object (the notion sync's config surface);
    a scalar left over from a botched manual edit must be rejected rather
    than silently reach dca_notion_sync expecting a mapping."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "reporting.schema.json").read_bytes())
    document = json.loads(json.dumps(_load("reporting.json")))  # deep copy
    document["notion"] = "not-an-object"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def _run_state_document(budget):
    return {
        "run_id": "test-run-2026-07-28",
        "generated_at": "2026-07-28T09:35:00-04:00",
        "trading_day": "2026-07-28",
        "broker": "robinhood",
        "counts": {},
        "budget": budget,
    }


def test_run_state_schema_allows_an_unevaluated_budget_for_pre_budget_exits():
    """The seven-slot roster's END_SILENTLY (weekend/holiday, exits at Gate
    0), GATE5_DISABLED (exits at Gate 5, before Gate 6), GATE4_EXPECTED_OVERLAP,
    and an ABORT at Gate 1/2/3/4 all exit before Gate 6 ever selects a tier,
    but schemas/run_state.schema.json unconditionally required budget_tier/
    daily_cap_usd/weekly_cap_usd -- forcing the executor to either fabricate
    a tier that was never selected or write a schema-invalid artifact (Codex
    review: "Define a schema-valid budget for pre-budget exits").
    budget.budget_evaluated=false with no tier/caps at all must now validate,
    while a legacy-style artifact with a full, real budget (and no
    budget_evaluated marker at all) must keep validating exactly as before,
    and one missing budget_tier with no marker must still fail -- the
    conditional must not weaken validation for the currently-live
    three-stream engine's own artifacts."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS_DIR / "run_state.schema.json").read_bytes())

    pre_budget_exit = _run_state_document({"budget_evaluated": False})
    jsonschema.validate(pre_budget_exit, schema)

    legacy_full_budget = _run_state_document({"budget_tier": "normal", "daily_cap_usd": 180.0, "weekly_cap_usd": 900.0})
    jsonschema.validate(legacy_full_budget, schema)

    legacy_incomplete_budget = _run_state_document({"daily_cap_usd": 180.0, "weekly_cap_usd": 900.0})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(legacy_incomplete_budget, schema)


# --- Two documented dev-install paths install the same tooling ---------------


def test_pyproject_dev_extra_and_requirements_dev_txt_name_the_same_packages():
    """pyproject.toml's `dev` extra backs `pip install -e '.[dev]'`;
    requirements-dev.txt backs `pip install -r requirements-dev.txt` and pins
    exact versions for CI. Both are documented as installing the same tooling
    (see the `dev` extra's own comment), but nothing enforced that -- PyYAML
    was pinned in requirements-dev.txt for scripts/workflow_policy.py's
    `import yaml` (itself imported by a pytest test file, so it's needed just
    to collect the suite) but missing from the `dev` extra, so the `-e '.[dev]'`
    path failed collection in an environment where jsonschema/referencing
    didn't happen to pull PyYAML in transitively (Codex review, PR #219:
    "Include PyYAML in the dev extra"). A package name present in only one of
    the two would silently reintroduce that split.
    """
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_extra = pyproject["project"]["optional-dependencies"]["dev"]
    # Package name only: strip the version specifier (">=1.2", "==1.2.3", ...).
    extra_names = {re.split(r"[<>=!~]", spec, maxsplit=1)[0].strip().lower() for spec in dev_extra}

    requirements_lines = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    requirements_names = {
        re.split(r"[<>=!~]", line, maxsplit=1)[0].strip().lower()
        for line in requirements_lines
        if line.strip() and not line.strip().startswith("#")
    }

    assert extra_names == requirements_names, (
        f"pyproject.toml [project.optional-dependencies.dev] and "
        f"requirements-dev.txt must name the same packages -- "
        f"only in pyproject: {extra_names - requirements_names or None}, "
        f"only in requirements-dev.txt: {requirements_names - extra_names or None}"
    )


def test_pyproject_ships_every_live_stream_module_and_config():
    """Codex review ("Ship all newly added stream modules and configs"): for
    a wheel or non-editable install, [tool.setuptools] py-modules/data-files
    are the only files shipped. market_open_dca.py, intraday_dca.py, and
    dca_cumulative.py were added without also adding them to py-modules, and
    config/market_open_dca.json/config/intraday_dca.json were added without
    adding them to data-files -- so the new executor/credit APIs and their
    checked-in stream configuration were unavailable outside a source
    checkout."""
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    py_modules = set(pyproject["tool"]["setuptools"]["py-modules"])
    config_data_files = set(pyproject["tool"]["setuptools"]["data-files"]["share/all-stock-dca/config"])

    for module in (
        # dca_alerts imports the shared outbound-network validation module at
        # import time, so omitting it prevents installed entry points such as
        # run-dca from starting (Codex review: "Ship the extracted network
        # module in the wheel").
        "dca_net_utils",
        "market_open_dca",
        "intraday_dca",
        "dca_cumulative",
        # dca_intraday_archive is imported unconditionally by both
        # morning_dca and near_close_dca (Codex review: "Package the
        # intraday archive module"); dca_ticker_paths is imported by the
        # ticker-history update/calibration scripts.
        "dca_intraday_archive",
        "dca_ticker_paths",
        # sizing_adjustment_reader is the live-DCA-side boundary module a
        # second sizing suggester imports (docs/repo_map.md); it has no
        # in-repo caller by design, so nothing else in the suite would notice
        # it missing from the wheel (2026-09-07 diagnostic P:PACKAGING-02).
        "sizing_adjustment_reader",
    ):
        assert module in py_modules, f"{module} must be listed in [tool.setuptools] py-modules"

    for config_file in ("config/market_open_dca.json", "config/intraday_dca.json"):
        assert config_file in config_data_files, f"{config_file} must be listed in [tool.setuptools.data-files]"


def test_per_ticker_calibration_wires_the_artifact_event_to_the_guarded_command():
    """Codex review ("Wire artifact events to event-calibrate"): the
    claude-per-ticker-calibration task's upstream_trigger fires the SAME
    command reused for the independent 6:30 PM ET scheduled run, which
    scripts.run_ticker_maintenance's ``calibrate`` subcommand always runs
    with force=True -- ignoring the event's own required_status/
    required_nonempty_field guard and republishing unconditionally on a
    partial or empty update event. A distinct ``artifact_event_command``
    field pointing at the guarded ``event-calibrate`` subcommand (force=False)
    closes that gap."""
    tasks = _load("scheduled_tasks.json")["tasks"]
    task = next(t for t in tasks if t["id"] == "claude-per-ticker-calibration")
    assert task["trigger_mode"] == "scheduled_and_artifact_event"
    assert task["command"].endswith("run_ticker_maintenance calibrate")
    assert task["artifact_event_command"].endswith("run_ticker_maintenance event-calibrate")


def test_no_live_task_routes_to_a_disabled_in_repo_executor():
    """Codex review ("Do not route live slots to disabled executors"): a
    task whose entry_point_status_* note says its in-repo executor SHIPS
    DISABLED (config's own live_execution_enabled=false) must keep
    no_python_entry_point=true. A scheduler that mechanically routes on
    that flag going false -- as the mere presence of an entry_point might
    otherwise suggest -- would silently replace real Robinhood placement
    with a no-op shadow run, submitting zero orders with no error surfaced.
    Flip the flag only together with enabling the executor's own config."""
    tasks = _load("scheduled_tasks.json")["tasks"]
    for task in tasks:
        ships_disabled = any(
            isinstance(value, str) and "SHIPS DISABLED" in value
            for key, value in task.items()
            if key.startswith("entry_point_status")
        )
        if ships_disabled:
            assert task.get("no_python_entry_point") is True, (
                f"{task.get('id')}: entry_point ships disabled but no_python_entry_point is not true"
            )


# --- Budget ladder -----------------------------------------------------------


def test_budget_tiers_match_config_budgets_json():
    """The runtime ladder and the documented ladder must be the same ladder.

    near_close_dca.BUDGET_TIERS carries the comment "must be kept in sync with
    config/budgets.json" -- this is what keeps that promise. A drift here means
    the daily/weekly notional ceilings the strategy enforces are not the ones
    the operator-facing config says it enforces.
    """
    documented = _load("budgets.json")["budget_tiers"]

    assert [t["name"] for t in documented] == [t.name for t in BUDGET_TIERS], (
        "budget tier names/order drifted between near_close_dca.BUDGET_TIERS and config/budgets.json"
    )
    for doc, code in zip(documented, BUDGET_TIERS, strict=True):
        assert doc["daily_cap_usd"] == code.daily_cap_usd, f"{code.name} daily cap drifted"
        assert doc["weekly_cap_usd"] == code.weekly_cap_usd, f"{code.name} weekly cap drifted"
        assert bool(doc.get("requires_rollover_reserve", False)) is code.uses_rollover_reserve, (
            f"{code.name} rollover-reserve flag drifted"
        )


def test_budget_tier_selection_thresholds_match_config_select_when():
    """The ladder's SELECTION boundaries must match the documented ones too.

    test_budget_tiers_match_config_budgets_json compares names, caps, and the
    rollover flag -- but not the threshold that decides WHICH tier applies. So
    TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS could be retuned, or a select_when
    boundary edited, and the runtime would pick a different tier from the one
    the operator-facing config documents while the test claiming to keep them
    synchronized stayed green (Codex review, PR #219).

    Tier selection is keyed SOLELY on the 3x+ signal count (see
    near_close_dca.select_budget_tier / config/budgets.json's selection_rule);
    select_when accordingly documents only the signal interval now ("<= 5",
    "6-10", "> 17"), and each tier's escalate_above_planned_spend_usd is a
    non-gating documentation/monitoring reference. This asserts on the
    numbers select_when contains rather than its wording: every signal
    threshold must appear as one of the integer boundaries, in order.

    The retained (non-gating) escalate_above_planned_spend_usd figures are
    still checked against TIER_SPEND_THRESHOLDS_USD below, purely so that
    documentation/monitoring reference does not silently drift from the
    historical thresholds it annotates -- not because either value gates
    tier selection.
    """
    documented = _load("budgets.json")["budget_tiers"]
    clauses = [str(tier.get("select_when", "")) for tier in documented]
    assert all(clauses), "every budget tier must document its select_when boundary"
    assert not any("planned spend" in clause.lower() for clause in clauses), (
        "select_when must not key on planned spend -- tier selection depends "
        "solely on the 3x-or-greater signal count "
        f"(found a 'planned spend' clause: {clauses!r})"
    )

    for i, tier in enumerate(documented[:-1]):
        spend_ref = tier.get("escalate_above_planned_spend_usd")
        assert spend_ref == TIER_SPEND_THRESHOLDS_USD[i], (
            f"{tier['name']!r}'s non-gating escalate_above_planned_spend_usd "
            f"reference {spend_ref!r} drifted from "
            f"near_close_dca.TIER_SPEND_THRESHOLDS_USD[{i}] "
            f"{TIER_SPEND_THRESHOLDS_USD[i]!r}"
        )
        assert "non-gating" in str(tier.get("escalate_above_planned_spend_usd_note", "")).lower(), (
            f"{tier['name']!r} must mark escalate_above_planned_spend_usd as "
            "non-gating via escalate_above_planned_spend_usd_note"
        )

    # Parse each tier's signal INTERVAL and compare it positionally. Set
    # membership is not enough: an earlier version of this test only asked
    # whether each runtime threshold appeared somewhere in the prose, so
    # changing the ladder from (5, 10, 17) to (6, 10, 17) still passed because
    # "6" occurs inside the documented "6-10" range -- while the runtime had
    # started escalating a signal later than the config claimed (Codex review,
    # PR #219).
    intervals = [_parse_signal_interval(clause) for clause in clauses]
    assert len(intervals) == len(TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS) + 1, (
        "the tier ladder and the threshold tuple describe a different number of "
        "tiers; select_by_signals uses N thresholds to select among N+1 tiers"
    )

    # The upper bound of every tier but the last IS that tier's threshold.
    documented_uppers = tuple(upper for _, upper in intervals[:-1])
    assert documented_uppers == tuple(TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS), (
        "signal thresholds drifted between "
        f"near_close_dca.TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS "
        f"{tuple(TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS)} and the select_when "
        f"upper bounds {documented_uppers} in config/budgets.json"
    )

    # ...and the intervals must be contiguous, so no signal count falls into a
    # gap or two tiers at once. This is what catches a range narrowed from
    # "6-10" to "7-10" while every threshold still appears somewhere.
    for index, ((lower, _), (_, prev_upper)) in enumerate(zip(intervals[1:], intervals[:-1], strict=True), start=1):
        assert prev_upper is not None
        assert lower == prev_upper + 1, (
            f"tier {documented[index]['name']!r} starts at {lower} but the "
            f"previous tier ends at {prev_upper}; select_when ranges must be "
            "contiguous or a signal count selects the wrong tier"
        )


def _parse_spend_boundary(clause: str) -> tuple[str, float]:
    """Return the (operator, dollar boundary) a select_when clause states.

    Every tier states exactly one: "planned spend <= $129" for the base tier and
    "planned spend > $129" for each tier above it. More or fewer than one dollar
    figure means the clause is not the shape this comparison assumes, and is
    raised rather than silently reduced to whichever value happened to be first.

    The OPERATOR is returned too. Comparing only the number let an operator flip
    from "<= $129" to "< $129", or from "> $129" to ">= $129", without the test
    noticing -- while near_close_dca._escalation_index uses strict ">" and so
    selects a different tier exactly at the boundary (Codex review, PR #219).
    """
    match = re.search(r"planned spend\s*(<=|>=|<|>)\s*\$([\d.]+)", clause)
    if not match:
        raise AssertionError(f"expected one 'planned spend <op> $N' boundary in select_when: {clause!r}")
    if len(re.findall(r"\$[\d.]+", clause)) != 1:
        raise AssertionError(f"expected exactly one dollar boundary in select_when clause: {clause!r}")
    return match.group(1), float(match.group(2))


def _parse_signal_interval(clause: str) -> tuple[int | None, int | None]:
    """Return the (lower, upper) 3x-signal bounds a select_when clause documents.

    Handles the three forms the ladder uses -- "<= 5", "6-10", "> 17" -- and
    returns None for an open end. Deliberately strict: an unrecognized clause
    raises rather than silently yielding a vacuous interval that would make the
    caller's assertions pass without checking anything.
    """
    signals = clause.split("signals", 1)[-1].strip()
    if match := re.fullmatch(r"<=\s*(\d+)", signals):
        return (None, int(match.group(1)))
    if match := re.fullmatch(r"(\d+)\s*-\s*(\d+)", signals):
        return (int(match.group(1)), int(match.group(2)))
    if match := re.fullmatch(r">\s*(\d+)", signals):
        return (int(match.group(1)) + 1, None)
    raise AssertionError(f"unrecognized select_when signal clause: {clause!r}")


def test_the_sibling_policy_prose_names_the_whole_share_carve_out():
    """The $10 figure appears in three OTHER free-text policy fields that
    config-driven agents and operators read as binding.

    Adding whole_share_max_per_order_usd without amending them left the file
    saying both "authorize this ~$23 RVII share" and "skip every order above
    $10", so an operator following the prose would keep blocking the purchase
    the setting exists to enable (Codex review, PR #786: "Reconcile the
    canonical hard limit with the whole-share allowance"). These are prose, so
    this pins only that each one still MENTIONS the carve-out."""
    budgets = _load("budgets.json")
    breakers = budgets["circuit_breakers"]
    prose = {
        "circuit_breakers.max_per_order_note": breakers["max_per_order_note"],
        "hard_limits": budgets["hard_limits"],
        "circuit_breakers.skip_conditions": " ".join(breakers["skip_conditions"]),
    }
    for field, text in prose.items():
        assert "whole_share" in text, f"{field} states the $10 ceiling without naming the whole-share allowance"


def test_whole_share_max_per_order_matches_the_strategy_config_default():
    """config/budgets.json's circuit_breakers.whole_share_max_per_order_usd is
    the whole-share path's own per-order ceiling. Some code paths build a bare
    StrategyConfig() and never see the JSON (see this module's docstring), so
    the dataclass default and the config value must agree or a whole-share fire
    is authorized at one size on one path and a different size on another."""
    documented = _load("budgets.json")["circuit_breakers"]["whole_share_max_per_order_usd"]
    assert documented == StrategyConfig().whole_share_max_per_order_usd
    # Never BELOW max_per_order_usd: that would silently tighten the ordinary
    # ceiling on this path instead of being the allowance it is meant to be.
    assert documented >= _load("budgets.json")["circuit_breakers"]["max_per_order_usd"]
    # It must actually clear the whole-share universe's real share prices, or
    # the category is a do-not-buy list in disguise -- the RVII defect this
    # knob was added for (PR #786 review).
    assert documented >= 25.0


def test_max_consecutive_placement_failures_matches_the_runner_constant():
    """config/budgets.json's circuit_breakers.max_consecutive_placement_failures
    is THE knob for how many placements may fail back to back before a live
    batch stops attempting the remainder; dca_runner reads it into
    MAX_CONSECUTIVE_PLACEMENT_FAILURES at import time. Same parity requirement
    as every other config-sourced live limit."""
    import dca_runner

    documented = _load("budgets.json")["circuit_breakers"]["max_consecutive_placement_failures"]
    assert documented == dca_runner.MAX_CONSECUTIVE_PLACEMENT_FAILURES
    # ONE knob, FIVE live loaders -- dca_runner (at import), the afternoon
    # candidate batch via dca_config.load_strategy_config, and
    # load_morning_config / load_stream_config / load_midday_config for the
    # morning, market-open, intraday and midday executors. They must not
    # disagree about when a live batch stops (Codex review, PR #786: "Enforce
    # the streak breaker in every live executor").
    import morning_dca
    from dca_config import load_strategy_config

    assert load_strategy_config(CONFIG_DIR).max_consecutive_placement_failures == documented
    assert morning_dca.load_max_consecutive_placement_failures(CONFIG_DIR) == documented
    # Required by the schema too: dca_runner indexes it at import time with no
    # fallback, so a "schema-valid" budgets.json without it would KeyError.
    circuit_breakers_schema = json.loads((SCHEMAS_DIR / "budgets.schema.json").read_bytes())["properties"][
        "circuit_breakers"
    ]
    assert "max_consecutive_placement_failures" in circuit_breakers_schema["required"]
    # >= 2 or the breaker collapses back to halt-on-first-rejection, the exact
    # behavior it was introduced to replace (2026-08-24).
    assert isinstance(documented, int) and not isinstance(documented, bool)
    assert documented >= 2


def test_every_loader_refuses_a_budgets_file_that_omits_the_breaker(tmp_path):
    """Value parity is not enough: each loader must also REFUSE an existing
    `budgets.json` that omits the key, or a `$DCA_CONFIG_DIR` override silently
    runs that live path with no breaker at all.

    `dca_runner` has always refused it (it indexes the key at import with no
    fallback) and the schema requires it, so a loader that returns None instead
    gives one live path weaker safety than another from the SAME file. The
    morning-side loader was fixed one round before `dca_config`'s, because the
    round that fixed it swept for siblings by comparing VALUES -- which these
    all agreed on -- rather than by removing the key and seeing who still
    started (Codex review, PR #786: "Fail closed when an existing budget omits
    the breaker", then "Reject an omitted breaker in the strategy loader").
    This test is that sweep, so the next loader added cannot quietly opt out.
    """
    import intraday_dca
    import market_open_dca
    import midday_dca
    import morning_dca
    from dca_config import load_strategy_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for src in CONFIG_DIR.glob("*.json"):
        shutil.copy(src, cfg_dir / src.name)
    budgets = json.loads((cfg_dir / "budgets.json").read_bytes())
    del budgets["circuit_breakers"]["max_consecutive_placement_failures"]
    (cfg_dir / "budgets.json").write_text(json.dumps(budgets), encoding="utf-8")

    loaders = {
        "load_strategy_config": lambda: load_strategy_config(cfg_dir),
        "load_max_consecutive_placement_failures": (
            lambda: morning_dca.load_max_consecutive_placement_failures(cfg_dir)
        ),
        # LIVE mode specifically: load_morning_config only overlays the live
        # circuit breakers under MORNING_LIVE_MODE, and a watchlist-mode config
        # places nothing, so there is no breaker for it to bypass.
        "load_morning_config": lambda: morning_dca.load_morning_config(str(cfg_dir), morning_dca.MORNING_LIVE_MODE),
        # The two real load_stream_config callers, rather than the shared
        # helper: these are what the market-open and intraday streams actually
        # start from.
        "load_market_open_config": lambda: market_open_dca.load_market_open_config(str(cfg_dir)),
        "load_intraday_config": lambda: intraday_dca.load_intraday_config(str(cfg_dir)),
        "load_midday_config": lambda: midday_dca.load_midday_config(str(cfg_dir)),
    }
    for name, call in loaders.items():
        try:
            call()
        except ValueError as exc:
            assert "max_consecutive_placement_failures" in str(exc), name
        else:
            raise AssertionError(
                f"{name} accepted a budgets.json with no breaker, so a "
                "$DCA_CONFIG_DIR override runs that live path uncapped"
            )
    # ... and it must be the OMISSION that is refused, not the config dir: the
    # same directory with the key restored has to load cleanly, or this test
    # would pass for the wrong reason.
    budgets["circuit_breakers"]["max_consecutive_placement_failures"] = 3
    (cfg_dir / "budgets.json").write_text(json.dumps(budgets), encoding="utf-8")
    for name, call in loaders.items():
        assert call() is not None, name


def test_budget_tiers_are_monotonically_non_decreasing():
    """The ladder is selected by `max(spend_idx, signal_idx)`, so a higher tier
    must never be a smaller allowance than a lower one."""
    for lower, higher in zip(BUDGET_TIERS, BUDGET_TIERS[1:], strict=False):
        assert higher.daily_cap_usd >= lower.daily_cap_usd
        assert higher.weekly_cap_usd >= lower.weekly_cap_usd


# --- Ticker universe ---------------------------------------------------------


def test_active_tickers_match_config_tickers_json():
    documented = _load("tickers.json")["active"]
    assert list(documented) == list(ACTIVE_TICKERS), (
        "the active universe drifted between near_close_dca.ACTIVE_TICKERS and config/tickers.json"
    )


def test_excluded_tickers_match_config_tickers_json():
    documented = _load("tickers.json")["excluded"]
    assert set(documented) == set(EXCLUDED_TICKERS), (
        "the exclusion list drifted between near_close_dca.EXCLUDED_TICKERS and config/tickers.json"
    )


def test_no_ticker_is_both_active_and_excluded():
    assert not (set(ACTIVE_TICKERS) & set(EXCLUDED_TICKERS))


def test_active_tickers_have_no_duplicates():
    assert len(ACTIVE_TICKERS) == len(set(ACTIVE_TICKERS))


def test_whole_share_tickers_match_config_tickers_json():
    documented = _load("tickers.json")["whole_share"]
    assert set(documented) == set(WHOLE_SHARE_TICKERS), (
        "the whole-share ticker list drifted between near_close_dca.WHOLE_SHARE_TICKERS and config/tickers.json"
    )


def test_whole_share_tickers_are_disjoint_from_active_and_excluded():
    # A whole-share ticker is a THIRD, distinct category (see
    # docs/whole_share_accumulation.md) -- membership in more than one of
    # active/excluded/whole_share would leave near_close_dca._run_once_impl's
    # ticker-list construction and _process_buy_candidate's dispatch
    # ambiguous about which path a symbol takes.
    assert not (set(ACTIVE_TICKERS) & set(WHOLE_SHARE_TICKERS))
    assert not (set(EXCLUDED_TICKERS) & set(WHOLE_SHARE_TICKERS))


def test_whole_share_tickers_have_a_one_dollar_per_ticker_sizing_entry():
    # A whole-share ticker (see near_close_dca.WHOLE_SHARE_TICKERS) is evaluated
    # by the SAME select_first_allocation_base_usd as the active universe. Every
    # active symbol's config/per_ticker_sizing.json entry deliberately floors
    # both base amounts at $1 rather than falling back to
    # StrategyConfig.base_buy_amount_usd's $2 default; a whole-share ticker with
    # no entry at all would silently fall back to that $2 default and
    # contribute double what every other ticker does at the same multiplier
    # (Codex review: "Give BYDDY the configured one-dollar sizing base").
    sizing = _load("per_ticker_sizing.json")
    for ticker in WHOLE_SHARE_TICKERS:
        entry = sizing["tickers"].get(ticker)
        assert entry is not None, f"{ticker} is missing a config/per_ticker_sizing.json entry"
        assert entry["base_amount_usd"] == 1.0, ticker
        assert entry["base_amount_below_close_usd"] == 1.0, ticker


def test_scheduled_tasks_universe_metadata_matches_the_real_universe():
    """config/scheduled_tasks.json carries a ticker_universe_policy summary and
    an active_count alongside its own copy of the arrays. Consumers and
    operators read that block as the canonical manifest, so a stale count or a
    summary naming only some of the whole-share symbols hands them
    contradictory metadata (Codex review, PR #786: "Synchronize the scheduled
    universe metadata")."""
    policy = _load("scheduled_tasks.json")["ticker_universe_policy"]
    assert policy["active_count"] == len(ACTIVE_TICKERS)
    for ticker in WHOLE_SHARE_TICKERS:
        assert ticker in policy["summary"], (
            f"{ticker} is a whole-share ticker but ticker_universe_policy.summary does not name it"
        )
    universe = _load("scheduled_tasks.json")["canonical_first_buy_and_topup_rule"]["active_universe"]
    assert list(universe["active"]) == list(ACTIVE_TICKERS)
    assert set(universe["whole_share"]) == set(WHOLE_SHARE_TICKERS)
    assert set(universe["excluded"]) == set(EXCLUDED_TICKERS)


def test_scheduled_tasks_circuit_breaker_mirror_carries_the_whole_share_ceiling():
    """config/scheduled_tasks.json's top-level `circuit_breakers` block
    describes itself as "Hard circuit breakers enforced on every scheduled
    live order" and stated the $10 `max_per_order_usd` cap as absolute --
    "No single order may exceed $10" -- with no exception, even after this
    PR's `ticker_universe_policy.summary` (the very next section) started
    describing RVII's ~$23 one-share fire going through
    `whole_share_max_per_order_usd` instead. A manifest-driven executor or
    validator enforcing the authoritative mirror literally would reject that
    fire, contradicting the routing this PR just wired (Codex review, PR #786:
    "Mirror the whole-share ceiling in the scheduled manifest").

    Pinned against `config/budgets.json`'s own value rather than a literal, so
    the two cannot drift silently -- `dca_config` loads the budgets.json copy
    at runtime; this manifest mirror is documentation for a human or an
    external agent reading the schedule, and must say the same number."""
    breakers = _load("scheduled_tasks.json")["circuit_breakers"]
    budgets_breakers = _load("budgets.json")["circuit_breakers"]
    assert breakers["whole_share_max_per_order_usd"] == budgets_breakers["whole_share_max_per_order_usd"]
    assert "No single order may exceed $10" not in breakers["max_per_order_note"], (
        "the mirror must not restate the $10 cap as absolute now that a whole-share exception exists"
    )
    assert "whole_share_max_per_order_usd" in breakers["max_per_order_note"]


def test_canonical_first_buy_rule_carries_the_whole_share_ceiling():
    """Fresh evidence after the `circuit_breakers` mirror above was fixed:
    `canonical_first_buy_and_topup_rule` -- a SEPARATE authoritative block,
    also describing itself as the source of truth for first-buy/top-up
    sizing -- had its own unscoped `per_order_ceiling_usd: 10.0` with no
    whole-share exception, even though this same block's own
    `active_universe.whole_share_note` documents routing RVII's ~$23
    one-share fire through it. A manifest-driven implementation of this
    rule would reject that fire outright (Codex review, PR #786: "Mirror
    the allowance in the canonical allocation policy").

    Pinned against `config/budgets.json`'s own value for the same reason as
    the `circuit_breakers` mirror above: `dca_config` loads the budgets.json
    copy at runtime, so this manifest mirror must never silently drift from
    it."""
    rule = _load("scheduled_tasks.json")["canonical_first_buy_and_topup_rule"]
    budgets_breakers = _load("budgets.json")["circuit_breakers"]
    assert rule["whole_share_max_per_order_usd"] == budgets_breakers["whole_share_max_per_order_usd"]
    assert "whole_share_max_per_order_usd" in rule["per_order_ceiling_note"]


def test_whole_share_tickers_are_not_excluded_from_month_end_risk_reporting():
    # config/scheduled_tasks.json's month_end_volatility_risk_policy.universe
    # documents a SEPARATE ticker-exclusion list (consumed by an external
    # agent session, not repo code -- see that policy's own "universe" block)
    # that must mirror config/tickers.json's true `excluded` category, never
    # its `whole_share` one. A whole-share ticker is actively purchased by the
    # graded ladder (see docs/whole_share_accumulation.md) -- it belongs in
    # this report's 30-day drop/3x-event/recovery-risk table alongside every
    # other active/whole-share holding, not silently omitted from it as if it
    # were a true exclusion (Codex review: "Include whole-share tickers in
    # month-end risk reporting").
    policy_excluded = set(_load("scheduled_tasks.json")["month_end_volatility_risk_policy"]["universe"]["excluded"])
    assert not (policy_excluded & set(WHOLE_SHARE_TICKERS)), (
        "month_end_volatility_risk_policy.universe.excluded must not list a whole-share ticker"
    )


# --- Secrets policy ----------------------------------------------------------

# Nine consecutive digits, bounded only by other digits or a decimal point.
#
# The bound is deliberately loose on the left. An earlier version excluded any
# adjacent word character or hyphen, which meant the common labeled forms --
# "robinhood-<digits>", "ACCOUNT_<digits>", "acct<digits>" -- slipped straight
# past, and this repo has already carried the identifier in a labeled
# "robinhood:<digits>" form once, so that is a demonstrated recurrence path and
# not a hypothetical one (Codex review, PR #219).
#
# The two exclusions that remain are the ones that produce false positives here:
# a longer digit run (an order id or epoch-millis timestamp is not an account),
# and a preceding "." (fractional seconds -- Robinhood returns nanosecond
# timestamps like 2026-07-06T18:31:29.201412148Z). Verified against the full
# tracked tree: those two exclusions leave exactly zero false positives, and
# dropping either the "\w" or the "-" exclusion adds none.
# resolve_broker_account and the classification schemas both accept 6-12 digits,
# so a scan for exactly nine missed most of the shapes a real account can take
# (Codex review, PR #219). The run must be a standalone token: without that,
# widening to 6-12 matched ~1800 substrings across the tree -- GitHub review ids
# (r3658334906), version fragments, and every embedded number -- and a guard that
# noisy gets exempted into uselessness.
_ACCOUNT_SHAPED = re.compile(r"(?<![.\w])\d{6,12}(?![\w.])")

# The anchored scan above deliberately requires a standalone token, which also
# excludes a digit run glued to a LABEL -- and "ACCOUNT_<digits>" or
# "acct<digits>" is exactly how a leaked account tends to be written (Codex
# review, PR #219). This second pattern covers that form directly instead of
# relaxing the anchor, which would let UUID fragments and review ids back in.
# Zero occurrences in the tree today, so it costs no exemptions.
# The label vocabulary covers the broker names this project actually uses as
# well as the generic words: "robinhood_<digits>" and "broker<digits>" are just
# as obvious a leak as "account_<digits>", and matched neither expression before
# (Codex review, PR #219). Zero occurrences in the tree, so no exemptions.
# "rh" is the repository's own shorthand for Robinhood (docs/live_wiring.md), so
# rh_<digits> is an established way to write an account here and matched neither
# expression (Codex review, PR #219). Left deliberately unanchored on the left:
# measured against the tree it costs zero false positives either way, and for a
# guard whose misses are silent while its false positives are one line to
# exempt, the wider form is the right side to err on.
_LABELED_ACCOUNT = re.compile(r"(?i)(?:account|acct|broker|robinhood|alpaca|rh)[^0-9A-Za-z]{0,3}(\d{6,12})")

# YYYYMM / YYYYMMDD. Trading days and dated artifact slots are all over config,
# docs, and tests; none is an account, and they are recognized by shape rather
# than listed because new ones appear with every run.
#
# The shape alone is not enough to exempt on: a six-digit run beginning "20"
# with an impossible month matches it but is not a date, and a six- or
# eight-digit account beginning 19 or 20 would have been
# discarded unexamined (Codex review, PR #219). _is_real_date() therefore
# requires an actual calendar date, so only genuine dates are exempt.
_DATE_LIKE = re.compile(r"(19|20)\d{4}(\d{2})?$")


# Cues that a line (or the file it lives in) is talking about dates. The date
# exemption is scoped to these: a valid calendar date is ALSO a valid 6- or
# 8-digit account, so exempting every date-shaped value everywhere left any
# account that happens to be a valid YYYYMM or YYYYMMDD committable with this
# guard green (Codex review, PR #219).
# Word-bounded on both sides. Without that, a cue matched as a SUBSTRING of an
# unrelated identifier: "candidate_pin" contains "date", so
# `candidate_pin = "<a valid date>"` was treated as date context and a
# supported account shape was exempted (Codex review, PR #219). Underscores
# count as boundaries here -- "trading_day" and "run_id" are the real cue forms
# -- so \b is not enough on its own and the classes are spelled out.
_DATE_CONTEXT = re.compile(
    r"(?i)(?<![0-9A-Za-z])"
    r"(?:date|dates|day|days|trading|timestamp|slot|schedule|scheduled|since|until"
    r"|window|run_id|et|month|yyyymm|yyyymmdd)"
    r"(?![0-9A-Za-z])"
)


# How much of the line before a match counts as "near" it. A cue anywhere on the
# line was already loose; a cue anywhere in the FILENAME was worse still, because
# "config/midday_dca.json" contains "day" and "scheduled_tasks.json" contains
# "schedule" -- so every date-shaped value anywhere in those files was exempt,
# including on an account-setting line (Codex review, PR #219). The filename is
# no longer consulted at all.
_DATE_CUE_WINDOW = 60


# Regex metacharacters that mean the surrounding text is a PATTERN rather than a
# value. schemas/*.json pin run_id date ranges as alternations, so a date can sit
# many characters from its "run_id" cue while still obviously not being an
# account literal -- nobody pastes an account into a regex alternation.
_REGEX_CONTEXT = re.compile(r"\(\?:|\[0-9\]\{|\\d\{")


def _css_without_comments(text: str) -> str:
    """``text`` with every ``/* ... */`` comment blanked to spaces (newlines kept), so
    line numbers and column positions still line up with the original file."""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


# A CSS declaration up to the colour token: ``prop: ... `` after a ``{``, ``;`` or the
# line start, with no further brace/semicolon in between.
_CSS_DECLARATION_PREFIX = re.compile(r"(?:^|[{;])\s*[A-Za-z-]+\s*:[^{};]*$")


def _is_css_hex_colour_value(css_lines: list[str], index: int, match: re.Match) -> bool:
    """True when an eight-digit run is a ``#RRGGBBAA`` colour VALUE in a declaration.

    ``css_lines`` is the stylesheet with comments blanked (see _css_without_comments).
    Requires, all on the comment-free line: exactly eight digits right after ``#``; the
    text before it to be an open ``property:`` declaration outside any string; and the
    next ``;``/``}`` to come before any ``{`` (so a selector like ``a:hover #<8 digits>{``
    or a selector list is NOT a colour). Anything else stays an account-shaped offender.
    """
    if index >= len(css_lines) or len(match.group(0)) != 8:
        return False
    line = css_lines[index]
    start, end = match.start(), match.end()
    if line[start:end] != match.group(0) or start == 0 or line[start - 1] != "#":
        return False
    prefix, rest = line[: start - 1], line[end:]
    if prefix.count('"') % 2 or prefix.count("'") % 2:
        return False
    if not _CSS_DECLARATION_PREFIX.search(prefix):
        return False
    terminator = re.search(r"[;}{]", rest)
    return terminator is None or terminator.group(0) != "{"


def _in_regex_pattern(line: str, match: re.Match) -> bool:
    """True when the match sits inside a regular expression rather than a value."""
    window = line[max(0, match.start() - _DATE_CUE_WINDOW) : match.start()]
    return bool(_REGEX_CONTEXT.search(window))


def _in_date_context(line: str, match: re.Match) -> bool:
    """True when a date-shaped value sits somewhere dates legitimately appear.

    Two ways to qualify: an explicit cue in the text immediately preceding the
    match (its key or label), or the value standing alone as a PATH SEGMENT
    (dated artifact directories such as ``model_usage/202607/`` and
    ``attempt_log/20260727/``). The path form additionally requires a separator
    on the line, so a bare quoted value in a config object -- which is what a
    leaked account would look like -- does not qualify.
    """
    window = line[max(0, match.start() - _DATE_CUE_WINDOW) : match.start()]
    if _DATE_CONTEXT.search(window):
        return True
    # The separator must be ADJACENT to the match, not merely present on the
    # line. Accepting a slash anywhere meant compact JSON such as
    # {"path":"/tmp/x","broker_account":"<a date>"} exempted the account: the
    # value is quoted on both sides and the line contains a slash, so it looked
    # like a path segment (Codex review, PR #219). One enclosing quote is
    # stepped over so a quoted segment inside a path expression still qualifies.
    before = line[: match.start()].rstrip()
    after = line[match.end() :].lstrip()
    if before.endswith(('"', "'")):
        before = before[:-1].rstrip()
    if after.startswith(('"', "'")):
        after = after[1:].lstrip()
    return before.endswith("/") or after.startswith("/")


def _is_real_date(value: str) -> bool:
    """True when a date-shaped literal is an actual YYYYMM or YYYYMMDD date."""
    if not _DATE_LIKE.fullmatch(value):
        return False
    fmt = "%Y%m" if len(value) == 6 else "%Y%m%d"
    try:
        datetime.strptime(value, fmt)
    except ValueError:
        return False
    return True


# Literals in the tree that widening to 6-12 digits newly matches, each reviewed
# individually and none an account number. Grouped by what they actually are:
#
#   * 42173154 -- the morning-live cron id (MORNING_LIVE_CRON_ID), by far the
#     most common at ~170 occurrences;
#   * HHMMSS clock values in tests (152001, 154500, ...);
#   * a unix timestamp (1730000000) and masking illustrations (123456,
#     123456789012) in test_near_close_dca.py;
#   * share counts, dollar amounts, and token totals quoted in the fetch//
#     analysis reports and the strategy monitors.
#
# NOTE none of these is nine digits -- the length a real Robinhood account uses
# -- so the previous nine-digit scan stays exactly as strict as it was, and the
# widening is what adds coverage rather than what needs excusing.
_NON_ACCOUNT_LITERALS = frozenset(
    {
        # The morning-live cron id (MORNING_LIVE_CRON_ID), ~170 occurrences.
        "42173154",
        # HHMMSS clock values and a unix timestamp in tests.
        "152001",
        # VS Code launch.json link.
        "830387",
        "110000",
        "154500",
        "152000",
        "100000",
        "120000",
        "1730000000",
        # Masking illustrations ("123456789012 becomes ********9012").
        "123456",
        "123456789012",
        # Share counts, dollar amounts, and token totals quoted in the fetch
        # reports, README_FETCH_SYSTEM, and the credit-usage proxy report.
        "4440195",
        "46240500",
        "676830",
        "2150000",
        "250000",
        "85700000",
        "450000",
        "50000000",
        "5000000",
        "2000000",
        "8190058",
        "11119381",
        # Synthetic broker order ids in test_dca_live_adapters.py.
        "4184502",
        "3483065",
        "5151032",
        # Google support-article id inside a documentation URL.
        "185833",
        # Fictional SEC EDGAR accession-number/CIK fixture pieces in
        # ticker-advisor/signals.py's _sec_filing_document_url() docstring
        # example and ticker-advisor/tests/test_signals.py's fetch_sec_filings
        # document-fetch tests -- SEC filing identifiers, not a brokerage
        # account number.
        "0001193125",
        "012345",
        "0000320193",
        "320193",
        # The microsecond component of an RFC3339 timestamp asserted in
        # test_dca_live_adapters.py -- neither a date nor an account.
        "201412",
        # Date-SHAPED non-dates used below as fixtures for _is_real_date. They
        # are deliberately impossible calendar values, which is exactly why the
        # scan no longer exempts them by shape.
        "201234",
        "20261345",
        "20260732",
        "199900",
        # SEC EDGAR Apple revenue fixture figures and mock CIKs in
        # dca-sizing-signals/tests/test_edgar_fundamentals.py
        "313695000000",
        "94036000000",
        "364357000000",
        "109417000000",
        "000020",
        "0000000000",
        # NVIDIA SEC CIK, accession-number pieces and capex amounts in
        # tests/test_scan_capex_deltas.py's XBRL fixture, not brokerage accounts.
        "1045810",
        "0001045810",
        "000213",
        "000120",
        "1227000000",
        "583000000",
        # Device bridge shell timeout in milliseconds (180s) in executor prompts
        "180000",
    }
)

# The ONLY account-shaped literals allowed anywhere in the tree. Both are
# synthetic and neither has ever been a real account:
#
#   * FIXTURE_ACCOUNT_NUMBER -- the value conftest.py assigns to
#     $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER for the whole suite;
#   * 123456789 -- the masking illustration ("123456789 becomes *****6789") in
#     config/scheduled_tasks.json, docs/credit_usage_proxy_report.md, and
#     scripts/tests/test_generate_credit_usage_proxy_report.py.
#
# Allowing specific VALUES rather than exempting whole files is deliberate.
# Exempting every test_*.py and scripts/tests/* file — which an earlier version
# of this guard did — left the largest recurrence path uncovered: the test tree
# was one of the places the real account number had to be removed from, and a
# future fixture that pasted it back would have sailed past this check
# (Codex review, PR #219: "Scan test files for account literals").
_SYNTHETIC_ACCOUNT_VALUES = frozenset({FIXTURE_ACCOUNT_NUMBER, "123456789", "100000"})

# Files exempt from scanning entirely. The first two are market-data tables whose
# volume columns are full of nine-digit share counts -- genuine data, not identifiers.
# They are named individually rather than exempting ".csv" as a class: an account
# pasted into any OTHER csv must still be caught (Codex review, PR #219).
#
# The third is the vendored three.js bundle (viz/vendor/, the repository's only
# third-party asset -- see its README for why it exists). Minified WebGL code is
# thousands of bare integer literals: packed RGB colours (0xFFFFFF as a decimal),
# bitmasks (0xFFFFFFFF), and shader constants. Every one is account-shaped to this
# scan. Note those are written here in hex deliberately -- spelling them in decimal
# would make this very comment an offender, which is a fair demonstration that the
# guard does what it claims.
#
# Exempting a file from a secrets guard needs a better argument than "it is noisy",
# and it has one: this file is not authored here, it is a pinned upstream artifact,
# and test_dca_market_graph.py::test_vendored_three_js_matches_its_recorded_checksum
# asserts its sha256 against the value recorded in viz/vendor/README.md. The exemption
# therefore only ever applies to bytes that are provably identical to the published
# npm tarball's -- edit the file to smuggle anything in, secret or otherwise, and the
# checksum test fails. Named individually, not as a "viz/vendor/" prefix, so a second
# vendored file cannot inherit the exemption without someone deciding it should.
_ALLOWED = {
    "historical_stock_data.csv",
    "claude/_agent_scratch/market_open_raw_data.json",
    "analysis/2026-06-29-revised-ladder/dca_revised_ladder_backtest_decisions.csv",
    "viz/vendor/three.min.js",
    # Tracked release archive; .tar.gz is skipped as binary and therefore needs
    # the same explicit exemption policy as every other unscanned tracked file.
    "dca-sizing-signals-package 2.tar.gz",
    # Generated package lockfile full of hashes and sizes that match account shapes.
    "uv.lock",
}

# The scan is an EXCLUDE list, not an include list. It used to allowlist text
# suffixes, which is a shape that can only ever be incomplete: .gitignore was
# tracked and unscanned, and an extensionless script or a future .ini/.cfg would
# have been too, so a supported account in any of them bypassed the guard
# entirely (Codex review, PR #219). Every tracked file is now read, and anything
# that is not valid UTF-8 is skipped by the decode itself -- which is what
# "binary" actually means here, rather than a list someone has to remember to
# extend. These suffixes are skipped before the read purely to avoid pulling
# large binaries into memory; adding one is an optimisation, not a policy.
_BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".svgz",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".xz",
    ".bz2",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pyc",
    ".so",
    ".dylib",
    ".dll",
}


def _is_scanned(rel: str) -> bool:
    """Every tracked file except known binaries; undecodable files are skipped
    at read time by the caller."""
    return Path(rel).suffix.lower() not in _BINARY_SUFFIXES


# Nothing else is exempt. analysis/ used to be, on the reasoning that dated
# audit records are immutable history -- but the account number was still
# tracked in four of those files, so the guard passed while the identifier it
# exists to remove was sitting in the working tree, and any future leak under
# analysis/ would have been invisible too (Codex review, PR #219). Those records
# are documentation; no code reads them, so the identifier is redacted in place
# and the directory is scanned like everything else.
_ALLOWED_PREFIXES: tuple[str, ...] = ()


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def test_no_account_shaped_literal_in_tracked_config_source_or_docs():
    """The live account number must never be re-inlined.

    It is supplied by $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER and referenced in config
    as "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}". It previously appeared 133 times
    across config/, docs/, schemas/, source, AND the test fixtures, while README
    and AGENTS.md both claimed the repository stored no such identifier.

    The test tree is scanned like everything else -- only the two synthetic
    values in _SYNTHETIC_ACCOUNT_VALUES pass, whatever file they appear in.
    """
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel in _ALLOWED or rel.startswith(_ALLOWED_PREFIXES):
            continue
        if not _is_scanned(rel):
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        css_lines = _css_without_comments(text).splitlines() if rel.endswith(".css") else []
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _LABELED_ACCOUNT.finditer(line):
                if match.group(1) not in _SYNTHETIC_ACCOUNT_VALUES:
                    offenders.append(f"{rel}:{lineno}: {match.group(1)}")
            for match in _ACCOUNT_SHAPED.finditer(line):
                value = match.group(0)
                if value in _SYNTHETIC_ACCOUNT_VALUES:
                    continue
                if value in _NON_ACCOUNT_LITERALS:
                    continue
                if _is_real_date(value) and (_in_date_context(line, match) or _in_regex_pattern(line, match)):
                    continue
                # A six-digit run right after "#" is a CSS hex colour, not an
                # account. Length-bounded on purpose: a nine-digit account
                # written as "account #<digits>" must still be caught.
                if len(value) == 6 and match.start() and line[match.start() - 1] == "#":
                    continue
                # Same for an eight-digit RGBA hex colour (a translucent black
                # shadow, say) -- but ONLY as a real colour VALUE in a stylesheet:
                # eight digits is also a valid account shape, so outside .css, and
                # inside a CSS comment, string or selector, it must still be caught
                # (Copilot review, PR #1597).
                if rel.endswith(".css") and _is_css_hex_colour_value(css_lines, lineno - 1, match):
                    continue
                offenders.append(f"{rel}:{lineno}: {match.group(0)}")

    assert not offenders, (
        "account-shaped literal(s) found in tracked files; use "
        '"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}" in config and the environment '
        "variable at runtime (only the synthetic values in "
        "_SYNTHETIC_ACCOUNT_VALUES are permitted):\n  " + "\n  ".join(offenders)
    )


def test_the_guard_rejects_a_real_account_literal_in_a_test_file(tmp_path, monkeypatch):
    """The guard must not be satisfied by a value simply because it sits in a test.

    Pins the behaviour the blanket test_*/scripts/tests/ exemption used to break:
    a fixture that pasted the live account number back in was skipped before the
    pattern was ever evaluated.
    """
    # Assembled from fragments rather than written as one literal: a real
    # nine-digit literal here would be flagged by the very guard under test when
    # it scans this file. (That it would be flagged is itself the proof the
    # tightened rule works -- test files no longer get a free pass.)
    probe_account = "1112" + "22333"
    offender = "test_regression_probe.py"
    # Written in a labeled form, which the previous pattern let through.
    (tmp_path / offender).write_text(f'ACCOUNT = "robinhood-{probe_account}"\n', encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.REPO_ROOT", tmp_path)
    monkeypatch.setattr(f"{__name__}._tracked_files", lambda: [offender])

    with pytest.raises(AssertionError, match=offender):
        test_no_account_shaped_literal_in_tracked_config_source_or_docs()


def test_the_guard_accepts_the_synthetic_fixture_value(tmp_path, monkeypatch):
    probe = "test_regression_probe.py"
    (tmp_path / probe).write_text(
        f'ACCOUNT = "{FIXTURE_ACCOUNT_NUMBER}"\nMASK_EXAMPLE = "123456789"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(f"{__name__}.REPO_ROOT", tmp_path)
    monkeypatch.setattr(f"{__name__}._tracked_files", lambda: [probe])

    test_no_account_shaped_literal_in_tracked_config_source_or_docs()


def test_the_guard_accepts_sec_capex_fixture_literals(tmp_path, monkeypatch):
    probe = "tests/test_scan_capex_deltas.py"
    (tmp_path / "tests").mkdir()
    (tmp_path / probe).write_text(
        'facts = {"cik": 1045810, "val": 1227000000, "prior_val": 583000000,\n'
        '         "accn": "0001045810-24-000213", "prior_accn": "0001045810-24-000120"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(f"{__name__}.REPO_ROOT", tmp_path)
    monkeypatch.setattr(f"{__name__}._tracked_files", lambda: [probe])

    test_no_account_shaped_literal_in_tracked_config_source_or_docs()

    # The fixture file remains scanned: unrelated account literals still fail.
    with (tmp_path / probe).open("a", encoding="utf-8") as handle:
        handle.write('ACCOUNT = "' + "1112" + '22333"\n')
    with pytest.raises(AssertionError, match=probe):
        test_no_account_shaped_literal_in_tracked_config_source_or_docs()


@pytest.mark.parametrize(
    "config_file,pointer",
    [
        ("morning_dca.json", ("live_task", "broker_account")),
        ("midday_dca.json", ("live_task", "broker_account")),
    ],
)
def test_live_broker_account_is_stored_as_an_env_reference(config_file, pointer):
    """The one config field the loaders actually read must be an env reference,
    not a literal -- otherwise the value is back in git and the environment
    variable is never consulted."""
    node = _load(config_file)
    for key in pointer:
        node = node[key]
    assert node == "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", (
        f"{config_file}:{'.'.join(pointer)} must reference the environment, got {node!r}"
    )


def test_a_tracked_dotenv_file_is_scanned_for_account_literals(tmp_path, monkeypatch):
    """.env has an empty suffix and .env.production a non-allowlisted one, so
    both used to slip past the suffix filter -- leaving the one format AGENTS.md
    names explicitly as where secrets live outside this guard (Codex review,
    PR #219)."""
    for name in (".env", ".env.production", ".env.local"):
        assert _is_scanned(name), f"{name} must be scanned"
    assert _is_scanned("config/budgets.json")
    assert not _is_scanned("assets/logo.png")


@pytest.mark.parametrize(
    "template",
    [
        "ACCOUNT_{0}",
        "acct{0}",
        "robinhood_{0}",
        "broker{0}",
        "alpaca-{0}",
        "BROKER_ACCOUNT: {0}",
        "rh_{0}",
        "RH-{0}",
        "account #{0}",
        "account: {0}",
        "the account is {0}",
    ],
)
def test_labelled_account_literals_are_detected(template):
    """A leaked account is usually written next to a label, and the anchored
    scan alone skips a digit run glued to one (Codex review, PR #219). Between
    the two patterns every common form must be caught. Built from the synthetic
    fixture value so this test does not itself trip the guard above."""
    text = template.format(FIXTURE_ACCOUNT_NUMBER)
    assert _LABELED_ACCOUNT.search(text) or _ACCOUNT_SHAPED.search(text), text


@pytest.mark.parametrize("value", ["201234", "20261345", "20260732", "199900"])
def test_date_shaped_non_dates_are_not_exempt(value):
    """Only real calendar dates earn the exemption; an account that merely looks
    date-shaped must still be caught (Codex review, PR #219)."""
    assert not _is_real_date(value)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 7, 1).strftime("%Y%m"),
        datetime(2026, 7, 28).strftime("%Y%m%d"),
        datetime(2024, 2, 29).strftime("%Y%m%d"),  # a real leap day
    ],
)
def test_real_dates_stay_exempt(value):
    assert _is_real_date(value)


@pytest.mark.parametrize(
    "clause,expected",
    [
        ("planned spend <= $129 and 3x-or-greater signals <= 5", ("<=", 129.0)),
        ("planned spend > $213 or 3x-or-greater signals 11-17", (">", 213.0)),
    ],
)
def test_spend_boundaries_carry_their_operator(clause, expected):
    """Comparing only the number let "<= $129" drift to "< $129" unnoticed while
    _escalation_index kept using a strict ">" (Codex review, PR #219)."""
    assert _parse_spend_boundary(clause) == expected


@pytest.mark.parametrize("rel", [".gitignore", ".env", "scripts/deploy", "Makefile", "tox.ini", "setup.cfg"])
def test_extensionless_and_dotfiles_are_scanned(rel):
    """The guard used to allowlist text SUFFIXES, which can only ever be
    incomplete -- .gitignore was tracked and unscanned, and an extensionless
    script or a future .ini would have been too (Codex review, PR #219)."""
    assert _is_scanned(rel)


@pytest.mark.parametrize("rel", ["assets/logo.png", "build/x.pyc", "docs/manual.pdf"])
def test_binaries_are_skipped_before_the_read(rel):
    assert not _is_scanned(rel)


def test_a_tracked_source_archive_must_be_a_named_exemption(monkeypatch):
    """A tracked .tar.gz -- a vendored package, a dependency snapshot -- is
    skipped before the read, which is exactly why it may not pass silently: an
    unscanned file that nobody named is a hole in the guard, not an exemption.
    Someone has to decide, in ``_ALLOWED``, that this particular archive is
    allowed to go unread."""
    rel = "some-vendored-package.tar.gz"
    assert not _is_scanned(rel)
    assert rel not in _ALLOWED
    monkeypatch.setattr(f"{__name__}._tracked_files", lambda: [rel])

    with pytest.raises(AssertionError, match=re.escape(rel)):
        test_every_tracked_file_is_either_scanned_or_a_named_exemption()


def test_every_tracked_file_is_either_scanned_or_a_named_exemption():
    """No file may fall through the guard unnoticed."""
    for rel in _tracked_files():
        assert _is_scanned(rel) or rel in _ALLOWED or rel.startswith(_ALLOWED_PREFIXES), rel


# Derived, not spelled: a date-shaped literal in this file is itself an
# account-shaped literal the guard above must flag.
_A_DATE = datetime(2026, 7, 28).strftime("%Y%m%d")


def test_a_date_cue_in_the_filename_does_not_exempt_the_whole_file():
    """ "config/midday_dca.json" contains "day" and "scheduled_tasks.json"
    contains "schedule", so consulting the filename exempted every date-shaped
    value in them, including on an account line (Codex review, PR #219)."""
    line = f'  "broker_account": "{_A_DATE}",'
    match = _ACCOUNT_SHAPED.search(line)
    assert match is not None
    assert not _in_date_context(line, match)


def test_a_date_cue_next_to_the_match_still_exempts():
    line = f'  "trading_day": "{_A_DATE}",'
    match = _ACCOUNT_SHAPED.search(line)
    assert match is not None
    assert _in_date_context(line, match)


def test_a_dated_path_segment_still_exempts():
    line = f'path = tmp_path / "attempt_log" / "{_A_DATE}" / "morning.json"'
    match = _ACCOUNT_SHAPED.search(line)
    assert match is not None
    assert _in_date_context(line, match)


@pytest.mark.parametrize(
    "line,exempt,label",
    [
        ('{{"path":"/tmp/artifacts","broker_account":"{0}"}}', False, "slash elsewhere on the line"),
        ('  "model_usage/{0}/model_usage.jsonl"', True, "real path segment"),
        ('p = tmp_path / "attempt_log" / "{0}" / "x.json"', True, "quoted segment in a path expr"),
        ('  "broker_account": "{0}",', False, "account line with no cue"),
    ],
)
def test_the_path_form_requires_an_adjacent_separator(line, exempt, label):
    """Accepting a slash anywhere on the line exempted a date-shaped account in
    compact JSON, because the value is quoted and the line happens to contain a
    path (Codex review, PR #219)."""
    text = line.format(_A_DATE)
    match = _ACCOUNT_SHAPED.search(text)
    assert match is not None, label
    assert _in_date_context(text, match) is exempt, label


@pytest.mark.parametrize(
    "line,exempt,label",
    [
        ('candidate_pin = "{0}"', False, "'date' inside 'candidate' must not cue"),
        ('update_candidates = "{0}"', False, "'candidates' must not cue"),
        ('  "trading_day": "{0}",', True, "a real cue still exempts"),
        ('  "scheduled_for": "{0}",', True, "'scheduled' still cues"),
    ],
)
def test_date_cues_are_word_bounded(line, exempt, label):
    """A cue matched as a SUBSTRING of an unrelated identifier exempted a
    supported account shape (Codex review, PR #219)."""
    text = line.format(_A_DATE)
    match = _ACCOUNT_SHAPED.search(text)
    assert match is not None, label
    assert _in_date_context(text, match) is exempt, label


# --- Runtime-parsed account fields must use the expandable placeholder --------
# ${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER} is resolved by dca_config.expand_config_env
# at load time; <ROBINHOOD_ACCOUNT_NUMBER> is prose that is never substituted.
# The two forms look similar enough that a config field a loader actually reads
# could be written in the prose form by mistake -- silently feeding the literal
# string "<ROBINHOOD_ACCOUNT_NUMBER>" to resolve_broker_account() instead of a
# real account, which fails closed (a malformed value is rejected, per
# test_account_pinning_rejects_malformed_accounts) but for the wrong, confusing
# reason. This checks the fields BY PATH, not by scanning every occurrence of
# "broker_account" in the tree: config/*.json's patch/incident-note blocks
# (e.g. watchdog_reconciliation_scope_patch_2026_07_27) also use the key
# "broker_account", but nothing reads those blocks at runtime -- see
# morning_dca.load_morning_config / midday_dca.load_midday_config, which only
# ever look at raw["live_task"] and raw["watchlist_task"]. Flagging those prose blocks would be noise, not signal.
_RUNTIME_BROKER_ACCOUNT_PATHS = {
    "morning_dca.json": ("live_task", "watchlist_task"),
    "midday_dca.json": ("live_task",),
}


def test_runtime_parsed_broker_account_fields_use_the_expandable_placeholder():
    offenders = []
    for filename, task_keys in _RUNTIME_BROKER_ACCOUNT_PATHS.items():
        document = _load(filename)
        for task_key in task_keys:
            block = document.get(task_key)
            if not isinstance(block, dict) or "broker_account" not in block:
                continue
            value = block["broker_account"]
            if isinstance(value, str) and "<" in value and ">" in value:
                offenders.append(f"{filename}:{task_key}.broker_account = {value!r}")
    assert not offenders, (
        "runtime-parsed broker_account fields must use "
        '"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}" (expanded by '
        "dca_config.expand_config_env), not an angle-bracket placeholder that "
        f"is never substituted: {offenders}"
    )


# --- The seven-slot executor prompts stay structurally in sync --------------
# claude/dca-executor-prompts/[1-7]-executor-*.txt are seven independently
# pasted prompt files (0-setup-card.md's manual-paste deployment model -- there
# is no shared template at runtime, by design: each prompt must carry its own
# complete, self-contained alert contract so a truncated fire still has it,
# rather than referencing a shared doc that could itself be truncated away).
# That duplication is deliberate, but it is also exactly the kind of text that
# can drift silently across seven copy-pasted files with no runtime code to
# catch it -- two review rounds on PR #246 found real drift this way (a Gate 4
# alert channel left silent after GATE5_DISABLED was fixed to alert; the
# truncation-safe header's channel list left scoped to only its final
# fallback), each caught by a human/Codex reading all seven files side by
# side rather than by any check. This is that check (Codex review: "Add a
# Prompt Consistency / Lint Test").

PROMPTS_DIR = REPO_ROOT / "claude" / "dca-executor-prompts"

_SLOT_FILES = [
    "1-executor-0935-cron_35_13.txt",
    "2-executor-1030-cron_30_14.txt",
    "3-executor-1130-cron_30_15.txt",
    "4-executor-1230-cron_30_16.txt",
    "5-executor-1330-cron_30_17.txt",
    "6-executor-1430-cron_30_18.txt",
    "7-executor-1530-cron_30_19.txt",
]

# Each slot's Gate 1 window, as it must appear both in the truncation-safe
# header and in Gate 1's own body section -- derived from the slot's own
# filename, so a copy-pasted neighbor's window landing in the wrong file is
# caught even though nothing at runtime ever reads these files as config.
_SLOT_TIME_WINDOWS = {
    "1-executor-0935-cron_35_13.txt": "9:35-10:00 AM ET",
    "2-executor-1030-cron_30_14.txt": "10:30-11:00 AM ET",
    "3-executor-1130-cron_30_15.txt": "11:30 AM-12:00 PM ET",
    "4-executor-1230-cron_30_16.txt": "12:30-1:00 PM ET",
    "5-executor-1330-cron_30_17.txt": "1:30-2:00 PM ET",
    "6-executor-1430-cron_30_18.txt": "2:30-3:00 PM ET",
    "7-executor-1530-cron_30_19.txt": "3:30-4:00 PM ET",
}

# Slots whose legacy counterpart has a committed, verifiable ref_id convention
# to attribute a same-window order to (see each prompt's Gate 4 and
# 0-setup-card.md) -- the other four slots have no such committed convention
# and must never attempt the classification, per Codex review ("Recognize the
# real legacy stream ref-id prefixes").
_SLOTS_WITH_GATE4_EXPECTED_OVERLAP = {
    "2-executor-1030-cron_30_14.txt",
    "4-executor-1230-cron_30_16.txt",
    "7-executor-1530-cron_30_19.txt",
}


def _prompt_text(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _midday_live_mirror_text() -> str:
    return (PROMPTS_DIR / "live-mirror" / "1230ET-1230-pm-midday-buying-run.txt").read_text(encoding="utf-8")


def test_alert_digest_docs_resolve_the_artifact_root_portably():
    """Keep the documented alert scan aligned with the shared path policy."""
    text = (REPO_ROOT / "docs" / "alert_digest.md").read_text(encoding="utf-8")

    assert "from dca_paths import cron_tracking_root" in text
    assert '--alerts-dir "$ALERTS_ROOT"' in text
    assert "/home/user/" not in text


def _placing_prompt_texts() -> dict:
    """Every prompt whose Gate 7 actually places orders.

    As of 2026-09-02 this is all seven live-mirror slots: the midday mirror's
    former planning-only override ("Live placement (Gate 7) is OFF
    unconditionally") was removed once placement was authorized for that
    slot too, so it now carries a real placement response to classify like
    every other slot.
    """
    texts = {f: _prompt_text(f) for f in _SLOT_FILES}
    mirror_dir = PROMPTS_DIR / "live-mirror"
    for path in sorted(mirror_dir.glob("*.txt")):
        if path.name.startswith("."):
            # .template.txt is a leftover generator scratch note, not a prompt.
            continue
        texts[f"live-mirror/{path.name}"] = path.read_text(encoding="utf-8")
    return texts


def _live_mirror_placing_prompt_texts() -> dict:
    """Live-mirror prompts whose Gate 7 actually places orders, excluding the
    superseded ``N-executor-*.txt`` generation (``README.md``: "do not patch
    these"). PR #1281 authorized live placement for the midday mirror too
    (removing its former "OFF unconditionally" text), so this now returns
    all seven live-mirror prompts.

    Unlike ``_placing_prompt_texts()`` above -- which intentionally also
    checks the frozen superseded generation for an invariant it already
    satisfies -- fixes landing only in the live generation must not be
    asserted against prompt text this repo's own policy forbids editing.
    """
    mirror_dir = PROMPTS_DIR / "live-mirror"
    texts = {}
    for path in sorted(mirror_dir.glob("*.txt")):
        if path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8")
        if "Live placement (Gate 7) is OFF unconditionally" in text:
            continue
        texts[f"live-mirror/{path.name}"] = text
    return texts


_MIDDAY_MIRROR_KEY = "live-mirror/1230ET-1230-pm-midday-buying-run.txt"


def _six_file_placing_prompt_texts() -> dict:
    """``_live_mirror_placing_prompt_texts()`` minus the midday mirror.

    Midday's per-order revalidation loop is structurally distinct (its own
    three-phase FIRST/SECOND/script design, later restructured again by
    PR #1281's REVALIDATE-BEFORE-EVERY-ORDER rewrite) and carries its own
    Codex-review citation wording for the equivalent fixes -- checked
    separately by the dedicated ``test_midday_live_mirror_*`` tests below.
    Tests asserting the SIX shared-block prompts' exact citation strings
    must not run those same assertions against midday's differently-worded
    text.
    """
    return {name: text for name, text in _live_mirror_placing_prompt_texts().items() if name != _MIDDAY_MIRROR_KEY}


def test_every_placing_prompt_isolates_only_proven_no_order_failures():
    """The prompts are what actually runs Gate 7 today -- no prompt calls
    execute_plan() (docs/live_wiring.md) -- so the halt-vs-isolate rule that
    lives in dca_runner._placement_response_may_hold_a_live_order has to be
    stated in the prose too.

    Telling a prompt-driven executor to isolate every rejection unconditionally
    means it continues past a response that may hold an accepted order for an
    amount no cap check saw, placing the rest of the batch against arithmetic
    that is already wrong (Codex review, PR #786: "Isolate only failures that
    prove no order was accepted"). Mirrors the same gap round 3 found for the
    consecutive-failure breaker: a rule that reaches only dca_runner reaches
    nothing that runs."""
    required = [
        # Isolate only on proof, and name both proving shapes.
        "ISOLATE ONLY A FAILURE THAT PROVES NO ORDER EXISTS",
        # The no-id disjunct must ALSO require silence about execution: a
        # response omitting the id while reporting a fill is real spend
        # (Codex review, PR #786: "Check fill evidence before isolating
        # missing-ID responses").
        # The id search must be as wide as the fill scan: an accepted id can
        # sit on an enclosing envelope while the inner one carries only an
        # error (Codex review, PR #786: "Inspect order IDs across every
        # response envelope").
        "no usable order id at ANY nesting level",
        "not just the innermost object",
        # ... and "no usable id" must not be read as "no id". An id the
        # executor cannot reproduce exactly is the broker naming an order it
        # cannot track, which is ambiguous; only an absent/empty id or a
        # wrapper's 0/-1 sentinel is really no id (Codex review, PR #786:
        # "Preserve acceptance evidence from oversized positive IDs"). The
        # code learned this and the prompts had to as well -- the same gap
        # rounds 9, 10, 12, 17, 20 and 38 each closed after the fact.
        "An id you cannot READ is NOT the same as no id",
        "that is AMBIGUOUS and STOPS THE BATCH",
        # ... and the same question on the OTHER side. Every rule above is
        # about a failure; a confirmed success is proof about the one order
        # validated, not proof it is the only one the response names (Codex
        # review, PR #786: "Inspect sibling orders before accepting a
        # successful response"). The engine learned this and the prose has to
        # as well -- the gap rounds 9, 10, 12, 17, 20, 38 and 39 each closed
        # after the fact.
        "a CONFIRMED SUCCESS is only proof about the ONE order you validated",
        "The same id repeated at two levels is one order and is fine",
        # ... and "the same id" is a CLAIM of sameness, not proof of it. Round
        # 44 taught the engine that an echo repeating the validated order's id
        # must still AGREE with it, and round 45 that agreement is about the
        # field's meaning rather than its spelling -- while the prose still
        # said a repeated id "is fine" full stop, which is the very shape both
        # rounds fixed. The prompts run Gate 7 today, so a rule the code
        # enforces and the prose contradicts is the worse half being live
        # (Codex review, PR #786: "Inspect contradictory same-ID sibling
        # envelopes", "Compare client-reference aliases across echoed
        # envelopes").
        "ONLY WHILE THE REPEATS AGREE",
        "a CLAIM of sameness and not proof of it",
        "symbol and instrument_symbol are the same field",
        "client_order_uuid and client_id are all the same client reference",
        # ... with ONE exemption, matching `_CHAIN_ORDER_REFERENCE_KEYS`: an
        # ENCLOSING level's own `client_id` is where a transport wrapper puts
        # its request id, and the engine deliberately does not compare it
        # against the order's reference. Round 55 narrowed the engine and left
        # the prose broader, so the seven prompt-driven live slots would have
        # halted every remaining ticker on an ordinary successful response the
        # engine accepts (Codex review, PR #786: "Exempt transport client IDs
        # in executor prompts"). The exemption is pinned as narrowly as it is
        # written: client_id ALONE, enclosing levels ALONE.
        "ONE EXCEPTION, and only one",
        "a client_id on an ENCLOSING level",
        "That exemption is for client_id alone and for enclosing levels alone",
        "still apply in full WITHIN the validated order itself",
        # ... but a later state is still one order: halting on queued-beside-
        # filled would stop ordinary successes whose wrapper reports progress
        # at two levels, which is why the engine compares every field EXCEPT
        # state here.
        "A repeat that only reports a LATER STATE",
        # ... and "agrees" is exact for a client reference and case-blind for
        # a ticker. Our own references end in the UPPERCASE ticker, so folding
        # case made "...-AAPL" and "...-aapl" one reference and let a sibling
        # naming a different request pass as an echo (Codex review, PR #786:
        # "Preserve client-reference case when comparing aliases").
        "Compare the client reference EXACTLY, letter",
        "be compared ignoring case",
        # ... the same question asked WITHIN one order, since the aliases are
        # spellings of one field there too (Codex review, PR #786: "Validate
        # every client-reference alias in the primary order").
        "The same applies WITHIN one order",
        "contradicting itself",
        # ... and an unreadable POSITIVE id names an order on the success path
        # exactly as it does on the failure path -- round 39's rule, which the
        # success-path check was built without (Codex review, PR #786: "Halt
        # on untrackable positive IDs in successful siblings").
        "an order id you cannot READ is still an order the broker named",
        "unreadably large POSITIVE id stops the batch",
        "REPORTS a fill",
        # EVERY supplied figure, not just one of them -- the prompts have to
        # state the same rule _terminal_order_may_have_filled enforces, or a
        # prompt-driven executor isolates a real partial fill whose quantity
        # field happens to read zero (Codex review, PR #786: "Require every
        # supplied fill field to report zero").
        "EVERY fill figure attributable to it be an explicit zero",
        "EVERY, not any one of them",
        # Pin the concrete mixed-field counterexample as well as the abstract
        # rule.  Without it, a future rewrite could retain the keywords above
        # while still leaving an executor unsure how to classify conflicting
        # quantity/notional evidence.
        "cumulative_quantity 0 alongside executed_dollar_amount 5 is a real $5 fill",
        # ... and one response can name MORE THAN ONE order -- a wrapper's own
        # request identity outside, the real broker order inside. Isolating
        # requires all of them to be provably dead; the code learned this in
        # round 19 and the prompts did not, which is the same gap rounds 9, 10,
        # 12 and 17 each closed after the fact (Codex review, PR #786:
        # "Inspect all response identities before isolating").
        "EVERY ORDER, not merely the first one you find",
        # ... and the zero-fill question is asked PER ORDER, so one identity's
        # explicit zero can never answer for a sibling that said nothing about
        # its own fill (Codex review, PR #786: "Require zero fill evidence for
        # each terminal identity").
        "ONCE PER ORDER, not once for the response as a whole",
        "a fill figure on an envelope naming no order is unattributed",
        # An unattributed ZERO vouches for one order only: with two named
        # orders it cannot say which it describes, so each needs its own
        # (Codex review, PR #786: "Require distinct zero-fill evidence for
        # each order"). An unattributed POSITIVE still poisons all of them.
        "An unattributed ZERO is different",
        "each order needs its own explicit zero",
        # ... and DISTINCT orders are counted, not envelope levels: a wrapper
        # echoing one id at two levels describes one order twice, and asking
        # the echo for fill evidence it cannot supply halted ordinary rejected
        # orders (Codex review, PR #786: "Deduplicate envelopes that repeat
        # the same order ID").
        "Count DISTINCT orders, though, not envelope levels",
        "Two levels of one order that DISAGREE",
        # ... and ids match on type as well as characters. Stringifying alone
        # merged numeric 1 with string "1", letting a terminal request's zero
        # answer for a distinct inner order that reported no fill (Codex
        # review, PR #786: "Keep differently typed order IDs in separate
        # groups").
        "same characters AND same type",
        "do not treat the number 1 and the string",
        # ... and an id has to be a REAL identifier before it counts as naming
        # an order at all, because naming one is what routes a response PAST
        # the no-identity acceptance-state and success-flag checks (Codex
        # review, PR #786: "Treat invalid IDs as missing before reading
        # success flags").
        "before it counts as naming an order at all",
        "treat that response as carrying NO order id",
        # Arrays are walked ALL the way down. A nested array was skipped
        # silently -- not even reported as unreadable -- so a live order two
        # levels into a list read as "no order anywhere" (Codex review,
        # PR #786: "Traverse nested response arrays before isolating").
        "including one array nested inside another",
        # A bridge/transport failure is the response-shaped twin of a raised
        # call and gets the same halt, while an ordinary refusal -- which
        # carries no isError at all -- still isolates (Codex review, PR #786:
        # "Halt on ID-less MCP bridge errors").
        "reports `isError` as the literal boolean true",
        "carries no `isError` at all, and that still ISOLATES",
        # An order id is not the only way a response says the broker accepted
        # something: a working or completed state is acceptance evidence even
        # with no id anywhere (Codex review, PR #786: "Honor working states
        # even when an order ID is missing"). Scoped to that vocabulary, and
        # the later absent-fill sentence has to agree with it rather than
        # contradict it -- the drift rounds 9, 10, 12 and 17 each had to undo.
        "reports no state meaning the broker took the order",
        "new/queued/confirmed/unconfirmed/partially_filled/filled",
        "a state word you simply do not recognize is the ordinary shape of a refusal",
        "UNLESS the response reports an accepting state",
        # ... and that acceptance test is read LEVEL BY LEVEL, not only when
        # the response names no order anywhere. The code's per-identity loop
        # inspects only envelopes that carry an id, so an ID-less `queued`
        # beside a rejected named order was never looked at, and the prompt
        # prose had the same "omits the id" framing (Codex review, PR #786:
        # "Inspect ID-less acceptance states beside named orders"). Scoped to
        # the accepting states only -- re-reading an unnamed level's
        # `cancelled` as a second order would halt every ordinary
        # cancellation.
        "That acceptance rule is NOT limited to responses with no id anywhere",
        "stops the batch even when OTHER levels do name orders",
        "Only the accepting states are read this way",
        # ... and the post-acceptance ones are read as a CONTRADICTION test.
        # Round 28 excluded them wholesale, justified as "an unnamed level
        # restating a named order's cancellation is not new information" --
        # true, but written broader than its own justification: an unnamed
        # `cancelled` beside a named REJECTED order restates nothing (Codex
        # review, PR #786: "Inspect ID-less terminal states beside named
        # orders").
        "as a CONTRADICTION test",
        "If NO named order reports the state an unnamed level reports",
        "it is an ordinary echo",
        # An explicit success flag is acceptance evidence too, and it is the
        # one signal that reaches the classifier having ALREADY been read by
        # the failure reader -- which rejected the response only for want of
        # an id (Codex review, PR #786: "Halt on success-flagged responses
        # that omit an ID"). Strict booleans, and no-identity only.
        "the wrapper's own explicit success flag",
        "It must be a real true, not merely a truthy value",
        "a success flag is describing the API call rather than that order's fate",
        # An omitted fill figure is not a zero -- but what that means is
        # OPPOSITE with and without an order id, and the unqualified version
        # of this sentence told a prompt-driven executor to halt on the first
        # ordinary rejection (Codex review, PR #786: "Limit absent-fill halts
        # to responses with order IDs").
        "ABSENT fill figure is not a zero",
        "WITH a usable order id at any level",
        "WITHOUT any usable order id",
        "Never read this as 'any missing fill figure halts'",
        # ... and it has to be looked for at EVERY level of the response, not
        # only the innermost order object: a wrapper can report the fill on
        # the outer envelope, and reading one level discards it (Codex review,
        # PR #786: "Inspect fill evidence across every response envelope").
        # The walk covers the WHOLE response, not the response -> data ->
        # data.order path: an order can hang off a sibling of `data` or an
        # `orders` array, and following one branch never visits it (Codex
        # review, PR #786: "Inspect order mappings outside the selected data
        # branch"). And one level naming two DIFFERENT ids is unattributable,
        # so it halts ("Inspect both identifier keys in each envelope").
        "READ EVERY LEVEL OF THE RESPONSE",
        "an order can hang off ANY branch",
        "If ONE level names two DIFFERENT order ids",
        # A response too big or too nested to inspect fully is "could not
        # check", not "no order found" -- the code's own bounds fail closed
        # for the same reason (Codex review, PR #786: "Fail closed when
        # response traversal is truncated").
        'that is not "no order found"',
        # `cancelled`/`expired` mean an order existed and was then killed, so
        # they need an explicit zero even with no id, while `rejected`/
        # `failed`/`voided` stay ordinary refusals (Codex review, PR #786:
        # "Require zero fills for missing-ID cancellations").
        "TERMINAL states split in two here",
        "mean an order EXISTED and was then killed",
        # The ambiguous cases that must halt.
        "$100 confirmed for a requested $2",
        "STOPS THE BATCH",
        # The unrecognized-state isolation is scoped to the NO-ID refusal, and
        # says so. Written unscoped it contradicted the per-order rule 900
        # words below it in the same paragraph -- "if any named order ... omits
        # its state everywhere it appears, or has a state you cannot read, the
        # batch STOPS" -- so a prompt-driven executor reading `{"id": "ORD",
        # "state": null}` could follow the earlier sentence and continue past a
        # named order whose status and spend are both unresolved, which is
        # exactly what the engine halts on (Codex review, PR #786: "Scope the
        # prompt's unknown-state isolation rule to missing IDs").
        "WHERE THE RESPONSE NAMES NO ORDER, an absent state",
        "WHERE A NAMED ORDER carries it, the OPPOSITE holds",
    ]
    texts = _placing_prompt_texts()
    assert len(texts) >= 13, f"expected every placing prompt, got {sorted(texts)}"
    missing = [f"{name}: {phrase!r}" for name, text in texts.items() for phrase in required if phrase not in text]
    assert not missing, (
        f"every placing prompt must carry the full halt-vs-isolate rule, not an unconditional 'continue': {missing}"
    )


def test_no_prompt_escalates_a_bare_order_rejection():
    """The Gate 7 carve-out has to hold everywhere in the file, not only where
    it is announced.

    Each prompt's standing rules also carried "the failure is a
    safety/account/ORDER rejection, escalate: abort and alert" further down the
    same paragraph. A prompt-driven executor reading that clause halts on the
    first rejection regardless of the carve-out above it -- the behavior this
    whole change removes (Codex review, PR #786: "Remove the remaining
    abort-on-order-rejection instruction"). Checked across EVERY prompt, not
    just the placing ones: the contradiction is in the standing rules, which
    every prompt carries."""
    texts = {f: _prompt_text(f) for f in _SLOT_FILES}
    mirror_dir = PROMPTS_DIR / "live-mirror"
    for path in sorted(mirror_dir.glob("*.txt")):
        if path.name.startswith("."):
            continue
        texts[f"live-mirror/{path.name}"] = path.read_text(encoding="utf-8")

    # Phrasings that lump an order rejection in with the safety/account
    # rejections that really do end the run.
    forbidden = (
        "safety/account/order rejection",
        "safety, account, or order rejection",
    )
    offenders = [
        f"{name}: {phrase!r}" for name, text in texts.items() for phrase in forbidden if phrase.lower() in text.lower()
    ]
    assert not offenders, (
        "an order rejection at Gate 7 is isolated, not escalated -- these "
        f"prompts still group it with the rejections that end the run: {offenders}"
    )


def test_no_placing_prompt_still_claims_only_two_stop_conditions():
    """The standing-rule carve-out used to enumerate 'the two batch-level stop
    conditions Gate 7 itself names'. Leaving that count behind while Gate 7
    grew a third one tells the executor the ambiguous-response halt is not one
    of them."""
    offenders = [name for name, text in _placing_prompt_texts().items() if "two batch-level stop conditions" in text]
    assert not offenders, offenders


def _market_open_live_mirror_text() -> str:
    return (PROMPTS_DIR / "live-mirror" / "0935ET-935-am-dca-morning-buy.txt").read_text(encoding="utf-8")


def test_market_open_live_mirror_fail_closes_reviewed_placement_boundaries():
    text = _market_open_live_mirror_text()

    # A recovered quote may remain below both session references while moving to
    # a shallower ladder tier; retaining the anchor-sized amount would oversize it.
    assert "fresh price is GREATER than the row's Gate 5 current_price, DROP it" in text
    # Logged target/decision references must stay bound to the actual prior order.
    assert "prior_logged_reference_price and prior_logged_reference_order_id" in text
    assert "exactly matches the latest same-day broker order" in text
    assert text.index("Keep the survivors in memory") < text.index("After enrichment")
    assert text.index("After enrichment") < text.index("python3 -m scripts.same_day_top_up_gate")
    assert "Do not invoke the CLI against an earlier version of the file" in text
    assert "ONLY when the fully paginated broker history likewise has no qualifying prior same-day filled buy" in text
    assert "manual or broker-UI buys with no run artifact" in text
    assert text.index("Resolve EVERY root") < text.index("Fetch today's order history")
    assert text.index("Fetch today's order history") < text.index("re-fetch each surviving candidate's current quote")
    # A slow recursive scan makes all shared safety snapshots stale. Refresh the
    # clock, sole-executor classification, shared caps, and buying power after it.
    scan_refresh = text[text.index("The completed scan invalidates") : text.index("Fetch today's order history")]
    assert "repeat Gate 1's ET time-sanity check" in scan_refresh
    assert "repeat Gate 4 in full" in scan_refresh
    assert "reclassify EVERY in-window order" in scan_refresh
    assert "repeat Gate 6 in full" in scan_refresh
    assert "shared daily and ISO-week spend" in scan_refresh
    assert "freshly fetched buying power" in scan_refresh
    assert "no second budget pass is needed" not in text
    # Placement rechecks the clock/interlock for every order, without classifying
    # this run's already-submitted broker order IDs as newly rogue activity.
    gate_7 = text[text.index("GATE 7 - PLACE THE ORDERS") : text.index("GATE 8 - CONFIRM")]
    assert "Immediately before EACH sequential placement" in gate_7
    assert "repeat Gate 1's ET wall-clock check" in gate_7
    assert "Gate 4's fully paginated, classify-every-order sole-executor interlock" in gate_7
    assert "Fetch all pages again before every order" in gate_7
    assert "broker order IDs successfully submitted earlier by THIS run" in gate_7
    assert "in-memory successfully-submitted-ID set" in gate_7
    assert "Do not exclude orders merely because their ticker or deterministic ref_id matches" in gate_7
    assert "inspect its CURRENT broker state" in gate_7
    assert "transitioned to rejected, canceled/cancelled, failed, voided" in gate_7
    assert "missing from otherwise complete history" in gate_7
    assert "new qualifying same-day manual, broker-UI, `user`, `recurring`, or `drip` buy" in gate_7
    assert "DROP that ticker for manual review" in gate_7
    assert "exact history snapshot that Gate 6.5 consumed" in gate_7
    assert "repeat Gate 6 IN FULL for EACH placement" in gate_7
    assert "newly fetched buying-power value" in gate_7
    assert "batch boundary for the entire remaining batch" in gate_7
    assert "circuit_breakers.max_orders_per_day" in gate_7
    assert "BrokerOrder.is_buy_attempt" in gate_7
    assert "canceled/cancelled and rejected attempts consume the shared limit" in gate_7
    assert "submit no remaining orders once the fresh count has reached it" in gate_7
    assert "skip the rest rather than submitting an attempt beyond the configured limit" in gate_7
    assert "ISO week's Monday at 00:00 ET" in gate_7
    assert "fully paginate it before EACH placement" in gate_7
    assert "fetch a NEW current bid/ask" in gate_7
    assert "strictly later than the placement-history snapshot" in gate_7
    assert "equal/pre-snapshot (cached/stale), or future timestamp" in gate_7
    assert "Extract a fresh finite, positive decision price from that same quote" in gate_7
    assert "Reapply Gate 6.5's price eligibility to that fresh decision price" in gate_7
    assert "no greater than the row's saved Gate 5 decision price" in gate_7
    assert "strictly below `reference_price`" in gate_7
    assert "never submit using the older Gate 6.5 `current_price`" in gate_7
    assert "reapply Gate 6.7 in full" in gate_7
    assert "never use Gate 6.7's earlier quote" in gate_7
    assert "submit no remaining orders" in gate_7
    assert "AFTER pagination and classification finish" in gate_7
    assert "immediately before calling place_equity_order" in gate_7
    assert "pre-fetch clock result is never placement authorization" in gate_7
    assert "same fail-closed checks as dca_runner._placement_failure_reason" in gate_7
    assert "symbol, side, dollar amount, account number, or client reference" in gate_7
    assert "only AFTER all of these checks pass" in gate_7
    assert "logged_reference_bound is true" in text
    assert "reference_source is logged" in text
    # Reference discovery is delegated to the canonical tested entry point.
    assert "python3 -m scripts.prior_reference_bindings" in text
    assert "near_close_dca.load_prior_same_day_allocations_multi" in text
    assert "never re-derive those private rules" in text
    assert "scan_complete is not true" in text
    # The 09:35 stream never inherits the final stream's dynamic cap escalation.
    assert "use the NORMAL tier unconditionally for this slot" in text
    assert "Do NOT run config/budgets.json's dynamic tier selection" in text
    # Invalid/crossed quotes cannot pass through a negative or non-finite spread.
    assert "near_close_dca._quote_bid_ask()" in text
    assert "ask must be at or above the bid" in text
    # Concurrent fires use the same broker-side idempotency key.
    assert "dca-runner-0935-<YYYY-MM-DD>-<TICKER>" in text


def test_midday_live_mirror_requires_qualifying_in_window_afternoon_evidence():
    text = _midday_live_mirror_text()

    assert "is_afternoon_evidence = dca_risk.is_afternoon_scheduled_evidence(state)" in text
    assert (
        "if is_validated_evidence and window_start <= trading_day < today and scheduled_for in ('14:30', '15:30'):"
        in text
    )
    assert "if missing_pairs:" in text


def test_market_open_gate_5_hands_rich_candidates_to_gate_6_5():
    text = _market_open_live_mirror_text()
    gate_5 = text[text.index("GATE 5 - BUY LIST") : text.index("GATE 6 - BUDGET")]

    assert "Take the buy list from the document's `cands` array" in gate_5
    assert "NOT from same_day_top_up_candidates" in gate_5

    # Tokenize rather than substring-match. ``prior_low`` is a substring of
    # ``unknown_prior_low``, so a plain ``in`` check stays green even if the
    # prompt drops the standalone field -- exactly the regression this test
    # exists to catch, since a candidate row without ``prior_low`` is one
    # Gate 6.5's drop-only re-check discards (Codex review: "Match prior_low
    # as a distinct field"). ``\b`` does not fire between ``_`` and ``p``
    # (both are word characters), so ``\bprior_low\b`` cannot match inside
    # ``unknown_prior_low``.
    named_fields = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", gate_5))
    for required_field in (
        "prior_close",
        "prior_low",
        "unknown_prior_low",
        "selected_base_amount_usd",
    ):
        assert required_field in named_fields, (
            f"Gate 5 no longer names {required_field} as a distinct field; Gate 6.5 drops any candidate row missing it"
        )


def test_midday_live_mirror_opens_with_placement_authorized():
    """As of 2026-09-02 the midday slot's placement is authorized like the
    other six (Geoff's explicit direction that placement be on for every
    scheduled buy run -- docs/safeguards.md#live-execution-roster). The old
    unconditional placement-off override is gone; this pins that it stays
    gone and that the Gate 9 alert contract (three channels, real email
    delivery not a draft) is still intact."""
    text = _midday_live_mirror_text()
    assert not text.startswith("STANDING GATE 5 OVERRIDE")
    assert "Live placement (Gate 7) is OFF unconditionally" not in text
    assert "LIVE EXECUTOR. This task places real equity orders with real money" in text
    assert "1. IN-APP MESSAGE CENTER" in text
    assert "2. REPO ALERT FILE" in text
    # The third channel must call for a SENT email, not a draft. Until 2026-08-26
    # every prompt said "EMAIL DRAFT" on the since-disproven premise that the
    # connector had no send capability; that turned ~50 abort alerts into drafts
    # the operator never saw (docs/alert_digest.md). Asserting the absence of the
    # old wording is the point of the test -- a revert to "EMAIL DRAFT" here
    # silently re-breaks delivery for the one channel that outlives the session.
    assert "do not leave it sitting as a draft" in text
    assert "A draft is NOT delivery" in text
    assert "EMAIL DRAFT" not in text


def test_midday_live_mirror_treats_a_closed_window_as_a_batch_halt():
    """A time-gate failure must not inherit the ticker-local rejection carve-out."""
    text = _midday_live_mirror_text()
    assert "if ANY of these three gates fails, TERMINATE THE WHOLE REMAINING BATCH" in text
    assert "A failed time, market-day, or trading-window gate is a batch-level halt" in text
    assert "never skip that ticker and continue submitting after the window has closed" in text
    assert "a window that closes mid-batch halts the remainder exactly like a rejection does" not in text


def test_every_live_mirror_front_loads_the_sent_email_contract():
    boundary = 30_000
    # Dotfiles in this directory are generator scratch, not scheduled-task
    # prompt captures.  In particular, .template.txt deliberately contains no
    # delivery contract and must not let this candidate-prompt check pretend it
    # describes a live task.
    prompt_paths = [path for path in (PROMPTS_DIR / "live-mirror").glob("*.txt") if not path.name.startswith(".")]
    assert prompt_paths
    for path in prompt_paths:
        prefix = path.read_text(encoding="utf-8")[:boundary]
        lowered = prefix.lower()
        has_compact_contract = "TRUNCATION-SAFE SENT-EMAIL CONTRACT:" in prefix
        # The 1230ET midday prompt carries its own long-form contract instead of
        # the compact one-liner. It must state BOTH halves inside the prefix:
        # email is SENT, and a fallback draft is not delivery. Main's midday
        # rewrite (the "placement off" fallback preamble, merged via #622 and
        # extended by #1288) restated the second half as "fall back to a draft
        # only if sending is genuinely unavailable or the send fails ... record
        # that non-delivery" and pushed the older literal "draft is not
        # delivery" sentence ~140k chars past the boundary, so either phrasing
        # of that half satisfies the check -- the rule, not the sentence, is
        # what a truncated executor needs.
        has_midday_contract = "EMAIL SENT (not drafted)" in prefix and (
            "draft is not delivery" in lowered
            or ("fall back to a draft only if" in lowered and "non-delivery" in lowered)
        )
        assert has_compact_contract or has_midday_contract, (
            f"{path.name}: sent-email instructions occur after the scheduler truncation boundary"
        )


_LIVE_MIRROR_FILES = [
    "0935ET-935-am-dca-morning-buy.txt",
    "1030ET-1030-dca-recurring-buy.txt",
    "1130ET-1130-mornig-buying-run.txt",
    "1230ET-1230-pm-midday-buying-run.txt",
    "1330ET-130-pm-afternoon-buyiing-run.txt",
    "1430ET-230-pm-afternoon-buying-run.txt",
    "1530ET-330-final-buying-run.txt",
]


def test_every_live_mirror_puts_email_send_contract_before_scheduler_truncation():
    # config/scheduled_tasks.json documents a working scheduler precedent of
    # roughly 5,850 characters. Gate 9 is much later in these canonical mirror
    # bodies, so a scheduler paste can lose the corrected delivery rule unless
    # the rule is also carried in the truncation-safe prefix.
    #
    # This complements test_every_live_mirror_front_loads_the_sent_email_contract
    # above rather than repeating it: that test pins the presence of the compact
    # "TRUNCATION-SAFE SENT-EMAIL CONTRACT:" one-liner, while this one pins the
    # rule's three halves as the STANDING ALERT DELIVERY OVERRIDE states them --
    # SEND not draft, a draft is not delivery, and the fallback bookkeeping
    # (record the non-delivery back in the channels written before email was
    # attempted) -- so a prefix that keeps the heading but loses the fallback
    # rule still fails.
    for filename in _LIVE_MIRROR_FILES:
        prefix = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")[:5_850]
        normalized_prefix = prefix.casefold()
        assert "SEND (do not merely draft)" in prefix or "EMAIL SENT (not drafted)" in prefix, (
            f"{filename}: email send rule must appear before the scheduler's known truncation boundary"
        )
        # The 1230ET midday prompt carries the same two halves in its own
        # long-form fallback preamble rather than the STANDING ALERT DELIVERY
        # OVERRIDE line: "fall back to a draft only if sending is genuinely
        # unavailable or the send fails, and either way record that
        # non-delivery back in channels 1 and 2", with the literal "draft is
        # not delivery" sentence ~140k chars past the boundary (same reasoning
        # as the previous test). Either phrasing satisfies each half -- the
        # rule, not the sentence, is what a truncated executor needs.
        assert "a draft is not delivery" in normalized_prefix or "fall back to a draft only if" in normalized_prefix, (
            f"{filename}: 'a draft is not delivery' rule must appear before the truncation boundary"
        )
        assert "send attempt fails" in normalized_prefix or "the send fails" in normalized_prefix, (
            f"{filename}: the draft fallback must be conditioned on a failed send before the truncation boundary"
        )
        assert "record the non-delivery" in normalized_prefix or "record that non-delivery" in normalized_prefix, (
            f"{filename}: the non-delivery bookkeeping rule must appear before the truncation boundary"
        )


def test_1530_live_mirror_front_loads_the_alert_file_path_and_pagination_guard():
    """codex review discussion_r3888077674 ("Inline the alert-file path before
    truncation") and discussion_r3891196435 ("Keep the pagination-failure
    guard before truncation"): the 1530ET prompt's concrete Gate 9
    dca_paths.stream_root(...) call and Gate 4's pagination fail-closed rule
    both sit far past the scheduler truncation boundary, so a truncated
    executor cannot determine the durable alert destination or the
    pagination interlock rule. Both must be restated inside the
    front-loaded, truncation-safe prefix.
    """
    boundary = 30_000
    path = PROMPTS_DIR / "live-mirror" / "1530ET-330-final-buying-run.txt"
    prefix = path.read_text(encoding="utf-8")[:boundary]
    assert "TRUNCATION-SAFE ALERT FILE PATH:" in prefix
    assert "dca_paths.stream_root('ce16ba6b', 'scheduled_runs', 'alerts'" in prefix
    assert "TRUNCATION-SAFE PAGINATION GUARD:" in prefix
    assert "follow every pagination cursor/page to completion" in prefix


def test_live_order_prompts_front_load_the_checkout_selection_interlock():
    boundary = 30_000
    filenames = (
        "1030ET-1030-dca-recurring-buy.txt",
        "1130ET-1130-mornig-buying-run.txt",
        "1330ET-130-pm-afternoon-buyiing-run.txt",
        "1430ET-230-pm-afternoon-buying-run.txt",
    )
    required = (
        "ls -d /sessions/*/mnt/all-stock-dca",
        "ls -d /sessions/*/mnt/*/Git/all-stock-dca",
        "find /sessions/*/mnt -maxdepth 4 -type d -name all-stock-dca",
        "canonicalize",
        "deduplicated union",
        "exactly one",
    )
    for filename in filenames:
        prefix = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")[:boundary].lower()
        missing = [rule for rule in required if rule.lower() not in prefix]
        assert not missing, f"{filename}: checkout interlock falls after truncation boundary: {missing}"


def test_midday_live_mirror_counts_complete_afternoon_evidence_by_trading_day():
    text = _midday_live_mirror_text()
    for required in (
        "expected_pairs - observed_pairs",
        "trading_day = dca_risk._trading_day(state)",
        "window_start <= trading_day < today",
        "classification_day = classification.get('trading_day')",
        "state_day == trading_day.isoformat() == classification_day",
        "is_attributable_reconciliation",
        "dca_risk.has_attributable_deployment_date(state)",
        "deployment_evidence_states.append(state)",
        "observed_pairs.add((trading_day, scheduled_for))",
        "scheduled_for in ('14:30', '15:30')",
        "if missing_pairs:",
    ):
        assert required in text
    assert "prints exactly one scalar: the deployment figure" in text
    assert "It prints four values" not in text
    assert "deployed_by_day(deployment_evidence_states)" in text


def test_market_open_live_mirror_checks_the_union_of_all_repo_probes():
    text = _market_open_live_mirror_text()
    gate_2 = text.split("GATE 2 - REPO REACHABLE", 1)[1].split("GATE 3 - TRADING WINDOW", 1)[0] if "GATE 2 - REPO REACHABLE" in text else ""
    assert "Run ALL FOUR probes" in gate_2
    assert "DEDUPLICATED UNION" in gate_2
    assert "require exactly one surviving checkout" in gate_2
    assert "2>/dev/null | head" not in gate_2


def test_every_live_mirror_probes_outside_the_cowork_mount_layout():
    """The 2026-09-08 alert reported "no repo access" for a slot whose shell
    never ran, but the probes themselves have a narrower gap of their own:
    they only ever looked under ``/sessions/*/mnt/``. That is the Cowork mount
    layout, not the only place this checkout lives. ``config/intraday_dca.json``
    already names ``/home/user/workspace/cron_tracking`` as the live artifact
    root for the sibling executor on cron 4412c3c4, and a Claude session's own
    clone can sit at ``/home/user/all-stock-dca`` -- neither is under
    ``/sessions``, so all three original probes come back empty and Gate 2
    reports a disconnected folder for a repo that was readable the whole time.
    Probe 4 closes that, under the same union/exactly-one/config-files rules as
    the others so it cannot be a back door into an arbitrary tree.
    """
    probe_4 = "find /home/user -maxdepth 3 -type d -name all-stock-dca"
    for filename in _LIVE_MIRROR_FILES:
        text = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")
        gate_2 = _gate_section(text, "GATE 2 - REPO REACHABLE")
        assert probe_4 in gate_2, f"{filename}: Gate 2 never looks outside /sessions/*/mnt/"
        assert "FOUR probes" in gate_2, f"{filename}: Gate 2 still tells the executor to run three probes"
        # The added probe must not become an exception to the ambiguity rules
        # the other three are bound by.
        assert "same deduplicated union" in gate_2, f"{filename}: probe 4 must fold into the same union as the others"
        assert "exactly-one-surviving-checkout" in gate_2, (
            f"{filename}: probe 4 must count toward the same exactly-one rule"
        )
        assert "2>/dev/null | head" not in gate_2, f"{filename}: probe output is truncated"


def test_every_live_mirror_forbids_the_account_number_in_alerts():
    """2026-09-08 12:30: the abort alert carried the live account number in
    plain text. Nothing in any mirror forbade it -- the only redaction rule any
    of them carries is the midday prompt's SYMBOL redaction for collision
    evidence, which is a different thing entirely.

    The repo-side invariant (``test_no_account_number_literals``) greps tracked
    files, and ``dca_alerts`` redacts what it writes, but these prompts emit
    alerts by hand through connectors: the text goes to Slack, email and the
    message center without passing either guard. The live prompt bodies carry
    the account as a literal (``claude/dca-executor-prompts/README.md``), so the
    executor sees the real digits and will echo them unless told not to. The
    rule belongs in the truncation-safe prefix because every abort path emits
    an alert, including the ones that never reach Gate 9 -- as this one did not.
    """
    boundary = 30_000
    for filename in _LIVE_MIRROR_FILES:
        prefix = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")[:boundary]
        assert "TRUNCATION-SAFE SECRET RULE:" in prefix, (
            f"{filename}: the account-number rule must survive truncation, since an abort "
            "that never reaches Gate 9 still sends an alert"
        )
        assert "NEVER goes into an alert body" in prefix, f"{filename}: the rule states no prohibition"
        assert "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}" in prefix, (
            f"{filename}: the rule must name the placeholder to use instead"
        )


def test_standing_perplexity_backup_policy_covers_all_seven_slots():
    """2026-09-08: the user directed that Perplexity's backup role be
    standing, not a recurring week-by-week or same-day grant -- "Perplexity
    shouldn't be disabled anywhere permanently... Perplexity is supposed to
    back up Claude if it fails, not be disabled" -- after a pattern of four
    one-off dated overrides (Aug 17, Aug 28, Aug 31, Sep 8-11) each expired
    or had to explicitly disable the previous batch's leftover task IDs.
    This pins the permanent replacement: a non-expiring top-level policy
    naming a backup task for every one of Claude's seven scheduled slots.
    """
    data = _load("scheduled_tasks.json")
    policy = data["standing_perplexity_backup_policy"]
    assert "expires" not in json.dumps(policy).lower()

    slots = policy["authorized_slots_et"]
    expected = {
        "09:35": ("2ba19526", "4412c3c4", "09:55"),
        "10:30": ("42173154", "e028361e", "10:50"),
        "11:30": ("7be82b97", "e3590042", "11:50"),
        "12:30": ("a6239dae", "062605c6", "12:50"),
        "13:30": ("7be82b97", "54ab24d2", "13:50"),
        "14:30": ("7be82b97", "54ab24d2", "14:50"),
        "15:30": ("10ce362c", "f83b8d62", "15:45"),
    }
    assert set(slots) == set(expected)
    task_by_id = {task["id"]: task for task in data["tasks"]}
    for et, (canonical_id, backup_id, backup_fire_time) in expected.items():
        assert slots[et]["canonical_task_id"] == canonical_id, et
        assert slots[et]["backup_task_id"] == backup_id, et
        assert slots[et]["backup_fires_at_et"] == backup_fire_time, et
        assert backup_fire_time in task_by_id[backup_id]["trigger_window"], et

    # Every backup task ID the policy names must be discoverable in tasks[],
    # the same discoverability rule test_aug_28_one_time_catch_up_task_is_
    # registered_in_the_manifest already established for one-time IDs -- an
    # override-named buy task with no tasks[] entry has no status, fire
    # window, or safety contract other consumers can validate.
    task_ids = set(task_by_id)
    backup_ids = {v["backup_task_id"] for v in slots.values()}
    for backup_id in backup_ids:
        assert backup_id in task_ids, f"{backup_id} has no tasks[] entry"
    for task in data["tasks"]:
        if task["id"] in backup_ids:
            assert task["status"] == "active", task["id"]
            assert task["trades"] is True, task["id"]

    # The now-superseded week-limited override must say so, not silently
    # lapse on its own end_trading_day with no record of why that is fine.
    superseded = data["temporary_live_scheduler_overrides"][
        "authorize_2026_09_08_delayed_morning_perplexity_buy_slots_week_only"
    ]
    assert "SUPERSEDED" in superseded["notes"]
    assert "standing_perplexity_backup_policy" in superseded["notes"]


def test_standing_perplexity_backup_policy_is_mirrored_in_docs():
    """The standing policy must read as current in both operator-facing
    docs, positioned so a reader meets it before the now-superseded dated
    overrides, mirroring this file's own established "authoritative table
    first" convention (test_the_canonical_schedule_table_precedes_the_
    expired_override_records)."""
    manifest = SCHEDULE_MANIFEST.read_text(encoding="utf-8")
    assert "Standing Perplexity backup policy" in manifest
    assert "PERMANENT, does not expire" in manifest
    standing_idx = manifest.index("Standing Perplexity backup policy")
    sep_11_idx = manifest.index("Sep 8-11, 2026 (SUPERSEDED")
    assert standing_idx < sep_11_idx

    todos = (REPO_ROOT / "docs" / "dca_operational_todos.md").read_text(encoding="utf-8")
    assert "standing Perplexity backup policy" in todos
    assert "062605c6" in todos and "54ab24d2" in todos


# The 1230 mirror derives its clock as datetime.now(ZoneInfo('America/New_York'))
# in its own Gate 0/1/3/4 script and cannot hit the bug below, so it is
# deliberately outside this check.
_ZONE_DERIVING_MIRROR = "1230ET-1230-pm-midday-buying-run.txt"
_GATE1_PROSE_MIRRORS = [f for f in _LIVE_MIRROR_FILES if f != _ZONE_DERIVING_MIRROR]


def test_gate1_derives_et_by_conversion_not_by_reading_the_host_clock():
    """2026-09-08 1:30 PM: the run sampled 11:31, reported it as "11:31 AM ET",
    and aborted the slot as an off-schedule scheduler fault. 11:31 at that
    instant was UTC-06:00; New York was 1:31 PM, inside the 1:25-1:55 window.
    The slot was on time and the abort threw the day away.

    Six of the seven mirrors said only "Get the current ET time", which a host
    in another zone answers confidently and wrongly. The 1230 mirror already
    did it right (``datetime.now(ZoneInfo('America/New_York'))``); this brings
    the rest into line and makes the offset check explicit.

    Fail-closed is unchanged: an out-of-window CONVERTED time still aborts. What
    changes is the diagnosis, so a whole-hour discrepancy is reported as a clock
    problem rather than sending the operator after a scheduler that fired fine.
    """
    for filename in _GATE1_PROSE_MIRRORS:
        text = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")
        gate_1 = _gate_section(text, "GATE 1 - TIME SANITY")

        assert "datetime.now(ZoneInfo('America/New_York'))" in gate_1, (
            f"{filename}: Gate 1 must derive ET by conversion, not read the host clock"
        )
        assert "Get the current ET time by CONVERSION" in gate_1, (
            f"{filename}: Gate 1 still tells the executor to just 'get the ET time'"
        )
        # The offset check is what actually catches a mislabelled zone.
        assert "-04:00 (EDT) or -05:00 (EST) and nothing else" in gate_1, (
            f"{filename}: Gate 1 must pin the only two offsets a real ET timestamp carries"
        )
        assert "_require_aware()" in gate_1, (
            f"{filename}: Gate 1 should route the timestamp through the repo's own aware-check"
        )
        # Fail-closed must survive the change.
        assert "Still ABORT" in gate_1 and "fail-closed does not change here" in gate_1, (
            f"{filename}: Gate 1 must keep aborting on an out-of-window converted time"
        )
        # And the abort must be classified, not blamed on the scheduler by default.
        assert "PROBABLE TIMEZONE MISREAD" in gate_1, (
            f"{filename}: a whole-hour discrepancy must not be reported as a scheduler fault"
        )
        assert "print the UTC instant and the offset you actually used" in gate_1, (
            f"{filename}: the alert must carry the evidence needed to fix the clock"
        )


def test_midday_mirror_still_samples_the_zone_aware_clock():
    """The precedent the other six were brought into line with. If this ever
    regresses to a bare local clock, the fix above loses its reference point.
    """
    text = _midday_live_mirror_text()
    assert "datetime.now(ZoneInfo('America/New_York'))" in text
    assert "never the nominal 12:30 schedule time" in text


def test_every_live_mirror_gate7_classifies_a_platform_refusal():
    """2026-09-04: the 1:30 PM run computed a valid buy list (QCOM $1.00, ABBV
    $2.00, VT $1.00) and every ``place_equity_order`` came back "Blocked by
    classifier" -- a platform permission denial, not a broker answer. The 3:30
    PM run the same day aborted at Gate 7 without recording any reason at all.

    Gate 7 had no branch for it, and all three of its existing outcomes are
    wrong: it is not the ticker-local carve-out (the broker never answered, so
    nothing is proven about that ticker), it is not a repo safety failure
    (every gate through 6.7 passed), and it must not consume a
    ``max_orders_per_day`` slot, which counts attempts the broker actually
    received. The resolved ``buy_usd`` amounts also exist nowhere but that run,
    so the alert has to carry them or the operator cannot place by hand.
    """
    for filename in _LIVE_MIRROR_FILES:
        text = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")
        gate_7 = _gate_section(text, "GATE 7 - PLACE THE ORDERS")

        assert "A PLATFORM REFUSAL IS NOT A BROKER REJECTION AND NOT A GATE FAILURE" in gate_7, (
            f"{filename}: Gate 7 has no branch for a refusal that never reached the broker"
        )
        assert "Blocked by classifier" in gate_7, f"{filename}: Gate 7 never names the observed refusal"
        # Whole-batch, not the ticker-local carve-out, and bounded to one retry.
        assert "STOPS THE BATCH" in gate_7, (
            f"{filename}: a platform refusal must halt the batch, not isolate one ticker"
        )
        assert "Retry the FIRST refusal once and no more" in gate_7, (
            f"{filename}: Gate 7 must bound retries on a permission denial"
        )
        # It consumes no attempt against the daily order ceiling.
        assert "IT CONSUMES NO PLACEMENT ATTEMPT" in gate_7, (
            f"{filename}: a refused call must not count toward max_orders_per_day"
        )
        assert "already_isolated_by_broker_rejection" in gate_7, (
            f"{filename}: Gate 7 must exclude refusals from the broker-rejection set"
        )
        # The alert must carry Gate 6.5's authoritative amounts, unresized.
        assert "CARRY THE RESOLVED BUY LIST INTO THE ALERT, VERBATIM" in gate_7, (
            f"{filename}: the resolved buy list exists only in this run; the alert must carry it"
        )
        assert "must NOT be resized" in gate_7, (
            f"{filename}: Gate 6.5's buy_usd is authoritative and must not be re-derived by hand"
        )
        assert "`artifacts/` directory" in gate_7, (
            f"{filename}: Gate 7's alert must avoid the gitignored artifacts/ destination"
        )


def test_live_mirror_interlock_prefix_names_the_fourth_probe():
    # The four files carrying the compact interlock are the ones whose Gate 2
    # body sits past the truncation boundary; a truncated fire there sees only
    # this line, so a probe missing from it does not exist for that fire.
    boundary = 30_000
    filenames = (
        "1030ET-1030-dca-recurring-buy.txt",
        "1130ET-1130-mornig-buying-run.txt",
        "1330ET-130-pm-afternoon-buyiing-run.txt",
        "1430ET-230-pm-afternoon-buying-run.txt",
    )
    for filename in filenames:
        prefix = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")[:boundary]
        assert "Run ALL FOUR repo probes" in prefix, f"{filename}: interlock still says three"
        assert "find /home/user -maxdepth 3 -type d -name all-stock-dca" in prefix, (
            f"{filename}: the fourth probe falls after the truncation boundary"
        )


def _gate_section(text: str, heading: str) -> str:
    """The body of one ``GATE N - NAME`` section, up to the next gate heading.

    The seven live mirrors do not agree on which gate follows which -- the
    1230 mirror folds Gates 0/1/3/4 into one script far above Gate 2, so its
    Gate 2 is followed by ``GATE 5 - BUY LIST`` rather than the
    ``GATE 3 - TRADING WINDOW`` every other mirror has next. Slicing to the
    NEXT line-initial ``GATE `` heading keeps one helper usable for all seven
    instead of hard-coding a successor per file.
    """
    body = text.split(heading, 1)[1]
    match = re.search(r"^GATE \S", body, re.MULTILINE)
    return body[: match.start()] if match else body


# The 2026-09-08 9:35 fire never reached a gate: the sandboxed shell tool
# refused to start on four consecutive attempts, each timing out at 120-180
# seconds, so `ls -d /sessions/*/mnt/all-stock-dca` never ran. The executor had
# no branch for "the shell itself is dead", so it improvised one -- it retried
# until roughly a third of the 9:30-10:00 window was gone, then reported the
# repo-access reason ("executor ran without repo access, no buy list
# available") whose stated remedy is to reconnect the operator's folder. The
# folder was never shown to be disconnected; nothing ever looked. It also
# labelled the abort "GATE 1 - REPO REACHABLE", which is the TIME SANITY
# bucket in scripts/alert_digest.py's `Failing gate: Gate N` grouping, so the
# repeat-streak count for this gate can never form. These two tests pin the
# branch that closes all three, in the truncation-safe prefix and in Gate 2's
# own body.
_SHELL_UNAVAILABLE_REASON = "executor shell tool unavailable, repo probes could not run"


def test_every_live_mirror_front_loads_the_shell_unavailable_rule():
    boundary = 30_000
    for filename in _LIVE_MIRROR_FILES:
        prefix = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")[:boundary]
        lowered = prefix.casefold()
        assert "TRUNCATION-SAFE SHELL-UNAVAILABLE RULE:" in prefix, (
            f"{filename}: a dead shell aborts before any gate runs, so the rule "
            "must survive the scheduler truncation boundary"
        )
        assert _SHELL_UNAVAILABLE_REASON in prefix, (
            f"{filename}: the distinct shell-unavailable reason must appear "
            "before the truncation boundary, not only in Gate 2's body"
        )
        assert "abort as GATE 2" in prefix, (
            f"{filename}: the truncation-safe rule must name the canonical gate "
            "number, or the alert lands in the wrong alert_digest bucket"
        )
        assert "retry at most once" in lowered, (
            f"{filename}: the retry budget must be bounded before the truncation "
            "boundary -- an unbounded retry loop is what burned the 9:35 window"
        )
        assert "not reconnecting the folder" in lowered, (
            f"{filename}: the truncation-safe rule must reject the repo-access "
            "remedy, which sends the operator to fix a mount nothing examined"
        )
        assert "READ config/trading_window.json" in prefix, (
            f"{filename}: the truncation-safe rule must try a file read before blaming the repo"
        )
        assert "Gate 2 PASSED" in prefix, f"{filename}: a truncated fire must know a successful read clears Gate 2"
        assert "never to the checkout's artifacts/ directory" in prefix, (
            f"{filename}: the durable alert destination must survive truncation"
        )


def test_every_live_mirror_gate2_separates_a_dead_shell_from_a_missing_repo():
    for filename in _LIVE_MIRROR_FILES:
        text = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")
        gate_2 = _gate_section(text, "GATE 2 - REPO REACHABLE")
        lowered = gate_2.casefold()

        # The liveness check itself, bounded in both attempts and wall clock.
        assert "SHELL UNAVAILABLE IS ITS OWN ABORT" in gate_2, f"{filename}: Gate 2 has no dead-shell branch"
        assert "retry it AT MOST ONCE" in gate_2, f"{filename}: Gate 2 leaves the retry count open-ended"
        assert "no more than 90 seconds" in gate_2, f"{filename}: Gate 2 leaves the retry deadline open-ended"

        # The distinct reason, and an explicit refusal of the repo-access one.
        assert _SHELL_UNAVAILABLE_REASON in gate_2, f"{filename}: Gate 2 has no distinct shell-unavailable reason"
        assert "never the repo-access reason below" in gate_2, (
            f"{filename}: Gate 2 must forbid reporting a dead shell as missing repo access"
        )
        assert "restore the workspace shell tool" in lowered, (
            f"{filename}: Gate 2 must state the remedy that actually applies"
        )

        # 2026-09-08 10:30 and 11:30: both fires lost the shell but still had
        # FILE access, and both read repo files successfully before reporting
        # repo access as the failure. A read that succeeds means Gate 2 PASSED,
        # so the alert must be labelled by the gate that actually failed --
        # scripts/alert_digest.py buckets on `Failing gate: Gate N`, and a
        # mislabel files the failure under a gate that was fine.
        assert "READ one known repo file" in gate_2, (
            f"{filename}: Gate 2 must try a read-only fallback before blaming the repo"
        )
        assert "repo readable but its signal entry point cannot be executed" in gate_2, (
            f"{filename}: Gate 2 needs a distinct reason for a readable repo with no execution"
        )
        assert "LABEL THE GATE THAT ACTUALLY FAILED" in gate_2, (
            f"{filename}: Gate 2 must label by outcome, not always claim itself"
        )
        assert "GATE 2 PASSED and must NOT be reported as the failure" in gate_2, (
            f"{filename}: a successful repo read must clear Gate 2 rather than blame it"
        )
        assert "scripts/alert_digest.py" in gate_2, f"{filename}: Gate 2 must say why the gate number matters"

        # A dead shell is not a dead session: file writes can still persist
        # evidence, and the durable alert must not land in gitignored output.
        assert "no artifact is possible is narrower than a dead shell" in gate_2, (
            f"{filename}: Gate 2 must stop treating a dead shell as proof no artifact is possible"
        )
        assert "`artifacts/` directory" in gate_2, (
            f"{filename}: Gate 2 must forbid the gitignored artifacts/ alert destination"
        )
        assert "live_run_state_root" in gate_2, (
            f"{filename}: Gate 2 must source the artifact path from config when dca_paths cannot run"
        )
        assert "all three" in lowered, f"{filename}: Gate 2's dead-shell abort must still alert on every channel"

        # "Workspace still starting" is the 10:30/11:30 signature; treating it
        # as progress is what bought ~18 and ~30 attempts for nothing.
        assert "Workspace still starting" in gate_2, (
            f"{filename}: Gate 2 must count a still-starting workspace against the retry bound"
        )


# 2026-09-29 2:30 PM: the shell was ALIVE, all four probes came back empty, and
# the repo was readable through the device file bridge -- with
# mcp__remote-devices__device_bash present although Gate 2 said that tool family
# "is not present in this environment". That is neither the dead-shell case
# (where a successful read PASSES Gate 2) nor an unmounted folder, so it got the
# generic repo-access abort and its reconnect-the-folder remedy. Zero checkouts
# in the EXECUTING shell still fails the exactly-one rule, so it is a real
# Gate 2 failure with its own reason; the bridge read authorizes nothing.
_BRIDGE_ONLY_REASON = "repo readable via device bridge, not mounted in executor shell"


def test_every_live_mirror_gate2_has_a_distinct_bridge_only_abort():
    for filename in _LIVE_MIRROR_FILES:
        text = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")
        gate_2 = _gate_section(text, "GATE 2 - REPO REACHABLE")
        assert _BRIDGE_ONLY_REASON in gate_2 or "TRUNCATION-SAFE BRIDGE-SHELL RULE" in text, f"{filename}: Gate 2 has no distinct bridge-only reason"
        # scripts/alert_digest.py buckets on this exact token; unlike a dead
        # shell with a readable repo, this case genuinely fails Gate 2.
        assert "Failing gate: Gate 2" in gate_2, f"{filename}: the bridge-only abort must be labelled Gate 2"
        assert "a bridge read never authorizes placement" in gate_2 or "A bridge READ alone, with no execution, never authorizes placement" in text, (
            f"{filename}: a repo readable only through the bridge must not count as a located checkout"
        )
        # The probes, dedupe and exactly-one rule stay exactly as strict.
        assert "exactly-one-surviving-checkout" in gate_2, f"{filename}: exactly-one rule weakened"
        # Case-insensitive: the 1230 mirror words these in lower case.
        assert "deduplicated union" in gate_2.casefold(), f"{filename}: dedupe rule weakened"
        assert "four probes" in gate_2.casefold(), f"{filename}: probe set weakened"


def test_every_live_mirror_gate2_makes_bridge_tool_presence_diagnostic_only():
    for filename in _LIVE_MIRROR_FILES:
        text = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")
        gate_2 = _gate_section(text, "GATE 2 - REPO REACHABLE")
        assert "not present in this environment" not in gate_2, (
            f"{filename}: Gate 2 still states as fact that the bridge tool is absent (false on 2026-09-29)"
        )
        # Running repo code through the bridge stays forbidden.
        assert "Do NOT reach for mcp__remote-devices__device_bash" in gate_2 or "TRUNCATION-SAFE BRIDGE-SHELL RULE" in text, (
            f"{filename}: the ban on executing repo code through the bridge must survive"
        )
        assert ("presence of mcp__remote-devices__" in gate_2 and "diagnostic only" in gate_2) or "TRUNCATION-SAFE BRIDGE-SHELL RULE" in text, (
            f"{filename}: tool presence must be reported, never used to satisfy Gate 2"
        )


def test_every_live_mirror_front_loads_the_bridge_only_reason():
    boundary = 30_000
    for filename in _LIVE_MIRROR_FILES:
        prefix = (PROMPTS_DIR / "live-mirror" / filename).read_text(encoding="utf-8")[:boundary]
        assert _BRIDGE_ONLY_REASON in prefix, (
            f"{filename}: a truncated fire must know the bridge-only reason before the truncation boundary"
        )
        assert "Failing gate: Gate 2" in prefix, (
            f"{filename}: the truncation-safe rule must name the canonical gate token"
        )


def test_midday_live_mirror_normalizes_broker_notionals_before_orders():
    text = _midday_live_mirror_text()
    assert "def _to_notional(v):" in text

    start = 0
    found = 0
    while True:
        idx = text.find("r.BrokerOrder(", start)
        if idx == -1:
            break
        window = text[idx : idx + 800]
        assert "'dollar_amount': _to_notional(d['dollar_amount'])" in window
        assert "'executed_dollar_amount': _to_notional(d['executed_dollar_amount'])" in window
        found += 1
        start = idx + 1

    assert found >= 1


def test_seven_executor_prompt_files_all_exist():
    missing = [f for f in _SLOT_FILES if not (PROMPTS_DIR / f).is_file()]
    assert not missing, f"missing executor prompt files: {missing}"


def test_every_executor_prompt_opens_with_the_standing_gate5_override():
    for filename in _SLOT_FILES:
        header = _prompt_text(filename).split("\n", 1)[0]
        assert header.startswith("STANDING GATE 5 OVERRIDE"), (
            f"{filename} must open with the truncation-safe STANDING GATE 5 "
            "OVERRIDE header, or an off-schedule/ambiguous-repo/expired-window "
            "fire that gets truncated has no alert contract to fall back on"
        )


def test_gate5_override_header_defines_alert_channels_before_any_abort():
    # Codex review: "Route pre-Gate-5 fallback alerts to all required
    # channels" -- the whole point of moving the channel list to the front of
    # the header is that a truncated fire, cut off anywhere in this paragraph,
    # still has the channel list in view for whichever abort branch it stopped
    # on. If the channel list ever drifts back after an abort branch, that
    # guarantee silently breaks again.
    abort_phrase = "abort and alert (all three channels above)"
    for filename in _SLOT_FILES:
        header = _prompt_text(filename).split("\n", 1)[0]
        channel_index = header.find("(1) IN-APP MESSAGE CENTER")
        assert channel_index != -1, f"{filename}: header is missing the channel list"
        abort_indices = [match.start() for match in re.finditer(re.escape(abort_phrase), header)]
        assert abort_indices, f"{filename}: header is missing an abort branch"
        assert all(index > channel_index for index in abort_indices), (
            f"{filename}: the three-channel alert list must be defined before "
            "every abort branch in the truncation-safe header, not after one"
        )


def test_gate5_disabled_alerts_every_fire_in_header_and_gate9_of_every_prompt():
    header_phrase = "GATE5_DISABLED alerts every fire as of 2026-07-30"
    gate9_phrase = "GATE5_DISABLED: alert on ALL THREE channels above, in order, exactly like any abort or skip"
    for filename in _SLOT_FILES:
        text = _prompt_text(filename)
        assert header_phrase in text, (
            f"{filename}: truncation-safe header must state that GATE5_DISABLED "
            "alerts every fire, or a truncated fire falling back to "
            "GATE5_DISABLED has no way to know it must still alert"
        )
        assert gate9_phrase in text, (
            f"{filename}: Gate 9 must alert on GATE5_DISABLED, not treat it as "
            "the silent verdict it was before the 2026-07-30 reversal"
        )


def test_gate4_expected_overlap_classification_matches_the_documented_three_slots():
    for filename in _SLOT_FILES:
        text = _prompt_text(filename)
        classifies = "Classify GATE4_EXPECTED_OVERLAP" in text
        if filename in _SLOTS_WITH_GATE4_EXPECTED_OVERLAP:
            assert classifies, (
                f"{filename}: this slot's legacy counterpart has a committed "
                "ref_id convention (see 0-setup-card.md) and must classify "
                "GATE4_EXPECTED_OVERLAP when every order in the window carries it"
            )
        else:
            assert not classifies, (
                f"{filename}: this slot has no legacy counterpart with a "
                "committed ref_id convention to verify against and must never "
                "attempt the GATE4_EXPECTED_OVERLAP classification"
            )
            assert "does NOT attempt a GATE4_EXPECTED_OVERLAP classification" in text, (
                f"{filename}: must explicitly say it does not attempt the classification, not merely omit it"
            )


def test_gate4_expected_overlap_alerts_where_it_applies():
    # Codex review: "Continue past expected overlap to report blocked Gate 5"
    # -- GATE4_EXPECTED_OVERLAP used to end these three slots' runs silently,
    # which meant three of seven slots could go quiet on an ordinary overlap
    # day even after GATE5_DISABLED itself was fixed to alert on every fire.
    for filename in _SLOTS_WITH_GATE4_EXPECTED_OVERLAP:
        text = _prompt_text(filename)
        assert "GATE4_EXPECTED_OVERLAP: alert on ALL THREE channels above" in text, (
            f"{filename}: GATE4_EXPECTED_OVERLAP must alert on all three "
            "channels, the same as GATE5_DISABLED, not end the run silently"
        )


def test_no_prompt_still_treats_gate4_expected_overlap_as_silent():
    for filename in _SLOT_FILES:
        assert "GATE4_EXPECTED_OVERLAP: stay silent" not in _prompt_text(filename), (
            f"{filename}: GATE4_EXPECTED_OVERLAP must not be documented as "
            "silent -- it alerts on every fire it triggers, as of 2026-07-30"
        )


def test_market_open_prompt_requires_canonical_and_fresh_budget_plans():
    """The live 09:35 handoff must never place raw signal-order candidates.

    Small orders can exhaust the account-wide attempt limit without exhausting
    the dollar cap, and account headroom can change during the post-budget
    gates.  Pin both canonical planner calls so prompt edits cannot silently
    restore either unsafe shortcut.
    """
    text = (PROMPTS_DIR / "live-mirror" / "0935ET-935-am-dca-morning-buy.txt").read_text()

    assert "The canonical planner is MANDATORY" in text
    assert "NEVER fall back to the raw `cands` list" in text
    assert "must never be passed directly to `budget_snapshot()`" in text
    assert "ONLY that normalized `BrokerOrder` list" in text
    assert "call `dca_runner.plan_orders()` again" in text
    assert "amount_usd=buy_usd" in text
    assert "Do not pass survivor dictionaries to `plan_orders()`" in text
    assert "Discard the earlier plan" in text


def test_every_executor_prompt_declares_the_correct_gate1_time_window():
    for filename in _SLOT_FILES:
        text = _prompt_text(filename)
        expected = _SLOT_TIME_WINDOWS[filename]
        header, _, body = text.partition("\n\n")
        assert f"falls outside {expected}" in header, (
            f"{filename}: truncation-safe header's Gate 1 window must be "
            f"{expected!r}, matching this slot's filename, not a "
            "copy-pasted neighbor's"
        )
        assert f"It must fall inside {expected}" in body, (
            f"{filename}: Gate 1's own body section must declare the same {expected!r} window as the header"
        )


def test_no_prompt_tells_its_placement_loop_to_stop_on_the_first_rejection():
    """The Gate 7 carve-out has to reach the PLACEMENT LOOP, not just the
    standing-rules header.

    The 12:30 live mirror carried the carve-out at the top of the file and, 280
    lines later, told its conditional live-placement loop to "Stop the batch on
    the first rejection" -- and, at the far end of the same gate, that "A
    confirmed rejection ... stops further placement". Placement is OFF there
    today, which is why the mirror is excluded from
    ``_placing_prompt_texts()``; the harm lands the day a human authorizes it,
    when the executor reads contradictory instructions at the actual placement
    site and keeps the halt-on-first-symbol behavior this change exists to
    remove (Codex review, PR #786: "Apply the Gate 7 carve-out inside the 12:30
    placement loop").

    This is rounds 9/10/12/16/17/20's failure mode once more -- a rule landing
    in one place and not in the one that runs -- so it is pinned across EVERY
    prompt, placing or not, at the level of the loop rather than the header."""
    texts = {f: _prompt_text(f) for f in _SLOT_FILES}
    mirror_dir = PROMPTS_DIR / "live-mirror"
    for path in sorted(mirror_dir.glob("*.txt")):
        if path.name.startswith("."):
            continue
        texts[f"live-mirror/{path.name}"] = path.read_text(encoding="utf-8")

    forbidden = (
        "Stop the batch on the first rejection",
        "stop the batch on the first rejection",
        "A confirmed rejection, tool error, or response that proves neither "
        "success nor rejection stops further placement",
    )
    offenders = [f"{name}: {phrase!r}" for name, text in texts.items() for phrase in forbidden if phrase in text]
    assert not offenders, (
        "a placement loop still halts the whole batch on an order rejection, "
        "contradicting the Gate 7 carve-out in the same prompt: " + "; ".join(offenders)
    )


def test_the_missing_budgets_fallback_defaults_match_the_shipped_config():
    """The absent-budgets fallback uses the SHIPPED limits, and stays in step.

    No `budgets.json` at all is a supported fallback -- the loader still
    supplies the ticker universe and every other default -- so it must not
    raise. But leaving the breaker and the daily order cap at `None` disables
    them on a path that really places orders: `_process_buy_candidates_batch`
    enforces the threshold only when the field is non-null (Codex review,
    PR #786: "Keep a breaker in the missing-budgets fallback").

    Hard-coding the fallback creates a second set of limits that can drift from
    the file, which is the failure mode this PR has hit repeatedly -- so this
    pins them equal rather than trusting the comment that says they are."""
    import json
    import tempfile
    from pathlib import Path

    import dca_config

    shipped = json.loads((dca_config.get_config_dir() / "budgets.json").read_bytes())["circuit_breakers"]
    assert dca_config.DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES == shipped["max_consecutive_placement_failures"]
    assert dca_config.DEFAULT_MAX_ORDERS_PER_DAY == shipped["max_orders_per_day"]

    # ... and the fallback really resolves to them, rather than to None.
    with tempfile.TemporaryDirectory() as empty:
        cfg = dca_config.load_strategy_config(config_dir=str(Path(empty)))
        assert cfg.max_consecutive_placement_failures == dca_config.DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES
        assert cfg.max_orders_per_day == dca_config.DEFAULT_MAX_ORDERS_PER_DAY

    # An explicit override still wins -- the fallback fills in where nothing
    # else resolved the field, it does not outrank anything.
    with tempfile.TemporaryDirectory() as empty:
        cfg = dca_config.load_strategy_config(
            config_dir=str(Path(empty)),
            max_consecutive_placement_failures=1,
            max_orders_per_day=2,
        )
        assert cfg.max_consecutive_placement_failures == 1
        assert cfg.max_orders_per_day == 2

    # ... and so does an explicit `base`. Emitting the default from
    # `load_budgets` made it outrank a caller who had asked for
    # halt-on-first-failure, silently allowing two more broker calls during an
    # outage -- round 42's precedence error repeated (Codex review, PR #786:
    # "Preserve an explicit base breaker in the fallback"). It is resolved in
    # `load_strategy_config` now, the only layer that sees all three sources.
    from near_close_dca import StrategyConfig

    with tempfile.TemporaryDirectory() as empty:
        cfg = dca_config.load_strategy_config(
            config_dir=str(Path(empty)),
            base=StrategyConfig(max_consecutive_placement_failures=1, max_orders_per_day=2),
        )
        assert cfg.max_consecutive_placement_failures == 1
        assert cfg.max_orders_per_day == 2

    # SCOPED TO AN ABSENT FILE. A budgets.json that exists and omits the
    # optional order cap stays uncapped -- the documented contract for every
    # optional overlay in that module, and a deployment running deliberately
    # uncapped on a config it wrote must not be silently capped. (The breaker
    # key is not optional: an existing file omitting it raises.) My first pass
    # applied the fallback to any unresolved value and swept this case in with
    # it, which the existing test caught.
    with tempfile.TemporaryDirectory() as present:
        (Path(present) / "budgets.json").write_text(
            json.dumps(
                {
                    "circuit_breakers": {
                        "max_per_order_usd": 10.0,
                        "max_consecutive_placement_failures": 3,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert dca_config.load_strategy_config(config_dir=str(Path(present))).max_orders_per_day is None

    # The marker is deliberately not a StrategyConfig field: a standalone
    # `load_budgets` caller that splatted the overlay would raise on it, the
    # loud failure rather than a silently wrong limit.
    assert dca_config._BUDGETS_FILE_ABSENT not in StrategyConfig.__dataclass_fields__

    # And the morning/midday executors read budgets.json through their OWN
    # loader, so the fallback has to reach them too (Codex review, PR #786:
    # "Keep the morning breaker enabled without budgets").
    import morning_dca

    with tempfile.TemporaryDirectory() as empty:
        overrides = morning_dca._live_circuit_breaker_overrides(Path(empty), morning_dca.MORNING_LIVE_MODE)
        assert overrides["max_consecutive_placement_failures"] == dca_config.DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES
        assert overrides["max_orders_per_day"] == dca_config.DEFAULT_MAX_ORDERS_PER_DAY
        # Watchlist never places, and must stay off this path so a malformed
        # budgets.json cannot block the 9:45 watchlist scan artifact.
        assert morning_dca._live_circuit_breaker_overrides(Path(empty), morning_dca.MORNING_WATCHLIST_MODE) == {}

    # ...and SCOPED THE SAME WAY on that side, which the round-50 fix asserted
    # for `dca_config` and never asked of the loader it was fixing.
    # `load_max_orders_per_day` returns None for two different situations --
    # no file, and a present file omitting the optional key -- so an
    # unconditional fallback capped a deliberately-uncapped deployment at 8
    # while `load_strategy_config` left the SAME file uncapped (Codex review,
    # PR #786: "Keep an omitted morning order cap uncapped").
    with tempfile.TemporaryDirectory() as present_morning:
        (Path(present_morning) / "budgets.json").write_text(
            json.dumps(
                {
                    "circuit_breakers": {
                        "max_per_order_usd": 10.0,
                        "max_consecutive_placement_failures": 3,
                    }
                }
            ),
            encoding="utf-8",
        )
        present_overrides = morning_dca._live_circuit_breaker_overrides(
            Path(present_morning), morning_dca.MORNING_LIVE_MODE
        )
        # Absent from the overlay entirely, so MorningConfig's own None
        # survives -- uncapped, exactly as load_strategy_config resolves it.
        assert "max_orders_per_day" not in present_overrides
        assert dca_config.load_strategy_config(config_dir=str(Path(present_morning))).max_orders_per_day is None
        # The breaker is NOT optional and still resolves from the file.
        assert present_overrides["max_consecutive_placement_failures"] == 3
        # A present file that DOES set the cap is still honoured.
        (Path(present_morning) / "budgets.json").write_text(
            json.dumps(
                {
                    "circuit_breakers": {
                        "max_per_order_usd": 10.0,
                        "max_consecutive_placement_failures": 3,
                        "max_orders_per_day": 2,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert (
            morning_dca._live_circuit_breaker_overrides(Path(present_morning), morning_dca.MORNING_LIVE_MODE)[
                "max_orders_per_day"
            ]
            == 2
        )


def test_every_live_loader_falls_back_to_the_shipped_count_limits(tmp_path, monkeypatch):
    """Codex review, PR #786 ("Apply the fallback breaker in the midday loader"
    and "Apply the fallback breaker in shared stream configs").

    The shipped-limits fallback for a deployment with NO `budgets.json` reached
    `dca_config.load_strategy_config` (round 49) and `_live_circuit_breaker_
    overrides` (round 50), and neither of those is what `load_midday_config` or
    `load_stream_config` read. Both were still leaving the breaker and the
    daily order cap at their dataclass default of `None` — which means
    DISABLED, not "unset". Midday ships `live_execution_enabled=True`, so its
    breaker really was off during a broker-wide outage.

    Asserted as PARITY across all five live loaders rather than per-loader, so
    a sixth reader of this knob cannot quietly disagree with the other five.
    """
    import json as _json
    import shutil

    import dca_config
    import intraday_dca
    import market_open_dca
    import midday_dca
    import morning_dca

    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", FIXTURE_ACCOUNT_NUMBER)

    def _config_dir(budgets):
        """A full copy of the bundled config, with `budgets.json` replaced or
        omitted entirely."""
        target = tmp_path / f"cfg{len(list(tmp_path.iterdir()))}"
        target.mkdir()
        for source in CONFIG_DIR.glob("*.json"):
            if source.name == "budgets.json":
                continue
            shutil.copy(source, target / source.name)
        if budgets is not None:
            (target / "budgets.json").write_text(_json.dumps(budgets), encoding="utf-8")
        return target

    def _resolved(config_dir):
        """(breaker, order cap) as each live loader resolves them."""
        out = {
            "midday": midday_dca.load_midday_config(config_dir=str(config_dir)),
            "market_open": market_open_dca.load_market_open_config(config_dir=str(config_dir)),
            "intraday": intraday_dca.load_intraday_config(config_dir=str(config_dir)),
            "afternoon": dca_config.load_strategy_config(config_dir=str(config_dir)),
        }
        resolved = {name: (cfg.max_consecutive_placement_failures, cfg.max_orders_per_day) for name, cfg in out.items()}
        overrides = morning_dca._live_circuit_breaker_overrides(config_dir, morning_dca.MORNING_LIVE_MODE)
        resolved["morning_live"] = (
            overrides["max_consecutive_placement_failures"],
            # Absent from the overlay means the config's own None survives.
            overrides.get("max_orders_per_day"),
        )
        return resolved

    shipped = (
        dca_config.DEFAULT_MAX_CONSECUTIVE_PLACEMENT_FAILURES,
        dca_config.DEFAULT_MAX_ORDERS_PER_DAY,
    )

    # NO budgets.json at all — a supported deployment, and the one where a
    # `None` breaker silently disabled itself.
    absent_config_dir = _config_dir(None)
    absent = _resolved(absent_config_dir)
    assert set(absent) == {"midday", "market_open", "intraday", "afternoon", "morning_live"}
    for name, values in absent.items():
        assert values == shipped, (name, values)

    # Pin the exact live-risk regression independently of the cross-loader
    # parity assertion above: the bundled midday config enables execution, so
    # its absent-budgets fallback must never leave the placement breaker off.
    midday_without_budgets = midday_dca.load_midday_config(config_dir=str(absent_config_dir))
    assert midday_without_budgets.live_execution_enabled is True
    assert midday_without_budgets.max_consecutive_placement_failures == shipped[0]

    # A PRESENT file that deliberately omits the OPTIONAL order cap stays
    # uncapped everywhere — round 52's scoping, which the fallback must not
    # undo now that it reaches three more loaders. (The breaker key is not
    # optional; an existing file omitting it raises.)
    omitted = _resolved(
        _config_dir(
            {
                "circuit_breakers": {
                    "max_per_order_usd": 10.0,
                    "max_consecutive_placement_failures": 3,
                }
            }
        )
    )
    for name, values in omitted.items():
        assert values == (3, None), (name, values)

    # A present file that sets both is honoured everywhere, fallback or not.
    configured = _resolved(
        _config_dir(
            {
                "circuit_breakers": {
                    "max_per_order_usd": 10.0,
                    "max_consecutive_placement_failures": 2,
                    "max_orders_per_day": 5,
                }
            }
        )
    )
    for name, values in configured.items():
        assert values == (2, 5), (name, values)

    # `max_per_order_usd` deliberately gets NO fallback: every one of these
    # config classes defaults it to a real $10 ceiling rather than "uncapped",
    # so inventing a limit for it would be a limit nobody shipped.
    assert "max_per_order_usd" not in morning_dca.resolved_count_breakers(_config_dir(None))


def test_midday_live_mirror_resolves_one_artifact_root_format():
    """Regression for "Resolve the fuller artifact root with compact dates".

    ``MorningDCAExecutor.run_state_dir`` builds the folder as
    ``f"{now_et.strftime('%Y%m%d')}_{folder_suffix}"``, and
    ``config/midday_dca.json``'s ``live_scheduled_run_dir`` and the scheduled
    artifact contract both spell it ``YYYYMMDD_1230ET``. A hyphenated
    ``<YYYY-MM-DD>_1230ET`` passed to ``dca_paths.stream_root`` is not an
    equivalent spelling -- that function just joins the component it is given,
    so the run lands in a parallel directory the pointer, recovery and
    artifact-contract consumers do not recognise as the scheduled run.

    This only began to matter when Gate 5 started writing the producer's
    market-data and candidate files under ``$ROOT``; the truncation fallback
    already used the compact form, so the two halves of the same prompt
    disagreed about where this run's artifacts live.
    """
    text = _midday_live_mirror_text()
    assert "stream_root('midday_dca', '<YYYYMMDD>_1230ET')" in text
    assert "<YYYY-MM-DD>_1230ET" not in text


# --- PR review round: seven gaps in the per-order revalidation loop --------
#
# Automated Codex reviews on the PR that ported 0935ET's "REVALIDATE BEFORE
# EVERY ORDER" logic into the midday mirror found seven gaps present in the
# reference 0935ET prompt too (pre-existing across the whole live-placing
# roster, not introduced by that port). Six of the seven (Gate 4 never
# re-run per order, a batch's own just-confirmed fills not counted when the
# broker hasn't surfaced them yet, isolated tickers not excluded from
# replanning, the same-day top-up guard never re-run per order, and two
# ambiguous phrasings around the isolate/halt distinction) are fixed here as
# prompt text across every live-mirror prompt with a per-order loop. The
# seventh (the sizing tier/multiplier never rechecked per order) is closed
# the conservative way its own KNOWN LIMITATION note always offered as a
# valid interim alternative to the real fix (a post-anchor ladder
# re-evaluation mode, a live-sizing change deliberately not made here).


def test_every_placing_prompt_reruns_gate4_before_each_order():
    """Gap 1: nothing else in the per-order refresh detects a competing
    executor. ``plan_orders()``/the rebuilt plan only enforce budget caps, so
    a rogue signal that appears mid-batch would sail through undetected
    unless Gate 4's own classification is re-run against the same freshly
    refetched history (Codex review: "Re-run Gate 4 before every order")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Re-run Gate 4 before every order" in text, name
        assert "sole-executor" in text.lower(), name


def test_every_placing_prompt_counts_batch_own_unconfirmed_spend():
    """Gap 2: broker order-history visibility can lag behind a just-confirmed
    placement, so a later rebuild in the same batch can understate the
    batch's own already-committed spend and authorize an order past a cap it
    would otherwise have hit (Codex review: "Count the batch's own
    unconfirmed-in-history spend")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Count the batch's own unconfirmed-in-history spend" in text, name


def test_every_placing_prompt_drops_isolated_tickers_from_replan():
    """Gap 3: a ticker isolated earlier in the batch (a proven no-order
    rejection) must not keep reserving notional/order-count position in the
    replanner's running totals on every later rebuild -- that can wrongly
    skip a still-eligible later ticker (Codex review: "Drop isolated tickers
    from every replan")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Drop isolated tickers from every replan" in text, name


def test_every_placing_prompt_reruns_topup_guard_before_each_order():
    """Gap 5: another slot, a manual run, or an overlapping executor can buy
    the same ticker between Gate 6.5 and this ticker's placement; without
    re-running the same-day top-up guard per order, a stale row can still
    submit the old full amount (Codex review: "Re-run the same-day top-up
    guard before every order")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Re-run the same-day top-up guard before every order" in text, name
        assert "scripts/same_day_top_up_gate.py" in text, name


def test_every_placing_prompt_scopes_the_isolate_opening_sentence():
    """Gap 6: "ISOLATE IT ... CONTINUE placing the remaining orders" read on
    its own as broader license than the paragraph's own tighter "ISOLATE
    ONLY A FAILURE THAT PROVES NO ORDER EXISTS" rule. The opening sentence
    must now say it names the OUTCOME of that tighter test, not a separate
    rule (Codex review: "Scope the isolate-and-continue opening to the
    proven-no-order test")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Scope the isolate-and-continue opening to the proven-no-order test" in text, name
        # The tighter test must still be the one actually governing isolation.
        assert "ISOLATE ONLY A FAILURE THAT PROVES NO ORDER EXISTS" in text, name


def test_every_placing_prompt_distinguishes_window_closure_from_rejection():
    """Gap 7: "a window that closes mid-batch halts the remainder exactly
    like a rejection does" read, on its own, as if a closed window were the
    ticker-local rejection carve-out (which continues placing later
    tickers), when the intent is a whole-batch stop (Codex review:
    "Distinguish a closed window from a ticker-local rejection")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Distinguish a closed window from a ticker-local rejection" in text, name
        assert "STOP-THE-BATCH condition, not the ticker-local" in text, name


def test_gate_6_5_known_limitation_adopts_the_conservative_interim_ladder_rule():
    """Gap 4, for the five prompts that carry Gate 6.5's DROP-ONLY re-check
    and its own KNOWN LIMITATION note: the note always described a one-line
    conservative alternative to the full ladder re-evaluation (drop a row
    whose refreshed drop is shallower than the drop that sized it). That
    alternative is now adopted as a real rule rather than left hypothetical
    (Codex review: "Revalidate the ladder tier on refreshed prices"). The
    real fix -- a post-anchor ladder re-evaluation mode -- remains a
    deliberate live-sizing change, not made here."""
    five_with_limitation = [
        "0935ET-935-am-dca-morning-buy.txt",
        "1030ET-1030-dca-recurring-buy.txt",
        "1130ET-1130-mornig-buying-run.txt",
        "1330ET-130-pm-afternoon-buyiing-run.txt",
        "1430ET-230-pm-afternoon-buying-run.txt",
    ]
    mirror_dir = PROMPTS_DIR / "live-mirror"
    for name in five_with_limitation:
        text = (mirror_dir / name).read_text(encoding="utf-8")
        assert "ADOPTED INTERIM RULE" in text, name
        assert "AT LEAST AS DEEP as the drop_pct recorded on this row at Gate 5" in text, name
        assert "Revalidate the ladder tier on refreshed prices" in text, name
        # The known limitation is downgraded, not deleted: the full fix is
        # still a separate, deliberate live-sizing change.
        assert "a live-sizing change, not prompt wording, and still not made here" in text, name


def test_1530_final_buy_documents_the_ladder_tier_limitation_too():
    """The 3:30 PM final-buy stream shares gap 4 (its Gate 6.5 refresh only
    re-checks the base close/low references, never the ladder tier) but
    cannot adopt the other five prompts' one-line interim rule without a
    schema change first: its Gate 5-to-6.5 candidate record carries no
    ``drop_pct``/``multiplier`` field to compare a refreshed drop against.
    This is the one live-placing stream (docs/live_wiring.md's
    ``run_dca.run_slot()`` entry point), so the limitation must be
    documented, not silently left unrecorded (Codex review: "Revalidate the
    ladder tier on refreshed prices")."""
    mirror_dir = PROMPTS_DIR / "live-mirror"
    final_text = (mirror_dir / "1530ET-330-final-buying-run.txt").read_text(encoding="utf-8")
    assert "KNOWN LIMITATION" in final_text
    assert "Revalidate the ladder tier on refreshed prices" in final_text
    assert "no `drop_pct` or `multiplier` field" in final_text


def test_midday_live_mirror_reruns_gate4_and_topup_guard_per_order():
    """The midday mirror's per-order refresh is structured differently (a
    three-phase FIRST/SECOND/script sequence rather than 0935ET's inline
    (a)/(b)/(c) loop) but shares gaps 1, 2, 3 and 5 with the other six
    prompts. Checked separately because it is excluded from
    ``_placing_prompt_texts()`` (its Gate 7 is planning-only while placement
    stays off)."""
    text = _midday_live_mirror_text()
    assert "Re-run Gate 4 before every order" in text
    assert "Count the batch's own unconfirmed-in-history spend" in text
    assert "Drop isolated tickers from every replan" in text
    assert "Re-run the midday top-up guard for every ticker" in text
    assert "already_confirmed" in text
    assert "already_isolated" in text


def test_midday_live_mirror_keeps_broker_order_construction_singular_in_gate_7():
    """Regression guard for the gap-2/gap-3 fix above: the batch's own
    just-confirmed spend must be merged into the SAME raw order list Gate 7's
    single BrokerOrder-construction comprehension already normalizes through
    ``_to_notional``, not built via a second, unnormalized construction path
    (which broke ``test_midday_live_mirror_normalizes_broker_notionals_before_orders``
    during development of this fix). Gate 4 and Gate 6 each have their own,
    separate single construction; this only scopes to Gate 7's."""
    text = _midday_live_mirror_text()
    gate_7 = text[text.index("GATE 7 - PLACE THE ORDERS") : text.index("GATE 8 - CONFIRM")]
    assert gate_7.count("r.BrokerOrder(") == 1


# --- Second Codex review round: bugs found in the six-gap fix itself -------
#
# The fixes above were themselves reviewed by Codex on the PR that added
# them, which found real correctness bugs in the fix: the Gate 4 rescan
# would have misclassified this batch's own orders as rogue on their very
# first reappearance in refreshed history, the ladder-tier interim rule
# checked a stale Gate 6.5 quote instead of the truly fresh per-order one,
# the same-day top-up guard's ABORT verdict was wrongly treated as a
# ticker-local isolate, its logged reference wasn't refreshed per rerun, the
# midday match-by-symbol-and-amount could be fooled by an unrelated order,
# midday's top-up rerun only covered rows already flagged as a top-up, and a
# newly observed compatible-executor ticker wasn't excluded from replanning.
# These are the fixes for those findings.


def test_every_placing_prompt_excludes_own_orders_from_gate4_rescan():
    """This batch's own orders are placed_agent 'agentic' like any
    unrecognized one, so reclassifying them unchanged on a later iteration
    of the Gate 4 rescan would stop the batch on its own first successful
    order (Codex review: "Exclude this batch's orders from the repeated
    Gate 4 scan")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Exclude this batch's orders from the repeated Gate 4 scan" in text, name


def test_every_placing_prompt_excludes_newly_observed_compatible_tickers():
    """A newly observed compatible-executor order discovered by the re-run
    Gate 4 rescan must join the per-order exclusion set too, or this
    executor can submit a second same-window order for that ticker (Codex
    review: "Exclude newly observed compatible-executor tickers")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Exclude newly observed compatible-executor tickers" in text, name


# Round 10 of the same Codex review pass (still on the seven-file placing
# roster, post PR #1281's midday merge): the drop-not-shallower interim rule
# only re-checked rows sized above 1.0x, leaving the lowest ladder tier
# unvalidated even though it can itself recover past its own configured
# minimum while staying below prior_close/prior_low; and the own-batch Gate
# 4/collision exemption matched ONLY by client reference, which this
# roster's own history contract permits to come back null on a refreshed
# fetch, so a just-confirmed order missing that field would misclassify
# itself as rogue/a collision on the very next rescan. These are the fixes
# for both findings.


def test_every_placing_prompt_revalidates_the_1x_drop_tier():
    """The Gate 6.5 drop-only re-check and the Gate 7 PRICE ELIGIBILITY
    placement-boundary recheck must both apply to EVERY positive
    `multiplier`, including exactly 1.0x -- the lowest tier can recover past
    its own configured minimum drop while remaining below
    prior_close/prior_low, and the ladder may map anything shallower than
    its 1x floor to 0x/no-buy, so 1.0x is not exempt from either check
    (Codex review: "Revalidate the minimum-drop tier for 1x rows")."""
    five_with_price_eligibility = {
        name: text for name, text in _six_file_placing_prompt_texts().items() if "1530ET" not in name
    }
    assert len(five_with_price_eligibility) == 5
    for name, text in five_with_price_eligibility.items():
        assert "for a row carrying ANY positive `multiplier` (including 1.0x" in text, name
        assert "for any row carrying ANY positive `multiplier` (including 1.0x" in text, name
        assert "greater than 1.0x, additionally require" not in text, name
        assert "greater than 1.0x, that the refreshed drop_pct" not in text, name


def test_every_placing_prompt_matches_own_attempts_without_a_client_reference():
    """The own-batch Gate 4/collision exemption must also match by the
    broker's own returned order id against this batch's confirmed-order-ID
    set, not solely by client reference: `client_order_id` can come back
    null on a refreshed history fetch even for an order this executor
    itself placed, and a client-reference-only match would misclassify that
    order as rogue (or, on midday, as a collision) the instant its client
    reference stops echoing back (Codex review: "Match own attempts when
    history omits the client reference")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Match own attempts when history omits the client reference" in text, name


# Round 11 of the same Codex review pass: fresh evidence found immediately
# after round 10 landed. The own-batch exemption for a PROVEN REJECTION was
# still ticker-scoped (`already_isolated_by_broker_rejection` as a bare
# ticker set), so once this batch's own rejection isolated a ticker, ANY
# later order for that same ticker -- including a genuine order from a
# competing executor -- was also treated as this batch's own activity and
# skipped past Gate 4/collision detection. The 1530 final-buy stream's
# per-order refresh carried and checked only `prior_close`, never
# `prior_low` or the selected base, so a $2.00-tier candidate that
# recovered above its prior_low (but stayed below prior_close) still placed
# at its full $2.00-scale amount. And midday's executable replan script
# tested a confirmed order's presence in refreshed history by
# `client_order_id` alone, which the same broker-history contract permits
# to come back null, risking a duplicate synthetic row that double-counts
# spend and order-count. These are the fixes for all three findings.


def test_every_placing_prompt_matches_rejected_attempts_by_order_identity():
    """The own-batch exemption for a PROVEN REJECTION must match by that
    rejection's own order identity (broker id, or absent that, its client
    reference), not merely by ticker -- a ticker-only exemption would also
    exempt a later, different order for that same ticker, letting a
    competing executor's genuine order bypass Gate 4/collision detection
    (Codex review: "Match rejected attempts by order identity")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Match rejected attempts by order identity" in text, name


def test_1530_live_mirror_rechecks_the_base_against_prior_low():
    """The 15:30 stream's per-order refresh must carry `prior_low` and
    `selected_base_amount_usd` through its candidates file and require a
    full-base ($2.00) row to still be below `prior_low`, not just
    `prior_close` -- otherwise a candidate that recovers above its prior
    low while staying below its prior close still places at its full
    $2.00-scale amount even though only the reduced $1.00 base is still
    supported (Codex review: "Recheck the 15:30 base against the prior
    low")."""
    text = _live_mirror_placing_prompt_texts()["live-mirror/1530ET-330-final-buying-run.txt"]
    assert "Recheck the 15:30 base against the prior low" in text
    assert '"prior_low": <' in text
    assert '"selected_base_amount_usd": <' in text


def test_midday_live_mirror_detects_confirmed_orders_by_broker_id():
    """Midday's executable replan script must test whether a confirmed
    order is already visible in refreshed history by EITHER its client
    reference OR its broker-assigned order id, not by client reference
    alone -- `client_order_id` can come back null even for an order this
    batch itself placed, and testing presence by that field alone risks
    appending a duplicate synthetic row that double-counts both spend and
    the order-count attempt (Codex review: "Detect visible confirmed
    orders by broker ID")."""
    text = _midday_live_mirror_text()
    assert "Detect visible confirmed orders by broker ID" in text
    assert "'order_id': the broker-assigned order id" in text


# Round 12 of the same Codex review pass, filed against round 11's own
# fixes: the reshaped orders_week.json never carried a broker order id (so
# the round-11 by-id match could never fire), the synthetic row's 'id' key
# was blindly spread into BrokerOrder's constructor (which has no such
# field, raising TypeError -> a false ABORT), midday's per-order refresh
# never repeated FIRST's own tier/multiplier revalidation against the
# fresh per-order quote, and the 15:30 stream's newly required prior_low/
# selected_base_amount_usd fields were never rejoined back onto the same-
# day top-up guard's minimal decision output. These are the fixes.


def test_midday_live_mirror_preserves_broker_ids_in_reshaped_history():
    """The reshaped orders_week.json row shape must carry the broker's own
    order id (not just client_order_id), or the round-11 by-id own-attempt
    match can never fire against a reshaped row (Codex review: "Preserve
    broker IDs in the reshaped history")."""
    text = _midday_live_mirror_text()
    assert "Preserve broker IDs in the reshaped history" in text
    assert '"order_id": <the broker-assigned order id for this row' in text


def test_midday_live_mirror_strips_identity_fields_before_broker_order():
    """BrokerOrder has no id/order_id field, so the synthetic row's 'id'
    key must be stripped before construction -- spreading it in raises
    TypeError, which upstream code catches as an ABORT, incorrectly
    halting the batch on ordinary broker-history lag (Codex review:
    "Remove the synthetic broker ID before constructing BrokerOrder")."""
    text = _midday_live_mirror_text()
    assert "Remove the synthetic broker ID before constructing BrokerOrder" in text
    assert "_BROKER_ORDER_FIELDS" in text


def test_midday_live_mirror_rechecks_the_tier_at_every_per_order_refresh():
    """Midday's per-order refresh (step (d)) must repeat FIRST's own
    tier/multiplier revalidation against the fresh per-order quote, not
    just eligibility/base -- otherwise a candidate that recovers into a
    shallower tier mid-batch still submits at its stale, no-longer-
    supported amount (Codex review: "Recheck the midday ladder tier on
    every fresh quote")."""
    text = _midday_live_mirror_text()
    assert "Recheck the midday ladder tier on every fresh quote" in text


def test_1530_live_mirror_rejoins_the_topup_guard_result():
    """`scripts/same_day_top_up_gate.py` does not echo back prior_close,
    prior_low, or selected_base_amount_usd, so the 1530 stream must
    explicitly rejoin its decision to the original candidate row by
    ticker before Gate 7 consumes it -- otherwise the newly required
    placement-boundary base checks have no evidence to evaluate (Codex
    review: "Rejoin the 15:30 guard result with its enriched
    candidate")."""
    text = _live_mirror_placing_prompt_texts()["live-mirror/1530ET-330-final-buying-run.txt"]
    assert "Rejoin the 15:30 guard result with its enriched candidate" in text
    assert "REJOIN the gate's decisions to the producer rows" in text


# Round 13 of the same Codex review pass: the own-batch exemption matched
# any order carrying the RIGHT-LOOKING deterministic client reference, but
# that pattern is derived from public inputs (slot/day/ticker) and is not
# itself ownership evidence -- a second executor using the same naming
# convention for a ticker this batch never touched would be wrongly
# exempted. Midday's separate SECOND-phase collision scan's confirmed-order
# clause was never given the round-11 broker-ID fallback, only its
# rejected-attempt clause was. And midday's "final" pre-placement window
# recheck ran too early -- before the spread recheck and top-up guard
# rerun, which themselves consume wall-clock time. These are the fixes.


def test_every_placing_prompt_scopes_own_attempts_to_a_confirmed_ticker():
    """The own-batch exemption must not treat a bare client-reference
    pattern match as ownership proof by itself -- the deterministic
    dca-runner-<slot>-<day>-<ticker> pattern is derived from public inputs,
    so a second executor sharing the naming convention would look
    identical for a ticker this batch never attempted. The exemption must
    require the ticker to already be in this batch's own confirmed set
    (Codex review: "Exempt only client references recorded by this
    batch")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Exempt only client references recorded by this batch" in text, name


def test_midday_live_mirror_matches_confirmed_orders_by_broker_id_in_collision_scan():
    """Midday's SECOND-phase collision scan (the one right before its
    executable replan script) must also match a confirmed order by its
    broker-assigned id, not client_order_id alone -- the same
    null-client-reference gap round 11 closed for the executable script's
    own presence check, but left open here (Codex review: "Match midday
    confirmed orders by broker ID during collision scan")."""
    text = _midday_live_mirror_text()
    assert "Match midday confirmed orders by broker ID during collision scan" in text


def test_midday_live_mirror_rechecks_window_after_the_last_refresh():
    """Midday's final pre-placement market-day/time/trading-window check
    must run AFTER the spread recheck and top-up guard rerun (which
    themselves consume wall-clock time via an artifact scan, a paginated
    history fetch, a buying-power fetch, and a Gate 4 reclassification),
    not before them, or it does not actually prove the window was open
    immediately before submission (Codex review: "Move the final midday
    window check after the last refresh")."""
    text = _midday_live_mirror_text()
    assert "Move the final midday window check after the last refresh" in text


# Round 14 of the same Codex review pass: the six shared-loop prompts never
# instructed the executor to actually populate the confirmed-order record
# after each validated success (midday does this explicitly; the six-file
# family never did), leaving every rescan after the first successful order
# with an empty record to match against. And midday's initial
# GATE_7_PLAN_ONLY script sent every candidate -- including already-reduced
# same-day top-up residuals -- through the ordinary plan_orders() call,
# which refuses a residual as inconsistent_multiplier_notional before the
# per-order loop's own trusted-ceiling handling ever sees it. These are the
# fixes.


def test_every_placing_prompt_populates_the_confirmed_record_after_success():
    """Immediately after a CONFIRMED SUCCESS, the executor must populate
    this batch's confirmed-order-ID set with the ticker, its client
    reference, its broker-assigned order id, and the amount placed --
    without this, the per-order REVALIDATE loop has nothing to recognize
    this batch's own orders by on the very next rescan, and a just-placed
    order is misclassified as rogue (Codex review: "Populate the
    confirmed-order record after each success")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Populate the confirmed-order record after each success" in text, name


def test_midday_live_mirror_bypasses_the_planner_for_residuals_in_the_initial_plan():
    """Midday's initial GATE_7_PLAN_ONLY script must split out already-
    reduced same-day top-up residuals BEFORE calling plan_orders(), the
    same way the later per-order rebuild already does -- otherwise
    plan_orders()'s own multiplier-consistency check refuses a residual as
    inconsistent_multiplier_notional before it ever reaches the loop where
    the trusted-ceiling handling would have preserved it (Codex review:
    "Apply the residual bypass before forming the initial plan")."""
    text = _midday_live_mirror_text()
    assert "Apply the residual bypass before forming the initial plan" in text
    assert "residual_rows = [" in text
    assert "c for c in rows" in text


# Round 15 of the same Codex review pass, all against round 14's own midday
# fixes: the residual-detection predicate required sizing_base_usd, which
# uncalibrated/global-fallback tickers never carry; the two EARLIER
# BrokerOrder conversions (Gates 0/1/3/4 and Gate 6) were never given the
# round-12 field-filtering fix, only the later per-order one was, so the
# REQUIRED order_id column round 12 added would TypeError on literally the
# first order in history; and the initial plan's to_place list put every
# ordinary row ahead of every residual regardless of priority, diverging
# from the per-order loop's own priority-ordered walk. These are the fixes.


def test_midday_live_mirror_detects_uncalibrated_residuals_too():
    """The residual-detection predicate in the initial GATE_7_PLAN_ONLY
    script must also catch an uncalibrated/global-fallback ticker's reduced
    top-up, not just a calibrated one's -- scripts/stream_candidates
    deliberately omits sizing_base_usd for those rows even though they
    still carry selected_base_amount_usd and multiplier (Codex review:
    "Include uncalibrated top-ups in the residual bypass")."""
    text = _midday_live_mirror_text()
    assert "Include uncalibrated top-ups in the residual bypass" in text
    assert "c.get('selected_base_amount_usd')" in text


def test_midday_live_mirror_strips_broker_ids_in_every_broker_order_conversion():
    """Every BrokerOrder(**d) construction site in the midday mirror --
    not just the per-order one round 12 fixed -- must filter out the
    identity-only order_id field before construction, or the FIRST order in
    history raises TypeError and aborts the run before it ever reaches
    candidate placement (Codex review: "Strip broker IDs in every
    BrokerOrder conversion")."""
    text = _midday_live_mirror_text()
    assert "Strip broker IDs in every BrokerOrder conversion" in text
    assert text.count("_BROKER_ORDER_FIELDS = {") == 3


def test_midday_live_mirror_preserves_priority_in_the_initial_plan():
    """The initial GATE_7_PLAN_ONLY script's to_place list must preserve
    Gate 6's own priority order across ordinary and residual rows alike,
    not put every ordinary row ahead of every residual -- otherwise the
    per-order loop's own priority-ordered replan diverges from this initial
    sequence and reads it as a changed plan, stopping the batch (Codex
    review: "Preserve priority in the initial residual plan")."""
    text = _midday_live_mirror_text()
    assert "Preserve priority in the initial residual plan" in text
    assert "to_place_ordered" in text


def test_every_placing_prompt_stops_the_batch_on_topup_guard_abort():
    """An ABORT from the same-day top-up guard means broker-history coverage
    is incomplete, not that no order exists -- Gate 6.5's own fail-closed
    contract requires that to stop the batch, never isolate a single ticker
    (Codex review: "Stop the batch when the top-up guard aborts")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Stop the batch when the top-up guard aborts" in text, name


def test_every_placing_prompt_refreshes_logged_references_per_topup_rerun():
    """A stale prior_logged_reference_order_id that no longer matches the
    broker's latest qualifying order is silently ignored by
    same_day_top_up_gate.py, which falls back to a newer fill price that can
    wrongly authorize a top-up (Codex review: "Refresh logged references
    during each top-up recheck")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Refresh logged references during each top-up recheck" in text, name


def test_five_placing_prompts_recheck_ladder_tier_at_placement_boundary():
    """Gate 6.5's own drop-only re-check uses a quote that is itself stale by
    placement time in a sequential batch; the truly fresh per-order quote in
    Gate 7's PRICE ELIGIBILITY step must apply the same conservative test
    (Codex review: "Recheck the ladder tier at the placement boundary").
    Scoped to the five prompts with a PRICE ELIGIBILITY paragraph -- 1530
    has neither that paragraph nor the drop_pct/multiplier fields to check."""
    five = [
        "0935ET-935-am-dca-morning-buy.txt",
        "1030ET-1030-dca-recurring-buy.txt",
        "1130ET-1130-mornig-buying-run.txt",
        "1330ET-130-pm-afternoon-buyiing-run.txt",
        "1430ET-230-pm-afternoon-buying-run.txt",
    ]
    mirror_dir = PROMPTS_DIR / "live-mirror"
    for name in five:
        text = (mirror_dir / name).read_text(encoding="utf-8")
        assert "Recheck the ladder tier at the placement boundary" in text, name
        assert "tier-no-longer-supported" in text, name


def test_midday_live_mirror_matches_confirmed_spend_by_order_id():
    """Matching a synthetic confirmed-spend row's presence in fresh history
    by ticker-plus-amount alone is fooled by an unrelated order for the same
    ticker at the same (routinely repeated, small) notional, wrongly
    concluding the batch's own order is already visible and understating
    committed spend (Codex review: "Match confirmed spend by order ID")."""
    text = _midday_live_mirror_text()
    assert "Match confirmed spend by order ID" in text
    assert "client_order_id" in text
    # Regression guard: the matching predicate itself must key off
    # client_order_id, not (only) symbol/amount.
    gate_7 = text[text.index("GATE 7 - PLACE THE ORDERS") : text.index("GATE 8 - CONFIRM")]
    assert "_d.get('client_order_id') == _cid" in gate_7


def test_midday_live_mirror_reruns_topup_for_every_ticker_not_just_flagged():
    """Restricting the per-order top-up recheck to rows already marked
    is_same_day_topup misses the transition where a first allocation
    becomes a top-up between Gate 6.5 and placement (Codex review: "Re-run
    the midday top-up guard for every ticker")."""
    text = _midday_live_mirror_text()
    assert "Re-run the midday top-up guard for every ticker" in text
    assert "for EVERY surviving candidate, not only one" in text


def test_midday_live_mirror_reapplies_account_gates_to_final_fetch():
    """The top-up-scoped history fetch happens after Gate 4/budget were
    already rebuilt; without reapplying the caps to what that fresher fetch
    shows, spend it observed (that the earlier budget check could not have)
    would not govern placement (Codex review: "Reapply account gates to the
    final history fetch")."""
    text = _midday_live_mirror_text()
    assert "Reapply account gates to the final history fetch" in text


# --- Third Codex review round: bugs surviving the second round of fixes ----
#
# A further Codex pass on the fixes above found five more real gaps: the
# six-prompt family's per-order top-up rerun reapplied account caps in
# midday but not in the other six, it never wrote the fresh per-order quote
# into the candidate before re-running the top-up tool, the compatible-
# ticker exclusion was wrongly gated on Gate 4's ORIGINAL outcome, and
# midday's own per-order Gate 4 rescan and reshaped order-history schema
# hadn't picked up the same-batch-exemption / client_order_id fixes applied
# to the other six prompts.


def test_every_placing_prompt_reapplies_caps_after_final_topup_fetch():
    """The per-order top-up rerun's own today-scoped fetch is the LAST
    observed account state before placement; without reapplying the caps to
    it, spend it observed (that the earlier budget replan could not have)
    would not govern placement (Codex review: "Reapply account caps after
    the final history fetch")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Reapply account caps after the final history fetch" in text, name


def test_every_placing_prompt_passes_fresh_quote_to_repeated_topup_guard():
    """same_day_top_up_gate.py makes its deeper-dip decision from
    candidate['current_price'] -- refreshing every other input but leaving
    the Gate 6.5 price in place tests the wrong number (Codex review: "Pass
    the fresh quote into the repeated top-up guard")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Pass the fresh quote into the repeated top-up guard" in text, name


def test_every_placing_prompt_excludes_compatible_tickers_unconditionally():
    """A compatible executor can start placing only DURING this batch, after
    Gate 4's own original clearance was already fully clear -- gating the
    newly-observed-compatible-ticker exclusion on Gate 4's ORIGINAL outcome
    misses exactly that case (Codex review: "Handle compatible orders after
    an initially clear scan")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Handle compatible orders after an initially clear scan" in text, name
        assert "If Gate 4 originally stood down for a compatible executor, this rescan" not in text, name


def test_midday_live_mirror_excludes_own_orders_from_gate4_rescan():
    """Midday's own per-order Gate 4 rescan (SECOND phase) needed the same
    own-batch exemption added to the other six prompts -- without it, this
    executor's own placed_agent 'agentic' order reclassifies as rogue the
    moment it becomes visible in broker history (Codex review: "Exclude
    this batch from the midday Gate 4 rescan")."""
    text = _midday_live_mirror_text()
    assert "Exclude this batch from the midday Gate 4 rescan" in text


def test_midday_live_mirror_reshape_carries_client_order_id():
    """Gate 4's mandated orders_week.json row shape must include
    client_order_id, or the Gate 7 match-by-order-ID fix (round two) can
    never find a match against broker-fetched rows, silently double-
    counting this batch's own confirmed spend (Codex review: "Preserve
    client references in the reshaped history")."""
    text = _midday_live_mirror_text()
    assert "Preserve client references in the reshaped history" in text
    gate4_reshape = text[
        text.index("Gate 4's own broker fetch (for orders_week.json above)") : text.index(
            "Gate 4's own broker fetch (for orders_week.json above)"
        )
        + 2000
    ]
    assert '"client_order_id"' in gate4_reshape


def test_every_placing_prompt_reclassifies_the_final_history_fetch():
    """Fourth Codex review round: the top-up recheck's own today-scoped
    fetch was fed into the reapplied budget caps but never back through
    Gate 4's collision classification, leaving a rogue or newly-placed
    compatible order in the gap between (b)'s fetch and this one invisible
    to every check the per-order refresh had run (Codex review: "Reclassify
    the final history fetch before placement")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Reclassify the final history fetch before placement" in text, name
    midday_text = _midday_live_mirror_text()
    assert "Reclassify the final history fetch before placement" in midday_text


def test_every_placing_prompt_rejects_late_compatible_exclusions_outright():
    """Fifth Codex review round: adding a newly discovered compatible
    ticker to the exclusion set doesn't help if the final proceed check only
    asks whether the ticker is "still in the rebuilt plan" -- that plan was
    computed BEFORE the exclusion existed. The proceed condition must check
    the ticker against the freshly updated exclusion set directly (Codex
    review: "Apply late compatible exclusions before placement")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Apply late compatible exclusions before placement" in text, name
        assert "is not in the exclusion set either rebuild just updated" in text, name


def test_midday_live_mirror_preserves_gate_sole_executor_semantics():
    """Fifth Codex review round found midday's actual Gate 4 --
    dca_runner.gate_sole_executor, which ignores placed_agent entirely and
    had no compatible-executor carve-out -- out of step with the other six
    prompts' Geoff/compatible/rogue classification, and that round
    deliberately kept it that way rather than importing the other prompts'
    whole scheme piecemeal (Codex review: "Preserve the midday gate's
    collision semantics", the citation string this fix keeps unchanged).

    Fixed 2026-09-08 (standing_perplexity_backup_policy,
    claude/PERPLEXITY-CLAUDE-COORDINATION.md section 5.5): gate_sole_executor
    itself still ignores placed_agent, by design, unchanged. What changed is
    that this prompt now classifies ITS OWN raw collision list the same way
    the other six slots do in prose -- placed_agent == 'agentic' is
    compatible and does not stop the batch, anything else does -- because
    Perplexity's delayed backup task for this slot (062605c6) is now a
    standing, non-expiring part of the schedule and a genuine fill from it
    must not read as a rogue second executor.
    """
    text = _midday_live_mirror_text()
    assert "Preserve the midday gate's collision semantics" in text
    assert "no Geoff carve-out and no compatible bucket" not in text
    assert "GATE4_COMPATIBLE_STAND_DOWN" in text
    assert "already_compatible_covered" in text
    assert "--already-covered-tickers" in text
    # The compatible/rogue split must appear at all four points that
    # classify collisions: Gate 4's own script, the SECOND refresh, and
    # REVALIDATE's own rescan plus its final pre-placement reclassification.
    assert text.count("placed_agent == 'agentic'") >= 4


def test_every_placing_prompt_preserves_reduced_topups_during_replan():
    """Sixth Codex review round: plan_orders()'s ordinary multiplier-
    consistency check assumes amount_usd == base * multiplier, which a
    Gate-6.5-reduced top-up deliberately violates -- reapplying that check
    during per-order replanning would wrongly refuse a legitimate top-up
    (Codex review: "Preserve reduced top-ups during per-order replanning")."""
    for name, text in _six_file_placing_prompt_texts().items():
        assert "Preserve reduced top-ups during per-order replanning" in text, name
    # Midday's own version of this fix was itself superseded by a later
    # round (the omit-fields workaround did not actually bypass
    # plan_orders()'s consistency check) -- see
    # test_midday_live_mirror_bypasses_planner_for_reduced_topups below.
    assert "Bypass the ordinary planner for reduced midday top-ups" in _midday_live_mirror_text()


def test_every_placing_prompt_counts_isolated_attempts_during_history_lag():
    """max_orders_per_day counts REJECTED and CANCELLED attempts, not just
    fills, so an isolated ticker's own rejection needs the same broker-
    visibility-lag handling a confirmed ticker's fill already gets, or the
    order-count ceiling understates today's attempts (Codex review: "Count
    isolated placement attempts during history lag")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Count isolated placement attempts during history lag" in text, name
    # Midday's version was narrowed in a later round to only broker-rejected
    # attempts -- see test_midday_live_mirror_counts_only_broker_rejections
    # below.
    assert "Count only isolated placement attempts toward the cap" in _midday_live_mirror_text()


def test_1530_final_buy_fetches_a_current_price_before_the_topup_rerun():
    """Sixth Codex review round: unlike the other five per-order-loop
    prompts, 1530 has no PRICE ELIGIBILITY step and no earlier quote fetch
    to reuse for the per-order top-up rerun -- it needs its own explicit
    fetch at that point (Codex review: "Fetch a current price in the 15:30
    loop")."""
    mirror_dir = PROMPTS_DIR / "live-mirror"
    text = (mirror_dir / "1530ET-330-final-buying-run.txt").read_text(encoding="utf-8")
    assert "Fetch a current price in the 15:30 loop" in text
    assert "ALSO fetch THIS ticker's current quote right here" in text


# --- Seventh Codex review round: four more bugs surviving round six -------


def test_every_placing_prompt_exempts_isolated_own_attempts_from_gate4():
    """A broker-rejected order (proven no-order-exists) becomes visible in
    history carrying the same placed_agent 'agentic' this executor's
    successful orders do -- without exempting it too, the very next Gate 4
    rescan classifies it as rogue and stops the batch over an order already
    proven to hold no live risk (Codex review: "Exempt isolated own attempts
    from Gate 4 rescans")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Exempt isolated own attempts from Gate 4 rescans" in text, name
        assert "already_isolated_by_broker_rejection" in text, name
    midday_text = _midday_live_mirror_text()
    assert "Exempt isolated own attempts from Gate 4 rescans" in midday_text
    assert "already_isolated_by_broker_rejection" in midday_text


def test_every_placing_prompt_counts_only_broker_rejected_attempts():
    """Only a ticker isolated via an actual place_equity_order call that
    proved no order exists consumed a real attempt against
    max_orders_per_day -- a ticker dropped before any placement call (a
    $0.00 top-up PROCEED, a tradability failure, a price/tier eligibility
    failure) must not get a synthetic attempt row, or the cap is understated
    against a nonexistent attempt (Codex review: "Count only isolated
    placement attempts toward the cap")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Count only isolated placement attempts toward the cap" in text, name


def test_midday_live_mirror_bypasses_planner_for_reduced_topups():
    """plan_orders() ALWAYS calls _candidate_multiplier_notional_mismatch(),
    whose no-metadata branch still requires an exact base*multiplier match
    -- omitting multiplier/drop_pct/sizing_* fields (the prior round's fix)
    does not exempt a reduced top-up from it. The corrected fix excludes
    reduced-topup rows from plan_orders() entirely and verifies them by hand
    against remaining headroom (Codex review: "Bypass the ordinary planner
    for reduced midday top-ups")."""
    text = _midday_live_mirror_text()
    assert "Bypass the ordinary planner for reduced midday top-ups" in text
    assert "plan_rows" in text and "residual_rows" in text


def test_midday_live_mirror_counts_only_broker_rejections():
    text = _midday_live_mirror_text()
    assert "Count only isolated placement attempts toward the cap" in text
    assert "already_isolated_by_broker_rejection" in text


def test_1530_final_buy_rechecks_signal_eligibility_with_fresh_quote():
    """scripts/same_day_top_up_gate.py's no_prior_run path only checks
    current_price is positive, never comparing it to prior_close -- 1530's
    per-order refresh must do that comparison itself before calling the
    tool, using prior_close now carried through the candidate schema (Codex
    review: "Recheck 15:30 signal eligibility with the fresh quote")."""
    mirror_dir = PROMPTS_DIR / "live-mirror"
    text = (mirror_dir / "1530ET-330-final-buying-run.txt").read_text(encoding="utf-8")
    assert "Recheck 15:30 signal eligibility with the fresh quote" in text
    assert '"prior_close":' in text


# --- Eighth Codex review round: two more midday-specific bugs -------------


def test_midday_live_mirror_feeds_topup_guard_the_per_iteration_fresh_price():
    """Superseded by PR #1281's REVALIDATE-BEFORE-EVERY-ORDER rewrite: the
    old three-phase design ran its FIRST per-candidate price/eligibility
    refresh only ONCE per batch, so a per-order top-up-guard rerun late in
    a long batch could still be working from a stale quote (the original
    "Revalidate sizing after the late midday re-quote" gap, from a version
    of this file no longer merged). The new design's (d) PRICE ELIGIBILITY
    step re-fetches and revalidates price/eligibility/tier/base EVERY
    iteration, so there is no longer a "FIRST ran once, went stale" case to
    guard against -- the top-up guard rerun instead needs only to consume
    THAT same freshly (d)-validated price, which it does."""
    text = _midday_live_mirror_text()
    assert "this ticker's fresh `current_price` from (d) above" in text


def test_midday_live_mirror_preserves_global_priority_for_residuals():
    """Planning plan_rows through plan_orders() and residual_rows only
    afterward (as two sequential passes) breaks Gate 6's allocation-
    priority order once a cap binds -- a lower-priority ordinary row can
    consume headroom before a higher-priority reduced top-up is ever
    considered. Must merge both into one priority-ordered pass (Codex
    review: "Preserve global priority when planning residual top-ups")."""
    text = _midday_live_mirror_text()
    assert "Preserve global priority when planning residual top-ups" in text
    assert "merge the surviving `plan_rows` and ALL of `residual_rows` into ONE list" in text


# --- Ninth Codex review round: window recheck and buying-power staleness --


def test_every_placing_prompt_rechecks_window_after_per_order_refresh():
    """Steps (a)-(d) of the per-order refresh can themselves consume enough
    wall-clock time (paginated history, artifact scans, multiple quote
    fetches) to carry an iteration past the slot's window boundary even
    though (a) sampled a still-valid clock at the START of the iteration --
    the time/market-day/trading-window gates must be rechecked once more
    immediately before placement (Codex review: "Recheck the slot window
    after the per-order refresh")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Recheck the slot window after the per-order refresh" in text, name
        assert "BOTH at (a) and on THIS final recheck" in text, name


def test_every_placing_prompt_refreshes_buying_power_at_final_reapply():
    """Reapplying account caps against the final today-scoped history fetch
    is not enough if the buying-power figure used alongside it is still the
    earlier, now-stale one -- a hold, withdrawal, or other consumption not
    reflected in order history can still be missed (Codex review: "Refresh
    buying power after the final history scan")."""
    for name, text in _live_mirror_placing_prompt_texts().items():
        assert "Refresh buying power after the final history scan" in text, name
    assert "Refresh buying power after the final history scan" in _midday_live_mirror_text()


# --- The in-code approved-date fallback tracks config/trading_window.json ----
#
# 2026-09-07 diagnostic T:F02: config/trading_window.json is the authoritative
# live window (extended to 2026-10-30 on 2026-07-26 with a documented DST
# rationale), but near_close_dca.APPROVED_TEST_DATES -- the fallback a bare
# StrategyConfig() and load_strategy_config() with an absent config dir both
# use -- still ended on the original 2026-08-28, and config/scheduled_tasks.json's
# 10ce362c trigger_window text said the same. Past that date every path that
# never saw the JSON blocked with a no-trade date gate while the JSON-driven
# paths kept trading: the "some paths see the JSON and some do not" drift
# CLAUDE.md's parity invariant exists to catch.


def test_in_code_approved_dates_cover_the_configured_trading_window():
    import dca_config
    from near_close_dca import APPROVED_TEST_DATES, MARKET_HOLIDAYS

    window = _load("trading_window.json")
    start = date.fromisoformat(window["start_date"])
    end = date.fromisoformat(window["end_date"])
    configured = dca_config.load_approved_dates(REPO_ROOT / "config")["approved_test_dates"]
    assert max(APPROVED_TEST_DATES) == max(configured), (
        f"near_close_dca.APPROVED_TEST_DATES ends {max(APPROVED_TEST_DATES)} but "
        f"config/trading_window.json approves through {max(configured)}; extend the in-code fallback with the JSON"
    )
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in MARKET_HOLIDAYS:
            assert cursor in APPROVED_TEST_DATES, f"{cursor} is inside the configured window but not approved in code"
        cursor += timedelta(days=1)


def test_final_task_trigger_window_defers_to_trading_window_json():
    # The sibling live tasks (2ba19526, 7be82b97) already phrase their
    # trigger_window as "within config/trading_window.json's approved_test_dates
    # window"; the canonical 3:30 PM task must not carry a literal end date
    # that the JSON has since moved past.
    tasks = {t["id"]: t for t in _load("scheduled_tasks.json")["tasks"]}
    window = tasks["10ce362c"]["trigger_window"]
    assert "config/trading_window.json" in window
    assert "2026-08-28" not in window


def test_every_config_file_dca_config_opens_is_shipped_in_the_wheel():
    """2026-09-07 diagnostic P:PACKAGING-01. config/near_close_dca.json is read
    at runtime by dca_config.load_near_close_overrides (a missing file yields
    {} silently), but was absent from [tool.setuptools.data-files], so an
    installed wheel ignored every afternoon override a checkout honoured.
    Derived from dca_config's own _read_json call sites so the list cannot
    drift again."""
    import re
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    shipped = set(pyproject["tool"]["setuptools"]["data-files"]["share/all-stock-dca/config"])
    source = (REPO_ROOT / "dca_config.py").read_text(encoding="utf-8")
    opened = set(re.findall(r'_read_json\(config_dir / "([^"]+\.json)"\)', source))
    assert opened, "expected dca_config to open at least one config file via _read_json"
    missing = {f"config/{name}" for name in opened} - shipped
    assert not missing, f"read at runtime by dca_config but not in [tool.setuptools.data-files]: {sorted(missing)}"


def test_placed_or_in_flight_status_set_has_one_definition():
    """2026-09-07 diagnostic P:DUP-03. The deployed/order-bearing status set
    was hand-copied into five modules; every name is now an alias of
    scripts.run_classification.PLACED_OR_IN_FLIGHT_CANDIDATE_STATUSES."""
    import dca_cost_tracker
    import dca_risk
    import morning_dca
    import near_close_dca
    from scripts.run_classification import _PLACED_OR_IN_FLIGHT_STATUSES, PLACED_OR_IN_FLIGHT_CANDIDATE_STATUSES

    assert PLACED_OR_IN_FLIGHT_CANDIDATE_STATUSES == frozenset(
        {"submitted", "filled", "pending", "unconfirmed", "potentially_submitted"}
    )
    for alias in (
        _PLACED_OR_IN_FLIGHT_STATUSES,
        near_close_dca._PRIOR_ALLOCATION_STATUSES,
        morning_dca._ORDER_BEARING_CANDIDATE_STATUSES,
        dca_risk.DEPLOYED_CANDIDATE_STATUSES,
        dca_cost_tracker._ORDER_BEARING_STATUSES,
    ):
        assert alias is PLACED_OR_IN_FLIGHT_CANDIDATE_STATUSES


def test_mirror_constants_are_sourced_from_their_canonical_definitions():
    """P:DUP-04 / P:DUP-05: terminal and live broker state sets come from
    scripts/broker_reconciliation, the executed-quantity columns from
    scripts/run_classification, and the account-number shape regex in
    near_close_dca (which cannot import dca_config without a cycle) must stay
    identical to dca_config.ACCOUNT_NUMBER_SHAPE."""
    import dca_config
    import near_close_dca
    from scripts.broker_reconciliation import _LIVE_ORDER_STATES, _TERMINAL_ORDER_STATES
    from scripts.run_classification import EXECUTED_QUANTITY_COLS

    assert near_close_dca._TERMINAL_BROKER_ORDER_STATES == _TERMINAL_ORDER_STATES
    assert near_close_dca._TERMINAL_NON_FILL_STATES == _TERMINAL_ORDER_STATES
    assert near_close_dca._WORKING_BROKER_ORDER_STATES == _LIVE_ORDER_STATES
    assert near_close_dca._EXECUTED_QUANTITY_COLS is EXECUTED_QUANTITY_COLS
    assert near_close_dca._ACCOUNT_NUMBER_SHAPE.pattern == dca_config.ACCOUNT_NUMBER_SHAPE.pattern


def _task_by_id(task_id: str) -> dict:
    tasks = _load("scheduled_tasks.json")["tasks"]
    for task in tasks:
        if task.get("id") == task_id:
            return task
    raise AssertionError(f"no scheduled task with id {task_id!r}")


def test_midday_task_has_a_run_classification_artifact_contract():
    """The 12:30 midday task (a6239dae) aborted daily since 2026-09-15 with
    run_states_unclassifiable > 0 -- dca_risk could not parse the
    run_classification block some days produced. Every other trading task
    with a state_file_guarantee (see intraday_task_patch_2026_07_27) spells
    out that run_classification must be a non-null object, never a bare
    string, and that state_schema_version is required; midday's own
    state_file_guarantee never said so. An LLM free-handing the write has
    config/scheduled_tasks.json's own scheduled_run_outcomes ledger sitting
    nearby using "run_classification": "failed_start_no_evidence" as a bare
    string for an unrelated purpose -- exactly the malformed shape
    dca_risk._unclassifiable_reason() flags as "wrong JSON type". This test
    pins the fix: midday's task entry must carry the same explicit warning
    intraday's does."""
    task = _task_by_id("a6239dae")
    contract = task.get("artifact_contract")
    assert contract is not None, "a6239dae is missing an artifact_contract block"
    field_rules = contract.get("field_rules", {})
    run_classification_rule = str(field_rules.get("run_classification", ""))
    assert "never a bare string" in run_classification_rule.lower()
    required_fields = contract.get("required_top_level_run_state_fields", [])
    assert any("state_schema_version" in str(f) for f in required_fields)
    assert any("run_classification" in str(f) for f in required_fields)


def test_every_trading_task_with_a_state_file_guarantee_warns_against_a_bare_string():
    """Generalizes the midday-specific fix above: any current or future
    trading task that promises to always write run_state.json must also warn
    that run_classification is never a bare string, so this exact gap cannot
    silently reappear for a different task."""
    tasks = _load("scheduled_tasks.json")["tasks"]
    for task in tasks:
        if not task.get("trades") or "state_file_guarantee" not in task:
            continue
        contract = task.get("artifact_contract")
        assert contract is not None, (
            f"{task.get('id')} has a state_file_guarantee but no artifact_contract "
            "warning that run_classification must never be a bare string"
        )
        run_classification_rule = str(contract.get("field_rules", {}).get("run_classification", ""))
        assert "never a bare string" in run_classification_rule.lower(), task.get("id")


def test_midday_live_mirror_documents_the_run_classification_template():
    """The live 12:30 Cowork task runs on this prompt text, not on
    MiddayDCAExecutor (docs/safeguards.md notes the deployed task is still a
    stale pre-executor version) -- so the tested Python schema never actually
    reaches production. This prompt's own Gate 9 must spell out the literal
    run_classification fields (mirroring docs/midday_dca.md's already-correct
    documentation), or an LLM free-handing the write has no template to
    follow and can reproduce the exact unclassifiable-artifact incident."""
    text = _midday_live_mirror_text()
    assert "state_schema_version" in text
    assert "midday-live-v1" in text
    assert "midday_dca_live" in text
    assert "never a bare string" in text


def test_no_live_slot_gate_5_reads_a_prefetched_fallback():
    """2026-10-12: no scheduled run has a fallback data source, so no live slot's Gate 5 merges a prefetched payload
    or runs the retired launchd prefetch helper. A prompt that still does would route a session to a source the
    generators now reject as unsupported."""
    mirror_dir = PROMPTS_DIR / "live-mirror"
    for mirror in sorted(mirror_dir.glob("[0-9][0-9][0-9][0-9]ET-*.txt")):
        text = mirror.read_text(encoding="utf-8").lower()
        for retired in ("yfinance", "yahoo", "prefetch_slot", "yfinance_prefetch"):
            assert retired not in text, (mirror.name, retired)


def test_robinhood_slots_never_record_a_skipped_fetch_as_a_robinhood_source_error():
    """2026-10-05 9:35 run: the session skipped its Robinhood fetch to stay under the
    tool output cap, then listed "robinhood" in source_errors because the prompt said
    to ("or would exceed a tool output or token limit"). The run evidence carried a Robinhood data-failure code for
    an outage that never happened -- and the Notion DCA Issues database and the
    self-heal logic key on exactly those codes.

    ``source_errors`` means "I attempted this fetch and it raised". A fetch the
    session chose not to attempt is an ABSENT ``sources.robinhood`` key, which the
    generator already treats as a whole-source outage and aborts on (there is no
    fallback source).
    """
    offenders = []
    for mirror in sorted((PROMPTS_DIR / "live-mirror").glob("[0-9][0-9][0-9][0-9]ET-*.txt")):
        text = mirror.read_text(encoding="utf-8")
        gate_5 = text[text.index("GATE 5 - BUY LIST") : text.index("GATE 6 - BUDGET")]
        if 'put "robinhood" in source_errors' not in gate_5:
            continue  # Finance-primary slots (12:30, 15:30) have no Robinhood-primary rule to pin
        sentence_start = gate_5.rfind(". ", 0, gate_5.index('put "robinhood" in source_errors')) + 2
        instruction = gate_5[sentence_start : gate_5.index('put "robinhood" in source_errors')]
        if "would exceed" in instruction:
            offenders.append(f"{mirror.name}: a fetch skipped for size is still told to set source_errors")
        if 'omit "robinhood" from sources' not in gate_5:
            offenders.append(f"{mirror.name}: no rule to omit sources.robinhood for a fetch not attempted")
    assert not offenders, offenders


def test_every_live_mirror_routes_oversized_order_pages_through_the_assembler():
    """All seven live mirrors must carry the OVERSIZED ORDER PAGES rule, ahead
    of their first ``get_equity_orders`` call, invoking the assembler as a
    module and aborting as ``output_cap_exceeded`` on refusal.

    DCA Issues ``output_cap_exceeded``: the 2026-09-09 12:30 run transcribed
    an oversized page by hand and Gate 6 aborted on two disagreeing row counts.
    The rule was pasted into seven independent prompts, so one slot losing it
    (or regressing to ``python3 scripts/assemble_order_pages.py``, which fails
    with ModuleNotFoundError) would otherwise pass every other check (Copilot
    review, PR #1520: "Add regression coverage for all live prompt mirrors").
    """
    texts = _live_mirror_placing_prompt_texts()
    assert len(texts) == 7, sorted(texts)
    offenders = []
    for name, text in texts.items():
        rule = text.find("OVERSIZED ORDER PAGES")
        first_fetch = text.find("call get_equity_orders")
        if rule == -1:
            offenders.append(f"{name}: rule missing")
            continue
        if first_fetch != -1 and rule > first_fetch:
            offenders.append(f"{name}: rule appears after the first get_equity_orders call")
        paragraph = text[rule : text.find("\n", rule)]
        for required in ("python3 -m scripts.assemble_order_pages", "--error-class output_cap_exceeded"):
            if required not in paragraph:
                offenders.append(f"{name}: rule lacks {required!r}")
        if "python3 scripts/assemble_order_pages.py" in text:
            offenders.append(f"{name}: path-style invocation cannot import dca_config")
    assert not offenders, offenders


def test_every_live_mirror_gate_7_records_per_order_timing_evidence():
    """2026-10-05 9:35 run: Gate 7 aborted out of window with 26 candidates built and
    no per-order checks completed before 10:00, and nothing on disk said where the
    time went. Every order repeats a fresh clock/window recheck, a full paginated
    order-history refetch, a Gate 4 rescan and a plan rebuild before its placement
    call; whether that routine is what outruns the slot window is unproven until a
    run records it.

    Each live mirror's Gate 7 must therefore carry a PER-ORDER TIMING rule: a
    timezone-aware timestamp around each step, per order, written into the Gate 9
    evidence. It is evidence only -- the rule must say it never skips, reorders or
    loosens a check, so the "revalidate before every order" guard stays whole.
    """
    texts = _live_mirror_placing_prompt_texts()
    assert len(texts) == 7, sorted(texts)
    offenders = []
    for name, text in texts.items():
        gate_7 = text[text.index("GATE 7 - PLACE THE ORDERS") : text.index("GATE 8")]
        start = gate_7.find("PER-ORDER TIMING")
        if start == -1:
            offenders.append(f"{name}: no PER-ORDER TIMING rule in Gate 7")
            continue
        paragraph = gate_7[start : gate_7.find("\n", start)]
        # Every step of the per-order routine must be named, and the rule must ask for a
        # start AND end timestamp around each: naming the steps alone would pass a prompt
        # that yields no duration evidence for the suspected bottleneck (Copilot review).
        for required in (
            "America/New_York",
            "start and end timestamp",
            "clock/window recheck",
            "order-history refetch",
            "Gate 4 rescan",
            "plan rebuild",
            "placement call",
            "Gate 9",
            "never skips, reorders or loosens",
        ):
            if required not in paragraph:
                offenders.append(f"{name}: PER-ORDER TIMING rule lacks {required!r}")
    assert not offenders, offenders


@pytest.mark.parametrize(
    ("css", "exempt"),
    [
        ("main{box-shadow:0 4px 16px #{SHADOW}}", True),
        ("a{border:1px solid #{SHADOW2};color:red}", True),
        ("/* tint: #{ACCT} */", False),
        ("a{color:red} /* x: #{ACCT}; */", False),
        ("a:hover #{ACCT}{color:red}", False),
        ("#{ACCT},#abcdef{color:red}", False),
        ('a{content:"#{ACCT}"}', False),
        ("/* multi\n   line: #{ACCT};\n*/", False),
    ],
)
def test_css_hex_colour_exemption_is_limited_to_real_colour_values(css, exempt):
    """The eight-digit CSS exemption must not let an account-shaped run hide in a CSS
    comment, string or selector (Copilot review, PR #1597)."""
    # Digits are substituted at runtime so this file never holds an account-shaped literal
    # itself (the repo-wide scan above reads the test tree too).
    css = css.replace("{ACCT}", "1234" + "5678").replace("{SHADOW}", "0000" + "0090").replace("{SHADOW2}", "0000" + "0050")
    lines = _css_without_comments(css).splitlines()
    found = [
        _is_css_hex_colour_value(lines, i, m)
        for i, original in enumerate(css.splitlines())
        for m in _ACCOUNT_SHAPED.finditer(original)
    ]
    assert found and all(found) is exempt
