"""Unsupported-number check: every figure a generated artifact asserts must trace to its source.

Two layers, both pure and stdlib-only:

* **Prose** -- :func:`extract_numbers` / :func:`unsupported_numbers` flag numbers in generated text
  (an alert, a summary, a report paragraph) that never appear in the text it was generated from.
  Unlike ``guardrails/output_guardrail.py`` (word overlap only), this compares NUMERIC VALUES, so
  "$5" is supported by a source saying "5.00" and "$6" is not.
* **Order plans** -- :func:`plan_grounding_errors` ties each plan order's price fields to the
  market payload the plan claims to be built from. ``dca_order_plan._order_errors`` only proves
  those fields are positive and finite; a plan an agent wrote by hand could carry a
  ``decision_price`` that no quote ever showed, and nothing would notice.

ROLLOUT: report-only. Callers record ``reason_codes.UNSUPPORTED_NUMBER`` and surface the findings
but do not block placement unless an explicit, default-off enforce flag is passed (see
``scripts/backup_planner.py --enforce-number-grounding``). That matches the plan-first shadow-mode
stance. Gate 5 candidates are deterministic code and are not checked here.

Nothing here places an order, reads the network, or touches the filesystem.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from dca_order_plan import PlanError

__all__ = [
    "BASE_USD_FALLBACK",
    "DROP_PCT_ABS_TOL",
    "PRICE_REL_TOL",
    "UNSUPPORTED_NUMBER_CODE",
    "extract_numbers",
    "plan_grounding_errors",
    "unsupported_numbers",
]

#: ``PlanError.code`` for every finding. The canonical reason-code spelling lives in
#: ``scripts.reason_codes.UNSUPPORTED_NUMBER``; kept as a literal here so this top-level module
#: never imports from ``scripts/`` (an installed wheel does not ship that tree).
UNSUPPORTED_NUMBER_CODE = "unsupported_number"

#: Relative slack for a price against its source quote. A plan is written seconds-to-minutes after
#: the quote fetch, so a strict equality would flag every live tick; 0.5% is far tighter than the
#: smallest ladder step (1% drop) yet absorbs spread/rounding noise.
PRICE_REL_TOL = Decimal("0.005")
#: Absolute slack, in percentage points, for ``drop_pct`` against its recomputation. The plan
#: rounds drop_pct to 6 decimals, so this only guards representation error.
DROP_PCT_ABS_TOL = Decimal("0.05")
#: Ladder bases accepted when the caller cannot say which one applied: the repo's $1 / $2 scales.
BASE_USD_FALLBACK: tuple[Decimal, ...] = (Decimal("1"), Decimal("2"))

_CENT = Decimal("0.01")
# Optional sign, optional $, digits with comma grouping or plain, optional decimals, optional %.
# A trailing comma/period belongs to the sentence, so the decimal part requires a digit after it.
_NUMBER_RE = re.compile(r"(?<![\w.])([-+]?)\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?%?")


# ---------------------------------------------------------------------------
# Prose
# ---------------------------------------------------------------------------


def extract_numbers(text: str) -> list[Decimal]:
    """Every numeric value in ``text`` in order of appearance (duplicates kept).

    Recognises currency (``$1,234.50``), percentages (``5.5%``), comma-grouped and decimal
    numbers. Values are parsed via ``Decimal(str(x))`` so no float error creeps in. The currency
    sign and percent sign are dropped: the check is on the figure, not its unit.

    Args:
        text: Any string; non-strings yield an empty list.
    """
    if not isinstance(text, str):
        return []
    found: list[Decimal] = []
    for sign, whole, fraction in (m.groups() for m in _NUMBER_RE.finditer(text)):
        literal = f"{sign}{whole.replace(',', '')}{fraction or ''}"
        try:
            found.append(Decimal(str(literal)))
        except InvalidOperation:  # pragma: no cover - the regex only admits parseable literals
            continue
    return found


def unsupported_numbers(source: str, generated: str) -> list[Decimal]:
    """Numbers in ``generated`` whose value does not appear anywhere in ``source``.

    Comparison is by numeric value (``5`` == ``5.00`` == ``$5``). Order and duplicates of the
    generated text are preserved so the caller can quote them back.
    """
    supported = set(extract_numbers(source))
    return [value for value in extract_numbers(generated) if value not in supported]


# ---------------------------------------------------------------------------
# Order plans
# ---------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    """``Decimal(str(value))`` for a real, finite number; ``None`` for anything else (bool included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return Decimal(str(value))


def _price_matches(claimed: Decimal, sources: Iterable[Decimal]) -> bool:
    """True when ``claimed`` equals a source price at cent precision or sits inside the relative band."""
    for source in sources:
        if claimed.quantize(_CENT) == source.quantize(_CENT):
            return True
        if source != 0 and abs(claimed - source) / abs(source) <= PRICE_REL_TOL:
            return True
    return False


def _source_values(payload: Mapping[str, Any], symbol: str, leaf: Callable[[Mapping[str, Any]], Any]) -> list[Decimal]:
    """Every source's value for ``symbol`` via ``leaf``; sources that lack it are skipped silently."""
    sources = payload.get("sources") if isinstance(payload, Mapping) else None
    values: list[Decimal] = []
    if not isinstance(sources, Mapping):
        return values
    for entry in sources.values():
        ticker = entry.get(symbol) if isinstance(entry, Mapping) else None
        if not isinstance(ticker, Mapping):
            continue
        try:
            value = _dec(leaf(ticker))
        except (AttributeError, TypeError):
            value = None
        if value is not None and value > 0:
            values.append(value)
    return values


def _quote_price(ticker: Mapping[str, Any]) -> Any:
    return ticker["quote"]["price"]


def _bar_field(field: str) -> Callable[[Mapping[str, Any]], Any]:
    return lambda ticker: ticker["previous_close_bar"][field]


def _flag(symbol: str, field: str, why: str) -> PlanError:
    return PlanError(UNSUPPORTED_NUMBER_CODE, f"{symbol}.{field}: {why}")


def plan_grounding_errors(
    plan: Mapping[str, Any],
    payloads: Mapping[str, Any],
    *,
    base_usd_for: Callable[[Mapping[str, Any]], float | None] | None = None,
) -> list[PlanError]:
    """Findings (one ``PlanError`` each) for plan numbers the market payload does not support.

    Checks per order:

    * ``decision_price`` vs ``sources[*][symbol].quote.price`` (cent match or ``PRICE_REL_TOL``).
    * ``prior_close`` / ``prior_low`` vs ``sources[*][symbol].previous_close_bar.close`` / ``.low``.
    * ``drop_pct`` vs ``(prior_close - decision_price) / prior_close * 100`` from the plan's OWN
      two prices (``DROP_PCT_ABS_TOL``), so a doctored drop that would inflate the multiplier fails
      even when both prices are individually plausible.
    * ``notional_usd`` vs ``multiplier`` x ladder base, to the cent.

    A symbol with no payload data at all is reported once as unsupported (a price nothing can
    vouch for is exactly the case this exists for). Never raises; malformed input is a finding.

    Args:
        plan: A decoded ``dca_order_plan`` document (``orders`` list).
        payloads: The assembled market payload (``MarketSnapshot.payload`` shape:
            ``{"sources": {name: {SYMBOL: {previous_close_bar, quote, ...}}}, ...}``).
        base_usd_for: Optional ``order -> base dollars`` (the caller's own call into
            ``near_close_dca.select_first_allocation_base_usd``). Without it the $1 / $2 scales in
            :data:`BASE_USD_FALLBACK` are both accepted.

    Returns:
        Findings, empty when everything is supported.
    """
    orders = plan.get("orders") if isinstance(plan, Mapping) else None
    if not isinstance(orders, list):
        return [PlanError(UNSUPPORTED_NUMBER_CODE, "plan has no orders list to ground")]
    findings: list[PlanError] = []
    for order in orders:
        if not isinstance(order, Mapping) or not isinstance(order.get("symbol"), str):
            findings.append(PlanError(UNSUPPORTED_NUMBER_CODE, "order is not an object with a symbol"))
            continue
        symbol = order["symbol"]
        decision, prior_close, prior_low = (_dec(order.get(k)) for k in ("decision_price", "prior_close", "prior_low"))

        quotes = _source_values(payloads, symbol, _quote_price)
        closes = _source_values(payloads, symbol, _bar_field("close"))
        if not quotes and not closes:
            findings.append(_flag(symbol, "decision_price", "no market-payload data for this symbol"))
        else:
            if decision is not None and quotes and not _price_matches(decision, quotes):
                findings.append(_flag(symbol, "decision_price", "does not match payload quote.price"))
            if prior_close is not None and closes and not _price_matches(prior_close, closes):
                findings.append(_flag(symbol, "prior_close", "does not match payload previous_close_bar.close"))
        if prior_low is not None:
            lows = _source_values(payloads, symbol, _bar_field("low"))
            if lows and not _price_matches(prior_low, lows):
                findings.append(_flag(symbol, "prior_low", "does not match payload previous_close_bar.low"))

        drop = _dec(order.get("drop_pct"))
        if drop is not None and decision is not None and prior_close:
            recomputed = (prior_close - decision) / prior_close * 100
            if abs(drop - recomputed) > DROP_PCT_ABS_TOL:
                findings.append(_flag(symbol, "drop_pct", f"recomputes to {recomputed.quantize(Decimal('0.0001'))}"))

        notional, multiplier = _dec(order.get("notional_usd")), _dec(order.get("multiplier"))
        if notional is not None and multiplier is not None:
            if base_usd_for is not None:
                base = _dec(base_usd_for(order))
                bases: Sequence[Decimal] = (base,) if base is not None else BASE_USD_FALLBACK
            else:
                bases = BASE_USD_FALLBACK
            if not any((b * multiplier).quantize(_CENT) == notional.quantize(_CENT) for b in bases):
                findings.append(_flag(symbol, "notional_usd", "is not multiplier x ladder base"))
    return findings
