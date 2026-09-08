"""Live market-data providers and broker adapter for the near-close graded DCA strategy.

These implementations conform to the ``MarketDataProvider`` and ``BrokerAdapter``
protocols in :mod:`near_close_dca`. They hold **no credentials and make no network
calls of their own**: every external interaction goes through *injected callables*
that the scheduler/agent supplies at runtime — typically thin wrappers around the
Robinhood MCP tools (``get_equity_historicals``, ``get_equity_quotes``,
``review_equity_order``, ``place_equity_order`` …).

This mirrors the dependency-injection pattern already used by ``AlpacaBroker`` and
keeps the module import-safe, unit-testable, and secret-free. The callables receive
plain keyword arguments that map 1:1 to the MCP tool inputs and return the raw MCP
response dicts; all parsing lives here.

Safety posture
--------------
* ``RobinhoodBroker.place_market_buy`` refuses to place a real order unless
  ``enable_live_orders=True`` *and* a ``place_fn`` is wired in. With the defaults it
  returns a dry-run sentinel id and never touches the network.
* ``near_close_dca.run_once`` additionally never calls ``place_market_buy`` for a
  non-``DryRunBroker`` while ``StrategyConfig.dry_run`` is True, so live placement
  requires flipping **two** independent gates.
"""

from __future__ import annotations

import decimal
import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from near_close_dca import (
    _TERMINAL_BROKER_ORDER_STATES,
    _WORKING_BROKER_ORDER_STATES,
    EASTERN,
    ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
    AmbiguousPlacementResponseError,
    Bar,
    FractionalPlacementRejectedError,
    MarketBuyPlacementTerminalError,
    OrderFill,
    OrderPlacementAmbiguousError,
    SourceName,
    WholeSharePlacementTerminalError,
    _is_blank_broker_field,
    _normalize_broker_lifecycle_alias,
    _normalized_order_id,
    _reconciled_order_may_have_executed,
    _recovered_broker_notional,
    _redacted_diagnostic_text,
    _unwrap_fill_alias_value,
    nyse_holidays,
    terminal_broker_row_proves_no_order,
)

logger = logging.getLogger("near_close_dca.live_adapters")


def _log_safe_repr(value: Any) -> str:
    """``repr(value)``, or a placeholder when that repr cannot be built safely.

    A broker response containing a native int beyond Python's integer-to-
    string conversion digit limit (e.g. a malformed ``id: 10**5000``)
    raises ``ValueError`` the instant anything -- including a logging
    call's own lazy ``%s`` formatting -- tries to render it, since Python
    ``repr``/``str`` on such an int hits the identical limit. A bare
    ``logger.error(..., response)`` call passing the raw response through
    then raised INSIDE the logging module's own formatting machinery,
    aborting whatever this call was trying to log about (Codex review, PR
    #1169: "Check failure-envelope ID bounds before stringifying" -- the
    same overflow class, reached via a diagnostic log call rather than a
    validation path). Logged only for operator diagnostics, so a
    placeholder here loses no correctness -- the caller's own validation
    still classifies the response as usual.
    """
    try:
        return repr(value)
    except ValueError:
        return f"<{type(value).__name__}: unrepresentable -- contains an oversized numeric value>"


def _artifact_safe_repr(value: Any) -> str:
    """Render broker data for an exception that may be persisted in artifacts."""
    return _redacted_diagnostic_text(_log_safe_repr(value))


# Sentinel distinguishing "not yet fetched" from a cached ``None`` (quote genuinely
# unavailable) in the signal-time quote memo, so a legitimately-absent quote is
# cached rather than re-fetched on the paired signal-time call.
_UNSET = object()

_UTC = timezone.utc


def _is_representable_numeric_order_id(value: "int | float") -> bool:
    """Whether an already-numeric-typed order id is finite enough to trust.

    ``json.loads`` parses an arbitrarily large integer literal (e.g. a
    malformed response's ``10**1000``) into a native, unbounded Python
    ``int`` with no error at parse time -- the overflow only surfaces
    later, when something tries to convert it to a float. The removed
    ``dca_runner._is_usable_order_id`` helper this module's own order-id
    validation otherwise mirrors explicitly caught that ``OverflowError``
    from ``math.isfinite()``; without the same guard here, an
    astronomically large id passed every other check (not a bool, not
    blank, not negative) and was returned as a successful broker order
    id -- booking the full notional against an identifier that later
    reconciliation's own overflow-aware validation
    (``near_close_dca._is_usable_reconciled_order_id``) could never
    actually treat as usable (Codex review, PR #1164: "Reject oversized
    numeric placement IDs").
    """
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_stringified_non_positive_number(value: str) -> bool:
    """True when a string spells a non-positive number (``"0"``, ``"-1"``, ``"0.0"``).

    Parsed via ``Decimal``, not ``float()``: binary floating point
    underflows a tiny but genuinely POSITIVE scientific spelling
    (``"1e-5000"``) to ``0.0``, which this check then misclassified as
    non-positive -- both ``place_market_buy()`` and
    ``place_whole_share_buy()`` discarded that valid order id and raised
    "no order id" after the broker had already accepted the order,
    forcing ambiguous-placement recovery or halting the batch (Codex
    review, PR #1169: "Preserve tiny positive numeric order IDs").
    ``Decimal`` parses any numeric spelling -- not just a bare digit
    string -- exactly at any magnitude, without Python 3.11's
    integer-string conversion digit limit applying (that limit is
    specific to ``int()``), so this stays safe and precise on an
    arbitrarily long or tiny numeric string.
    """
    try:
        return decimal.Decimal(value) <= 0
    except decimal.InvalidOperation:
        return False


def _is_stringified_non_finite_number(value: str) -> bool:
    """True when a string spells NaN or +/-infinity (``"nan"``, ``"Infinity"``, ``"-inf"``).

    ``_is_stringified_non_positive_number`` above cannot catch these: a
    NaN comparison against 0 is always False (neither ``<=`` direction
    holds), and positive infinity is not ``<= 0`` either. A connector
    serializing its non-finite sentinel as a string (``ref_id="NaN"``)
    therefore selected it as a usable reference over a valid secondary
    alias beside it, the identical failure mode the native-float
    non-finite rejection already covers for a Python ``float`` value
    (Codex review, PR #1168: "Fall through stringified non-finite
    reference aliases").

    Parsed via ``Decimal``, not ``float()``: a finite positive numeric
    string outside binary-float range (``"1e10000"``) converts through
    ``float()`` to ``inf`` -- no exception raised -- so this predicate
    misclassified it as non-finite even though ``_normalized_order_id``
    deliberately preserves that exact identifier. Both live placement
    validators then raised "no order id" after the broker had already
    accepted the order (Codex review, PR #1169: "Detect non-finite
    strings without binary-float overflow"). ``Decimal.is_finite()``
    distinguishes an actual NaN/infinity spelling from a merely
    large-magnitude one without ever routing through binary floating
    point.
    """
    try:
        _decimal_value = decimal.Decimal(value)
    except decimal.InvalidOperation:
        return False
    return not _decimal_value.is_finite()


def _is_unusable_dedup_id(value: Any) -> bool:
    """True when ``value`` cannot safely key pagination row deduplication.

    Shared by :func:`RobinhoodBroker._iter_same_day_orders`'s primary AND
    fallback (secondary alias) id lookups -- applying it only to the
    primary and stringifying whatever the secondary fallback happened to
    be let two rows with distinct malformed primary ids (``id=["BAD1"]``,
    ``id=["BAD2"]``) that share the SAME malformed secondary
    ``order_id=["SHARED"]`` both fall through to that shared, unvalidated
    fallback and deduplicate under the identical fabricated key
    ``"['SHARED']"``, discarding a still-live row (Codex review, PR #1168:
    "Validate fallback IDs before pagination deduplication"). Rejects
    blank values, falsey non-string sentinels (``0``, ``False``),
    non-scalar values (a list, a mapping), booleans, and numeric
    sentinels this module's own placement/reconciliation predicates
    already treat as unusable (negative, non-finite, or otherwise
    unrepresentable).

    The negative-sentinel rejection also applies when a connector
    serializes the placeholder as a STRING (``id="-1"``), not just a
    Python ``int``/``float`` -- the numeric-typed check alone missed
    this, so a rejected row and a queued row sharing that identical
    stringified sentinel (each with their own distinct valid secondary
    alias) still deduplicated together, dropping the queued row (Codex
    review, PR #1168: "Reject stringified sentinels before
    deduplicating history"). Mirrors the identical ``str(order_id).
    strip().startswith("-")`` check this module's own placement
    validation already applies.

    A stringified ZERO (``id="0"``) is rejected the same way, not just a
    leading ``"-"``, via ``_is_stringified_non_positive_number``: it is
    truthy (so ``not value`` misses it) and does not start with ``"-"``,
    so it survived every check above. If a rejected row and a queued row
    both carried that placeholder alongside distinct valid secondary
    aliases, they deduplicated together under ``"0"`` and the queued row
    was dropped, potentially authorizing a duplicate purchase (Codex
    review, PR #1168: "Reject stringified zero IDs before deduplicating
    history" / "Reject stringified zero IDs before history
    deduplication" -- two independently-authored fixes for the identical
    gap).

    A stringified NaN/infinity (``id="NaN"``) is rejected too, via
    ``_is_stringified_non_finite_number``: ``_is_stringified_non_positive_
    number`` alone cannot catch these -- a NaN comparison against 0 is
    always False, and positive infinity is not ``<= 0`` either -- despite
    the native-float non-finite rejection already covering a Python
    ``float("nan")`` a few lines below. A rejected row and a queued row
    sharing that identical stringified sentinel (each with their own
    distinct valid secondary alias) still deduplicated together, dropping
    the queued row (Codex review, PR #1168: "Reject serialized non-finite
    IDs before history deduplication").
    """
    return (
        _is_blank_broker_field(value)
        or not value
        or isinstance(value, bool)
        or not isinstance(value, (str, int, float))
        or (isinstance(value, str) and _is_stringified_non_positive_number(value.strip()))
        or (isinstance(value, str) and _is_stringified_non_finite_number(value.strip()))
        or (isinstance(value, (int, float)) and value < 0)
        or (isinstance(value, (int, float)) and not _is_representable_numeric_order_id(value))
    )


def _is_unusable_live_placement_order_id(order_id: Any) -> bool:
    """True when a SUCCESSFUL placement response's selected order_id is unusable.

    The exact validation ``place_market_buy``'s and
    ``place_whole_share_buy``'s own success paths already apply inline
    before accepting an ``order_id``, factored out here so it can also be
    reused as a FALLBACK check on the secondary ``"order_id"`` alias when
    the primary ``"id"`` fails it: ``{"id": true, "order_id": "REAL",
    "state": "queued"}`` selected ``True`` via ``_first_usable_order_id_
    alias`` (which only screens for blank/falsey, not full usability),
    both success paths rejected it and cleared to ``None``, and neither
    ever retried the perfectly usable ``"REAL"`` sitting right beside it
    -- an already-accepted order was halted into ambiguous-placement
    recovery instead of being recorded under its real id (Codex review,
    PR #1169: "Fall through unusable primary placement IDs"). Not folded
    into ``_first_usable_order_id_alias`` itself: that function's OTHER
    caller, the failure-envelope path, deliberately needs the RAW
    primary value (including an oversized or NaN sentinel) to run its
    OWN, more nuanced "malformed vs. genuinely absent" distinction (see
    ``_is_representable_numeric_order_id``'s callers there) -- collapsing
    that distinction here would have re-broken the exact case PR #1168's
    "Preserve ambiguity for malformed positive order IDs" fixed.
    """
    return (
        isinstance(order_id, bool)
        or not isinstance(order_id, (str, int, float))
        # Checked BEFORE the bare `str(order_id).strip()` below: for a
        # native (not string-encoded) int/float beyond
        # `_is_representable_numeric_order_id`'s own bound, `str(order_id)`
        # itself raises `ValueError` (Python's integer-to-string
        # conversion digit limit), aborting this helper with an unhandled
        # exception instead of cleanly rejecting the oversized candidate
        # (Codex review, PR #1169: "Check numeric representability before
        # stringifying placement IDs").
        or (isinstance(order_id, (int, float)) and not _is_representable_numeric_order_id(order_id))
        or not str(order_id).strip()
        or (isinstance(order_id, str) and _is_stringified_non_positive_number(order_id.strip()))
        or (isinstance(order_id, str) and _is_stringified_non_finite_number(order_id.strip()))
        or (isinstance(order_id, (int, float)) and order_id <= 0)
    )


def _first_usable_order_id_alias(container: Dict[str, Any]) -> Any:
    """The first of ``"id"``/``"order_id"`` on ``container`` that is not blank.

    A raw ``container.get("id") or container.get("order_id")`` picks
    whichever is merely non-empty-string-truthy FIRST, so a blank
    placeholder on ``"id"`` (``""``, whitespace, or ``None``) alongside a
    real, usable ``"order_id"`` -- ``{"id": "   ", "order_id": "REAL-1",
    "state": "queued"}`` -- selected the blank placeholder over the real
    id, and the caller's own subsequent blank check then discarded the
    result entirely, reporting an accepted response as ID-less and
    halting the batch even though a usable identifier was right there
    under the other alias (Codex review, PR #1164: "Fall back past blank
    primary order IDs"). A non-string, non-blank value (e.g. a boolean
    sentinel ``id=True``) is still returned as-is, unchanged -- every
    caller's own downstream validation (reject booleans, reject
    astronomically large ints, ...) still runs on whatever this returns,
    exactly as it did on the raw ``or`` fallback before.

    Also falls through a FALSEY primary placeholder (``id=0``, an empty
    list/mapping), not just a blank one: `_is_blank_broker_field` only
    reads ``None``/``""``/whitespace as blank, so ``{"id": 0, "order_id":
    "REAL", "state": "queued"}`` was non-blank and still selected the
    placeholder. Both live placement paths then reject that selected
    value outright and report the already-accepted order as ID-less
    instead of using "REAL", forcing ambiguous recovery and delaying
    durable notional/provenance bookkeeping -- the original ``id or
    order_id`` expression this function replaced never had this gap,
    since a falsey ``id`` there was never selected to begin with (Codex
    review, PR #1168: "Fall through falsey primary order-ID
    placeholders").
    """
    primary = container.get("id")
    if not _is_blank_broker_field(primary) and primary:
        return primary
    return container.get("order_id")


def _first_usable_ref_id_alias(order: Dict[str, Any]) -> str:
    """The first of ``ref_id``/``client_order_id``/``client_order_uuid``/
    ``client_id`` on ``order`` that is not blank, stripped, or ``""``.

    The raw ``order.get("ref_id") or order.get("client_order_id") or ...``
    chain these callers used picks whichever alias is merely non-empty-
    string-truthy FIRST -- a whitespace-only placeholder on ``ref_id``
    (``" "``) is truthy and wins over a real, usable ``client_order_id``
    sitting right beside it, e.g. a nested pagination row
    ``{"ref_id": " ", "client_order_id": "TARGET"}``. Crash recovery reads a
    clean ``None`` match as proof the claimed order was never submitted and
    releases the lease; hiding ``"TARGET"`` behind the blank placeholder let
    a provable page that in fact named the order be scanned to the end and
    misreported as "no match", authorizing a duplicate purchase (Codex
    review, PR #1169: "Validate nested reference aliases before proving
    absence").

    Requires the selected alias to be a USABLE SCALAR, not merely non-blank:
    ``_is_blank_broker_field`` only reads ``None``/``""``/whitespace as
    blank, so a FALSEY non-scalar (``[]``, ``{}``) or a falsey scalar
    (``0``, ``False``) is non-blank and was being selected and stringified
    into an apparently-valid reference (``"[]"``) instead of falling
    through to a valid secondary alias beside it -- the same failure mode
    the whitespace-placeholder fix above addressed, for a different set of
    inputs (Codex review, PR #1169: "Fall through malformed primary
    reference aliases"). `bool` is excluded from the scalar allowlist, same
    as every other order-id validator in this module.

    Also rejects a non-finite float (``nan``/``inf``): truthy and a
    supported scalar type, so it survived the checks above and stringified
    into the apparently-valid reference ``"nan"``, hiding a valid secondary
    alias beside it the same way a blank or malformed primary alias does
    (Codex review, PR #1169: "Reject non-finite reference aliases").

    Also rejects a finite NEGATIVE numeric candidate (``-1``): truthy,
    finite, and a supported scalar type, so it survived every check above
    and stringified into the apparently-valid reference ``"-1"`` -- no
    legitimate broker reference id is ever negative, and hiding a valid
    secondary alias behind one let a provable history scan finish with
    ``None`` and release a crash/whole-share recovery lease, authorizing a
    duplicate purchase (Codex review, PR #1169: "Fall through negative
    reference-ID sentinels").

    The negative rejection also applies when the sentinel is a STRING
    (``ref_id="-1"``), not just a Python ``int``/``float`` -- the
    numeric-typed check alone missed this, so
    ``{"ref_id": "-1", "client_order_id": "TARGET"}`` still stringified
    into the apparently-valid reference ``"-1"``, hiding "TARGET" the
    identical way (Codex review, PR #1168: "Skip stringified
    reference-ID sentinels").

    A stringified ZERO (``ref_id="0"``) is rejected the same way, not
    just a leading ``"-"``: the negative-prefix check alone missed this,
    so ``{"ref_id": "0", "client_order_id": "TARGET"}`` bypassed both the
    falsey and numeric checks and still stringified into the apparently-
    valid reference ``"0"``, hiding "TARGET" behind it -- a provable
    ``strategy_order_with_ref_id`` scan then reported no match, letting
    crash/whole-share recovery release the claim and potentially submit a
    duplicate order (Codex review, PR #1168: "Skip stringified zero
    reference sentinels").

    Also rejects an oversized native numeric candidate (``ref_id=10**5000``):
    truthy, positive, and a supported scalar type, so it survived every
    check above and reached the final ``str(_candidate)`` call -- which,
    for an integer beyond Python's integer-string conversion digit limit,
    raises ``ValueError`` instead of returning a string, aborting this
    loop before it ever reaches a valid alias sitting right beside it
    (``{"ref_id": 10**5000, "client_order_id": "TARGET"}``). Every
    reference-sensitive history/recovery lookup goes through this
    function, so the crash stuck a real recovery claim and blocked
    subsequent placement for that ticker instead of finding "TARGET"
    (Codex review, PR #1169: "Fall through oversized reference-ID
    aliases"). Reused ``_is_representable_numeric_order_id`` rather than a
    narrower check: it already catches this exact case (the ``OverflowError``
    from converting a too-large int to ``float`` inside ``math.isfinite``
    fires well before the string-conversion limit, since anything beyond
    ``float``'s own range is already far past the ~309-digit threshold).
    """
    for _alias in ("ref_id", "client_order_id", "client_order_uuid", "client_id"):
        _candidate = order.get(_alias)
        # A FALSEY non-string sentinel -- `ref_id=0`, `ref_id=False` -- is
        # not blank per `_is_blank_broker_field` (only None/""/whitespace
        # reads as blank), so it used to be accepted here and stringified
        # into the apparently-real reference "0"/"False", stopping this
        # loop before it ever reached a real, usable alias sitting right
        # beside it (`{"ref_id": 0, "client_order_id": "TARGET"}`). In the
        # provable-history recovery methods this hid "TARGET" behind the
        # sentinel, and a clean `None` match on the fabricated "0" was
        # misread as proof the claimed order was never submitted,
        # releasing a crash/whole-share recovery claim and authorizing a
        # duplicate purchase (Codex review, PR #1168: "Skip falsey
        # reference-ID sentinels before aliases"). Also requires the
        # candidate to be an actual SCALAR (str/int/float, never bool) --
        # a falsey non-scalar (`[]`, `{}`) is caught by the same `not
        # _candidate` check, but a TRUTHY non-scalar or a bool sentinel
        # needs its own explicit rejection (Codex review, PR #1169: "Fall
        # through falsey non-scalar/scalar reference aliases"). A
        # genuinely usable STRING is never affected -- `_is_blank_broker_
        # field` already filters out the one falsy string ("") this alias
        # could carry, so every string that survives that check is
        # truthy.
        if (
            _is_blank_broker_field(_candidate)
            or not _candidate
            or isinstance(_candidate, bool)
            or not isinstance(_candidate, (str, int, float))
            or (isinstance(_candidate, (int, float)) and not _is_representable_numeric_order_id(_candidate))
            or (isinstance(_candidate, (int, float)) and _candidate < 0)
            or (isinstance(_candidate, str) and _is_stringified_non_positive_number(_candidate.strip()))
            or (isinstance(_candidate, str) and _is_stringified_non_finite_number(_candidate.strip()))
        ):
            continue
        return str(_candidate).strip()
    return ""


def _usable_ref_id_aliases(order: Dict[str, Any]) -> List[str]:
    """Every USABLE reference-id alias on ``order``, not just the first.

    ``_first_usable_ref_id_alias`` returns only the first alias it finds
    usable, in priority order -- the right answer for stringifying "the"
    reference of a row, but wrong for MATCHING against a specific target:
    a history row can carry more than one usable alias that disagree
    (``{"ref_id": "OTHER", "client_order_id": "TARGET"}``), and a caller
    comparing only the first ("OTHER") against a wanted ref
    ("TARGET") never sees the match sitting right beside it, so a
    provable ``strategy_order_with_ref_id(..., "TARGET")`` scan reports
    no match and releases a crash/whole-share recovery claim, authorizing
    a duplicate purchase (Codex review, PR #1168: "Inspect every usable
    reference-ID alias"). Every caller matching against a specific target
    ref must check membership across ALL of these, not equality against
    just the first.
    """
    aliases: List[str] = []
    for _alias in ("ref_id", "client_order_id", "client_order_uuid", "client_id"):
        _candidate = order.get(_alias)
        if (
            _is_blank_broker_field(_candidate)
            or not _candidate
            or isinstance(_candidate, bool)
            or not isinstance(_candidate, (str, int, float))
            or (isinstance(_candidate, (int, float)) and not _is_representable_numeric_order_id(_candidate))
            or (isinstance(_candidate, (int, float)) and _candidate < 0)
            or (isinstance(_candidate, str) and _is_stringified_non_positive_number(_candidate.strip()))
            or (isinstance(_candidate, str) and _is_stringified_non_finite_number(_candidate.strip()))
        ):
            continue
        aliases.append(str(_candidate).strip())
    return aliases


# Robinhood ``get_equity_historicals`` accepts a fixed set of interval strings; the
# server does not aggregate intermediate bars. Map the strategy's interval-in-minutes
# to the matching Robinhood interval name. 15-minute bars are intentionally absent —
# Robinhood does not offer them, so callers must use a supported granularity.
RH_INTERVAL_BY_MINUTES: Dict[int, str] = {
    1: "minute",
    5: "5minute",
    10: "10minute",
    30: "30minute",
    60: "hour",
}

# Order states that mean "an order already exists for this ticker today" when scanning
# for the one-order-per-ticker-per-day rule. Cancelled/rejected/failed orders never
# consumed capacity and must not block a fresh attempt.
_LIVE_ORDER_STATES = frozenset(
    {"new", "queued", "confirmed", "unconfirmed", "partially_filled", "filled"}
)

# Ceiling on how many ``get_equity_orders`` pages one same-day lookup will follow.
# A single ticker's same-day history is a handful of orders in practice, so this is
# a runaway/cursor-loop backstop, not a real limit; hitting it raises rather than
# returning a truncated history (see ``_iter_same_day_orders``).
_MAX_ORDER_PAGES = 20

# Ceiling on how many nested "data" layers any envelope-descent loop
# (`_unwrap_traversed_layers`, `_unwrap_seeking_order_evidence`,
# `_unwrap_hides_contradicted_nested_data`, `_all_data_layers`) will walk
# through. None of them tracked visited objects or imposed any depth limit,
# so an injected broker callable returning a CYCLIC malformed mapping --
# `response["data"] = response`, easy to construct by accident in a stub or
# on purpose in a hostile one -- made every one of these loops run forever:
# the "not a dict" / "stop descending" exit conditions never fire on a node
# that keeps pointing back to itself. That hangs the live run indefinitely
# instead of failing closed, in every caller built on top of these
# functions (order-history/placement parsing, buying-power/review
# interpretation) (Codex review, PR #1169: "Bound recursive data-layer
# traversal"). A real envelope from any known broker/connector shape this
# module models is at most a handful of layers deep; this is a generous,
# still-effectively-unbounded-for-legitimate-input backstop, exactly the
# role `_MAX_ORDER_PAGES` plays for pagination.
_MAX_ENVELOPE_DESCENT_DEPTH = 50

_ERROR_CONTAINER_KEYS = frozenset(
    {
        "error",
        "errors",
        "error_messages",
        "rejections",
        "alerts",
        "warnings",
        "pre_trade_alerts",
        "messages",
        "checks",
    }
)

_PLACEMENT_EXECUTION_FIELDS = frozenset(
    {
        "cumulative_quantity",
        "executed_quantity",
        "filled_quantity",
        "quantity_filled",
        "executed_notional",
        "executed_dollar_amount",
        "filled_notional",
    }
)

_TERMINAL_NO_ACCEPTANCE_ORDER_STATES = frozenset(
    {"rejected", "failed", "cancelled", "canceled", "voided", "expired"}
)

_FILL_EVIDENCE_POSITIVE_OR_UNREADABLE = "positive_or_unreadable"
_FILL_EVIDENCE_EXPLICIT_ZERO = "explicit_zero"
_FILL_EVIDENCE_NONE_REPORTED = "none_reported"
_MAX_PLACEMENT_RESPONSE_CONTAINERS = 2048


def _placement_response_dicts(value: object) -> Iterator[Dict[str, Any]]:
    """Yield every order-bearing mapping in a placement response envelope."""
    # Do not expose diagnostic IDs on error containers as order IDs, but still
    # descend into them because connectors may nest a real order below one.
    pending = [(value, False)]
    visited = set()
    visited_count = 0
    while pending:
        current, is_direct_error_container = pending.pop()
        if isinstance(current, dict):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            visited_count += 1
            if visited_count > _MAX_PLACEMENT_RESPONSE_CONTAINERS:
                # A response too large to inspect completely cannot prove a
                # placement was rejected or unfilled.  Stop here and let the
                # conservative callers retain ambiguity.
                yield {"_placement_response_incomplete": True}
                return
            # Preserve state/fill evidence from an error envelope.  Its
            # diagnostic `id` is not an order identity, however.
            if is_direct_error_container:
                current = {**current, "_placement_error_container": True}
            yield current
            for key, val in current.items():
                if isinstance(val, (dict, list, tuple)):
                    pending.append((val, key in _ERROR_CONTAINER_KEYS))
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            visited_count += 1
            if visited_count > _MAX_PLACEMENT_RESPONSE_CONTAINERS:
                yield {"_placement_response_incomplete": True}
                return
            for item in current:
                if isinstance(item, (dict, list, tuple)):
                    pending.append((item, is_direct_error_container))


def _is_usable_order_id(order_id: object) -> bool:
    """Whether one candidate value is a usable broker order identifier."""
    if not order_id or isinstance(order_id, bool):
        return False
    if not isinstance(order_id, (str, int, float)):
        return False
    if isinstance(order_id, str) and not order_id.strip():
        return False
    if isinstance(order_id, (int, float)):
        try:
            if not math.isfinite(order_id) or order_id <= 0:
                return False
        except (OverflowError, TypeError, ValueError):
            return False
    return True


def _usable_order_ids(candidate: object) -> List[Any]:
    """Every usable order id one mapping names."""
    if not isinstance(candidate, dict):
        return []
    found = []
    seen = set()
    for key in ("id", "order_id"):
        if key == "id" and candidate.get("_placement_error_container"):
            continue
        order_id = candidate.get(key)
        if not _is_usable_order_id(order_id):
            continue
        identity = (type(order_id).__name__, str(order_id).strip())
        if identity in seen:
            continue
        seen.add(identity)
        found.append(order_id)
    return found


def _placement_response_has_order_id(response: object) -> bool:
    """Return whether any response envelope carries a usable order id."""
    return any(
        bool(_usable_order_ids(envelope))
        for envelope in _placement_response_dicts(response)
    )


def _placement_response_has_untrackable_positive_order_id(response: object) -> bool:
    """Whether a response names a positive numeric ID too large to track safely."""
    for envelope in _placement_response_dicts(response):
        for key in ("id", "order_id"):
            if key == "id" and envelope.get("_placement_error_container"):
                continue
            value = envelope.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                if value > 0 and not math.isfinite(value):
                    return True
            except (OverflowError, TypeError, ValueError):
                # A giant positive Python integer cannot be converted by
                # math.isfinite(), but is still acceptance evidence.
                try:
                    if value > 0:
                        return True
                except (TypeError, ValueError):
                    pass
    return False


def _order_identity_groups(
    envelopes: List[Dict[str, Any]],
) -> List[Tuple[Any, List[Dict[str, Any]]]]:
    """The distinct orders this response names, each with its own envelopes."""
    groups: List[Tuple[Any, List[Dict[str, Any]]]] = []
    by_id: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for envelope in envelopes:
        for order_id in _usable_order_ids(envelope):
            key = (type(order_id).__name__, str(order_id).strip())
            group = by_id.get(key)
            if group is not None:
                group.append(envelope)
                continue
            by_id[key] = [envelope]
            groups.append((order_id, by_id[key]))
    return groups


def _is_finite_zero(value: Any) -> bool:
    """Whether a number is a readable, finite zero."""
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value == 0
    except (OverflowError, TypeError, ValueError):
        return False


def _fill_evidence(envelope: Dict[str, Any]) -> str:
    """What an envelope says about how much of an order executed."""
    saw_explicit_zero = False
    for key in _PLACEMENT_EXECUTION_FIELDS:
        if key not in envelope:
            continue
        value = envelope[key]
        if _is_blank_broker_field(value):
            continue
        if isinstance(value, bool):
            return _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE
        if isinstance(value, (int, float)):
            if _is_finite_zero(value):
                saw_explicit_zero = True
                continue
            return _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                numeric = float(text)
            except (ValueError, OverflowError):
                return _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE
            if _is_finite_zero(numeric):
                saw_explicit_zero = True
                continue
            return _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE
        return _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE
    return _FILL_EVIDENCE_EXPLICIT_ZERO if saw_explicit_zero else _FILL_EVIDENCE_NONE_REPORTED


def _combined_fill_evidence(envelopes: List[Dict[str, Any]]) -> str:
    """_fill_evidence over several envelopes, combined conservatively."""
    verdict = _FILL_EVIDENCE_NONE_REPORTED
    for envelope in envelopes:
        evidence = _fill_evidence(envelope)
        if evidence == _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE:
            return evidence
        if evidence == _FILL_EVIDENCE_EXPLICIT_ZERO:
            verdict = evidence
    return verdict


def _placement_response_proves_zero_execution(response: object) -> bool:
    """Return true only when every represented order explicitly reports zero execution."""
    envelopes = list(_placement_response_dicts(response))
    if not envelopes:
        return False

    identities = _order_identity_groups(envelopes)

    # An envelope that names two distinct orders cannot attribute its state or
    # fill fields to either one.  Do not reuse one zero-fill proof for both.
    if any(len(_usable_order_ids(envelope)) > 1 for envelope in envelopes):
        return False

    # Every named order must report a terminal non-acceptance state.
    for _, group in identities:
        states = [
            envelope.get("state") for envelope in group if envelope.get("state") is not None
        ]
        if not states:
            return False
        for state in states:
            if not (
                isinstance(state, str)
                and state.strip().lower() in _TERMINAL_NO_ACCEPTANCE_ORDER_STATES
            ):
                return False

    unattributed = [e for e in envelopes if not _usable_order_ids(e)]
    unattributed_evidence = _combined_fill_evidence(unattributed)
    if unattributed_evidence == _FILL_EVIDENCE_POSITIVE_OR_UNREADABLE:
        return False

    if identities:
        share_unattributed = len(identities) == 1
        for _, group in identities:
            scope = group + unattributed if share_unattributed else group
            if _combined_fill_evidence(scope) != _FILL_EVIDENCE_EXPLICIT_ZERO:
                return False
        return True

    # No order identities were named in the response.
    if unattributed_evidence != _FILL_EVIDENCE_EXPLICIT_ZERO:
        return False
    for e in unattributed:
        state = e.get("state")
        if state is not None:
            if not (
                isinstance(state, str)
                and state.strip().lower() in _TERMINAL_NO_ACCEPTANCE_ORDER_STATES
            ):
                return False
    return True


def _placement_response_has_acceptance_evidence(response: object) -> bool:
    """Return whether a failure envelope may nevertheless describe accepted spend."""
    for envelope in _placement_response_dicts(response):
        if envelope.get("_placement_response_incomplete") is True:
            return True
        state = str(envelope.get("state", "")).strip().lower()
        if state in _LIVE_ORDER_STATES:
            return True
        for key in _PLACEMENT_EXECUTION_FIELDS:
            if key not in envelope or _is_blank_broker_field(envelope[key]):
                continue
            if isinstance(envelope[key], bool):
                return True
            try:
                value = float(envelope[key])
            except (TypeError, ValueError, OverflowError):
                return True
            if not math.isfinite(value) or value != 0:
                return True
    return False


# Review-endpoint `state` strings recognized as an explicit APPROVAL --
# used by `_interpret_review`'s per-layer blocking scan as an ALLOWLIST,
# not a denylist. Checking only `{"rejected", "failed"}` as blocking left
# every OTHER negative or unrecognized state string (`"denied"`,
# `"blocked"`, a broker-specific spelling this module has never seen) fall
# through as non-blocking, approving a live order on a review verdict this
# module never actually understood the meaning of (Codex review, PR #1169:
# "Reject unsupported review states"). An unrecognized state is treated
# the same fail-closed way an unrecognized response SHAPE already is.
_REVIEW_APPROVED_STATES = frozenset({"approved", "confirmed", "accepted", "cleared", "ok", "passed"})


def _next_order_cursor(data: Dict[str, Any], current_cursor: Optional[str] = None) -> Optional[str]:
    """Extract the next-page cursor from an unwrapped orders page, or None on the last page.

    Mirrors ``scripts/broker_reconciliation._next_cursor``: an explicit
    ``next_cursor``, or the ``cursor`` query param of the MCP ``next`` URL
    (``https://.../orders/?cursor=ABC``). A bare ``cursor`` field is read LAST
    because some APIs echo the CURRENT page's cursor there, and a bare cursor
    EQUAL to ``current_cursor`` (the cursor this very page was requested with)
    is exactly that echo -- the end of the list, not a continuation. Reading it
    as a continuation made the caller detect a repeated cursor and raise, so an
    absence check over a paginated history failed closed on a perfectly good
    final page, freezing crash leases and blocking valid buys (Codex review:
    "Treat an echoed cursor as the end of pagination").
    """
    direct_next = data.get("next_cursor")
    if isinstance(direct_next, str) and direct_next.strip():
        return direct_next.strip()
    next_url = data.get("next")
    if isinstance(next_url, str) and next_url.strip():
        cursor_values = parse_qs(urlparse(next_url).query).get("cursor")
        if cursor_values and cursor_values[0].strip():
            return cursor_values[0].strip()
        # A next URL with no cursor param -- or an empty/whitespace one, the
        # same end-of-list convention checked for next_cursor/cursor above --
        # is still a "there is more" signal we cannot follow deterministically;
        # _has_continuation_pointer below turns it into a raise rather than
        # letting a blank cursor be treated as a real continuation.
        return None
    direct = data.get("cursor")
    if isinstance(direct, str):
        stripped_direct = direct.strip()
        # Compare the STRIPPED value against current_cursor: current_cursor
        # was itself normalized by this same function on the prior call, so
        # a response that echoes it back with incidental surrounding
        # whitespace (e.g. " ABC " for a page requested with "ABC") is still
        # the same echo -- the end of the list, not a continuation. Comparing
        # the raw value made that echo look like a fresh cursor, which
        # _iter_same_day_orders then rejected as a repeated cursor instead of
        # ending pagination cleanly, failing closed on a perfectly good final
        # page (Codex review: "Normalize the cursor before checking for an
        # echo").
        if stripped_direct and stripped_direct != current_cursor:
            return stripped_direct
    return None


def _has_unusable_direct_cursor_alias(data: Dict[str, Any]) -> bool:
    """True when this layer's own ``next``/``next_cursor``/``cursor`` field
    is a PRESENT but malformed (non-string) pointer -- independent of
    whether ``_next_order_cursor`` found a DIFFERENT, usable alias on this
    same layer to follow instead.

    ``_next_order_cursor`` reads ``next_cursor``, then the ``next`` URL's
    own ``cursor`` query param, then a bare ``cursor``, returning the
    FIRST usable one it finds and silently skipping any earlier alias
    that is present but not a string. ``{"next_cursor": 0, "next":
    "...?cursor=P2"}`` and ``{"next_cursor": "P2", "cursor": 0}`` both
    resolve to a usable "P2" without this function's check, so the
    caller's existing unusable-pointer check -- gated on ``_layer_cursor
    is None`` (see ``_iter_same_day_orders``) -- never even runs for
    either layer, and the malformed sibling alias (``next_cursor: 0`` /
    ``cursor: 0``) is discarded unnoticed. A page can then be followed to
    completion while one of its layers advertised a SECOND continuation
    signal this call could not interpret, letting a truncated history
    prove a same-day reference absent and authorize a duplicate purchase
    (Codex review, PR #1168: "Reject malformed direct cursors beside
    usable aliases").

    Only a NON-STRING present value is flagged, mirroring
    ``_has_continuation_pointer``'s own malformed-pointer detection: a
    blank/whitespace-only STRING alias is the ordinary, legitimate
    "nothing here, try the next alias" convention ``_next_order_cursor``
    itself falls through on, not a malformed sibling worth rejecting.
    """
    for _key in ("next", "next_cursor", "cursor"):
        _value = data.get(_key)
        if _value is not None and not isinstance(_value, str):
            return True
    return False


def _has_unusable_next_url_cursor(data: Dict[str, Any]) -> bool:
    """True when a layer's ``next`` URL is present but its own cursor is unusable.

    ``_next_order_cursor`` selects only the FIRST usable alias it finds
    (``next_cursor``, then the ``next`` URL's ``cursor`` query param, then a
    bare ``cursor``) -- so a layer carrying BOTH a valid ``next_cursor="P2"``
    AND a ``next`` URL with no (or a blank) ``cursor`` query param returns
    "P2" and looks entirely usable: the caller never learns that the SAME
    layer also advertised a second continuation it could not interpret.
    Because the earlier `_all_order_cursor_aliases`-based conflict check only
    ever collects USABLE aliases, an unusable one never enters that
    comparison either -- it simply vanishes. The scan then follows "P2" as
    if it were the layer's only continuation signal and can prove a
    same-day reference absent from a page that, per its own ``next`` URL,
    was never actually the last one -- authorizing a duplicate purchase
    (Codex review, PR #1169: "Reject unusable cursor aliases beside a valid
    one"). Checked independently of whatever ``_next_order_cursor`` selected
    for this layer, not only when it returned ``None``.

    Every value the ``cursor`` query parameter repeats is checked, not just
    the first: ``parse_qs`` returns every occurrence of a repeated query
    key as a list, and ``_next_order_cursor`` (like this function's own
    first version) only ever reads index ``[0]`` to select ONE cursor to
    follow. A URL like ``"...?cursor=P2&cursor=%20"`` supplies a usable
    first value ("P2") alongside a second, blank one this function used to
    never inspect -- the SAME "there is more we cannot fully interpret"
    signal a wholly-unusable ``next`` URL already raises for, just hidden
    behind a usable sibling instead of the whole field (Codex review, PR
    #1169: "Validate every cursor value in next URLs").
    """
    next_url = data.get("next")
    if not (isinstance(next_url, str) and next_url.strip()):
        return False
    # `keep_blank_values=True`, not the default `False`: `parse_qs`
    # otherwise DISCARDS an empty occurrence of a repeated query key
    # entirely, so a malformed URL like "...?cursor=P2&cursor=" parsed to
    # only `["P2"]` -- the truly blank second value vanished before this
    # function ever got a chance to flag it, and the pagination scan
    # treated the URL as fully usable and followed "P2" alone, letting a
    # provable-absence scan skip the unusable continuation value entirely
    # (Codex review, PR #1168: "Preserve empty cursor query values during
    # validation").
    cursor_values = parse_qs(urlparse(next_url).query, keep_blank_values=True).get("cursor")
    if not cursor_values:
        return True
    return any(not value.strip() for value in cursor_values)


def _all_order_cursor_aliases(data: Dict[str, Any], current_cursor: Optional[str] = None) -> List[str]:
    """Every usable continuation-cursor alias present on this ONE layer.

    :func:`_next_order_cursor` deliberately returns only the FIRST usable
    alias it finds (``next_cursor``, then the ``next`` URL's ``cursor``
    query param, then a bare ``cursor``) -- the right behavior for
    selecting the cursor to actually follow, but wrong for detecting a
    conflict: a single layer carrying BOTH ``next_cursor="P2"`` and a
    ``next`` URL whose cursor is ``"P3"`` returned only ``"P2"``, so the
    cross-layer conflict check below never saw ``"P3"`` at all and the
    scan followed P2 while a live same-day order sat on P3, proving a
    reference absent from a truncated view of the real history (Codex
    review, PR #1168: "Reject conflicting cursor aliases within one
    layer"). Collects every alias this same layer supports, in the same
    order and using the same echo/blank rules `_next_order_cursor`
    applies to each, so a caller comparing the full set catches an
    intra-layer conflict the single-value function alone cannot.
    """
    aliases: List[str] = []
    direct_next = data.get("next_cursor")
    if isinstance(direct_next, str) and direct_next.strip():
        aliases.append(direct_next.strip())
    next_url = data.get("next")
    if isinstance(next_url, str) and next_url.strip():
        cursor_values = parse_qs(urlparse(next_url).query).get("cursor")
        # EVERY usable value, not just the first: a malformed URL with
        # more than one distinct `cursor` query parameter
        # (`?cursor=P2&cursor=P3`) previously kept only `cursor_values[0]`
        # ("P2"), so the cross-layer conflict check below never saw "P3"
        # at all -- the pagination scan followed P2 while a live same-day
        # order sat on P3, letting a recovery caller treat that truncated
        # result as proof the claimed order was absent and release the
        # claim, authorizing a duplicate purchase (Codex review, PR
        # #1168: "Reject conflicting cursor values in a next URL").
        for _cursor_value in cursor_values or []:
            if isinstance(_cursor_value, str) and _cursor_value.strip():
                aliases.append(_cursor_value.strip())
    direct = data.get("cursor")
    if isinstance(direct, str):
        stripped_direct = direct.strip()
        if stripped_direct and stripped_direct != current_cursor:
            aliases.append(stripped_direct)
    return aliases


def _canonicalized_order_row(row: Any) -> Any:
    """``row`` with its BROKER order-identifier, reference-alias, and
    lifecycle fields canonicalized.

    A connector can mirror the identical broker order across two
    envelope layers while spelling its ``id``/``order_id`` field in a
    different SUPPORTED numeric encoding on each -- ``{"id": 123, ...}``
    directly and ``{"id": "123", ...}`` nested. Used only for the
    row-collection AGREEMENT check just below, via
    :func:`_normalized_order_identifier` -- the same canonicalizer this
    module already trusts for comparing a singular order's own ``id``/
    ``order_id`` aliases (Codex review, PR #1169: "Compare redundant
    order collections semantically").

    ``ref_id``/``client_order_id``/``client_order_uuid``/``client_id``
    are OPAQUE client-supplied references, not broker order identifiers,
    so their CONTENT is never numerically canonicalized the way ``id``/
    ``order_id`` are -- ``ref_id="001"`` and ``ref_id="1"`` stay two
    DISTINCT client references that merely happen to look numeric, not
    two encodings of the same value. Numerically canonicalizing the
    content declared a row naming the wanted reference "equivalent" to
    one naming an unrelated reference, letting the DIRECT collection's
    row win and silently discarding the NESTED row actually matching the
    caller's target -- ``strategy_order_with_ref_id()`` then reported a
    clean absence and released a crash/whole-share recovery claim for an
    order that may still exist (Codex review, PR #1169: "Preserve opaque
    reference IDs in collection comparisons"). What IS collapsed is the
    four alias KEY NAMES themselves, via :func:`_usable_ref_id_aliases` --
    see the dedicated comment on that step below for why redundant rows
    naming the identical reference through two different alias fields
    (``ref_id="TARGET"`` vs. ``client_order_id="TARGET"``) used to compare
    unequal purely on key spelling.

    ``id`` and ``order_id`` are also collapsed onto ONE unified ``"id"``
    key when they do NOT genuinely conflict (see below), not left as two
    separately-canonicalized keys: two rows describing the identical
    broker order but spelling it through different SUPPORTED aliases --
    ``{"id": "123"}`` directly and ``{"order_id": "123"}`` nested --
    previously canonicalized to two dicts with different KEY NAMES, which
    still compare unequal as dicts even though their identifier VALUES
    agree, so a settled pagination layer reported a spurious conflict and
    ``_iter_same_day_orders()`` rejected an otherwise-valid history page
    (Codex review, PR #1169: "Treat id and order_id as equivalent
    collection keys").

    A row whose OWN ``id``/``order_id`` aliases genuinely DISAGREE --
    ``{"id": "A", "order_id": "B"}``, checked via
    :func:`_order_identifier_aliases_conflict` -- is the one case NOT
    collapsed: both normalized aliases are kept under their own distinct
    keys instead, so a sibling row naming only one of them can never
    structurally match this one (Codex review, PR #1168: "Preserve
    conflicting identifier aliases during comparison").

    That unification falls through an UNUSABLE primary to the secondary
    alias, via :func:`_is_unusable_live_placement_order_id` -- not a bare
    :func:`_first_usable_order_id_alias`, which only screens for blank/
    falsey, not full usability: ``{"id": true, "order_id": "REAL"}``
    selected the truthy boolean ``True`` via that bare selector, and this
    canonicalized it to ``"True"`` while a sibling row spelling the same
    order as ``{"order_id": "REAL"}`` (no unusable "id" of its own)
    canonicalized to ``"REAL"`` -- two canonical strings for one order,
    reported as a conflict, even though every other id-selection path in
    this module already falls through this exact sentinel to the usable
    secondary (Codex review, PR #1169: "Fall through unusable IDs when
    canonicalizing collections").

    ``state``/``status`` are collapsed the SAME way ``id``/``order_id``
    are, via :func:`_normalize_broker_lifecycle_alias` -- which also
    folds the "cancelled" spelling onto "canceled" -- and guarded by the
    identical :func:`_order_lifecycle_aliases_conflict` conflict check: a
    redundant row spelling the same lifecycle through the other key name
    (``{"id": "1", "state": "queued"}`` vs. ``{"id": "1", "status":
    "queued"}``) previously canonicalized to two dicts differing purely
    on which key held the value, and even a shared key name still
    disagreed on the terminal spelling alone (``state="cancelled"`` vs.
    ``state="canceled"``) -- both false conflicts rejected an otherwise
    completely redundant collection pair the identical way an un-collapsed
    id/order_id pair used to (Codex review, PR #1169: "Normalize lifecycle
    aliases in collection rows"). A row whose OWN ``state``/``status``
    genuinely disagree (``{"state": "queued", "status": "rejected"}``) is
    preserved as a real conflict, not silently collapsed to whichever
    alias this function happens to prefer, mirroring the id/order_id
    conflict-preservation just above.
    """
    if not isinstance(row, dict):
        return row
    canonical = dict(row)
    if "id" in canonical or "order_id" in canonical:
        _id_alias = row.get("id")
        _order_id_alias = row.get("order_id")
        _id_alias_is_unusable = _is_blank_broker_field(_id_alias) or _is_unusable_live_placement_order_id(
            _id_alias
        )
        # `_order_identifier_aliases_conflict` itself already skips a
        # KNOWN-UNUSABLE alias (Codex review, PR #1169: "Skip unusable
        # aliases in the identity-conflict check") -- an unusable sentinel
        # like a boolean `id=True` normalizes to `None`, which that
        # function no longer treats as disagreeing with a genuinely usable
        # sibling `order_id="REAL"`, so `{"id": True, "order_id": "REAL"}`
        # falls through to the ordinary else branch below instead of being
        # wrongly preserved as a genuine conflict.
        if _order_identifier_aliases_conflict(row):
            # A row whose OWN "id"/"order_id" aliases DISAGREE -- e.g.
            # {"id": "A", "order_id": "B"} -- is not safe to collapse
            # onto a single unified "id" key the way an ordinary
            # single-alias (or agreeing-alias) row is: collapsing via
            # `_first_usable_order_id_alias` picks only the PREFERRED
            # alias ("A"), silently discarding "B" entirely. A DIRECT
            # collection's row carrying only {"id": "A"} then
            # canonicalizes to the IDENTICAL dict this conflicted nested
            # row does, and the multiset comparison calls them agreeing
            # -- erasing the nested row's own conflicting "B" identity.
            # If the direct row proves "A" rejected, whole-share/crash
            # recovery can release its claim without ever accounting for
            # the possibly-live order "B" actually names (Codex review,
            # PR #1168: "Preserve conflicting identifier aliases during
            # comparison"). Both aliases are normalized and kept under
            # their OWN distinct keys instead, so a row with only one of
            # them can never structurally match this one.
            canonical["id"] = _normalized_order_identifier(canonical.get("id"))
            canonical["order_id"] = _normalized_order_identifier(canonical.get("order_id"))
        else:
            # Falls through an UNUSABLE primary to the secondary alias,
            # via `_is_unusable_live_placement_order_id` -- not a bare
            # `_first_usable_order_id_alias`, which only screens for
            # blank/falsey, not full usability: `{"id": true, "order_id":
            # "REAL"}` selected the truthy boolean `True` via that bare
            # selector, and this canonicalized it to "True" while a
            # sibling row spelling the same order as `{"order_id":
            # "REAL"}` (no unusable "id" of its own) canonicalized to
            # "REAL" -- two canonical strings for one order, reported as
            # a conflict (Codex review, PR #1169: "Fall through unusable
            # IDs when canonicalizing collections").
            canonical.pop("id", None)
            canonical.pop("order_id", None)
            _selected_id = _id_alias
            if _id_alias_is_unusable:
                _selected_id = _order_id_alias
            canonical["id"] = _normalized_order_identifier(_selected_id)
    # Reference-alias fields get a TYPE-only normalization -- via
    # `_safe_order_id_str`, never numeric canonicalization -- so a
    # type-only encoding difference (`ref_id=123` vs `ref_id="123"`) no
    # longer trips this row-agreement check: `_usable_ref_id_aliases()`
    # (the actual reference-matching helper elsewhere in this module)
    # already converts both supported scalar encodings to the identical
    # string "123" for matching, but this row-canonicalization function
    # left them as a native int and a str respectively, comparing unequal
    # as dict values even though the module's own reference semantics
    # already treat them as the same reference. Two GENUINELY different
    # opaque string spellings (`ref_id="001"` vs `ref_id="1"`) are left
    # exactly as different as before -- `str("001").strip() == "001"` and
    # `str("1").strip() == "1"` never collide -- only the encoding TYPE is
    # normalized away, not the reference's own content (Codex review, PR
    # #1169: "Normalize reference alias types before comparing rows").
    # Routed through `_safe_order_id_str`, not a bare `str(value).strip()`:
    # a native numeric reference alias beyond
    # `_is_representable_numeric_order_id`'s own bound (e.g. a malformed
    # `ref_id: 10**5000`) raised `ValueError` the instant this loop tried
    # to stringify it, aborting the entire row-agreement comparison over
    # an already-broker-accepted response (Codex review, PR #1169: "Guard
    # oversized reference aliases before stringifying"). `_safe_order_id_
    # str` returns `None` for exactly that case, so the field is simply
    # left as its original (unrepresentable) value rather than crashing --
    # it still cannot equal any OTHER row's representable value, which is
    # the correct, conservative outcome for a value that cannot even be
    # rendered.
    #
    # The four alias fields are also collapsed onto ONE unified "ref_id"
    # key when the row carries exactly one USABLE reference among them --
    # e.g. `{"client_order_id": "TARGET"}` and `{"ref_id": "TARGET"}`
    # canonicalize identically, mirroring the id/order_id unification
    # above. `_usable_ref_id_aliases()` -- the same helper every reference-
    # matching lookup in this module already treats these four fields as
    # INTERCHANGEABLE through -- previously had no counterpart here: two
    # rows naming the identical target reference through two different
    # (but both usable) alias fields canonicalized to two dicts with
    # different KEY NAMES, comparing unequal even though the module's own
    # reference semantics treat them as the same alias family (Codex
    # review, PR #1169: "Collapse equivalent reference aliases in
    # collection rows"). A row with ZERO or MULTIPLE disagreeing usable
    # references is not safe to collapse -- the former has nothing proven
    # to unify onto, and the latter would silently discard one of two
    # genuinely different, both-usable references the same way the id/
    # order_id conflict case above is careful not to (a naive "merge every
    # usable value into one set" collapse would instead have let two rows
    # that SWAP the same two values across two different alias fields --
    # `{"ref_id": "A", "client_order_id": "B"}` vs. `{"ref_id": "B",
    # "client_order_id": "A"}` -- compare equal, since both produce the
    # identical set; kept per-field here specifically to avoid that) -- so
    # both are left under their own original keys, each still individually
    # type-normalized.
    _ref_keys = ("ref_id", "client_order_id", "client_order_uuid", "client_id")
    if any(_ref_key in canonical for _ref_key in _ref_keys):
        _usable_ref_values = set(_usable_ref_id_aliases(row))
        if len(_usable_ref_values) == 1:
            for _ref_key in _ref_keys:
                canonical.pop(_ref_key, None)
            canonical["ref_id"] = next(iter(_usable_ref_values))
        else:
            for _ref_key in _ref_keys:
                _ref_value = canonical.get(_ref_key)
                if isinstance(_ref_value, (str, int, float)) and not isinstance(_ref_value, bool):
                    _safe_ref_value = _safe_order_id_str(_ref_value)
                    if _safe_ref_value is not None:
                        canonical[_ref_key] = _safe_ref_value
    # `state`/`status` are unified the same way `id`/`order_id` are above:
    # collapsed onto one canonical `"state"` key when they do not
    # genuinely conflict (checked via `_order_lifecycle_aliases_conflict`,
    # the same self-conflict predicate `_order_rows_contradict` already
    # uses, normalized via `_normalize_broker_lifecycle_alias` so
    # "cancelled" and "canceled" read as the identical terminal state, not
    # two disagreeing dialects) -- otherwise two rows naming the identical
    # lifecycle through different alias KEYS (`{"state": "filled"}` vs.
    # `{"status": "filled"}`) canonicalized to different dicts and a
    # provably-agreeing pagination layer was reported as a spurious
    # conflict (Codex review, PR #1169: "Normalize lifecycle aliases in
    # collection rows"). A row whose own `state`/`status` genuinely
    # disagree keeps both, normalized, under their own distinct keys --
    # the same conflict-preservation the id/order_id branch above applies.
    if "state" in canonical or "status" in canonical:
        if _order_lifecycle_aliases_conflict(row):
            canonical["state"] = _normalize_broker_lifecycle_alias(canonical.get("state"))
            canonical["status"] = _normalize_broker_lifecycle_alias(canonical.get("status"))
        else:
            canonical.pop("state", None)
            canonical.pop("status", None)
            canonical["state"] = _normalize_broker_lifecycle_alias(
                row.get("state")
            ) or _normalize_broker_lifecycle_alias(row.get("status"))
    return canonical


def _dict_order_independent_sort_key(value: Any) -> Any:
    """A sort key for ``value`` that never depends on a dict's insertion order.

    Plain ``repr(dict)`` renders a dict's items in INSERTION order, not
    sorted order -- two dicts with identical key/value pairs but different
    insertion order (``{"id": "1", "state": "z"}`` vs. the same pair
    inserted ``state`` first) produce different repr strings, so sorting a
    row list by ``key=repr`` split them into a different relative order
    and the two canonicalized row lists no longer compared ``==`` even
    though the underlying collections agree completely (Codex review, PR
    #1168/#1169: "Compare row multisets independently of dictionary key
    order" / "Canonicalize mapping key order before multiset sorting" --
    two independently-authored fixes for the identical gap). Recurses
    into nested dicts/lists so the same insertion-order independence
    holds at any depth, not just the top level -- a bare ``repr(sorted(
    row.items()))`` on the row alone still depends on insertion order one
    level down, inside any NESTED dict value the row carries.

    Every ``repr()`` call here goes through :func:`_log_safe_repr`, not a
    bare ``repr()``: a redundant broker-order row can carry a native int
    beyond Python's integer-to-string conversion digit limit in an
    ORDINARY, non-identifier field this function has no special handling
    for (``{"id": "1", "quantity": 10**5000}``) -- a bare ``repr(value)``
    on that scalar raises ``ValueError`` the instant this sort key is
    built, and ``_order_collections_semantically_agree`` only ever catches
    ``TypeError`` from its ``sorted()`` call, so that ``ValueError``
    propagated straight out of it and aborted history parsing for a
    caller (``_iter_same_day_orders`` in particular) even when the two
    collections being compared were otherwise completely identical (Codex
    review, PR #1169: "Avoid unsafe repr in collection sort keys").
    ``_log_safe_repr`` already exists in this module for the identical
    class of failure on a diagnostic logging path; reused here rather than
    duplicated, since a placeholder sort key costs nothing but the (already
    accepted, diagnostics-only) loss of that one oversized value's own
    ordering precision -- the surrounding equality check further down still
    compares the actual canonicalized rows, not just this key.
    """
    if isinstance(value, dict):
        return (
            "dict",
            tuple(
                sorted((_log_safe_repr(k), _dict_order_independent_sort_key(v)) for k, v in value.items())
            ),
        )
    if isinstance(value, list):
        return ("list", tuple(_dict_order_independent_sort_key(v) for v in value))
    return ("scalar", _log_safe_repr(value))


def _order_collections_semantically_agree(a: List[Any], b: List[Any]) -> bool:
    """Whether two order-row lists describe the identical set of orders.

    A raw ``==`` comparison between two "orders" collections at
    different envelope layers declares them conflicting whenever either
    (1) a shared row spells an identifier field in a different SUPPORTED
    numeric encoding (``id=123`` vs. ``id="123"``), or (2) the two
    layers simply list the identical rows in a different order -- neither
    of which is a genuine disagreement about which orders exist. Treating
    either as a real conflict fails a provable page closed and can
    release a crash/whole-share recovery claim or block same-day-order
    detection over what is, in fact, complete agreement (Codex review, PR
    #1169: "Compare redundant order collections semantically"). Rows are
    canonicalized (see :func:`_canonicalized_order_row`) and compared as
    a MULTISET, not positionally, so row order never matters; a row shape
    ``sorted()``/``repr()`` cannot handle (nested unhashable/uncomparable
    structures beyond a plain dict) falls back to the original strict
    list equality rather than raising. Sorted via
    :func:`_dict_order_independent_sort_key`, not bare ``repr()`` -- see
    its own docstring for why a plain repr-based sort key falsely
    disagreed on two dicts differing only in insertion order.

    The sort itself is also guarded against ``ValueError``, not just
    ``TypeError``: ``_dict_order_independent_sort_key`` already routes its
    own scalar rendering through :func:`_log_safe_repr` to avoid this, but
    a defensive fallback here keeps this comparison from ever aborting
    page/history parsing over an unrepresentable value this module's own
    canonicalization missed, the same fail-safe posture every other
    oversized-numeric path in this module already takes.
    """
    if len(a) != len(b):
        return False
    try:
        _canon_a = sorted((_canonicalized_order_row(_row) for _row in a), key=_dict_order_independent_sort_key)
        _canon_b = sorted((_canonicalized_order_row(_row) for _row in b), key=_dict_order_independent_sort_key)
    except (TypeError, ValueError):
        return a == b
    return _canon_a == _canon_b


def _orders_and_cursor_layers_in_settled_layer_or_nested(
    data: Dict[str, Any],
) -> Tuple[Any, List[Dict[str, Any]]]:
    """``(orders, layers)`` -- the same rows :func:`_orders_in_settled_layer_or_nested`
    returns, plus every LAYER traversed to reach them, row-bearing layer
    FIRST and the outer settled ``data`` LAST.

    Any layer along that traversal chain -- not just the row-bearing layer
    itself, and not just the outer settled layer -- can carry its OWN
    continuation pointer: ``{"orders": [], "data": {"next_cursor": "P3",
    "data": {"orders": [...]}}}}`` has the settled OUTER layer's own
    ``"orders": []`` (a stale placeholder, not the real rows), the row-
    bearing layer two levels down carrying no cursor of its own, and the
    ONE genuine ``next_cursor="P3"`` sitting on the INTERVENING layer
    between them -- checking only the row-bearing layer and the outer
    settled layer (as an earlier fix did) skipped that intervening layer
    entirely and lost the real continuation pointer (Codex review, PR
    #1168: "Carry intervening pagination cursors forward" -- the fresh-
    evidence follow-up to "Preserve nested continuation pointers with
    nested orders", which fixed the identical gap for the 2-layer case).
    Returning every traversed layer, innermost first, lets the caller
    check each one in turn for a cursor -- the ordinary case (rows and
    cursor on the same layer, and the one every existing pagination test
    already covers) is unaffected, since there ``layers == [data]``.

    A NON-EMPTY direct ``"orders"`` is real, positive evidence on its own --
    returned immediately, no need to look further. A direct value that is
    EITHER absent (``None``) OR an empty list is not trusted as final on its
    own: a response can carry a stale/placeholder ``"orders": []`` directly
    on the settled layer while the actual rows sit in its own nested
    ``"data"`` chain, e.g. ``{"next_cursor": "P2", "orders": [], "data":
    {"orders": [{"id": "LIVE", ...}]}}`` -- returning the direct empty list
    here discarded that nested live order (Codex review, PR #1169: "Inspect
    nested rows before accepting an empty direct collection").
    """
    orders = data.get("orders")
    if isinstance(orders, list) and orders:
        # Still walks the nested "data" chain below `data` for cursor
        # evidence, even though the rows themselves are already found
        # here: a page can carry its rows directly while its OWN
        # continuation sits one or more wrappers deeper, e.g. `{"orders":
        # [...], "data": {"next_cursor": "P2"}}}`. Returning only `[data]`
        # made that nested next_cursor invisible to the caller's cursor
        # scan, silently ending pagination as though this were the final
        # page (Codex review, PR #1169: "Inspect nested cursors even with
        # direct rows"). Purely a cursor search here -- `orders` is
        # already settled and never overwritten by anything found below.
        _cursor_only_layers: List[Dict[str, Any]] = []
        _cursor_node: Any = data.get("data")
        _cursor_depth = 0
        _cursor_depth_limit_reached = False
        while isinstance(_cursor_node, dict):
            if _cursor_depth >= _MAX_ENVELOPE_DESCENT_DEPTH:
                _cursor_depth_limit_reached = True
                break
            _nested_singular_order_for_cursor_scan = _cursor_node.get("order")
            if isinstance(_nested_singular_order_for_cursor_scan, dict) and _nested_singular_order_for_cursor_scan:
                # A layer visited ONLY for its cursor can still carry its
                # own non-blank SINGULAR "order" -- {"orders": [other],
                # "data": {"order": {"id": "LIVE", "ref_id": "TARGET",
                # "state": "queued"}}} -- the reverse nesting of the
                # sibling "singular order beside a nested collection"
                # case: here the DIRECT layer holds the list and a layer
                # below it holds the singular order, and this loop only
                # ever read a nested layer's "orders" key, silently
                # ignoring "order" entirely. `strategy_order_with_ref_id`
                # then read only the direct ["other"] row, missing
                # "TARGET" and releasing a crash/whole-share recovery
                # claim for an order the response itself named (Codex
                # review, PR #1169: "Reconcile nested singular orders
                # before trusting direct rows"). Failed closed here the
                # same way the sibling case is, rather than silently
                # discarding this layer's own evidence.
                return "nested singular order coexists with a direct order collection", [data]
            if _nested_singular_order_for_cursor_scan is not None and not isinstance(
                _nested_singular_order_for_cursor_scan, dict
            ):
                # A malformed (present but non-dict, e.g. a truncated
                # string) nested singular "order" is unreadable evidence
                # too, exactly like the malformed-nested-"orders"-value
                # check just below: this loop's own non-blank-dict check
                # just above only catches a genuine (or falsey) dict, so
                # `{"orders": [A], "data": {"order": "truncated"}}` fell
                # through here silently, leaving the direct ["A"] rows
                # alone provable even though part of this response's own
                # order evidence was never actually readable (Codex
                # review, PR #1169: "Reject malformed singular orders
                # during nested cursor scans"). Failed closed the same
                # way every other malformed/conflicting shape in this
                # scan already is.
                return "malformed nested singular order collection", [data]
            _nested_orders_for_cursor_scan = _cursor_node.get("orders")
            if _nested_orders_for_cursor_scan is not None and not isinstance(
                _nested_orders_for_cursor_scan, list
            ):
                # A malformed "orders" value on a NESTED layer -- present,
                # but not a list -- is unreadable evidence too, exactly
                # like the malformed-DIRECT-value check just above: this
                # loop only ever walked nested layers for their cursor
                # info, silently ignoring their own "orders" key entirely,
                # so a settled page with valid direct rows next to a
                # nested layer whose OWN order collection was truncated/
                # malformed still read as fully provable. `strategy_order_
                # with_ref_id()` could then return None for a missing ref
                # even though part of the page's evidence was never
                # actually readable (Codex review, PR #1168: "Reject
                # malformed nested order collections"). Fails the whole
                # page closed -- the already-found direct rows are
                # discarded, not trusted alone -- for
                # `_is_provable_orders_page`'s own `isinstance(...,
                # list)` check to reject.
                return _nested_orders_for_cursor_scan, [data]
            if (
                isinstance(_nested_orders_for_cursor_scan, list)
                and _nested_orders_for_cursor_scan
                and not _order_collections_semantically_agree(_nested_orders_for_cursor_scan, orders)
            ):
                # A NESTED layer with its own non-empty "orders" list that
                # DISAGREES with the direct one -- {"orders": [other],
                # "data": {"orders": [matching_order]}} -- used to be
                # walked purely for cursor evidence, silently discarding
                # its genuinely different rows in favor of treating the
                # direct list as the sole authority.
                # `strategy_order_with_ref_id()` could then return `None`
                # for a ref that only appears in the discarded nested
                # list, releasing a crash/whole-share recovery claim and
                # authorizing a duplicate purchase (Codex review, PR
                # #1168: "Reject conflicting nested order lists"). Two
                # non-empty order collections at different layers that
                # do not even agree on their CONTENT is unreconcilable,
                # ambiguous evidence -- failed closed here the same way a
                # malformed nested value is, rather than silently picking
                # one side.
                return "conflicting nested order collections", [data]
            _cursor_only_layers.append(_cursor_node)
            _cursor_node = _cursor_node.get("data")
            _cursor_depth += 1
        if _cursor_depth_limit_reached:
            # This cursor-only scan hit `_MAX_ENVELOPE_DESCENT_DEPTH`
            # before exhausting the chain, even though `orders` ALREADY
            # holds real, positive direct evidence -- a cyclic or
            # pathologically deep envelope this loop deliberately refuses
            # to follow forever. Returning the already-found direct rows
            # here (as the ordinary natural-end case does) marked the
            # page fully provable even though a continuation cursor -- or
            # a matching order on a deeper layer this scan never reached
            # -- can still exist beyond the cap, letting
            # `strategy_order_with_ref_id()` return `None` for a
            # reference hidden below the limit and release a crash/
            # whole-share recovery claim to authorize a duplicate
            # purchase (Codex review, PR #1168: "Fail closed when
            # cursor-only descent reaches its limit"). Discards the
            # already-found direct rows too, not just refuses to report
            # them provable -- truncated evidence is "there is more we
            # cannot reach", not "nothing more exists", exactly like the
            # no-direct-row branch's own identical depth-limit sentinel.
            return "envelope descent exceeded the max depth", [data, *_cursor_only_layers]
        return orders, [data, *_cursor_only_layers]
    if orders is not None and not isinstance(orders, list):
        # A malformed DIRECT "orders" value -- present, but not a list at
        # all (e.g. a truncated string, a dict) -- is unreadable evidence
        # on the settled layer itself, not an absent/empty placeholder a
        # nested chain is free to override. Falling through to the nested-
        # data fallback below let a malformed direct value be silently
        # discarded in favor of a nested list, reading the page as
        # provably complete from evidence that was never actually parsed
        # (Codex review, PR #1168: "Reject malformed direct order
        # collections before descending"). Only an absent (`None`) or
        # intentionally-supported empty-list direct value may fall through
        # to the nested lookup; every other direct shape fails closed here,
        # returned as-is for `_is_provable_orders_page`'s own
        # `isinstance(..., list)` check to reject.
        return orders, [data]
    # A NON-BLANK singular "order" alongside an absent/empty direct
    # "orders" is real, positive evidence about ONE order this settled
    # layer names -- and the nested-list fallback below would otherwise
    # silently discard it in favor of whatever a deeper "orders"
    # collection holds: `{"order": {"id": "LIVE", "ref_id": "TARGET",
    # "state": "queued"}, "data": {"orders": [{"id": "OTHER", "state":
    # "rejected"}]}}` has the singular row naming a DIFFERENT, live order
    # the nested collection never mentions. Trusting the nested list
    # alone made `_iter_same_day_orders()` yield only "OTHER",
    # `strategy_order_with_ref_id(..., "TARGET")` return `None`, and
    # crash/whole-share recovery release the claim and authorize a
    # duplicate purchase even though this very response already proved
    # "TARGET" live (Codex review, PR #1169: "Reject singular orders
    # beside nested history lists"). Failed closed the same way a
    # malformed or conflicting nested collection already is above,
    # rather than silently picking one side.
    _singular_order = data.get("order")
    if isinstance(_singular_order, dict) and _singular_order:
        return "singular order coexists with a nested order collection", [data]
    if _singular_order is not None and not isinstance(_singular_order, dict):
        # A malformed (present but non-dict, e.g. a truncated string)
        # singular "order" is unreadable evidence too, exactly like the
        # malformed-direct-"orders"-value check above: the `isinstance(...,
        # dict)` check just above only catches a genuine (or falsey) dict,
        # so a value like `{"order": "truncated", "data": {"orders":
        # [{"id": "OTHER"}]}}` fell through here silently, discarding the
        # unreadable "order" evidence in favor of the nested collection
        # and reading the page as provably complete from evidence that
        # was never actually parsed (Codex review, PR #1169: "Reject
        # malformed singular orders before descending"). Failed closed
        # the same way every other malformed/conflicting shape on this
        # layer already is.
        return "malformed singular order collection", [data]
    # Only a genuinely NON-EMPTY nested "orders" list is trusted as this
    # page's own rows -- an earlier fix ("Retain pagination pointers
    # during envelope descent") deliberately keeps a bare `next_cursor`
    # with an EMPTY nested `{"orders": []}` unprovable/ambiguous rather
    # than reading it as a genuinely empty page, since a pagination
    # pointer with nothing visible at its own layer is exactly the
    # "cannot rule out hidden evidence" shape that fix exists to fail
    # closed on. Falling back to an empty nested list here would silently
    # re-introduce that same false "provably empty" reading one layer
    # later. A nested list that actually HAS rows carries real, positive
    # evidence this function's callers must not discard, so only that
    # case is trusted.
    node: Any = data.get("data")
    _traversed: List[Dict[str, Any]] = []
    _depth = 0
    _depth_limit_reached = False
    while isinstance(node, dict):
        if _depth >= _MAX_ENVELOPE_DESCENT_DEPTH:
            _depth_limit_reached = True
            break
        _traversed.append(node)
        # Every INTERMEDIATE wrapper walked on the way to a deeper "orders"
        # collection gets the SAME singular-"order" check the outer settled
        # layer's own direct check applies -- not just the row-bearing
        # layer once found: this loop previously inspected only each
        # node's "orders" field, so a singular order sitting on an
        # intermediate wrapper ABOVE the eventual row-bearing layer --
        # `{"data": {"order": {"id": "LIVE", "ref_id": "TARGET", "state":
        # "queued"}, "data": {"orders": [{"id": "OTHER", "state":
        # "rejected"}]}}}` -- was silently skipped in favor of the deeper
        # "orders" list, letting `strategy_order_with_ref_id(..., "TARGET")`
        # return `None` and release a crash/whole-share recovery claim even
        # though this very response already proved "TARGET" live (Codex
        # review, PR #1169: "Validate singular orders on intermediate
        # wrappers"). Failed closed the same way the outer settled layer's
        # own singular-order conflict/malformed cases already are.
        _intermediate_singular_order = node.get("order")
        if isinstance(_intermediate_singular_order, dict) and _intermediate_singular_order:
            return "singular order coexists with a nested order collection", [*reversed(_traversed), data]
        if _intermediate_singular_order is not None and not isinstance(_intermediate_singular_order, dict):
            return "malformed singular order collection", [*reversed(_traversed), data]
        _nested_orders = node.get("orders")
        if isinstance(_nested_orders, list) and _nested_orders:
            # Keeps walking BELOW the row-bearing layer too, purely for
            # cursor evidence, before returning -- mirroring the direct-
            # rows early return's own "Inspect nested cursors even with
            # direct rows" fix. A row-bearing layer can carry its rows
            # while its OWN continuation sits one or more wrappers
            # deeper still: `{"cursor": "P1", "data": {"orders": [...],
            # "data": {"next_cursor": "P2"}}}}` returning as soon as the
            # rows were found discarded that deeper "P2" entirely,
            # letting the outer layer's echoed "P1" read as the end of
            # history even though a live order sits on P2 (Codex review,
            # PR #1168: "Keep scanning cursors below nested order rows").
            _deeper_cursor_layers: List[Dict[str, Any]] = []
            _deeper_node: Any = node.get("data")
            _deeper_depth = _depth + 1
            _deeper_depth_limit_reached = False
            while isinstance(_deeper_node, dict):
                if _deeper_depth >= _MAX_ENVELOPE_DESCENT_DEPTH:
                    _deeper_depth_limit_reached = True
                    break
                _deeper_singular_order_for_cursor_scan = _deeper_node.get("order")
                if (
                    isinstance(_deeper_singular_order_for_cursor_scan, dict)
                    and _deeper_singular_order_for_cursor_scan
                ):
                    # A layer BELOW the row-bearing one, visited only for
                    # its cursor, can still carry its own non-blank
                    # SINGULAR "order" -- {"data": {"orders": [A], "data":
                    # {"order": {"id": "B", "ref_id": "TARGET", "state":
                    # "queued"}}}} -- the identical gap the row-bearing
                    # layer's own direct-vs-nested-singular check closes,
                    # just one (or more) layers further down: this loop
                    # only ever read a deeper layer's "orders" key,
                    # silently ignoring "order" entirely. A live order
                    # named only here was invisible to
                    # `strategy_order_with_ref_id`, letting recovery
                    # release the claim and authorize a duplicate purchase
                    # despite this response proving the order live (Codex
                    # review, PR #1169: "Reject singular orders below
                    # nested history rows"). Failed closed the same way
                    # every other conflicting/malformed shape in this scan
                    # already is.
                    return "nested singular order coexists with a nested order collection", [data]
                if _deeper_singular_order_for_cursor_scan is not None and not isinstance(
                    _deeper_singular_order_for_cursor_scan, dict
                ):
                    # A malformed (present but non-dict) singular "order"
                    # BELOW the row-bearing layer is unreadable evidence
                    # too, mirroring the identical malformed-singular-order
                    # check the row-bearing layer's own cursor-only scan
                    # already applies (Codex review, PR #1169: "Reject
                    # malformed singular orders during nested cursor
                    # scans" -- applied proactively here too, one layer
                    # further down, for the identical reason).
                    return "malformed nested singular order collection", [data]
                _deeper_orders_for_cursor_scan = _deeper_node.get("orders")
                if _deeper_orders_for_cursor_scan is not None and not isinstance(
                    _deeper_orders_for_cursor_scan, list
                ):
                    # A malformed "orders" value BELOW the row-bearing
                    # layer -- present, but not a list -- is exactly the
                    # same unreadable-evidence gap the direct-rows branch's
                    # own "Reject malformed nested order collections" fix
                    # closed, just one layer further down: this loop only
                    # ever walked deeper layers for cursor info, silently
                    # ignoring their own malformed "orders" key entirely.
                    return _deeper_orders_for_cursor_scan, [data]
                if (
                    isinstance(_deeper_orders_for_cursor_scan, list)
                    and _deeper_orders_for_cursor_scan
                    and not _order_collections_semantically_agree(
                        _deeper_orders_for_cursor_scan, _nested_orders
                    )
                ):
                    # A layer BELOW the row-bearing one has its own
                    # non-empty "orders" list that DISAGREES with the
                    # already-found row-bearing list -- the identical
                    # "Reject conflicting nested order lists" gap the
                    # direct-rows branch was already closed for, one layer
                    # further down: `{"next_cursor": "P2", "data":
                    # {"orders": [other], "data": {"orders": [matching]}}}}`
                    # discarded the deeper, genuinely different `matching`
                    # row in favor of the shallower one, letting
                    # `strategy_order_with_ref_id()` follow P2 and return
                    # `None` for a ref that was actually present in this
                    # very response (Codex review, PR #1168: "Reject
                    # conflicting order lists below nested rows"). Two
                    # disagreeing non-empty collections is unreconcilable,
                    # ambiguous evidence -- failed closed exactly like the
                    # direct-vs-nested conflict, rather than silently
                    # trusting the shallower list alone.
                    #
                    # Compared via `_order_collections_semantically_agree`,
                    # not a raw `!=`, mirroring the direct-rows branch's own
                    # identical fix above: two layers repeating the SAME
                    # orders with a different SUPPORTED numeric ID encoding
                    # (`id=123` vs. `id="123"`) or a different row order are
                    # not a genuine conflict, and the raw list comparison
                    # here reported one anyway, failing closed on an
                    # otherwise valid page and stranding a recovery lease or
                    # blocking a legitimate same-day purchase (Codex review,
                    # PR #1168: "Compare deeper order collections
                    # semantically").
                    return "conflicting nested order collections", [data]
                _deeper_cursor_layers.append(_deeper_node)
                _deeper_node = _deeper_node.get("data")
                _deeper_depth += 1
            if _deeper_depth_limit_reached:
                # The IDENTICAL depth-limit gap the direct-rows branch's
                # own cursor-only scan was just closed for ("Fail closed
                # when cursor-only descent reaches its limit"), one layer
                # further down: this scan already found real rows on the
                # row-bearing layer, but hitting the cap before exhausting
                # the chain below it means a continuation cursor or a
                # matching order could still exist past the limit, never
                # actually inspected. Discards the already-found
                # `_nested_orders` rows too, returning the same unreadable
                # sentinel, rather than reporting the page provable from
                # truncated evidence (Codex review, PR #1168: "Fail closed
                # when cursor-only descent reaches its limit").
                return "envelope descent exceeded the max depth", [
                    *_deeper_cursor_layers,
                    *reversed(_traversed),
                    data,
                ]
            # Deepest-first, then row-bearing layer first, then every layer
            # traversed to reach it (innermost-of-the-intervening-set
            # first), then the outer settled layer last.
            return _nested_orders, [*_deeper_cursor_layers, *reversed(_traversed), data]
        # A PRESENT nested "orders" that is not a list at all -- a string,
        # a number, a mapping -- is malformed evidence, not "nothing here
        # yet": silently descending past it (as the old code did) let a
        # deeper/outer absent-or-empty collection be read as the effective
        # page while this genuinely unreadable one was never surfaced,
        # e.g. `{"next_cursor": "P2", "data": {"orders": "truncated"}}}`
        # returning the OUTER layer's absent collection let the ordinary
        # duplicate gate advance past this page as though it had nothing,
        # when in fact it could not be read at all. Returned immediately,
        # same as the direct-value malformed case just above, for
        # `_is_provable_orders_page`'s own `isinstance(..., list)` check to
        # reject (Codex review, PR #1169: "Reject malformed nested order
        # collections").
        if _nested_orders is not None and not isinstance(_nested_orders, list):
            return _nested_orders, [*reversed(_traversed), data]
        node = node.get("data")
        _depth += 1
    # No non-empty nested list found: fall back to the direct value exactly
    # as before (None if absent, [] if an explicit empty list -- both
    # preserved distinctly for _is_provable_orders_page's isinstance(...,
    # list) check). The traversed layers are still returned even here --
    # not collapsed to just `[data]` -- because an intervening layer along
    # this now-empty-at-the-end chain can still carry its OWN genuine
    # continuation pointer (e.g. `{"cursor": "P2", "data": {"next_cursor":
    # "P3", "data": {"orders": []}}}}`): the outer layer's echoed cursor
    # makes the nested empty list provable, but that says nothing about
    # the intervening `next_cursor="P3"` this function walked straight
    # past -- discarding it here made the caller conclude history ended
    # at the outer page's cursor while a live order still sat on P3
    # (Codex review, PR #1168: "Preserve cursors traversed while resolving
    # empty pages").
    if _depth_limit_reached:
        # The descent hit `_MAX_ENVELOPE_DESCENT_DEPTH` before finding a
        # non-empty nested "orders" list -- a cyclic or pathologically
        # deep envelope this loop deliberately refuses to follow forever.
        # Returning the direct `orders` value here (as the ordinary
        # natural-end case does) let a page whose direct collection
        # happens to be an empty list `[]` read as PROVABLY empty even
        # though real rows could be sitting one layer past the depth cap,
        # never actually inspected: `_is_provable_orders_page`'s own
        # `isinstance(..., list)` check saw that empty list and accepted
        # the page, letting `strategy_order_with_ref_id()` return `None`
        # for a reference hidden below the limit and release a crash/
        # whole-share recovery claim (Codex review, PR #1168: "Fail
        # closed when envelope descent hits its limit"). Truncated
        # evidence is "there is more we cannot reach", not "nothing more
        # exists" -- returned as an unreadable non-list value for that
        # same isinstance check to reject, exactly like a malformed
        # direct/nested value already does.
        return "envelope descent exceeded the max depth", [*reversed(_traversed), data]
    return orders, [*reversed(_traversed), data]


def _orders_in_settled_layer_or_nested(data: Dict[str, Any]) -> Any:
    """The ``orders`` value on ``data``, or on a nested ``"data"`` chain below it.

    ``_unwrap_seeking_order_evidence`` correctly settles on a pagination-
    pointer layer rather than descending past it (a genuine ``next_cursor``
    must never be silently skipped) -- but that same layer's own order rows
    can still sit one OR MORE levels deeper, in its own nested ``"data"``
    chain: ``{"next_cursor": "P2", "data": {"data": {"orders": [...]}}}``
    has the settled layer's bare ``"orders"`` key absent, with the actual
    rows sitting past not one but TWO more pass-through wrappers. A first
    version of this fallback checked only the immediate child, so a second
    (or deeper) wrapper still discarded the page's own rows silently
    (Codex review, PR #1169: "Traverse all wrappers below pagination
    pointers" -- the fresh-evidence follow-up to "Preserve nested orders
    when retaining pagination pointers"). Bounded by
    ``_MAX_ENVELOPE_DESCENT_DEPTH`` for the same reason every other
    envelope-descent loop in this module is -- a cyclic or pathologically
    deep response must terminate, not hang.

    Shared by :func:`_is_provable_orders_page` and
    :func:`RobinhoodBroker._iter_same_day_orders` so both read the exact
    same effective rows: a valid page whose provability this function can
    see must not be rejected by a provability CHECK that saw a narrower
    view than the READER actually consumes (Codex review, PR #1169:
    "Validate nested rows before rejecting a provable page").

    A thin wrapper over :func:`_orders_and_cursor_layers_in_settled_layer_or_nested`
    for callers (like :func:`_is_provable_orders_page`) that only need the
    rows themselves, not which layers they came from.
    """
    orders, _ = _orders_and_cursor_layers_in_settled_layer_or_nested(data)
    return orders


def _is_provable_orders_page(response: Any, current_cursor: Optional[str] = None) -> bool:
    """True when ``response`` is a LIST-mode orders page a caller may reason about.

    Requires an actual ``orders`` list. A ``None``, a bare list, or a dict with
    no ``orders`` list is unparseable and must never be read as a valid *empty*
    history by a caller whose next step depends on absence being proven. An
    explicit ``{"orders": []}`` is fine -- a genuine empty page.

    A SINGLE-ORDER envelope (``{"order": {...}}``, or a bare order object) is
    rejected too, matching ``scripts/broker_reconciliation``'s own
    ``single_order_envelope_in_pagination`` rule. It is evidence about ONE order
    and says nothing about whether the account list has ended -- and
    ``_iter_same_day_orders`` only ever yields ``data["orders"]``, so such an
    envelope would pass a laxer check while silently yielding nothing, which
    crash recovery would read as "the claimed order was never submitted" and act
    on by releasing the balance (Codex review: "Reject single-order envelopes
    when proving history").

    Reads the SAME effective ``orders`` value :func:`_iter_same_day_orders`
    itself will consume -- including one sitting past a pagination
    pointer's own nested "data" chain, at any depth, not just one level
    deeper -- via :func:`_orders_in_settled_layer_or_nested`, not just the
    settled layer's bare key: a valid page whose rows sit deeper used to
    fail this check before the reader's own nested-row fallback ever got a
    chance to run, so ``require_provable_history=True`` callers (crash
    recovery in particular) raised and could preserve a lease indefinitely
    over a response that in fact named the requested order (Codex review,
    PR #1168 and PR #1169: "Validate nested orders before rejecting a
    provable page").

    The nested list must be NON-EMPTY to count, unlike the bare/settled
    case above where an explicit empty ``{"orders": []}`` IS proof of a
    genuine empty page: a nested ``{"next_cursor": "P2", "data": {"orders":
    []}}}`` is NOT that -- it is a pointer to more history paired with an
    empty sub-page, which proves nothing about what that further history
    holds and must stay unprovable/fail-closed (Codex review, PR #859/#911
    round 44: "Retain pagination pointers during envelope descent"). Only
    a nested list with actual visible rows is real evidence.

    ``current_cursor`` -- the cursor THIS page was requested with, when the
    caller has it -- narrows that exact rule for one specific shape: a
    settled layer whose only pagination evidence is a bare ``"cursor"``
    EQUAL to ``current_cursor`` (``{"cursor": "P2", "data": {"orders":
    []}}}``, requested with cursor ``"P2"``) is not "a pointer to more
    history paired with an empty sub-page" at all -- it is the broker's own
    end-of-list ECHO (the exact shape :func:`_next_order_cursor` already
    reads as "no more pages"), so its nested EMPTY orders list is trusted
    as a genuine final page rather than staying ambiguous (Codex review, PR
    #1168: "Accept nested empty orders after an echoed cursor"). A real
    forward-pointing ``next_cursor``/``next``, or a bare ``cursor`` that is
    NOT this exact echo, still falls through to the ordinary non-empty-only
    rule above.
    """
    if not isinstance(response, dict):
        return False
    _settled = _unwrap_seeking_order_evidence(response)
    _orders = _orders_in_settled_layer_or_nested(_settled)
    if isinstance(_orders, list):
        # A row that is a genuinely EMPTY mapping (``{}``) -- or a NONEMPTY
        # one carrying no usable broker id/order_id alias AND no usable
        # reference alias (``{"state": "queued"}``, ``{"error":
        # "truncated"}``) -- carries no order EVIDENCE at all: reference
        # matching has nothing to compare against it, so it can never be
        # proven to be (or not be) the order a recovery caller is looking
        # for. Declaring the page provable purely because it is a list let
        # `strategy_order_with_ref_id()` scan straight over such a row and
        # report a clean "no match", releasing a crash/whole-share recovery
        # claim and authorizing a duplicate purchase even though the
        # unreadable row could have represented the target (Codex review,
        # PR #1169: "Reject empty rows in provable order pages" / "Reject
        # nonempty rows without any order identity"). An ordinary empty
        # PAGE (``[]``, no rows at all) is unaffected -- that already means
        # "no orders exist" and stays provable.
        def _row_has_no_usable_identity(_row: Any) -> bool:
            if not isinstance(_row, dict):
                return False
            # `_first_usable_order_id_alias` only screens BLANK/falsey
            # values (see its own docstring) -- it deliberately still
            # returns a known-UNUSABLE sentinel like `id=True`, `id=-1`,
            # `id=NaN`, or the stringified `id="0"` unchanged, since every
            # caller is expected to run its own full usability check on
            # whatever it returns. This predicate skipped that check, so a
            # row like `{"id": True, "state": "queued"}` -- with no usable
            # reference alias either -- read as carrying a genuine identity
            # and the page was declared provable from a sentinel that
            # names no real order at all (Codex review, PR #1169: "Require
            # a genuinely usable identity on every history row").
            _id_alias = _first_usable_order_id_alias(_row)
            _has_usable_id = _id_alias is not None and not _is_unusable_live_placement_order_id(_id_alias)
            return not _has_usable_id and not _usable_ref_id_aliases(_row)

        return not any(_row_has_no_usable_identity(_row) for _row in _orders)
    if current_cursor is None:
        return False
    _bare_cursor = _settled.get("cursor")
    _is_echo = (
        isinstance(_bare_cursor, str)
        and _bare_cursor.strip() == current_cursor
        and _settled.get("next_cursor") is None
        and _settled.get("next") is None
    )
    if not _is_echo:
        return False
    # Searches the full depth-bounded nested "data" chain for the (possibly
    # empty) orders list, not just the immediate child: an echoed-cursor
    # page can wrap its empty collection behind MORE than one pass-through
    # "data" layer (e.g. {"cursor": "P2", "data": {"data": {"orders":
    # []}}}}), the same multi-level-wrapper shape
    # _orders_in_settled_layer_or_nested already handles for the NON-empty
    # case. Checking only the immediate child returned False here, so
    # `require_provable_history=True` callers rejected a valid, complete
    # history and could strand a crash-recovery lease indefinitely (Codex
    # review, PR #1169: "Traverse all wrappers for echoed empty order
    # pages").
    node: Any = _settled.get("data")
    _depth = 0
    while isinstance(node, dict):
        if _depth >= _MAX_ENVELOPE_DESCENT_DEPTH:
            return False
        if isinstance(node.get("orders"), list):
            return True
        node = node.get("data")
        _depth += 1
    return False


def _has_continuation_pointer(data: Dict[str, Any]) -> bool:
    """True when a page advertises MORE results via ``next``/``next_cursor``.

    Used only after :func:`_next_order_cursor` already failed to produce a usable
    cursor, to distinguish "this was the last page" from "there is more history we
    cannot reach" -- the latter must never be reported to callers as a complete
    (and therefore absence-proving) history.

    Mirrors ``scripts/broker_reconciliation._has_continuation`` exactly rather
    than testing truthiness. A present but FALSEY non-string pointer --
    ``next_cursor: 0``, ``next: []`` -- is one ``_next_order_cursor`` cannot
    parse, so a truthiness test reported "no continuation" and the iterator
    declared a truncated history complete. Crash recovery reads that as proof
    the claimed order was never submitted, releases the lease and authorizes a
    duplicate purchase (Codex review: "Reject falsey non-string continuation
    pointers"). Any non-string value is an unusable continuation and fails
    closed; only an absent pointer, or an empty/whitespace string (the
    conventional end-of-list), means there is genuinely nothing more.

    Also checks a bare ``cursor`` value -- but only when it is NOT a string.
    ``{"orders": [], "cursor": 0}`` has a malformed, unusable bare cursor
    :func:`_next_order_cursor` cannot parse (its own bare-cursor fallback
    requires ``isinstance(direct, str)``), and without this check the
    iterator read that as a complete history exactly like the ``next``/
    ``next_cursor`` gap above (Codex review, PR #859/#911 round 44: "Reject
    malformed bare cursors after recursive unwrapping"). A STRING bare
    cursor is deliberately left OUT of this check: unlike ``next``/
    ``next_cursor``, a bare ``cursor`` has real echo semantics --
    ``_next_order_cursor`` already treats a string cursor equal to
    ``current_cursor`` as the broker echoing back "this is the last page",
    which this function is called precisely to double-check, AFTER that
    filtering already ran. Flagging every non-blank string cursor here too
    would misread that ordinary, correctly-terminated final page's own
    echo as an unusable continuation and fail closed on a perfectly good
    end of history.
    """
    for key in ("next", "next_cursor"):
        value = data.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif value is not None:
            return True
    cursor_value = data.get("cursor")
    if cursor_value is not None and not isinstance(cursor_value, str):
        return True
    return False


#: Keys any of this module's classifiers reads directly off an order/envelope
#: mapping. Used by :func:`_unwrap` alone to decide whether an already-
#: unwrapped layer carries evidence of its own or is merely a further
#: passthrough wrapper.
#:
#: ``isError``/``error`` are included alongside the order/fill fields above:
#: ``{"data": {"isError": true, "error": "timeout", "data": {"order": {"id":
#: "ORD", "state": "queued"}}}}`` has the isError layer carrying evidence of
#: its own -- a bridge/connector failure -- but neither key used to be in this
#: tuple, so the round-43 descent loop below skipped right past that failing
#: layer and landed on the innermost ``{"order": {...}}`` as if it were a
#: clean success. ``_response_failure`` then found no isError/error anywhere
#: and returned ``None``, and ``place_market_buy`` read the connector timeout
#: as a proven success and handed back "ORD" as a placed order id (Codex
#: review, PR #859/#911 round 44: "Stop the envelope descent at a failure-
#: bearing layer").
#:
#: ``next``/``next_cursor`` are included too, for the identical reason: a
#: recursively wrapped order-history page can carry its pagination pointer
#: on an INTERMEDIATE ``data`` layer, sibling to (not inside) the deeper
#: layer that carries the actual ``orders`` list --
#: ``{"data": {"next_cursor": "abc", "data": {"orders": []}}}``. Neither
#: pointer counted as evidence, so the descent loop walked straight past
#: the layer advertising more history and returned only the deeper,
#: pointer-less ``{"orders": []}``. ``_next_order_cursor``/``_has_
#: continuation_pointer`` (both read the SAME unwrapped mapping this
#: returns) then saw no pagination pointer at all and reported this page
#: as the last one -- ``_iter_same_day_orders`` stopped short of a
#: crash-recovery order that lived on a later page, and releasing that
#: claim on "the order was never submitted" can authorize a duplicate
#: purchase (Codex review, PR #859/#911 round 44: "Retain pagination
#: pointers during envelope descent").
#:
#: ``cursor`` (the bare, no-prefix form) is included alongside ``next``/
#: ``next_cursor`` for the identical reason: ``_next_order_cursor`` reads a
#: bare ``cursor`` field too (its own last-resort fallback, used when the
#: page carries no ``next``/``next_cursor`` of its own), and
#: ``{"data": {"cursor": "abc", "data": {"orders": []}}}`` lost THAT
#: pointer to the same descent-past-it gap the ``next``/``next_cursor`` fix
#: closed for the other two aliases (Codex review, PR #859/#911 round 44:
#: "Preserve bare cursors during recursive unwrapping").
#: Review-response fields (``approved``, ``tradeable``, ``alerts``, ...) are
#: deliberately NOT in this tuple, even though :func:`_interpret_review`
#: reads them off an ``_unwrap``-produced layer too. A first attempt added
#: them here to keep a review response's blocking outer layer from being
#: descended past -- but presence-only anchoring on THESE keys cuts both
#: ways: an intermediate layer's ``approved: true`` anchored just as hard
#: as a blocking ``approved: false`` would have, so a genuine rejection one
#: layer deeper (``{"data": {"approved": true, "data": {"approved":
#: false}}}``) was never reached at all (Codex review, PR #1164 round 15:
#: "Inspect nested blockers after affirmative review fields"); and an
#: ordinary, unrelated ORDER-history response carrying incidental blank
#: review-shaped metadata (``{"data": {"warnings": [], "data": {"orders":
#: [...]}}}``) got anchored on that empty list and never reached the real
#: orders collection either (Codex review, PR #1164 round 15: "Skip empty
#: review metadata in order-history wrappers"). Both are solved instead by
#: :func:`_interpret_review` scanning every layer
#: :func:`_unwrap_traversed_layers` walks through, not just the one
#: ``_unwrap`` happens to stop on -- so review evidence no longer needs an
#: anchor key in this shared, order/pagination-focused tuple at all.

_ENVELOPE_EVIDENCE_KEYS = (
    "order",
    "orders",
    "id",
    "order_id",
    "state",
    "status",
    "success",
    "ok",
    "executed_notional",
    "executed_dollar_amount",
    "filled_notional",
    "cumulative_quantity",
    "isError",
    "error",
    "next",
    "next_cursor",
    "cursor",
)

#: Finite ALLOWLIST of the field names this module's own non-order
#: consumers actually read off an unwrapped envelope layer -- not a
#: denylist of "everything incidental", which two earlier fix attempts
#: showed cannot converge (a connector is free to add any metadata key
#: name, and this module cannot enumerate all of them). Checked via
#: PRESENCE in `_layer_carries_envelope_evidence`, not truthiness -- see
#: that function's own docstring for why (Codex review, PR #1164: "Look
#: past metadata containers when unwrapping order history"). Empty now
#: that the balance aliases have moved to `_TRUTHY_DOMAIN_PAYLOAD_KEYS`
#: below -- kept as its own allowlist (rather than folded away) since a
#: presence-checked domain field is a real, distinct category this module
#: may need again, not merely a historical artifact.
_DOMAIN_PAYLOAD_KEYS: "frozenset[str]" = frozenset()

#: Domain-payload fields checked for TRUTHINESS, not presence: `_unwrap`
#: is shared with order-history unwrapping too, and an EMPTY/ZERO value on
#: an intermediate wrapper layer carries no evidence a caller looking for
#: real content further in should anchor on. `results` -- the single
#: field every quote (`_quote_for`), bar (`_parse_bars`), and tradability
#: (`is_tradable_for_fractional_buy`) parser in this module reads off its
#: unwrapped response -- is the original member: `{"data": {"results": [],
#: "data": {"orders": [{"id": "LIVE", "state": "queued"}]}}}` has an EMPTY
#: "results" sibling that carries no evidence at all -- every one of this
#: module's own "results" consumers reads it via `data.get("results") or
#: []`, so an empty list already means "nothing here" to them too.
#: Checking it by presence anchored descent on that empty sibling and hid
#: the real "orders" collection one layer deeper, letting
#: `_iter_same_day_orders` see an empty history and
#: `same_day_strategy_order_exists` authorize a duplicate purchase (Codex
#: review, PR #1164: "Do not let results mask nested order history").
#:
#: The five balance aliases -- exactly `RobinhoodBroker.
#: available_buying_power`'s own lookup loop -- joined this set for the
#: identical reason, having started out presence-checked instead (a
#: decision made specifically to preserve an explicit `buying_power: 0`,
#: which IS meaningful, over a stale nonzero figure sitting deeper in the
#: same envelope): `{"data": {"success": true, "buying_power": 0, "data":
#: {"orders": [{"id": "LIVE", "state": "queued"}]}}}` has a `buying_power:
#: 0` sibling on an ORDER-HISTORY response's own wrapper layer -- entirely
#: incidental to that call -- and presence-only anchored `_unwrap()` on it
#: just as hard as a genuine balance response's own zero, hiding the real
#: `orders` collection from `_iter_same_day_orders`/
#: `same_day_strategy_order_exists` and authorizing a duplicate purchase
#: (Codex review, PR #1168: "Do not let zero balance fields hide nested
#: orders"). `available_buying_power` no longer relies on `_unwrap`
#: settling on the zero-carrying layer to see it -- it scans every layer
#: :func:`_unwrap_traversed_layers` walks through instead, so the explicit
#: zero this key set exists to preserve is still found even though it no
#: longer anchors descent.
_TRUTHY_DOMAIN_PAYLOAD_KEYS = frozenset(
    {
        "results",
        "buying_power",
        "cash",
        "unallocated_buying_power",
        "portfolio_cash",
        "withdrawable_amount",
    }
)


def _layer_carries_envelope_evidence(data: Dict[str, Any], *, include_domain_keys: bool = True) -> bool:
    """Whether an unwrapped layer carries evidence ``_unwrap`` should stop on.

    Presence alone for most of ``_ENVELOPE_EVIDENCE_KEYS`` -- an ``id``,
    ``state``, or fill alias mapped to a blank value is still that key being
    ACTIVELY reported by this layer, distinct from the key being absent.

    The three pagination keys -- ``next``/``next_cursor``/``cursor`` -- are the
    one exception: a BLANK value there (``None``, ``""``, whitespace) is the
    same "no continuation" reading ``_next_order_cursor``/``_has_continuation_
    pointer`` already give it everywhere else in this module, not a real
    pointer. Presence-only treated ``{"next_cursor": null, "data": {"orders":
    [...]}}``'s intermediate wrapper layer as evidence-bearing purely because
    the key existed with a ``null`` value, stopping the descent one layer too
    early and leaving the caller holding an empty ``{"next_cursor": null}``
    instead of the nested ``{"orders": [...]}`` -- `_iter_same_day_orders`
    then read the truncated view as a complete (and therefore absence-
    proving) history, letting duplicate guards and placement recovery miss a
    live order sitting one layer deeper (Codex review, PR #1164: "Descend
    past empty pagination metadata"). A non-blank pointer of either shape --
    string or not -- still counts, matching those two functions' own
    fail-closed reading of a malformed non-string pointer.

    ``"error"`` gets the identical blank exception, for the identical
    reason: ``{"data": {"error": null, "data": {"orders": [{"id": "LIVE",
    "state": "queued"}]}}}`` has an explicitly-null "error" on the
    intermediate wrapper layer -- a connector actively reporting "no
    error", not a diagnostic payload -- and presence-only treated that
    null as evidence, stopping the descent before the nested "orders"
    collection was ever reached. `_iter_same_day_orders` then read the
    truncated wrapper as a complete (and therefore absence-proving) page,
    and its callers (the ordinary duplicate-guard path in particular,
    which does not require a provable page the way placement recovery
    does) authorized a purchase a live order sitting one layer deeper
    should have blocked (Codex review, PR #1164: "Descend past non-error
    wrapper metadata"). A non-blank "error" -- a real diagnostic payload,
    however shaped -- still counts as evidence, same as before.

    ``"isError"``/``"success"``/``"ok"`` get their OWN, value-specific
    exception, not a blank check: these are boolean HEALTH flags, not
    order data, and their AFFIRMATIVE-of-nothing-wrong values --
    ``isError: false``, ``success: true``, ``ok: true`` -- are exactly as
    uninformative about a nested order collection as a null "error" or a
    blank pagination pointer is. ``{"data": {"isError": false, "data":
    {"orders": [{"id": "LIVE", "state": "queued"}]}}}`` stopped descent on
    the intermediate wrapper purely because ``isError`` was PRESENT, even
    though ``false`` is the connector actively saying "nothing went
    wrong" -- the same "no continuation"/"no error" reading the other
    exceptions above already give their own blank sentinel (Codex review,
    PR #1164: "Descend through non-error wrapper flags"). The FAILURE
    values -- ``isError: true``, ``success: false``, ``ok: false`` -- are
    the opposite: real diagnostic evidence this function must still stop
    on, matching the round-44 reasoning (`_ENVELOPE_EVIDENCE_KEYS`'s own
    docstring) for why a failure-bearing layer must not be treated as a
    clean success.

    ``_unwrap`` is shared by non-order consumers too, whose payload fields
    (``buying_power``, ``results``, ...) are not in `_ENVELOPE_EVIDENCE_KEYS`
    at all. ``{"data": {"success": true, "buying_power": "100", "data": {}}}``
    treated the affirmative ``success`` flag as license to keep descending,
    discarding the sibling ``buying_power`` and leaving `available_buying_
    power` (and quote/bar parsing, which reads the same unwrap path) with
    an empty deepest layer instead of the usable one it was actually sitting
    on (Codex review, PR #1164: "Keep successful non-order payload fields
    during unwrapping").

    This is checked via `_DOMAIN_PAYLOAD_KEYS` -- a finite ALLOWLIST of the
    actual field names this module's own non-order consumers read off an
    unwrapped layer (``available_buying_power``'s five balance aliases) --
    not a denylist of "every unrecognized key". Two earlier attempts at a
    denylist both failed to hold: excluding known INCIDENTAL review-
    bookkeeping keys (``warnings``, ``approved``, ``tradeable``, ...)
    missed that ANY unlisted truthy sibling, including ones with no
    relationship to review responses at all -- an ordinary connector-added
    ``{"metadata": {"latency_ms": 12}}`` -- still tripped the denylist's
    "any truthy key I don't recognize" fallback and anchored descent on an
    order-history wrapper before reaching the real ``orders`` collection
    (Codex review, PR #1164: "Look past metadata containers when
    unwrapping order history"), and gating on TRUTHINESS at all discarded
    a genuine, presently-relevant ``buying_power: 0`` layer in favor of a
    stale nonzero figure sitting deeper in the same envelope (Codex
    review, PR #1164: "Preserve explicit zero-valued domain fields") -- a
    real broker-reported zero balance is exactly as much evidence as a
    positive one, and `available_buying_power`'s own ``for key in (...):
    if key in data:`` loop already reads presence, not truthiness, for the
    identical fields. An allowlist checked by PRESENCE closes both gaps
    at once: a genuinely unrecognized field (whatever a connector calls
    it) is never mistaken for domain content just because this function
    doesn't have a name for it, and a zero-valued domain field still
    counts.

    ``results`` is checked SEPARATELY, via `_TRUTHY_DOMAIN_PAYLOAD_KEYS`
    and truthiness rather than presence: `_unwrap` is shared with order-
    history unwrapping too, and an EMPTY ``results`` list on an
    intermediate wrapper -- ``{"data": {"results": [], "data": {"orders":
    [{"id": "LIVE", "state": "queued"}]}}}`` -- carries no evidence at
    all, the same "nothing reported" reading every one of this module's
    own ``results`` consumers already gives it via their own ``data.get(
    "results") or []``. Checking it by presence, like the balance keys,
    anchored descent on that empty sibling and hid the real ``orders``
    collection one layer deeper (Codex review, PR #1164: "Do not let
    results mask nested order history") -- unlike a balance figure, an
    empty ``results`` list is not a meaningful "genuine zero", it is
    exactly the "no data" a blank/absent field already is.

    ``include_domain_keys=False`` disables BOTH domain-key checks above
    entirely, for callers -- specifically :func:`_unwrap_seeking_order_evidence`
    -- that must never stop descent on a balance/quote/tradability field:
    order-history parsing wants exactly the ``orders``-bearing layer, and
    a domain field on an ancestor layer must not be mistaken for that
    caller's own evidence the way it correctly is for `_unwrap`'s single-
    answer descent (Codex review, PR #1164: "Keep domain fields from
    masking nested order history"). The `_ENVELOPE_EVIDENCE_KEYS` check
    below -- including the pagination-pointer and health-flag readings --
    still applies unconditionally either way.
    """
    if include_domain_keys:
        if any(key in _DOMAIN_PAYLOAD_KEYS for key in data):
            return True
        if any(key in _TRUTHY_DOMAIN_PAYLOAD_KEYS and value for key, value in data.items()):
            return True
    for key in _ENVELOPE_EVIDENCE_KEYS:
        if key not in data:
            continue
        if key in ("next", "next_cursor", "cursor", "error") and _is_blank_broker_field(data.get(key)):
            continue
        if key == "isError" and data.get(key) is False:
            continue
        if key in ("success", "ok") and data.get(key) is True:
            continue
        return True
    return False


def _skippable_health_flag_contradicted(data: Dict[str, Any], nested: Dict[str, Any]) -> bool:
    """Whether descending from ``data`` into ``nested`` would silently drop a
    genuine self-contradiction between the two layers' own health flags.

    ``_layer_carries_envelope_evidence`` treats ``isError: false``/``success:
    true``/``ok: true`` as "no evidence, keep descending" -- they affirm
    nothing went wrong, same as a blank ``error``/pagination pointer. But
    ``{"success": true, "data": {"success": false, "order": {"id": "A",
    "state": "rejected", "cumulative_quantity": "0"}}}`` has the OUTER layer
    actively affirming success while the layer ``_unwrap`` is about to
    descend PAST it reports the opposite for the identical flag -- a real
    self-contradiction, not an empty wrapper to skip past. Descending
    unconditionally lost the outer's ``success: true`` (it is not the FIRST
    unwrap, so nothing else ever inspects that layer again) and left
    ``_failure_is_broker_authored`` seeing only the inner ``success: false``,
    proving a rejection the response's own outer layer just as clearly
    disputed -- skipping history reconciliation for an order that may in
    fact have been accepted (Codex review, PR #1164: "Preserve contradictory
    success flags during envelope descent"). Halting descent here instead
    (``_unwrap`` returns ``data``, the outer layer, unchanged) keeps that
    ``success: true`` visible to every caller that inspects ``_unwrap(...)``'s
    own top-level keys, restoring the SAME "contradictory envelope ->
    unattributable, not a proven rejection" handling
    ``_failure_is_broker_authored`` already gives an outer/inner conflict at
    the FIRST unwrap layer.

    Compared as ONE combined affirmative-vs-negative reading, not as three
    independent same-name pairs: ``isError``/``success``/``ok`` are all
    interchangeable health-flag SPELLINGS a connector is free to mix across
    layers of the SAME response -- ``{"ok": true, "data": {"success":
    false, "order": {...}}}`` has the outer layer affirming health via
    ``ok`` while the nested layer disputes it via ``success``, exactly the
    same self-contradiction as two layers using the identical flag name,
    but a same-name-only comparison never noticed a mismatched pair of
    ALIASES said the opposite things (Codex review, PR #1164: "Compare
    equivalent health flags before descending"). A layer counts as
    affirmative if ANY of its own three flags says so, and negative if ANY
    does -- the same "any recognized alias is real evidence" reading this
    module already gives these flags elsewhere.
    """
    _data_affirms = data.get("isError") is False or data.get("success") is True or data.get("ok") is True
    _nested_denies = nested.get("isError") is True or nested.get("success") is False or nested.get("ok") is False
    if _data_affirms and _nested_denies:
        return True
    return False


def _unwrap_hides_contradicted_nested_data(payload: Any) -> bool:
    """Whether :func:`_unwrap` stopped early on ``payload`` because the layer it
    settled on is self-contradicted by a deeper layer -- meaning real content
    (an ``orders`` collection, say) may sit one layer further in than
    ``_unwrap``'s caller ever reaches.

    Right for :func:`_unwrap_traversed_layers`'s own placement-classifier
    callers: halting on the outer, affirmative layer keeps that layer's own
    ``success: true`` visible instead of silently proving a rejection from
    the inner ``success: false`` an equally-authoritative outer layer just
    disputed (see :func:`_skippable_health_flag_contradicted`'s own
    docstring). WRONG for an order-HISTORY reader calling plain ``_unwrap()``
    -- it does not want "whichever layer is safest for a single order
    verdict", it wants the actual ``orders`` collection, and a genuine
    self-contradiction is malformed evidence that collection may be sitting
    behind, not a normal "nothing more to see" bottom. ``{"data": {"success":
    true, "data": {"success": false, "orders": [{"id": "LIVE", "state":
    "queued"}]}}}`` has `_unwrap()` stop on the outer, order-less layer, so
    `_iter_same_day_orders` read `data.get("orders")` as `None` and reported
    a clean empty history -- authorizing a duplicate purchase despite the
    queued order sitting one layer deeper (Codex review, PR #1168: "Keep
    contradictory order-history wrappers fail-closed"). Callers that need
    the real collection, not a single-order verdict, must check this and
    fail closed instead of reading the truncated layer as proof of absence.

    Traverses with ``include_domain_keys=False``, the SAME policy
    :func:`_unwrap_seeking_order_evidence` itself uses, not the default
    ``_layer_carries_envelope_evidence`` behavior: this function exists
    specifically to protect that order-seeking descent's caller
    (`_iter_same_day_orders`), and checking with the DEFAULT (domain-
    aware) policy stopped on a recognized domain field before the
    contradiction was ever reached. ``{"data": {"buying_power": "100",
    "data": {"success": true, "data": {"success": false, "error": "down",
    "orders": [...]}}}}`` has this check stop at the buying-power layer --
    which carries no `success`/`ok`/`isError` flag of its own to compare
    against anything -- so the outer/inner `success` contradiction one
    layer further in went undetected, and `_unwrap_seeking_order_evidence`
    (using the identical domain-aware-disabled policy) settled cleanly on
    the outer `success: true` layer instead of raising on the malformed,
    self-contradictory envelope (Codex review, PR #1164/#1169: "Scan
    contradictions beyond domain wrappers").
    """
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    _depth = 0
    while not _layer_carries_envelope_evidence(data, include_domain_keys=False):
        if _depth >= _MAX_ENVELOPE_DESCENT_DEPTH:
            # See `_MAX_ENVELOPE_DESCENT_DEPTH`'s own docstring: bounds a
            # cyclic or pathologically deep envelope instead of looping
            # forever. Reported as a contradiction (fail closed) rather
            # than "no contradiction found" -- a chain this deep or
            # cyclic is itself malformed evidence this function exists to
            # keep a caller from silently trusting.
            return True
        nested = data.get("data")
        if not isinstance(nested, dict):
            return False
        if _skippable_health_flag_contradicted(data, nested):
            return True
        data = nested
        _depth += 1
    return False


def _unwrap(payload: Any) -> Dict[str, Any]:
    """Extract the inner data dict from an MCP resource/tool payload.

    Returns ``payload['data']`` when present and a dict; otherwise returns the
    original payload dict. Non-dict payloads return an empty dict.

    Descends through FURTHER nested ``"data"`` wrappers beyond the first, but
    only past a layer that is itself EMPTY of everything this module's
    classifiers read: ``{"success": false, "data": {"data": {"order": {"id":
    "LIVE", "state": "queued"}}}}`` stopped after one unwrap, landing on
    ``{"data": {...}}`` -- a layer with no order/orders/id/state/fill
    evidence of its own -- and every classifier reading `data` found
    nothing there, raising a plain unresolved failure while the doubly-
    nested ``LIVE`` order sat outside reconciliation entirely (Codex
    review, PR #859/#911 round 43: "Inspect recursively nested placement
    envelopes"). The FIRST unwrap stays unconditional exactly as before --
    an ordinary envelope's outer layer carries "success"/"id" alongside its
    own "data", and must not be treated as evidence-free.

    Also stops -- without descending -- the moment the layer about to be
    skipped over is contradicted by the very layer it would be replaced
    with (see ``_skippable_health_flag_contradicted``), so a self-
    contradictory response is never quietly resolved to whichever health
    flag happens to sit deepest.
    """
    layers = _unwrap_traversed_layers(payload)
    if layers:
        return layers[-1]
    return payload if isinstance(payload, dict) else {}


def _unwrap_seeking_order_evidence(payload: Any) -> Dict[str, Any]:
    """``_unwrap``, but for order-history AND order-placement consumers:
    seeks the layer that actually carries order evidence rather than
    stopping on an unrelated recognized domain field.

    ``_unwrap``'s own descent stops the moment a layer carries a recognized
    DOMAIN payload key (``buying_power``, ``results``) -- correct for
    `available_buying_power`/quote/tradability, which want exactly that
    layer, but wrong for order-history and placement parsing alike:
    ``{"data": {"buying_power": "100", "data": {"orders": [{"id": "LIVE",
    "state": "queued"}]}}}`` stopped `_unwrap` at the buying-power layer,
    so `_iter_same_day_orders` read ``data.get("orders")`` as absent,
    substituted an empty list, and `same_day_strategy_order_exists`
    authorized what could have been a duplicate purchase despite the
    nested working order (Codex review, PR #1164/#1169: "Keep domain
    fields from masking nested order history"). ``{"data": {"buying_power":
    "100", "data": {"order": {"id": "ORD", "state": "queued"}}}}`` -- the
    identical shape for a SUCCESSFUL placement response -- hit the same
    bug in `place_market_buy`: the accepted order was discarded and the
    call raised `AmbiguousPlacementResponseError` even though the broker
    had accepted the purchase (Codex review, PR #1164: "Keep placement
    descent past unrelated domain fields"). The earlier
    ``_failure_is_broker_authored`` fix (switching to `_all_data_layers`)
    only fixed the CONTRADICTION scan; both of these callers need their
    own caller-specific descent instead, since neither scans every layer
    for a flag -- each needs exactly one settled layer.

    Mirrors ``_unwrap``'s own descent loop exactly, EXCEPT it calls
    :func:`_layer_carries_envelope_evidence` with ``include_domain_keys=
    False`` -- so a pagination pointer (``next_cursor``/``cursor``), an
    explicit ``error``, an ``order``/``orders``/``id``/``state``/fill-
    evidence key, or a health flag still correctly halts descent
    (unchanged from `_unwrap`; an ambiguous, unprovable order-history page
    must still read as unprovable, not silently skipped past to a
    pointer-less ``orders`` list one layer deeper), but a domain payload
    key (``buying_power``, ``results``) no longer does. Deliberately NOT a
    blind "find any `orders` key anywhere" search -- that reading passed
    straight through a genuine pagination-pointer layer too, turning a
    correctly-unprovable page (a `next_cursor` with no visible `orders` of
    its own) into a falsely "provable" empty one. Also stops -- without
    descending -- at the same self-contradictory-layer boundary ``_unwrap``
    itself does (see :func:`_skippable_health_flag_contradicted`): walking
    past a domain-key layer that hides a deeper health-flag contradiction
    would otherwise settle on a layer this call was never meant to trust.

    The unconditional FIRST descent into ``payload["data"]`` -- inherited
    from ``_unwrap``'s own design, where the outer layer is ordinarily
    just wrapper metadata (``"success"``, ``"id"``) that never competes
    with the real content one level down -- is skipped entirely when
    ``payload`` itself already carries a genuine, non-blank ``"orders"``
    collection or singular ``"order"`` of its OWN, sibling to its own
    ``"data"`` key: ``{"orders": [{"id": "LIVE", "ref_id": "TARGET",
    "state": "queued"}], "data": {"orders": [{"id": "OTHER", "state":
    "rejected"}]}}`` has the OUTER top level directly naming the live
    target order, but the unconditional descent discarded it in favor of
    the unrelated nested collection without ever comparing the two,
    letting `strategy_order_with_ref_id()` report a clean absence and
    recovery release the claim for an order the response itself proved
    still exists (Codex review, PR #1169: "Validate outer order
    collections before unwrapping"). Deliberately narrower than a general
    :func:`_layer_carries_envelope_evidence` check: THAT also treats a
    bare ``"success": false``/``"isError": true`` health flag (with no
    order evidence of its own) as reason enough to stop, which is exactly
    the ordinary failure-envelope shape this function's whole descent
    exists to see PAST -- ``{"success": false, "data": {"order": {"id":
    "LIVE", "state": "queued"}}}}`` must still descend into ``data`` to
    find the real order, not stop on the outer health flag alone. Only an
    ACTUAL ``"order"``/``"orders"`` key at the outer level halts descent
    here; an ordinary ``{"success": true/false, "data": {...}}`` envelope
    -- where the outer layer names no order of its own -- is completely
    unaffected and still descends exactly as before.
    """
    if not isinstance(payload, dict):
        return {}
    _outer_orders = payload.get("orders")
    _outer_order = payload.get("order")
    if (isinstance(_outer_orders, list) and _outer_orders) or (
        isinstance(_outer_order, dict) and _outer_order
    ):
        return payload
    # A PRESENT but malformed outer "orders"/"order" value -- e.g.
    # {"orders": "truncated", "data": {"orders": []}} -- is unreadable
    # evidence, not "nothing there": the checks just above only ever
    # trust a well-shaped, nonempty collection/singular and silently fell
    # through to the unconditional `data` descent for anything else,
    # including a value that could not even be interpreted. That descent
    # then reached the nested "orders": [] and `_is_provable_orders_page`
    # read it as a clean, provable empty page, letting
    # `strategy_order_with_ref_id()` report absence and recovery release
    # a claim even though part of THIS response was never actually
    # readable (Codex review, PR #1168: "Reject malformed outer order
    # evidence before descending"). A well-shaped but EMPTY value (``[]``
    # / ``{}``) is unaffected -- that's the ordinary "nothing here yet,
    # keep looking deeper" case every other collection check in this
    # module already treats the same way, not malformed evidence.
    if (_outer_orders is not None and not isinstance(_outer_orders, list)) or (
        _outer_order is not None and not isinstance(_outer_order, dict)
    ):
        return payload
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    _depth = 0
    while not _layer_carries_envelope_evidence(data, include_domain_keys=False):
        if _depth >= _MAX_ENVELOPE_DESCENT_DEPTH:
            # See `_MAX_ENVELOPE_DESCENT_DEPTH`'s own docstring: bounds a
            # cyclic or pathologically deep envelope instead of looping
            # forever. Settling on the current (still-unresolved) layer is
            # safe here -- every caller of this function already runs its
            # OWN failure/shape checks on the returned layer afterward.
            break
        _nested = data.get("data")
        if not isinstance(_nested, dict):
            break
        if _skippable_health_flag_contradicted(data, _nested):
            break
        data = _nested
        _depth += 1
    return data


def _unwrap_traversed_layers(payload: Any) -> List[Dict[str, Any]]:
    """Every intermediate layer :func:`_unwrap` descends past, plus its stop.

    ``_unwrap``'s own loop treats a layer whose only flags are affirmative-
    of-nothing-wrong (``isError: false``, ``success: true``, ``ok: true``)
    as carrying no evidence and transparently descends past it to reach
    genuine order/fill evidence deeper in the envelope -- but a layer
    skipped that way can still say something a DEEPER layer flatly
    contradicts: ``{"data": {"success": true, "data": {"success": false,
    "order": {"id": "A", "state": "rejected", "cumulative_quantity":
    "0"}}}}`` has the outer of the two inner layers claiming success while
    the layer ``_unwrap`` actually stops on claims failure. Calling
    ``_unwrap`` alone throws the outer's claim away entirely -- neither the
    raw ``response`` nor ``_unwrap(response)`` retains it, so
    ``_failure_is_broker_authored``'s own two-layer contradiction check
    (comparing exactly those two) never sees the dispute and calls the
    inner failure a clean, broker-authored rejection (Codex review, PR
    #1164 round 14: "Preserve contradictory success flags during envelope
    descent"). Returned so that caller can scan every layer along the
    descent path, not just the outermost and the innermost.

    Deliberately NOT folded back into ``_unwrap`` itself: every other
    caller of ``_unwrap`` wants only the final, most-specific layer, and
    handing all of them a list would be a needless behavior change to
    call sites that were never part of this contradiction.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return [payload]
    layers = [data]
    _depth = 0
    while not _layer_carries_envelope_evidence(data):
        if _depth >= _MAX_ENVELOPE_DESCENT_DEPTH:
            # See `_MAX_ENVELOPE_DESCENT_DEPTH`'s own docstring: bounds a
            # cyclic or pathologically deep envelope instead of looping
            # forever.
            break
        _nested = data.get("data")
        if not isinstance(_nested, dict):
            break
        if _skippable_health_flag_contradicted(data, _nested):
            break
        data = _nested
        layers.append(data)
        _depth += 1
    return layers


def _to_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float parse for Robinhood's stringified numbers; ``None`` -> default.

    Non-finite values (``NaN``/``inf`` from e.g. ``"NaN"``) collapse to ``default`` so they
    can never slip through a numeric comparison — a NaN buying power would make
    ``buying_power < amount`` false and bypass the gate.
    """
    if value is None or isinstance(value, bool):
        # float(True) == 1.0 passes the finiteness check below, so a JSON
        # `true` in a price or balance field read as a 1.0 close/quote -- a
        # ~100% drop that selects the maximum ladder multiplier, the exact
        # spurious-max-BUY the close<=0 guard exists to prevent.
        # dca_config._config_float already rejects bool the same way
        # (2026-09-07 diagnostic, S:ADP-03).
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError too: float(10**1000) raises rather than returning inf,
        # and every value reaching this adapter is JSON-decoded, so an
        # unbounded int is exactly the shape a hostile or buggy payload takes
        # (Codex review, PR #786: "Treat oversized numeric order IDs as
        # unusable" -- swept to the adapter's own parse).
        return default
    return result if math.isfinite(result) else default


#: Numeric tolerance for :func:`_fill_evidence_values_conflict`. Tight enough
#: to absorb only stringification noise -- outer "0" vs inner "0.0", or "5"
#: vs "5.00" -- never a genuine reported-amount disagreement; this module has
#: no pre-existing money/quantity tolerance constant to reuse (unlike
#: ``near_close_dca._INVERSE_MARTINGALE_SOURCE_AGREEMENT_TOLERANCE``, which
#: exists for a different comparison and is loose enough to mask a real
#: fill-figure conflict here).
_FILL_EVIDENCE_NUMERIC_TOLERANCE = 1e-9
# Half a cent: the request is sent as f"{notional:.2f}", so a faithful echo can
# differ from the float request by rounding only.
_PLACEMENT_IDENTITY_NOTIONAL_TOLERANCE_USD = 0.005


def _placement_response_identity_mismatch(
    order: Dict[str, Any],
    *,
    ticker: str,
    ref_id: str,
    notional_usd: Optional[float] = None,
    quantity: Optional[int] = None,
    require_identity: bool = False,
) -> Optional[str]:
    """Why ``order`` does not describe the request that produced it, or ``None``.

    ``place_market_buy``/``place_whole_share_buy`` validated the order id,
    lifecycle state and fill evidence of a placement response but never that
    the order they were handed is the one they asked for: a connector wrapper
    echoing a stale or previous response, a broker-side symbol substitution,
    or a wrong-order echo was booked as this ticker's buy at the requested
    size and consumed budget and caps for the wrong instrument (2026-09-07
    diagnostic, S:ADP-01).

    Only NON-BLANK fields are compared -- Robinhood omits some of these on a
    fresh placement and an absent field says nothing -- and a present field
    that disagrees is decisive: another symbol, another side, another client
    reference, or another size. The echoed value is rendered with
    ``_artifact_safe_repr`` (redacted, not just overflow-safe): this text is
    embedded in ``AmbiguousPlacementResponseError``, which callers persist as
    reconciliation evidence, so a broker-supplied alias carrying an
    account-shaped or credential-bearing string must not travel with it
    (Copilot review, PR #1394: "Redact echoed mismatch values").

    ``require_identity=True`` is the owner-controlled strict mode
    (``RobinhoodBroker(require_placement_identity=True)``): a response that
    echoes neither a symbol nor a client reference is then refused too, so a
    stale replay with every identity field omitted cannot be booked. Off by
    default because the live connector's response shape cannot be verified
    from this repository and requiring the fields blind would fail every real
    placement closed (Copilot review, PR #1394: "Reject placement responses
    missing required identity fields").
    """
    symbol_seen = False
    for symbol_key in ("symbol", "instrument_symbol"):
        symbol = order.get(symbol_key)
        if _is_blank_broker_field(symbol):
            continue
        symbol_seen = True
        if not isinstance(symbol, str) or symbol.strip().upper() != ticker.upper():
            return f"{symbol_key} {_artifact_safe_repr(symbol)} is not the requested {ticker!r}"
    side = order.get("side")
    if not _is_blank_broker_field(side):
        if not isinstance(side, str) or side.strip().lower() != "buy":
            return f"side {_artifact_safe_repr(side)} is not 'buy'"
    reference_seen = False
    for alias in ("ref_id", "client_order_id", "client_order_uuid", "client_id"):
        value = order.get(alias)
        if _is_blank_broker_field(value):
            continue
        reference_seen = True
        if not isinstance(value, str) or value.strip() != ref_id:
            return f"{alias} {_artifact_safe_repr(value)} is not the requested ref_id {ref_id!r}"
    if require_identity and not symbol_seen:
        return "response carries no symbol to prove it describes this request (strict placement identity)"
    if require_identity and not reference_seen:
        return "response carries no client reference to prove it describes this request (strict placement identity)"
    if notional_usd is not None:
        for key in ("dollar_amount", "notional"):
            raw = order.get(key)
            if _is_blank_broker_field(raw):
                continue
            reported = _to_float(raw, default=math.nan)
            if not math.isfinite(reported):
                return f"{key} {_artifact_safe_repr(raw)} is not a usable notional"
            if abs(reported - notional_usd) > _PLACEMENT_IDENTITY_NOTIONAL_TOLERANCE_USD:
                return f"{key} {reported!r} is not the requested notional {notional_usd:.2f}"
    if quantity is not None:
        raw = order.get("quantity")
        if not _is_blank_broker_field(raw):
            reported = _to_float(raw, default=math.nan)
            if not math.isfinite(reported):
                return f"quantity {_artifact_safe_repr(raw)} is not a usable quantity"
            if abs(reported - float(quantity)) > _FILL_EVIDENCE_NUMERIC_TOLERANCE:
                return f"quantity {reported!r} is not the requested quantity {quantity}"
    return None

#: Sub-cent money tolerance for comparing a RECONSTRUCTED notional
#: (``quantity * average_price``) against a directly-reported one --
#: matching the $0.001 tolerance ``_recovered_broker_notional``'s own
#: callers (`near_close_dca`'s notional-agreement checks) already apply,
#: not `_FILL_EVIDENCE_NUMERIC_TOLERANCE` above, which is far too tight
#: for a multiplied, independently-rounded figure (Codex review, PR #1164:
#: "Use a money tolerance for reconstructed notionals").
_RECONSTRUCTED_NOTIONAL_MONEY_TOLERANCE = 0.001


def _fill_evidence_values_conflict(old: Any, new: Any) -> bool:
    """Whether two raw fill-evidence field VALUES genuinely disagree.

    :func:`_merge_envelope_layers_prefer_innermost_nonblank` used to decide
    this with a bare ``str(old) != str(new)`` -- so outer
    ``cumulative_quantity="0"`` and inner ``cumulative_quantity="0.0"``,
    numerically identical but formatted differently, were flagged as a
    conflict. That marker feeds ``_order_identity_conflicts`` in
    ``place_market_buy``, so an otherwise clean, non-conflicting order raised
    ``AmbiguousPlacementResponseError`` and halted the batch over nothing but
    a stringification difference (Codex review, PR #859/#911 round 44:
    "Compare fill aliases numerically before declaring conflicts").

    Parses both sides as floats and compares within
    ``_FILL_EVIDENCE_NUMERIC_TOLERANCE`` when both parse cleanly. Deliberately
    NOT ``_to_float`` here -- that helper collapses an unparseable value to
    ``0.0`` by design (for gate comparisons where "unreadable" and "zero"
    should behave alike), which would make an unparseable figure compare
    equal to a genuine zero and silently swallow a real conflict. A value
    that fails to parse on either side, or two values whose numeric gap
    exceeds the tolerance, are both left to the string-equality fallback (in
    the ``!=`` case) and reported as a real conflict, exactly as before this
    fix -- only the equal-except-for-formatting case changes.
    """
    try:
        if str(old).strip() == str(new).strip():
            return False
    except ValueError:
        # `str()` on a native int beyond Python's integer-to-string
        # conversion digit limit (e.g. `cumulative_quantity=10**5000`)
        # raises `ValueError`, not something the bare comparison above
        # tolerates -- so two identically oversized, genuinely EQUAL
        # values (the same figure echoed by two envelope layers) escaped
        # this handler entirely instead of being recognized as agreeing,
        # the same misdiagnosis the `float()` conversion below already
        # guards against for a merely-oversized-but-otherwise-readable
        # figure (Codex review, PR #1169: "Safely render oversized
        # unreadable fill values"). Falls back to direct equality, which
        # needs no stringification and correctly reports two identical
        # oversized ints as non-conflicting; anything else still falls
        # through to the float-based comparison below.
        if old == new:
            return False
    try:
        old_value = float(old)
        new_value = float(new)
    except (TypeError, ValueError, OverflowError):
        # `float()` on a JSON-decoded integer too large to represent as a
        # float (e.g. `10**1000`) raises `OverflowError`, not `ValueError`
        # -- a malformed-but-syntactically-valid outer fill figure paired
        # with a readable inner one used to escape this handler entirely
        # as a plain, uncaught `OverflowError`, instead of being read the
        # same way every OTHER unparseable pairing here is: left to
        # report a conflict rather than silently treated as agreeing
        # (Codex review, PR #1164: "Catch oversized integers when
        # comparing fill evidence").
        return True
    if not (math.isfinite(old_value) and math.isfinite(new_value)):
        return True
    return abs(old_value - new_value) > _FILL_EVIDENCE_NUMERIC_TOLERANCE


def _first_positive_float(mapping: Dict[str, Any], *keys: str) -> float:
    """Return the first strictly-positive float found across ``keys`` in ``mapping``.

    Returns ``0.0`` if no key is present or every candidate is non-positive/non-numeric,
    so callers can treat that the same as "field not present" rather than special-casing
    each of several possible key names individually.
    """
    for key in keys:
        value = _to_float(mapping.get(key))
        if value > 0:
            return value
    return 0.0


def _is_error_response(data: Any) -> Optional[str]:
    """Check if an un-wrapped response dictionary represents an MCP-level error.

    Returns the error message if one is found, otherwise None.
    """
    if not isinstance(data, dict):
        return None
    # 1. The explicit isError envelope returned by mcp-bridge for network failures.
    if data.get("isError") is True:
        msg = data.get("error")
        return str(msg) if msg else "Unknown network/bridge error"

    # 2. A truthy top-level "error" is always a failure signal on its own, whatever
    # its shape (string, dict, list, ...) — only its rendering differs, so detection
    # never depends on it happening to be a string and empty values stay non-errors.
    error_val = data.get("error")
    if error_val:
        return f"error: {error_val}"

    # 3. "message"/"detail" are informational field names, not intrinsic error
    # indicators — only trust them as a failure reason once an explicit
    # success=false/ok=false flag has already established that this is a failure.
    for flag in ("success", "ok"):
        if data.get(flag) is False:
            for key in ("message", "detail"):
                val = data.get(key)
                if val:
                    return str(val)
            return f"{flag}=false"
    return None

def _response_failure(response: Any) -> Optional[str]:
    """Extract the error message from a raw MCP response, if any.

    Checks for failure markers at two levels: the outer payload and the first
    unwrapped inner ``data`` value (via :func:`_unwrap`). Does not recursively
    unwrap nested envelopes.
    Returns None if the response is valid and contains no error markers.
    """
    data = _unwrap(response)
    if not data and not isinstance(response, dict):
        return "Response is not a valid JSON object"
    # Empty unwrapped dict is usually just empty data, not necessarily an error,
    # unless the raw payload itself was an error envelope.
    return _is_error_response(data) or _is_error_response(response if isinstance(response, dict) else {})


def _failure_is_broker_authored(
    response: Any,
    selected_order: Optional[Dict[str, Any]] = None,
    collection_rows: Any = None,
) -> bool:
    """Whether a failure envelope is the BROKER's own refusal, not the bridge's.

    ``_response_failure`` answers "did this fail", which is the only question a
    fail-closed placement path used to need. Telling a placement apart from a
    PROVEN one needs the second question too, because the two failures mean
    opposite things for the batch:

    * ``{"success": false, "error": "instrument is not tradable"}`` is the
      Robinhood tool answering. The call reached the broker, the broker refused
      it, and no order exists -- the RVII case this halt logic exists to keep
      isolated, so the batch may safely continue past it.
    * ``{"isError": true, "error": "..."}`` is what mcp-bridge returns for a
      network failure or a timeout. It says the ROUND TRIP failed, not that the
      broker refused: an acceptance can be lost on the way back, leaving a live
      order this run knows nothing about. Reading it as a proven rejection
      skipped history reconciliation entirely and let later candidates place
      against deployment and cap totals missing that order (Codex review,
      PR #859: "Keep connector failures in the unresolved-placement path").

    So an explicit ``isError`` disqualifies the envelope outright, and what
    qualifies it is the broker tool's own ``success``/``ok`` false flag. Anything
    else -- a bare ``{"error": ...}`` with no tool envelope around it, a shape
    this adapter has never seen -- is unattributable, and unattributable is
    handled the safe way here: not proven, so the caller stays on the
    reconcile-then-halt path.

    A CONTRADICTORY envelope is unattributable too, not broker-authored:
    ``{"success": false, "data": {"success": true}}`` has the outer wrapper
    reporting failure while the unwrapped inner layer reports success for the
    identical flag. Checking "does ANY layer say false" alone answered yes and
    called this broker-authored, so the caller went on to treat a response an
    inner layer just as clearly disputes as a proven refusal -- skipping
    history reconciliation for an order that may in fact have been accepted
    (Codex review, PR #859: "Keep contradictory success envelopes
    unresolved"). Real broker/bridge responses never disagree with themselves
    this way; a response that does is malformed evidence, exactly like the
    bridge-transport case above, and gets the same safe treatment.

    ``selected_order`` is the SAME row :func:`_select_singular_order_against_
    collection` already picked out of ``data`` (its own ``"order"``/``"orders"``
    nesting) -- a THIRD layer this scan otherwise never sees.
    ``{"success": false, "data": {"order": {"success": true}}}`` disputes the
    outer failure exactly as clearly as an inner ``data.success: true`` does,
    but checking only ``response``/``_unwrap(response)`` missed it entirely,
    so the caller proved a rejection and skipped history reconciliation
    despite the response's own selected order disputing the refusal (Codex
    review, PR #859 round 31: "Inspect success flags in the selected order
    layer"). Optional and passed only by the call site that has already
    selected an order at this point in the flow; omitted (``None``) leaves
    the two-layer check unchanged.

    ``collection_rows`` is the raw ``orders`` collection alongside
    ``selected_order`` -- a FOURTH layer. ``_select_singular_order_against_
    collection`` already proved every entry in that collection agrees with
    ``selected_order`` before returning it non-ambiguous (or the collection
    is absent/promoted-from entirely, in which case this has nothing extra
    to add), so a row here carrying its own ``success``/``ok`` flag is the
    same order described a second time, not a different one. ``{"order":
    {"id": "ORD", "state": "rejected"}, "orders": [{"id": "ORD", "state":
    "rejected", "success": true}]}`` has the collection row disputing the
    outer refusal for the identical order, but scanning only the singular
    row missed it, so the caller proved the rejection and skipped history
    reconciliation despite the collection's own agreeing row disputing the
    refusal (Codex review, PR #859 round 32: "Inspect success flags in
    agreeing collection rows"). Filtered to rows whose own id/order_id
    alias matches ``selected_order``'s -- an id-less selected order, or a
    row naming a different id, contributes nothing here; that mismatch is
    a job for the identifier-conflict checks, not this authorship scan.

    Scans every layer :func:`_all_data_layers` walks through, not just
    ``response`` and ``_unwrap(response)`` -- a layer descent skips past
    because it carries only an affirmative-of-nothing-wrong flag
    (``success: true``, ``ok: true``, ``isError: false``) can still
    contradict the deeper layer descent stops on, and comparing only the
    outermost and innermost layers missed that middle disagreement
    entirely (Codex review, PR #1164 round 14: "Preserve contradictory
    success flags during envelope descent").

    ``_all_data_layers``, not `_unwrap_traversed_layers`, because the
    latter ALSO stops early on a recognized DOMAIN payload key
    (``buying_power``, ``results``) -- exactly right for `_unwrap`'s own
    single-answer descent (see `_layer_carries_envelope_evidence`'s own
    docstring), but wrong here: ``{"success": false, "data": {
    "buying_power": "100", "data": {"success": true}}}`` stopped
    `_unwrap_traversed_layers` at the buying-power layer, so this scan
    never reached the DEEPER ``success: true`` that disputes the outer
    refusal, and `place_market_buy` raised a proven rejection over a
    response that was never unanimous about failing at all (Codex review,
    PR #1164: "Continue scanning past domain fields for success
    conflicts"). `_all_data_layers` walks the full "data" chain
    unconditionally, so a domain field on an intermediate layer no longer
    hides a deeper contradiction from this specific scan.
    """
    _data_layers, _data_layers_truncated = _all_data_layers(response)
    if _data_layers_truncated:
        # See `_all_data_layers`'s own docstring ("Fail closed when
        # envelope descent reaches its cap"): a bounded-but-truncated scan
        # may be hiding a deeper layer that disputes the failure this
        # function would otherwise call proven. Not proven is the safe
        # reading -- the caller then treats the response as ambiguous
        # rather than a clean broker-authored rejection.
        return False
    payloads = [
        p
        for p in (response, *_data_layers, selected_order)
        if isinstance(p, dict)
    ]
    if isinstance(collection_rows, list) and isinstance(selected_order, dict):
        # Blank-aware on BOTH sides of this comparison, not a raw
        # `... or ...` fallback: a whitespace-only "id" alongside a real
        # "order_id" -- on the selected order, or on a collection row --
        # is truthy in Python, so the raw fallback picked the blank
        # placeholder over the usable alias and this match failed even
        # though `_select_singular_order_against_collection` had already
        # matched the SAME row through its "order_id" alias. A response
        # whose collection row disputes success then went unmatched here,
        # silently discarding that dispute (Codex review, PR #1164: "Fall
        # back past blank IDs when matching collection rows").
        _selected_id = _first_usable_order_id_alias(selected_order)
        if not _is_blank_broker_field(_selected_id):
            # `_safe_order_id_str`, not a bare `str(...).strip()`: a native
            # oversized numeric id (beyond Python's integer-to-string
            # conversion digit limit) raised `ValueError` here instead of
            # being treated as an id this match simply cannot compare
            # (Codex review, PR #1169: "Check failure-envelope ID bounds
            # before stringifying" -- the same overflow class, reached via
            # this collection-row-matching path).
            _selected_id = _safe_order_id_str(_selected_id)
            if _selected_id is not None:
                payloads.extend(
                    row
                    for row in collection_rows
                    if isinstance(row, dict)
                    and _safe_order_id_str(_first_usable_order_id_alias(row) or "") == _selected_id
                )
    if any(p.get("isError") is True for p in payloads):
        return False
    saw_false = any(p.get("success") is False or p.get("ok") is False for p in payloads)
    saw_true = any(p.get("success") is True or p.get("ok") is True for p in payloads)
    if saw_false and saw_true:
        return False
    return saw_false


# The same seven fill-evidence aliases near_close_dca._reconciled_order_may_
# have_executed reads (kept as a plain tuple literal here too, matching that
# function's own convention rather than importing a shared constant neither
# module defines one for).
#: Dollar-denominated fill-evidence aliases -- comparable to EACH OTHER
#: (all report the same executed notional in the same unit), but never
#: to a quantity alias below: ``executed_notional="5"`` and
#: ``cumulative_quantity="0.5"`` are $5 and 0.5 SHARES, not two readings
#: of the same figure, and comparing them numerically as though they
#: were is comparing dollars to shares (Codex review, PR #1164: "Compare
#: fill aliases within compatible units").
_FILL_EVIDENCE_NOTIONAL_ALIASES = (
    "executed_notional",
    "executed_dollar_amount",
    "filled_notional",
)
#: Share-count fill-evidence aliases -- comparable to each other, never
#: to a notional alias above, for the identical unit-mismatch reason.
_FILL_EVIDENCE_QUANTITY_ALIASES = (
    "cumulative_quantity",
    "executed_quantity",
    "filled_quantity",
    "quantity_filled",
)
_FILL_EVIDENCE_ALIASES = _FILL_EVIDENCE_NOTIONAL_ALIASES + _FILL_EVIDENCE_QUANTITY_ALIASES


def _order_lifecycle_aliases_conflict(order: Dict[str, Any]) -> bool:
    """Whether ONE order row's own ``state`` and ``status`` fields disagree.

    ``{"id": "ORD", "state": "queued", "status": "rejected"}`` is a single
    row telling two different stories about whether the order is still
    live. Every caller that reads a row's lifecycle picks ``state or
    status`` -- correct when only one is present, but silently discarding
    the OTHER field the moment both are present and disagree, exactly the
    same shape :func:`_order_identifier_aliases_conflict` already guards
    for the ``id``/``order_id`` pair. ``False`` whenever either field is
    absent -- a single lifecycle value is the ordinary, unambiguous case.

    "Absent" is ``_is_blank_broker_field``'s definition -- ``None`` or an
    empty/whitespace-only string -- not a bare ``is None`` check: ``{"id":
    "ORD", "state": "", "status": "queued"}`` has a genuinely blank
    ``state`` next to a real ``status``, the ordinary single-alias case
    every routing call site already handles correctly by falling back to
    ``status``. Reading the blank ``""`` as a real, disagreeing value here
    declared a conflict for an otherwise unambiguous accepted order, and
    the caller raised ``AmbiguousPlacementResponseError`` and halted the
    batch over it (Codex review, PR #859 round 33: "Treat blank lifecycle
    aliases as absent").

    Normalized via the SAME ``near_close_dca._normalize_broker_lifecycle_
    alias`` the shared terminal classifier uses before comparing: Robinhood
    reports both "cancelled" and "canceled" for the identical terminal
    state, so ``state="cancelled", status="canceled"`` is one broker
    answering itself in two dialects, not two disagreeing answers. A bare
    lowercased-string compare read that spelling difference alone as a
    conflict and raised ``AmbiguousPlacementResponseError`` over a row that
    was actually self-consistent (Codex review, PR #859/#911 round 44:
    "Canonicalize canceled lifecycle aliases before comparing").
    """
    state_value = order.get("state")
    status_value = order.get("status")
    if _is_blank_broker_field(state_value) or _is_blank_broker_field(status_value):
        return False
    return _normalize_broker_lifecycle_alias(state_value) != _normalize_broker_lifecycle_alias(status_value)


def _order_rows_contradict(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Whether two order-row dicts, both naming the SAME broker id, disagree.

    An id match alone does not prove two representations describe the
    order identically: ``{"id": "ORD", "state": "rejected"}`` and
    ``{"id": "ORD", "state": "queued"}`` name the same order but tell
    opposite stories about whether it is still live. Checked across every
    key present on EITHER side -- not just ``state`` -- so a fill-evidence
    field (``executed_notional``, ``cumulative_quantity``, ...) reported
    differently by the two representations is caught the same way (Codex
    review, PR #859: "Require order representations to agree beyond their
    IDs"). A key present on only ONE side is not a contradiction -- it is
    simply more (or less) information than the other representation
    happened to carry, not a conflicting claim about it. String values
    compare case-insensitively (broker state strings are not reliably
    cased the same way twice); everything else compares exactly.

    ``state`` and ``status`` are resolved to one lifecycle value per side
    before that generic key-by-key pass, the same ``state or status``
    fallback used a few lines below in
    ``_failure_envelope_state_permits_proven_rejection``. Comparing them
    only under identical key names let ``{"id": "ORD", "state":
    "rejected"}`` sit next to ``{"id": "ORD", "status": "queued"}`` and
    read as agreement -- neither side had a value under the OTHER
    representation's key, so the per-key loop skipped both as "only on one
    side" instead of catching that they name the same field (Codex review,
    PR #859: "Treat state/status alias conflicts as unresolved").

    A FILL-EVIDENCE alias present on only ONE side is a narrower exception
    to "one-sided presence is not a conflict": ``{"id": "ORD", "state":
    "rejected"}`` alongside ``{"id": "ORD", "state": "rejected",
    "executed_notional": "5"}`` are not disagreeing about ``state``, but
    the collection's reported ``$5`` fill is real evidence the singular row
    is silent about, not silent BECAUSE nothing happened. Letting that
    silence read as agreement declared the two representations consistent,
    and the caller then merged only the singular (fill-blind) row into its
    own evidence, discarding the reported execution entirely (Codex
    review, PR #859: "Treat collection-only fill evidence as
    contradictory"). Checked via the SAME per-alias fill-or-not reading
    ``_reconciled_order_may_have_executed`` itself uses, so this only ever
    fires when the lone value actually reports a fill (or is unreadable,
    read identically) -- an explicit zero on one side and silence on the
    other is still not a conflict.
    A row that disagrees with ITSELF -- ``state="rejected"`` alongside its
    own ``status="queued"`` -- is checked first and independently of the
    cross-row comparison below: resolving each side to a single ``state or
    status`` value before comparing let a self-conflicting collection row
    (both fields present, disagreeing) collapse to whichever one happened
    to be truthy, silently discarding its OTHER field's own contradictory
    claim. A row that cannot even agree with itself has not been shown to
    agree with the other representation either (Codex review, PR #859
    round 29: "Reject internally conflicting collection rows").

    A fill-evidence field present on BOTH sides is compared numerically via
    :func:`_fill_evidence_values_conflict`, not the generic string/exact
    comparison below: outer ``cumulative_quantity="1"`` and inner
    ``cumulative_quantity="1.0"`` are the SAME reported figure formatted
    differently, and the raw ``!=`` this function otherwise applies read
    them as disagreeing -- discarding an otherwise clean, agreeing order as
    ambiguous over nothing but a stringification difference (Codex review,
    PR #859/#911 round 45: "Compare fill aliases numerically across
    duplicate rows").
    """
    if _order_lifecycle_aliases_conflict(a) or _order_lifecycle_aliases_conflict(b):
        return True
    # Each alias is normalized BEFORE the `state`-or-`status` fallback is
    # applied, not after -- a raw `a.get("state") or a.get("status")`
    # picks a whitespace-only placeholder over a real value on the SAME
    # row, since a nonblank-but-whitespace string is still truthy in
    # Python: `{"id": "A", "state": "   "}` alongside `{"id": "A",
    # "status": "queued"}` has no real state/status on the first row at
    # all, but the raw fallback selected `"   "` as `state_a` anyway
    # (never even looking at a `status` alias that happened to be absent
    # here), which then normalized to `None` and compared as a genuine
    # conflict against the other row's `"queued"` -- discarding an
    # otherwise clean, agreeing match as ambiguous over an ordinary blank
    # placeholder, the same class of false conflict the terminal-row
    # classifier a few hundred lines below (`near_close_dca.
    # terminal_broker_row_proves_no_order`) already avoids by normalizing
    # each alias first (Codex review, PR #1164: "Treat blank lifecycle
    # aliases as absent across rows").
    state_a = _normalize_broker_lifecycle_alias(a.get("state")) or _normalize_broker_lifecycle_alias(a.get("status"))
    state_b = _normalize_broker_lifecycle_alias(b.get("state")) or _normalize_broker_lifecycle_alias(b.get("status"))
    if state_a is not None and state_b is not None:
        # Normalized via the SAME near_close_dca._normalize_broker_
        # lifecycle_alias the within-row check just above (_order_
        # lifecycle_aliases_conflict) already uses: the singular row
        # reporting state="cancelled" alongside a collection row's own
        # status="canceled" is one broker answering itself in two
        # dialects for the SAME terminal state, not two disagreeing
        # answers. A bare lowercased-string compare read that spelling
        # difference alone as a conflict and returned an otherwise clean,
        # agreeing match as ambiguous, sending a proven terminal zero-fill
        # rejection into unresolved reconciliation instead (Codex review,
        # PR #859/#911 round 46: "Canonicalize lifecycle aliases across
        # order rows"). Both sides are already normalized above, so no
        # further conversion is needed here.
        if state_a != state_b:
            return True
    # Fill-evidence aliases are compared as ONE combined reading per side,
    # not key-by-key: two representations of the SAME order can report
    # the identical fill through DIFFERENT alias names -- a singular row's
    # `executed_notional="5"` and its same-ID collection row's
    # `filled_notional="5.00"` are the SAME $5 fill, just spelled through
    # the two different aliases `_recovered_broker_notional` and its
    # siblings already treat as interchangeable. The per-key loop below
    # compares same-NAMED keys only, so `executed_notional` was present
    # only on side A and `filled_notional` only on side B -- each read as
    # "fill evidence on just one side", which the one-sided rule a few
    # lines below (correctly, for a GENUINELY silent other side) treats as
    # a conflict. That declared an otherwise consistent, agreeing accepted
    # order ambiguous and halted the batch over nothing but which alias
    # name the two representations happened to use (Codex review, PR
    # #1164: "Compare equivalent fill aliases across duplicate rows").
    #
    # Grouped by UNIT -- notional aliases against notional, quantity
    # against quantity, never mixed -- not one flat pool of every alias:
    # a first version of this fix compared "$5" (`executed_notional`)
    # against "0.5" (`cumulative_quantity`) as though they were two
    # readings of the same figure, when they are a dollar amount and a
    # share count that happen to disagree numerically for the most
    # ordinary of reasons (a sub-dollar-per-share price). That declared a
    # perfectly consistent accepted order ambiguous over comparing dollars
    # to shares (Codex review, PR #1164: "Compare fill aliases within
    # compatible units").
    #
    # Each alias is unwrapped through `_unwrap_fill_alias_value` before
    # comparison, not read raw: a Robinhood money value is a scalar OR an
    # ``{"amount": ...}`` mapping (`_finite_nonnegative_float`'s own
    # documented contract), and one duplicate representation reporting
    # ``executed_notional={"amount": "5"}`` against another's plain
    # ``filled_notional="5"`` is the identical figure in two encodings the
    # rest of this module already treats as equivalent. Comparing the raw
    # mapping fed `float(dict)` straight into `_fill_evidence_values_
    # conflict`, which cannot parse it and calls the pair contradictory --
    # halting an otherwise consistent, agreeing accepted order over nothing
    # but which encoding the two representations happened to use (Codex
    # review, PR #1164 round 13: "Unwrap mapping-shaped fill aliases before
    # comparing rows").
    #
    # The one-sided check below (a group where only ONE side reports any
    # value) is applied only once every group has been scanned for BOTH-
    # sided conflicts, and only when the OPPOSING row has no fill evidence
    # in ANY unit group -- not per group in isolation. A first version of
    # this fix ran the one-sided rule inside the same per-group loop as the
    # both-sided comparison, so `executed_notional="5"` alone on one row
    # and `cumulative_quantity="5"` alone on the other -- two rows agreeing
    # on the identical $5/5-share fill through complementary aliases --
    # were compared group-by-group, found "no evidence" for the OTHER
    # group each time, and were declared one-sided contradictory even
    # though each row's own evidence, taken as a whole, agrees with the
    # other's (Codex review, PR #1164 round 14: "Avoid flagging
    # complementary fill units as one-sided").
    _values_by_group_a: Dict[Tuple[str, ...], List[Any]] = {}
    _values_by_group_b: Dict[Tuple[str, ...], List[Any]] = {}
    for _alias_group in (_FILL_EVIDENCE_NOTIONAL_ALIASES, _FILL_EVIDENCE_QUANTITY_ALIASES):
        _values_a = [
            _unwrap_fill_alias_value(a.get(_field))
            for _field in _alias_group
            if not _is_blank_broker_field(_unwrap_fill_alias_value(a.get(_field)))
        ]
        _values_b = [
            _unwrap_fill_alias_value(b.get(_field))
            for _field in _alias_group
            if not _is_blank_broker_field(_unwrap_fill_alias_value(b.get(_field)))
        ]
        _values_by_group_a[_alias_group] = _values_a
        _values_by_group_b[_alias_group] = _values_b
        # A row contradicting ITSELF through two aliases in the SAME unit
        # group -- e.g. `executed_notional="5"` alongside `filled_notional=
        # "6"` on the identical row -- is contradictory regardless of what
        # the OTHER row reports: if the other row's only evidence sits in
        # the COMPLEMENTARY unit group (e.g. `cumulative_quantity="1"`),
        # `_values_a`/`_values_b` never BOTH populate this group, so the
        # cross-side check just below never runs and this self-conflicting
        # row was read as agreeing with a consistent-looking other side.
        # `_select_singular_order_against_collection` then accepted the
        # response as unambiguous even though this row cannot describe one
        # coherent fill on its own (Codex review, PR #1169: "Reject
        # conflicting fill aliases within each row").
        if len(_values_a) > 1 and any(
            _fill_evidence_values_conflict(_values_a[i], _values_a[j])
            for i in range(len(_values_a))
            for j in range(i + 1, len(_values_a))
        ):
            return True
        if len(_values_b) > 1 and any(
            _fill_evidence_values_conflict(_values_b[i], _values_b[j])
            for i in range(len(_values_b))
            for j in range(i + 1, len(_values_b))
        ):
            return True
        if _values_a and _values_b:
            if any(_fill_evidence_values_conflict(_va, _vb) for _va in _values_a for _vb in _values_b):
                return True
    # Zero-versus-positive evidence CROSS unit groups is contradictory too,
    # not "complementary" -- unlike two positive figures in different units
    # (a genuine $/share pair for the same fill), an explicit ZERO in one
    # unit alongside a POSITIVE figure in the other cannot both describe
    # the same execution: `executed_notional="0"` (no dollars moved) beside
    # a same-ID row's `cumulative_quantity="5"` (5 shares moved) disagree
    # about whether anything filled at all, exactly as directly as an
    # explicit zero notional already conflicts with a positive notional on
    # the SAME side a few hundred lines above in `_recovered_broker_
    # notional`. Both `*_has_any_fill_evidence` flags being true (each side
    # has evidence, just in different units) let this pair skip the one-
    # sided check entirely and read as agreeing, dropping the structural-
    # ambiguity signal a later exact-ID history match could otherwise
    # resolve past (Codex review, PR #1164: "Reject zero-versus-positive
    # complementary fill units").
    def _values_include_zero(_values: List[Any]) -> bool:
        for _v in _values:
            try:
                if float(_v) == 0:
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
        return False

    def _values_include_positive(_values: List[Any]) -> bool:
        for _v in _values:
            try:
                _fv = float(_v)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(_fv) and _fv > 0:
                return True
        return False

    _a_notional = _values_by_group_a[_FILL_EVIDENCE_NOTIONAL_ALIASES]
    _b_notional = _values_by_group_b[_FILL_EVIDENCE_NOTIONAL_ALIASES]
    _a_quantity = _values_by_group_a[_FILL_EVIDENCE_QUANTITY_ALIASES]
    _b_quantity = _values_by_group_b[_FILL_EVIDENCE_QUANTITY_ALIASES]
    if (_values_include_zero(_a_notional) and _values_include_positive(_b_quantity)) or (
        _values_include_zero(_b_notional) and _values_include_positive(_a_quantity)
    ):
        return True
    if (_values_include_zero(_a_quantity) and _values_include_positive(_b_notional)) or (
        _values_include_zero(_b_quantity) and _values_include_positive(_a_notional)
    ):
        return True
    # A quantity-side row that ALSO carries an `average_price` sibling is no
    # longer just "shares, incomparable to dollars" -- `quantity *
    # average_price` reconstructs the same notional unit the other side is
    # reporting directly, and the two are directly comparable once
    # reconstructed. A singular row's `executed_notional="5"` beside a
    # same-ID collection row's `cumulative_quantity="2", average_price="10"`
    # ($20 once reconstructed) passed the notional-only-vs-quantity-only
    # "complementary units" reading above as agreeing, even though the rows
    # report $5 against $20 for the identical order -- `_select_singular_
    # order_against_collection` then accepted them, and for `state="filled"`
    # `place_market_buy` returned the collection's order ID instead of
    # preserving the contradictory execution amount for reconciliation
    # (Codex review, PR #1164: "Check reconstructed amounts across
    # complementary fill units"). Compared via `_RECONSTRUCTED_NOTIONAL_
    # MONEY_TOLERANCE` -- the same $0.001 sub-cent tolerance
    # `_recovered_broker_notional`'s own callers already apply -- not
    # `_fill_evidence_values_conflict`'s far tighter `_FILL_EVIDENCE_
    # NUMERIC_TOLERANCE` (1e-9), which is sized for two SAME-formatted
    # scalars (``"5"`` vs ``"5.0"``), not a multiplied reconstruction:
    # `cumulative_quantity="0.3333", average_price="15"` reconstructs to
    # `4.9995`, a few tenths of a cent off a genuinely agreeing
    # `executed_notional="5.00"` purely from independent rounding on each
    # side, and the tight tolerance declared that agreement a conflict,
    # halting the batch over ordinary sub-cent rounding (Codex review, PR
    # #1164: "Use a money tolerance for reconstructed notionals").
    def _reconstructed_notional_conflicts(reconstructed: float, direct: Any) -> bool:
        try:
            _direct_f = float(direct)
        except (TypeError, ValueError, OverflowError):
            return True
        if not math.isfinite(_direct_f):
            return True
        return abs(reconstructed - _direct_f) > _RECONSTRUCTED_NOTIONAL_MONEY_TOLERANCE

    # A row's `average_price` can be PRESENT but unreadable -- malformed,
    # non-numeric, or non-finite -- and that is a different reading than
    # the price being ABSENT altogether. `executed_notional="5"` beside a
    # same-ID row's `cumulative_quantity="1", average_price="bad"` used to
    # return no reconstructed value for either reason alike, which let
    # `_select_singular_order_against_collection` treat the pair as
    # ordinary complementary-unit evidence and accept it -- discarding the
    # collection's own unreadable valuation instead of preserving it for
    # reconciliation (Codex review, PR #1164: "Reject unreadable prices in
    # complementary fill evidence"). A row with a genuinely ABSENT price
    # still gets the ordinary "notional-only vs quantity-only" reading
    # unchanged (this function returns no reconstruction, but does not
    # itself force a conflict); only a PRESENT-but-unusable price does --
    # and only when the row ALSO supplies at least one usable POSITIVE
    # quantity that would actually depend on that price, not merely a
    # non-empty quantity list. A terminal row carrying nothing but a
    # malformed `average_price` and no quantity at all -- a rejected
    # singular row with `average_price="bad"` beside a same-ID collection
    # row's agreeing `executed_notional="0"` -- has nothing to reconstruct
    # in the first place, so flagging the unreadable price as a conflict
    # turned a proven, agreeing zero-fill rejection into an
    # `AmbiguousPlacementResponseError` over a price nobody needed (Codex
    # review, PR #1164: "Require quantity before rejecting an unreadable
    # price"). A genuine EXPLICIT ZERO quantity is the identical case --
    # `cumulative_quantity="0", average_price="bad"` beside a same-ID
    # `executed_notional="0"` both consistently prove no fill, and a zero
    # quantity needs no price to value it at all -- so `bool(quantities)`
    # alone (true for a single "0" entry) still forced a false conflict
    # over a price that was never relevant (Codex review, PR #1164/#1169:
    # "Ignore unreadable prices when executed quantity is zero"). Only a
    # quantity list containing at least one READABLE, POSITIVE value
    # actually depends on the price.
    # A row's OWN `average_price` can be genuinely ABSENT while a same-ID
    # counterpart row supplies it instead: a singular `executed_notional="5",
    # average_price="10"` beside a same-ID collection row's bare
    # `cumulative_quantity="2"` (no `average_price` of its own) used to
    # reconstruct nothing for either side -- the quantity-bearing row has no
    # price of its own to multiply by, and the price-bearing row has no
    # quantity to reconstruct -- so `_select_singular_order_against_
    # collection` read the pair as ordinary complementary-unit evidence and
    # accepted it, silently dropping the fact that $5 direct and 2 x $10 =
    # $20 reconstructed describe the SAME shared order (Codex review, PR
    # #1164: "Use prices from either duplicate representation"). Falling
    # back to `other`'s `average_price` when `row`'s own is BLANK (never
    # when it is merely unreadable -- a present-but-malformed price on
    # `row` itself still gets its own unreadable-price handling below,
    # unrelated to what `other` happens to report) lets either agreed
    # same-ID representation's price value the pair's quantity.
    def _reconstructed_notionals(
        row: Dict[str, Any], other: Dict[str, Any], quantities: List[Any]
    ) -> Tuple[List[float], bool]:
        _avg_price = _unwrap_fill_alias_value(row.get("average_price"))
        if _is_blank_broker_field(_avg_price):
            _avg_price = _unwrap_fill_alias_value(other.get("average_price"))
        if _is_blank_broker_field(_avg_price):
            return [], False
        try:
            _avg_price_f = float(_avg_price)
        except (TypeError, ValueError, OverflowError):
            return [], _values_include_positive(quantities)
        if not math.isfinite(_avg_price_f):
            return [], _values_include_positive(quantities)
        _reconstructed = []
        for _qty in quantities:
            try:
                _qty_f = float(_qty)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(_qty_f):
                _reconstructed.append(_qty_f * _avg_price_f)
        return _reconstructed, False

    _a_reconstructed, _a_price_unreadable = _reconstructed_notionals(a, b, _a_quantity)
    _b_reconstructed, _b_price_unreadable = _reconstructed_notionals(b, a, _b_quantity)
    if _a_price_unreadable and _b_notional:
        return True
    if _b_price_unreadable and _a_notional:
        return True
    if _a_reconstructed and _b_notional:
        if any(_reconstructed_notional_conflicts(_ra, _vb) for _ra in _a_reconstructed for _vb in _b_notional):
            return True
    if _b_reconstructed and _a_notional:
        if any(_reconstructed_notional_conflicts(_rb, _va) for _rb in _b_reconstructed for _va in _a_notional):
            return True
    _a_has_any_fill_evidence = any(_values_by_group_a.values())
    _b_has_any_fill_evidence = any(_values_by_group_b.values())
    # A value present in a fill-evidence alias but UNREADABLE as a finite,
    # non-negative number -- `executed_notional="bad"` beside a same-ID
    # row's `cumulative_quantity="1"` -- used to satisfy the "has fill
    # evidence" flags above just as readily as a genuine figure, which let
    # it qualify for the SAME complementary-units exception a real $/share
    # pair earns: neither the both-sided comparison (different unit
    # groups) nor the zero-vs-positive check (the malformed value fails to
    # parse as either) caught it, and the one-sided rule below never fires
    # either, since the OPPOSING row's readable evidence means neither
    # side reads as "no evidence in any group". For a `state="filled"`
    # response, `place_market_buy` consequently returned the order ID and
    # persisted only the singular row, dropping the collection's own
    # positive quantity (Codex review, PR #1164: "Reject unreadable
    # complementary fill evidence"). Malformed, non-finite, or negative
    # evidence on one side alongside ANY genuine fill evidence on the
    # other is a real conflict, not complementary agreement -- we cannot
    # verify what the unreadable figure would have agreed with.
    def _is_readable_nonnegative_fill_value(value: Any) -> bool:
        try:
            _fv = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(_fv) and _fv >= 0

    _a_has_unreadable_fill_value = any(
        not _is_readable_nonnegative_fill_value(_v) for _values in _values_by_group_a.values() for _v in _values
    )
    _b_has_unreadable_fill_value = any(
        not _is_readable_nonnegative_fill_value(_v) for _values in _values_by_group_b.values() for _v in _values
    )

    # Fresh evidence beyond the fix above: both sides can report the exact
    # SAME unreadable value in the SAME unit group -- `executed_notional=
    # "N/A"` on both rows -- and the blanket "unreadable on one side plus
    # ANY fill evidence on the other" check above treated that identical,
    # duplicate encoding as a conflict, since the opposing side's own copy
    # of that same "N/A" already counts as "has fill evidence". Two rows
    # reporting the identical unreadable figure are not disputing anything
    # -- they cannot even both be wrong about the SAME value in the SAME
    # way by accident -- so this discarded a usable order ID (or, for a
    # terminal response, prevented a later clean exact-ID history match
    # from resolving it) over no real disagreement at all (Codex review,
    # PR #1169: "Let identical unreadable fill values agree"). Compared
    # per unit group (never across groups -- an "N/A" in one unit
    # coinciding with an "N/A" in a DIFFERENT unit is not evidence the two
    # rows agree about anything) and only treated as agreeing when the
    # opposing row's own value(s) in that SAME group are unreadable too and
    # normalize to the identical set of strings; a readable value, or a
    # genuinely DIFFERENT unreadable one, in that same group still
    # conflicts exactly as before.
    def _unreadable_value_reprs(values: List[Any]) -> set:
        # A native int beyond Python's integer-to-string conversion digit
        # limit (e.g. a malformed `cumulative_quantity=10**5000`) is
        # already classified unreadable by `_is_readable_nonnegative_fill_
        # value` above (it catches the `OverflowError` converting to
        # `float`), but this `str(_v)` call raises that SAME digit-limit
        # `ValueError` the instant anything renders it -- escaping this
        # helper and aborting `_order_rows_contradict()` entirely during
        # placement/history consistency checks, instead of the two rows'
        # otherwise-agreeing (or safely-unresolved) unreadable evidence
        # ever getting compared (Codex review, PR #1169: "Safely render
        # oversized unreadable fill values"). Falls back to a placeholder
        # keyed by type, matching this module's existing `_log_safe_repr`
        # fail-safe posture elsewhere.
        _reprs = set()
        for _v in values:
            if _is_readable_nonnegative_fill_value(_v):
                continue
            try:
                _reprs.add(str(_v).strip().lower())
            except ValueError:
                _reprs.add(f"<unrepresentable:{type(_v).__name__}>")
        return _reprs

    _a_has_readable_fill_evidence = any(
        _is_readable_nonnegative_fill_value(_v) for _values in _values_by_group_a.values() for _v in _values
    )
    _b_has_readable_fill_evidence = any(
        _is_readable_nonnegative_fill_value(_v) for _values in _values_by_group_b.values() for _v in _values
    )

    def _unreadable_conflicts_with_other_side(
        values_by_group: Dict[Tuple[str, ...], List[Any]],
        other_values_by_group: Dict[Tuple[str, ...], List[Any]],
        other_has_any_fill_evidence: bool,
        other_has_readable_fill_evidence: bool,
    ) -> bool:
        for _group, _values in values_by_group.items():
            _unreadable_reprs = _unreadable_value_reprs(_values)
            if not _unreadable_reprs:
                continue
            # The other side reporting NOTHING at all, in any group, is
            # silence, not a dispute -- unreadable evidence alone cannot
            # be contradicted by the total absence of any evidence to
            # compare it against (matches the original blanket check's own
            # "other side has any fill evidence at all" gate).
            if not other_has_any_fill_evidence:
                continue
            # A genuine READABLE figure anywhere on the other side is
            # exactly the "opposing row supplies readable evidence" case
            # this check must still conflict on, whatever group it sits
            # in -- unaffected by the identical-unreadable-value exemption
            # below, which is scoped to unreadable-vs-unreadable only.
            if other_has_readable_fill_evidence:
                return True
            # The other side has no READABLE evidence anywhere (only
            # unreadable values), but does report SOME evidence -- compare
            # THIS SAME group's own unreadable reprs. An exact match is
            # the "duplicate encoding of the same information" case that
            # must agree; anything else -- a genuinely different
            # unreadable value in this group, or the other side reporting
            # nothing in THIS group while reporting unreadable evidence
            # elsewhere -- is still the real disagreement the original
            # blanket check existed to catch.
            if _unreadable_reprs != _unreadable_value_reprs(other_values_by_group.get(_group, [])):
                return True
        return False

    if _unreadable_conflicts_with_other_side(
        _values_by_group_a, _values_by_group_b, _b_has_any_fill_evidence, _b_has_readable_fill_evidence
    ):
        return True
    if _unreadable_conflicts_with_other_side(
        _values_by_group_b, _values_by_group_a, _a_has_any_fill_evidence, _a_has_readable_fill_evidence
    ):
        return True
    for _alias_group in (_FILL_EVIDENCE_NOTIONAL_ALIASES, _FILL_EVIDENCE_QUANTITY_ALIASES):
        _values_a = _values_by_group_a[_alias_group]
        _values_b = _values_by_group_b[_alias_group]
        # Only fields actually PRESENT on the source row are forwarded --
        # not every field in the alias group, blanket-read via `.get()`.
        # `_reconciled_order_may_have_executed` treats an explicit ``None``
        # as unreadable, blocking fill evidence, deliberately different
        # from a field being absent altogether; materializing every alias
        # in the group (present or not) turned an ordinary ABSENT field
        # into an explicit null, so a cancelled row with a genuine, single
        # explicit zero (`{"cumulative_quantity": "0"}`) was read as
        # unresolved instead of the clean terminal zero-fill it actually
        # reports, and `_order_rows_contradict` raised
        # `AmbiguousPlacementResponseError` over a consistent rejection
        # (Codex review, PR #1164 round 15: "Preserve absent aliases when
        # checking one-sided zero fills").
        if _values_a and not _values_b and not _b_has_any_fill_evidence:
            if _reconciled_order_may_have_executed({_field: a[_field] for _field in _alias_group if _field in a}):
                return True
        elif _values_b and not _values_a and not _a_has_any_fill_evidence:
            if _reconciled_order_may_have_executed({_field: b[_field] for _field in _alias_group if _field in b}):
                return True
    for key in set(a) | set(b):
        if key in ("id", "order_id", "state", "status") or key in _FILL_EVIDENCE_ALIASES:
            continue
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            continue
        # `average_price` (and any future numeric field added to this
        # generic catch-all loop) needs the same numeric comparison every
        # other fill-evidence field already gets, not a raw string
        # comparison: `average_price="10"` vs `average_price="10.0"` --
        # equivalent numeric spellings, and exactly the kind of
        # stringification noise `_reconstructed_notional_conflicts` above
        # already tolerates via `_RECONSTRUCTED_NOTIONAL_MONEY_TOLERANCE`
        # -- compared as raw strings declared them contradictory, so a
        # successful placement's usable order ID was discarded and
        # `place_market_buy` raised an unnecessary `AmbiguousPlacement
        # ResponseError` (Codex review, PR #1169: "Compare average prices
        # numerically across duplicate rows"). Also unwrapped through
        # `_unwrap_fill_alias_value` first, matching every other reader of
        # this field, for the supported ``{"amount": ...}`` money-wrapper
        # encoding. Falls through to the generic string/equality
        # comparison below when either side fails to parse as a finite
        # number -- unchanged behavior for a genuinely malformed price.
        if key == "average_price":
            _va_price = _unwrap_fill_alias_value(va)
            _vb_price = _unwrap_fill_alias_value(vb)
            try:
                _va_price_f = float(_va_price)
                _vb_price_f = float(_vb_price)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if math.isfinite(_va_price_f) and math.isfinite(_vb_price_f):
                    if abs(_va_price_f - _vb_price_f) > _RECONSTRUCTED_NOTIONAL_MONEY_TOLERANCE:
                        return True
                    continue
        if isinstance(va, str) and isinstance(vb, str):
            if va.strip().lower() != vb.strip().lower():
                return True
        elif va != vb:
            return True
    return False


def _order_identifier_aliases_conflict(order: Dict[str, Any]) -> bool:
    """Whether an order row's ``id`` and ``order_id`` aliases name DIFFERENT orders.

    ``{"id": "DEAD", "order_id": "LIVE", "state": "rejected"}`` carries two
    non-blank identifier aliases that disagree. Every call site in this
    module that reads an order's id picks ``id or order_id`` -- correct
    when only one alias is present, but silently discarding the SECOND,
    possibly-live order's identity when both are present and disagree.
    ``terminal_broker_row_proves_no_order`` never looks at either alias, so
    it proves the row's ``state`` (here, "rejected") regardless, and the
    caller then raises a proven rejection carrying only the FIRST alias's
    id -- the row's own second identifier, which may in fact be a live
    order, is discarded from reconciliation and cap accounting entirely
    (Codex review, PR #859 round 27: "Keep conflicting identifier aliases
    unresolved"). ``False`` whenever either alias is blank/absent -- a
    single identifier is exactly the ordinary, unambiguous case every
    other caller in this module already handles.
    """
    id_a, id_b = order.get("id"), order.get("order_id")
    if _is_blank_broker_field(id_a) or _is_blank_broker_field(id_b):
        return False
    # Compared through `_normalized_order_id` (the durable numeric-aware
    # canonicalizer), not a raw `str(...).strip()`: a successful response
    # can carry equivalent aliases in two different SUPPORTED numeric
    # encodings -- `{"id": 123.0, "order_id": "123"}` -- and the raw
    # string comparison reported that as a disagreement (`"123.0" !=
    # "123"`) even though both spell the identical broker order. The
    # caller then converted an accepted response into an
    # AmbiguousPlacementResponseError and halted later placements over
    # what is, in fact, complete agreement (Codex review, PR #1169:
    # "Canonicalize equivalent numeric order aliases").
    #
    # A KNOWN-UNUSABLE alias (a truthy sentinel like `id=True`, which
    # `_normalized_order_id` correctly rejects to `None`) is not treated
    # as disagreeing with a usable one, via
    # `_is_unusable_live_placement_order_id` -- not just the blank check
    # above: `{"id": true, "order_id": "REAL", "state": "queued"}` is an
    # accepted response `place_market_buy`'s own primary-then-secondary
    # fallback correctly resolves to "REAL", but this comparison still
    # ran `_normalized_order_id(true) != _normalized_order_id("REAL")`
    # (`None != "REAL"`), reporting a conflict for a response with only
    # ONE genuine identity. That flag gates a broader
    # `AmbiguousPlacementResponseError` raise entirely independent of the
    # fallback, halting an already-accepted order despite the usable
    # secondary alias unambiguously identifying it (Codex review, PR
    # #1169: "Skip unusable aliases in the identity-conflict check") --
    # the same fall-through the whole-share collection-selection path
    # (and this module's other id-selection helpers) already apply.
    if _is_unusable_live_placement_order_id(id_a) or _is_unusable_live_placement_order_id(id_b):
        return False
    return _normalized_order_id(id_a) != _normalized_order_id(id_b)


def _normalized_order_identifier(value: Any) -> Optional[str]:
    """A canonicalized string form of an order identifier, or ``None`` if blank.

    Duplicated broker representations of the SAME order can use
    equivalent numeric and string ids -- a singular ``{"id": 123, ...}``
    next to a collection row ``{"id": "123", ...}`` -- and a raw ``==``
    comparison between them (``123 != "123"``) declares them different
    orders. The collection-agreement check below then discards the
    accepted order as ambiguous, and if broker history has not indexed it
    yet the unresolved-placement path halts the remaining batch entirely
    over what is, in fact, complete agreement (Codex review, PR #859
    round 39: "Normalize IDs before comparing collection rows").

    Routed through ``_normalized_order_id`` -- the durable numeric-aware
    canonicalizer, the same one ``_order_identifier_aliases_conflict``
    already uses for the identical purpose -- not a bare ``str(...).strip()``:
    a plain string comparison still read equivalent numeric encodings of
    the SAME order (``id=123.0`` vs. ``id="123"``) as a disagreement
    (``"123.0" != "123"``), raising ``WholeSharePlacementTerminalError``
    for an accepted order and sending it into ambiguous recovery even
    though every row actually agreed (Codex review, PR #1168/#1169:
    "Canonicalize numeric IDs across whole-share collection rows" /
    "Canonicalize numeric IDs before validating whole-share collections"
    -- two independently-authored fixes for the identical gap). Falls
    back to the plain stripped string when ``_normalized_order_id``
    rejects the value (e.g. a non-numeric string, or a sentinel it
    discards outright like a non-positive number): this function's own
    contract is "never None for a non-blank, REPRESENTABLE value", which
    callers here rely on to still detect and compare non-numeric ids.

    The one exception is a native numeric value beyond Python's integer-
    to-string conversion digit limit (e.g. a broker response's raw
    ``id: 10**5000``): ``_normalized_order_id`` already safely rejects it
    (returning ``None``), but the plain-stringified fallback used to call
    a bare ``str(value)`` on it anyway, raising ``ValueError`` instead of
    falling back at all -- aborting whatever comparison this function was
    called to support, on an already-broker-accepted response (Codex
    review, PR #1169: "Guard unrepresentable IDs before fallback
    stringification"). Routed through :func:`_safe_order_id_str`, which
    returns ``None`` for exactly that case, the same way it is already
    treated for every other unusable-id path in this module.
    """
    if _is_blank_broker_field(value):
        return None
    return _normalized_order_id(value) or _safe_order_id_str(value)


def _safe_order_id_str(value: Any) -> Optional[str]:
    """``str(value).strip()``, or ``None`` when that conversion cannot be done safely.

    A native (not string-encoded) numeric id beyond
    :func:`_is_representable_numeric_order_id`'s own bound -- e.g. a
    broker response's raw ``id: 10**5000`` -- raises ``ValueError``
    (Python's integer-to-string conversion digit limit) the instant
    anything calls a bare ``str()`` on it. Several envelope-comparison
    helpers in this module used to do exactly that unconditionally, so an
    already-broker-accepted response carrying such an id crashed with an
    unhandled exception instead of being cleanly treated as an
    unrepresentable, unusable identifier -- the same fail-safe treatment
    every other oversized-id path in this module already gives it (Codex
    review, PR #1169: "Check numeric representability before stringifying
    placement IDs"). Returning ``None`` here, not raising and not
    returning a lossy/fabricated string, lets callers treat an
    unrepresentable id the same way they already treat a blank one --
    something that carries no comparable identity, rather than a crash.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool) and not _is_representable_numeric_order_id(
        value
    ):
        return None
    try:
        return str(value).strip()
    except ValueError:
        return None


def _envelope_layer_shares_order_identity(layer: Any, order_id: Any) -> bool:
    """Whether an envelope layer's own id, if any, is safe to merge as ``order``'s evidence.

    A layer that names NO id of its own says nothing about identity and is
    always safe to fold in -- an anonymous ``{"cumulative_quantity": "0"}``
    sibling to a single named order is presumed to describe it, exactly as
    before. A layer that DOES carry its own id must match ``order_id``
    EXACTLY, in both type and value -- not just the normalized-string
    equality :func:`_normalized_order_identifier` uses to avoid FALSE
    ambiguity between two representations of the SAME order. That
    normalization is safe when deciding whether two representations
    DISAGREE (the question :func:`_envelope_layers_conflict_with_order`
    answers); it is not safe here, where agreement is being used to justify
    folding one layer's fill evidence into another's identity: ``{"id": 1,
    "state": "cancelled", "cumulative_quantity": "0", "data": {"order":
    {"id": "1", "state": "cancelled"}}}`` has the outer envelope's own
    INTEGER id normalizing to the same string as the inner order's STRING
    id, but a connector reporting two representations with disagreeing id
    TYPES for what may or may not be the same broker order is exactly the
    kind of untrustworthy coincidence this module's "prove nothing you
    cannot read cleanly" posture exists to catch -- letting the outer zero
    merge in unconditionally proved a rejection for the inner order while
    its OWN representation carried no fill evidence at all (Codex review,
    PR #859/#911 round 44: "Require an exact type match before sharing
    outer fill evidence").
    """
    if order_id is None or not isinstance(layer, dict):
        return True
    layer_id = layer.get("id")
    if layer_id is None:
        layer_id = layer.get("order_id")
    if _is_blank_broker_field(layer_id):
        return True
    if type(layer_id) is not type(order_id):
        return False
    # Compared via direct numeric `==`, not `str(...).strip()`, when both
    # ids are int/float: `type(...) is type(...)` already proved they are
    # the identical numeric type, and Python's arbitrary-precision `int`
    # comparison has no magnitude limit, but `str()` on an int beyond its
    # own integer-to-string conversion digit limit (e.g. a native
    # `10**5000` id) raises `ValueError` -- aborting this identity check
    # entirely instead of comparing two oversized-but-otherwise-valid ids
    # that share the exact same representable type (Codex review, PR
    # #1169: "Check numeric representability before stringifying
    # placement IDs" -- the same overflow class, reached via this
    # envelope-evidence path instead of the placement-validation path
    # that finding's own example targets).
    if isinstance(layer_id, (int, float)):
        return layer_id == order_id
    return str(layer_id).strip() == str(order_id).strip()


def _envelope_layers_conflict_with_order(order: Dict[str, Any], *layers: Any) -> bool:
    """Whether an OUTER envelope layer's own id or lifecycle state disagrees with ``order``'s.

    ``_order_identifier_aliases_conflict`` catches a single row's ``id``
    and ``order_id`` disagreeing with EACH OTHER; it says nothing about
    the row selected out of a nested ``data``/``order`` structure
    disagreeing with the id the ENCLOSING envelope carries at its own top
    level. ``{"success": false, "id": "LIVE", "state": "queued", "data":
    {"order": {"id": "DEAD", "state": "rejected"}}}`` has the outer
    response naming a live, queued order for itself while the nested
    ``data.order`` names a different, rejected one -- two layers telling
    two stories about which order this is, not one row contradicting
    itself. The merge (``_merge_envelope_layers_prefer_innermost_
    nonblank``) lets the inner ``DEAD``/"rejected" pair simply win, and
    the caller proved a rejection for ``DEAD`` while the outer envelope's
    own ``LIVE``/"queued" order sat outside reconciliation and cap
    accounting entirely (Codex review, PR #859 round 32: "Compare order
    identities across envelope layers").

    Only layers with their OWN non-blank id/order_id count -- a layer
    silent about identity is not a competing claim, exactly like a single
    row's blank alias above. ``order`` itself is skipped even if it
    appears again among ``layers`` (a caller's own envelope-fallback
    reassigning ``order = data``), since comparing a layer against itself
    can never disagree.

    EQUAL ids across layers is not the end of the check: ``{"success":
    false, "id": "ORD", "state": "queued", "data": {"order": {"id": "ORD",
    "state": "rejected"}}}`` has outer and inner layers naming the SAME
    order id while reporting CONTRADICTORY lifecycle states -- the merge
    (``_merge_envelope_layers_prefer_innermost_nonblank``) lets the inner
    "rejected" simply win, and the caller proved a rejection while the
    outer layer's own "queued" independently disputes it, exactly as
    clearly as a disagreeing id would. Checked the same way the id
    comparison is: only a layer with its OWN non-blank state/status
    counts as a competing claim (Codex review, PR #859 round 33: "Compare
    lifecycle states across envelope layers").

    A layer's PREFERRED alias matching ``order``'s id is not proof the
    layer agrees, either: ``{"success": false, "id": "DEAD", "order_id":
    "LIVE", "state": "rejected", "data": {"order": {"id": "DEAD", "state":
    "rejected"}}}`` has the outer envelope's own ``id`` match ``order``'s
    "DEAD" while its OWN ``order_id`` names a different, possibly-live
    "LIVE" order -- exactly the shape :func:`_order_identifier_aliases_
    conflict` exists to catch for a single row, but `layer.get("id") or
    layer.get("order_id")` only ever reads the FIRST non-blank alias, so
    the internally-conflicting layer read as agreeing and "LIVE" was
    discarded from reconciliation and cap accounting entirely (Codex
    review, PR #859 round 35: "Reject conflicting ID aliases on envelope
    layers").

    The SAME self-conflict gap existed on the lifecycle side, one round
    later: ``{"state": "rejected", "status": "queued", "data": {"order":
    {"state": "rejected", "status": "rejected"}}}`` has the outer layer's
    own ``state``/``status`` disagreeing with EACH OTHER while its
    PREFERRED alias (``state``, read first) happens to match ``order``'s
    "rejected" -- `layer.get("state") or layer.get("status")` only ever
    read that first alias, so the self-conflicting outer layer's own
    "queued" evidence was silently discarded and ``FractionalPlacementRejectedError``
    could be raised, skipping reconciliation, despite the outer layer
    disputing the very state that decision relies on (Codex review, PR
    #859 round 36: "Reject self-conflicting lifecycle envelope layers").

    A SELECTED ``order`` carrying an id but no lifecycle field of its own
    used to skip lifecycle comparison ENTIRELY, since every check above
    compares a layer against ``order``'s own state -- with none to
    compare against, `order_state is not None` was always False and the
    loop below never even looked at a layer's state. ``{"success": false,
    "state": "queued", "data": {"state": "rejected", "order": {"id":
    "ORD"}}}`` has the outer response and the ``data`` layer flatly
    disagreeing with EACH OTHER ("queued" vs "rejected") while ``order``
    itself is silent, so the inner "rejected" was free to win the merge
    unopposed and a proven rejection was raised despite the outer layer's
    own evidence disputing it (Codex review, PR #859 round 40: "Compare
    lifecycle evidence between outer envelope layers"). The layers can
    disagree with EACH OTHER even when there is no `order`-level state to
    anchor the comparison to.

    An early return used to short-circuit the whole function -- no layer
    ever inspected at all -- whenever ``order`` had NEITHER an id NOR a
    state of its own: ``{"success": false, "id": "DEAD", "order_id":
    "LIVE", "state": "rejected", "cumulative_quantity": "0", "data":
    {"order": {"cumulative_quantity": "0"}}}`` selects an ``order`` row
    carrying only fill evidence, no id and no lifecycle field, so the
    outer envelope's own internally-conflicting ``id``/``order_id``
    aliases -- exactly what the unconditional self-conflict check a few
    lines below exists to catch -- were never even reached (Codex review,
    PR #859/#911 round 43: "Validate envelope IDs before the anchorless
    early return"). That check, and the layer-vs-layer anchorless sets
    below it, need no anchor on ``order`` at all; bailing out before the
    loop defeated the exact case they were built for.
    """
    # `_first_usable_order_id_alias`, not a raw `order.get("id") or
    # order.get("order_id")`: a whitespace-only "id" alongside a real
    # "order_id" is truthy, so the raw fallback picked the blank
    # placeholder, this then normalized it to `None`, and a caller
    # comparing collection rows against this order's id (see the two
    # functions below and their own identical fix) found no usable
    # anchor to match against even though a real id was one alias over
    # (Codex review, PR #1164: "Fall back past blank IDs when matching
    # collection rows").
    order_id = _first_usable_order_id_alias(order)
    order_id = None if _is_blank_broker_field(order_id) else _safe_order_id_str(order_id)
    # Each alias normalized BEFORE the `state`-or-`status` fallback picks
    # one, not after: a raw `order.get("state") or order.get("status")`
    # -- exactly like the id alias above -- picks a whitespace-only
    # "state" over a real "status" (both here and on every LAYER's own
    # state a few lines below), and normalizing the ALREADY-selected
    # blank value afterward is too late -- it collapses to `None`
    # regardless of what "status" actually said, so this function never
    # compared against it at all (Codex review, PR #1164: "Normalize
    # lifecycle aliases before envelope comparison").
    order_state = _normalize_broker_lifecycle_alias(order.get("state")) or _normalize_broker_lifecycle_alias(
        order.get("status")
    )
    _layer_ids_without_an_order_anchor = set()
    _layer_states_without_an_order_anchor = set()
    for layer in layers:
        if not isinstance(layer, dict) or layer is order:
            continue
        # Checked UNCONDITIONALLY, not only when `order` itself has an id:
        # a layer's own id/order_id aliases disagreeing with EACH OTHER
        # (`{"id": "DEAD", "order_id": "LIVE", ...}`) is evidence a
        # possibly-live order exists regardless of whether the SELECTED
        # row happens to carry an id of its own to compare it against.
        # `order={"state": "rejected"}` (no id at all) alongside a self-
        # conflicting outer layer used to reach the id-less terminal
        # branch with neither identity ever inspected (Codex review, PR
        # #859 round 41: "Validate envelope IDs without a selected-order
        # ID").
        if _order_identifier_aliases_conflict(layer):
            return True
        if _order_lifecycle_aliases_conflict(layer):
            return True
        if order_id is not None:
            layer_id = _first_usable_order_id_alias(layer)
            _safe_layer_id = None if _is_blank_broker_field(layer_id) else _safe_order_id_str(layer_id)
            if _safe_layer_id is not None and _safe_layer_id != order_id:
                return True
        else:
            # No `order`-level id to anchor a comparison to, but the
            # layers can still disagree with EACH OTHER -- the same
            # generalization already applied to lifecycle state below.
            layer_id = _first_usable_order_id_alias(layer)
            _safe_layer_id = None if _is_blank_broker_field(layer_id) else _safe_order_id_str(layer_id)
            if _safe_layer_id is not None:
                _layer_ids_without_an_order_anchor.add(_safe_layer_id)
        # Checked UNCONDITIONALLY too, mirroring the id-conflict check
        # above: a layer's own state/status disagreeing with EACH OTHER
        # (`{"state": "rejected", "status": "queued"}`) is ambiguous
        # evidence regardless of whether `order` itself supplies a state
        # to compare it against. Gating this only on `order_state is not
        # None` let a self-conflicting layer's `state or status` alias
        # (always "state" when both are present) flow straight into the
        # no-anchor set below as though it were a single, trustworthy
        # reading -- an outer `state="rejected", status="queued"` layer
        # alongside a self-consistent inner `state="rejected"` layer
        # collapsed to the single value {"rejected"} and reported no
        # conflict at all, when the outer layer's OWN evidence never
        # settled which of "rejected"/"queued" it actually meant (Codex
        # review, PR #859/#911 round 43: "Check layer lifecycle aliases
        # without a selected state").
        if _order_lifecycle_aliases_conflict(layer):
            return True
        # Each layer's own alias normalized BEFORE its fallback too -- the
        # same fix as `order_state` above, for the identical reason: a
        # layer reporting `state="   ", status="rejected"` must compare
        # as "rejected", not as a blank the raw fallback picked first.
        if order_state is not None:
            normalized_layer_state = _normalize_broker_lifecycle_alias(
                layer.get("state")
            ) or _normalize_broker_lifecycle_alias(layer.get("status"))
            if normalized_layer_state is not None and normalized_layer_state != order_state:
                return True
        else:
            normalized_layer_state = _normalize_broker_lifecycle_alias(
                layer.get("state")
            ) or _normalize_broker_lifecycle_alias(layer.get("status"))
            if normalized_layer_state is not None:
                _layer_states_without_an_order_anchor.add(normalized_layer_state)
    if len(_layer_ids_without_an_order_anchor) > 1:
        return True
    if len(_layer_states_without_an_order_anchor) > 1:
        return True
    return False


def _merged_order_collection_view(*layers: Any) -> Dict[str, Any]:
    """Combine ``order``/``orders`` evidence from several envelope layers
    into one dict shaped for :func:`_select_singular_order_against_collection`.

    That selector only ever reads ONE dict's own ``"order"``/``"orders"``
    keys -- correct when a response nests them together, but a response
    can also carry its own top-level ``"orders"`` collection SIBLING to a
    ``"data"`` wrapper that carries the singular ``"order"``: ``{"success":
    false, "orders": [{"id": "LIVE", "state": "queued"}], "data": {"order":
    {"id": "DEAD", "state": "rejected"}}}`` has the OUTER response naming a
    live queued order in its own ``"orders"``, entirely invisible to a
    selector call scoped to ``data`` alone. The caller proved a rejection
    for ``DEAD`` while the response's own outer ``LIVE`` order sat outside
    reconciliation and cap accounting (Codex review, PR #859 round 34:
    "Validate order collections on every envelope layer").

    The FIRST layer supplying a non-``None`` ``"order"`` wins as the
    singular candidate -- callers pass the most specific layer (``data``)
    first, matching every other merge in this module's "innermost is most
    specific" convention. Every layer's ``"orders"`` list is concatenated,
    not just the first one found, so the selector's own agreement/conflict
    check still sees every named order across every layer. A malformed
    non-list ``"orders"`` on ANY layer short-circuits straight to that
    single malformed value -- diluting it with other layers' lists would
    let the selector's own "not a list" detection silently pass a
    concatenated list instead of the unreadable evidence it actually is.

    A LATER layer's own singular ``"order"`` disagreeing with the first
    one already selected is not silently discarded either: ``{"success":
    false, "order": {"id": "LIVE", "state": "queued"}, "data": {"order":
    {"id": "DEAD", "state": "rejected"}}}`` has the outer response's own
    top-level ``"order"`` naming a live order while ``data.order`` names a
    different, rejected one -- keeping only the first layer's pick (as a
    plain "first non-None wins" merge would) proved a rejection for
    "DEAD" while the outer "LIVE" order sat outside reconciliation and
    cap accounting, exactly the risk this function otherwise guards
    against for "orders" collections (Codex review, PR #859 round 35:
    "Compare singular orders across envelope layers"). Fed into the
    combined ``"orders"`` list instead, so the selector's own singular-
    vs-collection agreement check (``_order_rows_contradict``) decides
    whether the two singular rows actually agree -- exactly the same
    machinery a collection entry from any other layer already goes
    through, rather than this function silently picking a winner itself.
    """
    merged: Dict[str, Any] = {}
    combined_orders: List[Any] = []
    saw_orders_list = False
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        singular = layer.get("order")
        if singular is not None and not singular:
            # An EMPTY placeholder (``{}``, ``""``, ``[]``, ...) supplies no
            # id, state, or fill evidence -- ``{"order": {}, "data":
            # {"order": {"id": "ORD", "state": "queued"}}}`` has the OUTER
            # layer's own "order" key present but meaningless. Treating it
            # as a competing candidate in the ``elif`` branch below turned
            # an unambiguous single real order into a falsely ambiguous
            # 2-entry "orders" collection downstream -- the empty entry can
            # never be proven to agree with anything by id, so
            # ``_select_singular_order_against_collection``'s own
            # collection-agreement check rejected the otherwise-clean match
            # (Codex review, PR #859/#911 round 42: "Ignore empty singular
            # orders on outer envelope layers"). Matches that same
            # function's own "an empty singular mapping is treated as
            # absent" rule; a NONEMPTY malformed value (a list, a nonblank
            # string) is left alone -- that is still real, if unreadable,
            # evidence, not nothing.
            singular = None
        if singular is not None:
            if "order" not in merged:
                merged["order"] = singular
            elif singular != merged["order"]:
                combined_orders.append(singular)
                saw_orders_list = True
        collection = layer.get("orders")
        if isinstance(collection, list):
            combined_orders.extend(collection)
            saw_orders_list = True
        elif collection is not None:
            merged["orders"] = collection
            return merged
    if saw_orders_list:
        merged["orders"] = combined_orders
    return merged


def _other_order_shaped_rows(*layers: Any, order: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """Order-shaped mappings any of ``layers`` names under a key OTHER than ``order``/``orders``.

    ``_merged_order_collection_view`` only ever reads a layer's OWN
    ``"order"``/``"orders"`` keys -- correct for the shapes those two names
    cover, but a connector is free to name a second order under any other
    key entirely: ``{"order": {"id": "ORD-A", "state": "cancelled",
    "cumulative_quantity": "0"}, "sibling": {"id": "ORD-B", "state":
    "queued", "cumulative_quantity": "0"}}`` names a live, queued SIBLING
    order that neither that function nor :func:`_select_singular_order_
    against_collection` ever sees -- both are scoped to "order"/"orders"
    by name. The terminal-state checks below proved a rejection from "A"
    alone while "B" -- named in the very same response, one key over --
    sat outside reconciliation and cap accounting entirely (Codex review,
    PR #859/#911 round 44: "Scan sibling keys for other named orders
    before proving a rejection").

    Scans EVERY given layer, not just the unwrapped ``data`` mapping: a
    sibling can sit at the RAW response's own top level too, alongside (not
    inside) ``"data"`` -- ``{"success": true, "sibling": {"id": "ORD-B",
    "state": "queued"}, "data": {"order": {"id": "ORD-A", "state":
    "cancelled", "cumulative_quantity": "0"}}}`` has "sibling" sibling to
    "data" itself, invisible to a scan scoped to ``data`` alone (Codex
    review, PR #859/#911 round 44: "Scan outer envelopes for sibling
    orders"). Callers pass every envelope layer already in scope at the
    call site (typically ``data`` and ``response``); duplicate mappings
    reachable under more than one layer are naturally deduplicated by
    object identity below.

    Recurses into a sibling key's own value when that value is itself not
    order-shaped: ``{"success": true, "data": {"order": {"id": "A",
    "state": "cancelled", "cumulative_quantity": "0"}, "meta": {"sibling":
    {"id": "B", "state": "queued"}}}}`` has "B" two levels down from
    "data", nested inside "meta" (itself carrying no id/state/fill of its
    own, so it is not a competing order and would have been silently
    skipped by a scan that only ever checked immediate children). A flat
    one-level scan proved a rejection from "A" alone while "B" sat outside
    reconciliation and cap accounting, invisible one container further in
    (Codex review, PR #859/#911 round 46: "Traverse nested sibling
    containers before proving rejection"). Recursion stops the moment a
    container IS order-shaped -- that mapping is returned as one of the
    "other" rows and not searched further for orders of its own, the same
    "this is a competing order, not more envelope" reading a flat scan
    already gave every direct child. Bounded to a shallow depth (well
    below Python's default recursion limit) as a defensive cap against a
    pathologically deep malformed payload, not because a real connector
    response is expected to nest anywhere near that deep.

    Returns ``(rows, truncated)``: ``truncated`` is ``True`` when the depth
    cap actually cut a branch of the scan short, meaning this call could
    NOT prove there is no sibling order past that point -- an empty
    ``rows`` list paired with ``truncated=True`` must be read by the
    caller as "unresolved", never as "confirmed no siblings" (Codex
    review, PR #1164: "Fail closed when sibling traversal reaches its
    depth cap").

    A value only counts as "order-shaped" if it carries an id, a lifecycle
    alias, or one of the recognized fill-evidence fields -- an unrelated
    sibling key holding a plain string or an empty mapping supplies no
    evidence this classifier can read and is not a competing order. The
    SAME mapping object already selected as ``order`` is never returned
    again as one of its own siblings, even if it happens to be reachable
    under a second key too.

    A peer layer's own ``"error"`` key -- or, generally, ANY key's value
    that is the exact same object as one of the top-level ``layers``
    already passed in (typically ``_failure_error_layer``, or an envelope
    wrapper like ``data`` reachable a second time through its own sibling
    key -- see ``_visit``'s identity check) -- is skipped when -- and only
    when -- that key's VALUE IS that same object: the
    failure-envelope path already gives that container first-class
    treatment as its own dedicated argument, which this function's own
    top-level scan loop walks directly, so a connector's plain diagnostic
    payload -- ``{"success": false, "error": {"id": "ERR-123", "code":
    "BAD_REQ", "message": "order refused"}}`` -- is order-shaped by this
    function's own id/state/fill test (it carries an ``id``) but names no
    real order at all, and re-testing THAT SAME container a second time as
    an ordinary sibling key on ``data``/``response`` treated that
    diagnostic id as an unproven live order and blocked an otherwise clean
    rejection forever, since a bare diagnostic row can never itself prove
    ``terminal_broker_row_proves_no_order`` (Codex review, PR #859/#911
    round 45: "Do not rescan the error container as a sibling order"). A
    DIFFERENT dict merely reachable under a key named ``"error"`` (or
    ``"order"`` -- the selected ``order`` object itself is excluded by
    IDENTITY, not by key name, in ``_visit`` below) is a genuine,
    unexamined nested container and is still descended into: an ``error``
    layer not literally identical to one of the passed-in ``layers``, or a
    real order nested two levels down as ``error.order``, both used to be
    discarded unconditionally by the old by-name skip, hiding a live
    sibling order from reconciliation and cap accounting entirely (Codex
    review, PR #1164: "Traverse nested order and error containers").
    """
    rows: List[Dict[str, Any]] = []
    seen_ids: set = set()
    # Set when the depth cap below (`depth > 8`) actually truncates a
    # branch of the scan -- see that check's own comment for why the
    # caller must read this as "the scan could not prove there is no
    # sibling", not as "there is no sibling".
    _truncated = False
    # Every TOP-LEVEL layer already gets its own direct `_scan(data, 0)`
    # call below -- including `_failure_error_layer` itself, when the
    # caller passes it. A nested key that happens to point at that SAME
    # object (`data["error"] is _failure_error_layer`) is therefore
    # already covered; recursing into it a second time here would just
    # redo that work. Compared by identity, not by key name -- see `_scan`
    # (Codex review, PR #1164: "Traverse nested order and error
    # containers").
    _known_top_layer_ids = {id(_layer) for _layer in layers if isinstance(_layer, dict)}

    def _is_order_shaped(value: Dict[str, Any]) -> bool:
        return (
            not _is_blank_broker_field(value.get("id"))
            or not _is_blank_broker_field(value.get("order_id"))
            or not _is_blank_broker_field(value.get("state"))
            or not _is_blank_broker_field(value.get("status"))
            or any(not _is_blank_broker_field(value.get(_field)) for _field in _FILL_EVIDENCE_ALIASES)
            # A non-empty "executions" collection is evidence too, even
            # when the container carries none of the aliases above: a
            # response can report a fill EXCLUSIVELY through this
            # standard collection -- `{"executions": [{"quantity": "1",
            # "price": "5"}]}`, whose rows commonly expose the fill
            # through plain "quantity", which is not itself one of
            # `_FILL_EVIDENCE_ALIASES` -- and without this, such a
            # container had no id/state/fill alias of its own, was never
            # classified order-shaped, and this scan silently recursed
            # past it as though it were pure, evidence-free envelope
            # instead of adding it as a sibling row. Once added here, the
            # caller's own `terminal_broker_row_proves_no_order` check on
            # it already reads "executions" (see that function's own fix)
            # and, lacking any recognized terminal state of its own,
            # conservatively reads as unresolved rather than proven dead
            # either way (Codex review, PR #1164: "Preserve nested
            # execution collections during sibling scans").
            #
            # A PRESENT but non-list "executions" -- a miswrapped single
            # execution object, `{"executions": {"quantity": "1", "price":
            # "5"}}`, the same non-standard shape `_reconciled_order_may_
            # have_executed` already treats as unresolved fill evidence at
            # the top level -- is order-shaped too when found NESTED
            # during this scan: the `isinstance(..., list)` check above
            # only recognizes the well-formed collection shape, so a
            # malformed one still had no id/state/fill alias of its own
            # and was silently recursed past exactly like an absent
            # "executions" field, even though it is present and
            # unreadable, not absent (Codex review, PR #1164: "Treat
            # malformed nested executions as unresolved"). Blank (``None``/
            # ``""``/whitespace) is genuinely absent, matching every other
            # field this scan reads via `_is_blank_broker_field`; an empty
            # LIST is genuinely absent too (already handled -- and
            # deliberately excluded -- by the list check above), so both
            # continue to read as no evidence.
            or (
                not _is_blank_broker_field(value.get("executions"))
                and not isinstance(value.get("executions"), list)
            )
        )

    def _visit(value: Any, depth: int) -> None:
        if not isinstance(value, dict) or not value or value is order:
            return
        # Excluded by IDENTITY, not by the key it happened to be reached
        # under: a value that IS one of the top-level `layers` -- most
        # often an envelope WRAPPER reachable a second time through its
        # own sibling key, e.g. `{"data": {"state": "queued", "order":
        # {"id": "ORD", "state": "queued"}}}` reaches `data` again via
        # `response["data"]` -- is already covered by its own direct
        # `_scan(data, 0)` call in the outer loop below; testing it here
        # via `_is_order_shaped` a SECOND time treated the wrapper's own
        # mirrored state/id as a competing sibling order, not the SAME
        # order's own envelope repeating its metadata, and raised a
        # structurally-ambiguous halt over a perfectly ordinary,
        # unambiguous response (Codex review, PR #1164: "Exclude envelope
        # wrappers from sibling-order detection"). A DIFFERENT dict merely
        # reachable under the SAME key name a layer happens to use (e.g. a
        # genuine, distinct "error" container elsewhere) is not covered by
        # this check at all -- only exact object identity with an already-
        # scanned top-level layer is.
        if id(value) in _known_top_layer_ids:
            return
        if id(value) in seen_ids:
            return
        seen_ids.add(id(value))
        if _is_order_shaped(value):
            rows.append(value)
        # Recursion no longer stops just because THIS container is itself
        # order-shaped: an intermediate sibling can carry its OWN
        # lifecycle/fill evidence (making it order-shaped, and therefore
        # added as a row above) while ALSO nesting a genuinely SEPARATE
        # order underneath it -- `{"state": "cancelled",
        # "cumulative_quantity": "0", "order": {"id": "B", "state":
        # "queued"}}` is itself a terminal, zero-fill sibling (correctly
        # added as a row) but also names live order "B" one key deeper,
        # which the old `else` never reached because this container had
        # already been classified "resolved" the moment it qualified as
        # order-shaped. Continuing to scan its children -- deduplicated by
        # identity exactly like every other value here, so this container
        # itself is never re-tested or re-added -- is what actually finds
        # "B" (Codex review, PR #1164: "Continue scanning order-shaped
        # sibling containers").
        _scan(value, depth + 1)

    def _scan(container: Dict[str, Any], depth: int) -> None:
        if depth > 8:
            # A pathologically deep malformed payload (see this function's
            # own docstring on the 8-level cap) stops the descent here --
            # but stopping is NOT the same as proving nothing lives below
            # this point. `_truncated` records that this scan could not
            # rule out a sibling order past the cap, so a caller reading
            # an EMPTY `rows` list -- indistinguishable, before this flag
            # existed, from "genuinely no siblings anywhere" -- can still
            # tell the two apart: a selected order `A` cancelled with an
            # explicit zero fill used to prove a clean rejection from a
            # truncated scan exactly as confidently as from a complete
            # one, even though a queued order `B` nine levels down was
            # never actually ruled out, silently letting `B` sit outside
            # deployment and cap accounting (Codex review, PR #1164:
            # "Fail closed when sibling traversal reaches its depth cap").
            nonlocal _truncated
            _truncated = True
            return
        for key, value in container.items():
            # "orders" is skipped only at the TOP level (depth 0, one of
            # the layers this function was directly called with): THAT
            # collection gets its own dedicated agreement/conflict
            # handling via `_select_singular_order_against_collection`,
            # and scanning it here too would duplicate (and could
            # disagree with) that dedicated logic. A "orders" key reached
            # by RECURSING into some other, anonymous sibling container is
            # a genuine, unexamined collection nothing else ever reads:
            # `{"order": {"id": "A", "state": "cancelled",
            # "cumulative_quantity": "0"}, "meta": {"orders": [{"id": "B",
            # "state": "queued"}]}}` names a live sibling "B" only inside
            # "meta"'s own "orders" list, and skipping that key
            # unconditionally at every depth discarded it too, hiding "B"
            # from reconciliation and cap accounting entirely (Codex
            # review, PR #1164: "Traverse nested order keys during sibling
            # scans"). `_scan` is only ever called with `depth == 0` for a
            # top-level layer (the outer loop below); every recursive call
            # passes `depth + 1`, so this check exactly distinguishes the
            # two cases.
            if key == "orders" and depth == 0:
                continue
            if isinstance(value, list):
                # A sibling order can be reported as one entry in a list
                # under any non-"orders" key too -- e.g. ``related: [{"id":
                # "B", "state": "queued"}]`` -- so each list ITEM gets the
                # same order-shaped-or-recurse treatment a lone dict value
                # gets below (Codex review, PR #1164: "Traverse list-valued
                # sibling containers before proving rejection").
                for item in value:
                    _visit(item, depth)
                continue
            _visit(value, depth)

    for data in layers:
        if not isinstance(data, dict):
            continue
        _scan(data, 0)
    return rows, _truncated


def _agreeing_collection_row_disputes_success(order: Dict[str, Any], collection_rows: Any) -> bool:
    """Whether a collection row sharing ``order``'s id reports an error.

    Mirrors ``_failure_is_broker_authored``'s own ``collection_rows`` scan
    (Codex review, PR #859 round 32: "Inspect success flags in agreeing
    collection rows"), for the SUCCESS path's own selected-order dispute
    check: a successful envelope's singular order can omit the flag while
    an otherwise agreeing collection row for the identical order reports
    ``success=false`` or a connector error -- ``data.order={"id": "ORD",
    "state": "queued"}`` alongside an otherwise identical collection row
    carrying ``isError=true`` must not be accepted when only the singular row
    is clean. One representation of the SAME order still disputes acceptance.
    Filtered to rows whose own id/order_id
    alias matches ``order``'s -- a row naming a different id is a job for
    the identifier-conflict checks, not this dispute scan.

    Compared through ``_normalized_order_identifier`` -- the numeric-aware
    canonicalizer ``_select_singular_order_against_collection`` already
    uses to decide agreement -- not a bare ``str(...).strip()``: a row
    reaches this function only after already being selected/merged as
    agreeing with ``order`` under that canonicalized comparison, so a
    plain string re-comparison here (``"123.0" != "123"``) could fail to
    match the very same row the caller just treated as this order's own
    (Codex review, PR #1168: "Normalize IDs when merging agreeing
    collection evidence").
    """
    if not isinstance(collection_rows, list):
        return False
    order_id = _normalized_order_identifier(_first_usable_order_id_alias(order))
    if order_id is None:
        return False
    return any(
        isinstance(row, dict)
        and _normalized_order_identifier(_first_usable_order_id_alias(row)) == order_id
        and _is_error_response(row) is not None
        for row in collection_rows
    )


def _agreeing_collection_row(order: Dict[str, Any], collection_rows: Any) -> Optional[Dict[str, Any]]:
    """Every collection row sharing ``order``'s id, merged into one dict.

    A row reaching here at all -- ``order`` was already selected against
    this exact collection without setting ``ambiguous_nested_orders`` --
    is definitionally agreeing: ``_select_singular_order_against_
    collection``'s own agreement check (``_order_rows_contradict``)
    already rejected any same-id row that disagrees on a shared field
    before ``order`` was ever chosen. So the row's FULL evidence, not
    just its lifecycle alias (``_agreeing_collection_row_lifecycle_
    alias`` reads only ``state``/``status``), is safe to fold into
    ``_evidence_order``: ``order={"id": "ORD"}`` alongside
    ``orders=[{"id": "ORD", "state": "cancelled", "cumulative_quantity":
    "0"}]`` has the state pulled from this agreeing row already, but its
    OWN zero-fill evidence never reached `_evidence_order` at all, so
    `terminal_broker_row_proves_no_order` found no fill figure to read
    and could not prove the rejection the collection row's own zero
    already settles (Codex review, PR #859 round 40: "Merge agreeing
    collection fill evidence before classification").

    MERGED across EVERY matching row, not just the first found: two same-
    id rows can each individually agree with `order` (and with each
    other -- `_order_rows_contradict` never found a shared, disagreeing
    field between them either) while carrying DIFFERENT, complementary
    pieces of evidence -- one reporting `state="cancelled"`, another
    reporting only `cumulative_quantity="0"`. Returning just the FIRST
    row found either fact alone, still leaving `_evidence_order` without
    the OTHER one and unable to prove the rejection both facts together
    settle (Codex review, PR #859 round 41: "Merge evidence from every
    agreeing collection row").

    Compared through ``_normalized_order_identifier``, not a bare
    ``str(...).strip()``, for the identical reason
    ``_agreeing_collection_row_disputes_success`` just above now does
    (Codex review, PR #1168: "Normalize IDs when merging agreeing
    collection evidence").
    """
    if not isinstance(collection_rows, list):
        return None
    order_id = _normalized_order_identifier(_first_usable_order_id_alias(order))
    if order_id is None:
        return None
    merged: Dict[str, Any] = {}
    found_any = False
    for row in collection_rows:
        if isinstance(row, dict) and _normalized_order_identifier(_first_usable_order_id_alias(row)) == order_id:
            found_any = True
            merged = _merge_envelope_layers_prefer_innermost_nonblank(merged, row)
    return merged if found_any else None


def _agreeing_collection_row_lifecycle_alias(
    order: Dict[str, Any], collection_rows: Any
) -> Tuple[Optional[str], bool]:
    """A ``state``/``status`` alias reported by a collection row sharing ``order``'s id.

    ``_select_singular_order_against_collection`` -- and the ``_order_rows_
    contradict`` agreement check underneath it -- only calls a lifecycle
    field a CONFLICT when it is present on BOTH the singular row and the
    collection row and they disagree; a lifecycle field present on only
    ONE side is the ordinary single-alias case, so the two rows are read
    as agreeing. That is correct for AMBIGUITY, but the caller still went
    on to read the selected order's OWN lifecycle alone: ``order={"id":
    "ORD"}`` alongside ``orders=[{"id": "ORD", "state": "rejected"}]`` has
    no state/status field on the selected row at all, so the caller's
    ``order.get("state") or order.get("status")`` found nothing, matched
    neither terminal gate, and returned "ORD" as a successful submission
    -- booking the requested notional even though the identical order's
    OTHER representation in the very collection just validated against it
    reports it was rejected (Codex review, PR #859 round 36: "Honor
    lifecycle states from agreeing collection rows"). Returns the first
    non-blank ``state`` or ``status`` found on a row sharing ``order``'s
    id, or ``None`` when there is none -- callers fall back to this only
    when ``order`` itself has no lifecycle alias of its own, exactly the
    shape that reaches here as "agreeing" in the first place.

    Returns ``(value, disagreed)``, aggregated across EVERY matching row,
    not just the first one found: a state-less singular order can have
    MULTIPLE same-id collection rows with DIFFERENT lifecycles -- e.g.
    one ``queued`` row followed by one ``rejected`` row for the identical
    id, both individually "agreeing" with the state-less singular row
    since a lifecycle field present on only one side is never itself the
    conflict ``_order_rows_contradict`` checks for. Returning whichever
    value happened to come first made the adapter's own verdict depend on
    collection ORDER: a ``queued`` row appearing first silently discarded
    a genuinely contradictory ``rejected`` row appearing later in the
    SAME collection (Codex review, PR #859 round 38: "Require collection
    lifecycle rows to agree"). ``value`` is ``None`` whenever the matching
    rows disagree -- disagreeing values are ambiguous, not a value to
    pick one of -- and ``disagreed`` is ``True`` in exactly that case
    (two or more matching rows found, at least one lifecycle value
    differing), so a caller can distinguish "genuinely nothing to read"
    from "read something, but it contradicts itself" and treat the
    latter as unresolved rather than silently falling through to the
    former's "no evidence at all" handling.

    Compared through ``_normalized_order_identifier``, not a bare
    ``str(...).strip()``, for the identical reason the sibling
    ``_agreeing_collection_row``/``_agreeing_collection_row_disputes_
    success`` functions now do (Codex review, PR #1168: "Normalize IDs
    when merging agreeing collection evidence").
    """
    if not isinstance(collection_rows, list):
        return None, False
    order_id = _normalized_order_identifier(_first_usable_order_id_alias(order))
    if order_id is None:
        return None, False
    _found: Optional[str] = None
    for row in collection_rows:
        if not isinstance(row, dict):
            continue
        if _normalized_order_identifier(_first_usable_order_id_alias(row)) != order_id:
            continue
        for _field in ("state", "status"):
            _normalized = _normalize_broker_lifecycle_alias(row.get(_field))
            if _normalized is None:
                continue
            if _found is None:
                _found = _normalized
            elif _found != _normalized:
                return None, True
    return _found, False


def _select_singular_order_against_collection(
    data: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Pick the ``order`` row a broker response describes, and whether that
    pick is AMBIGUOUS -- named by a nested ``orders`` collection that does
    not agree with it, or malformed beyond what this classifier can read.

    Shared by both the failure-envelope and success-envelope placement-
    response paths in :meth:`RobinhoodBroker.place_market_buy` (Codex
    review, PR #859: "Validate order collections on successful envelopes"
    -- the success path used to skip this entirely and trust a singular
    ``order`` a conflicting ``orders`` collection actually disputed).

    Accepts the SAME list-mode envelope ``get_order_fill`` already reads
    (``{"orders": [...]}}``), not just the singular ``{"order": {...}}``
    nesting: a response shaped ``{"orders": [{"id": "ORD", "state":
    "queued"}]}`` left ``order`` bound to the enclosing mapping, with no
    id/state/fill of its own to inspect. Only a SINGLE nested order is
    unambiguous about which order this response describes; two or more
    is returned as ambiguous rather than guessed at.

    An EMPTY singular mapping is treated as absent, not as the selected
    order: ``{"order": {}, "orders": [{"id": "ORD", "state": "queued"}]}``
    carries a real, live queued order in the "orders" collection, but a
    bare ``isinstance(..., dict)`` check would accept the meaningless
    ``{}`` as ``order`` and never even look at "orders". A non-empty
    singular mapping still wins outright, exactly as before -- this only
    widens what counts as "nothing there".

    A NONEMPTY singular order does not automatically win when an "orders"
    collection is ALSO present: ``{"order": {"id": "OLD", "state":
    "rejected"}, "orders": [{"id": "LIVE", "state": "queued"}]}`` names a
    live, different order the singular row alone would mask entirely.
    Only trust the singular when every entry in the collection AGREES
    with it: the SAME id is necessary but not sufficient -- ``{"id":
    "ORD", "state": "rejected"}`` alongside ``{"id": "ORD", "state":
    "queued"}`` names the identical order while flatly disagreeing about
    whether it is still live, and an id-only check called that
    agreement too. ``_order_rows_contradict`` checks every shared field,
    not just id/state, so a fill-evidence mismatch is caught the same
    way. An id-less singular can never be proven to agree with anything
    and is always treated as conflicting.

    Returns ``(None, False)`` when the response carries no singular or
    collection evidence at all -- the caller's own fallback (the
    enclosing envelope, or ``{}``) decides what "order" means then.

    A PRESENT but non-dict, non-empty singular ``order`` value -- e.g.
    ``{"order": [{"id": "LIVE", "state": "queued"}]}``, the list-mode
    shape misfiled under the singular key instead of "orders" -- is
    malformed evidence of exactly the same kind a non-list "orders" value
    already is: it names something this classifier cannot read, not
    nothing at all. The bare ``isinstance(..., dict)`` check above already
    turns it into ``order = None``, but without also marking it ambiguous
    it collapsed to the SAME reading as a response carrying no order
    evidence whatsoever, letting the caller fall back to the enclosing
    envelope and prove a rejection from it while a queued (or otherwise
    live) order sat inside the discarded list, unaccounted for (Codex
    review, PR #859 round 32: "Treat malformed singular order values as
    unresolved"). An EMPTY non-dict value (``""``, ``[]``, ``0``, ...) is
    still read as "nothing there", same as ``{}`` above -- there is no
    evidence to lose either way.
    """
    _singular_order = data.get("order")
    order = (
        _singular_order
        if isinstance(_singular_order, dict) and _singular_order
        else None
    )
    # An ambiguous nested collection (2+ entries) names orders this
    # placement call's response might describe, but not WHICH one --
    # unlike an envelope carrying no row at all, so it must never
    # reach the "envelope itself is the evidence" reasoning the caller
    # applies, which would otherwise call it proven from an order dict
    # that merely failed to pick a candidate. A malformed singular value
    # (present, non-empty, not a dict) starts this flag True for the same
    # reason -- see the docstring note above.
    ambiguous_nested_orders = bool(_singular_order) and not isinstance(_singular_order, dict)
    _nested_orders = data.get("orders")
    # A PRESENT but non-list "orders" -- e.g. a bare mapping
    # {"orders": {"id": "LIVE", "state": "queued"}} instead of the
    # expected [{"id": "LIVE", "state": "queued"}] -- is malformed
    # evidence this classifier cannot read as either "no collection" or
    # "a collection that agrees/conflicts". Both isinstance-list checks
    # below already treat it as absent, which let a NONEMPTY singular
    # order win outright with the malformed collection silently
    # discarded, even though it may itself describe a live order this
    # response is actually about -- and, when there is no singular order
    # either, fell all the way through to (None, False), the SAME reading
    # this function gives a response with no order evidence at all,
    # rather than the ambiguous, unreadable response it actually is
    # (Codex review, PR #859 round 27: "Treat malformed order collections
    # as unresolved").
    if _nested_orders is not None and not isinstance(_nested_orders, list):
        order = None
        ambiguous_nested_orders = True
    elif order is not None and isinstance(_nested_orders, list) and _nested_orders:
        _singular_id = _normalized_order_identifier(order.get("id")) or _normalized_order_identifier(
            order.get("order_id")
        )
        # A collection row's OWN id/order_id aliases can disagree with
        # EACH OTHER -- {"id": "ORD", "order_id": "LIVE", "state":
        # "rejected"} -- which the two checks below never catch:
        # comparing only the PREFERRED alias ("id" or "order_id",
        # whichever is truthy) against the singular row's own preferred
        # id can still match ("ORD" == "ORD"), and _order_rows_contradict
        # deliberately skips both identifier fields (it exists to compare
        # every OTHER shared field). A rejected singular row accompanied
        # by exactly this collection row was therefore trusted as
        # agreeing, and the selector proved the singular rejection while
        # discarding the collection row's own second, possibly-live
        # identity entirely (Codex review, PR #859 round 31: "Reject
        # identifier conflicts inside collection rows").
        _collection_agrees = _singular_id is not None and all(
            isinstance(_row, dict)
            and (
                _normalized_order_identifier(_row.get("id"))
                or _normalized_order_identifier(_row.get("order_id"))
            )
            == _singular_id
            and not _order_identifier_aliases_conflict(_row)
            and not _order_rows_contradict(order, _row)
            for _row in _nested_orders
        )
        if not _collection_agrees:
            order = None
            ambiguous_nested_orders = True
    if order is None and not ambiguous_nested_orders:
        if isinstance(_nested_orders, list) and _nested_orders:
            if (
                len(_nested_orders) == 1
                and isinstance(_nested_orders[0], dict)
                and _nested_orders[0]
            ):
                order = _nested_orders[0]
            elif _collection_rows_are_the_same_order(_nested_orders):
                # 2+ entries, but every one names the SAME order and none
                # contradicts another: `_merged_order_collection_view`
                # concatenates EVERY layer's own "orders" list rather than
                # keeping only the first, so the identical single order
                # repeated on two layers -- `{"orders": [{"id": "ORD",
                # "state": "queued"}]}` on both the outer response and
                # "data" -- lands here as two list ENTRIES, not two
                # candidate ORDERS. Treating list length alone as "which
                # side this response describes" raised without carrying
                # "ORD" at all, halting the remaining batch and requesting
                # manual recovery despite every representation agreeing
                # (Codex review, PR #859/#911 round 42: "Collapse
                # duplicate collection rows before declaring ambiguity").
                order = _nested_orders[0]
            else:
                # Either 2+ GENUINELY DISAGREEING entries (which SIDE this
                # response describes), a nonempty singleton that is not
                # itself a mapping (e.g. {"orders": [null]}), or a
                # singleton mapping that IS empty ({"orders": [{}]}) -- an
                # empty entry supplies no id, state, or fill evidence any
                # more than the missing-entry case does, yet a len==1-and-
                # dict-only condition alone would accept it outright,
                # leaving `order = {}` and `ambiguous_nested_orders` False
                # so the enclosing envelope could still be read as clean
                # proof of a rejection. None of these malformed shapes
                # names anything this classifier can read.
                ambiguous_nested_orders = True
    return order, ambiguous_nested_orders


def _collection_rows_are_the_same_order(rows: List[Any]) -> bool:
    """Whether every entry in a 2+-item collection names the SAME order.

    ``_merged_order_collection_view`` concatenates EVERY layer's own
    "orders" list rather than keeping only the first (by design -- see its
    own docstring), so the identical single order repeated verbatim across
    two envelope layers produces a 2-item list describing ONE order, not
    two competing candidates. Requires every row to be a non-empty dict, to
    share the same normalized id/order_id alias, and to not contradict any
    other row via :func:`_order_rows_contradict` -- the same agreement
    machinery :func:`_select_singular_order_against_collection` already
    applies when checking a collection against an outside singular row. An
    id-less row can never be proven to agree with anything, matching that
    same convention.

    Every row is compared against every OTHER row, not merely against the
    first: ``[{"id": "ORD"}, {"id": "ORD", "state": "queued"}, {"id":
    "ORD", "state": "rejected", "cumulative_quantity": "0"}]`` has its
    first entry carry no state at all, so comparing rows[1] and rows[2]
    only against rows[0] finds no contradiction with that evidence-free
    row -- while rows[1] and rows[2] flatly disagree about the SAME
    order's lifecycle with each other, a contradiction only a pairwise
    comparison (or an equivalent progressively-merged one) can see (Codex
    review, PR #859 round 43: "Compare every collection row before
    collapsing duplicates").
    """
    if len(rows) < 2:
        return False
    if not all(isinstance(row, dict) and row for row in rows):
        return False
    first_id = _normalized_order_identifier(rows[0].get("id")) or _normalized_order_identifier(
        rows[0].get("order_id")
    )
    if first_id is None:
        return False
    compared_rows = [rows[0]]
    for row in rows[1:]:
        _row_id = _normalized_order_identifier(row.get("id")) or _normalized_order_identifier(row.get("order_id"))
        if _row_id != first_id:
            return False
        if _order_identifier_aliases_conflict(row) or any(
            _order_rows_contradict(previous, row) for previous in compared_rows
        ):
            return False
        compared_rows.append(row)
    return True


def _failure_envelope_state_permits_proven_rejection(order: Dict[str, Any]) -> bool:
    """Whether a failure envelope's own ``state`` leaves room to call it proven.

    A refusal envelope usually carries no order row at all -- the RVII shape,
    ``{"success": false, "error": "instrument is not tradable"}`` -- and there
    the envelope IS the evidence: the broker answered, and it answered no.

    But an envelope can also carry a row, and then the row outranks the
    envelope. ``{"success": false, "data": {"state": "queued"}}`` is a
    broker-authored failure with no id and no fill, and treating it as proven
    let the caller skip history reconciliation and keep placing while an
    ACCEPTED order sat outside deployment and cap totals -- a working state is
    a possible acceptance whatever the envelope around it says (Codex review,
    PR #859: "Keep ID-less working states unresolved"). A terminal row is
    handed to the shared classifier so the adapter cannot reach a verdict
    ``_reconcile_ambiguous_fractional_placement`` would not, and an
    unrecognized state is not proof of anything.
    """
    # Normalized per-alias before the fallback, the same fix applied to
    # the ID-bearing terminal-routing state a few hundred lines below (and
    # for the identical reason): a raw `state or status` picks a
    # whitespace-only "state" over a real "status" value, and blank
    # ANYTHING must read as absent rather than as the empty string this
    # function then returns `True` (unresolved) for even when "status"
    # alone carried a perfectly readable value (Codex review, PR #1164:
    # "Normalize lifecycle aliases before terminal routing").
    state = _normalize_broker_lifecycle_alias(order.get("state")) or _normalize_broker_lifecycle_alias(
        order.get("status")
    ) or ""
    if not state:
        return True
    if state in _WORKING_BROKER_ORDER_STATES:
        return False
    return terminal_broker_row_proves_no_order(order)


def _merge_envelope_layers_prefer_innermost_nonblank(*layers: Any) -> Dict[str, Any]:
    """Merge envelope layers outer-to-inner, keeping the innermost NON-BLANK value per key.

    A plain dict spread (``{**outer, **inner}``) lets ANY key present in
    ``inner`` shadow ``outer``'s value for that key regardless of whether the
    inner value is blank -- so a failure shaped ``{"success": false,
    "executed_dollar_amount": "5", "data": {"order": {"state": "rejected",
    "executed_dollar_amount": null}}}`` erased the outer, genuinely-reported
    ``"5"`` the moment the inner row's OWN blank placeholder for the same key
    was spread on top. Every classifier reading the merged evidence
    (``terminal_broker_row_proves_no_order``, ``_reconciled_order_may_have_
    executed``) treats an absent/blank field as "no evidence" and a present
    non-blank one as proof of a fill -- so this silently downgraded a real
    reported execution to "nothing here", letting a proven-rejection verdict
    through while that $5 stayed outside deployment and cap accounting
    (Codex review, PR #859: "Preserve outer fill evidence when inner fields
    are blank").

    Still lets an inner layer's non-blank value win over an outer one for the
    same key -- ``order``'s own ``"state"`` deciding over a stale ``data``-
    level status, say -- since only a BLANK inner value is the problem case;
    an inner value that actually disagrees, non-blank, is the more specific
    and more current read. "Blank" is ``_is_blank_broker_field``'s definition:
    ``None`` or an empty/whitespace-only string.

    One exception to "innermost non-blank wins": a CONTRADICTORY explicit
    zero. ``{"executed_dollar_amount": "5", "data": {"order": {"state":
    "rejected", "executed_dollar_amount": "0"}}}`` reports a real $5
    execution at the outer level and an explicit, nonblank "0" at the
    inner one for the SAME key -- both non-blank, so the naive rule let the
    inner zero simply overwrite the outer positive figure, and the terminal
    classifier read the merged "0" as proof of no fill while that $5 stayed
    outside deployment and cap accounting (Codex review, PR #859: "Preserve
    contradictory outer execution evidence"). Checked via
    ``_reconciled_order_may_have_executed`` -- the SAME per-alias fill-or-
    not reading the terminal classifiers themselves use, so this only ever
    fires for the seven fill-evidence aliases that function recognizes;
    every other key (``id``, ``state``, ...) reads as "no evidence" for
    both old and new values and this exception never engages, leaving
    "innermost wins" as the rule for everything else. A GENUINE fill
    figure disagreement (outer "5" vs. inner "3") is not this case -- both
    read as fill evidence, so the inner, more specific figure still wins.

    The conflict check itself is numeric-aware, not a raw string compare --
    outer ``cumulative_quantity="0"`` vs inner ``"0.0"`` describe the SAME
    quantity and must not be flagged as disagreeing evidence just because
    they are formatted differently; see :func:`_fill_evidence_values_conflict`
    (Codex review, PR #859/#911 round 44: "Compare fill aliases numerically
    before declaring conflicts").
    """
    merged: Dict[str, Any] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for key, value in layer.items():
            if key not in merged:
                merged[key] = value
                continue
            if _is_blank_broker_field(value):
                continue
            if (
                key in _FILL_EVIDENCE_ALIASES
                and not _is_blank_broker_field(merged[key])
                and _fill_evidence_values_conflict(merged[key], value)
            ):
                # Keep an explicit marker so callers route this response
                # through reconciliation rather than choosing either fill.
                merged["_conflicting_fill_evidence"] = True
                continue
            if _reconciled_order_may_have_executed(
                {key: merged[key]}
            ) and not _reconciled_order_may_have_executed({key: value}):
                # The already-merged (outer) value reports real/unreadable
                # fill evidence and this more-inner value is an explicit
                # zero for the identical key -- keep the more fill-
                # suggestive reading rather than letting the zero erase it.
                continue
            merged[key] = value
    return merged


def _parse_rfc3339(value: Any) -> Optional[datetime]:
    """Parse an RFC3339/ISO-8601 timestamp (incl. ``Z`` and nanosecond precision).

    Robinhood returns timestamps like ``2026-07-06T18:31:29.201412148Z`` (9 fractional
    digits). ``datetime.fromisoformat`` accepts at most microsecond precision, so trim
    the fractional part to 6 digits and normalise the trailing ``Z`` first.

    Returns a timezone-*aware* datetime, or ``None`` when the value is missing/unparseable.
    The instant is preserved exactly: an explicit offset (``Z`` or ``±HH:MM``) is kept as
    given; a naive string (no zone suffix) is assumed to be Eastern, since that is the
    market timezone the strategy operates in. Downstream callers convert via
    ``astimezone(EASTERN)``, so any correctly-zoned input yields the right wall-clock time.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        frac = tail
        offset = ""
        for sep in ("+", "-"):
            idx = tail.find(sep)
            if idx != -1:
                frac, offset = tail[:idx], tail[idx:]
                break
        frac = frac[:6]
        text = f"{head}.{frac}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=EASTERN)
    return parsed


def _rh_interval(interval_minutes: int) -> str:
    try:
        return RH_INTERVAL_BY_MINUTES[interval_minutes]
    except KeyError as exc:
        supported = ", ".join(str(m) for m in sorted(RH_INTERVAL_BY_MINUTES))
        raise ValueError(
            f"Robinhood has no {interval_minutes}-minute bar; supported minutes: {supported}"
        ) from exc


# Callable signatures (all keyword-only in practice) that mirror the Robinhood MCP tools.
HistoricalsFn = Callable[..., Dict[str, Any]]  # (symbols, start_time, end_time, interval, bounds) -> dict
QuotesFn = Callable[..., Dict[str, Any]]        # (symbols) -> dict
BarsFn = Callable[..., Any]                     # generic finance bars -> iterable of bar-like mappings
PrevCloseFn = Callable[..., Any]                # generic finance previous close -> price or mapping
QuoteFn = Callable[..., Any]                    # generic finance quote -> price or mapping


class RobinhoodDataProvider:
    """``MarketDataProvider`` backed by the Robinhood ``get_equity_historicals`` /
    ``get_equity_quotes`` MCP tools via injected callables.

    Parameters
    ----------
    historicals_fn:
        ``fn(symbols=[...], start_time=iso, end_time=iso, interval=str, bounds=str,
        adjustment_type=str) -> dict`` returning the raw ``get_equity_historicals``
        response. All six keywords are always passed (they mirror the MCP tool inputs),
        so a wrapper must accept them — declare ``**kwargs`` if it ignores some.
    quotes_fn:
        Optional ``fn(symbols=[...]) -> dict`` returning the raw ``get_equity_quotes``
        response. Required for the previous-close and signal-time quote; without it
        ``get_previous_close_bar`` falls back to a daily historicals lookup and
        ``get_quote`` returns ``None`` (the strategy then uses the decision-bar close).
    """

    source = SourceName.ROBINHOOD

    def __init__(
        self,
        historicals_fn: HistoricalsFn,
        quotes_fn: Optional[QuotesFn] = None,
        *,
        bounds: str = "regular",
        adjustment_type: str = "none",
    ) -> None:
        self._historicals_fn = historicals_fn
        self._quotes_fn = quotes_fn
        self._bounds = bounds
        self._adjustment_type = adjustment_type
        # ⚡ Bolt: signal-time quote memo, scoped to a single decision moment. Both
        # get_previous_close_bar (via _previous_close_from_quote) and get_quote are
        # invoked from near_close_dca.evaluate_source_signal for the SAME ticker at the
        # SAME decision moment, each previously issuing an identical
        # quotes_fn(symbols=[ticker]) call -- a duplicate live MCP/network round-trip
        # (or a duplicate O(N) scan of the frozen quote snapshot) per ticker every run.
        # Memoizing the signal-time snapshot halves signal-time quote fetches
        # (~44 redundant fetches -> 0 for the live universe) with byte-identical
        # behavior: within one run each ticker is evaluated once at one decision moment.
        # The cache is cleared whenever the decision moment (``as_of``) changes, so a
        # reused provider stays bounded to the current moment's universe (~44 entries)
        # and never grows without bound or stale-serves a later run.
        # Deliberately NOT consulted by get_bid_ask -- that is the pre-placement spread
        # circuit breaker, which must always read a fresh quote (it keeps using the
        # uncached _quote_for), so the memo can never stale-serve the breaker.
        self._signal_quote_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._signal_quote_as_of: Optional[datetime] = None

    def get_intraday_bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval_minutes: int,
    ) -> List[Bar]:
        interval = _rh_interval(interval_minutes)
        response = self._historicals_fn(
            symbols=[ticker],
            start_time=_to_utc_iso(start),
            end_time=_to_utc_iso(end),
            interval=interval,
            bounds=self._bounds,
            adjustment_type=self._adjustment_type,
        )
        return self._parse_bars(response, ticker)


    def get_batch_intraday_bars(
        self,
        tickers: List[str],
        start: datetime,
        end: datetime,
        interval_minutes: int,
    ) -> Dict[str, List[Bar]]:
        """Fetch one historicals batch and return ``{symbol: [Bar, ...]}``.

        Three kinds of row are dropped without a log line, on purpose, because
        each is routine in a healthy Robinhood response and logging them would
        drown the one drop that IS worth a warning (a non-positive close):

        * result blocks whose ``symbol`` is missing or was not requested --
          the batch endpoint can echo related symbols, and a signal computed
          for a ticker the caller never asked about must never reach sizing;
        * bars flagged ``interpolated`` -- Robinhood synthesises those to fill
          gaps, so they carry no trade and would fake a price the market never
          printed;
        * bars whose ``begins_at`` does not parse -- a bar with no usable
          timestamp cannot be placed in the session, so it is safer to lose
          than to misfile.

        A symbol whose every bar was dropped is omitted from the result rather
        than mapped to ``[]`` so callers treat it exactly like a symbol the
        broker returned nothing for (2026-09-07 diagnostic P:COMMENT-03).
        """
        interval = _rh_interval(interval_minutes)
        response = self._historicals_fn(
            symbols=tickers,
            start_time=_to_utc_iso(start),
            end_time=_to_utc_iso(end),
            interval=interval,
            bounds=self._bounds,
            adjustment_type=self._adjustment_type,
        )
        data = _unwrap(response)
        results = data.get("results") or []
        grouped = {}
        for result in results:
            symbol = result.get("symbol")
            if not symbol or symbol not in tickers:
                continue  # unrequested/related symbol: never feed sizing

            bars = []
            for raw in result.get("bars") or []:
                if raw.get("interpolated"):
                    continue  # gap-filler with no trade behind it
                timestamp = _parse_rfc3339(raw.get("begins_at"))
                if timestamp is None:
                    continue  # unplaceable in the session; drop, don't misfile
                close = _to_float(raw.get("close_price"), default=-1.0)
                if close <= 0:
                    logger.warning("dropping %s bar with invalid close_price=%r", symbol, raw.get("close_price"))
                    continue
                bars.append(
                    Bar(
                        timestamp=timestamp,
                        open=_to_float(raw.get("open_price")),
                        high=_to_float(raw.get("high_price")),
                        low=_to_float(raw.get("low_price")),
                        close=close,
                        volume=_to_float(raw.get("volume")),
                    )
                )
            if bars:
                grouped[symbol] = bars
        return grouped

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        if self._quotes_fn is not None:
            bar = self._previous_close_from_quote(ticker, as_of)
            if bar is not None:
                return bar
        # Fallback: pull daily bars over the trailing two weeks and take the last
        # completed regular-session day strictly before the decision date.
        as_of_date = as_of.astimezone(EASTERN).date()
        response = self._historicals_fn(
            symbols=[ticker],
            start_time=_to_utc_iso(datetime.combine(as_of_date - timedelta(days=14), time(0, 0), tzinfo=EASTERN)),
            end_time=_to_utc_iso(as_of),
            interval="day",
            bounds=self._bounds,
            adjustment_type=self._adjustment_type,
        )
        daily = [bar for bar in self._parse_bars(response, ticker) if bar.timestamp.astimezone(EASTERN).date() < as_of_date]
        if not daily:
            return None
        return max(daily, key=lambda bar: bar.timestamp)

    def get_quote(self, ticker: str, as_of: datetime) -> Optional[Tuple[float, Optional[datetime]]]:
        if self._quotes_fn is None:
            return None
        quote = self._signal_quote_for(ticker, as_of)
        if quote is None:
            return None
        if not quote.get("has_traded", True) or quote.get("state", "active") != "active":
            logger.warning("skipping quote for %s: has_traded=%s state=%s", ticker, quote.get("has_traded"), quote.get("state"))
            return None
        price = _to_float(quote.get("last_trade_price"))
        if price <= 0:
            return None
        return price, _parse_rfc3339(quote.get("venue_last_trade_time"))

    def get_bid_ask(self, ticker: str, as_of: datetime) -> Optional[Tuple[float, float]]:
        """Return the current bid/ask quote for the spread circuit breaker."""
        if self._quotes_fn is None:
            return None
        quote = self._quote_for(ticker)
        if quote is None:
            return None
        # Check activity from both the nested quote and the raw sibling result —
        # some payloads expose has_traded/state at the top-level result alongside
        # the nested "quote" object. Fail closed if either source says inactive.
        siblings = quote.get("_siblings") or {}
        has_traded = quote.get("has_traded", True) and siblings.get("has_traded", True)
        state = quote.get("state", "active")
        sibling_state = siblings.get("state", "active")
        if not has_traded or state != "active" or sibling_state != "active":
            raise ValueError(
                f"no spread for {ticker}: inactive quote "
                f"(has_traded={quote.get('has_traded')}/{siblings.get('has_traded')}, "
                f"state={quote.get('state')}/{siblings.get('state')})"
            )
        # Some Robinhood MCP payloads expose bid/ask under alternate key names
        # (best_bid/best_ask) and/or as siblings of the nested "quote" object
        # rather than inside it. Check the nested quote first, then the raw
        # top-level result, so a genuinely positive value in either location
        # wins rather than an empty/zero nested placeholder unconditionally
        # shadowing a usable sibling value.
        bid_keys = ("bid_price", "bid", "best_bid", "best_bid_price")
        ask_keys = ("ask_price", "ask", "best_ask", "best_ask_price")
        bid = _first_positive_float(quote, *bid_keys) or _first_positive_float(siblings, *bid_keys)
        ask = _first_positive_float(quote, *ask_keys) or _first_positive_float(siblings, *ask_keys)
        # Deliberately no last-trade-price fallback here: a spread breaker
        # that treats a missing/one-sided NBBO as a synthetic 0% spread would
        # fail open exactly when it has the least information. Any quote
        # without a genuine two-sided bid/ask stays unavailable.
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError(
                f"no usable bid/ask in {ticker} quote (bid={bid!r}, ask={ask!r})"
            )
        return bid, ask

    # -- internals ---------------------------------------------------------------

    def _parse_bars(self, response: Any, ticker: str) -> List[Bar]:
        data = _unwrap(response)
        results = data.get("results") or []
        bars: List[Bar] = []
        for result in results:
            if result.get("symbol") and result.get("symbol") != ticker:
                continue
            for raw in result.get("bars") or []:
                if raw.get("interpolated"):
                    continue  # gap-fill bar carries no new information
                timestamp = _parse_rfc3339(raw.get("begins_at"))
                if timestamp is None:
                    continue
                close = _to_float(raw.get("close_price"), default=-1.0)
                if close <= 0:
                    # A missing/non-numeric close during an API gap or partial bar would
                    # otherwise become 0.0 and read as a ~100% drop -> spurious max BUY.
                    # Drop it so the strategy degrades to NO_DATA instead.
                    logger.warning("dropping %s bar with invalid close_price=%r", ticker, raw.get("close_price"))
                    continue
                bars.append(
                    Bar(
                        timestamp=timestamp,
                        open=_to_float(raw.get("open_price")),
                        high=_to_float(raw.get("high_price")),
                        low=_to_float(raw.get("low_price")),
                        close=close,
                        volume=_to_float(raw.get("volume")),
                    )
                )
        return bars

    def _signal_quote_for(self, ticker: str, as_of: datetime) -> Optional[Dict[str, Any]]:
        """Signal-time quote for ``ticker``, memoized per (ticker, decision moment).

        ⚡ Bolt: dedupes the two signal-time quote fetches that
        get_previous_close_bar and get_quote each make for the same ticker at the
        same decision moment. get_bid_ask intentionally bypasses this (it must read
        a fresh quote for the pre-placement spread breaker). The returned dict is
        only ever read by its consumers, so sharing the cached object is safe.

        The memo is scoped to a single decision moment: when ``as_of`` changes it is
        dropped, keeping it bounded to the current moment's universe rather than
        accumulating an entry per (ticker, moment) forever on a reused provider.
        """
        if as_of != self._signal_quote_as_of:
            self._signal_quote_cache.clear()
            self._signal_quote_as_of = as_of
        cached = self._signal_quote_cache.get(ticker, _UNSET)
        if cached is not _UNSET:
            return cached  # type: ignore[return-value]
        quote = self._quote_for(ticker)
        self._signal_quote_cache[ticker] = quote
        return quote

    def _quote_for(self, ticker: str) -> Optional[Dict[str, Any]]:
        data = _unwrap(self._quotes_fn(symbols=[ticker]))  # type: ignore[misc]
        for result in data.get("results") or []:
            quote = result.get("quote") or {}
            if quote.get("symbol") in (ticker, None):
                # Attach the official settled close so callers can reuse it.
                quote = dict(quote)
                quote["_close"] = result.get("close") or {}
                # Some MCP payloads expose bid/ask (and possibly other quote-side
                # fields) as top-level siblings of the nested "quote" object rather
                # than inside it. Keep the raw top-level result available so callers
                # like get_bid_ask can fall back to sibling fields without an
                # unconditional merge silently discarding usable nested data (or
                # vice versa).
                quote["_siblings"] = result
                return quote
        return None

    def _previous_close_from_quote(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        quote = self._signal_quote_for(ticker, as_of)
        if quote is None:
            return None
        official = quote.get("_close") or {}
        price = _to_float(official.get("price")) or _to_float(quote.get("previous_close"))
        if price <= 0:
            return None
        close_date_str = official.get("date") or quote.get("previous_close_date")
        close_date = _parse_iso_date(close_date_str) or (as_of.astimezone(EASTERN).date() - timedelta(days=1))
        # Stamp the prior-session close bar at the 4pm ET regular-session close.
        timestamp = datetime.combine(close_date, time(16, 0), tzinfo=EASTERN)
        # low=0.0 (not price): this quote-derived bar has no real prior-session low,
        # only a close. select_first_allocation_base_usd treats a low <= 0 as
        # unknown/missing (conservative $1 base) -- fabricating low=price here would
        # make every below-close decision price also read as "below the low" and
        # always select the $2 base.
        return Bar(timestamp=timestamp, open=price, high=price, low=0.0, close=price, volume=0.0)


class FinanceDataProvider:
    """Source-agnostic ``MarketDataProvider`` for the independent "finance" cross-check.

    The reconciliation logic in :mod:`near_close_dca` compares a finance-side and a
    Robinhood-side signal before ordering, so the finance provider should be a genuinely
    independent source (e.g. yfinance/Yahoo, a vendor OHLCV API, or a second Robinhood
    query at a different granularity). Wire it with two required callables and
    an optional quote callable:

    * ``bars_fn(ticker, start, end, interval_minutes) -> iterable`` yielding bar-like
      mappings with ``timestamp`` (datetime or ISO string) and ``open/high/low/close/volume``.
    * ``previous_close_fn(ticker, as_of) -> float | mapping`` returning the prior close.
    * ``quote_fn(ticker, as_of) -> float | mapping`` returning a current quote when
      the connector supports one; omitting it preserves completed-bar semantics.
      Keyword-only, and added after ``supports_corporate_actions`` rather than
      before it, so an existing positional caller of
      ``FinanceDataProvider(bars_fn, previous_close_fn, False)`` keeps setting
      ``supports_corporate_actions`` rather than silently reinterpreting that
      boolean as ``quote_fn`` (Codex review: "Preserve the existing third
      positional argument").

    Keys are matched leniently (``close``/``close_price``, ``timestamp``/``begins_at``/``t``)
    so most vendor payloads work without a bespoke adapter.

    ``supports_corporate_actions`` defaults to True: ``get_corporate_action_factor``
    below is always defined on this class regardless of what ``bars_fn`` actually
    returns, so ``getattr(provider, "get_corporate_action_factor", None)`` can never
    tell apart a provider whose ``bars_fn`` genuinely carries split/dividend fields
    (e.g. a yfinance-style daily row) from one that never will (e.g. a bare
    Robinhood-historicals wrapper returning plain OHLCV). For the latter, every call
    would fail closed and return ``None``, and near_close_dca.py's durable rearm-latch
    check treats that as "confirmed on today's data was tried but is unavailable" --
    holding the latch and requiring its archive fallback to match the evaluation day
    exactly, rather than the more lenient, always-available fallback a provider with
    no live capability at all gets. That can leave the latch held indefinitely on
    every run for a real, OHLC-only provider that was perfectly usable before this
    capability existed (Codex review: "Preserve the documented FinanceDataProvider
    contract"). Pass ``supports_corporate_actions=False`` when wiring a ``bars_fn``
    known to never carry split/dividend fields, so callers route it through the
    lenient legacy-provider fallback instead.
    """

    source = SourceName.FINANCE

    def __init__(
        self,
        bars_fn: BarsFn,
        previous_close_fn: PrevCloseFn,
        supports_corporate_actions: bool = True,
        *,
        quote_fn: Optional[QuoteFn] = None,
    ) -> None:
        self._bars_fn = bars_fn
        self._previous_close_fn = previous_close_fn
        self._quote_fn = quote_fn
        self.supports_corporate_actions = supports_corporate_actions

    def get_quote(self, ticker: str, as_of: datetime) -> Optional[Tuple[float, Optional[datetime]]]:
        """Return the connector quote when the Finance wiring supplies one."""
        if self._quote_fn is None:
            return None
        result = self._quote_fn(ticker, as_of)
        if result is None:
            return None
        if isinstance(result, dict):
            raw_price = result.get("price") or result.get("last_trade_price")
            timestamp = _parse_rfc3339(result.get("as_of") or result.get("timestamp"))
        else:
            raw_price = result
            timestamp = as_of
        # bool is an int subclass, so float(True) == 1.0 would otherwise sail
        # through _to_float as an ordinary $1.00 quote. A malformed connector
        # response like {"price": true, "as_of": ...} would then be accepted
        # as a positive price and could turn an ordinary stock into a
        # maximum-drop BUY signal (Codex review: "Reject boolean Finance quote
        # prices").
        if isinstance(raw_price, bool):
            return None
        price = _to_float(raw_price)
        return (price, timestamp) if price > 0 else None

    def get_intraday_bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval_minutes: int,
    ) -> List[Bar]:
        raw_bars = self._bars_fn(ticker, start, end, interval_minutes) or []
        bars: List[Bar] = []
        for raw in raw_bars:
            bar = _coerce_bar(raw)
            if bar is not None:
                bars.append(bar)
        return bars

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        result = self._previous_close_fn(ticker, as_of)
        if result is None:
            return None
        if isinstance(result, Bar):
            return result
        price = 0.0
        low = 0.0
        if isinstance(result, dict):
            bar = _coerce_bar(result)
            if bar is not None:
                return bar
            # A price-only mapping (e.g. {"price": "100"} / {"close": "100"}) has no bar
            # timestamp; fall through to the scalar path rather than dropping it, so the
            # finance source still confirms and the reconciliation stays two-sided.
            price = _to_float(result.get("price") or result.get("close") or result.get("previous_close"))
            # Preserve a real low the mapping supplied even though it lacks a bar
            # timestamp (Codex review: "Preserve an available low in price-only
            # finance mappings") -- e.g. {"close": 100, "low": 95} would otherwise
            # fall through here and lose its genuine low to the low=0.0 default
            # below, wrongly downgrading a below-$95 decision price to the $1 base.
            # select_first_allocation_base_usd separately validates this against
            # previous_close, so an invalid/missing low still safely falls back to
            # 0.0 (unknown).
            low = _to_float(result.get("low"))
        else:
            price = _to_float(result)
        if price <= 0:
            return None
        timestamp = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        # low=0.0 (not price) when no real low was supplied: a scalar previous-close
        # carries no real prior-session low. See the matching comment in
        # _previous_close_from_quote above -- fabricating low=price here would make
        # select_first_allocation_base_usd treat every below-close price as also
        # below the low and always pick the $2 base.
        return Bar(timestamp=timestamp, open=price, high=price, low=low, close=price, volume=0.0)

    def get_corporate_action_factor(
        self, ticker: str, since: date, as_of: date
    ) -> Optional[Tuple[float, date]]:
        """Read split and dividend metadata from a fresh daily finance query.

        ``since`` is an exclusive durable cursor.  The caller deliberately
        seeds a newly armed latch one day before its reference date, while an
        already-applied cursor is the event date itself; keeping the boundary
        exclusive here therefore includes cycle-start-day splits without ever
        applying an old split twice.

        The result is unavailable unless every returned row explicitly carries
        both a split field and a dividend field.  OHLC-only responses cannot
        prove that no action occurred and must not advance the durable cursor.

        A cash dividend is folded into the SAME cumulative factor a split is,
        using ``(close + dividend) / close`` from that row's own EX-DATE
        close -- exactly the same reconstruction ``archived_split_factor``
        performs -- so a latch spanning a large special distribution is
        rebased instead of staying pinned to its pre-distribution scale
        (Codex review: "Fold dividends into the live corporate-action
        factor").

        A present-but-blank/``None`` split or dividend value is metadata the
        provider could not supply, not confirmed "no action"; treating it as
        ``0.0`` would let the cursor advance past a row a later, corrected
        response could still reveal a real action on, exactly the false
        negative this whole live path exists to avoid (Codex review: "Reject
        blank split values before advancing the cursor"). Both naive
        ``datetime`` row timestamps AND ISO date/timestamp strings are
        interpreted as Eastern via :func:`_parse_rfc3339` -- consistent with
        ``_coerce_bar`` elsewhere in this module -- rather than the host's
        local zone or a raw textual-prefix slice; an offset-bearing string
        like ``"2026-08-18T02:00:00Z"`` (August 17 ET) sliced to its first
        ten characters would misfile it as August 18 (Codex review:
        "Interpret naive action datetimes as Eastern"; "Parse ISO action
        timestamps in Eastern time"). Duplicate rows for the same effective
        date (permitted by the generic ``bars_fn`` contract, e.g. overlapping
        result batches) are folded once, not multiplied once per copy -- a
        conflicting duplicate (different ratio/dividend reported for the same
        date) fails the whole call rather than silently picking one (Codex
        review: "Deduplicate action dates before multiplying ratios"). A
        failure raised while the (possibly lazily-evaluated) row iterable is
        consumed, not just while it is created -- including an
        ``OverflowError`` from ``since`` sitting at/near ``date.max`` -- is
        caught the same as one raised at creation time -- a transient
        failure mid-iteration must degrade to "cannot confirm", not abort
        the entire scheduled scan (Codex review: "Catch failures raised
        while iterating finance rows"; "Catch cursor-range overflow before
        querying"). The close backing a dividend reconstruction accepts the
        same key aliases :func:`_coerce_bar` does (``Close``/``close``/
        ``close_price``/``c``), not just the first two (Codex review:
        "Accept the provider's documented close aliases").

        The newest EXAMINED row must itself land on ``as_of`` -- a response
        whose daily rows stop short of the evaluation day (e.g. an
        in-progress session the provider has not yet published) is treated
        as unavailable rather than a confirmed factor, since a split or
        dividend effective ON ``as_of`` could still be missing from it and
        this same evaluation's decision price may already reflect it (Codex
        review: "Require action coverage through the evaluation day"). That
        endpoint check alone does not prove every INTERIOR session was
        examined too -- a response covering Monday and Wednesday but
        transiently omitting Tuesday still lands its newest row on ``as_of``.
        Every NYSE trading weekday strictly after ``since`` through ``as_of``
        (per :func:`near_close_dca.nyse_holidays`) must have its own row
        (Codex review: "Reject action windows with missing interior
        sessions").

        ``since == as_of`` means the cursor already covers everything through
        the evaluation day -- a same-day later stream re-evaluating an
        already-examined latch, most commonly. Querying anyway would send
        the injected ``bars_fn`` an inverted range (``start`` one day past
        ``end``), an unnecessary charged request that some wrappers would
        reject outright with an exception this method's narrow catch list
        does not list. Short-circuit to the already-confirmed no-op
        ``(1.0, since)`` instead (Codex review: "Skip action queries when
        the cursor already covers today"). ``since > as_of`` -- a restored
        ledger carrying a schema-valid but nonsensical future cursor -- is
        NOT the same no-op: treating the whole unexamined interval as
        confirmed would evaluate recovery against a stale reference that a
        real corporate action occurring before ``as_of`` could have already
        invalidated. Report unavailable instead (Codex review: "Reject
        cursors later than the evaluation day").
        """
        if since == as_of:
            return 1.0, since
        if since > as_of:
            return None
        factor = 1.0
        cursor: Optional[date] = None
        row_factors: dict = {}
        try:
            start = datetime.combine(since + timedelta(days=1), time.min, tzinfo=EASTERN)
            end = datetime.combine(as_of, time.max, tzinfo=EASTERN)
            rows = self._bars_fn(ticker, start, end, 1440) or []
            for row in rows:
                if not isinstance(row, dict):
                    return None
                split_key = next(
                    (key for key in ("Stock Splits", "stock_splits", "split_ratio") if key in row), None
                )
                dividend_key = next(
                    (key for key in ("Dividends", "dividends", "dividend_amount") if key in row), None
                )
                if split_key is None or dividend_key is None:
                    return None
                raw_day = (
                    row.get("date")
                    or row.get("Date")
                    or row.get("timestamp")
                    or row.get("begins_at")
                    or row.get("t")
                )
                if isinstance(raw_day, datetime):
                    aware_day = raw_day if raw_day.tzinfo is not None else raw_day.replace(tzinfo=EASTERN)
                    row_day = aware_day.astimezone(EASTERN).date()
                else:
                    parsed_day = _parse_rfc3339(str(raw_day)) if raw_day is not None else None
                    if parsed_day is None:
                        return None
                    row_day = parsed_day.astimezone(EASTERN).date()
                if row_day <= since or row_day > as_of:
                    continue
                raw_ratio = row[split_key]
                raw_dividend = row[dividend_key]
                # Providers sometimes serialize unavailable numeric metadata as
                # whitespace rather than an empty string. Keep that on the
                # same fail-closed path: it is not evidence of a zero action.
                split_blank = raw_ratio is None or (isinstance(raw_ratio, str) and not raw_ratio.strip())
                dividend_blank = raw_dividend is None or (
                    isinstance(raw_dividend, str) and not raw_dividend.strip()
                )
                if split_blank or dividend_blank:
                    return None
                # bool is a subclass of int in Python, so float(True)/
                # float(False) succeed as 1.0/0.0 instead of raising --
                # "Dividends": true survived as a fabricated $1 dividend and
                # "Stock Splits": true as a real 1:1 split ratio, letting the
                # inverse-Martingale recovery-latch path durably rebase
                # itself on corporate-action data that was never actually
                # reported (Codex review, PR #1299: "Reject boolean
                # corporate-action values"). Rejected the same way the
                # non-numeric/empty-string case just above already is.
                if isinstance(raw_ratio, bool) or isinstance(raw_dividend, bool):
                    return None
                ratio = float(raw_ratio)
                dividend = float(raw_dividend)
                if not math.isfinite(ratio) or ratio < 0 or not math.isfinite(dividend) or dividend < 0:
                    return None
                row_factor = ratio if ratio > 0 else 1.0
                if dividend > 0:
                    close = _to_float(
                        row.get("Close", row.get("close", row.get("close_price", row.get("c"))))
                    )
                    if close is None or close <= 0:
                        return None
                    row_factor *= (close + dividend) / close
                if row_day in row_factors:
                    if row_factors[row_day] != row_factor:
                        return None
                    continue
                row_factors[row_day] = row_factor
                cursor = row_day if cursor is None or row_day > cursor else cursor
                factor *= row_factor
            # Endpoint coverage (cursor == as_of, checked below) alone does not
            # prove EVERY session in the window was examined -- a response that
            # returns Monday and Wednesday but transiently omits Tuesday still
            # reports cursor == as_of and would silently miss Tuesday's split
            # (Codex review: "Reject action windows with missing interior
            # sessions"). Require a row for every NYSE trading weekday strictly
            # after `since` through `as_of`; a market holiday or weekend is
            # correctly never expected to have one. Bounded to a ~10-year span
            # so a corrupted/degenerate cursor (e.g. `since` near date.min)
            # cannot turn this into an unbounded day-by-day scan.
            if cursor is not None and (as_of - since).days <= 3650:
                holidays_by_year: Dict[int, Any] = {}
                probe_day = since + timedelta(days=1)
                while probe_day <= as_of:
                    if probe_day.weekday() < 5:
                        year_holidays = holidays_by_year.get(probe_day.year)
                        if year_holidays is None:
                            year_holidays = nyse_holidays(probe_day.year)
                            holidays_by_year[probe_day.year] = year_holidays
                        if probe_day not in year_holidays and probe_day not in row_factors:
                            cursor = None
                            break
                    probe_day += timedelta(days=1)
            elif cursor is not None:
                cursor = None
        except Exception:  # noqa: BLE001 - provider boundary must fail closed
            # Injected connectors also decode/iterate third-party payloads here.
            # A malformed response may raise exceptions such as KeyError or
            # IndexError that are not part of the connector's advertised API;
            # no provider failure may abort evaluation of the remaining tickers
            # (PR #1357: "Fix corporate action isolation and state update
            # races"). The narrower (OSError, RuntimeError, TypeError,
            # ValueError, OverflowError) tuple this used to catch let exactly
            # those payload-shape errors escape past the strategy's own
            # per-ticker isolation and abort the whole scheduled scan.
            return None
        if cursor is None or cursor != as_of or not math.isfinite(factor) or factor <= 0:
            return None
        return factor, cursor


class RobinhoodBroker:
    """``BrokerAdapter`` for Robinhood equities via injected MCP callables.

    All callables are optional; while ``enable_live_orders`` is False a missing
    callable degrades that check to a safe default (permissive review/tradability,
    no same-day order, simulated buying power) so the broker is usable in
    progressively-wired environments. With ``enable_live_orders`` True those same
    defaults are the opposite of safe -- a partially wired live broker would place
    real orders against a fabricated buying-power figure, skip the tradability
    check, and run its duplicate gate against an empty history -- so every lookup
    whose callable is missing raises instead (2026-09-07 diagnostic, C:F1; the
    rule ``review_market_buy`` has always applied, extended to the other three).
    Real placement is gated behind ``enable_live_orders`` **and** a ``place_fn``.

    Callable signatures (keyword args mirror the Robinhood MCP tools):

    * ``buying_power_fn(account_number) -> float``
    * ``orders_fn(account_number, symbol, created_at_gte) -> dict`` (get_equity_orders)
    * ``tradability_fn(account_number, symbols) -> dict`` (get_equity_tradability)
    * ``review_fn(account_number, symbol, side, type, dollar_amount) -> dict`` (review_equity_order)
    * ``place_fn(account_number, symbol, side, type, dollar_amount, ref_id, time_in_force) -> dict``
    * ``order_fill_fn(account_number, order_id) -> dict`` (get_equity_orders single-order mode)

    ``supports_concurrent_fill_lookup`` defaults to False: ``order_fill_fn`` is typically a
    thin wrapper around a scheduler-owned MCP callable, and MCP client sessions are commonly
    bound to a single connection/event loop that is not safe to invoke from multiple threads
    at once. Only pass ``True`` once the wired ``order_fill_fn`` has been verified safe for
    concurrent calls (e.g. it opens an independent connection per call).

    ``review_fn``/``place_fn`` are reused (not duplicated) for whole-share,
    quantity-based orders -- see :meth:`review_whole_share_buy` and
    :meth:`place_whole_share_buy`, built for
    ``dca_whole_share_accumulator.py`` -- by passing ``quantity=`` instead of
    ``dollar_amount=``. This assumes the wired Robinhood
    ``review_equity_order``/``place_equity_order`` MCP tools accept a
    ``quantity`` argument as an alternative order-sizing method to
    ``dollar_amount``, matching how Robinhood's real order API supports both.
    **Unverified in this repo**: no session that has written this module has
    had live Robinhood MCP access to confirm it. A live agent session with
    real broker access should confirm the tool accepts ``quantity`` (and
    what it returns for a non-fractional instrument) before the whole-share
    path is trusted with a real order -- see
    ``docs/whole_share_accumulation.md``.
    """

    def __init__(
        self,
        account: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
        *,
        buying_power_fn: Optional[Callable[..., Any]] = None,
        orders_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        tradability_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        review_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        place_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        order_fill_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        enable_live_orders: bool = False,
        time_in_force: str = "gfd",
        simulated_buying_power_usd: float = 300.0,
        supports_concurrent_fill_lookup: bool = False,
        require_placement_identity: bool = False,
    ) -> None:
        self._account = account
        self._buying_power_fn = buying_power_fn
        self._orders_fn = orders_fn
        self._tradability_fn = tradability_fn
        self._review_fn = review_fn
        self._place_fn = place_fn
        self._order_fill_fn = order_fill_fn
        self.enable_live_orders = enable_live_orders
        self._time_in_force = time_in_force
        self._simulated_buying_power_usd = simulated_buying_power_usd
        self._raw_placement_responses: List[Dict[str, Any]] = []
        # Order ids this instance has already booked as successful placements;
        # see _refuse_replayed_order_id.
        self._accepted_order_ids: set = set()
        # Strict placement identity: refuse a placement response that echoes
        # neither a symbol nor a client reference. Off by default; see
        # _placement_response_identity_mismatch's docstring.
        self.require_placement_identity = require_placement_identity
        self.supports_concurrent_fill_lookup = supports_concurrent_fill_lookup

    def account_number(self) -> str:
        return self._account

    def _refuse_unwired_live_lookup(self, callable_name: str, consequence: str) -> None:
        """Raise when a lookup callable is missing while the live gate is on.

        Truthiness, not ``is True``, on purpose: a misconfigured truthy value
        (``"false"``, ``1``) must fail closed here even though placement itself
        requires the literal ``True``. Only a genuinely dry-run broker may fall
        through to the simulated default the caller supplies.
        """
        if self.enable_live_orders:
            raise RuntimeError(
                f"live orders enabled but no {callable_name} wired; refusing to {consequence}"
            )

    def available_buying_power(self) -> float:
        if self._buying_power_fn is None:
            self._refuse_unwired_live_lookup(
                "buying_power_fn",
                f"assume ${self._simulated_buying_power_usd:.2f} of simulated buying power",
            )
            return self._simulated_buying_power_usd
        try:
            result = self._buying_power_fn(account_number=self._account)
        except Exception:  # noqa: BLE001 - never let buying-power lookup crash a scan
            logger.exception("buying-power lookup failed; treating as 0")
            return 0.0
        # Accept either a scalar or a raw MCP dict (per the module-level callable contract).
        if isinstance(result, dict):
            # A layer this loop walks past on its way to a balance field can
            # itself report FAILURE: `{"data": {"success": true, "data":
            # {"success": false, "buying_power": "100"}}}` has the inner
            # layer carry both the balance figure this loop is looking for
            # AND an explicit `success: false` -- reading the balance
            # without checking that flag first lets a live placement gate
            # be authorized against a fabricated figure from a lookup that
            # failed one layer down (Codex review, PR #1164: "Reject failed
            # balance layers reached through success wrappers"). Scanning
            # every layer for failure evidence BEFORE the balance-field loop
            # below means a failure anywhere along the traversal fails
            # closed, whichever layer eventually would have supplied the
            # figure.
            _data_layers, _data_layers_truncated = _all_data_layers(result)
            if _data_layers_truncated:
                # See `_all_data_layers`'s own docstring ("Fail closed when
                # envelope descent reaches its cap"): failure evidence past
                # the depth cap is invisible to the scan below, so an
                # incomplete scan must not be read as "no failure found".
                logger.warning("buying-power lookup response truncated at max envelope depth; treating as 0")
                return 0.0
            for _layer in (result, *_data_layers):
                _layer_error = _is_error_response(_layer)
                if _layer_error is not None:
                    # _artifact_safe_repr, not the raw error text (S:ADP-02,
                    # 2026-09-07 diagnostic): a broker/connector error string
                    # can echo request context (account number, URLs), and
                    # this is the same class of leak the placement-response
                    # logging elsewhere in this module already redacts.
                    logger.warning("buying-power lookup reported a failure: %s", _artifact_safe_repr(_layer_error))
                    return 0.0
            # Scanned across every layer `_unwrap_traversed_layers` walks
            # through, not just the single layer `_unwrap` settles on: the
            # balance aliases are truthy-checked in `_layer_carries_
            # envelope_evidence` now (see `_TRUTHY_DOMAIN_PAYLOAD_KEYS`'s
            # own docstring), so an explicit `buying_power: 0` no longer
            # anchors `_unwrap`'s descent -- it can settle on a DEEPER
            # layer that never mentions balance at all. Scanning every
            # traversed layer, outermost first, still finds that explicit
            # zero exactly where `_unwrap` used to stop on it, preserving
            # the "genuine zero over a stale nonzero figure sitting deeper"
            # reading this loop has always given these fields (Codex
            # review, PR #1168: "Do not let zero balance fields hide
            # nested orders").
            for data in _unwrap_traversed_layers(result):
                for key in (
                    "buying_power",
                    "cash",
                    "unallocated_buying_power",
                    "portfolio_cash",
                    "withdrawable_amount",
                ):
                    if key in data:
                        return _to_float(data.get(key))
            logger.warning(
                "buying-power response had no recognizable amount field: %s", _artifact_safe_repr(result)
            )
            return 0.0
        return _to_float(result)

    def _iter_same_day_orders(
        self, ticker: str, trading_day: date, *, require_provable_history: bool = False
    ) -> Iterator[Dict[str, Any]]:
        """Yield every order for ``ticker`` from ``trading_day`` onward, page by page.

        Lazy on purpose. ``get_equity_orders`` has a lower time bound
        (``created_at_gte``) but no upper one, so a lease recovered several
        trading days after its claim scans every order since -- and the
        newest-first ordering puts the one we want last. Materializing the whole
        result before searching it meant a long-enough history tripped the page
        ceiling and raised even when the matching ref had already gone past,
        which recovery then read as "cannot reconcile" and preserved the lease
        indefinitely (Codex review: "Bound historical recovery queries to the
        claim day"). Yielding lets a caller looking for ONE order stop the
        moment it finds it, so a found ref never depends on how much history
        sits behind it.

        The ceiling still binds for callers that must consume everything --
        proving absence, or summing notional -- which is the fail-closed
        direction: those callers raise rather than conclude from a truncated
        history.

        ``require_provable_history`` raises, rather than yielding nothing, for
        the two shapes that are indistinguishable from a genuinely empty history
        but prove nothing: an adapter with no ``orders_fn`` wired at all, and a
        response that is not a recognizable orders envelope. Silence from either
        one let crash recovery conclude that a claimed order was never submitted
        and release the balance for a duplicate purchase (Codex review:
        "Preserve leases when order history is unavailable").

        It is opt-in for the DRY-RUN broker, whose ordinary duplicate-order gate
        keeps the established contract that an unwired adapter reports "no order
        today" and lets a simulated buy proceed. A LIVE broker (``enable_live_orders``
        True) gets the provable-history rule unconditionally: ``morning_dca.py``
        uses this lookup as its sole duplicate gate, so an unwired live adapter
        answering "no order today" is precisely the same-day double-buy the
        2026-07-23 incident recorded (2026-09-07 diagnostic, C:F1). That rule
        covers BOTH silent shapes for a live broker, not only the unwired one:
        the 2026-09-07 mock beta run showed a wired callable returning a
        non-empty page the adapter cannot recognize was still read as "no
        order today", and the executors' sole duplicate gate re-placed every
        order on a retry.
        """
        require_provable_history = require_provable_history or bool(self.enable_live_orders)
        if self._orders_fn is None:
            if require_provable_history:
                raise RuntimeError(
                    f"same-day order lookup for {ticker} has no orders_fn wired; "
                    "an unqueryable history cannot prove an order is absent"
                )
            return
        seen_cursors: set[str] = set()
        seen_order_ids: set[str] = set()
        cursor: Optional[str] = None
        for _ in range(_MAX_ORDER_PAGES):
            kwargs: Dict[str, Any] = {
                "account_number": self._account,
                "symbol": ticker,
                "created_at_gte": trading_day.isoformat(),
            }
            if cursor is not None:
                kwargs["cursor"] = cursor
            response = self._orders_fn(**kwargs)
            error = _response_failure(response)
            if error is not None:
                # Fail closed: an error-shaped lookup ("no orders" by omission) must not let
                # the one-order-per-ticker-per-day guard pass and allow a duplicate buy.
                raise RuntimeError(f"same-day order lookup for {ticker} failed: {error}")
            if _unwrap_hides_contradicted_nested_data(response):
                # A self-contradictory envelope (an affirmative outer health
                # flag disputed by a negative inner one) makes `_unwrap`
                # settle on the OUTER layer -- correct for a placement
                # classifier's single-order verdict, but this reader wants
                # the real `orders` collection, which may be hidden one
                # layer past the contradiction. Reading the truncated outer
                # layer as a clean, order-less page would authorize a
                # duplicate buy against a live order this call never even
                # looked at (Codex review, PR #1168: "Keep contradictory
                # order-history wrappers fail-closed").
                raise RuntimeError(
                    f"same-day order lookup for {ticker} returned a self-contradictory response"
                )
            data = _unwrap_seeking_order_evidence(response)
            # `_response_failure` above only inspects `response` and its
            # own single, non-recursive `_unwrap(response)` layer -- a
            # failure sitting past a recognized DOMAIN field (`{"data":
            # {"buying_power": "100", "data": {"success": false, "error":
            # "down", "data": {"orders": [...]}}}}`) is invisible to that
            # narrow check, so `error` above stayed `None` and this
            # function proceeded straight to `_unwrap_seeking_order_
            # evidence`, which correctly settles on the DEEPER failure
            # layer (its own domain-field-skipping descent) -- but nothing
            # then inspected THAT layer for failure evidence either, so
            # `data.get("orders")` (absent on the failure layer, one more
            # level past it) was read as an ordinary empty page instead of
            # the genuine service failure it reports (Codex review, PR
            # #1164/#1169: "Scan nested history failures past domain
            # wrappers"). Checking only the settled `data` layer is STILL
            # not enough: `_unwrap_seeking_order_evidence` also halts on a
            # pagination pointer (`next_cursor`/`cursor`) one layer ABOVE a
            # deeper failure (`{"data": {"next_cursor": "abc", "data":
            # {"success": false, "error": "history down"}}}}`), so `data`
            # here can settle on the pointer layer while the real failure
            # sits one level past it, unseen. Scan every layer
            # `_all_data_layers` walks -- an unconditional, full-depth
            # "data" descent independent of whichever layer the
            # order/pagination-seeking unwrap happens to settle on -- for
            # failure evidence instead (Codex review, PR #1164: "Scan
            # failures beyond pagination-pointer layers").
            _data_layers, _data_layers_truncated = _all_data_layers(response)
            if _data_layers_truncated:
                # See `_all_data_layers`'s own docstring ("Fail closed when
                # envelope descent reaches its cap"): failure evidence past
                # the depth cap is invisible to the scan below, so an
                # incomplete scan must not be read as "no failure found".
                raise RuntimeError(
                    f"same-day order lookup for {ticker} response exceeded the max envelope depth"
                )
            _data_error = None
            for _layer in _data_layers:
                _data_error = _is_error_response(_layer)
                if _data_error is not None:
                    break
            if _data_error is not None:
                raise RuntimeError(f"same-day order lookup for {ticker} failed: {_data_error}")
            if require_provable_history and not _is_provable_orders_page(response, cursor):
                # A None, a bare list, a dict carrying no orders list, or a
                # single-order envelope is not a list-mode page -- NOT a valid
                # empty history.
                raise RuntimeError(
                    f"same-day order lookup for {ticker} returned an unrecognizable page"
                )
            # See `_orders_and_cursor_layers_in_settled_layer_or_nested`'s
            # own docstring: reads the SAME effective rows
            # `_is_provable_orders_page` above already validated,
            # including any sitting past the settled layer's own nested
            # "data" chain -- not just its bare "orders" key (Codex
            # review, PR #1169: "Preserve nested orders when retaining
            # pagination pointers" / "Traverse all wrappers below
            # pagination pointers"). Also captures every LAYER traversed
            # to reach those rows, so the cursor lookup below can read
            # each one's own continuation pointer -- not just the row-
            # bearing layer or only ever the outer settled layer's (Codex
            # review, PR #1168: "Preserve nested continuation pointers
            # with nested orders" / "Carry intervening pagination cursors
            # forward").
            orders, _cursor_layers = _orders_and_cursor_layers_in_settled_layer_or_nested(data)
            if orders is None:
                orders = []
            if not isinstance(orders, list):
                raise RuntimeError(
                    f"same-day order lookup for {ticker} returned orders in an unexpected shape"
                )
            for order in orders:
                # Robinhood can repeat an order at a page boundary (an insert
                # ahead of the cursor shifts every row behind it by one). An
                # id-less order cannot be deduplicated and is always yielded --
                # every observed shape carries one, so this only widens the
                # fail-open surface for a response shape that would already be
                # unusable elsewhere. Summing every returned row verbatim
                # overstates same_day_ticker_notional_usd's broker-reported
                # notional and can wrongly shrink or skip a guarded top-up
                # (Codex review: "Deduplicate orders across pagination
                # boundaries").
                order_id = order.get("id") if isinstance(order, dict) else None
                # Falls through past a FALSEY (not just blank) primary id
                # too: `id=0` is not blank per `_is_blank_broker_field`
                # (only None/""/whitespace reads as blank), so two
                # DIFFERENT rows -- one rejected, one still live -- each
                # carrying a sentinel `id=0` alongside their own distinct
                # `order_id` were deduplicated together under the shared
                # key "0", and the live row could be silently discarded
                # (Codex review, PR #1169: "Fall through falsey primary IDs
                # while deduplicating history"). Also falls through past a
                # NON-SCALAR primary id (a list, a mapping) -- present and
                # truthy, so neither the blank nor the falsey check catches
                # it, but `str(order_id)` below would stringify it into an
                # apparently-valid key like "['BAD']". Two UNRELATED
                # malformed rows sharing that identical non-scalar `id`
                # (e.g. both `{"id": ["BAD"], "order_id": ...}`) then
                # deduplicated together under that shared fabricated key
                # even though their own distinct `order_id`s prove them
                # different orders, silently discarding a still-live row
                # `same_day_strategy_order_exists()` needed to see (Codex
                # review, PR #1168: "Validate primary IDs before
                # deduplicating history"). `bool` is excluded from the
                # scalar allowlist -- never a real broker id, same as
                # everywhere else in this module. A truthy NUMERIC
                # sentinel -- `id=-1`, `id=float("nan")` -- passes every
                # check above too (not blank, not falsey, not non-scalar),
                # even though this module's own placement/reconciliation
                # predicates (`_is_representable_numeric_order_id`, the
                # negative-id rejection in `place_market_buy`) already
                # treat both as unusable. A rejected row and a queued row
                # sharing that identical sentinel, each with their own
                # distinct valid `order_id` alias, were still keyed by the
                # sentinel and deduplicated together, dropping the queued
                # row (Codex review, PR #1168: "Validate numeric primary
                # IDs before deduplicating"; PR #1169: "Reject non-finite
                # IDs before deduplicating history" -- NaN/inf are among the
                # non-representable values this same check already
                # rejects). The full usable-numeric-ID validation is
                # applied here too, not just the type check.
                if _is_unusable_dedup_id(order_id):
                    order_id = order.get("order_id") if isinstance(order, dict) else None
                    # The FALLBACK secondary alias needs the identical
                    # usable-scalar validation, not just the primary --
                    # two rows with distinct malformed primary ids
                    # (`id=["BAD1"]`, `id=["BAD2"]`) sharing the SAME
                    # malformed secondary `order_id=["SHARED"]` both fell
                    # through to that shared, unvalidated fallback and were
                    # stringified into the identical fabricated dedup key
                    # "['SHARED']", discarding a still-live row even though
                    # neither row's own reference actually collided (Codex
                    # review, PR #1168: "Validate fallback IDs before
                    # pagination deduplication"). Left unusable entirely
                    # (not stringified) rather than deduplicated on
                    # fabricated evidence -- an id-less row is always
                    # yielded, per this loop's own established contract.
                    if _is_unusable_dedup_id(order_id):
                        order_id = None
                if not _is_blank_broker_field(order_id):
                    # Routed through `_normalized_order_id` -- the same
                    # numeric-string canonicalization the durable order-ID
                    # ledger already applies -- rather than a raw
                    # `str(order_id).strip()`, so a broker that repeats an
                    # order at a pagination boundary but changes its
                    # numeric encoding stringifies to the SAME dedup key
                    # regardless of which representation carried it:
                    # `id=123` vs `id=123.0` (a native int vs an integral
                    # float), AND `id=123` vs `id="123.0"` (a native int
                    # vs a numeric-STRING spelling of the same integral
                    # value) -- the float-only canonicalization this
                    # function used to apply inline caught only the first
                    # pair, so `"123"` and `"123.0"` still survived as two
                    # distinct keys and `same_day_ticker_notional_usd()`
                    # summed the same order twice, overstating the
                    # broker-backed baseline and potentially shrinking or
                    # skipping a valid guarded top-up (Codex review, PR
                    # #1168: "Canonicalize numeric IDs before pagination
                    # deduplication"; PR #1169: "Canonicalize numeric-string
                    # IDs during pagination dedup"). Falls back to the
                    # original stringification if normalization somehow
                    # returns nothing usable -- unreachable in practice
                    # since `_is_unusable_dedup_id` above already rejected
                    # every value `_normalized_order_id` would discard.
                    order_id = _normalized_order_id(order_id) or str(order_id).strip()
                    if order_id in seen_order_ids:
                        continue
                    seen_order_ids.add(order_id)
                yield order
            # Checked across EVERY traversed layer, row-bearing layer
            # first: any one of them -- not just the row-bearing layer,
            # and not just the outer settled layer -- can carry the real
            # continuation pointer (Codex review, PR #1168: "Preserve
            # nested continuation pointers with nested orders" / "Carry
            # intervening pagination cursors forward"; PR #1169: "Reuse
            # the requested cursor when checking the outer layer"). The
            # ordinary, single-layer case (`_cursor_layers == [data]`) is
            # completely unaffected.
            #
            # `_requested_cursor` -- the cursor THIS page was actually
            # fetched with -- is captured BEFORE any reassignment below and
            # passed to every `_next_order_cursor` call, never the loop
            # variable itself once it has already been overwritten: an
            # earlier version of this loop reused `cursor`'s own
            # progressively-overwritten value as the `current_cursor`
            # argument to each successive call, so an outer settled
            # layer's bare `"cursor"` echoing the real requested page
            # ("P2") was compared against `None` (an earlier layer's own
            # empty result) instead of "P2" -- misread as a FRESH
            # continuation rather than the echo it is, requesting the same
            # page again and raising "repeated a pagination cursor" (Codex
            # review, PR #1168: "Retain the requested cursor during
            # fallback parsing").
            # Every layer is scanned to completion, not stopped at the
            # first hit: two layers can each carry their OWN genuine
            # (non-echo) continuation pointer that DISAGREES with the
            # other's -- an inner `next_cursor="P3"` alongside an outer
            # `next_cursor="P4"` -- and silently taking whichever layer
            # happened to be checked first let the scan follow P3 while an
            # order sitting on P4 was never reached, proving a ref absent
            # from a truncated view of the real history (Codex review, PR
            # #1168/#1169: "Reject conflicting continuation cursors across
            # wrappers"). Multiple layers landing on the SAME cursor value
            # (one layer's genuine pointer echoed by a pass-through wrapper
            # above it, say) is not a conflict -- only genuinely DISTINCT
            # values are.
            _requested_cursor = cursor
            _cursor_by_layer = [
                (_cursor_layer, _next_order_cursor(_cursor_layer, _requested_cursor))
                for _cursor_layer in _cursor_layers
            ]
            _found_cursors = [_layer_cursor for _, _layer_cursor in _cursor_by_layer if _layer_cursor is not None]
            # Compared across EVERY alias each layer supports (not just the
            # single value `_next_order_cursor` would select for that
            # layer): a conflict can be entirely WITHIN one layer -- see
            # `_all_order_cursor_aliases`'s own docstring -- so the cross-
            # layer conflict set alone missed it (Codex review, PR #1168:
            # "Reject conflicting cursor aliases within one layer").
            _all_layer_cursor_aliases = [
                _alias
                for _cursor_layer in _cursor_layers
                for _alias in _all_order_cursor_aliases(_cursor_layer, _requested_cursor)
            ]
            if len({*_all_layer_cursor_aliases}) > 1:
                raise RuntimeError(
                    f"same-day order lookup for {ticker} returned conflicting pagination cursors "
                    "across traversed layers"
                )
            # Every layer with a pointer `_next_order_cursor` could NOT turn
            # into a usable cursor is checked unconditionally -- not only
            # when `_found_cursors` is empty. A page where one traversed
            # layer reports a genuine `next_cursor="P2"` while ANOTHER
            # independently reports an unusable continuation (`next_cursor:
            # 0`) used to skip this check entirely once P2 was found,
            # following P2 and proving a ref absent even though the other
            # layer explicitly advertised history that could not be
            # followed (Codex review, PR #1168: "Reject unusable cursors
            # even when another layer has a valid one"). A next/next_cursor
            # pointer we could not turn into a usable cursor is "there is
            # more we cannot reach", not "the list ended" -- letting a
            # caller prove absence from a truncated history.
            # The `_has_unusable_next_url_cursor` half of this check runs
            # UNCONDITIONALLY, not only when `_layer_cursor is None`: a
            # layer can have a perfectly usable `next_cursor` alias AND a
            # `next` URL whose own cursor param is missing/blank, and
            # `_next_order_cursor` silently prefers the usable alias,
            # hiding the fact that the SAME layer advertised a second
            # continuation it could not interpret (Codex review, PR
            # #1169: "Reject unusable cursor aliases beside a valid
            # one").
            # The `_has_unusable_direct_cursor_alias` half runs
            # UNCONDITIONALLY too, for the identical reason: a layer's
            # own `next_cursor`/`cursor` field can itself be a malformed
            # (non-string) sentinel masked by a DIFFERENT usable alias on
            # the same layer -- `{"next_cursor": 0, "next":
            # "...?cursor=P2"}` resolves to a usable "P2" without ever
            # surfacing the unreadable `next_cursor: 0` sibling (Codex
            # review, PR #1168: "Reject malformed direct cursors beside
            # usable aliases").
            _unusable_pointer_layers = [
                _layer
                for _layer, _layer_cursor in _cursor_by_layer
                if (_layer_cursor is None and _has_continuation_pointer(_layer))
                or _has_unusable_next_url_cursor(_layer)
                or _has_unusable_direct_cursor_alias(_layer)
            ]
            if _unusable_pointer_layers:
                raise RuntimeError(
                    f"same-day order lookup for {ticker} returned an unusable pagination pointer"
                )
            cursor = _found_cursors[0] if _found_cursors else None
            if cursor is None:
                return
            if cursor in seen_cursors:
                raise RuntimeError(f"same-day order lookup for {ticker} repeated a pagination cursor")
            seen_cursors.add(cursor)
        raise RuntimeError(
            f"same-day order lookup for {ticker} exceeded {_MAX_ORDER_PAGES} pages"
        )

    def _same_day_orders(self, ticker: str, trading_day: date) -> List[Dict[str, Any]]:
        """Return EVERY same-day order for ``ticker``, following pagination.

        ``get_equity_orders`` is a paginated tool: a busy account's same-day
        history spans several pages and the first one alone is not the whole
        story. Reading only page one made every caller here fail OPEN --
        ``same_day_strategy_order_exists`` would report "no order today" and
        authorize a duplicate buy, and the crash-recovery lookup
        ``strategy_order_with_ref_id`` would report the claimed fire order as
        never submitted and release the lease for a second real purchase --
        purely because the matching order sat on page two (Codex review:
        "Page broker history before releasing the fire lease").
        ``scripts/broker_reconciliation.paginate_same_day_orders`` is the
        reference implementation of the cursor protocol; this is the same
        contract narrowed to one symbol, and it fails CLOSED the same way:
        an unfollowable continuation pointer, a cursor loop, or hitting the
        page ceiling raises instead of returning a partial history that
        callers would read as proof of absence.

        The eager form, for callers that genuinely need the complete list.
        Callers searching for ONE order should iterate ``_iter_same_day_orders``
        and stop on the match instead.
        """
        return list(self._iter_same_day_orders(ticker, trading_day))

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        for order in self._iter_same_day_orders(ticker, trading_day):
            if str(order.get("state", "")).lower() in _LIVE_ORDER_STATES:
                # Proving PRESENCE needs no further pages; only the negative
                # answer below depends on having seen the whole history.
                return True
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: Tuple[str, ...]
    ) -> Optional[Dict[str, Any]]:
        """Return the first live same-day order whose client ref id exactly matches one of
        ``ref_id_prefixes``, or ``None`` if no such order exists.

        Exact matching avoids false positives when one ticker symbol is a prefix of another
        (e.g. ``GOOG`` vs ``GOOGL``). The idempotency keys generated by ``_place_broker_orders``
        are fully-qualified exact strings, so a raw ``startswith`` check is unnecessary and
        unsafe.
        """
        for order in self._iter_same_day_orders(ticker, trading_day):
            if str(order.get("state", "")).lower() not in _LIVE_ORDER_STATES:
                continue
            if any(_alias in ref_id_prefixes for _alias in _usable_ref_id_aliases(order)):
                return order
        return None

    def same_day_order_with_ref_id_prefix_any_state(
        self, ticker: str, trading_day: date, ref_id_prefixes: Tuple[str, ...]
    ) -> Optional[Dict[str, Any]]:
        """Same exact-ref-id match as
        :meth:`same_day_strategy_order_exists_with_ref_id_prefix`, but WITHOUT
        the ``_LIVE_ORDER_STATES`` filter -- also returns a genuinely
        terminal (``cancelled``/``rejected``/``expired``/etc.) order.

        The live-only method exists to answer "does an order still occupy
        this idempotency slot" and deliberately excludes a cancelled/
        rejected order (it "never consumed capacity", see
        ``_LIVE_ORDER_STATES``'s own comment). That is the wrong question
        for whole-share reconciliation of an AMBIGUOUS placement: an order
        that partially filled and was THEN cancelled is real, already-
        executed capital the ledger must still record, and the live-only
        method would report it as a clean "no match" indistinguishable from
        one that never reached the broker at all -- silently releasing
        credit for shares that already executed (Codex review: "Reconcile
        partial fills before releasing canceled claims"). Exposes the raw
        order (any state) so the caller can inspect its own
        ``cumulative_quantity``/``state`` directly.

        Iterates with ``require_provable_history=True``, the same posture
        :meth:`strategy_order_with_ref_id` already uses for the same
        recovery-sensitive lookup: an unwired ``orders_fn`` or an
        unrecognizable page is indistinguishable from a genuinely empty
        history, but proves nothing. Every caller of this method is
        whole-share reconciliation, which reads a clean ``None`` as
        permission to restore a stale or placement-failure claim -- if that
        "found nothing" were actually "could not prove anything", an order
        that genuinely reached the broker could have its already-spent
        credit released and reused (Codex review: "Require provable order
        history before releasing claims").
        """
        for order in self._iter_same_day_orders(ticker, trading_day, require_provable_history=True):
            if any(_alias in ref_id_prefixes for _alias in _usable_ref_id_aliases(order)):
                return order
        return None

    def strategy_order_with_ref_id(
        self, ticker: str, trading_day: date, ref_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the matching historical order regardless of its current state.

        Iterates lazily and returns on the first match, so a ref found anywhere
        in the history never trips the page ceiling -- important for a crash
        lease recovered days after its claim, where the query has no upper time
        bound and the newest-first ordering puts the wanted order last.

        Requires a PROVABLE history: this is crash reconciliation, where an
        empty answer is taken as evidence the claimed order was never submitted
        and the reserved balance is released. An unwired ``orders_fn`` or an
        unrecognizable page therefore raises instead of returning ``None``, so
        the caller preserves the lease rather than authorizing a duplicate
        purchase on the strength of a lookup that never happened.
        """
        for order in self._iter_same_day_orders(
            ticker, trading_day, require_provable_history=True
        ):
            if ref_id in _usable_ref_id_aliases(order):
                return order
        return None

    def same_day_ticker_notional_usd(self, ticker: str, trading_day: date) -> float:
        """Sum the dollar notional of every live same-day order for ``ticker``.

        Unlike the two methods above, this is NOT scoped to any particular
        ref-id convention -- it sums every live-state order the broker itself
        reports for this ticker/day, regardless of which stream or executor
        (this roster, a legacy morning_dca.py/midday_dca.py run, or a manual
        fill) placed it. Ground truth for a guarded top-up's incremental
        delta, which must never trust an artifact-derived prior allocation
        that is lower than what the broker actually shows (see
        near_close_dca.evaluate_top_up_guard's KNOWN GAP note).
        """
        total = 0.0
        for order in self._same_day_orders(ticker, trading_day):
            if str(order.get("state", "")).lower() not in _LIVE_ORDER_STATES:
                continue
            raw = order.get("dollar_amount") or order.get("notional") or 0
            amount = _to_float(raw, default=math.nan)
            if not math.isfinite(amount):
                # Fail closed, not skip: the consumer (near_close_dca's guarded
                # top-up) trusts this figure only when it is GREATER than the
                # artifact-derived prior, so a NaN (which compares False) or a
                # dropped unparseable row silently disabled the broker
                # ground-truth cross-check for this ticker instead of blocking
                # it. Its own except path already treats a raise as "cannot
                # prove" (2026-09-07 diagnostic, S:ADP-04).
                raise RuntimeError(
                    f"same-day order for {ticker} reports a non-finite or unparseable notional "
                    f"({_log_safe_repr(raw)}); refusing to sum it"
                )
            total += amount
        return total

    def same_day_ticker_orders(self, ticker: str, trading_day: date) -> List[Dict[str, Any]]:
        """Return every broker row used to reconcile same-day ticker exposure."""
        return list(self._same_day_orders(ticker, trading_day))

    def is_tradable_for_fractional_buy(self, ticker: str) -> bool:
        if self._tradability_fn is None:
            self._refuse_unwired_live_lookup("tradability_fn", f"assume {ticker} is fractional-tradable")
            return True
        data = _unwrap(self._tradability_fn(account_number=self._account, symbols=[ticker]))
        for result in data.get("results") or []:
            if result.get("symbol") != ticker:
                continue
            # `is True`, not truthiness: a string "false" is truthy
            # (2026-09-07 diagnostic, S:ADP-03).
            if result.get("tradeable") is not True or result.get("state") != "active":
                return False
            return str(result.get("fractional_tradability", "")) == "tradable"
        return False

    def is_tradable_for_whole_share_buy(self, ticker: str) -> bool:
        """Same tradeable/active check as :meth:`is_tradable_for_fractional_buy`,
        WITHOUT the ``fractional_tradability`` requirement -- a whole-share
        ticker (e.g. BYDDY) is in that category precisely because it fails
        that requirement, so reusing the fractional check verbatim would
        block every whole-share order outright. Checked unconditionally
        (both scheduled and manual whole-share candidates), unlike
        :meth:`review_whole_share_buy`, which mirrors :meth:`review_market_buy`
        in only running on a manual run (Codex review: "Verify whole-share
        tradability before scheduled placement" -- without this, a scheduled
        run could reach ``place_whole_share_buy`` for an instrument that
        went halted/inactive/non-tradeable while quote data remained
        available, with no pre-placement tradability check at all).
        """
        if self._tradability_fn is None:
            self._refuse_unwired_live_lookup("tradability_fn", f"assume {ticker} is tradeable and active")
            return True
        data = _unwrap(self._tradability_fn(account_number=self._account, symbols=[ticker]))
        # Compared case-insensitively: unlike "active" (schema-enforced
        # uppercase), "whole_share" has no such schema constraint, so a
        # config-authored lowercase entry (e.g. "byddy") reaches this call
        # verbatim. The broker's real API response uses its own canonical
        # (typically uppercase) casing regardless of what was requested, so
        # an exact-match comparison would silently treat every real,
        # tradeable response as "symbol not found" -> not tradable,
        # restoring every fire's deduction instead of ever placing an order
        # (Codex review: "Require canonical symbols for whole-share
        # tickers").
        for result in data.get("results") or []:
            if str(result.get("symbol", "")).upper() != ticker.upper():
                continue
            return result.get("tradeable") is True and result.get("state") == "active"
        return False

    def review_market_buy(self, ticker: str, notional_usd: float) -> Tuple[bool, str]:
        if self._review_fn is None:
            # Fail closed on the live path: a real order must never be placed without a
            # broker-side review. Only the dry-run/simulated path may skip it.
            if self.enable_live_orders:
                return False, "live orders enabled but no review callable wired; refusing to place unreviewed order"
            return True, "no review callable wired; skipping broker-side review (dry-run)"
        response = self._review_fn(
            account_number=self._account,
            symbol=ticker,
            side="buy",
            type="market",
            dollar_amount=f"{notional_usd:.2f}",
        )
        return _interpret_review(response)

    def _refuse_replayed_order_id(self, order_id: Any, *, ticker: str, order: Dict[str, Any]) -> None:
        """Raise if ``order_id`` was already booked by this broker instance.

        A bare ``{id, state}`` response with every identity field omitted is
        accepted by contract: the live-mirror prompts and
        ``dca_runner._placement_failure_reason`` compare symbol, side,
        client reference and size only WHEN PRESENT, because a fresh
        Robinhood placement omits several of them, and requiring them would
        fail every real placement closed. The stale-echo shape that contract
        leaves open is a connector replaying an EARLIER placement's response
        verbatim -- and that response names an order id this instance already
        accepted for a different request, which cannot also describe this
        one. Refused as structurally ambiguous (may have executed) so the
        caller halts and reconciles from history rather than booking the
        same order twice (Copilot review, PR #1394: "Reject placement
        responses missing required identity fields", resolved by this
        replay guard instead of a field requirement).
        """
        if order_id is None:
            return
        key = str(order_id)
        if key not in self._accepted_order_ids:
            return
        logger.error(
            "place_equity_order for %s returned order id %s, which this run already accepted for an earlier request: %s",
            ticker,
            key,
            _artifact_safe_repr(order),
        )
        raise AmbiguousPlacementResponseError(
            f"live placement for {ticker} returned order id {key}, which this run already accepted for an "
            f"earlier request; response={_artifact_safe_repr(order)}",
            order_id=key,
            response_notional_usd=_recovered_broker_notional(order),
            response_may_have_executed=True,
            structurally_ambiguous=True,
        )

    def place_market_buy(self, ticker: str, notional_usd: float, ref_id: str) -> str:
        # `is True`, not truthiness, matching dca_runner.execute_plan's own
        # literal-boolean gate: a passthrough like enable_live_orders="false"
        # is a truthy string and reached real placement here while the runner
        # refused the same value (2026-09-07 diagnostic, C:F11).
        if not (self.enable_live_orders is True and self._place_fn is not None):
            # run_once only calls a live broker's place_market_buy on the non-dry-run path
            # (dry-run uses a DRY-RUN-SIM sentinel without touching the broker). Reaching
            # here with the live gate disabled is a misconfiguration; returning a
            # success-like id would make run_once record a phantom buy and consume budget.
            raise RuntimeError(
                f"place_market_buy called for {ticker} with the live gate disabled "
                f"(enable_live_orders={self.enable_live_orders}, "
                f"place_fn={'set' if self._place_fn else 'None'}); use dry_run=True for simulated runs"
            )
        try:
            response = self._place_fn(
                account_number=self._account,
                symbol=ticker,
                side="buy",
                type="market",
                dollar_amount=f"{notional_usd:.2f}",
                time_in_force=self._time_in_force,
                ref_id=ref_id,
            )
        except OrderPlacementAmbiguousError:
            # A wrapping adapter (e.g. morning_dca._PersistingBrokerAdapter,
            # when the placement response is a raw-input pointer it could
            # not read back) can raise this directly instead of the
            # underlying connector call raising a generic exception --
            # already the right, specific error; pass it through unwrapped
            # rather than nesting it inside another one below.
            raise
        except Exception as exc:  # noqa: BLE001 - the connector call itself
            # (network timeout, connection reset, MCP transport error, ...)
            # gave no broker response at all, unlike every raise below this
            # point, which reacts to a definitive response the broker DID
            # return (a failure envelope, no order id, or a terminal state).
            # Whether this order actually reached and was accepted by the
            # broker is genuinely unknown here -- re-raise as the distinct
            # ambiguous-placement error so the caller records
            # "potentially_submitted" and conservatively reserves this
            # notional instead of treating the ticker as unbought (Codex
            # review, PR #1300 discussion_r3920155064: "Emit the new
            # ambiguous-placement status").
            raise OrderPlacementAmbiguousError(
                f"placement connector call for {ticker} failed with no broker response: {_artifact_safe_repr(exc)}"
            ) from exc
        outer_failure = _response_failure(response)
        if outer_failure is not None:
            # A failure envelope that still carries an inner order id (e.g.
            # {"success": false, "data": {"order": {...}}}) must not be read as success.
            logger.error("place_equity_order for %s reported failure: %s", ticker, _artifact_safe_repr(response))
            # Normalize the complete response before any shape-specific
            # unwrapping. Connectors may place an accepted order below an
            # arbitrary failure/context wrapper rather than ``data`` or
            # ``error``; losing that identity would prevent exact-reference
            # reconciliation and could exclude real spend from cap accounting.
            _response_envelopes = list(_placement_response_dicts(response))
            _response_order_groups = _order_identity_groups(_response_envelopes)
            _response_has_acceptance = _placement_response_has_acceptance_evidence(response)
            _response_traversal_incomplete = any(
                layer.get("_placement_response_incomplete") is True for layer in _response_envelopes
            )
            _response_single_order_id = (
                _response_order_groups[0][0] if len(_response_order_groups) == 1 else None
            )
            _response_structurally_ambiguous = (
                _response_traversal_incomplete or len(_response_order_groups) > 1
            )
            data = _unwrap_seeking_order_evidence(response)
            # A structured "error" value is real order evidence too, not just a
            # message: `_is_error_response` above already treats a truthy "error"
            # as a failure signal "whatever its shape (string, dict, list, ...)",
            # and a connector can put the broker's own order payload there instead
            # of under "data": {"success": false, "error": {"order": {"id":
            # "ORD-NESTED", "state": "queued"}}} names a live queued order
            # SIBLING to (not nested inside) "data", which `_unwrap` never sees --
            # it only ever descends into "data". The order-selection calls below
            # found no order at all, so the ID-less "envelope itself is the
            # evidence" branch proved a clean rejection while a possibly-live
            # order sat outside reconciliation and cap accounting entirely
            # (Codex review, PR #859/#911 round 44: "Preserve nested orders
            # before proving a rejection").
            _failure_error_layer = response.get("error") if isinstance(response, dict) else None
            if not isinstance(_failure_error_layer, dict):
                _failure_error_layer = None
            # Accept the SAME list-mode envelope get_order_fill already reads
            # ({"orders": [...]}), not just the singular {"order": {...}}
            # nesting: a failure shaped as {"success": false, "data":
            # {"orders": [{"id": "ORD", "state": "queued"}]}} left `order`
            # bound to the enclosing `data` mapping, with no id/state/fill
            # of its own to inspect -- so the ID-less branch below (nothing
            # in the row contradicts it) declared it a proven rejection
            # while a nested, possibly-live queued order sat outside
            # deployment and cap accounting (Codex review, PR #859: "Inspect
            # nested order collections before proving rejection"). Only a
            # SINGLE nested order is unambiguous about which order this
            # placement call's response describes; two or more is tracked
            # below and kept out of the proven-rejection reasoning entirely.
            #
            # An EMPTY singular mapping is treated as absent, not as the
            # selected order: {"success": false, "data": {"order": {},
            # "orders": [{"id": "ORD", "state": "queued"}]}} carries a real,
            # live queued order in the "orders" collection, but a bare
            # `isinstance(..., dict)` check accepted the meaningless `{}` as
            # `order` and never even looked at "orders" -- masking the only
            # real evidence in the response and letting a bare `{}` (no id,
            # no state) reach the "envelope itself is the evidence" branch
            # below as a proven rejection (Codex review, PR #859: "Inspect
            # order collections alongside singular order mappings"). A
            # non-empty singular mapping still wins outright, exactly as
            # before -- this only widens what counts as "nothing there".
            # Selection and the "orders" collection's own agreement/conflict
            # check are shared with the success-envelope path further below
            # via _select_singular_order_against_collection -- see that
            # function's own docstring for the full reasoning (Codex review,
            # PR #859: "Inspect order collections alongside singular order
            # mappings", "Require order representations to agree beyond
            # their IDs", "Keep empty singleton order rows unresolved", "Keep
            # malformed singleton order lists unresolved"). Merged with the
            # OUTER response's own "order"/"orders" evidence, not `data`
            # alone -- a response can carry its own top-level "orders"
            # collection sibling to the "data" wrapper that carries the
            # singular "order" (Codex review, PR #859 round 34: "Validate
            # order collections on every envelope layer").
            order, ambiguous_nested_orders = _select_singular_order_against_collection(
                _merged_order_collection_view(data, _failure_error_layer, response)
            )
            if order is None:
                order = data
            if not isinstance(order, dict):
                order = {}
            # Fill evidence attached to an OUTER wrapper -- the raw
            # `response` itself, or the unwrapped `data` envelope -- rather
            # than the selected `order` row is real evidence too:
            # {"success": false, "executed_dollar_amount": "5", "data":
            # {"order": {"state": "rejected"}}} reports a $5 execution
            # sibling to "data", which examining `order` alone never sees.
            # Merged outer-first so the most specific level (`order`) still
            # decides fields like "state", but a field ABSENT from `order`
            # and present only at an outer level still counts as evidence
            # (Codex review, PR #859: "Scan every envelope before proving
            # rejection") -- and, unlike a plain dict spread, a field
            # PRESENT in `order` but BLANK (`null`/empty) does not shadow a
            # genuinely non-blank value the same key carried at an outer
            # level either (Codex review, PR #859: "Preserve outer fill
            # evidence when inner fields are blank").
            # An agreeing collection row (same id, never disagreeing --
            # `_select_singular_order_against_collection` already vetted
            # that before `order` was chosen) supplies FULL evidence, not
            # just a lifecycle alias: `order={"id": "ORD"}` alongside
            # `orders=[{"id": "ORD", "state": "cancelled",
            # "cumulative_quantity": "0"}]` has that row's own zero-fill
            # figure, which `terminal_broker_row_proves_no_order` needs to
            # settle a proven rejection, silently absent from the merge
            # otherwise (Codex review, PR #859 round 40: "Merge agreeing
            # collection fill evidence before classification").
            # `response`/`data`/`_failure_error_layer` are only folded in
            # when their OWN id, if any, is an exact type-and-value match
            # for `order`'s -- exactly like the success-envelope path's
            # identical merge below. An outer `id=1` alongside an inner
            # order `id="1"` is a TYPE mismatch, not a confirmed identity
            # match (`_envelope_layer_shares_order_identity`'s own docstring
            # covers why a normalized-but-not-exact match is unsafe to
            # treat as "this is the same order's own evidence"), and merging
            # that outer layer's fill evidence in unconditionally let an
            # outer `cumulative_quantity="0"` sibling to a differently-typed
            # id settle a proven zero-fill rejection for `order` even though
            # neither layer ever proved they name the SAME order (Codex
            # review, PR #859/#911 round 44/PR #1164: "Gate failure-layer
            # evidence on exact order identity").
            _order_id_for_failure_evidence = _first_usable_order_id_alias(order)
            _evidence_order = _merge_envelope_layers_prefer_innermost_nonblank(
                response
                if _envelope_layer_shares_order_identity(response, _order_id_for_failure_evidence)
                else {},
                data if _envelope_layer_shares_order_identity(data, _order_id_for_failure_evidence) else {},
                _failure_error_layer
                if _envelope_layer_shares_order_identity(_failure_error_layer, _order_id_for_failure_evidence)
                else {},
                order,
                _agreeing_collection_row(
                    order, _merged_order_collection_view(data, _failure_error_layer, response).get("orders")
                )
                or {},
            )
            # A failure envelope can name a SECOND order under a key sibling
            # to "order"/"error" itself, exactly like the success-envelope
            # path's identical check below -- see `_other_order_shaped_
            # rows`'s own docstring. `{"success": false, "data": {"order":
            # {"id": "A", "state": "cancelled", "cumulative_quantity": "0"}},
            # "sibling": {"id": "B", "state": "queued", "cumulative_
            # quantity": "0"}}` has "A" cleanly proven dead while "B" is
            # reported live -- proving the rejection from "A" alone while
            # never even looking at "B" would let a possibly-accepted second
            # order sit outside reconciliation and cap accounting entirely
            # (Codex review, PR #859/#911 round 45: "Inspect every
            # represented order before proving rejection").
            _sibling_rows, _sibling_scan_truncated = _other_order_shaped_rows(
                data, _failure_error_layer, response, order=order
            )
            # `_sibling_scan_truncated` forces this True regardless of what
            # `_sibling_rows` found: a truncated scan proved nothing about
            # whatever lies past its depth cap, so an empty `_sibling_rows`
            # from a truncated scan must read exactly like an unproven
            # sibling would (Codex review, PR #1164: "Fail closed when
            # sibling traversal reaches its depth cap").
            _unresolved_sibling_orders = _sibling_scan_truncated or any(
                not terminal_broker_row_proves_no_order(_sibling) for _sibling in _sibling_rows
            )
            # The broker must have authored the refusal before ANY of it can be
            # called proven -- see _failure_is_broker_authored; a bridge-level
            # transport failure says nothing about what the broker did. `order`
            # -- the SAME row selected above -- is passed too, so a success
            # flag living only in that nested layer still disputes the outer
            # failure (Codex review, PR #859 round 31: "Inspect success flags
            # in the selected order layer"). collection_rows scans the
            # MERGED collection across every layer, not just data's own
            # "orders" -- an agreeing row named only in the OUTER
            # response's own top-level "orders" (sibling to "data")
            # disputing the failure was invisible to a data-scoped scan
            # (Codex review, PR #859 round 35: "Pass outer collection
            # rows into the authorship check").
            envelope_order_id = _first_usable_order_id_alias(order)
            # The SAME sentinel normalization the success-envelope path
            # below already applies to its own `order_id`: a negative
            # number, a whitespace-only string, or anything not a plain
            # str/int is never a usable broker identifier, only a
            # diagnostic placeholder some connectors echo back. `{"success":
            # false, "id": -1, "message": "order refused"}` is truthy
            # (`-1 or ...` never falls through to `order_id`), so this used
            # to carry -1 forward as though it named a real order -- routing
            # a plain refusal with no order evidence at all into the
            # ID-BEARING branch below, where it could not be proven (no
            # state, no fill) and was reported as merely AMBIGUOUS instead
            # of the proven rejection an envelope with nothing else to it
            # actually is (Codex review, PR #859/#911 round 44: "Reject
            # sentinel order IDs on the failure-envelope path too").
            # Reuses the SAME complete validator the success-envelope path
            # below applies to its own `order_id` -- boolean rejection,
            # finite-numeric-overflow rejection (`_is_representable_numeric_
            # order_id`), and acceptance of a finite positive FLOAT id, not
            # just `str`/`int`. This branch's own narrower `(str, int)`-only
            # check discarded a perfectly usable `123.0` float id (turning a
            # reconcilable failed order into a plain, ID-less `RuntimeError`
            # a few lines below) while STILL accepting a boolean or an
            # astronomically large integer sentinel the success path
            # explicitly rejects, routing a plain broker refusal through the
            # ambiguous-placement halt path instead of the proven-rejection
            # one it belongs on (Codex review, PR #1164/#1165: "Reuse the
            # complete order-ID validator for failure envelopes").
            # A finite-overflow id (`10**400`) is not the same absence of
            # evidence a blank/negative/boolean sentinel is: the envelope
            # DID name a positive order id, just one too large to trust --
            # a malformed response, not a broker actively reporting "no
            # order". `{"success": false, "id": 10**400, "message":
            # "timeout"}` used to clear the id and fall straight into the
            # id-less "the envelope itself is the evidence" branch below,
            # proving a clean rejection from a response that may in fact
            # name a real (if unrepresentable) accepted order -- silently
            # dropping it from deployment and cap accounting (Codex
            # review, PR #1168: "Preserve ambiguity for malformed positive
            # order IDs"). Tracked separately so only THIS shape is barred
            # from the id-less proven-rejection reading below; the other
            # sentinel shapes still correctly prove a plain, evidence-free
            # refusal the way they always have. Restricted to NON-negative
            # values: an oversized NEGATIVE id (`id=-(10**400)`) is every
            # bit as unambiguous a no-order sentinel as `-1` is -- it is
            # cleared as a rejection sentinel by the very check below this
            # one -- so flagging it here too barred the id-less branch from
            # an envelope that was never ambiguous to begin with, forcing a
            # plain broker refusal through the unresolved-halt path instead
            # of the proven-rejection one it belongs on (Codex review, PR
            # #1168: "Treat oversized negative IDs as rejection sentinels").
            # A NaN id can never satisfy `envelope_order_id >= 0` -- NaN
            # compares false against everything, including itself -- so it
            # fell through this gate entirely and was accepted as a
            # nonblank, seemingly-nonnegative id one line below despite
            # `_is_representable_numeric_order_id` (which calls
            # `math.isfinite`, itself false for NaN) already saying it is
            # unusable. Unlike a genuinely negative id (a real, comparable
            # "no order" sentinel the check below this one already clears)
            # or an oversized positive one (this branch's own original
            # target), NaN carries no sign to gate on at all -- it must
            # bypass the sign check and go straight to the finiteness one
            # (Codex review, PR #1169: "Reject NaN IDs on failure
            # envelopes").
            _envelope_order_id_is_nan = isinstance(envelope_order_id, float) and math.isnan(envelope_order_id)
            _envelope_order_id_unrepresentable = (
                isinstance(envelope_order_id, (int, float))
                and not isinstance(envelope_order_id, bool)
                and (_envelope_order_id_is_nan or envelope_order_id >= 0)
                and not _is_representable_numeric_order_id(envelope_order_id)
            )
            # Blank/negative detection is done WITHOUT ever calling a bare
            # `str(envelope_order_id)` on a numeric value: for a native int
            # beyond `_is_representable_numeric_order_id`'s own bound (e.g.
            # a native `10**5000`, or its negative counterpart), `str()`
            # itself raises `ValueError` (Python's integer-to-string
            # conversion digit limit) -- and it used to be called TWICE
            # here (for the blank AND the leading-"-" checks) before
            # `_envelope_order_id_unrepresentable` -- computed just above,
            # for exactly this magnitude class -- was ever consulted,
            # aborting this failure-envelope classification instead of
            # cleanly producing the intended ambiguous-placement or
            # proven-rejection result (Codex review, PR #1169: "Check
            # failure-envelope ID bounds before stringifying"). Python's
            # arbitrary-precision `int` comparison (`< 0`) never raises
            # regardless of magnitude, so the negative check for a numeric
            # `envelope_order_id` is done that way instead; only a STRING
            # candidate is still inspected via `str(...).strip()`, which
            # was always safe (a Python string never hits the int-to-str
            # conversion limit).
            _envelope_order_id_is_blank_string = (
                isinstance(envelope_order_id, str) and not envelope_order_id.strip()
            )
            _envelope_order_id_is_negative = (
                isinstance(envelope_order_id, (int, float))
                and not isinstance(envelope_order_id, bool)
                and envelope_order_id < 0
            ) or (isinstance(envelope_order_id, str) and envelope_order_id.strip().startswith("-"))
            if (
                isinstance(envelope_order_id, bool)
                or not isinstance(envelope_order_id, (str, int, float))
                or _envelope_order_id_unrepresentable
                or _envelope_order_id_is_blank_string
                or _envelope_order_id_is_negative
            ):
                envelope_order_id = None
            _order_identity_conflicts = (
                _order_identifier_aliases_conflict(order)
                or _envelope_layers_conflict_with_order(order, response, data, _failure_error_layer)
                or bool(_evidence_order.get("_conflicting_fill_evidence"))
            )
            # Transport/connector failure envelopes can still contain a
            # broker-selected order.  Preserve its usable, unambiguous ID for
            # reconciliation even when the envelope itself is not sufficient
            # proof that the broker authored the failure.
            _carryable_failure_order_id = (
                envelope_order_id
                if envelope_order_id and not _order_identity_conflicts and not ambiguous_nested_orders
                else None
            )
            if (
                _carryable_failure_order_id is None
                and _response_has_acceptance
                and _response_single_order_id is not None
                and not _response_structurally_ambiguous
            ):
                _carryable_failure_order_id = _response_single_order_id
            if _failure_is_broker_authored(
                response,
                order,
                collection_rows=_merged_order_collection_view(data, _failure_error_layer, response).get("orders"),
            ):
                # {"id": "DEAD", "order_id": "LIVE", "state": "rejected"}
                # carries two disagreeing identifier aliases -- picking one
                # (envelope_order_id above always prefers "id") and treating
                # it as authoritative silently discards the OTHER alias's
                # own, possibly-live order from reconciliation and cap
                # accounting entirely, since terminal_broker_row_proves_no_
                # order never looks at either id field itself (Codex review,
                # PR #859 round 27: "Keep conflicting identifier aliases
                # unresolved"). Guards BOTH branches below, not only the
                # id-bearing one: a conflict is not "no id" either, so it
                # must not fall into the envelope-itself-is-evidence
                # reasoning that branch applies. Also covers a conflict
                # ACROSS envelope layers, not just within `order` itself --
                # the outer `response`/`data` naming a different id than
                # the selected `order` row (Codex review, PR #859 round 32:
                # "Compare order identities across envelope layers").
                # An id inside a failure envelope does not make the refusal
                # unprovable, and treating it that way threw away the clearest
                # evidence there is: `{"success": false, "data": {"order":
                # {"id": "ORD", "state": "rejected"}}}` names the order AND its
                # terminal state, and forcing it down the plain-RuntimeError
                # path meant an eventually-consistent history miss turned a
                # settled rejection into an unresolved halt for every remaining
                # candidate (Codex review, PR #859: "Honor terminal proof
                # inside failure envelopes"). Same classifier and same id
                # carriage as the non-envelope terminal path below.
                if not _order_identity_conflicts:
                    # `not ambiguous_nested_orders` gates the ID-LESS branch
                    # below already; this ID-BEARING branch used to skip that
                    # check entirely. `{"id": "OUTER", "state": "rejected",
                    # "orders": [{"id": "A", "state": "queued"}, {"id": "B",
                    # "state": "queued"}]}` sets ambiguous_nested_orders (2+
                    # collection entries -- which order this response
                    # actually describes is unknown) but still carries an id
                    # on the restored outer mapping, so this branch proved a
                    # rejection from it while the response also names two
                    # possibly-live orders the batch never accounted for
                    # (Codex review, PR #859 round 31: "Honor ambiguity
                    # before proving an ID-bearing failure").
                    if (
                        envelope_order_id
                        and not ambiguous_nested_orders
                        and not _unresolved_sibling_orders
                        and not _response_has_acceptance
                    ):
                        if terminal_broker_row_proves_no_order(_evidence_order):
                            raise FractionalPlacementRejectedError(
                                f"live placement for {ticker} was explicitly rejected: {outer_failure}",
                                order_id=envelope_order_id,
                            )
                    # No id: the envelope itself is the evidence, provided
                    # nothing in the row contradicts it -- no fill, and no
                    # working state -- and provided the envelope did not
                    # name several orders this call's response could
                    # ambiguously mean.
                    elif (
                        not ambiguous_nested_orders
                        and not _unresolved_sibling_orders
                        and not _response_has_acceptance
                        and not _envelope_order_id_unrepresentable
                        and not _reconciled_order_may_have_executed(_evidence_order)
                        and _failure_envelope_state_permits_proven_rejection(_evidence_order)
                    ):
                        raise FractionalPlacementRejectedError(
                            f"live placement for {ticker} was explicitly rejected: {outer_failure}"
                        )
                    # Reaching HERE means this exact id could not be proven
                    # dead by either branch above -- an ID-BEARING WORKING
                    # order (`{"success": false, "data": {"order": {"id":
                    # "ORD", "state": "queued"}}}`), or an ID-less envelope
                    # whose own state/fill evidence did not permit a clean
                    # rejection. The plain `RuntimeError` below carries no
                    # id at all, and failure responses are never appended
                    # to the raw-placement buffer either, so a possibly-
                    # live `ORD` this call already knows about disappeared
                    # from durable recovery evidence the moment history has
                    # not indexed it yet -- even though the batch correctly
                    # halts either way (Codex review, PR #859 round 40:
                    # "Preserve IDs from unresolved failure envelopes").
                    if envelope_order_id:
                        _carryable_failure_order_id = envelope_order_id
            if _carryable_failure_order_id:
                raise AmbiguousPlacementResponseError(
                    f"live placement for {ticker} failed: {outer_failure}",
                    order_id=_carryable_failure_order_id,
                    # The success-envelope path's identical raise (below)
                    # already carries this; a broker-authored failure
                    # envelope naming an order with positive or unreadable
                    # fill evidence is exactly as real: `success=false`
                    # with `order={"id": "ORD", "state": "rejected",
                    # "executed_notional": "100"}` used to raise with
                    # `response_notional_usd=None`, so an immediate history
                    # row showing the same id merely queued for the
                    # approved amount was accepted, and the $100 execution
                    # this exact response already proved vanished (Codex
                    # review, PR #859/#911 round 43: "Carry failure-
                    # envelope fill evidence into reconciliation").
                    response_notional_usd=_recovered_broker_notional(_evidence_order),
                    # Mirrors the success-envelope path's own
                    # `structurally_ambiguous` computation: `order_id`
                    # here names only ONE identity, and an outer identity
                    # conflict, a disputed nested collection, or an
                    # unresolved sibling order named under some other key
                    # each mean that ONE identity is not the whole of this
                    # response's ambiguity. Omitting this let a caller's
                    # exact-ref history match on `_carryable_failure_
                    # order_id` alone resolve a response that also named a
                    # second, still-unproven order -- exactly the gap the
                    # success path's own marker exists to close (Codex
                    # review, PR #1164: "Preserve sibling-order ambiguity
                    # through reconciliation").
                    structurally_ambiguous=(
                        _order_identity_conflicts
                        or ambiguous_nested_orders
                        or _unresolved_sibling_orders
                        or _response_structurally_ambiguous
                    ),
                )
            if (
                ambiguous_nested_orders
                or _unresolved_sibling_orders
                or _order_identity_conflicts
                or _response_has_acceptance
                or _response_structurally_ambiguous
            ):
                # No usable envelope-level id survived above, but the
                # response still names 2+ conflicting candidate orders --
                # either through the "orders" collection itself
                # (`success=false` with queued orders "A" and "B" and no
                # id anywhere at the envelope level), through an unproven
                # order named under some OTHER sibling key, or through the
                # outer envelope and the selected order themselves naming
                # DIFFERENT identities (outer `id="B"` around `order.id=
                # "A"`) -- `_order_identity_conflicts` clears the carryable
                # id above precisely because no single id is safe to
                # trust, but was omitted from THIS final branch's own
                # condition, so that exact shape fell through to the bare
                # `RuntimeError` below with neither an id nor the
                # ambiguity marker (Codex review, PR #1169: "Preserve
                # structural ambiguity for envelope ID conflicts" -- the
                # fresh-evidence follow-up to "Preserve sibling-order
                # ambiguity through reconciliation"). A bare `RuntimeError`
                # here carries no `structurally_ambiguous` marker, so the
                # caller's own downgrade (which exists precisely to stop
                # a coincidental history match on ONE disputed identity
                # from resolving the response's actual ambiguity) never
                # even applies -- `isinstance(exc,
                # AmbiguousPlacementResponseError)` is False for a plain
                # RuntimeError, so an exact-ref history match for "A"
                # alone was accepted outright while "B" disappeared from
                # reconciliation and cap accounting entirely (Codex
                # review, PR #859/#911 round 43: "Preserve structural
                # ambiguity from failure envelopes"). No id is carried --
                # none is usable -- but the structural-ambiguity flag
                # alone is enough for the caller's downgrade to fire.
                raise AmbiguousPlacementResponseError(
                    f"live placement for {ticker} failed: {outer_failure}",
                    structurally_ambiguous=True,
                )
            raise RuntimeError(f"live placement for {ticker} failed: {outer_failure}")
        data = _unwrap_seeking_order_evidence(response)
        # Selection and the "orders" collection's own agreement/conflict
        # check are shared with the failure-envelope path above via
        # _select_singular_order_against_collection -- this success path
        # used to trust `data.get("order")` outright with no regard for a
        # conflicting "orders" collection alongside it: {"order": {"id":
        # "ORD", "state": "rejected"}, "orders": [{"id": "LIVE", "state":
        # "queued"}]} names a live, different order the singular row alone
        # masked entirely, and the terminal checks below proved a
        # rejection from it while that live order stayed unresolved (Codex
        # review, PR #859: "Validate order collections on successful
        # envelopes"). Merged with the OUTER response's own "order"/
        # "orders" evidence too, the same way the failure-envelope path
        # above does (Codex review, PR #859 round 34: "Validate order
        # collections on every envelope layer").
        order, ambiguous_nested_orders = _select_singular_order_against_collection(
            _merged_order_collection_view(data, response)
        )
        if order is None:
            order = data
        order_id = _first_usable_order_id_alias(order)
        # A connector sentinel -- `id=true` (a bool, which `isinstance(x,
        # int)` alone would NOT catch, since bool subclasses int in Python),
        # `id=-1`, or a whitespace-only string -- is truthy, so this used to
        # accept it as a real broker identifier. `{"order": {"id": true,
        # "state": "queued"}}` then became order id `"True"`, and the caller
        # marked the placement accepted and consumed budget against an id
        # that can never actually be queried back from the broker (Codex
        # review, PR #859/#911 round 45: "Reject boolean order identifiers").
        # An astronomically large int (e.g. `10**1000`) passes every check
        # above -- see `_is_representable_numeric_order_id`'s own docstring
        # (Codex review, PR #1164: "Reject oversized numeric placement
        # IDs"). A finite positive FLOAT id (`{"id": 123.0, "state":
        # "queued"}`) is a previously-supported shape too -- the removed
        # `dca_runner._is_usable_order_id` accepted it, and
        # `near_close_dca._is_usable_reconciled_order_id` still does --
        # but this validation's own `isinstance(order_id, (str, int))`
        # gate rejected it outright (a plain `float` is neither), turning
        # an otherwise-accepted placement into "no order id" and routing
        # a live order onto the unresolved-placement halt path (Codex
        # review, PR #1165: "Preserve finite positive float order IDs").
        if (
            isinstance(order_id, bool)
            or not isinstance(order_id, (str, int, float))
            # Checked BEFORE the bare `str(order_id).strip()` below, not
            # after: for a native (not string-encoded) int/float beyond
            # `_is_representable_numeric_order_id`'s own bound (e.g. a
            # native `10**5000`), `str(order_id)` itself raises `ValueError`
            # (Python's integer-to-string conversion digit limit) before
            # ever reaching that later check -- aborting this validation
            # with an unhandled exception instead of cleanly rejecting the
            # oversized primary and letting the caller's secondary-alias
            # fallback run, on an already-broker-accepted placement (Codex
            # review, PR #1169: "Check numeric representability before
            # stringifying placement IDs").
            or (isinstance(order_id, (int, float)) and not _is_representable_numeric_order_id(order_id))
            or not str(order_id).strip()
            or (isinstance(order_id, str) and _is_stringified_non_positive_number(order_id.strip()))
            # A serialized non-finite sentinel ("NaN", "Infinity", "-inf")
            # is truthy, doesn't start with "-", and isn't caught by the
            # stringified-non-positive check above (a NaN comparison
            # against 0 is always False, and positive infinity isn't <= 0
            # either) -- so this validation accepted it as a real broker
            # identifier and place_market_buy()/place_whole_share_buy()
            # returned "NaN" as a proven submission, even though fill
            # lookup and recovery can never identify an order by that id
            # and `_normalized_order_id` later discards it from durable
            # provenance anyway. Mirrors the identical check the history
            # and reference-ID paths already apply (Codex review, PR
            # #1169: "Reject serialized non-finite placement IDs").
            or (isinstance(order_id, str) and _is_stringified_non_finite_number(order_id.strip()))
            or (isinstance(order_id, (int, float)) and order_id <= 0)
        ):
            # A stringified non-positive placement id ("0", not just a
            # leading "-") passed the checks above: the durable normalizer
            # (`near_close_dca._normalized_order_id`) and the reference-ID
            # validator (`_first_usable_ref_id_alias`) both already reject
            # "0", but this validation only ever rejected a leading "-", so
            # `place_market_buy()` returned "0" as a real broker id and the
            # caller recorded the requested notional as if submission were
            # proven, leaving the attempt without usable provenance for
            # later reconciliation (Codex review, PR #1168: "Reject
            # stringified zero placement IDs"). A native non-positive
            # int/float sentinel is rejected outright the same way, for the
            # identical reason this module's other order-id validators
            # already do.
            order_id = None
        if order_id is None:
            # `_first_usable_order_id_alias` only fell through to
            # "order_id" when "id" was blank/falsey -- a truthy but
            # UNUSABLE primary (a boolean, a non-scalar, a negative or
            # non-finite number) was still selected and just cleared
            # above, discarding a perfectly usable secondary alias
            # sitting right beside it: `{"id": true, "order_id": "REAL",
            # "state": "queued"}` halted an already-accepted order into
            # ambiguous-placement recovery instead of returning "REAL"
            # (Codex review, PR #1169: "Fall through unusable primary
            # placement IDs"). Retried through the identical validation,
            # not accepted unconditionally.
            _secondary_order_id = order.get("order_id")
            if not _is_unusable_live_placement_order_id(_secondary_order_id):
                order_id = _secondary_order_id
        # {"id": "DEAD", "order_id": "LIVE", "state": "rejected"} carries
        # two disagreeing identifier aliases -- picking one (order_id above
        # always prefers "id") and treating it as authoritative silently
        # discards the OTHER alias's own, possibly-live order (Codex
        # review, PR #859 round 27: "Keep conflicting identifier aliases
        # unresolved"). Gates only the proven-rejection raise below that
        # carries this id as authoritative evidence; the still-unproven
        # AmbiguousPlacementResponseError path further down is already the
        # conservative, halting reading and needs no such gate. Also
        # covers a conflict ACROSS envelope layers, not just within
        # `order` itself -- the outer `response`/`data` naming a
        # different id than the selected `order` row, exactly like the
        # failure-envelope path above (Codex review, PR #859 round 32:
        # "Compare order identities across envelope layers").
        _order_identity_conflicts = (
            _order_identifier_aliases_conflict(order)
            or _envelope_layers_conflict_with_order(order, response, data)
        )
        # Fill evidence attached to an OUTER wrapper -- the raw `response`
        # itself, or the unwrapped `data` envelope -- rather than the
        # selected `order` row is real evidence here too, exactly like the
        # failure-envelope path above: {"success": true, "executed_dollar_
        # amount": "5", "data": {"order": {"id": "ORD", "state":
        # "rejected"}}} reports a $5 execution sibling to "data", which
        # examining only `order` never sees, letting the terminal checks
        # below prove a rejection while that $5 stayed outside deployment
        # and cap accounting (Codex review, PR #859: "Inspect outer
        # evidence on successful response envelopes"). `_response_failure`
        # returning None (this is not a failure envelope) says nothing
        # about whether the ORDER itself is terminal -- a broker can answer
        # "success" at the transport level while its own nested order row
        # reports a rejection.
        # See the identical merge in the failure-envelope branch above for
        # why the agreeing collection row's FULL evidence -- not just its
        # lifecycle alias -- is folded in here too (Codex review, PR #859
        # round 40: "Merge agreeing collection fill evidence before
        # classification"). `response` and `data` are only folded in when
        # their OWN id, if any, is an exact type-and-value match for
        # `order`'s -- see `_envelope_layer_shares_order_identity`'s own
        # docstring for why a normalized-but-not-exact match (an outer
        # integer id next to an inner string id naming the same digits) is
        # not safe to treat as "this is the same order's own evidence"
        # (Codex review, PR #859/#911 round 44: "Require an exact type
        # match before sharing outer fill evidence").
        # `_first_usable_order_id_alias`, not a bare `order.get("id")`
        # falling back only on exact `None`: a whitespace-only "id"
        # alongside a real "order_id" (`{"id": "   ", "order_id": "A",
        # ...}`) is not `None`, so the old fallback never engaged, and
        # `_envelope_layer_shares_order_identity` then compared every
        # wrapper layer's own real id against the blank placeholder
        # instead of against "A" -- a comparison that can never match, so
        # BOTH wrapper layers were excluded from evidence-merging even
        # though `order_id`'s validated, usable alias ("A") was one field
        # over (Codex review, PR #1164: "Use the usable ID alias when
        # merging envelope evidence").
        _order_id_for_evidence = _first_usable_order_id_alias(order)
        _evidence_order = _merge_envelope_layers_prefer_innermost_nonblank(
            response if _envelope_layer_shares_order_identity(response, _order_id_for_evidence) else {},
            data if _envelope_layer_shares_order_identity(data, _order_id_for_evidence) else {},
            order,
            _agreeing_collection_row(
                order, _merged_order_collection_view(data, response).get("orders")
            )
            or {},
        )
        # Two non-blank fill values for the same field disagreeing across
        # envelope layers cannot be reduced to either value safely.  Keep the
        # placement unresolved so reconciliation sees the contradictory
        # broker evidence instead of recording a proven rejection.
        _order_identity_conflicts = _order_identity_conflicts or bool(
            _evidence_order.get("_conflicting_fill_evidence")
        )
        # A response can name a SECOND order under a key sibling to "order"
        # itself -- see `_other_order_shaped_rows`'s own docstring. Every
        # such sibling must independently prove its own zero-fill/terminal
        # status before this call may prove ANYTHING from this response:
        # `{"order": {"id": "A", "state": "cancelled", "cumulative_
        # quantity": "0"}, "sibling": {"id": "B", "state": "queued",
        # "cumulative_quantity": "0"}}` has "A" cleanly proven dead while
        # "B" is reported live -- proving the response from "A" alone while
        # never even looking at "B" would let a possibly-accepted second
        # order sit outside reconciliation and cap accounting entirely
        # (Codex review, PR #859/#911 round 44: "Require every named
        # sibling order to prove its own terminal zero fill"). Each
        # sibling is judged on ITS OWN evidence only -- not merged with
        # `response`'s outer fields the way `order`'s own evidence is --
        # since an anonymous outer fill figure is exactly as unattributable
        # between two distinct named orders as it would be between any two
        # strangers' claims.
        _sibling_rows, _sibling_scan_truncated = _other_order_shaped_rows(data, response, order=order)
        # `_sibling_scan_truncated` forces this True the same way the
        # failure-envelope path's identical check above does -- see that
        # one's own comment (Codex review, PR #1164: "Fail closed when
        # sibling traversal reaches its depth cap").
        _unresolved_sibling_orders = _sibling_scan_truncated or any(
            not terminal_broker_row_proves_no_order(_sibling) for _sibling in _sibling_rows
        )
        if not order_id:
            # Fail fast: run_once treats any returned id as a submitted order and records
            # the notional against daily/weekly budgets. Returning "" would book a phantom
            # buy with no order to reconcile, so raise instead of reporting false success.
            logger.error("place_equity_order returned no order id for %s: %s", ticker, _artifact_safe_repr(data))
            state = str(order.get("state") or order.get("status") or "").lower()
            # The SHARED classifier decides, not a local state list: it applies
            # _reconcile_ambiguous_fractional_placement's own rule, so fill
            # evidence outranks the "rejected" label, a working state is never
            # a refusal, and a cancelled/expired row with an explicit zero fill
            # is recognized as the proven no-order it is instead of halting the
            # batch over an eventually-consistent history miss (Codex review,
            # PR #859: "Recognize every proven terminal no-order response").
            # Gated on `not ambiguous_nested_orders` too, the same guard the
            # failure-envelope path applies to its own "envelope itself is
            # the evidence" reading -- a conflicting "orders" collection
            # names a possibly-live order this classifier never gets to see
            # once `order` selection falls back to `ambiguous_nested_orders`
            # (Codex review, PR #859: "Validate order collections on
            # successful envelopes").
            # `_order_identity_conflicts` is computed above (it also covers
            # a conflict on `order` ITSELF -- an id-less `order` cannot
            # conflict with itself, but the failure-envelope branch's
            # identical id-less gate already includes this same check, and
            # this one must not be the odd one out): an outer layer
            # reporting two disagreeing identifier aliases of its own
            # (`{"id": "DEAD", "order_id": "LIVE", ...}`) is a possibly-
            # live order this ID-less branch's own state/fill reasoning
            # never gets to see, and proving a rejection from `_evidence_
            # order` while ignoring it discarded both conflicting
            # identities entirely (Codex review, PR #859 round 41:
            # "Validate envelope IDs without a selected-order ID").
            # `_unresolved_sibling_orders` too: a second, unproven order
            # named under a sibling key is exactly as disqualifying here as
            # it is for the ID-bearing terminal gate below -- see that
            # flag's own computation above for the full reasoning (Codex
            # review, PR #859/#911 round 44: "Require every named sibling
            # order to prove its own terminal zero fill").
            if (
                not ambiguous_nested_orders
                and not _order_identity_conflicts
                and not _unresolved_sibling_orders
                and terminal_broker_row_proves_no_order(_evidence_order)
            ):
                raise FractionalPlacementRejectedError(
                    f"live placement for {ticker} was explicitly rejected in state {state}"
                )
            if ambiguous_nested_orders or _order_identity_conflicts or _unresolved_sibling_orders:
                # The proven-rejection gate just above already excludes this
                # branch when any of these three hold, but a plain
                # `RuntimeError` here still lost the SAME ambiguity signal
                # the failure-envelope path's own id-less branch already
                # preserves: no `structurally_ambiguous` marker means the
                # caller's downgrade -- which exists precisely to stop an
                # exact-ref history match on ONE disputed identity from
                # resolving the response's actual ambiguity -- never even
                # applies (`isinstance(exc, AmbiguousPlacementResponseError)`
                # is False for a plain `RuntimeError`), so a match for one
                # named order was accepted outright while another queued
                # order the SAME response also names disappeared from
                # reconciliation and cap accounting entirely (Codex review,
                # PR #1164: "Preserve ambiguity for ID-less success
                # responses").
                raise AmbiguousPlacementResponseError(
                    f"live placement for {ticker} returned no order id; response={_artifact_safe_repr(data)}",
                    structurally_ambiguous=True,
                )
            raise RuntimeError(f"live placement for {ticker} returned no order id; response={_artifact_safe_repr(data)}")
        # Falls back to "status" too, the SAME alias the ID-LESS branch just
        # above and terminal_broker_row_proves_no_order itself already read:
        # an ID-bearing response reporting its terminal lifecycle only
        # through "status" (e.g. {"id": "ORD", "status": "rejected"}) read
        # as an empty state here, matched NEITHER terminal-state gate below,
        # and fell through to reporting a successful submission -- booking
        # the full requested notional for an order the broker had actually
        # rejected (Codex review, PR #859 round 28: "Normalize status
        # before accepting an ID-bearing response").
        #
        # STRIPPED, not just lowered: the shared classifier
        # (terminal_broker_row_proves_no_order) normalizes with
        # `.strip().lower()`, but this local `state` -- used for the
        # membership test just below, not merely a log message -- did not,
        # so {"id": "ORD", "state": " rejected ", "cumulative_quantity":
        # "0"} matched neither terminal gate and fell through to a
        # successful submission despite a proven terminal rejection (Codex
        # review, PR #859 round 29: "Strip lifecycle state before terminal
        # routing").
        #
        # Falls back to an AGREEING COLLECTION ROW's own lifecycle alias,
        # too, when `order` itself carries neither: `order={"id": "ORD"}`
        # alongside `orders=[{"id": "ORD", "state": "rejected"}]` has the
        # selector treat the two as agreeing -- a lifecycle field present
        # on only one side is not the CONTRADICTION `_order_rows_
        # contradict` checks for -- but this line then read only the
        # state-less singular row, found nothing, matched neither terminal
        # gate below, and returned "ORD" as a successful submission even
        # though the identical order's OTHER representation in the same
        # collection reports it was rejected (Codex review, PR #859 round
        # 36: "Honor lifecycle states from agreeing collection rows").
        # Only consulted when `order` has no alias of its own -- a
        # DIFFERING alias on `order` itself is a genuine contradiction the
        # selector above already resolves into `ambiguous_nested_orders`.
        #
        # `_collection_lifecycle_conflict` is the SEPARATE signal that
        # helper's own aggregation can surface: TWO OR MORE same-id
        # collection rows disagreeing with EACH OTHER (one `queued`, one
        # `rejected`) is not "nothing to read" -- reading only the FIRST
        # value found made the verdict depend on collection order, and a
        # state-less singular row with such a collection returns `None`
        # from that helper either way, which the `or ""` chain below
        # cannot distinguish from "no matching rows at all". OR'd into
        # the ambiguous-raise gate below, the same way `_lifecycle_alias_
        # conflict` and `_order_disputes_success` already are (Codex
        # review, PR #859 round 38: "Require collection lifecycle rows to
        # agree").
        _agreeing_collection_lifecycle, _collection_lifecycle_conflict = (
            _agreeing_collection_row_lifecycle_alias(
                order, _merged_order_collection_view(data, response).get("orders")
            )
        )
        # Falls back to `_evidence_order` -- the merged response/data/order
        # view computed above, which already resolves an outer envelope
        # layer's own lifecycle when the selected order has none of its
        # own -- as the LAST resort: `{"success": true, "state":
        # "rejected", "cumulative_quantity": "0", "data": {"order": {"id":
        # "ORD"}}}` has no state/status anywhere on `order` or an agreeing
        # collection row, yet the outer envelope's own "rejected" (with a
        # proven zero fill) is real terminal evidence `_evidence_order`
        # already carries. Reading only `order`/collection here matched
        # neither terminal gate below and returned "ORD" as a successful
        # submission despite that outer rejection (Codex review, PR #859
        # round 38: "Honor outer lifecycle evidence before accepting
        # orders").
        # Each candidate is normalized (via `_normalize_broker_lifecycle_
        # alias`, which treats a blank/whitespace-only value as `None`)
        # BEFORE the fallback chain picks one, not after: a raw
        # `order.get("state") or order.get("status") or ...` picks
        # whichever candidate is merely non-empty-string-truthy FIRST, so
        # an ID-bearing response with a whitespace placeholder in "state"
        # and the real terminal value in "status" -- `{"id": "REAL-1",
        # "state": "   ", "status": "rejected", "cumulative_quantity":
        # "0"}` -- selected the blank "state" over "status", and the
        # SUBSEQUENT `.strip().lower()` on that already-selected blank
        # collapsed to `""`, matching neither terminal-state gate below
        # and returning "REAL-1" as a successful submission despite the
        # broker's own rejection (Codex review, PR #1164: "Normalize
        # lifecycle aliases before terminal routing").
        state = (
            _normalize_broker_lifecycle_alias(order.get("state"))
            or _normalize_broker_lifecycle_alias(order.get("status"))
            or _normalize_broker_lifecycle_alias(_agreeing_collection_lifecycle)
            or _normalize_broker_lifecycle_alias(_evidence_order.get("state"))
            or _normalize_broker_lifecycle_alias(_evidence_order.get("status"))
            or ""
        )
        # The SAME terminal-state set terminal_broker_row_proves_no_order
        # itself recognizes, not a narrower local list: this hard-coded set
        # omitted "expired", so an ID-bearing {"state": "expired",
        # "cumulative_quantity": "0"} response -- proven zero-fill by the
        # shared classifier -- matched NEITHER this gate NOR the one below
        # it and fell all the way through to `return str(order_id)`,
        # reporting a successful submission and booking the full requested
        # notional for an order the broker proved never executed (Codex
        # review, PR #859: "Route expired placements through the terminal
        # classifier").
        #
        # `state` above always prefers the "state" alias over "status" --
        # correct when only one is present, but when they DISAGREE and the
        # preferred one happens to be a WORKING alias (e.g. {"id": "ORD",
        # "state": "queued", "status": "rejected"}), `state` reads as
        # "queued", never matches _TERMINAL_BROKER_ORDER_STATES, and
        # terminal_broker_row_proves_no_order -- which DOES already detect
        # this exact conflict -- is never even called, so the row fell all
        # the way through to a successful submission despite its own
        # contradictory rejection marker (Codex review, PR #859 round 29:
        # "Keep working rows with conflicting lifecycle aliases
        # unresolved"). Checked independently of which alias `state`
        # picked, so it also covers the OPPOSITE ordering the terminal
        # gate below already handled via terminal_broker_row_proves_no_
        # order's own internal check.
        _lifecycle_alias_conflict = _order_lifecycle_aliases_conflict(_evidence_order)
        # `_response_failure` (computed above as `outer_failure`, already
        # None here or this would be the failure-envelope branch) checks
        # only the outer `response` and unwrapped `data` mappings -- never
        # the SELECTED order layer. `{"success": true, "data": {"order":
        # {"id": "ORD", "state": "queued", "success": false}}}` has the
        # order row's own success flag disputing the outer envelope's
        # claim of success, but nothing here read it before falling
        # through to `return str(order_id)`, booking the requested
        # notional despite the order layer itself disputing acceptance
        # (Codex review, PR #859 round 34: "Honor failure flags inside
        # successful order envelopes"). Also checks agreeing collection
        # rows for the SAME order id, not just the selected row itself
        # (Codex review, PR #859 round 35: "Inspect failure flags in
        # agreeing collection rows").
        _order_disputes_success = (
            order.get("success") is False
            or order.get("ok") is False
            or _is_error_response(order) is not None
            or _agreeing_collection_row_disputes_success(
                order, _merged_order_collection_view(data, response).get("orders")
            )
        )
        # Identity FIRST, before any terminal-state interpretation below: a
        # stale "rejected" response naming another symbol, side, client
        # reference or size was otherwise raised as a definitive rejection
        # of THIS ticker (isolated, batch continues, ticker treated as
        # unbought) while the broker may well hold this ticker's own order.
        # A mismatch is structurally ambiguous whatever lifecycle state it
        # reports, so the caller reconciles from history instead of trusting
        # it either way (Copilot review, PR #1394: "Validate identity before
        # interpreting terminal responses"; 2026-09-07 diagnostic, S:ADP-01).
        identity_mismatch = _placement_response_identity_mismatch(
            order,
            ticker=ticker,
            ref_id=ref_id,
            notional_usd=notional_usd,
            require_identity=self.require_placement_identity,
        )
        if identity_mismatch is not None:
            logger.error(
                "place_equity_order for %s returned an order that does not describe this request (%s): %s",
                ticker,
                identity_mismatch,
                _artifact_safe_repr(order),
            )
            raise AmbiguousPlacementResponseError(
                f"live placement for {ticker} returned an order that does not describe this request: "
                f"{identity_mismatch}; response={_artifact_safe_repr(order)}",
                order_id=str(order_id),
                response_notional_usd=_recovered_broker_notional(order),
                response_may_have_executed=True,
                structurally_ambiguous=True,
            )
        self._refuse_replayed_order_id(order_id, ticker=ticker, order=order)
        if (
            state in _TERMINAL_BROKER_ORDER_STATES
            and not ambiguous_nested_orders
            and not _order_identity_conflicts
            and not _order_disputes_success
            and not _collection_lifecycle_conflict
            and not _unresolved_sibling_orders
            and terminal_broker_row_proves_no_order(_evidence_order)
        ):
            # Every declined state, plus a cancelled/voided/expired one
            # whose executed quantity is an explicit zero -- the response
            # itself settles the question, so the batch may continue past
            # this ticker.
            #
            # The id travels WITH the refusal. This branch is reached only with
            # an order_id in hand, the caller skips history reconciliation on
            # this exception, and a proven-dead order is still an order an
            # operator may need to look up -- so the artifact keeps the broker's
            # own identifier rather than a bare null (Codex review, PR #859:
            # "Preserve the terminal order ID from proven rejections").
            raise FractionalPlacementRejectedError(
                f"live placement for {ticker} was explicitly rejected in terminal state {state}",
                order_id=order_id,
            )
        if (
            state in _TERMINAL_BROKER_ORDER_STATES
            or _lifecycle_alias_conflict
            or _order_identity_conflicts
            or ambiguous_nested_orders
            or _order_disputes_success
            or _collection_lifecycle_conflict
            or _unresolved_sibling_orders
        ):
            # An id in a terminal-failure state means no live order exists; reporting it as
            # submitted would consume budget and block a retry, so raise instead.
            #
            # Two shapes reach this branch and are told apart below:
            #
            # 1. The row is terminal but not PROVEN zero-fill (it reported a
            #    positive or unreadable fill), with no other identity/collection
            #    conflict -- unlike a genuinely ambiguous placement failure (e.g.
            #    a connector timeout, after which the broker may have silently
            #    accepted the order), this response DOES name one single,
            #    terminal order; it merely can't be proven a clean zero-fill
            #    rejection. That is exactly the case the distinct
            #    MarketBuyPlacementTerminalError exists for -- raising it lets a
            #    multi-ticker batch caller catch it and safely continue to the
            #    next candidate instead of halting the whole batch (Codex
            #    review, PR #688: "Continue after definitive broker rejections").
            #
            # 2. The response ALSO carries a structural conflict -- disagreeing
            #    lifecycle aliases, disagreeing order identities, 2+ candidate
            #    orders in a nested collection, an order row disputing the
            #    envelope's own success claim, or an unresolved sibling order
            #    named under some other key -- so it is not just terminal, it
            #    is genuinely ambiguous about WHICH order (or how many) this
            #    call actually placed. Those cases keep raising the richer
            #    AmbiguousPlacementResponseError so the caller preserves every
            #    identity and fill figure this response names, instead of
            #    collapsing them onto the one order MarketBuyPlacementTerminalError
            #    would carry (Codex review, PR #859/#911/#1164: "Preserve
            #    execution evidence / structural ambiguity in terminal
            #    placement responses").
            #
            # `_lifecycle_alias_conflict` covers a WORKING-preferred,
            # terminal-contradicted row that never reaches `state in
            # _TERMINAL_BROKER_ORDER_STATES` at all (see the comment where
            # it is computed above). `_order_identity_conflicts` was, until
            # this fix, checked ONLY inside the terminal-state branch just
            # above -- so a WORKING row with two disagreeing identifiers
            # ({"id": "DEAD", "order_id": "LIVE", "state": "queued"}) never
            # reached ANY conflict guard at all and fell through to `return
            # str(order_id)`, reporting "DEAD" as a successful submission
            # and discarding "LIVE"'s own, possibly-real order entirely
            # (Codex review, PR #859 round 29: "Reject identifier conflicts
            # before accepting working orders"). Both are, like a terminal-
            # but-unproven row, evidence this call cannot resolve on its
            # own -- unresolved, not silently accepted.
            #
            # `ambiguous_nested_orders` was, until this fix, checked only by
            # the terminal-proven branch above -- gated on the fallback
            # STATE being nonterminal, a WORKING order whose singular and
            # collection rows conflict (2+ distinct candidates) fell
            # through both gates entirely: `{"success": true, "id": "OUT",
            # "state": "queued", "order": {"id": "A", "state": "queued"},
            # "orders": [{"id": "B", "state": "queued"}]}` returned "OUT"
            # as a successful submission while the response's own "A"/"B"
            # representations sat outside reconciliation and cap
            # accounting entirely (Codex review, PR #859 round 33: "Keep
            # ambiguous collections unresolved for working orders").
            _structurally_ambiguous = bool(
                _lifecycle_alias_conflict
                or _order_identity_conflicts
                or ambiguous_nested_orders
                or _order_disputes_success
                or _collection_lifecycle_conflict
                or _unresolved_sibling_orders
            )
            if not _structurally_ambiguous:
                # Redacted AND overflow-safe: this text reaches the scheduler's
                # log stream and persisted skip_reason fields, and a bare
                # f-string on `data` raised ValueError on an oversized int
                # instead of the intended terminal error (2026-09-07
                # diagnostic, S:ADP-02).
                logger.error(
                    "place_equity_order for %s returned terminal state %s: %s", ticker, state, _artifact_safe_repr(data)
                )
                raise MarketBuyPlacementTerminalError(
                    f"live placement for {ticker} came back in terminal state {state}; response={_artifact_safe_repr(data)}",
                    order,
                )
            logger.error("place_equity_order for %s returned terminal state %s: %s", ticker, state, _artifact_safe_repr(data))
            # This exact response's own recovered execution notional, so
            # reconciliation can refuse to silently discard it if a later
            # history match reports a different (or no) executed amount
            # for the same id (Codex review, PR #859 round 39: "Retain
            # response fill evidence through reconciliation").
            raise AmbiguousPlacementResponseError(
                f"live placement for {ticker} came back in terminal state {state}; response={_artifact_safe_repr(data)}",
                order_id=order_id,
                response_notional_usd=_recovered_broker_notional(_evidence_order),
                response_may_have_executed=_reconciled_order_may_have_executed(_evidence_order),
                # `order_id` above names only ONE identity when the response
                # itself disputes its own (`_order_identity_conflicts`) or
                # names several distinct candidate orders
                # (`ambiguous_nested_orders`) -- the caller must not let a
                # history match on that ONE identity stand in for resolving
                # the response's actual ambiguity (Codex review, PR #859/#911
                # round 42: "Keep structurally ambiguous responses
                # unresolved"). `_unresolved_sibling_orders` belongs
                # alongside them: a queued SIBLING order this scan found
                # under some other key is exactly as real a second
                # identity as one named in `orders` or in conflicting id
                # aliases, and a caller's downgrade path (an exact-ref
                # history match resolving `order_id` alone) must not treat
                # that as resolving the sibling's own unproven state too
                # (Codex review, PR #1164: "Preserve sibling-order
                # ambiguity through reconciliation").
                structurally_ambiguous=(
                    _order_identity_conflicts or ambiguous_nested_orders or _unresolved_sibling_orders
                ),
            )
        # Accumulate raw broker response for orders_after_place.json so fill data
        # is replayable from durable storage without a live broker re-query.
        self._raw_placement_responses.append({
            "ticker": ticker,
            "order_id": str(order_id),
            **order,
        })
        self._accepted_order_ids.add(str(order_id))
        return str(order_id)

    def review_whole_share_buy(self, ticker: str, quantity: int) -> Tuple[bool, str]:
        """Quantity-based counterpart to :meth:`review_market_buy`.

        For :mod:`dca_whole_share_accumulator`'s fire decisions: ``ticker``
        is not tradable for fractional/notional buys, so a dollar-amount
        review would be reviewing the wrong kind of order entirely. See the
        class docstring's note on the ``quantity=`` assumption.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive for a whole-share buy review")
        if self._review_fn is None:
            if self.enable_live_orders:
                return False, "live orders enabled but no review callable wired; refusing to place unreviewed order"
            return True, "no review callable wired; skipping broker-side review (dry-run)"
        response = self._review_fn(
            account_number=self._account,
            symbol=ticker,
            side="buy",
            type="market",
            quantity=str(quantity),
        )
        return _interpret_review(response)

    def place_whole_share_buy(self, ticker: str, quantity: int, ref_id: str) -> str:
        """Quantity-based counterpart to :meth:`place_market_buy`.

        Places a whole-share (non-fractional) market buy for exactly
        ``quantity`` shares, for a ticker not tradable for fractional/notional
        buys -- see :mod:`dca_whole_share_accumulator`. Every fail-closed
        check :meth:`place_market_buy` applies (live gate + place_fn wired,
        failure envelope, missing order id, terminal rejection state) applies
        identically here; only the order-sizing argument differs.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive for a whole-share buy")
        # `is True`, not truthiness, matching dca_runner.execute_plan's own
        # literal-boolean gate: a passthrough like enable_live_orders="false"
        # is a truthy string and reached real placement here while the runner
        # refused the same value (2026-09-07 diagnostic, C:F11).
        if not (self.enable_live_orders is True and self._place_fn is not None):
            raise RuntimeError(
                f"place_whole_share_buy called for {ticker} with the live gate disabled "
                f"(enable_live_orders={self.enable_live_orders}, "
                f"place_fn={'set' if self._place_fn else 'None'}); use dry_run=True for simulated runs"
            )
        try:
            response = self._place_fn(
                account_number=self._account,
                symbol=ticker,
                side="buy",
                type="market",
                quantity=str(quantity),
                time_in_force=self._time_in_force,
                ref_id=ref_id,
            )
        except OrderPlacementAmbiguousError:
            # See place_market_buy's identical pass-through just above.
            raise
        except Exception as exc:  # noqa: BLE001 - see place_market_buy's
            # identical wrapping just above: the connector call itself
            # (network timeout, connection reset, MCP transport error, ...)
            # gave no broker response at all, unlike every raise below this
            # point, which reacts to a definitive response the broker DID
            # return. Whether this whole-share order actually reached and
            # was accepted by the broker is genuinely unknown here (Codex
            # review, PR #1300 discussion_r3920546926: "Preserve ambiguity
            # for whole-share placement timeouts").
            raise OrderPlacementAmbiguousError(
                f"whole-share placement connector call for {ticker} failed with no broker response: {_artifact_safe_repr(exc)}"
            ) from exc
        outer_failure = _response_failure(response)
        if outer_failure is not None:
            logger.error("place_equity_order (whole-share) for %s reported failure: %s", ticker, _artifact_safe_repr(response))
            artifact_safe_failure = _redacted_diagnostic_text(outer_failure)
            # An outer failure envelope (e.g. {"success": false, "data":
            # {"order": {...}}}) can still carry an inner order that reached
            # the broker and reports its own terminal state and
            # cumulative_quantity -- for example, a partially filled order
            # that was then cancelled, with the cancellation itself surfaced
            # as the outer "success: false". Discarding that inner payload
            # and raising a bare RuntimeError forces the caller to rely
            # exclusively on a SEPARATE, later order-history lookup that can
            # itself be eventually consistent and report a clean "no match"
            # for an order this very response already proved landed --
            # restoring the full reservation and making already-spent
            # credit reusable (Codex review: "Preserve inner orders from
            # failure envelopes"). Mirrors the terminal-state branch below:
            # an inner order in a genuinely terminal state is authoritative
            # evidence regardless of the outer envelope's own verdict.
            outer_failure_data = _unwrap_seeking_order_evidence(response)
            # Mirrors the normal success path's own `data.get("order") or
            # data` fallback just below: _unwrap(response) can itself BE the
            # bare order object (no nested "order" key at all), a shape the
            # success path already treats as equally valid. Checking only
            # `.get("order")` missed that shape entirely, so an outer
            # failure like {"success": false, "data": {"id": ..., "state":
            # "cancelled", "cumulative_quantity": ...}} still fell through to
            # a bare RuntimeError, discarding the very evidence this branch
            # exists to preserve (Codex review: "Preserve bare orders in
            # outer failure envelopes").
            # Keep this extraction identical to the success-envelope path:
            # a nested order wins when present, while the unwrapped payload
            # itself remains the candidate for brokers that return a bare
            # order in ``data``.  Besides making the supported response
            # shapes explicit, this also avoids treating a non-mapping
            # ``order`` member as authoritative over a usable bare payload.
            inner_order = None
            if isinstance(outer_failure_data, dict):
                nested_order = outer_failure_data.get("order")
                inner_order = nested_order if isinstance(nested_order, dict) else outer_failure_data
            if isinstance(inner_order, dict):
                inner_order_id = inner_order.get("id") or inner_order.get("order_id")
                inner_state = str(inner_order.get("state", "")).lower()
                # Not gated on inner_state being in a TERMINAL set -- an
                # inner order still LIVE/working (e.g. "new"/"confirmed"/
                # "partially_filled") is just as real a broker acceptance as
                # a terminal one, and discarding it here (an earlier version
                # of this fix did) forced the caller to rely exclusively on
                # its own separate live/terminal-inclusive lookups, which can
                # BOTH independently miss the very order this response
                # already proved was accepted (an eventually-consistent
                # order-history endpoint) -- restoring the full reservation
                # and making already-committed credit reusable while the
                # order remains live at the broker (Codex review: "Preserve
                # accepted nonterminal orders in failure envelopes"). The
                # caller's own exc.order handling already discriminates
                # terminal vs. still-live states correctly (see its "Verify
                # terminal state after the any-state fallback" branch), so
                # passing ANY inner order with a usable id through this same
                # exception -- regardless of state -- is safe: it is treated
                # exactly like the any-state fallback's own result.
                if inner_order_id:
                    raise WholeSharePlacementTerminalError(
                        f"live whole-share placement for {ticker} reported an outer failure "
                        f"({artifact_safe_failure}), but its inner order carries evidence of a real "
                        f"broker attempt (state={inner_state or 'unknown'}); response={_artifact_safe_repr(outer_failure_data)}",
                        order=inner_order,
                    )
            raise RuntimeError(f"live whole-share placement for {ticker} failed: {artifact_safe_failure}")
        data = _unwrap_seeking_order_evidence(response)
        # Validated against a conflicting "orders" collection alongside
        # the singular "order" row, the same way `place_market_buy`'s
        # identical success path already is -- a bare `data.get("order")
        # or data` trusted the singular row outright: `{"order": {"id":
        # "A", "state": "queued"}, "orders": [{"id": "B", "state":
        # "queued"}]}` names a SECOND, potentially-live broker order the
        # singular row alone masks entirely, so the caller recorded and
        # settled only "A" while "B" stayed outside attempt and notional
        # accounting, potentially understating shared caps and permitting
        # another purchase (Codex review, PR #1169: "Validate whole-share
        # order collections before returning").
        order, _ambiguous_nested_orders = _select_singular_order_against_collection(
            _merged_order_collection_view(data, response)
        )
        if order is None:
            order = data
        elif not _ambiguous_nested_orders:
            # Merge in any agreeing collection row's FULL evidence, the same
            # way `place_market_buy`'s identical success path already does
            # via `_agreeing_collection_row` -- a bare selected `order`
            # trusted the singular row's own fields alone: `{"order":
            # {"id": "A"}, "orders": [{"id": "A", "state": "rejected",
            # "cumulative_quantity": "0"}]}` has the singular row agreeing
            # (same id, no contradicting field) but supplying no lifecycle
            # of its own, so the terminal-state check below read state=""
            # from the unmerged singular row and returned "A" as an
            # accepted placement despite the broker's own rejection sitting
            # right beside it in the collection (Codex review, PR #1168:
            # "Merge agreeing collection evidence before accepting").
            order = _merge_envelope_layers_prefer_innermost_nonblank(
                order,
                _agreeing_collection_row(
                    order, _merged_order_collection_view(data, response).get("orders")
                )
                or {},
            )
        if _ambiguous_nested_orders:
            raise AmbiguousPlacementResponseError(
                f"live whole-share placement for {ticker} named a singular order alongside a "
                f"conflicting orders collection this call cannot resolve; response={_artifact_safe_repr(data)}",
                structurally_ambiguous=True,
            )
        order_id = _first_usable_order_id_alias(order)
        # The SAME usable-ID validation the fractional path already applies
        # (see place_market_buy's own identical check), not a bare
        # truthiness test: a connector sentinel -- `id=true` (a bool, which
        # `isinstance(x, int)` alone would NOT catch, since bool subclasses
        # int in Python), `id=-1`, or a whitespace-only string -- is
        # truthy, so this used to accept it as a real broker identifier.
        # The caller marks the placement accepted, reserves/settles
        # accumulator credit, and persists a submitted order that can never
        # actually be queried back from the broker by that id (Codex
        # review, PR #859/#911 round 44: "Restore whole-share order-ID
        # validation"). Also rejects an astronomically large int, and
        # accepts a finite positive float, the same way the fractional
        # path's identical check does -- see `_is_representable_numeric_
        # order_id`'s own docstring (Codex review, PR #1164: "Reject
        # oversized numeric placement IDs"; PR #1165: "Preserve finite
        # positive float order IDs").
        if (
            isinstance(order_id, bool)
            or not isinstance(order_id, (str, int, float))
            # Checked BEFORE the bare `str(order_id).strip()` below --
            # see `place_market_buy`'s identical fix (Codex review, PR
            # #1169: "Check numeric representability before stringifying
            # placement IDs") for why a native oversized int/float must
            # be rejected here before `str()` can raise on it.
            or (isinstance(order_id, (int, float)) and not _is_representable_numeric_order_id(order_id))
            or not str(order_id).strip()
            or (isinstance(order_id, str) and _is_stringified_non_positive_number(order_id.strip()))
            # A serialized non-finite sentinel ("NaN", "Infinity", "-inf")
            # is truthy, doesn't start with "-", and isn't caught by the
            # stringified-non-positive check above (a NaN comparison
            # against 0 is always False, and positive infinity isn't <= 0
            # either) -- so this validation accepted it as a real broker
            # identifier and place_market_buy()/place_whole_share_buy()
            # returned "NaN" as a proven submission, even though fill
            # lookup and recovery can never identify an order by that id
            # and `_normalized_order_id` later discards it from durable
            # provenance anyway. Mirrors the identical check the history
            # and reference-ID paths already apply (Codex review, PR
            # #1169: "Reject serialized non-finite placement IDs").
            or (isinstance(order_id, str) and _is_stringified_non_finite_number(order_id.strip()))
            or (isinstance(order_id, (int, float)) and order_id <= 0)
        ):
            # Same validation as `place_market_buy`'s identical check
            # (Codex review, PR #1168: "Reject stringified zero placement
            # IDs") -- a stringified non-positive id ("0") is rejected the
            # same way, not just a leading "-".
            order_id = None
        if order_id is None:
            # Same fallback as `place_market_buy`'s identical check
            # (Codex review, PR #1169: "Fall through unusable primary
            # placement IDs"): a truthy but unusable primary alias
            # (e.g. a boolean sentinel) was selected and just cleared
            # above, discarding a perfectly usable secondary alias
            # sitting right beside it.
            _secondary_order_id = order.get("order_id")
            if not _is_unusable_live_placement_order_id(_secondary_order_id):
                order_id = _secondary_order_id
        if not order_id:
            logger.error("place_equity_order (whole-share) returned no order id for %s: %s", ticker, _artifact_safe_repr(data))
            raise RuntimeError(f"live whole-share placement for {ticker} returned no order id; response={_artifact_safe_repr(data)}")
        # Falls back to "status" when "state" is blank/absent, normalized
        # via the SAME `_normalize_broker_lifecycle_alias` the fractional
        # placement path already uses, not a bare `order.get("state", "")`:
        # a connector reporting lifecycle only through "status" ({"id":
        # "A", "status": "rejected", "cumulative_quantity": "0"}) read as
        # an empty state here, matched neither terminal check, and
        # returned "A" as a successful whole-share submission despite the
        # broker's own rejection -- the caller then settled accumulator
        # credit and recorded the placement as accepted (Codex review, PR
        # #1169: "Honor the status alias in whole-share placement
        # responses").
        state = (
            _normalize_broker_lifecycle_alias(order.get("state"))
            or _normalize_broker_lifecycle_alias(order.get("status"))
            or ""
        )
        # A row whose "state" and "status" aliases DISAGREE (one working,
        # one terminal) is unresolved evidence, the same reasoning
        # `_order_lifecycle_aliases_conflict` already documents for the
        # fractional path: picking either alias silently discards the
        # other's own, possibly-authoritative story. Routed through the
        # SAME terminal-error path below (not accepted as a successful
        # submission) so the caller's existing reconciliation -- which
        # inspects the real cumulative fill -- resolves it rather than
        # this call guessing (Codex review, PR #1169: "Honor the status
        # alias in whole-share placement responses").
        _lifecycle_alias_conflict = _order_lifecycle_aliases_conflict(order)
        # A CONFLICTING alias pair -- e.g. state="rejected" alongside
        # status="queued" -- is NOT the same shape as an ordinary,
        # self-consistent terminal response: `state` above already picked
        # ONE alias (state, when present, wins over status) and trusting
        # it here would make the terminal-only path below -- which skips
        # the broker-history lookup entirely and reads the response's own
        # `state` as authoritative (see `_process_whole_share_candidate`'s
        # own "A WholeSharePlacementTerminalError already carries the
        # broker's OWN immediate response" comment) -- release the
        # reserved claim as a proven zero-fill even though the OTHER
        # alias still says the order is live. Routed through
        # `AmbiguousPlacementResponseError` instead -- the SAME
        # structurally-ambiguous halt path the singular-vs-collection
        # conflict above already uses -- which the caller's generic
        # `except Exception` handler does NOT special-case the way it
        # does `WholeSharePlacementTerminalError`, so it falls through to
        # the real broker-history reconciliation lookup rather than
        # trusting either alias (Codex review, PR #1251: "Halt lifecycle-
        # conflicting whole-share responses").
        # Identity FIRST, before the lifecycle-conflict and terminal branches
        # below: WholeSharePlacementTerminalError is special-cased by the
        # caller as authoritative fill evidence for THIS claim, so a stale
        # terminal response naming another symbol or quantity could restore
        # or settle the wrong ticker's credit. A mismatch stays structurally
        # ambiguous and goes through the real broker-history reconciliation
        # (Copilot review, PR #1394: "Validate identity before interpreting
        # terminal responses"; 2026-09-07 diagnostic, S:ADP-01).
        identity_mismatch = _placement_response_identity_mismatch(
            order,
            ticker=ticker,
            ref_id=ref_id,
            quantity=quantity,
            require_identity=self.require_placement_identity,
        )
        if identity_mismatch is not None:
            logger.error(
                "place_equity_order (whole-share) for %s returned an order that does not describe this request (%s): %s",
                ticker,
                identity_mismatch,
                _artifact_safe_repr(order),
            )
            raise AmbiguousPlacementResponseError(
                f"live placement for {ticker} returned an order that does not describe this request: "
                f"{identity_mismatch}; response={_artifact_safe_repr(order)}",
                order_id=str(order_id) if order_id is not None else None,
                response_notional_usd=_recovered_broker_notional(order),
                response_may_have_executed=True,
                structurally_ambiguous=True,
            )
        self._refuse_replayed_order_id(order_id, ticker=ticker, order=order)
        if _lifecycle_alias_conflict:
            logger.error(
                "place_equity_order (whole-share) for %s reported conflicting lifecycle aliases "
                "(state=%r, status=%r): %s",
                ticker,
                order.get("state"),
                order.get("status"),
                # Redacted here too (S:ADP-02, 2026-09-07 diagnostic): no log
                # sink in this repo redacts on the caller's behalf, so a raw
                # `data` here would echo account_number/URLs into whatever
                # transport the external scheduler's logging handler sends
                # to, the same exposure the raised exception below already
                # guards against.
                _artifact_safe_repr(data),
            )
            raise AmbiguousPlacementResponseError(
                f"live whole-share placement for {ticker} reported conflicting lifecycle aliases "
                f"(state={order.get('state')!r}, status={order.get('status')!r}); "
                # Redacted (not `_log_safe_repr`): this message can be
                # interpolated into a persisted `skip_reason` and later
                # written into a run artifact (Codex review, PR #1251:
                # "Redact lifecycle-conflict payloads before raising").
                f"response={_artifact_safe_repr(data)}",
                structurally_ambiguous=True,
            )
        # "expired" is a terminal, non-live state exactly like the others
        # here (see _LIVE_ORDER_STATES's own comment above, which already
        # excludes it) -- without rejecting it too, the caller would treat
        # this response as a successful full submission and settle the
        # claim for the ENTIRE requested quantity even if no shares
        # actually executed (Codex review: "Reject expired whole-share
        # placement responses"). Raising here (rather than returning the
        # order id) routes this through the SAME ambiguous-placement
        # reconciliation the caller already applies to any other raised
        # exception, which inspects the real cumulative fill via the
        # terminal-inclusive lookup before restoring or settling credit.
        if state in {"rejected", "failed", "cancelled", "canceled", "voided", "expired"}:
            logger.error(
                "place_equity_order (whole-share) for %s returned terminal state %s: %s",
                ticker,
                state,
                _artifact_safe_repr(data),
            )
            # Carries the order payload rather than a bare RuntimeError --
            # this response is ALREADY authoritative terminal evidence
            # (state + cumulative_quantity + average_price), and discarding
            # it before raising would force the caller to depend
            # exclusively on a SEPARATE, later order-history lookup that can
            # itself be eventually consistent and report a clean "no match"
            # for an order this very response already proved landed (Codex
            # review: "Reconcile terminal fills from the placement
            # response").
            raise WholeSharePlacementTerminalError(
                f"live whole-share placement for {ticker} came back in terminal state {state}; response={_artifact_safe_repr(data)}",
                order=order,
            )
        self._raw_placement_responses.append({
            "ticker": ticker,
            "order_id": str(order_id),
            **order,
        })
        self._accepted_order_ids.add(str(order_id))
        return str(order_id)

    def get_raw_placement_responses(self) -> List[Dict[str, Any]]:
        """Return and clear all raw broker placement responses for the current run.

        Drains the internal buffer so a reused broker instance does not carry
        responses from a prior pipeline call into a subsequent one.
        """
        responses = list(self._raw_placement_responses)
        self._raw_placement_responses.clear()
        return responses

    def get_order_fill(self, order_id: str) -> Optional[OrderFill]:
        if not order_id or self._order_fill_fn is None:
            return None
        try:
            data = _unwrap(self._order_fill_fn(account_number=self._account, order_id=order_id))
        except Exception:  # noqa: BLE001 - a fill-lookup outage must not abort the run
            # The order is already placed by the time we look up its fill; a transient
            # get_equity_orders failure should leave it recorded as submitted/unfilled,
            # not crash run_once before the durable artifacts are written.
            logger.exception("order-fill lookup failed for %s; recording as unfilled", order_id)
            return None
        # Accept list mode ({"orders": [...]}), a single-order envelope ({"order": {...}},
        # the same nesting place_market_buy accepts), or a bare top-level order object.
        single = data.get("order") if isinstance(data.get("order"), dict) else None
        if single is None and (data.get("id") or data.get("order_id")):
            single = data
        orders = data.get("orders") or ([single] if single else [])
        for order in orders:
            # place_market_buy accepts either "id" or "order_id"; match on both here so a
            # history wrapper that keys fills by "order_id" is not misread as an unfilled gap.
            oid = order.get("id") or order.get("order_id")
            if str(oid) != order_id:
                continue
            # Plain "quantity" is the REQUESTED amount, not what executed --
            # scoped to state == "filled" for the same reason
            # near_close_dca._recovered_broker_notional's own quantity_field
            # selection is: a cancelled/rejected/expired order that omits
            # cumulative_quantity but carries its requested quantity and an
            # average_price would otherwise combine into a fabricated fill
            # for an order that moved nothing, marking a correctly-$0
            # recovery as "filled" in build_execution_record() (Codex
            # review: "Do not turn confirmed zero-fill orders into fills").
            order_state = str(order.get("state", "")).strip().lower()
            if not _is_blank_broker_field(order.get("cumulative_quantity")):
                quantity_source = order.get("cumulative_quantity")
            elif order_state == "filled":
                quantity_source = order.get("quantity")
            else:
                quantity_source = None
            filled_qty = _to_float(quantity_source) if quantity_source is not None else 0.0
            avg_price = _to_float(order.get("average_price") or order.get("price"))
            if filled_qty <= 0 or avg_price <= 0:
                return None
            return OrderFill(
                avg_fill_price=avg_price,
                quantity_filled=filled_qty,
                order_submitted_at=_parse_rfc3339(order.get("created_at")),
                order_filled_at=_parse_rfc3339(order.get("last_transaction_at") or order.get("updated_at")),
            )
        return None


# -- module helpers --------------------------------------------------------------


def _to_utc_iso(moment: datetime) -> str:
    """Render an aware datetime as a UTC RFC3339 string (naive assumed Eastern)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=EASTERN)
    return moment.astimezone(tz=_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_date(value: Any) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _coerce_bar(raw: Any) -> Optional[Bar]:
    """Coerce a vendor bar mapping (or a ready ``Bar``) into a ``Bar``."""
    if isinstance(raw, Bar):
        return raw
    if not isinstance(raw, dict):
        return None
    ts_value = raw.get("timestamp") or raw.get("begins_at") or raw.get("t") or raw.get("date")
    if isinstance(ts_value, datetime):
        timestamp: Optional[datetime] = ts_value if ts_value.tzinfo else ts_value.replace(tzinfo=EASTERN)
    elif isinstance(ts_value, date):
        timestamp = datetime.combine(ts_value, time(16, 0), tzinfo=EASTERN)
    else:
        timestamp = _parse_rfc3339(ts_value)
    if timestamp is None:
        return None
    close = _to_float(raw.get("close", raw.get("close_price", raw.get("c"))), default=-1.0)
    if close <= 0:
        # Reject placeholder/non-numeric closes (e.g. "N/A") like the Robinhood parser: a
        # 0.0 close would read as a ~100% drop and trigger a spurious max BUY.
        return None
    return Bar(
        timestamp=timestamp,
        open=_to_float(raw.get("open", raw.get("open_price", raw.get("o")))),
        high=_to_float(raw.get("high", raw.get("high_price", raw.get("h")))),
        low=_to_float(raw.get("low", raw.get("low_price", raw.get("l")))),
        close=close,
        volume=_to_float(raw.get("volume", raw.get("v"))),
    )


def _all_data_layers(payload: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """Every dict layer along the recursive ``"data"`` descent chain, from
    ``payload`` itself down to the innermost nested ``"data"`` dict --
    unconditionally, never stopping early the way
    :func:`_unwrap_traversed_layers` deliberately does -- plus whether
    ``_MAX_ENVELOPE_DESCENT_DEPTH`` cut the walk short before it reached a
    genuine stopping point (a non-dict/absent ``"data"`` value).

    Returns ``(layers, truncated)``, NOT bare ``layers``: a bounded walk
    that stops at the depth cap can still be looking at a cyclic or
    pathologically deep envelope with real failure/blocker evidence sitting
    PAST the cut -- ``available_buying_power``'s own pre-scan for a failed
    layer (or ``_iter_same_day_orders``'s, or ``_failure_is_broker_
    authored``'s contradiction scan, or ``_interpret_review``'s blocking
    scan) only sees what made it into ``layers`` and would otherwise read
    "no failure found in what I saw" as "no failure exists", returning an
    early ``buying_power``/approving a review/proving a rejection despite a
    ``success: false``/blocker one layer past the cap (Codex review, PR
    #1169: "Fail closed when envelope descent reaches its cap" -- the
    fresh-evidence follow-up to "Bound recursive data-layer traversal": the
    depth cap fixed the HANG, but a caller that treats a merely-bounded
    scan as a COMPLETE one reopens the same fail-open gap in a different
    shape). Every caller of this function must fail closed when this flag
    is ``True``, exactly as it would on genuine failure evidence -- an
    incomplete scan is not evidence of a clean response.

    ``_unwrap_traversed_layers`` stops descending the moment a layer
    carries a TRUTHY, otherwise-unrecognized key (the "preserve domain
    fields before descending" fix a few hundred lines up) or is contradicted
    by the very layer :func:`_skippable_health_flag_contradicted` is about
    to skip past -- exactly right for placement classifiers, where the safe
    reading of a self-contradictory or domain-bearing response is
    "unattributable, not a proven outcome either way" (see that function's
    own docstring). :func:`_interpret_review` needs the OPPOSITE default on
    both counts: an outer ``{"approved": true, "data": {"approved":
    false}}`` has ``approved`` (a review-specific field, never in
    `_ENVELOPE_EVIDENCE_KEYS`) tripping the truthy check and stopping
    descent before the nested rejection is ever reached (Codex review, PR
    #1164 round 15: "Inspect nested blockers after affirmative review
    fields"), and a self-contradictory response (``{"data": {"success":
    true, "data": {"success": false, "approved": true}}}``) must still
    surface the inner ``success: false`` as a blocker, not have it silently
    made unreachable because the outer ``success: true`` it contradicts
    made ``_unwrap_traversed_layers`` stop one layer short (Codex review, PR
    #1164/#1165: "Block false health flags from every review layer").
    Walking unconditionally to the bottom means every layer's
    ``success``/``ok``/``isError``/``approved``/``tradeable``/alert fields
    are visible to a caller that unions blocking evidence across all of
    them, whatever the health flags along the way say.

    The RAW top-level ``payload`` is included as a layer only when it has
    no ``"data"`` key to descend into at all -- mirroring ``_unwrap``'s own
    "the first unwrap is unconditional" rule. Including the bare transport
    envelope UNCONDITIONALLY (a first version of this function did) let an
    unrelated response's outer ``{"success": true, "data": {"quote":
    ...}}}`` satisfy ``_interpret_review``'s shape check on the connector's
    own transport flag alone, even though the actual unwrapped content
    (``{"quote": ...}``) carries no review evidence whatsoever -- returning
    a silent approval for a response that was never a review outcome to
    begin with (Codex review, PR #1164/#1165: "Keep outer success from
    satisfying review shape checks"). ``_response_failure``, called
    separately by ``_interpret_review``, already inspects the raw
    ``response`` for outer-envelope FAILURE signals, so excluding it here
    costs nothing on the failure side.
    """
    if not isinstance(payload, dict):
        return [], False
    data = payload.get("data")
    if not isinstance(data, dict):
        return [payload], False
    layers: List[Dict[str, Any]] = [data]
    node: Any = data
    _depth = 0
    while isinstance(node, dict):
        if _depth >= _MAX_ENVELOPE_DESCENT_DEPTH:
            # See `_MAX_ENVELOPE_DESCENT_DEPTH`'s own docstring: bounds a
            # cyclic or pathologically deep envelope instead of looping
            # forever. Reported to the caller as `truncated=True` --
            # unlike a genuine stop (a non-dict/absent nested "data"), this
            # is NOT proof there is nothing more to see.
            return layers, True
        node = node.get("data")
        if not isinstance(node, dict):
            break
        layers.append(node)
        _depth += 1
    return layers, False


def _interpret_review(response: Any) -> Tuple[bool, str]:
    """Interpret a ``review_equity_order`` response into ``(approved, reason)``.

    Robinhood's review returns a live quote plus pre-trade *alerts*. Approval requires no
    blocking signal. Blocking comes from **structured** evidence only, so that neither a
    harmless warning nor a happy-path quote flips the result the wrong way:

    * explicit failure fields — ``error`` truthy, or ``success``/``ok``/``approved`` is
      ``False`` — fail closed (a wrapper that reports failure as data, not an exception);
    * ``state`` in ``{rejected, failed}`` or ``tradeable is False``;
    * a dict alert (in any container) flagged error/blocking/reject/critical;
    * a non-empty string in an explicitly error-named container (``errors`` …).

    Plain-string entries under ``warnings``/``messages``/``alerts`` are treated as
    informational and never block on their own (they carry no severity). An empty
    response approves — it is genuinely empty, not a reported failure.

    Scans EVERY layer :func:`_all_data_layers` walks through, not
    the single layer ``_unwrap`` happens to stop on, and -- unlike
    :func:`_unwrap_traversed_layers` -- without stopping early at a
    self-contradictory or otherwise-truthy layer (see that function's own
    docstring for why `_interpret_review` cannot share its early-stop
    behavior). A nested rejection one layer past an affirmative outer
    layer (``{"data": {"approved": true, "data": {"approved": false}}}``)
    is otherwise never reached at all -- ``_ENVELOPE_EVIDENCE_KEYS``
    deliberately does not anchor ``_unwrap`` on review-specific fields for
    exactly this reason (see that tuple's own docstring), so this function
    does its own multi-layer scan instead (Codex review, PR #1164 round
    15: "Inspect nested blockers after affirmative review fields").
    """
    layers, _layers_truncated = _all_data_layers(response)
    if _layers_truncated:
        # See `_all_data_layers`'s own docstring ("Fail closed when
        # envelope descent reaches its cap"): a blocker sitting past the
        # depth cap is invisible to every scan below, so an incomplete
        # scan must not be read as "no blocking alerts reported".
        return False, "review response exceeded the max envelope depth"
    _dict_layers = [layer for layer in layers if isinstance(layer, dict)]
    # A layer contributing only a "data" key -- itself just the wrapper
    # `_all_data_layers` descended THROUGH to reach the next layer --
    # carries no content of its own and must not count as "non-empty"
    # evidence: `{"data": {"data": {}}}` produces the layer list
    # `[{"data": {}}, {}]`, and a bare `any(_dict_layers)` reads the FIRST
    # layer's own non-empty `{"data": {}}` mapping as truthy, so this
    # previously-accepted empty-envelope shape fell through to the shape
    # check below and was rejected as an "unexpected" response instead of
    # approved as the empty one it actually is (Codex review, PR #1164:
    # "Treat data-wrapped empty reviews as empty"). The blocking scan below
    # still runs unconditionally across every layer, so a real blocker on
    # an outer layer alongside an empty nested "data" is still caught --
    # only the emptiness check itself needed the "data"-only exclusion.
    # The chain must also actually BOTTOM OUT cleanly -- in a blank value or
    # an empty mapping -- not merely have every DICT layer along the way
    # carry no content of its own. `_all_data_layers` stops walking the
    # moment a layer's own "data" value is not itself a dict, so
    # `{"data": {"data": "broker error"}}` produces the dict-layer list
    # `[{"data": "broker error"}]` -- its one key is bare "data", passing
    # the per-layer check above, but the STRING it points to (a real,
    # if malformed, diagnostic payload) was never walked into and is
    # silently dropped from `_dict_layers` entirely. Reading that as
    # "nothing here" approved a response this module could not actually
    # read, rather than the fail-closed "unexpected shape" rejection an
    # unreadable review response deserves (Codex review, PR #1164: "Reject
    # malformed values inside empty review wrappers").
    _trailing_data = _dict_layers[-1].get("data") if _dict_layers else None
    _chain_ends_cleanly = (
        _trailing_data is None
        or _is_blank_broker_field(_trailing_data)
        or (isinstance(_trailing_data, dict) and not _trailing_data)
    )
    # The RAW top-level `response` is excluded from `_all_data_layers`'s own
    # layer list (and therefore from `_dict_layers`) whenever it has a
    # "data" key pointing to a dict -- correct for the shape check further
    # down (a bare transport-level `success: true` must not satisfy it on
    # its own, per that check's own comment), but wrong here: `{"approved":
    # false, "data": {}}` has `_dict_layers == [{}]`, an empty mapping that
    # satisfies BOTH the clean-chain check and the "no content of its own"
    # check above, so this function approved the response before ever
    # reaching the blocking scan below (which DOES include the raw layer)
    # -- the raw layer's own `approved: false` was excluded from the
    # emptiness read the identical way an inner nested rejection used to be
    # excluded from it before the "data"-only-wrapper fix (Codex review, PR
    # #1169: "Scan raw blockers before approving an empty review"). A raw
    # layer carrying any content beyond a bare "data" pass-through must
    # not be read as "nothing here", whatever the nested nested layer says.
    _raw_layer_excluded_with_content = (
        isinstance(response, dict)
        and (not _dict_layers or response is not _dict_layers[0])
        and bool(response.keys() - {"data"})
    )
    # Every traversed `_dict_layers` entry is either absent, an empty
    # mapping, or a pure "data" pass-through wrapper with no content of
    # its own, AND the chain bottoms out cleanly -- i.e. the entire nested
    # chain beneath the raw excluded layer contributes NOTHING. Named so
    # it can also decide whether the RAW layer's own fields are the
    # TERMINAL review payload (see its use in `_shape_recognized` below),
    # not just whether the response as a whole should read as empty here.
    _nested_chain_is_empty = _chain_ends_cleanly and not any(layer.keys() - {"data"} for layer in _dict_layers)
    if _nested_chain_is_empty and not _raw_layer_excluded_with_content:
        # A genuinely empty response, the same reading the single-layer
        # `if not data` check gave it before this function scanned every
        # layer instead of just the one `_unwrap` stops on.
        return True, "empty review response; no blocking alerts reported"

    # Shape sanity-check: a well-formed review payload must contain at least one recognised
    # review field on SOME traversed layer.  A plain quote response (e.g. {"quote": {...}})
    # with no review/alert keys anywhere does not constitute approval — fail closed so
    # unexpected broker shapes don't silently green-light a live order.
    _REVIEW_FIELDS = frozenset(
        {
            "approved",
            "state",
            "tradeable",
            "alerts",
            "warnings",
            "pre_trade_alerts",
            "messages",
            "checks",
            "errors",
            "error_messages",
            "rejections",
            "success",
            "ok",
            "error",
        }
    )
    # The RAW top-level `response` -- excluded from `_dict_layers` above
    # whenever it carries content beyond a bare "data" pass-through -- must
    # also be allowed to satisfy this shape check with its OWN
    # review-specific fields, not just disqualify the empty-response fast
    # path: `{"approved": true, "data": {}}` has `_dict_layers == [{}]`,
    # so scanning only `_dict_layers` here rejected an explicitly-approved
    # response as "unexpected shape" the moment `_raw_layer_excluded_with_
    # content` correctly kept it out of the empty-response reading above
    # (Codex review, PR #1168: "Preserve affirmative review fields on the
    # raw wrapper").

    # The deepest reached `_dict_layers` entry -- the actual TERMINAL review
    # payload this module stops descending into, exactly what `_trailing_data`
    # above is drawn from. Every OTHER `_dict_layers` entry, plus the raw
    # excluded wrapper, is a pass-through layer this module merely descended
    # through on the way there.
    _terminal_layer = _dict_layers[-1] if _dict_layers else None

    def _field_is_substantive(key: str, value: Any, *, is_terminal: bool) -> bool:
        # `success`/`ok` are ALWAYS transport-only, never review evidence,
        # on ANY layer -- not just the raw top-level wrapper, and not even
        # the terminal one. A bare pass-through `{"success": true, "data":
        # {"quote": ...}}}` (this exact "success" wrapping an unrelated
        # inner "data") lets an INTERMEDIATE `_dict_layers` entry satisfy
        # the shape check on the connector's own "the request succeeded"
        # flag alone, even though neither it nor the innermost payload
        # carries any review outcome (Codex review, PR #1168: "Exclude
        # intermediate transport flags from review evidence" -- the
        # sibling of "Exclude transport placeholders from raw review
        # evidence", which fixed the identical gap on the raw excluded
        # layer alone; both are now one shared check across every layer).
        if key in ("success", "ok"):
            return False
        # `approved`/`tradeable`/`state` must carry an actual VERDICT, not
        # just be present -- unconditionally, on every layer including the
        # terminal one. `{"approved": null, "data": {"quote": ...}}` had
        # this raw excluded wrapper's own placeholder `null` verdict
        # satisfy the shape check on bare presence alone (the blocker scan
        # only ever rejects `approved is False`, never treats `None` as a
        # blocker), approving an unrelated nested quote payload with no
        # valid review outcome at all (Codex review, PR #1168: "Require a
        # valid raw review verdict").
        #
        # A merely NONBLANK value is still not enough: `{"approved": 0}`,
        # `{"approved": "false"}`, or `{"tradeable": 0}` are all nonblank
        # per `_is_blank_broker_field` (only `None`/blank-string reads as
        # blank), so they satisfied the shape check as a "recognized"
        # verdict -- but the blocking scan only ever tests `is False`
        # (a strict identity check against the Python boolean), never `0`
        # or the string `"false"`, so neither of these serialized/
        # malformed negatives is ever caught as blocking. The response
        # then reads as a shape-recognized, non-blocked review and
        # approves a live order from a verdict that was never actually a
        # real `True` either (Codex review, PR #1169: "Require typed
        # review verdicts before approving"). `approved`/`tradeable` must
        # be actual Python booleans; `state` must be an actual (non-blank)
        # string -- `_is_blank_broker_field` already rejects a blank one,
        # but accepts a non-string value like `state=0` as "not blank"
        # the same way it did for the other two.
        if key in ("approved", "tradeable"):
            return isinstance(value, bool)
        if key == "state":
            return isinstance(value, str) and not _is_blank_broker_field(value)
        # Every OTHER field is presence-based on the TERMINAL layer: a
        # manual/ad hoc review response `{"alerts": []}` -- the ENTIRE,
        # already-fully-unwrapped payload, not a wrapper hiding something
        # deeper -- reports "checked, nothing to flag" exactly like the
        # function's own "empty response approves" contract already treats
        # a response with NO recognised fields at all. Filtering it out
        # here made `_interpret_review` reject this genuine review as
        # "unexpected shape" and block placement despite there being no
        # blocking alert (Codex review, PR #1168: "Accept empty terminal
        # alert containers as review evidence"). The blank/empty-aware
        # reading below exists ONLY to stop a PASS-THROUGH layer's
        # unrelated placeholder field (an intermediate wrapper's own blank
        # `error`, or an empty alert container sitting beside a deeper
        # `data` this module is about to descend past) from manufacturing
        # shape evidence for content it never actually reached.
        if is_terminal:
            # A genuine empty container (`{"alerts": []}`) is real "checked,
            # nothing to flag" evidence per the comment above, but a bare
            # NULL placeholder (`{"alerts": null}`) or a falsy `error`
            # (`{"error": false}`) on the terminal layer is not: neither
            # was ever populated by the reviewer, so treating its mere
            # presence as shape evidence let a response that never actually
            # ran a review (e.g. `{"alerts": null}` alone, with no other
            # recognised field anywhere) satisfy `_shape_recognized` and
            # sail through to the "no blocking alerts" approval path (Codex
            # review, PR #1168: "Reject terminal review placeholders").
            # `error` gets the same falsy-value reading the pass-through
            # branch below (and the blocking scan itself) already give it --
            # `false` carries no more evidence on the terminal layer than it
            # does anywhere else -- while the other containers only need to
            # rule out the `None` placeholder, keeping `[]`/`{}` as evidence.
            if key == "error":
                return bool(value)
            # A genuine empty container ([]/{}) is real evidence per the
            # comment above, but `value is not None` alone also accepted
            # any other non-None placeholder that was never a real alert/
            # warning/etc. container to begin with: `{"warnings": false,
            # "quote": {...}}`, `{"checks": 0, ...}` both satisfied this on
            # the terminal layer, letting an unrelated quote response sail
            # through `_shape_recognized` with no actual review verdict
            # ever run (Codex review, PR #1168: "Reject malformed terminal
            # review containers"). A NONBLANK STRING is still accepted,
            # not just list/dict -- `_as_list` (used by the blocking scan
            # a few hundred lines below) deliberately treats a bare scalar
            # value as a single informational entry, so `{"warnings":
            # "price moved"}`/`{"messages": "informational"}` are
            # genuinely-supported, previously-accepted review shapes, not
            # malformed placeholders; requiring `list`/`dict` alone
            # regressed those (Codex review, PR #1168: "Accept
            # informational string review containers"). A BLANK string
            # (""/whitespace) is still rejected, matching every other
            # blank-placeholder reading in this function.
            if isinstance(value, (list, dict)):
                return True
            if isinstance(value, str):
                return not _is_blank_broker_field(value)
            return False
        # `error` is ALSO a transport placeholder, not just success/ok --
        # `{"error": false, "data": {"quote": ...}}` has a falsy `error`
        # that carries no review outcome whatsoever, and treating its bare
        # PRESENCE as shape evidence let this unrelated payload through the
        # shape check, approving a response that was never a review
        # outcome to begin with (Codex review, PR #1168: "Exclude
        # transport placeholders from raw review evidence"). The alert/
        # error CONTAINERS get the identical "empty carries no evidence"
        # reading the round-15 fix already gives them a few hundred lines
        # up for the blocking scan. `approved`/`tradeable`/`state`, by
        # contrast, are kept PRESENCE-based: `approved: false` is itself
        # the review verdict, not a placeholder to see past.
        # `_is_blank_broker_field` only reads `None`/whitespace-string as
        # blank -- exactly right for keeping a real `error: false`
        # (Python) value distinct from a blank placeholder ANYWHERE this
        # module reads a genuine diagnostic payload, but wrong here: a
        # bare boolean `error: false` is not blank by that definition (it
        # is not a string and not `None`), so `not _is_blank_broker_field
        # (False)` evaluated to `True`, letting the identical falsy
        # transport placeholder this branch exists to exclude satisfy the
        # shape check anyway. `error` needs the same FALSY-value reading
        # the blocking scan itself already gives it (`if _layer_error:
        # blocking.append(...)`), not a blank-string-only check (Codex
        # review, PR #1169: "Validate pass-through review fields before
        # approving").
        if key == "error":
            return bool(value)
        if key in ("alerts", "warnings", "pre_trade_alerts", "messages", "checks", "errors", "error_messages", "rejections"):
            return bool(value)
        # Unreachable in practice -- every member of `_REVIEW_FIELDS`
        # (the only keys ever passed here) is handled by one of the
        # branches above -- but kept as a defensive, non-blank-requiring
        # default for any field added to `_REVIEW_FIELDS` in the future
        # without a dedicated branch of its own.
        return not _is_blank_broker_field(value)

    _shape_recognized = any(
        _field_is_substantive(key, layer.get(key), is_terminal=(layer is _terminal_layer))
        for layer in _dict_layers
        for key in layer.keys() & _REVIEW_FIELDS
    ) or (
        _raw_layer_excluded_with_content
        and isinstance(response, dict)
        and any(
            # `is_terminal=_nested_chain_is_empty`, not a hardcoded
            # `False`: the raw layer is excluded from `_dict_layers`
            # purely because it has a "data" key to descend into, but when
            # that ENTIRE nested chain contributes nothing (`{"alerts":
            # [], "data": {}}`), the raw layer's OWN fields ARE the
            # entire, already-terminal review payload -- not a pass-
            # through wrapper hiding a real verdict deeper in.
            # `_chain_ends_cleanly` alone is NOT this signal -- it is true
            # any time `_dict_layers[-1]` simply has no "data" key of its
            # own, which is the ORDINARY case for a terminal layer that
            # DOES carry real content (`{"quote": {...}}}` has no "data"
            # key either), so using it here would have made nearly every
            # raw-excluded response read as terminal. Hardcoding
            # `is_terminal=False` made `_field_is_substantive` apply the
            # pass-through-only "empty alert container carries no
            # evidence" reading to a genuinely complete "checked, nothing
            # to flag" response, rejecting it as an unexpected shape
            # (Codex review, PR #1169: "Treat raw empty-alert reviews as
            # terminal evidence").
            _field_is_substantive(key, response.get(key), is_terminal=_nested_chain_is_empty)
            for key in response.keys() & _REVIEW_FIELDS
        )
    )
    if not _shape_recognized:
        _all_keys = sorted({key for layer in _dict_layers for key in layer})
        return False, f"unexpected review response shape; no recognised review fields (keys: {_all_keys})"

    def _is_malformed_verdict_field(key: str, value: Any) -> bool:
        # `_field_is_substantive`'s `approved`/`tradeable`/`state` type
        # checks only ever decide whether THAT one field can establish
        # `_shape_recognized` via the surrounding `any(...)` -- a layer
        # with a malformed second verdict alongside one genuinely valid
        # field (`{"approved": true, "tradeable": 0}`, `{"approved": true,
        # "state": 0}`) still satisfies `_shape_recognized` through
        # `approved` alone, and the blocking scan below only ever tests
        # `tradeable is False` (a strict identity check) or a STRING
        # `state`, never catching a serialized/malformed non-bool
        # `tradeable`/non-string `state` sitting right next to it.
        # `_interpret_review()` then approved a live placement from a
        # response with a verdict field it could never actually interpret
        # (Codex review, PR #1168: "Reject every malformed review
        # verdict"). Checked independently of `_shape_recognized` -- ANY
        # present verdict field of the wrong type fails the whole response
        # closed, regardless of whether another field already established
        # the shape.
        # `success`/`ok`/`isError` join the allowlist for the identical
        # reason: the blocking scan below tests them via strict `is False`/
        # `is True` identity checks, so a malformed transport verdict like
        # `success: 0`, `success: "false"`, or `isError: 1` passes the
        # shape check on a genuinely valid `approved: true` beside it and
        # satisfies none of the strict identity tests either -- neither
        # recognized as the blocker it actually represents nor rejected as
        # unreadable, so `_interpret_review` approved a response carrying
        # an unrecognized negative transport signal (Codex review, PR
        # #1168: "Reject malformed transport verdicts beside valid
        # approvals").
        if key in ("approved", "tradeable", "success", "ok", "isError"):
            return value is not None and not isinstance(value, bool)
        if key == "state":
            # A BLANK string ("", whitespace) is malformed too, not just a
            # non-string: `_field_is_substantive` already refuses to use a
            # blank state as review evidence for `_shape_recognized`, but
            # this check previously considered every string well-formed.
            # A layer with a valid `approved` alongside a blank `state`
            # (`{"approved": true, "state": ""}`) still established the
            # shape through `approved` alone, and the blocking scan below
            # ignores a blank state (it only tests a non-blank string
            # against the approved-states allowlist), so the malformed
            # second verdict was silently ignored and the response was
            # approved (Codex review, PR #1168: "Reject blank secondary
            # review states").
            return value is not None and (not isinstance(value, str) or not value.strip())
        return False

    _VERDICT_FIELDS = frozenset({"approved", "tradeable", "state", "success", "ok", "isError"})
    _has_malformed_verdict = any(
        _is_malformed_verdict_field(key, layer.get(key))
        for layer in _dict_layers
        for key in layer.keys() & _VERDICT_FIELDS
    ) or (
        _raw_layer_excluded_with_content
        and isinstance(response, dict)
        and any(
            _is_malformed_verdict_field(key, response.get(key))
            for key in response.keys() & _VERDICT_FIELDS
        )
    )
    if _has_malformed_verdict:
        return False, "unexpected review response shape; malformed review verdict field"

    blocking: List[str] = []

    # Blocking evidence is scanned across EVERY layer along the descent
    # chain, not just the single layer `_unwrap` settles on: a layer whose
    # only _ENVELOPE_EVIDENCE_KEYS-recognized field is a skippable
    # "success: true"/"ok: true" gets descended past by `_unwrap` even when
    # it ALSO carries a real review blocker (`approved: false`,
    # `tradeable: false`, a blocking alert) under a field name no
    # order-placement classifier ever reads -- silently losing that blocker
    # the same way an outer/inner success contradiction used to (Codex
    # review, PR #1164: "Preserve review blockers while descending success
    # wrappers"). `data` (the `_unwrap`-settled layer) still anchors the
    # shape check and the "no blocking alerts" success message above; this
    # scan is additive, catching evidence `data` alone would miss.
    # Explicit failure signals, at the outer envelope or inside data: fail closed when the
    # wrapper reports failure as data (e.g. {"success": false, "data": {...}}).
    outer_failure = _response_failure(response)
    if outer_failure is not None:
        blocking.append(outer_failure)

    # The RAW top-level `response` is excluded from `_all_data_layers`'s
    # own layer list whenever it has a "data" key to descend into (see
    # that function's own docstring for why -- a bare transport-level
    # `success: true` must not satisfy the shape check above on its own).
    # But excluding it from the SHAPE check does not mean its own review
    # evidence should vanish: `{"approved": false, "data": {"approved":
    # true}}` has the raw outer layer naming an explicit rejection an
    # inner, affirmative layer disputes -- `_response_failure` above only
    # ever checks `success`/`ok`/`isError`/`error`, never `approved`/
    # `tradeable`/alerts, so that outer `approved: false` was invisible to
    # every check in this function and a response an outer layer had
    # already rejected was approved from its inner layer alone (Codex
    # review, PR #1168: "Preserve top-level review blockers during data
    # descent"). Scanned for BLOCKING evidence only, never re-added to
    # `_dict_layers` itself, so the shape/emptiness checks above keep
    # reading exactly as they did before this fix.
    _blocking_scan_layers = _dict_layers
    if (
        isinstance(response, dict)
        and (not _dict_layers or response is not _dict_layers[0])
    ):
        _blocking_scan_layers = [response, *_dict_layers]

    for data in _blocking_scan_layers:
        # `success`/`ok`/`isError`/`error` checked on EVERY layer here too,
        # not left to `outer_failure` above (which only ever inspects
        # `response` and `_unwrap(response)` -- exactly two layers). A
        # deeper `success: false` past an affirmative outer `approved: true`
        # (``{"data": {"approved": true, "data": {"success": false}}}``) was
        # invisible to both `outer_failure` and this loop alike -- this
        # loop checked `approved`/`state`/`tradeable` on every layer but not
        # these equally-recognized failure aliases, so `_interpret_review`
        # approved a review its own inner layer just as clearly failed
        # (Codex review, PR #1164: "Check failure aliases on every review
        # layer"). `_all_data_layers` walks past any early-stop point
        # unconditionally (see its own docstring), so this loop is what
        # actually reaches it.
        if data.get("success") is False or data.get("ok") is False:
            blocking.append("success=false")
        if data.get("isError") is True:
            blocking.append("isError=true")
        # Tested for TRUTHINESS, not `_is_blank_broker_field` (which reads
        # only None/""/whitespace as blank): `_is_error_response`'s own
        # rule 2 a few hundred lines below already establishes that a
        # falsy "error" -- `False`, `0`, `[]`, `{}` -- is a non-error
        # placeholder, not a blocking signal, exactly like an absent or
        # blank one. `_is_blank_broker_field` read `False`/`0`/`[]`/`{}` as
        # non-blank (they are not None/""/whitespace), so `{"approved":
        # true, "error": false}` blocked every live placement on a
        # broker's own explicit "nothing wrong" placeholder (Codex review,
        # PR #1164: "Ignore falsy review error placeholders").
        _layer_error = data.get("error")
        if _layer_error:
            blocking.append(str(_layer_error))
        # A malformed `approved`/`tradeable`/`state` verdict field is
        # caught earlier, right after the shape check above (`_has_
        # malformed_verdict`) -- fails the whole response closed as an
        # "unexpected shape" before this blocking scan ever runs, so this
        # loop only ever sees a well-typed value for these three fields
        # from here on (Codex review, PR #1168/#1169: "Reject every
        # malformed review verdict" / "Reject malformed verdicts beside
        # valid review fields" -- two independently-authored fixes for the
        # identical gap, this one wins since it also catches a malformed
        # `state`).
        if data.get("approved") is False:
            blocking.append("approved=false")
        # An ALLOWLIST, not a denylist: a string `state` reads as an
        # explicit review VERDICT (the shape check above already requires
        # it be a non-blank string to count as evidence at all), so a
        # value outside the small set of recognized approvals -- `"denied"`,
        # `"blocked"`, or any other unrecognized string -- is unrecognized
        # negative evidence, not a silent pass-through. Checking only
        # `{"rejected", "failed"}` let every OTHER negative or unknown
        # state slip through as non-blocking, approving a live order on a
        # review verdict this module never actually understood (Codex
        # review, PR #1169: "Reject unsupported review states").
        _state_value = data.get("state")
        if isinstance(_state_value, str) and _state_value.strip():
            if _state_value.strip().lower() not in _REVIEW_APPROVED_STATES:
                blocking.append(f"review state={_state_value}")
        if data.get("tradeable") is False:
            blocking.append("instrument not tradeable")

        # Structured alerts: block only on error/blocking severity, regardless of container.
        for key in ("alerts", "warnings", "pre_trade_alerts", "messages", "checks"):
            for alert in _as_list(data.get(key)):
                if not isinstance(alert, dict):
                    continue  # plain strings here are informational, not blockers
                # Every supplied severity alias is checked, not just the
                # first truthy one: a raw `severity or level or type` chain
                # trusted only whichever alias came first, so an alert
                # supplying BOTH a benign `severity` and a blocking `level`
                # (e.g. `{"severity": "warning", "level": "critical"}`) read
                # as non-blocking on the benign alias alone even though
                # `level` explicitly marked it critical -- a contradictory
                # broker review could authorize a live order (Codex review,
                # PR #1169: "Check every supplied alert severity alias").
                _severity_values = (alert.get("severity"), alert.get("level"), alert.get("type"))
                _severities = [_v.strip().lower() for _v in _severity_values if isinstance(_v, str) and _v.strip()]
                # A truthy severity value that is NOT a string -- a
                # non-scalar (`{"severity": ["critical"]}`, stringifying to
                # `"['critical']"`) OR a scalar of the WRONG type (`{"severity":
                # 1}`/`{"severity": true}`, stringifying to `"1"`/`"true"`) --
                # matches no recognized blocking spelling either way, so the
                # allowlist above silently dropped it (or, before this fix,
                # stringified a wrong-typed scalar into an unmatched string)
                # and read an alert actually flagged critical as non-blocking.
                # Every other verdict field in this function already fails
                # closed on an unreadable value; an unreadable severity must
                # too, rather than letting a malformed but truthy critical
                # signal approve a live order (Codex review, PR #1168:
                # "Reject malformed structured alert severities" / "Reject
                # non-string alert severity values" -- two rounds of the
                # identical class of gap, this one closing it for every
                # non-string type at once).
                _has_unreadable_severity = any(_v and not isinstance(_v, str) for _v in _severity_values)
                # Tested for TRUTHINESS, not `is True`: a malformed but
                # truthy blocking marker (`"blocking": 1`, `"blocking":
                # "true"`) failed this strict identity check and fell
                # through to the other, unrelated blocking signals below,
                # so an otherwise-affirmative alert (`{"approved": true,
                # "alerts": [{"blocking": 1, ...}]}`) read as non-blocking
                # and permitted the live placement. Every OTHER verdict
                # field in this function already fails closed on a
                # wrong-typed-but-truthy value, so a present `blocking`
                # marker should too (Codex review, PR #1168: "Reject
                # malformed truthy blocking-alert flags").
                is_blocking = (
                    bool(alert.get("blocking"))
                    or bool(alert.get("is_error"))
                    or any(_s in {"error", "blocking", "reject", "rejected", "critical"} for _s in _severities)
                    or _has_unreadable_severity
                )
                if is_blocking:
                    blocking.append(
                        str(alert.get("message") or alert.get("reason") or alert.get("code")
                            or (_severities[0] if _severities else ""))
                    )

        # Non-empty strings in explicitly error-named containers do block.
        for key in ("errors", "error_messages", "rejections"):
            for item in _as_list(data.get(key)):
                if item:
                    # `_log_safe_repr`, not a bare `str(item)`: a truthy
                    # error item that is a native int beyond Python's
                    # integer-to-string conversion digit limit (e.g. a
                    # malformed `10**5000` echoed back by the broker) made
                    # `str(item)` itself raise `ValueError`, aborting
                    # `_interpret_review()` with an unexpected exception
                    # instead of returning the intended blocking verdict --
                    # turning malformed evidence into a placement-batch
                    # halt rather than the fail-closed rejection this
                    # function exists to produce (Codex review, PR #1169:
                    # "Render malformed review errors without unsafe
                    # string conversion").
                    blocking.append(_log_safe_repr(item))

    if blocking:
        return False, "; ".join(dict.fromkeys(blocking))
    if not _chain_ends_cleanly:
        # A malformed trailing value (`{"data": {"success": true, "data":
        # "broker error"}}` -- `_trailing_data == "broker error"`, a
        # nonblank non-dict this module could never actually walk into) is
        # unreadable evidence, not a clean pass. `_chain_ends_cleanly`
        # being False only ever disqualified the empty-response fast path
        # above -- a layer whose own recognized fields (`success: true`
        # here) satisfy the shape check and raise no blocker on their own
        # still fell through to this final "no blocking alerts" approval,
        # silently treating a response this function never finished
        # reading as a clean pass (Codex review, PR #1168: "Reject
        # malformed trailing review data before approval"). Checked LAST,
        # after the real blocking scan, so a genuine blocker elsewhere in
        # the response still reports its own specific reason first.
        return False, f"unreadable review response; malformed trailing data: {_trailing_data!r}"
    return True, "review passed with no blocking alerts"


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
