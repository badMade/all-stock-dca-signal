"""Decision core for the Notion-backed self-healing loop.

The loop closes a cycle over the operator's DCA Issues database: select an open
issue that has a written remediation, dispatch one repair branch for it, read
the pull-request outcome back, and -- crucially -- notice later if the fix
stopped holding. This module owns every decision in that cycle and none of the
input or output. It takes already-fetched records as flat mappings, in the shape
the connector's SQL mode returns, and gives back plans. The scheduled Routine
that holds connector runtime access is the only component that touches the wire,
so nothing here imports a network client, reads a credential, or names a Notion
identifier. ``tests/selfheal/test_notion_issue_loop.py`` asserts that directly.

Two record streams feed the loop:

* **Issue rows** from the DCA Issues data source, carrying what is wrong and how
  far the loop has got with it.
* **Run facts**, a per-error-code aggregate of the Run Log data source, carrying
  how often each code has fired and when it last fired.

The second stream exists because the DCA Issues rollups (``Occurrences``,
``First seen``, ``Last seen``) are declared unavailable to SQL and a ``SELECT``
naming one fails outright, so a parsed occurrence field on an issue row could
only ever hold a silent zero. Aggregating the Run Log's real ``Error code`` and
``Date`` columns yields the same two facts and is the only path that works.

Relationship to ``rocket-fuel/self-heal/PLAN.md``: the states, leases, attempt
ceiling and quarantine terminal here are that document's incident state machine.
Two rules deliberately differ, because this loop has neither a durable execution
lease nor an authoritative ``state.json`` to lean on:

1. **The attempt is charged at dispatch, not at outcome.** Without a lease to
   detect abandonment, a tick that died after opening the branch would leave the
   row chargeable forever. Charging up front fails closed, so
   :func:`plan_outcome` and :func:`plan_regression_check` never alter the count.
2. **The breaker is the refused queue itself.** It latches once enough rows sit
   in ``Refused``, so clearing it means a human triaging those rows rather than a
   pipeline-writable latch.

Standard library only. No repair execution, no subprocesses, no network.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import FrozenSet

# --- Vocabulary -------------------------------------------------------------

#: The row has never been attempted. Every pre-existing row predates the column,
#: so a blank cell reads as this rather than failing to parse.
STATE_NEW = "New"
#: A branch was dispatched and its pull request has not yet settled.
STATE_IN_REVIEW = "In review"
#: The fix merged and the default branch stayed green. Terminal until a later
#: run of the same error code proves otherwise (see :func:`plan_regression_check`).
STATE_REPAIRED = "Repaired"
#: An attempt settled unsuccessfully. Refused rows are the breaker's fuel.
STATE_REFUSED = "Refused"
#: The attempt ceiling is spent. Terminal, and only a human clears it.
STATE_QUARANTINED = "Quarantined"

ALL_STATES: FrozenSet[str] = frozenset(
    {STATE_NEW, STATE_IN_REVIEW, STATE_REPAIRED, STATE_REFUSED, STATE_QUARANTINED}
)

STATUS_OPEN = "Open"
STATUS_MONITORING = "Monitoring"
STATUS_RESOLVED = "Resolved"
ALL_STATUSES: FrozenSet[str] = frozenset({STATUS_OPEN, STATUS_MONITORING, STATUS_RESOLVED})

ALL_CLASSES: FrozenSet[str] = frozenset({"Transient", "Structural", "Environmental", "Policy"})

#: Classes with no repository-side fix at all. A session refusal, a permission
#: denial or a sandbox boot failure is decided outside this codebase, so
#: admitting one would spend an attempt that could never succeed.
NON_REPAIRABLE_CLASSES: FrozenSet[str] = frozenset({"Environmental"})

#: Severity in repair order, worst first. This is the ranking key, so it is
#: deliberately not the order the database happens to list the options in.
SEVERITY_RANK: tuple[str, ...] = (
    "Blocks slot",
    "Silent data loss",
    "Degrades slot",
    "Cosmetic",
)
ALL_SEVERITIES: FrozenSet[str] = frozenset(SEVERITY_RANK)

# --- Decision reason codes --------------------------------------------------
# Mirrors selfheal.reasons: the vocabulary is enumerated rather than inferred,
# so an unknown string can never be mistaken for a recognized decision.

#: Every gate passed; this row may be dispatched.
ADMITTED = "ADMITTED"
#: The operator gate in the shipped configuration is closed.
DISABLED = "DISABLED"
#: Enough rows sit in Refused that the loop must stop until a human triages them.
BREAKER_LATCHED = "BREAKER_LATCHED"
#: Eligible, but this tick's repair allowance is already spent.
TICK_CAP_REACHED = "TICK_CAP_REACHED"
#: The issue is not currently Open, so there is nothing to repair.
ISSUE_NOT_OPEN = "ISSUE_NOT_OPEN"
#: The class has no repository-side fix; see :data:`NON_REPAIRABLE_CLASSES`.
CLASS_NOT_REPAIRABLE = "CLASS_NOT_REPAIRABLE"
#: No remediation sentence is written, so there is nothing to act on.
NO_REMEDIATION = "NO_REMEDIATION"
#: A dispatched attempt is still settling; a second would duplicate it.
AWAITING_REVIEW = "AWAITING_REVIEW"
#: The row is already repaired.
ALREADY_REPAIRED = "ALREADY_REPAIRED"
#: The attempt ceiling is spent, whatever the stored state says.
ISSUE_QUARANTINED = "ISSUE_QUARANTINED"

ALL_LOOP_REASONS: FrozenSet[str] = frozenset(
    {
        ADMITTED,
        DISABLED,
        BREAKER_LATCHED,
        TICK_CAP_REACHED,
        ISSUE_NOT_OPEN,
        CLASS_NOT_REPAIRABLE,
        NO_REMEDIATION,
        AWAITING_REVIEW,
        ALREADY_REPAIRED,
        ISSUE_QUARANTINED,
    }
)

# --- Pull-request outcomes --------------------------------------------------

#: Merged, and the default branch stayed green afterwards. The only success.
PR_MERGED_MAIN_GREEN = "PR_MERGED_MAIN_GREEN"
#: Merged, but the default branch went red. The loop cannot undo this.
PR_MERGED_MAIN_RED = "PR_MERGED_MAIN_RED"
#: Closed without merging.
PR_CLOSED_UNMERGED = "PR_CLOSED_UNMERGED"
#: Still open, but its checks are failing.
PR_CHECKS_RED = "PR_CHECKS_RED"
#: Still open and not yet decided.
PR_OPEN_PENDING = "PR_OPEN_PENDING"
#: No pull request could be found for the dispatched branch.
PR_MISSING = "PR_MISSING"

ALL_OUTCOMES: FrozenSet[str] = frozenset(
    {
        PR_MERGED_MAIN_GREEN,
        PR_MERGED_MAIN_RED,
        PR_CLOSED_UNMERGED,
        PR_CHECKS_RED,
        PR_OPEN_PENDING,
        PR_MISSING,
    }
)

_REFUSAL_NOTES: dict[str, str] = {
    PR_MERGED_MAIN_RED: (
        "Merged, but the default branch went red afterwards. The loop cannot undo a merge, "
        "so this needs a human now."
    ),
    PR_CLOSED_UNMERGED: "Closed without merging, so the attempt did not land.",
    PR_CHECKS_RED: "Checks failed on the proposed fix.",
    PR_MISSING: "No pull request was found for the dispatched branch.",
}

_LEASE_EXPIRED_NOTE = "The review lease elapsed with the attempt unsettled."

#: The Run Log writes this placeholder in Error code for a clean run. It is not
#: an issue key and must be dropped silently rather than reported as malformed.
CLEAN_RUN_CODE = "—"

#: A Key doubles as the branch suffix, the pull-request dedupe token and the Run
#: Log join column, so a value that is not a safe identifier is refused outright
#: rather than sanitized into something that might collide.
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: Matching a scheme separator is a deliberately weak URL check: this module
#: only needs to reject an obviously non-URL value before it is written into the
#: database as a pull-request link.
_SCHEME_SEPARATOR = "://"


class LoopConfigError(ValueError):
    """Raised when the loop's configuration cannot be trusted.

    Args:
        message: Human-readable refusal naming the offending key or condition.
    """


class RowError(ValueError):
    """Raised when one fetched record cannot be parsed safely.

    Args:
        message: Human-readable refusal naming the offending field.
    """


class LoopStateError(RuntimeError):
    """Raised when a plan is requested for a row that cannot be in that state.

    Args:
        message: Human-readable refusal naming the row and the conflict.
    """


# --- Records ----------------------------------------------------------------


@dataclass(frozen=True)
class LoopConfig:
    """Validated loop policy.

    Attributes:
        enabled: Operator gate for the repair half. Shipped closed.
        max_repairs_per_tick: Most dispatches one tick may plan.
        max_attempts_per_issue: Attempts before an issue quarantines.
        breaker_refused_threshold: Refused rows that latch the breaker.
        review_lease_days: Days a dispatched attempt may remain unsettled.
    """

    enabled: bool
    max_repairs_per_tick: int
    max_attempts_per_issue: int
    breaker_refused_threshold: int
    review_lease_days: int


@dataclass(frozen=True)
class IssueRow:
    """One DCA Issues row, normalized.

    There is deliberately no occurrence field: ``Occurrences`` is a rollup that
    SQL cannot return, so a field here could only ever hold a silent zero.
    Frequency comes from :class:`RunFact` instead.

    Attributes:
        key: Stable snake_case identifier, also the Run Log's Error code.
        name: Human-readable title.
        page_url: The row's own address, used to write the decision back.
        issue_class: Transient, Structural, Environmental or Policy.
        component: Owning component; free text, since the options list grows.
        severity: One of :data:`SEVERITY_RANK`.
        status: Open, Monitoring or Resolved.
        remediation: The one-sentence fix instruction; empty when unwritten.
        retry_safe: Operator's judgement on whether a retry is safe.
        fix_state: One of :data:`ALL_STATES`.
        attempts: Dispatches charged so far.
        pr_url: The most recent dispatch's pull request, if any.
        last_attempt: When the most recent dispatch happened.
        fixed_on: When the fix landed; the baseline regression is judged against.
    """

    key: str
    name: str
    page_url: str
    issue_class: str
    component: str
    severity: str
    status: str
    remediation: str
    retry_safe: bool
    fix_state: str
    attempts: int
    pr_url: str | None
    last_attempt: date | None
    fixed_on: date | None


@dataclass(frozen=True)
class RunFact:
    """Aggregated Run Log evidence for one error code.

    Attributes:
        key: The Run Log's Error code, joined to :attr:`IssueRow.key`.
        occurrences: How many run records carry this code.
        last_seen: The most recent run date, or ``None`` when undated.
    """

    key: str
    occurrences: int
    last_seen: date | None


@dataclass(frozen=True)
class ParseResult:
    """Issue rows that parsed, plus one message per row that did not.

    Attributes:
        rows: Successfully parsed rows, in input order.
        errors: One message per rejected row, each naming the offending row.
    """

    rows: tuple[IssueRow, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class RunFactResult:
    """Run facts that parsed, plus one message per aggregate row that did not.

    Attributes:
        facts: Parsed facts keyed by error code.
        errors: One message per rejected aggregate row.
    """

    facts: Mapping[str, RunFact]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class TickPlan:
    """What one repair tick should do.

    Attributes:
        selected: Rows to dispatch, worst first.
        refusals: Reason code per row that was not selected.
        breaker_latched: Whether the refused queue stopped the loop.
    """

    selected: tuple[IssueRow, ...]
    refusals: Mapping[str, str]
    breaker_latched: bool


@dataclass(frozen=True)
class RowUpdate:
    """The write-back a caller should apply to one row.

    Attributes:
        key: The row's Key, for logging and dedupe.
        page_url: Where to write.
        fix_state: New Fix state.
        status: New Status.
        attempts: New attempt count.
        pr_url: New PR value.
        last_attempt: New Last attempt value.
        fixed_on: New Fixed on value.
        needs_human: Whether this outcome requires an operator's attention.
        note: One line explaining the transition, for the row's comment.
    """

    key: str
    page_url: str
    fix_state: str
    status: str
    attempts: int
    pr_url: str | None
    last_attempt: date | None
    fixed_on: date | None
    needs_human: bool
    note: str


# --- Field readers ----------------------------------------------------------


def _text(mapping: Mapping[str, object], field: str, *, required: bool) -> str:
    """Read one text field, refusing a wrong type or a required blank."""
    if field not in mapping:
        if required:
            raise RowError(f"{field}: required field is missing")
        return ""
    value = mapping[field]
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RowError(f"{field}: value must be text, got {type(value).__name__}")
    text = value.strip()
    if required and not text:
        raise RowError(f"{field}: value must not be blank")
    return text


def _enum(
    mapping: Mapping[str, object],
    field: str,
    allowed: FrozenSet[str],
    *,
    default: str | None = None,
) -> str:
    """Read one enumerated field, refusing an option this loop does not know.

    A new option added in the Notion interface must fail loudly rather than be
    bucketed into a default, because a silently-defaulted terminal state could
    make an exhausted row eligible again.
    """
    if field not in mapping or mapping[field] is None:
        if default is not None:
            return default
        raise RowError(f"{field}: required field is missing")
    value = mapping[field]
    if isinstance(value, str) and not value.strip():
        if default is not None:
            return default
        raise RowError(f"{field}: value must not be blank")
    if not isinstance(value, str) or value not in allowed:
        raise RowError(f"{field}: {value!r} is not a recognized option")
    return value


def _checkbox(mapping: Mapping[str, object], field: str) -> bool:
    """Read the connector's checkbox encoding, defaulting an unset box to off."""
    value = mapping.get(field)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if value == "__YES__":
        return True
    if value == "__NO__":
        return False
    raise RowError(f"{field}: {value!r} is not a recognized checkbox value")


def _count(mapping: Mapping[str, object], field: str) -> int:
    """Read one whole non-negative number, tolerating the connector's floats."""
    value = mapping.get(field)
    if value is None:
        return 0
    # bool subclasses int, so this guard must precede the integer check or True
    # would silently read as one consumed attempt.
    if isinstance(value, bool):
        raise RowError(f"{field}: value must be a number, not a boolean")
    if isinstance(value, float):
        # Numbers arrive as floats over SQL, so an integral float is the normal
        # case; a fractional one is a genuine defect.
        if not math.isfinite(value) or not value.is_integer():
            raise RowError(f"{field}: value must be a whole number, got {value!r}")
        value = int(value)
    if not isinstance(value, int):
        raise RowError(f"{field}: value must be a number, got {type(value).__name__}")
    if value < 0:
        raise RowError(f"{field}: value must not be negative, got {value}")
    return value


def _date(mapping: Mapping[str, object], field: str) -> date | None:
    """Read one date, accepting both spellings the connector may use.

    SQL mode exposes a date property only as ``date:<name>:start``, while other
    shapes use the plain property name. Accepting both keeps a caller from
    having to reshape records before calling in.
    """
    raw = mapping.get(field)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = mapping.get(f"date:{field}:start")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RowError(f"{field}: value must be an ISO-8601 date, got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        return None
    try:
        # A datetime is truncated to its date: every comparison this loop makes
        # is day-grained, and the connector returns both shapes.
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise RowError(f"{field}: {raw!r} is not an ISO-8601 date: {exc}") from exc


def _looks_like_url(value: object) -> bool:
    """Return whether ``value`` is plausibly a URL rather than a stray string."""
    return (
        isinstance(value, str)
        and _SCHEME_SEPARATOR in value
        and not value.startswith(_SCHEME_SEPARATOR)
    )


# --- Parsing ----------------------------------------------------------------


def parse_row(mapping: Mapping[str, object]) -> IssueRow:
    """Normalize one fetched DCA Issues record.

    Args:
        mapping: One record, keyed by column name.

    Returns:
        The frozen :class:`IssueRow`.

    Raises:
        RowError: If identity, classification, a number or a date is unusable.
            An empty remediation is not an error; it is an ordinary
            ineligibility that :func:`classify` reports, because one unwritten
            cell must not abort a whole tick.
    """
    page_url = _text(mapping, "url", required=True)
    key = _text(mapping, "Key", required=True)
    if not _KEY_PATTERN.match(key):
        raise RowError(f"Key: {key!r} is not a snake_case identifier")

    component = _text(mapping, "Component", required=False) or "None"
    return IssueRow(
        key=key,
        name=_text(mapping, "Name", required=True),
        page_url=page_url,
        issue_class=_enum(mapping, "Class", ALL_CLASSES),
        component=component,
        severity=_enum(mapping, "Severity", ALL_SEVERITIES),
        status=_enum(mapping, "Status", ALL_STATUSES),
        remediation=_text(mapping, "Remediation", required=False),
        retry_safe=_checkbox(mapping, "Retry safe"),
        fix_state=_enum(mapping, "Fix state", ALL_STATES, default=STATE_NEW),
        attempts=_count(mapping, "Attempts"),
        pr_url=_text(mapping, "PR", required=False) or None,
        last_attempt=_date(mapping, "Last attempt"),
        fixed_on=_date(mapping, "Fixed on"),
    )


def _row_identifier(mapping: Mapping[str, object], index: int) -> str:
    """Name one record well enough for an operator to find it in the database."""
    for field in ("url", "Key"):
        value = mapping.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"record {index}"


def parse_rows(mappings: Iterable[Mapping[str, object]]) -> ParseResult:
    """Normalize many records, reporting rather than raising on a bad one.

    One malformed row must not abort a tick, so each failure becomes a message
    naming the offending record and the remaining rows are still returned.

    Args:
        mappings: Fetched records, keyed by column name.

    Returns:
        A :class:`ParseResult` whose errors name every rejected record.
    """
    rows: list[IssueRow] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, mapping in enumerate(mappings):
        try:
            row = parse_row(mapping)
        except RowError as exc:
            errors.append(f"{_row_identifier(mapping, index)}: {exc}")
            continue
        # Two rows claiming one Key would make attempts, state and the Run Log
        # join ambiguous, and the loop would act on whichever it saw last.
        if row.key in seen:
            errors.append(f"{row.key}: duplicate Key; refusing an ambiguous row")
            continue
        seen.add(row.key)
        rows.append(row)
    return ParseResult(tuple(rows), tuple(errors))


def parse_run_facts(mappings: Iterable[Mapping[str, object]]) -> RunFactResult:
    """Normalize the per-error-code Run Log aggregate.

    Each record is expected to carry ``code``, ``occurrences`` and ``last_seen``,
    the aliases the documented grouped query produces.

    Args:
        mappings: Aggregated Run Log records.

    Returns:
        A :class:`RunFactResult` keyed by error code. A blank code and the
        clean-run placeholder are dropped silently: neither names an issue.
        A code with no issue row is kept, since it is evidence either way.
    """
    facts: dict[str, RunFact] = {}
    errors: list[str] = []
    for mapping in mappings:
        raw_code = mapping.get("code")
        code = raw_code.strip() if isinstance(raw_code, str) else ""
        if not code or code == CLEAN_RUN_CODE:
            continue
        try:
            occurrences = _count(mapping, "occurrences")
            last_seen = _date(mapping, "last_seen")
        except RowError as exc:
            errors.append(f"{code}: {exc}")
            continue
        # The documented query groups by Error code, so a duplicate means the
        # caller's query was wrong; keeping the last row would hide that.
        if code in facts:
            errors.append(f"{code}: duplicate error code; the aggregate must group by Error code")
            continue
        facts[code] = RunFact(key=code, occurrences=occurrences, last_seen=last_seen)
    return RunFactResult(facts, tuple(errors))


# --- Admission --------------------------------------------------------------


def classify(row: IssueRow, *, config: LoopConfig) -> str:
    """Return the one reason code describing this row's admission decision.

    Terminal states are tested before intrinsic properties, and both before
    transient ones, so the reported reason is the most durable truth about the
    row rather than whichever gate happened to run first.

    Args:
        row: The parsed issue row.
        config: The loop policy supplying the attempt ceiling.

    Returns:
        A member of :data:`ALL_LOOP_REASONS`; :data:`ADMITTED` means dispatchable.
    """
    if row.fix_state == STATE_REPAIRED:
        return ALREADY_REPAIRED
    # Quarantine follows from the attempt count, not only from the stored state:
    # otherwise editing one cell in the Notion interface would re-arm an
    # exhausted issue and hand it an unlimited budget.
    if row.fix_state == STATE_QUARANTINED or row.attempts >= config.max_attempts_per_issue:
        return ISSUE_QUARANTINED
    if row.issue_class in NON_REPAIRABLE_CLASSES:
        return CLASS_NOT_REPAIRABLE
    if not row.remediation.strip():
        return NO_REMEDIATION
    if row.fix_state == STATE_IN_REVIEW:
        return AWAITING_REVIEW
    if row.status != STATUS_OPEN:
        return ISSUE_NOT_OPEN
    return ADMITTED


def rank_issues(
    rows: Iterable[IssueRow], *, facts: Mapping[str, RunFact]
) -> tuple[IssueRow, ...]:
    """Order rows worst-first: severity, then frequency, then key.

    Args:
        rows: Rows to order; never mutated.
        facts: Run Log aggregate. A row with no fact ranks as zero occurrences,
            which is correct: nothing in the Run Log has argued for it yet.

    Returns:
        A new ordered tuple.
    """

    def sort_key(row: IssueRow) -> tuple[int, int, str]:
        try:
            severity_index = SEVERITY_RANK.index(row.severity)
        except ValueError:  # pragma: no cover - parsing rejects unknown severities
            severity_index = len(SEVERITY_RANK)
        fact = facts.get(row.key)
        occurrences = fact.occurrences if fact is not None else 0
        # The key is the final tiebreak so a tick's selection is reproducible.
        return (severity_index, -occurrences, row.key)

    return tuple(sorted(rows, key=sort_key))


def breaker_latched(rows: Iterable[IssueRow], *, config: LoopConfig) -> bool:
    """Return whether enough refused rows have accumulated to stop the loop.

    The refused queue *is* the latch. There is no elapsed-time reset and no
    pipeline path that clears it: a human triages those rows, which is the same
    human-only property ``PLAN.md`` requires of its stored latch.

    Args:
        rows: Every known issue row.
        config: The loop policy supplying the threshold.

    Returns:
        ``True`` when the loop must not dispatch.
    """
    refused = sum(1 for row in rows if row.fix_state == STATE_REFUSED)
    return refused >= config.breaker_refused_threshold


def plan_tick(
    rows: Iterable[IssueRow], *, config: LoopConfig, facts: Mapping[str, RunFact]
) -> TickPlan:
    """Decide what one repair tick should dispatch.

    Every row lands in exactly one of ``selected`` or ``refusals``, so a caller
    can always account for the whole database.

    Args:
        rows: Every known issue row.
        config: The loop policy.
        facts: Run Log aggregate used for ranking.

    Returns:
        The :class:`TickPlan`.
    """
    rows = list(rows)
    latched = breaker_latched(rows, config=config)
    refusals: dict[str, str] = {}

    if not config.enabled:
        for row in rows:
            refusals[row.key] = DISABLED
        return TickPlan((), refusals, latched)

    eligible: list[IssueRow] = []
    for row in rows:
        reason = classify(row, config=config)
        if reason == ADMITTED:
            eligible.append(row)
        else:
            refusals[row.key] = reason

    if latched:
        for row in eligible:
            refusals[row.key] = BREAKER_LATCHED
        return TickPlan((), refusals, True)

    ranked = rank_issues(eligible, facts=facts)
    selected = ranked[: config.max_repairs_per_tick]
    for row in ranked[config.max_repairs_per_tick :]:
        refusals[row.key] = TICK_CAP_REACHED
    return TickPlan(selected, refusals, False)


# --- Transitions ------------------------------------------------------------


def plan_dispatch(
    row: IssueRow, *, pr_url: object, now: date, config: LoopConfig
) -> RowUpdate:
    """Plan the write-back for a row whose repair branch has just been opened.

    The attempt is charged here rather than at outcome. Without a durable
    execution lease, a tick that died after opening the branch would leave the
    row chargeable forever; charging up front fails closed.

    Args:
        row: The row being dispatched.
        pr_url: The opened pull request's address.
        now: The dispatch date, which starts the review lease.
        config: The loop policy.

    Returns:
        The :class:`RowUpdate` to apply.

    Raises:
        LoopStateError: If the row is not admissible, or no usable pull-request
            address was supplied.
    """
    reason = classify(row, config=config)
    if reason != ADMITTED:
        raise LoopStateError(f"{row.key}: not admissible for dispatch ({reason})")
    if not _looks_like_url(pr_url):
        raise LoopStateError(f"{row.key}: dispatch requires a pull request address, got {pr_url!r}")
    attempts = row.attempts + 1
    return RowUpdate(
        key=row.key,
        page_url=row.page_url,
        fix_state=STATE_IN_REVIEW,
        status=STATUS_MONITORING,
        attempts=attempts,
        pr_url=str(pr_url),
        last_attempt=now,
        fixed_on=row.fixed_on,
        needs_human=False,
        note=f"Attempt {attempts} of {config.max_attempts_per_issue} dispatched.",
    )


def _lease_expired(row: IssueRow, *, now: date, config: LoopConfig) -> bool:
    """Return whether a dispatched attempt has outlived its review lease.

    A row that cannot prove when its lease started is treated as expired. Fail
    closed: otherwise a missing date would let it hold the In review state, and
    the loop's only slot for that issue, indefinitely.
    """
    if row.last_attempt is None:
        return True
    return (now - row.last_attempt).days > config.review_lease_days


def _refusal(
    row: IssueRow, *, config: LoopConfig, note: str, escalate: bool
) -> RowUpdate:
    """Build the shared refusal write-back, quarantining at the ceiling.

    The attempt count is carried unchanged because it was charged at dispatch,
    and the fix date is cleared because a refused row is no longer claiming one.
    """
    quarantined = row.attempts >= config.max_attempts_per_issue
    return RowUpdate(
        key=row.key,
        page_url=row.page_url,
        fix_state=STATE_QUARANTINED if quarantined else STATE_REFUSED,
        status=STATUS_OPEN,
        attempts=row.attempts,
        pr_url=row.pr_url,
        last_attempt=row.last_attempt,
        fixed_on=None,
        needs_human=escalate or quarantined,
        note=f"{note} Attempt ceiling reached; quarantined." if quarantined else note,
    )


def plan_outcome(
    row: IssueRow, outcome: str, *, now: date, config: LoopConfig
) -> RowUpdate:
    """Plan the write-back for a settled or still-settling dispatched attempt.

    Args:
        row: The dispatched row.
        outcome: A member of :data:`ALL_OUTCOMES`.
        now: Today, used for the lease and for the fix date.
        config: The loop policy.

    Returns:
        The :class:`RowUpdate` to apply.

    Raises:
        LoopStateError: If ``outcome`` is not a recognized outcome.
    """
    if outcome not in ALL_OUTCOMES:
        raise LoopStateError(f"{row.key}: {outcome!r} is not a recognized outcome")

    if outcome == PR_MERGED_MAIN_GREEN:
        return RowUpdate(
            key=row.key,
            page_url=row.page_url,
            fix_state=STATE_REPAIRED,
            status=STATUS_RESOLVED,
            attempts=row.attempts,
            pr_url=row.pr_url,
            last_attempt=row.last_attempt,
            # Stamped here and nowhere else: this is the baseline a later
            # regression is judged against.
            fixed_on=now,
            needs_human=False,
            note="Merged with the default branch green; marked repaired.",
        )

    if outcome == PR_OPEN_PENDING and not _lease_expired(row, now=now, config=config):
        return RowUpdate(
            key=row.key,
            page_url=row.page_url,
            fix_state=STATE_IN_REVIEW,
            status=STATUS_MONITORING,
            attempts=row.attempts,
            pr_url=row.pr_url,
            last_attempt=row.last_attempt,
            fixed_on=row.fixed_on,
            needs_human=False,
            note="Still in review within its lease; held.",
        )

    note = _REFUSAL_NOTES.get(outcome, _LEASE_EXPIRED_NOTE)
    # A merge that reddens the default branch is the one refusal the loop can
    # never walk back on its own, so it always escalates.
    return _refusal(row, config=config, note=note, escalate=outcome == PR_MERGED_MAIN_RED)


def plan_regression_check(
    row: IssueRow, *, facts: Mapping[str, RunFact], config: LoopConfig
) -> RowUpdate | None:
    """Plan the write-back for a repaired row whose fix may have stopped holding.

    A merged, green fix is not proof the incident stopped happening; the Run Log
    is. When a run recorded this error code strictly after the fix landed, the
    row reopens as ``Refused`` rather than ``New``, so a regression feeds the
    breaker exactly as a failed attempt does. The attempt count is preserved:
    resetting it would hand a half-working fix an unlimited budget.

    Same-day recurrence is not treated as evidence, because the merge and that
    day's runs are not ordered relative to each other.

    Args:
        row: A row to examine; only a repaired one can regress.
        facts: Run Log aggregate.
        config: The loop policy.

    Returns:
        The :class:`RowUpdate` to apply, or ``None`` when nothing changed.
    """
    if row.fix_state != STATE_REPAIRED:
        return None
    fact = facts.get(row.key)
    if fact is None or fact.last_seen is None:
        return None

    if row.fixed_on is None:
        # Without a baseline the loop cannot tell an old run from a new one.
        # Reopening on that would repeat every tick, so it reports once and
        # leaves the state for a human to settle.
        return RowUpdate(
            key=row.key,
            page_url=row.page_url,
            fix_state=STATE_REPAIRED,
            status=row.status,
            attempts=row.attempts,
            pr_url=row.pr_url,
            last_attempt=row.last_attempt,
            fixed_on=None,
            needs_human=True,
            note=(
                f"Marked repaired but carries no fix date, so the run recorded on "
                f"{fact.last_seen} cannot be judged. Needs an operator."
            ),
        )

    if fact.last_seen <= row.fixed_on:
        return None

    return _refusal(
        row,
        config=config,
        note=(
            f"Reopened: the Run Log recorded this code on {fact.last_seen}, "
            f"after the fix landed on {row.fixed_on}."
        ),
        escalate=True,
    )


# --- Configuration ----------------------------------------------------------

_SCHEMA_REFERENCE = "../schemas/notion_issue_loop.schema.json"

# A checkout keeps config/ beside the package; an installed wheel ships it under
# sys.prefix. Checking both means the strict loader never has to manufacture
# defaults because packaging was incomplete.
_DEFAULT_CONFIG_PATHS = (
    Path(__file__).resolve().parent.parent / "config" / "notion_issue_loop.json",
    Path(sys.prefix) / "share" / "all-stock-dca" / "config" / "notion_issue_loop.json",
)


@dataclass(frozen=True)
class _IntegerSetting:
    """One required JSON integer and its inclusive bounds."""

    minimum: int
    maximum: int


@dataclass(frozen=True)
class _OversizedInteger:
    """A valid JSON integer that exceeded Python's conversion safety limit."""

    digit_count: int


def _parse_json_integer(raw: str) -> int | _OversizedInteger:
    """Parse a JSON integer without leaking Python's digit-limit ValueError."""
    try:
        return int(raw)
    except ValueError:
        # Keeping only the length avoids retaining or echoing an attacker-sized
        # token while still letting the validator name the offending key.
        return _OversizedInteger(digit_count=len(raw.lstrip("-")))


# Every integer is bounded here even though the shipped document also carries a
# JSON Schema: a caller may supply an alternate path and is not required to run
# a schema library, so runtime validation is the safety boundary, not CI alone.
# Without ceilings a configured value defeats boundedness as effectively as a
# missing one.
_INTEGER_SETTINGS: dict[str, _IntegerSetting] = {
    "max_repairs_per_tick": _IntegerSetting(1, 10),
    "max_attempts_per_issue": _IntegerSetting(1, 10),
    "breaker_refused_threshold": _IntegerSetting(1, 20),
    "review_lease_days": _IntegerSetting(1, 90),
}

_REQUIRED_KEYS = frozenset({"$schema", "enabled", *_INTEGER_SETTINGS})


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while refusing duplicate member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        # json.loads otherwise keeps the final value, letting file order decide
        # which ceiling the loop runs under.
        if key in result:
            raise LoopConfigError(f"{key}: duplicate JSON key; refusing an ambiguous limit")
        result[key] = value
    return result


def _default_config_path() -> Path:
    """Return the checkout or installed config path, failing if neither exists."""
    for candidate in _DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    checked = ", ".join(str(candidate) for candidate in _DEFAULT_CONFIG_PATHS)
    raise LoopConfigError(f"$config: packaged configuration file is missing; checked {checked}")


def _read_document(path: Path) -> object:
    """Read and decode one JSON document without accepting duplicate keys."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise LoopConfigError(f"$config: configuration file {path} is missing") from exc
    except (OSError, ValueError) as exc:
        raise LoopConfigError(f"$config: configuration file {path} is unreadable: {exc}") from exc
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys, parse_int=_parse_json_integer)
    except LoopConfigError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise LoopConfigError(f"$document: {path} is not valid JSON: {exc}") from exc


def _validated_integer(key: str, value: object, setting: _IntegerSetting) -> int:
    """Validate one raw integer setting against its inclusive bounds."""
    if isinstance(value, _OversizedInteger):
        raise LoopConfigError(
            f"{key}: integer literal has {value.digit_count} digits and exceeds the parser safety limit"
        )
    # Python's JSON decoder accepts NaN and infinities even though ordinary JSON
    # does not; naming that first tells the operator the value is non-finite
    # rather than merely the wrong type.
    if isinstance(value, float) and not math.isfinite(value):
        raise LoopConfigError(f"{key}: value must be finite, got {value!r}")
    # bool subclasses int, so this guard must precede the integer check or true
    # could silently become a one-unit ceiling.
    if isinstance(value, bool):
        raise LoopConfigError(f"{key}: value must be an integer, not a boolean")
    if not isinstance(value, int):
        raise LoopConfigError(f"{key}: value must be an integer, got {type(value).__name__}")
    if value < setting.minimum:
        raise LoopConfigError(f"{key}: value {value} is below minimum {setting.minimum}")
    if value > setting.maximum:
        raise LoopConfigError(f"{key}: value {value} is above maximum {setting.maximum}")
    return value


def load_config(path: str | Path | None = None) -> LoopConfig:
    """Load and strictly validate the loop configuration.

    Args:
        path: Optional JSON path. When omitted, the version-controlled checkout
            path is tried first, followed by the installed data-file path.

    Returns:
        A frozen :class:`LoopConfig`.

    Raises:
        LoopConfigError: If the file is missing or unreadable, the JSON is
            malformed or ambiguous, a key is missing or unknown, or a value has
            the wrong type or lies outside its bound.
    """
    config_path = Path(path) if path is not None else _default_config_path()
    document = _read_document(config_path)
    if not isinstance(document, dict):
        raise LoopConfigError(
            f"$document: configuration must be a JSON object, got {type(document).__name__}"
        )

    unknown = sorted(set(document) - _REQUIRED_KEYS)
    if unknown:
        raise LoopConfigError(f"{unknown[0]}: unknown configuration key; allowed keys are fixed")
    missing = sorted(_REQUIRED_KEYS - set(document))
    if missing:
        raise LoopConfigError(f"{missing[0]}: required configuration key is missing")

    if document["$schema"] != _SCHEMA_REFERENCE:
        raise LoopConfigError(f"$schema: value must be exactly {_SCHEMA_REFERENCE!r}")

    enabled = document["enabled"]
    if not isinstance(enabled, bool):
        raise LoopConfigError(f"enabled: value must be a boolean, got {type(enabled).__name__}")

    values = {
        key: _validated_integer(key, document[key], setting)
        for key, setting in _INTEGER_SETTINGS.items()
    }
    return LoopConfig(enabled=enabled, **values)


def replace_config(config: LoopConfig, **changes: object) -> LoopConfig:
    """Return a copy of ``config`` with ``changes`` applied.

    Provided so a caller (and the suite) can open the operator gate for one
    evaluation without editing the shipped document.

    Args:
        config: The policy to copy.
        **changes: Field overrides.

    Returns:
        The new frozen :class:`LoopConfig`.
    """
    return replace(config, **changes)


__all__ = [
    "ADMITTED",
    "ALL_CLASSES",
    "ALL_LOOP_REASONS",
    "ALL_OUTCOMES",
    "ALL_SEVERITIES",
    "ALL_STATES",
    "ALL_STATUSES",
    "ALREADY_REPAIRED",
    "AWAITING_REVIEW",
    "BREAKER_LATCHED",
    "CLASS_NOT_REPAIRABLE",
    "CLEAN_RUN_CODE",
    "DISABLED",
    "ISSUE_NOT_OPEN",
    "ISSUE_QUARANTINED",
    "IssueRow",
    "LoopConfig",
    "LoopConfigError",
    "LoopStateError",
    "NON_REPAIRABLE_CLASSES",
    "NO_REMEDIATION",
    "PR_CHECKS_RED",
    "PR_CLOSED_UNMERGED",
    "PR_MERGED_MAIN_GREEN",
    "PR_MERGED_MAIN_RED",
    "PR_MISSING",
    "PR_OPEN_PENDING",
    "ParseResult",
    "RowError",
    "RowUpdate",
    "RunFact",
    "RunFactResult",
    "SEVERITY_RANK",
    "STATE_IN_REVIEW",
    "STATE_NEW",
    "STATE_QUARANTINED",
    "STATE_REFUSED",
    "STATE_REPAIRED",
    "STATUS_MONITORING",
    "STATUS_OPEN",
    "STATUS_RESOLVED",
    "TICK_CAP_REACHED",
    "TickPlan",
    "breaker_latched",
    "classify",
    "load_config",
    "parse_row",
    "parse_rows",
    "parse_run_facts",
    "plan_dispatch",
    "plan_outcome",
    "plan_regression_check",
    "plan_tick",
    "rank_issues",
    "replace_config",
]
