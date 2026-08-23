"""Neutral, dependency-free contracts shared by every independent
forecasting or signal-suggestion component in this repo.

Design rule, enforced by tests/test_architecture_boundaries.py: this module
must never import ml_forecaster, wondrous_octopus, connector_signals, or
near_close_dca. It is the one shared vocabulary every sibling can depend on
without depending on each other -- and the one thing the live DCA engine
depends on instead of any concrete suggester class. That is the whole
mechanism: near_close_dca depends on an abstraction defined here, never on
a concrete producer package.

Note: introducing a shared cross-cutting layer like this is exactly the
kind of change Section 5 of the AI-assisted development guidelines flags as
needing an ADR ("No new patterns/layers/abstractions without an ADR"). This
file is a proposal for review, not something to merge silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ForecastResult:
    """The minimal, provider-agnostic forecast shape. Wondrous Octopus's
    p10/p50/p90 bands and ml_forecaster's simpler point estimate both fit
    through this without either provider needing to know the other's
    richer or simpler internal shape -- extra detail belongs in a
    provider-specific payload the caller asks for separately, not forced
    into this common type."""

    ticker: str
    asof: date
    horizon_trading_days: int
    point_estimate: float
    source: str
    model_version: str


@dataclass(frozen=True, slots=True)
class SizingSuggestion:
    """An immutable recommendation. Nothing that receives one can mutate
    it, and producing one has no side effects -- computing a suggestion
    and persisting it are two different responsibilities, kept in two
    different classes (see SizingSuggester here vs. SuggestionPublisher in
    publishing.py)."""

    ticker: str
    asof: date
    multiplier_adjustment_pct: float
    eval_metric_mae: float
    source: str
    model_version: str
    generated_at: datetime


@runtime_checkable
class ForecastProvider(Protocol):
    """The broad research-forecast contract. Any component that can
    forecast a ticker on a given day implements this -- including
    Wondrous Octopus, which never goes any further than this."""

    def forecast(self, ticker: str, asof: date) -> ForecastResult: ...


@runtime_checkable
class SizingSuggester(Protocol):
    """A narrower, separate, opt-in contract. Only components explicitly
    wired into the live-sizing suggestion pathway implement this -- today
    that is ml_forecaster and (proposed) connector_signals. Notice what is
    absent: there is no write(), no apply(), no publish(), and no
    parameter anywhere that accepts a reference to the live DCA engine.
    A class cannot reach live sizing through this interface even if
    someone tried, because that capability does not exist on it.

    wondrous_octopus implements ForecastProvider but deliberately does NOT
    implement this -- matching its own README: "It does not influence live
    DCA sizing." That guarantee needs no runtime check; it falls out of
    which Protocol the class does and does not satisfy.
    """

    def suggest(self, ticker: str, asof: date) -> SizingSuggestion: ...
