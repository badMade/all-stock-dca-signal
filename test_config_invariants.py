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
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from conftest import FIXTURE_ACCOUNT_NUMBER
from near_close_dca import (
    ACTIVE_TICKERS,
    BUDGET_TIERS,
    EXCLUDED_TICKERS,
    TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS,
    TIER_SPEND_THRESHOLDS_USD,
)

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = REPO_ROOT / "config"


def _load(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))


# --- config/*.json is well-formed --------------------------------------------

def test_every_config_file_parses_as_a_json_object():
    files = sorted(CONFIG_DIR.glob("*.json"))
    assert files, "config/ must not be empty"
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{path.name} must contain a JSON object"


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
        "budget tier names/order drifted between near_close_dca.BUDGET_TIERS and "
        "config/budgets.json"
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
    rollover flag -- but not the thresholds that decide WHICH tier applies. So
    TIER_SPEND_THRESHOLDS_USD / TIER_HIGH_MULTIPLIER_SIGNAL_THRESHOLDS could be
    retuned, or a select_when boundary edited, and the runtime would pick a
    different tier from the one the operator-facing config documents while the
    test claiming to keep them synchronized stayed green (Codex review, PR #219).

    select_when is prose ("planned spend > $129 or 3x-or-greater signals 6-10"),
    so this asserts on the numbers it contains rather than its wording: every
    dollar figure across the ladder must be exactly the spend thresholds, and
    every signal threshold must appear as one of the integer boundaries.
    """
    documented = _load("budgets.json")["budget_tiers"]
    clauses = [str(tier.get("select_when", "")) for tier in documented]
    assert all(clauses), "every budget tier must document its select_when boundary"

    # Parse each tier's spend INTERVAL and compare positionally, for the same
    # reason the signal intervals are compared that way below: a set comparison
    # passes as long as the same three numbers appear SOMEWHERE, so swapping the
    # elevated $129 and high-volatility $213 boundaries between tiers left this
    # green while runtime selection disagreed with the operator-facing config
    # (Codex review, PR #219).
    parsed_spend = tuple(_parse_spend_boundary(clause) for clause in clauses)
    spend_bounds = tuple(value for _, value in parsed_spend)
    # _escalation_index escalates on a STRICT ">" and the base tier is the
    # "not above the first threshold" case, so the documented operators must be
    # exactly "<=" for tier 0 and ">" for every tier above it.
    operators = tuple(op for op, _ in parsed_spend)
    assert operators[0] == "<=", (
        f"the base tier documents 'planned spend {operators[0]} ...' but tier "
        "selection treats it as the <= case"
    )
    assert set(operators[1:]) == {">"}, (
        f"escalating tiers document {sorted(set(operators[1:]))} but "
        "_escalation_index escalates on a strict '>'"
    )
    assert len(spend_bounds) == len(TIER_SPEND_THRESHOLDS_USD) + 1, (
        "the tier ladder and the spend-threshold tuple describe a different "
        "number of tiers; select_by_spend uses N thresholds to select among N+1"
    )
    # Tier 0 states the ceiling it stays under; every later tier states the
    # threshold it exceeds. Both are the SAME boundary, so all N+1 clauses must
    # agree with the N thresholds read in order.
    assert spend_bounds[0] == TIER_SPEND_THRESHOLDS_USD[0], (
        f"the first tier's select_when ceiling {spend_bounds[0]} is not "
        f"TIER_SPEND_THRESHOLDS_USD[0] {TIER_SPEND_THRESHOLDS_USD[0]}"
    )
    assert spend_bounds[1:] == tuple(TIER_SPEND_THRESHOLDS_USD), (
        "spend thresholds drifted between near_close_dca.TIER_SPEND_THRESHOLDS_USD "
        f"{tuple(TIER_SPEND_THRESHOLDS_USD)} and the per-tier select_when dollar "
        f"boundaries {spend_bounds[1:]} in config/budgets.json"
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
    for index, ((lower, _), (_, prev_upper)) in enumerate(
        zip(intervals[1:], intervals[:-1], strict=True), start=1
    ):
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
        raise AssertionError(
            f"expected one 'planned spend <op> $N' boundary in select_when: {clause!r}"
        )
    if len(re.findall(r"\$[\d.]+", clause)) != 1:
        raise AssertionError(
            f"expected exactly one dollar boundary in select_when clause: {clause!r}"
        )
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
        "the active universe drifted between near_close_dca.ACTIVE_TICKERS and "
        "config/tickers.json"
    )


def test_excluded_tickers_match_config_tickers_json():
    documented = _load("tickers.json")["excluded"]
    assert set(documented) == set(EXCLUDED_TICKERS), (
        "the exclusion list drifted between near_close_dca.EXCLUDED_TICKERS and "
        "config/tickers.json"
    )


def test_no_ticker_is_both_active_and_excluded():
    assert not (set(ACTIVE_TICKERS) & set(EXCLUDED_TICKERS))


def test_active_tickers_have_no_duplicates():
    assert len(ACTIVE_TICKERS) == len(set(ACTIVE_TICKERS))


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
_LABELED_ACCOUNT = re.compile(
    r"(?i)(?:account|acct|broker|robinhood|alpaca|rh)[^0-9A-Za-z]{0,3}(\d{6,12})"
)

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
        "110000",
        "154500",
        "152000",
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
_SYNTHETIC_ACCOUNT_VALUES = frozenset({FIXTURE_ACCOUNT_NUMBER, "123456789"})

# Files exempt from scanning entirely. Both are market-data tables whose volume
# columns are full of nine-digit share counts -- genuine data, not identifiers.
# They are named individually rather than exempting ".csv" as a class: an account
# pasted into any OTHER csv must still be caught (Codex review, PR #219).
_ALLOWED = {
    "historical_stock_data.csv",
    "analysis/2026-06-29-revised-ladder/dca_revised_ladder_backtest_decisions.csv",
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
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".svgz",
    ".pdf", ".zip", ".gz", ".tar", ".xz", ".bz2",
    ".woff", ".woff2", ".ttf", ".eot", ".pyc", ".so", ".dylib", ".dll",
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
                if _is_real_date(value) and (
                    _in_date_context(line, match) or _in_regex_pattern(line, match)
                ):
                    continue
                # A six-digit run right after "#" is a CSS hex colour, not an
                # account. Length-bounded on purpose: a nine-digit account
                # written as "account #<digits>" must still be caught.
                if len(value) == 6 and match.start() and line[match.start() - 1] == "#":
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
    (tmp_path / offender).write_text(
        f'ACCOUNT = "robinhood-{probe_account}"\n', encoding="utf-8"
    )
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


@pytest.mark.parametrize(
    "rel", [".gitignore", ".env", "scripts/deploy", "Makefile", "tox.ini", "setup.cfg"]
)
def test_extensionless_and_dotfiles_are_scanned(rel):
    """The guard used to allowlist text SUFFIXES, which can only ever be
    incomplete -- .gitignore was tracked and unscanned, and an extensionless
    script or a future .ini would have been too (Codex review, PR #219)."""
    assert _is_scanned(rel)


@pytest.mark.parametrize("rel", ["assets/logo.png", "build/x.pyc", "docs/manual.pdf"])
def test_binaries_are_skipped_before_the_read(rel):
    assert not _is_scanned(rel)


def test_every_tracked_file_is_either_scanned_or_a_named_exemption():
    """No file may fall through the guard unnoticed."""
    for rel in _tracked_files():
        assert _is_scanned(rel) or rel in _ALLOWED or rel.startswith(_ALLOWED_PREFIXES), rel


# Derived, not spelled: a date-shaped literal in this file is itself an
# account-shaped literal the guard above must flag.
_A_DATE = datetime(2026, 7, 28).strftime("%Y%m%d")


def test_a_date_cue_in_the_filename_does_not_exempt_the_whole_file():
    """"config/midday_dca.json" contains "day" and "scheduled_tasks.json"
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
# ever look at raw["live_task"], raw["watchlist_task"], and
# raw["preflight_task"]. Flagging those prose blocks would be noise, not signal.
_RUNTIME_BROKER_ACCOUNT_PATHS = {
    "morning_dca.json": ("live_task", "watchlist_task", "preflight_task"),
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
