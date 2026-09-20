"""Tests for the Notion-backed self-healing loop's decision core.

Written before ``selfheal/notion_issue_loop.py`` exists, per this repository's
bug/feature workflow: the behaviour is derived from the loop contract in
``rocket-fuel/self-heal/PLAN.md`` ("Incident state machine") and from the
DCA Issues database schema, never from an implementation.

The module under test is deliberately pure. It takes already-fetched records as
flat mappings, in the shape the Notion connector's SQL mode returns, and gives
back decisions. It performs no network access, reads no credentials and holds
no Notion identifiers, so the scheduled Routine that owns connector runtime
access stays the only component that touches the wire.

Two record streams feed it:

* **Issue rows** from the DCA Issues data source: what is wrong, and how far
  the loop has got with it.
* **Run facts** aggregated from the Run Log data source: how often each error
  code has fired and when it last fired.

The second stream exists because the DCA Issues rollups (``Occurrences``,
``First seen``, ``Last seen``) are listed ``notAvailableInQuerySql`` and a
``SELECT`` naming one fails outright. Aggregating the Run Log's real
``Error code`` and ``Date`` columns yields the same facts and is the only path
that actually works over the connector.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from selfheal import notion_issue_loop as loop

# --- Fixtures ---------------------------------------------------------------

# One valid issue row in connector shape. Tests override single fields so that
# a failure names the field that actually mattered rather than a whole literal.
_VALID_ROW: dict[str, object] = {
    "url": "https://app.notion.com/p/row-market-data-unavailable",
    "Key": "market_data_unavailable",
    "Name": "Market data unavailable to session",
    "Class": "Structural",
    "Component": "Market data",
    "Severity": "Blocks slot",
    "Status": "Open",
    "Remediation": "Supply market_data_*.json upstream before the entry point runs.",
    "Retry safe": "__NO__",
    "Fix state": "New",
    "Attempts": 0,
    "PR": None,
    "Last attempt": None,
    "Fixed on": None,
}

# One aggregated Run Log row, in the shape the documented GROUP BY produces.
_VALID_FACT: dict[str, object] = {
    "code": "market_data_unavailable",
    "occurrences": 4,
    "last_seen": "2026-09-16",
}


def _mapping(**overrides: object) -> dict[str, object]:
    """Return the valid issue row with ``overrides`` applied."""
    row = dict(_VALID_ROW)
    row.update(overrides)
    return row


def _row(**overrides: object) -> "loop.IssueRow":
    """Parse the valid issue row with ``overrides`` applied."""
    return loop.parse_row(_mapping(**overrides))


def _facts(*pairs: tuple[str, int, str | None]) -> dict[str, "loop.RunFact"]:
    """Build a code-keyed fact map from ``(code, occurrences, last_seen)``."""
    result = loop.parse_run_facts(
        [{"code": code, "occurrences": count, "last_seen": seen} for code, count, seen in pairs]
    )
    assert result.errors == ()
    return dict(result.facts)


@pytest.fixture()
def config() -> "loop.LoopConfig":
    """The shipped policy, loaded from the version-controlled config file."""
    return loop.load_config()


@pytest.fixture()
def enabled(config) -> "loop.LoopConfig":
    """The shipped policy with the operator gate opened, for tick tests."""
    return loop.replace_config(config, enabled=True)


# --- parse_row: identity and required fields --------------------------------


def test_parse_row_returns_every_field_of_a_valid_row() -> None:
    row = _row()
    assert row.key == "market_data_unavailable"
    assert row.name == "Market data unavailable to session"
    assert row.page_url == "https://app.notion.com/p/row-market-data-unavailable"
    assert row.issue_class == "Structural"
    assert row.component == "Market data"
    assert row.severity == "Blocks slot"
    assert row.status == "Open"
    assert row.retry_safe is False
    assert row.fix_state == "New"
    assert row.attempts == 0
    assert row.pr_url is None
    assert row.last_attempt is None
    assert row.fixed_on is None


def test_parse_row_is_frozen_so_a_decision_cannot_mutate_its_input() -> None:
    row = _row()
    with pytest.raises(FrozenInstanceError):
        row.attempts = 99  # type: ignore[misc]


def test_issue_row_carries_no_occurrence_count() -> None:
    # Occurrences is a rollup and is notAvailableInQuerySql, so a field here
    # could only ever hold a default. Frequency comes from the Run Log facts;
    # this assertion stops it being reintroduced as a silently-zero field.
    assert not hasattr(_row(), "occurrences")


@pytest.mark.parametrize("missing", ["url", "Key", "Name", "Class", "Severity", "Status"])
def test_parse_row_rejects_a_missing_required_field(missing: str) -> None:
    mapping = _mapping()
    del mapping[missing]
    # Fail closed: a row whose identity or classification is unknown must never
    # reach admission, because every downstream gate keys off those fields.
    with pytest.raises(loop.RowError) as excinfo:
        loop.parse_row(mapping)
    assert missing in str(excinfo.value)


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_parse_row_rejects_a_blank_key(blank: object) -> None:
    with pytest.raises(loop.RowError) as excinfo:
        loop.parse_row(_mapping(Key=blank))
    assert "Key" in str(excinfo.value)


@pytest.mark.parametrize("bad_key", ["Market Data", "market-data", "_leading", "9leading", "UPPER"])
def test_parse_row_rejects_a_key_that_is_not_snake_case(bad_key: str) -> None:
    # The Key doubles as the branch suffix, the pull-request dedupe token and
    # the Run Log join column, so a value that is not a safe identifier is a
    # refusal rather than something to sanitize.
    with pytest.raises(loop.RowError) as excinfo:
        loop.parse_row(_mapping(Key=bad_key))
    assert "Key" in str(excinfo.value)


@pytest.mark.parametrize("good_key", ["a", "cap_breach", "output_cap_exceeded", "run_states_v2"])
def test_parse_row_accepts_documented_snake_case_keys(good_key: str) -> None:
    assert loop.parse_row(_mapping(Key=good_key)).key == good_key


# --- parse_row: enumerated fields -------------------------------------------


@pytest.mark.parametrize("value", ["Transient", "Structural", "Environmental", "Policy"])
def test_parse_row_accepts_every_documented_class(value: str) -> None:
    assert loop.parse_row(_mapping(Class=value)).issue_class == value


@pytest.mark.parametrize("value", ["Blocks slot", "Degrades slot", "Silent data loss", "Cosmetic"])
def test_parse_row_accepts_every_documented_severity(value: str) -> None:
    assert loop.parse_row(_mapping(Severity=value)).severity == value


@pytest.mark.parametrize("value", ["Open", "Monitoring", "Resolved"])
def test_parse_row_accepts_every_documented_status(value: str) -> None:
    assert loop.parse_row(_mapping(Status=value)).status == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("Class", "Cosmic"),
        ("Severity", "Catastrophic"),
        ("Status", "Wontfix"),
        ("Fix state", "Fixed"),
    ],
)
def test_parse_row_rejects_an_unknown_enum_value(field: str, value: str) -> None:
    # A new option added in the Notion UI must fail loudly here rather than be
    # silently bucketed into a default that could make a terminal row eligible.
    with pytest.raises(loop.RowError) as excinfo:
        loop.parse_row(_mapping(**{field: value}))
    assert field in str(excinfo.value)


@pytest.mark.parametrize("value", [None, ""])
def test_parse_row_defaults_an_unset_fix_state_to_new(value: object) -> None:
    # Every pre-existing row in the database predates this column, so an unset
    # value must mean "never attempted", not a parse failure.
    assert loop.parse_row(_mapping(**{"Fix state": value})).fix_state == loop.STATE_NEW


# --- parse_row: numbers, booleans, dates ------------------------------------


@pytest.mark.parametrize("raw,expected", [("__YES__", True), ("__NO__", False), (None, False)])
def test_parse_row_reads_the_connector_checkbox_encoding(raw: object, expected: bool) -> None:
    assert loop.parse_row(_mapping(**{"Retry safe": raw})).retry_safe is expected


@pytest.mark.parametrize("raw,expected", [(0, 0), (3, 3), (2.0, 2), (None, 0)])
def test_parse_row_normalizes_integral_numbers(raw: object, expected: int) -> None:
    # Notion returns numbers as floats over SQL, so an integral float is the
    # normal case and must not be rejected.
    assert loop.parse_row(_mapping(Attempts=raw)).attempts == expected


@pytest.mark.parametrize("raw", [1.5, -1, "3", True])
def test_parse_row_rejects_a_non_integral_or_negative_attempt_count(raw: object) -> None:
    # bool is an int subclass, so True must be rejected explicitly or it would
    # silently read as one consumed attempt.
    with pytest.raises(loop.RowError) as excinfo:
        loop.parse_row(_mapping(Attempts=raw))
    assert "Attempts" in str(excinfo.value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-05", date(2026, 9, 5)),
        ("2026-09-05T11:12:00Z", date(2026, 9, 5)),
        (None, None),
        ("", None),
    ],
)
def test_parse_row_reads_a_date_or_datetime_last_attempt(raw: object, expected: object) -> None:
    assert loop.parse_row(_mapping(**{"Last attempt": raw})).last_attempt == expected


@pytest.mark.parametrize("field,attribute", [("Last attempt", "last_attempt"), ("Fixed on", "fixed_on")])
def test_parse_row_also_accepts_the_expanded_date_column_spelling(field: str, attribute: str) -> None:
    # SQL mode exposes a date property only as "date:<name>:start"; accepting
    # both spellings keeps the Routine from reshaping rows before calling in.
    mapping = _mapping()
    del mapping[field]
    mapping[f"date:{field}:start"] = "2026-09-05"
    assert getattr(loop.parse_row(mapping), attribute) == date(2026, 9, 5)


@pytest.mark.parametrize("field", ["Last attempt", "Fixed on"])
@pytest.mark.parametrize("raw", ["05/09/2026", "2026-13-01", "yesterday"])
def test_parse_row_rejects_an_unparseable_date(field: str, raw: str) -> None:
    with pytest.raises(loop.RowError) as excinfo:
        loop.parse_row(_mapping(**{field: raw}))
    assert field in str(excinfo.value)


@pytest.mark.parametrize(
    "raw,expected", [(None, None), ("", None), ("https://x/pull/1", "https://x/pull/1")]
)
def test_parse_row_normalizes_an_absent_pr_url_to_none(raw: object, expected: object) -> None:
    assert loop.parse_row(_mapping(PR=raw)).pr_url == expected


def test_parse_row_treats_an_absent_remediation_as_empty_not_an_error() -> None:
    # An empty Remediation is an ordinary ineligibility, reported by classify();
    # raising here would let one unwritten cell abort a whole tick.
    assert loop.parse_row(_mapping(Remediation=None)).remediation == ""


# --- parse_rows: one bad row must not abort the tick ------------------------


def test_parse_rows_returns_valid_rows_and_names_each_rejected_one() -> None:
    result = loop.parse_rows(
        [
            _mapping(Key="cap_breach"),
            _mapping(Key="", url="https://app.notion.com/p/bad-row"),
            _mapping(Key="false_all_clear"),
        ]
    )
    assert [row.key for row in result.rows] == ["cap_breach", "false_all_clear"]
    assert len(result.errors) == 1
    # The message must identify the offending row so an operator can find it.
    assert "https://app.notion.com/p/bad-row" in result.errors[0]


def test_parse_rows_rejects_a_duplicate_key() -> None:
    # Two rows claiming the same Key would make attempts, state and the Run Log
    # join ambiguous, and the loop would act on whichever it saw last.
    result = loop.parse_rows([_mapping(Key="cap_breach"), _mapping(Key="cap_breach")])
    assert len(result.rows) == 1
    assert any("cap_breach" in message for message in result.errors)


# --- parse_run_facts --------------------------------------------------------


def test_parse_run_facts_keys_the_aggregate_by_error_code() -> None:
    result = loop.parse_run_facts([dict(_VALID_FACT)])
    fact = result.facts["market_data_unavailable"]
    assert fact.occurrences == 4
    assert fact.last_seen == date(2026, 9, 16)
    assert result.errors == ()


def test_parse_run_facts_skips_the_clean_run_placeholder_code() -> None:
    # The Run Log writes an em dash in Error code for a clean run. That is not
    # an issue key and must be dropped silently, not reported as malformed.
    result = loop.parse_run_facts(
        [{"code": "—", "occurrences": 9, "last_seen": "2026-09-16"}, dict(_VALID_FACT)]
    )
    assert set(result.facts) == {"market_data_unavailable"}
    assert result.errors == ()


@pytest.mark.parametrize("code", [None, "", "   "])
def test_parse_run_facts_skips_a_blank_error_code(code: object) -> None:
    result = loop.parse_run_facts([{"code": code, "occurrences": 1, "last_seen": "2026-09-16"}])
    assert result.facts == {}
    assert result.errors == ()


def test_parse_run_facts_tolerates_a_code_with_no_matching_issue_row() -> None:
    # The Run Log carries codes with no row in DCA Issues; they are simply not
    # actionable here, and must not be an error.
    facts = _facts(("already_met_or_overfilled", 4, "2026-09-17"))
    assert facts["already_met_or_overfilled"].occurrences == 4


def test_parse_run_facts_accepts_an_absent_last_seen_date() -> None:
    facts = _facts(("cap_breach", 2, None))
    assert facts["cap_breach"].last_seen is None


@pytest.mark.parametrize("occurrences", [-1, 1.5, "many", True])
def test_parse_run_facts_rejects_a_malformed_occurrence_count(occurrences: object) -> None:
    result = loop.parse_run_facts(
        [{"code": "cap_breach", "occurrences": occurrences, "last_seen": "2026-09-16"}]
    )
    assert result.facts == {}
    assert any("occurrences" in message for message in result.errors)


def test_parse_run_facts_rejects_an_unparseable_last_seen() -> None:
    result = loop.parse_run_facts(
        [{"code": "cap_breach", "occurrences": 1, "last_seen": "last Tuesday"}]
    )
    assert result.facts == {}
    assert any("last_seen" in message for message in result.errors)


def test_parse_run_facts_rejects_a_duplicated_code() -> None:
    # The documented query groups by Error code, so a duplicate means the
    # caller's SQL was wrong. Keeping the last row would hide that silently.
    result = loop.parse_run_facts(
        [
            {"code": "cap_breach", "occurrences": 1, "last_seen": "2026-09-01"},
            {"code": "cap_breach", "occurrences": 7, "last_seen": "2026-09-16"},
        ]
    )
    assert any("cap_breach" in message for message in result.errors)


# --- classify: the admission gates ------------------------------------------


def test_classify_admits_an_open_structural_row_with_a_remediation(config) -> None:
    assert loop.classify(_row(), config=config) == loop.ADMITTED


def test_classify_refuses_a_repaired_row(config) -> None:
    assert loop.classify(_row(**{"Fix state": "Repaired"}), config=config) == loop.ALREADY_REPAIRED


def test_classify_refuses_a_row_already_marked_quarantined(config) -> None:
    row = _row(**{"Fix state": "Quarantined"})
    assert loop.classify(row, config=config) == loop.ISSUE_QUARANTINED


def test_classify_quarantines_at_the_attempt_ceiling_whatever_the_stored_state(config) -> None:
    # PLAN.md: quarantined (terminal) follows from attempts >= max, so a row
    # whose stored Fix state disagrees must still be refused. Otherwise editing
    # one cell in the Notion UI would re-arm an exhausted issue.
    row = _row(**{"Fix state": "Refused", "Attempts": config.max_attempts_per_issue})
    assert loop.classify(row, config=config) == loop.ISSUE_QUARANTINED


def test_classify_admits_one_attempt_below_the_ceiling(config) -> None:
    row = _row(**{"Fix state": "Refused", "Attempts": config.max_attempts_per_issue - 1})
    assert loop.classify(row, config=config) == loop.ADMITTED


def test_classify_refuses_an_environmental_row(config) -> None:
    # Session refusals and permission-layer denials have no repository fix, so
    # spending an attempt on one is pure waste.
    assert loop.classify(_row(Class="Environmental"), config=config) == loop.CLASS_NOT_REPAIRABLE


@pytest.mark.parametrize("value", ["", "   ", None])
def test_classify_refuses_a_row_with_no_remediation(config, value: object) -> None:
    assert loop.classify(_row(Remediation=value), config=config) == loop.NO_REMEDIATION


def test_classify_refuses_a_row_awaiting_review(config) -> None:
    row = _row(**{"Fix state": "In review", "Status": "Monitoring", "Attempts": 1})
    assert loop.classify(row, config=config) == loop.AWAITING_REVIEW


@pytest.mark.parametrize("status", ["Monitoring", "Resolved"])
def test_classify_refuses_a_row_that_is_not_open(config, status: str) -> None:
    assert loop.classify(_row(Status=status), config=config) == loop.ISSUE_NOT_OPEN


def test_every_classify_result_is_a_declared_reason_code(config) -> None:
    # Mirrors selfheal.reasons: an unknown string must never be mistaken for a
    # recognized decision, so the vocabulary is enumerated, not inferred.
    for row in (
        _row(),
        _row(Class="Environmental"),
        _row(Remediation=""),
        _row(Status="Resolved"),
        _row(**{"Fix state": "In review"}),
        _row(**{"Fix state": "Repaired"}),
        _row(**{"Attempts": 3}),
    ):
        assert loop.classify(row, config=config) in loop.ALL_LOOP_REASONS


# --- rank_issues ------------------------------------------------------------


def test_rank_issues_orders_by_severity_then_occurrences_then_key() -> None:
    rows = [
        _row(Key="cosmetic_one", Severity="Cosmetic"),
        _row(Key="degrades_one", Severity="Degrades slot"),
        _row(Key="blocks_low", Severity="Blocks slot"),
        _row(Key="blocks_high", Severity="Blocks slot"),
        _row(Key="silent_one", Severity="Silent data loss"),
        _row(Key="blocks_aaa", Severity="Blocks slot"),
    ]
    facts = _facts(
        ("cosmetic_one", 99, "2026-09-16"),
        ("degrades_one", 1, "2026-09-16"),
        ("blocks_low", 2, "2026-09-16"),
        ("blocks_high", 9, "2026-09-16"),
        ("silent_one", 1, "2026-09-16"),
        ("blocks_aaa", 2, "2026-09-16"),
    )
    assert [row.key for row in loop.rank_issues(rows, facts=facts)] == [
        "blocks_high",
        "blocks_aaa",
        "blocks_low",
        "silent_one",
        "degrades_one",
        "cosmetic_one",
    ]


def test_rank_issues_treats_a_row_with_no_run_facts_as_zero_occurrences() -> None:
    rows = [_row(Key="never_logged"), _row(Key="often_logged")]
    facts = _facts(("often_logged", 3, "2026-09-16"))
    assert [row.key for row in loop.rank_issues(rows, facts=facts)] == [
        "often_logged",
        "never_logged",
    ]


def test_rank_issues_does_not_mutate_its_input() -> None:
    rows = [_row(Key="b_key", Severity="Cosmetic"), _row(Key="a_key", Severity="Blocks slot")]
    original = list(rows)
    loop.rank_issues(rows, facts={})
    assert rows == original


# --- breaker ----------------------------------------------------------------


def test_breaker_is_open_below_the_refused_threshold(config) -> None:
    rows = [
        _row(Key=f"refused_{index}", **{"Fix state": "Refused"})
        for index in range(config.breaker_refused_threshold - 1)
    ]
    assert loop.breaker_latched(rows, config=config) is False


def test_breaker_latches_at_the_refused_threshold(config) -> None:
    # The latch is the refused queue itself, so clearing it requires a human to
    # triage those rows. No elapsed time and no pipeline path can clear it.
    rows = [
        _row(Key=f"refused_{index}", **{"Fix state": "Refused"})
        for index in range(config.breaker_refused_threshold)
    ]
    assert loop.breaker_latched(rows, config=config) is True


def test_breaker_ignores_rows_in_other_states(config) -> None:
    rows = [
        _row(Key="a_key", **{"Fix state": "Quarantined"}),
        _row(Key="b_key", **{"Fix state": "In review"}),
        _row(Key="c_key", **{"Fix state": "Repaired"}),
        _row(Key="d_key", **{"Fix state": "New"}),
    ]
    assert loop.breaker_latched(rows, config=config) is False


# --- plan_tick --------------------------------------------------------------


def test_plan_tick_selects_the_top_rows_up_to_the_cap(enabled) -> None:
    rows = [
        _row(Key=f"blocks_{index}", Severity="Blocks slot")
        for index in range(enabled.max_repairs_per_tick + 2)
    ]
    facts = _facts(
        *[(f"blocks_{index}", index, "2026-09-16") for index in range(len(rows))]
    )
    plan = loop.plan_tick(rows, config=enabled, facts=facts)
    assert len(plan.selected) == enabled.max_repairs_per_tick
    # Highest occurrence count first, so the cap sheds the least frequent rows.
    assert plan.selected[0].key == f"blocks_{len(rows) - 1}"


def test_plan_tick_reports_capped_rows_distinctly_from_ineligible_ones(enabled) -> None:
    rows = [
        _row(Key=f"blocks_{index}", Severity="Blocks slot")
        for index in range(enabled.max_repairs_per_tick + 1)
    ] + [_row(Key="env_row", Class="Environmental")]
    facts = _facts(
        *[(f"blocks_{index}", index + 1, "2026-09-16") for index in range(len(rows) - 1)]
    )
    plan = loop.plan_tick(rows, config=enabled, facts=facts)
    assert plan.refusals["blocks_0"] == loop.TICK_CAP_REACHED
    assert plan.refusals["env_row"] == loop.CLASS_NOT_REPAIRABLE


def test_plan_tick_selects_nothing_when_the_loop_is_disabled(config) -> None:
    plan = loop.plan_tick([_row()], config=config, facts={})
    assert config.enabled is False
    assert plan.selected == ()
    assert plan.refusals[str(_VALID_ROW["Key"])] == loop.DISABLED


def test_plan_tick_selects_nothing_while_the_breaker_is_latched(enabled) -> None:
    rows = [
        _row(Key=f"refused_{index}", **{"Fix state": "Refused"})
        for index in range(enabled.breaker_refused_threshold)
    ] + [_row(Key="fresh_row")]
    plan = loop.plan_tick(rows, config=enabled, facts={})
    assert plan.breaker_latched is True
    assert plan.selected == ()
    assert plan.refusals["fresh_row"] == loop.BREAKER_LATCHED


def test_plan_tick_gives_every_row_exactly_one_outcome(enabled) -> None:
    rows = [
        _row(Key="admitted_row"),
        _row(Key="env_row", Class="Environmental"),
        _row(Key="done_row", **{"Fix state": "Repaired"}),
    ]
    plan = loop.plan_tick(rows, config=enabled, facts={})
    selected_keys = {row.key for row in plan.selected}
    assert selected_keys | set(plan.refusals) == {"admitted_row", "env_row", "done_row"}
    assert not (selected_keys & set(plan.refusals))


# --- plan_dispatch ----------------------------------------------------------


def test_plan_dispatch_charges_the_attempt_and_moves_the_row_into_review(enabled) -> None:
    # The attempt is charged here, not at outcome: without a durable execution
    # lease, a tick that dies after opening the pull request would otherwise
    # leave the row chargeable forever. Charging up front fails closed.
    update = loop.plan_dispatch(
        _row(), pr_url="https://github.com/o/r/pull/7", now=date(2026, 9, 20), config=enabled
    )
    assert update.attempts == 1
    assert update.fix_state == loop.STATE_IN_REVIEW
    assert update.status == "Monitoring"
    assert update.pr_url == "https://github.com/o/r/pull/7"
    assert update.last_attempt == date(2026, 9, 20)
    assert update.fixed_on is None
    assert update.needs_human is False


def test_plan_dispatch_refuses_a_row_that_is_not_admissible(enabled) -> None:
    with pytest.raises(loop.LoopStateError):
        loop.plan_dispatch(
            _row(Class="Environmental"),
            pr_url="https://github.com/o/r/pull/7",
            now=date(2026, 9, 20),
            config=enabled,
        )


@pytest.mark.parametrize("bad_url", ["", None, "not-a-url"])
def test_plan_dispatch_requires_a_real_pr_url(enabled, bad_url: object) -> None:
    with pytest.raises(loop.LoopStateError):
        loop.plan_dispatch(_row(), pr_url=bad_url, now=date(2026, 9, 20), config=enabled)


# --- plan_outcome: the state machine ----------------------------------------


def _in_review(**overrides: object) -> "loop.IssueRow":
    base: dict[str, object] = {
        "Fix state": "In review",
        "Status": "Monitoring",
        "Attempts": 1,
        "PR": "https://github.com/o/r/pull/7",
        "Last attempt": "2026-09-05",
    }
    base.update(overrides)
    return _row(**base)


def test_merged_with_main_green_repairs_the_row_and_stamps_the_fix_date(config) -> None:
    update = loop.plan_outcome(
        _in_review(), loop.PR_MERGED_MAIN_GREEN, now=date(2026, 9, 6), config=config
    )
    assert update.fix_state == loop.STATE_REPAIRED
    assert update.status == "Resolved"
    assert update.needs_human is False
    # Terminal success does not consume a further attempt.
    assert update.attempts == 1
    # Fixed on is what regression detection later compares the Run Log against,
    # so it must be stamped exactly here and nowhere else.
    assert update.fixed_on == date(2026, 9, 6)


def test_merged_with_main_red_refuses_and_escalates(config) -> None:
    # A merged fix that reddens main is the worst outcome in the machine: the
    # loop cannot undo it, so it must stop and say so.
    update = loop.plan_outcome(
        _in_review(), loop.PR_MERGED_MAIN_RED, now=date(2026, 9, 6), config=config
    )
    assert update.fix_state == loop.STATE_REFUSED
    assert update.status == "Open"
    assert update.needs_human is True
    assert update.fixed_on is None


@pytest.mark.parametrize("outcome", ["PR_CLOSED_UNMERGED", "PR_CHECKS_RED", "PR_MISSING"])
def test_a_failed_pull_request_refuses_and_reopens_the_row(config, outcome: str) -> None:
    update = loop.plan_outcome(
        _in_review(), getattr(loop, outcome), now=date(2026, 9, 6), config=config
    )
    assert update.fix_state == loop.STATE_REFUSED
    assert update.status == "Open"
    # The attempt was charged at dispatch; charging again here would halve the
    # effective ceiling.
    assert update.attempts == 1


def test_an_open_pull_request_within_the_lease_holds_the_row(config) -> None:
    update = loop.plan_outcome(
        _in_review(), loop.PR_OPEN_PENDING, now=date(2026, 9, 6), config=config
    )
    assert update.fix_state == loop.STATE_IN_REVIEW
    assert update.status == "Monitoring"
    assert update.attempts == 1
    assert update.needs_human is False


def test_an_expired_review_lease_refuses_the_row(config) -> None:
    expired_day = date(2026, 9, 5 + config.review_lease_days + 1)
    update = loop.plan_outcome(_in_review(), loop.PR_OPEN_PENDING, now=expired_day, config=config)
    assert update.fix_state == loop.STATE_REFUSED
    assert update.status == "Open"


def test_the_review_lease_is_inclusive_on_its_final_day(config) -> None:
    last_day = date(2026, 9, 5 + config.review_lease_days)
    update = loop.plan_outcome(_in_review(), loop.PR_OPEN_PENDING, now=last_day, config=config)
    assert update.fix_state == loop.STATE_IN_REVIEW


def test_an_in_review_row_with_no_recorded_attempt_date_is_treated_as_expired(config) -> None:
    # Fail closed: a row that cannot prove when its lease started must not be
    # able to occupy the In review state indefinitely.
    row = _in_review(**{"Last attempt": None})
    update = loop.plan_outcome(row, loop.PR_OPEN_PENDING, now=date(2026, 9, 6), config=config)
    assert update.fix_state == loop.STATE_REFUSED


def test_a_refusal_at_the_attempt_ceiling_quarantines_and_escalates(config) -> None:
    row = _in_review(**{"Attempts": config.max_attempts_per_issue})
    update = loop.plan_outcome(row, loop.PR_CHECKS_RED, now=date(2026, 9, 6), config=config)
    assert update.fix_state == loop.STATE_QUARANTINED
    assert update.status == "Open"
    assert update.needs_human is True


def test_a_refusal_below_the_attempt_ceiling_does_not_quarantine(config) -> None:
    row = _in_review(**{"Attempts": config.max_attempts_per_issue - 1})
    update = loop.plan_outcome(row, loop.PR_CHECKS_RED, now=date(2026, 9, 6), config=config)
    assert update.fix_state == loop.STATE_REFUSED
    assert update.needs_human is False


def test_plan_outcome_rejects_an_unknown_outcome(config) -> None:
    with pytest.raises(loop.LoopStateError):
        loop.plan_outcome(_in_review(), "PR_VIBES_OFF", now=date(2026, 9, 6), config=config)


def test_plan_outcome_carries_the_identity_needed_to_write_the_row_back(config) -> None:
    row = _in_review()
    update = loop.plan_outcome(row, loop.PR_OPEN_PENDING, now=date(2026, 9, 6), config=config)
    assert update.key == row.key
    assert update.page_url == row.page_url
    assert update.pr_url == row.pr_url


def test_every_update_carries_a_nonempty_note_for_the_notion_comment(config) -> None:
    for outcome in loop.ALL_OUTCOMES:
        update = loop.plan_outcome(_in_review(), outcome, now=date(2026, 9, 6), config=config)
        assert update.note.strip()


# --- plan_regression_check: did the fix actually hold? ----------------------


def _repaired(**overrides: object) -> "loop.IssueRow":
    base: dict[str, object] = {
        "Fix state": "Repaired",
        "Status": "Resolved",
        "Attempts": 1,
        "PR": "https://github.com/o/r/pull/7",
        "Last attempt": "2026-09-05",
        "Fixed on": "2026-09-06",
    }
    base.update(overrides)
    return _row(**base)


def test_a_repaired_row_with_no_later_run_is_left_alone(config) -> None:
    facts = _facts(("market_data_unavailable", 4, "2026-09-06"))
    assert loop.plan_regression_check(_repaired(), facts=facts, config=config) is None


def test_a_run_on_the_fix_date_itself_is_not_a_regression(config) -> None:
    # The merge and that day's runs are not ordered relative to each other, so
    # same-day recurrence is not evidence the fix failed. Strictly later only.
    facts = _facts(("market_data_unavailable", 5, "2026-09-06"))
    assert loop.plan_regression_check(_repaired(), facts=facts, config=config) is None


def test_a_run_after_the_fix_date_reopens_the_row(config) -> None:
    facts = _facts(("market_data_unavailable", 6, "2026-09-20"))
    update = loop.plan_regression_check(_repaired(), facts=facts, config=config)
    assert update is not None
    # Refused rather than New, so a regression feeds the breaker exactly as a
    # failed pull request does.
    assert update.fix_state == loop.STATE_REFUSED
    assert update.status == "Open"
    assert update.needs_human is True


def test_a_regression_preserves_the_attempt_count(config) -> None:
    # Resetting attempts would hand a half-working fix an unlimited budget.
    facts = _facts(("market_data_unavailable", 6, "2026-09-20"))
    update = loop.plan_regression_check(
        _repaired(**{"Attempts": 2}), facts=facts, config=config
    )
    assert update is not None
    assert update.attempts == 2


def test_a_regression_clears_the_fix_date(config) -> None:
    # Leaving the old stamp would make the next tick compare against a date the
    # fix no longer justifies, and the regression would be reported forever.
    facts = _facts(("market_data_unavailable", 6, "2026-09-20"))
    update = loop.plan_regression_check(_repaired(), facts=facts, config=config)
    assert update is not None
    assert update.fixed_on is None


def test_a_regression_at_the_attempt_ceiling_quarantines(config) -> None:
    facts = _facts(("market_data_unavailable", 6, "2026-09-20"))
    update = loop.plan_regression_check(
        _repaired(**{"Attempts": config.max_attempts_per_issue}), facts=facts, config=config
    )
    assert update is not None
    assert update.fix_state == loop.STATE_QUARANTINED
    assert update.needs_human is True


def test_the_regression_note_names_the_date_that_proved_it(config) -> None:
    facts = _facts(("market_data_unavailable", 6, "2026-09-20"))
    update = loop.plan_regression_check(_repaired(), facts=facts, config=config)
    assert update is not None
    assert "2026-09-20" in update.note


@pytest.mark.parametrize("state", ["New", "In review", "Refused", "Quarantined"])
def test_regression_check_applies_only_to_repaired_rows(config, state: str) -> None:
    # A row that is not claiming to be fixed has nothing to regress from, and
    # reopening it here would fight the outcome state machine.
    facts = _facts(("market_data_unavailable", 6, "2026-09-20"))
    row = _row(**{"Fix state": state, "Fixed on": None})
    assert loop.plan_regression_check(row, facts=facts, config=config) is None


def test_a_repaired_row_with_no_run_facts_at_all_is_left_alone(config) -> None:
    assert loop.plan_regression_check(_repaired(), facts={}, config=config) is None


def test_a_repaired_row_with_no_last_seen_date_is_left_alone(config) -> None:
    facts = _facts(("market_data_unavailable", 4, None))
    assert loop.plan_regression_check(_repaired(), facts=facts, config=config) is None


def test_a_repaired_row_with_no_fix_date_is_escalated_not_reopened(config) -> None:
    # Without a Fixed on stamp the loop cannot tell an old run from a new one.
    # Reopening on every tick would loop forever, so it reports the
    # inconsistency once and leaves the state alone for a human to settle.
    facts = _facts(("market_data_unavailable", 6, "2026-09-20"))
    update = loop.plan_regression_check(
        _repaired(**{"Fixed on": None}), facts=facts, config=config
    )
    assert update is not None
    assert update.fix_state == loop.STATE_REPAIRED
    assert update.needs_human is True


# --- config -----------------------------------------------------------------


def test_load_config_reads_the_shipped_document() -> None:
    config = loop.load_config()
    assert config.max_attempts_per_issue >= 1
    assert config.breaker_refused_threshold >= 1
    assert config.review_lease_days >= 1
    assert config.max_repairs_per_tick >= 1


def test_the_shipped_config_ships_disabled() -> None:
    # The repair half is operator-gated, matching config/self_heal.json. A fresh
    # checkout must never open a pull request on its own.
    assert loop.load_config().enabled is False


def _write(tmp_path, document: object) -> str:
    path = tmp_path / "notion_issue_loop.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _valid_document() -> dict[str, object]:
    return {
        "$schema": "../schemas/notion_issue_loop.schema.json",
        "enabled": False,
        "max_repairs_per_tick": 3,
        "max_attempts_per_issue": 3,
        "breaker_refused_threshold": 3,
        "review_lease_days": 14,
    }


def test_load_config_accepts_a_valid_alternate_document(tmp_path) -> None:
    config = loop.load_config(_write(tmp_path, _valid_document()))
    assert config.max_repairs_per_tick == 3


def test_load_config_rejects_a_missing_file(tmp_path) -> None:
    with pytest.raises(loop.LoopConfigError):
        loop.load_config(str(tmp_path / "absent.json"))


def test_load_config_rejects_a_non_object_document(tmp_path) -> None:
    with pytest.raises(loop.LoopConfigError):
        loop.load_config(_write(tmp_path, [1, 2, 3]))


def test_load_config_rejects_duplicate_keys(tmp_path) -> None:
    # json.loads would otherwise keep the last value, letting file order decide
    # which ceiling the loop runs under.
    path = tmp_path / "dup.json"
    path.write_text(
        '{"$schema": "../schemas/notion_issue_loop.schema.json", "enabled": false,'
        ' "max_repairs_per_tick": 1, "max_repairs_per_tick": 9,'
        ' "max_attempts_per_issue": 3, "breaker_refused_threshold": 3, "review_lease_days": 14}',
        encoding="utf-8",
    )
    with pytest.raises(loop.LoopConfigError):
        loop.load_config(str(path))


@pytest.mark.parametrize(
    "key",
    [
        "enabled",
        "max_repairs_per_tick",
        "max_attempts_per_issue",
        "breaker_refused_threshold",
        "review_lease_days",
    ],
)
def test_load_config_rejects_a_missing_required_key(tmp_path, key: str) -> None:
    document = _valid_document()
    del document[key]
    with pytest.raises(loop.LoopConfigError) as excinfo:
        loop.load_config(_write(tmp_path, document))
    assert key in str(excinfo.value)


def test_load_config_rejects_an_unknown_key(tmp_path) -> None:
    document = _valid_document()
    document["auto_merge"] = True
    with pytest.raises(loop.LoopConfigError) as excinfo:
        loop.load_config(_write(tmp_path, document))
    assert "auto_merge" in str(excinfo.value)


@pytest.mark.parametrize("value", [0, -1, 1.5, "3", True])
def test_load_config_rejects_a_malformed_integer(tmp_path, value: object) -> None:
    document = _valid_document()
    document["max_attempts_per_issue"] = value
    with pytest.raises(loop.LoopConfigError) as excinfo:
        loop.load_config(_write(tmp_path, document))
    assert "max_attempts_per_issue" in str(excinfo.value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_repairs_per_tick", 99),
        ("max_attempts_per_issue", 99),
        ("breaker_refused_threshold", 999),
        ("review_lease_days", 999),
    ],
)
def test_load_config_enforces_an_upper_bound_on_every_integer(tmp_path, key, value) -> None:
    # Without ceilings a configured value defeats boundedness as effectively as
    # a missing one -- the same rule selfheal/config.py applies.
    document = _valid_document()
    document[key] = value
    with pytest.raises(loop.LoopConfigError) as excinfo:
        loop.load_config(_write(tmp_path, document))
    assert key in str(excinfo.value)


def test_load_config_rejects_a_non_boolean_enabled(tmp_path) -> None:
    document = _valid_document()
    document["enabled"] = "yes"
    with pytest.raises(loop.LoopConfigError) as excinfo:
        loop.load_config(_write(tmp_path, document))
    assert "enabled" in str(excinfo.value)


def test_load_config_rejects_a_wrong_schema_reference(tmp_path) -> None:
    document = _valid_document()
    document["$schema"] = "../schemas/self_heal.schema.json"
    with pytest.raises(loop.LoopConfigError):
        loop.load_config(_write(tmp_path, document))


# --- module hygiene ---------------------------------------------------------


def test_the_module_holds_no_notion_identifiers_or_network_imports() -> None:
    # The loop's decision core must stay connector-agnostic and offline; the
    # Routine that owns runtime access is the only component on the wire.
    with open(loop.__file__, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("collection://", "app.notion.com", "requests", "urllib", "http"):
        assert forbidden not in text
