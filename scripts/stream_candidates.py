#!/usr/bin/env python3
"""Shared Gate 5 machinery for the executor-class streams.

WHAT THIS IS FOR
----------------
Four of the seven roster slots -- market-open (0935ET), morning (1030ET),
midday (1230ET) and intraday (1130/1330/1430ET) -- run their signal generation
through ``morning_dca.MorningDCAExecutor.evaluate()``, as specialized by a
per-stream subclass. Only the 3:30 PM final buy runs on
``near_close_dca.evaluate_source_signal`` (that stream's own engine, and what
``scripts/generate_buy_candidates.py`` calls).

That means a Gate 5 entry point for any of those four is the SAME code with a
different executor class, config loader, and slot identity bolted on. This
module is that code, exactly once. Each slot's entry point --
``scripts/generate_market_open_candidates.py`` (0935ET),
``scripts/generate_morning_candidates.py`` (1030ET),
``scripts/generate_intraday_candidates.py`` (1130/1330/1430ET, one module with a
``--slot`` selector, since one executor class serves all three) and
``scripts/generate_midday_candidates.py`` (1230ET) -- is a :class:`StreamSpec`
plus a thin CLI wrapper; nothing about the signal path, the payload contract, the
fail-closed input validation, or the output document is written twice.

The alternative -- copying one slot's module and editing the constants -- is how
the three entry points in ``CLAUDE.md``'s "Three entry points coexist" warning
came to drift from each other. A fix applied to one copy does not reach the
others, and neither does a review round's hardening.

WHAT THIS IS NOT
----------------
Gate 5 and nothing past it. Every function here calls the executor's
``evaluate()``, never its ``run()``:

* ``run()`` owns budget arithmetic, the same-day top-up guard, broker placement,
  drift detection and the artifact tree. Those are Gates 6 through 9, and on
  these slots they belong to the executing agent's own gate sequence. Wiring
  ``run()`` in as a "candidate producer" would mean two independent
  implementations of Gates 6/6.5/7 running against the same account in one fire
  -- which is precisely the open question
  ``claude/dca-executor-prompts/live-mirror/1230ET-*.txt``'s Gate 5 names as
  needing a human decision.
* ``evaluate()`` needs no broker, no credentials and no network, writes nothing,
  and cannot place an order. That is why it, and not ``run()``, is what a Gate 5
  producer should call.

Because ``run()`` is never reached, a stream config's ``live_execution_enabled``
switch -- which gates placement INSIDE ``run()`` -- has no effect on anything
here. Its value is reported in the output document so a reader can see it, but
it never suppresses a candidate.

MARKET DATA
-----------
Bars and quotes arrive as an already-fetched JSON file: the caller (an agent
session holding real Robinhood access) fetches them with its own tools and hands
them over. A subprocess cannot receive a live Python callable across its
boundary, only data -- the same reason ``scripts/same_day_top_up_gate.py`` takes
``--orders-pages`` rather than a broker object.

The payload is parsed by ``scripts.generate_buy_candidates._StaticMarketData``,
imported rather than reimplemented, so every entry point in this repo accepts and
rejects exactly the same market-data files: non-finite/boolean/non-positive
prices, naive timestamps, duplicate intraday timestamps, internally inconsistent
OHLC bars, previous-close bars not dated the immediately previous market session,
and future-dated or unverifiable quotes all fail closed, in one place.

Standard library only, save for the in-repo imports below.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from dca_config import get_config_dir, load_strategy_config
from morning_dca import (
    EXEC_AUTO_BUY_ELIGIBLE,
    EXEC_MANUAL_REVIEW_CANDIDATE,
    MorningSignal,
    completed_bar_anchor,
)
from near_close_dca import (
    BASE_BELOW_CLOSE_AND_LOW,
    EASTERN,
    Action,
    SourceName,
    kill_switch_engaged,
)

# Reused, not re-derived. These are this repo's single definition of "a valid
# pre-fetched market-data payload" and "a candidate row dca_runner.plan_orders()
# would actually accept" -- both learned through several review rounds on the
# 1530ET entry point. Copying either would create a second contract that drifts
# from the first the next time one of those rounds happens. They are private to
# that module only because it had no second caller until now.
from scripts.generate_buy_candidates import (  # noqa: E402
    _REQUIRED_CONFIG_FILENAMES as _SHARED_REQUIRED_CONFIG_FILENAMES,
)
from scripts.generate_buy_candidates import (  # noqa: E402
    _planner_incompatible,
    _require_populated_config,
    _StaticMarketData,
)

__all__ = [
    "StreamSpec",
    "StreamMarketData",
    "build_document",
    "run_cli",
    "run_multi_slot_cli",
    "required_config_filenames",
]


@dataclass(frozen=True)
class StreamSpec:
    """Everything that differs between one executor-class slot and another.

    Deliberately data, not subclassing: a new slot is a literal here plus a
    six-line CLI module, and a reader can diff two specs side by side to see the
    whole difference between two slots.
    """

    slot: str
    """The slot label the prompts and artifact folder names use, e.g. "0935ET"."""

    runner_slot_id: str
    """``dca_runner.SLOTS``'s id for the same slot (no "ET" suffix). Published
    directly because ``dca_runner.assess()``/``execute_plan()`` fail loud on an
    unrecognized slot_id, so a caller feeding the document's ``slot`` straight
    into that shared path would abort on the mismatch even though Gate 5
    succeeded (Codex review, 1530ET module: "Publish the runner slot id")."""

    stream: str
    cron_id: str
    executor_cls: type
    config_loader: Callable[..., Any]
    live_mode: str
    stream_config_filename: str
    """The stream's own config/*.json. Added to the shared required-file list
    because every ``load_*_config`` degrades to hardcoded dataclass DEFAULTS
    (with only a log warning) when its file is missing -- the same silent
    fallback ``_require_populated_config`` exists to prevent for the others."""

    source: SourceName = SourceName.ROBINHOOD
    """The single market-data source this slot's executor evaluates.
    ``MorningDCAExecutor.evaluate()`` takes ONE combined provider, so there is no
    second source to reconcile against and none is accepted.

    KNOWN DIVERGENCE, open for a human decision -- do not "fix" this by flipping
    the default. ``config/scheduled_tasks.json``'s ``market_data_source_policy``
    names **Finance** the primary market-data source, with Robinhood as
    validation/context (GFUZ excepted), and ``config/midday_dca.json``'s
    ``raw_input_generation.data_sources`` repeats that for the midday stream's
    raw inputs. Serving these entry points from Robinhood therefore does not
    match that policy's letter, and it was flagged in review ("Preserve the
    Finance-primary signal source").

    Two facts make it not a one-line change:

    1. **Cadence.** ``StrategyConfig.finance_interval_minutes`` is 15;
       ``robinhood_interval_minutes`` is 5. At the 09:35 anchor the first
       15-minute bar does not COMPLETE until 09:45, so a Finance-primary
       market-open payload cannot produce a completed decision bar at all --
       Finance-primary is impossible for 0935ET, not merely undesirable.
    2. **One interval per provider.** ``MorningConfig.interval_minutes`` is a
       single value (default 5, the Robinhood cadence; neither stream config
       overrides it) used for the request window, the completed-bar anchor and
       the staleness check. Feeding 15-minute Finance bars to an executor
       configured for 5 would have its own staleness rule reject them.

    So honouring Finance-primary here means deciding a per-source interval for
    this executor family, and for the market-open slot deciding what "primary"
    can even mean five minutes into the session. That is a live-sizing policy
    call with money attached, not a refactor, and it is deliberately NOT made in
    the change that introduced these entry points."""

    requires_deployed_last_7d_usd: bool = False
    """Whether this slot's Gate 5 producer contract requires the trailing-7-day
    deployment figure that funds the kill switch. See
    :func:`_guarded_sizing_config` for why this is per-slot rather than uniform."""

    universe_filter: Optional[Callable[[date, Sequence[str], Any], Sequence[str]]] = None
    """Narrows the scored universe the way this stream's executor will, BEFORE
    payload validation and parsing.

    ``(trading_day, tickers, config) -> admitted tickers``. ``None`` on every
    stream whose executor scores the whole configured universe, which is all of
    them except 1030ET: ``MorningDCAExecutor.evaluate()`` first restricts itself
    to the same-day 09:45 pre-flight verdict, so a payload for a gated-out ticker
    is evidence the executor never reads. Validating and parsing it anyway let
    one malformed gated-out payload abort a run the executor would have completed
    (Codex review: "Apply preflight exclusions before parsing payloads").

    Takes the trading DAY rather than a datetime because that is all the
    pre-flight lookup needs, and the executor's decision anchor is not available
    here -- the executor is not constructed until after this narrowing has
    already decided which payloads to parse.
    """

    executor_kwargs: Dict[str, Any] = dataclasses_field(default_factory=dict)
    """Extra keyword arguments the executor's constructor needs beyond
    config/mode. Only the intraday stream uses this today: one executor class
    serves three slots (11:30/1:30/2:30) under a single cron, selected by
    ``slot=``. Keeping it a spec field rather than a subclass hook means the
    three intraday slots stay three literals rather than three modules."""

    doc_ref: str = ""


# The policy this entry point depends on actually being present in a stream
# config -- see _require_parseable_stream_config for why existence and valid
# JSON are not enough on their own.
_REQUIRED_STREAM_CONFIG_KEYS: tuple[str, ...] = (
    "stream",
    "live_task",
    "sizing_ladder",
    "budget_ledger",
)


def required_config_filenames(spec: StreamSpec) -> tuple[str, ...]:
    return _SHARED_REQUIRED_CONFIG_FILENAMES + (spec.stream_config_filename,)


def _require_config_number(value: Any, label: str, *, positive: bool) -> None:
    """Reject anything that is not a real, finite number in a config policy field.

    ``bool`` is rejected FIRST, for the third time in this module and for the
    same reason each time: ``bool`` subclasses ``int``, so a JSON ``true``
    satisfies every truthiness and numeric test and then converts through
    ``float(...)`` to 1.0. In a sizing ladder that silently halves the checked-in
    $2.00 base; in a budget ledger it silently replaces a $180 cap with $1.00.
    The earlier revision of this validator only checked truthiness, so
    ``"base_amount_usd": true`` passed and published every full-base candidate at
    half size with nothing reporting a policy substitution (Codex review:
    "Reject boolean values in the stream sizing ladder"; reproduced loading
    $1.00 where the checked-in value is $2.00).
    """
    if isinstance(value, bool):
        raise ValueError(
            f"{label} must be a number, got the boolean {value!r}: Python treats bool as an int, "
            f"so the loader would read it as {float(value)} and substitute that for the "
            "checked-in policy without reporting anything"
        )
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    if positive and value <= 0:
        raise ValueError(f"{label} must be greater than zero, got {value!r}")
    if not positive and value < 0:
        raise ValueError(f"{label} must not be negative, got {value!r}")


def _require_parseable_stream_config(path: Path, expected_stream: str) -> None:
    """Reject a stream config that exists but cannot be trusted.

    ``_require_populated_config`` validates the SHARED file list it was written
    for; it has no idea this module appended a stream-specific filename, so that
    file was only ever checked for existence. Every ``load_*_config`` treats
    unparseable exactly like absent -- a logged warning and the dataclass
    DEFAULTS -- so a damaged or duplicate-keyed ``market_open_dca.json`` /
    ``midday_dca.json`` produced a full, exit-0 candidate document sized by the
    hardcoded fallback policy instead of the checked-in one, with nothing in the
    output saying so (Codex review: "Reject malformed market-open
    configuration", reproduced against this CLI).

    Duplicate keys are rejected for the same reason ``config/*.json`` reads
    elsewhere reject them: plain ``json.loads`` silently keeps only the last
    occurrence, so two ``live_execution_enabled`` or ``sizing_ladder`` entries
    would discard one without a trace. A valid-JSON-but-not-an-object file is
    rejected too, matching ``dca_config``'s own fail-closed loading contract.
    """
    import dca_runner  # deferred -- see run_cli's config-dir injection

    try:
        parsed = json.loads(path.read_bytes(), object_pairs_hook=dca_runner._reject_duplicate_keys)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{path} is unreadable, malformed, or has a duplicate JSON key: {exc}. Refusing to "
            "score with this stream's hardcoded dataclass defaults, which is what loading it "
            "would silently fall back to"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"{path} must contain a JSON object, got {type(parsed).__name__}; loading it would "
            "silently fall back to this stream's hardcoded dataclass defaults"
        )
    # Parsing is not the same as APPLYING. `{}` is a syntactically valid JSON
    # object, so the type check above passes it, and every load_*_config then
    # builds its hardcoded dataclass defaults from it without complaint -- the
    # same silent-fallback outcome the malformed-file check exists to prevent,
    # reached through a file that merely lost its contents rather than its
    # syntax (Codex review: "Reject empty stream configuration objects"). These
    # four keys are the policy this entry point actually depends on: `stream`
    # identifies it, `live_task` carries the slot identity, `sizing_ladder` the
    # ladder and bases, `budget_ledger` the caps. A file missing any of them is
    # not this stream's checked-in policy, whatever else it contains.
    missing_policy_keys = [key for key in _REQUIRED_STREAM_CONFIG_KEYS if key not in parsed]
    if missing_policy_keys:
        raise ValueError(
            f"{path} is missing required policy key(s) {missing_policy_keys!r}. A syntactically "
            "valid but hollow stream config loads as this stream's hardcoded dataclass defaults, "
            "so Gate 5 would emit real candidates sized by fallback policy while appearing to "
            "have used the checked-in one"
        )
    # A config for the WRONG stream is the same failure wearing a different
    # hat: it loads cleanly and sizes from another slot's ladder and caps.
    declared_stream = parsed.get("stream")
    if declared_stream != expected_stream:
        raise ValueError(
            f"{path} declares stream {declared_stream!r}, but this entry point is "
            f"{expected_stream!r}. Refusing to score one slot's signals against another's "
            "checked-in policy"
        )

    # PRESENCE is not CONTENT. The required-key check above accepts
    # `{"stream": ..., "live_task": {}, "sizing_ladder": {}, "budget_ledger": {}}`
    # -- four required keys, every one of them hollow -- and each load_*_config
    # then reads nothing out of the empty objects and keeps its hardcoded
    # dataclass defaults, which is the identical silent-fallback outcome one
    # level deeper (Codex review: "Validate nested stream policies before
    # loading"; reproduced against this CLI, where that file sized a 5% drop at
    # 1x/$1.00 instead of the checked-in 2.5x/$5.00, exit 0, with nothing in the
    # document saying which policy it used).
    #
    # What is validated is deliberately the MONEY, not the whole schema: the
    # ladder that decides how much to buy and the caps that bound it. Anything
    # broader would duplicate schemas/ and drift from it; anything narrower
    # leaves a hollow block that changes sizing.
    hollow_blocks = sorted(
        key
        for key in _REQUIRED_STREAM_CONFIG_KEYS
        if isinstance(parsed[key], (dict, list, str)) and not parsed[key]
    )
    if hollow_blocks:
        raise ValueError(
            f"{path} has empty required policy block(s) {hollow_blocks!r}. Present-but-empty is "
            "not the same as configured: the loader reads nothing out of them and silently keeps "
            "this stream's hardcoded dataclass defaults, so Gate 5 would size real money from "
            "fallback policy while the run's evidence claims the checked-in config"
        )
    ladder = parsed["sizing_ladder"]
    if not isinstance(ladder, dict):
        raise ValueError(f"{path}'s 'sizing_ladder' must be an object, got {type(ladder).__name__}")
    # base_amount_usd and tiers are the two halves of `base * multiplier`. A
    # ladder carrying only one of them loads the OTHER from defaults, which is
    # the same wrong-policy sizing arrived at by a partial edit rather than an
    # emptied file.
    missing_ladder = [k for k in ("base_amount_usd", "tiers") if not ladder.get(k)]
    if missing_ladder:
        raise ValueError(
            f"{path}'s 'sizing_ladder' is missing or empty for {missing_ladder!r}. Both halves of "
            "`base amount x ladder multiplier` must come from the checked-in config; a ladder "
            "supplying only one silently takes the other from hardcoded defaults"
        )
    # Present is not the same as USABLE. Each of these is read straight through
    # `float(...)` by every load_*_config, so a malformed value substitutes
    # policy rather than failing.
    _require_config_number(ladder["base_amount_usd"], f"{path}'s sizing_ladder.base_amount_usd",
                           positive=True)
    for optional in ("base_amount_below_close_usd", "no_buy_below_pct"):
        if optional in ladder:
            _require_config_number(
                ladder[optional], f"{path}'s sizing_ladder.{optional}",
                # A 0.0 minimum-drop floor is meaningful (buy on any drop); a
                # 0.0 base amount is not, it is an order for nothing.
                positive=optional != "no_buy_below_pct",
            )
    tiers = ladder["tiers"]
    if not isinstance(tiers, list):
        raise ValueError(f"{path}'s 'sizing_ladder.tiers' must be a list, got {type(tiers).__name__}")
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            raise ValueError(
                f"{path}'s 'sizing_ladder.tiers[{index}]' must be an object, got "
                f"{type(tier).__name__}"
            )
        for key in ("min_drop_pct", "multiplier"):
            if key not in tier:
                raise ValueError(f"{path}'s 'sizing_ladder.tiers[{index}]' has no {key!r}")
            _require_config_number(
                tier[key], f"{path}'s sizing_ladder.tiers[{index}].{key}",
                # A 0x tier is a real "do not buy" rung; a negative drop is not.
                positive=False,
            )
    # trend_min_bars is top-level, not inside a policy block, and it is the one
    # non-monetary field that decides whether a real order is placed: every
    # loader applies `int(raw["trend_min_bars"])`, so `true` loads as 1 and a
    # "60-minute downtrend" can be validated on a SINGLE bar -- passing both the
    # executor's trend test and this module's own coverage probe, and publishing
    # an actionable candidate against a checked-in six-bar minimum (Codex review:
    # "Validate the configured trend bar minimum"; reproduced loading 1 where the
    # checked-in value is 6).
    #
    # The earlier revision of this validator scoped itself to "the MONEY, not the
    # whole schema". That was the wrong line to draw: this field gates placement
    # as directly as the ladder does, it just does not carry a dollar sign.
    if "trend_min_bars" in parsed:
        value = parsed["trend_min_bars"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                f"{path}'s 'trend_min_bars' must be an integer of at least 1, got {value!r}"
                + (
                    ": Python treats bool as an int, so the loader would read it as "
                    f"{int(value)} and validate a 60-minute downtrend on that many bars"
                    if isinstance(value, bool)
                    else ""
                )
            )

    ledger = parsed["budget_ledger"]
    if not isinstance(ledger, dict):
        raise ValueError(f"{path}'s 'budget_ledger' must be an object, got {type(ledger).__name__}")
    # The EXACT keys this stream's loader reads, derived from the stream name --
    # every stream spells its caps `<stream>_daily_cap_usd` /
    # `<stream>_weekly_cap_usd`, so this needs no per-stream table and cannot go
    # stale for a stream it has not learned about.
    #
    # An earlier revision matched by SUFFIX instead, which was too loose in the
    # one direction that matters: a midday_dca.json whose ledger carried only
    # `market_open_daily_cap_usd`/`market_open_weekly_cap_usd` -- a partially
    # copied or damaged file -- satisfied `*_daily_cap_usd` while
    # load_midday_config() ignored those keys entirely and kept its hardcoded
    # defaults. The validation then claimed the checked-in money policy had been
    # applied when the caps bounding a real batch were the fallback ones (Codex
    # review: "Honor each stream's configured cap keys"). The `stream` identity
    # check above does not cover it: that file still declares stream "midday".
    for kind in ("daily", "weekly"):
        key = f"{expected_stream}_{kind}_cap_usd"
        if ledger.get(key) is not None:
            # Same bool/float trap as the ladder above: a JSON `true` here is not
            # None, so the presence check below passes it, and the loader then
            # reads a $1.00 cap in place of the checked-in $180.
            _require_config_number(ledger[key], f"{path}'s budget_ledger.{key}", positive=True)
        if ledger.get(key) is None:
            present = sorted(k for k in ledger if k.endswith("_cap_usd"))
            raise ValueError(
                f"{path}'s 'budget_ledger' has no {key!r} value (cap keys present: {present!r}). "
                f"That is the exact key this stream's loader reads; without it the loader keeps "
                "this stream's hardcoded default cap, so the ceiling bounding a real batch is not "
                "the one in the checked-in config -- and another stream's cap keys do not "
                "substitute, because the loader never looks at them"
            )


class StreamMarketData(_StaticMarketData):
    """``_StaticMarketData`` plus per-ticker provider errors.

    ``_StaticMarketData`` models a ticker the caller never fetched (absent from
    the per-ticker map) but has no way to model a ticker whose fetch RAISED. On
    the 1530ET path that distinction is carried by ``SourceSignal.provider_error``
    through ``reconcile_signals``; ``MorningDCAExecutor.evaluate_signal`` has no
    such field and instead learns it the way a live run does -- by the provider
    itself raising, which it catches and records as a
    ``"previous_close provider error: ..."`` warning before returning ``NO_DATA``.

    So an errored ticker is modelled here as a provider that raises for it. That
    is not a simulation of the live failure: it IS the live failure shape.
    Without it, a single-symbol fetch exception would be indistinguishable from a
    symbol this source legitimately does not cover, and the run's own evidence
    would show a clean, quiet result for a ticker whose data actually broke.
    """

    def __init__(self, *args: Any, ticker_errors: Sequence[str] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ticker_errors = frozenset(ticker_errors)

    def _raise_if_errored(self, ticker: str) -> None:
        if ticker in self._ticker_errors:
            raise RuntimeError(
                f"{self.source.value} fetch failed for {ticker} "
                "(reported in --market-data's ticker_errors)"
            )

    def get_previous_close_bar(self, ticker: str, as_of: datetime):
        self._raise_if_errored(ticker)
        return super().get_previous_close_bar(ticker, as_of)

    def get_intraday_bars(self, ticker: str, start: datetime, end: datetime, interval_minutes: int):
        self._raise_if_errored(ticker)
        return super().get_intraday_bars(ticker, start, end, interval_minutes)

    def get_quote(self, ticker: str, as_of: datetime):
        self._raise_if_errored(ticker)
        return super().get_quote(ticker, as_of)


def _validate_tickers(
    label: str,
    tickers: Sequence[str],
    known: Sequence[str],
    *,
    allow_extra: frozenset[str] = frozenset(),
) -> None:
    """Reject a ticker list naming a symbol this config has never heard of.

    A typo or a stale hand-maintained list is otherwise silent: an unknown
    ``ticker_errors`` entry marks nothing as errored, and an unknown
    ``--already-covered-tickers`` entry excludes nothing. In both cases the
    caller's intended fail-closed effect quietly does not happen.
    """
    if isinstance(tickers, (str, bytes)) or not isinstance(tickers, Sequence):
        raise ValueError(f"{label} must be a list of ticker strings, got {type(tickers).__name__}")
    permitted = set(known) | set(allow_extra)
    unknown = [t for t in tickers if not isinstance(t, str) or t not in permitted]
    if unknown:
        raise ValueError(f"{label} names ticker(s) not in this config's active universe: {unknown!r}")


def _guarded_sizing_config(
    executor: Any, config_dir: Optional[Path], trading_day: date, deployed_last_7d_usd: float
):
    """The executor's own sizing view with the 7-day kill-switch inputs injected.

    ``MorningConfig.as_strategy_config()`` builds its ``StrategyConfig`` from the
    ladder fields only -- no ``deployed_last_7d_usd``, no
    ``monthly_cash_allocation_usd``, no ``kill_switch_7d_deploy_fraction`` -- so
    the cap baked into ``near_close_dca.graded_buy_multiplier`` can never fire on
    these streams' own live path. That is deliberate for streams whose Gate 5
    contract does not ask for it, and this function is not called for them.

    Where a slot's producer contract DOES require the guard (see
    ``StreamSpec.requires_deployed_last_7d_usd`` and the 1230ET prompt's Gate 5:
    "the producer MUST apply dca_risk.apply_risk_guard ... nothing downstream of
    Gate 5 in this prompt enforces this"), the guard is applied by handing the
    real ladder function the real inputs, NOT by post-processing a multiplier
    this module capped itself. ``graded_buy_multiplier`` already implements the
    cap; recomputing it here would be a second copy of the one piece of sizing
    math the prompt singles out as unenforced anywhere else.

    The allocation and fraction come from ``load_strategy_config`` (the same
    ``config/budgets.json``-derived values the final-buy stream's guard uses), so
    the threshold is the account's real one rather than a per-stream invention.
    Only the three risk fields are replaced; the ladder, calibration, bases and
    minimum drop stay exactly as the stream configured them.

    Note the direction of the effect: the kill switch only ever CAPS a
    multiplier. A run through this path can size at or below what the stream's
    own ``run()`` would have produced, never above.
    """
    stream_sizing = executor.sizing_config
    now = datetime.combine(trading_day, executor.decision_time, tzinfo=EASTERN)
    # ``config_dir=None`` is the documented default on every public entry path
    # here, but load_strategy_config's first act is ``Path(config_dir)``, which
    # raises TypeError on None before a single ticker is evaluated. The CLI
    # always passes a path, so only a direct caller taking the advertised
    # default hit it -- an unusable default is still a broken API (Codex review:
    # "Make the optional config directory usable"). Resolved the same way every
    # other loader in this repo resolves an omitted directory.
    account_risk = load_strategy_config(
        get_config_dir() if config_dir is None else Path(config_dir),
        now=now,
        deployed_last_7d_usd=deployed_last_7d_usd,
    )
    return dataclasses.replace(
        stream_sizing,
        deployed_last_7d_usd=deployed_last_7d_usd,
        monthly_cash_allocation_usd=account_risk.monthly_cash_allocation_usd,
        kill_switch_7d_deploy_fraction=account_risk.kill_switch_7d_deploy_fraction,
    )


def _apply_sizing_config(executor: Any, sizing_config: Any) -> None:
    """Point the executor's sizing property at ``sizing_config``.

    ``MorningDCAExecutor.sizing_config`` is a read-only property over
    ``_sizing_config``, which ``__init__`` derives from the stream config. Both
    ``multiplier_for_drop`` (-> ``graded_buy_multiplier``) and
    ``select_first_allocation_base_usd`` read it through that property, so
    replacing the underlying value is what makes the real signal path see the
    guard inputs -- as opposed to grading normally and then editing the result.
    """
    executor._sizing_config = sizing_config


def _signal_to_row(signal: MorningSignal) -> Dict[str, Any]:
    """One evaluated ticker, in the same vocabulary the 1530ET entry point
    publishes, so a Gate 6/6.5/9 caller reads either document the same way.

    ``buy_multiplier``/``suggested_amount_usd``/``target_price`` are the 1530ET
    names for what ``MorningSignal`` calls ``multiplier``/
    ``hypothetical_amount_usd``/``current_price``. "hypothetical" there is a
    watchlist-mode legacy label, not a statement about this run: in live mode it
    is the amount the executor's own placement path would submit (subject to
    Gates 6/6.5), which is exactly what ``suggested_amount_usd`` means on the
    1530ET side.

    ``multiplier_drop_pct`` and ``drop_vs_previous_close_pct`` are the same
    number here, unlike on the dual-source 1530ET path where the allocation-sort
    drop is the MAX across sources and can disagree with the winning source's
    own. Both keys are emitted anyway so one shape of row-reading code works for
    either slot rather than branching on which produced the document.
    """
    return {
        "ticker": signal.ticker,
        "action": signal.action.value,
        "reason": signal.reason,
        "source": signal.source,
        "buy_multiplier": signal.multiplier,
        "suggested_amount_usd": round(signal.hypothetical_amount_usd, 2),
        "selected_base_amount_usd": signal.selected_base_amount_usd,
        "base_selection_reason": signal.base_selection_reason,
        "target_price": signal.current_price,
        "target_price_source": signal.source,
        "previous_close": signal.previous_close,
        "previous_low": signal.previous_low,
        "drop_vs_previous_close_pct": signal.drop_pct,
        "multiplier_drop_pct": signal.drop_pct,
        "execution_status": signal.execution_status,
        "auto_buy_signal": signal.execution_status == EXEC_AUTO_BUY_ELIGIBLE,
        # None on a trend-exempt slot (the trend was never evaluated, which is
        # different from evaluated-and-failed); True/False where the stream does
        # validate it. Emitted either way so the evidence shows which.
        "trend_validated": signal.trend_validated,
        # The executor records a provider exception as a warning string and
        # returns NO_DATA (see StreamMarketData). Surfacing it as its own boolean
        # means Gate 9 can report "N tickers had provider errors" without
        # string-matching prose.
        "provider_error": any("provider error" in w for w in signal.warnings),
        "warnings": list(signal.warnings),
    }


def _calibration_fields(ticker: str, multiplier: float, selected_base_usd: float) -> Dict[str, Any]:
    """``sizing_base_usd``/``sizing_min_drop_pct``/``sizing_max_drop_pct`` for a
    calibrated candidate, or ``{}`` for an uncalibrated one.

    Read through ``dca_runner._configured_calibration_bounds`` -- the SAME
    function ``plan_orders()`` validates these fields against -- rather than
    re-deriving the tier bounds from ``config/per_ticker_sizing.json`` here. A
    second derivation could disagree with the validator over exactly the rows
    this exists to make placeable, which is the failure it is meant to prevent.

    Emitted only when the selected base is actually one of the ticker's
    configured bases: a mismatch means the calibration evidence would not survive
    the downstream check anyway, and publishing fields that fail validation is
    worse than publishing none (the row is then treated as uncalibrated, which is
    the conservative reading).
    """
    import dca_runner  # deferred -- see run_cli's config-dir injection

    bounds = dca_runner._configured_calibration_bounds(ticker, multiplier)
    if bounds is None:
        return {}
    configured_bases, configured_min, configured_max, _minimum_order_usd = bounds
    if selected_base_usd not in configured_bases:
        return {}
    return {
        "sizing_base_usd": selected_base_usd,
        "sizing_min_drop_pct": configured_min,
        "sizing_max_drop_pct": configured_max,
    }


def _require_matching_runner_config_dir(config_dir: Optional[Path]) -> None:
    """Raise when this run would score from one config directory while
    ``dca_runner`` validates against another.

    ``dca_runner`` reads ``CONFIG_DIR = get_config_dir()`` at import time with no
    per-call override, and three things in a run reach it: the previous-trading-
    day calendar check inside ``_StaticMarketData``, the ladder validation in
    ``_planner_incompatible``, and the calibration bounds in
    ``_calibration_fields``. The calendar one is the dangerous member -- its
    verdict is neither withheld nor reported anywhere in the document, so
    top-up rows built on a wrong-calendar baseline still look actionable.
    """
    import dca_runner  # deferred -- see run_cli's config-dir injection

    selected = get_config_dir() if config_dir is None else Path(config_dir)
    if selected.resolve() != dca_runner.CONFIG_DIR.resolve():
        raise ValueError(
            f"config_dir {selected} does not match the directory dca_runner already resolved at "
            f"import time ({dca_runner.CONFIG_DIR}). dca_runner reads its config once, at import, "
            "with no per-call override, so this run would validate the market calendar, sizing "
            "ladder and per-ticker calibration against a DIFFERENT directory than the one it "
            "scores from. Set $DCA_CONFIG_DIR to the same directory before this process starts, "
            "or run in a fresh process"
        )


def build_document(
    spec: StreamSpec,
    *,
    trading_day: date,
    config: Any,
    market_data: Mapping[str, Mapping[str, Any]],
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    deployed_last_7d_usd: Optional[float] = None,
    config_dir: Optional[Path] = None,
    check_planner_compatibility: bool = True,
) -> Dict[str, Any]:
    """Run ``spec``'s executor over ``market_data`` and return its Gate 5 document.

    Validation lives HERE, not only in :func:`run_cli`, because this function is
    public and directly importable: a caller invoking it instead of the CLI must
    get the same fail-closed guarantees, not a quieter subset of them (Codex
    review, 1530ET module: "Validate error markers in the public helper").

    ``already_covered_tickers`` is Gate 4's compatible-stand-down case (the
    live-mirror prompts: "generate this slot's candidate list EXCLUDING every
    ticker the compatible executor already placed an order for in THIS SAME
    window"). Those tickers are added to the executor's own excluded set before
    it evaluates, so they are never scored at all rather than scored and then
    filtered -- ``config/budgets.json``'s ``one_order_per_ticker_per_stream``
    safeguard blocks a second buy for them in this window regardless of size, so
    publishing a row Gate 6.5 might be asked to resize would be publishing an
    order that is not permitted at any amount.
    """
    # dca_runner resolves CONFIG_DIR once, at ITS OWN import, and
    # _StaticMarketData's previous-trading-day calendar check reads it -- so a
    # caller scoring from one directory while dca_runner holds another can have
    # a stale previous-close bar accepted, or a correct one rejected, against
    # the wrong holiday calendar. run_cli() guards that, but this helper is
    # public and directly importable, and check_planner_compatibility does not
    # cover the calendar (Codex review: "Validate the config directory in direct
    # helper calls"). Same check, same fail-closed answer, on every entry path.
    _require_matching_runner_config_dir(config_dir)

    universe = tuple(t for t in config.tickers if t not in config.excluded_tickers)
    _validate_tickers(
        f"--market-data's 'ticker_errors.{spec.source.value}'", ticker_errors, universe
    )
    _validate_tickers("--already-covered-tickers", already_covered_tickers, universe)

    if not isinstance(market_data, Mapping):
        raise ValueError(
            f"--market-data's 'sources.{spec.source.value}' must be an object, "
            f"got {type(market_data).__name__}"
        )
    _validate_tickers(
        f"--market-data's 'sources.{spec.source.value}'",
        tuple(market_data),
        universe,
        # An excluded ticker (BYDDY, which is not tradable for fractional buys)
        # in the payload is a caller who fetched more than this slot trades, not
        # a typo: harmless, since the executor never scores it. Rejecting it
        # would force a separate fetch per stream for no safety gain.
        allow_extra=frozenset(config.excluded_tickers),
    )

    unsupported_error_sources = sorted(set(tuple(source_errors)) - {spec.source.value})
    if unsupported_error_sources:
        raise ValueError(
            f"--market-data's 'source_errors' names unsupported source(s) "
            f"{unsupported_error_sources!r}; only {spec.source.value!r} applies to this slot, "
            "and an unrecognized marker would silently mark nothing as failed"
        )
    if spec.source.value in tuple(source_errors):
        raise ValueError(
            f"--market-data lists {spec.source.value!r} in source_errors: this slot has exactly "
            "one market-data source, so a failed fetch leaves nothing to score against. Refusing "
            "to emit a zero-candidate document a caller would read as a quiet tape -- this is "
            "Gate 5's abort-and-alert branch, not its end-silently branch"
        )

    if spec.requires_deployed_last_7d_usd and deployed_last_7d_usd is None:
        raise ValueError(
            f"{spec.slot} requires deployed_last_7d_usd: its Gate 5 producer contract makes the "
            "7-day deployment kill switch the producer's responsibility, and nothing downstream "
            "enforces it. Silently assuming zero would emit uncapped sizing on exactly the day "
            "the guard exists to catch"
        )
    # Checked HERE and not only at the argparse boundary: this function is public
    # and directly importable, so a caller building its own invocation bypasses
    # _finite_non_negative_float entirely. A NaN or negative figure compares False
    # against every threshold inside kill_switch_engaged, silently DISABLING the
    # guard rather than tripping it -- the value looks supplied while doing
    # nothing (Codex review: "Validate deployment values in the public helper";
    # the 1530ET module carries the identical check for the identical reason).
    _require_deployment_figure(deployed_last_7d_usd, "deployed_last_7d_usd")
    # A figure supplied for a slot whose contract does not accept one is
    # REFUSED, not quietly honoured. build_document is exported, so a direct
    # caller could hand a market-open/morning/intraday spec a deployment figure
    # and silently get the kill switch applied: reproduced turning a 5x/$10
    # market-open candidate into 1x/$2 while the document reported
    # kill_switch_evaluated: true. That contradicts the one guarantee these three
    # slots' entry points make -- that they size exactly like the executor they
    # mirror, which never sees this figure (Codex review: "Reject deployment
    # figures for unsupported streams"). Raising beats silently ignoring it: a
    # caller passing the argument believes it matters, and an ignored one would
    # leave them thinking the guard ran.
    if deployed_last_7d_usd is not None and not spec.requires_deployed_last_7d_usd:
        raise ValueError(
            f"{spec.slot} does not accept deployed_last_7d_usd (got "
            f"{deployed_last_7d_usd!r}): this slot's Gate 5 producer contract does not make the "
            "7-day deployment kill switch the producer's responsibility, so applying it here "
            "would size DIFFERENTLY from the executor this entry point mirrors. Only the slot "
            "whose spec sets requires_deployed_last_7d_usd may pass one"
        )

    # Already-covered tickers ride the executor's OWN exclusion set rather than a
    # filter bolted on afterwards, so exactly one place in this process decides
    # which symbols get evaluated.
    scoring_config = dataclasses.replace(
        config,
        excluded_tickers=frozenset(config.excluded_tickers) | frozenset(already_covered_tickers),
    )
    scored_universe = tuple(
        t for t in scoring_config.tickers if t not in scoring_config.excluded_tickers
    )
    # Narrowed by the same universe filter the executor itself will apply, BEFORE
    # the completeness check and the parse below. Only the 1030ET spec sets one
    # (the inherited 09:45 pre-flight gate); everywhere else this is a no-op.
    #
    # Without it, a malformed or truncated payload for a ticker the pre-flight
    # GATED OUT aborted the whole run, even though MorningDCAExecutor.evaluate()
    # would never have looked at that ticker -- and the prompt tells the caller
    # to fetch the entire active universe, so evidence for gated-out symbols is
    # the normal case rather than the exception (Codex review: "Apply preflight
    # exclusions before parsing payloads"; reproduced aborting a run whose only
    # bad payload belonged to a gated-out symbol).
    #
    # This deliberately narrows only what gets SCORED. `universe` above, which
    # _validate_tickers checks the payload's keys against, stays the full
    # configured set: a payload for a gated-out ticker is legitimate input to
    # drop, not an unknown symbol to reject.
    #
    # The gate therefore runs twice per fire -- once here, once inside
    # evaluate(). It is a read of the same same-day artifact both times and is
    # idempotent, and calling the repo's own shared implementation is the point:
    # re-deriving "which tickers did the pre-flight admit" here would be a second
    # definition of a contract morning_preflight_eligible_tickers exists to hold
    # in exactly one place.
    if spec.universe_filter is not None:
        admitted = frozenset(spec.universe_filter(trading_day, scored_universe, config))
        scored_universe = tuple(t for t in scored_universe if t in admitted)

    scored_payloads = {
        ticker: payload
        for ticker, payload in market_data.items()
        if ticker in scored_universe and ticker not in set(ticker_errors)
    }
    # A ticker PRESENT in the payload but missing the evidence needed to score it
    # -- no previous_close_bar, or no intraday bars -- is a truncated or broken
    # fetch, not a symbol this source does not cover. _StaticMarketData turns
    # both absences into None/[], and the executor then returns an ordinary
    # NO_DATA with no provider error, so on this single-source slot a broken
    # response collapses into exactly the zero-candidate document Gate 5 ends
    # silently on (Codex review: "Reject incomplete payloads for attempted
    # tickers"). Omitting the ticker key entirely stays reserved for a genuinely
    # unsupported symbol, and a fetch that RAISED belongs in ticker_errors; this
    # third shape is neither, so it fails closed.
    incomplete = sorted(
        ticker
        for ticker, payload in scored_payloads.items()
        if not isinstance(payload, Mapping)
        or not payload.get("previous_close_bar")
        or not payload.get("intraday_bars")
    )
    if incomplete:
        raise ValueError(
            f"--market-data's 'sources.{spec.source.value}' has incomplete payload(s) for "
            f"{incomplete!r}: a ticker present in the map must carry both a previous_close_bar "
            "and a non-empty intraday_bars list. Omit the ticker entirely if this source does not "
            "cover it, or list it in ticker_errors if its fetch failed -- an incomplete payload "
            "would otherwise score as an ordinary NO_DATA and read as a quiet tape"
        )

    provider = StreamMarketData(
        spec.source,
        # An errored or already-covered ticker's raw payload is dropped BEFORE
        # parsing (see scored_payloads above): a malformed bar for a symbol this
        # run will not score must not abort the whole universe (Codex review,
        # 1530ET module: "Exclude same-window tickers before scoring").
        scored_payloads,
        # The window the executor's own _current_price accepts a quote in is
        # [completed_bar_anchor, decision_dt]; this is the coarser pre-filter
        # _StaticMarketData applies first. Both run, and the executor's rule is
        # the authority -- passing the config's real interval keeps this
        # pre-filter from ever being the stricter of the two and silently
        # dropping a quote the live path would have honored.
        quote_staleness_minutes=scoring_config.interval_minutes,
        trading_day=trading_day,
        ticker_errors=ticker_errors,
    )

    executor = spec.executor_cls(config=scoring_config, mode=spec.live_mode, **spec.executor_kwargs)
    guard_engaged = False
    if deployed_last_7d_usd is not None:
        guarded_sizing = _guarded_sizing_config(
            executor, config_dir, trading_day, deployed_last_7d_usd
        )
        _apply_sizing_config(executor, guarded_sizing)
        # Read back through the repo's own predicate rather than re-deriving
        # "is the threshold crossed" here: this is reported evidence a Gate 9
        # summary quotes, and a second definition of engagement could disagree
        # with the cap that graded_buy_multiplier actually applied below.
        guard_engaged = kill_switch_engaged(guarded_sizing)
    # `now` only selects the trading day and is otherwise unused for the decision
    # anchor: evaluate_signal builds that from the profile's own decision_time.
    # Using the slot's nominal time rather than the wall clock keeps the document
    # a pure function of its inputs, so the same payload re-run later produces
    # the same candidates.
    now = datetime.combine(trading_day, executor.decision_time, tzinfo=EASTERN)
    signals = executor.evaluate(now, provider)

    rows = [_signal_to_row(signal) for signal in signals]
    # A BUY the executor did not mark auto-buy eligible is a
    # manual_review_candidate and must never reach the auto-placement-shaped
    # outputs below. Keyed on the executor's own execution_status rather than on
    # any assumption about whether this slot validates trend, so a profile change
    # cannot quietly promote a manual-review row into an auto-placed order.
    buy_rows = [
        row
        for row in rows
        if row["action"] == Action.BUY.value
        and row["buy_multiplier"] > 0
        and row["execution_status"] == EXEC_AUTO_BUY_ELIGIBLE
    ]

    # BUY-only {"ticker", "desired_usd", "current_price"} rows, the exact shape
    # scripts/same_day_top_up_gate.py's --candidates input documents.
    #
    # DELIBERATELY NOT restricted to planner-approved tickers, which is where
    # this diverges from the 1530ET module -- read this before "fixing" the
    # inconsistency. That module withholds a planner-incompatible row from its
    # top-up list because its documented Gate 6 IS dca_runner.plan_orders(): a
    # row plan_orders() refuses is a row that slot cannot place. These slots'
    # prompts size Gate 6 from paginated broker history and config/budgets.json
    # instead, so plan_orders' verdict gates nothing on this path, and importing
    # its restriction would silently delete real buys from the one output Gate
    # 6.5 actually consumes. Most of the active universe is calibrated, and a
    # calibrated ticker's correct multiplier/drop pairing routinely falls outside
    # the global ladder -- so coupling the two lists would empty this one on
    # ordinary days, handing the executor a zero-candidate file for a tape that
    # genuinely qualified.
    #
    # current_price is this evaluation's own decision price. That is NOT a claim
    # it is still current: Gate 6.5's contract requires re-fetching each
    # survivor's quote immediately before placement and overwriting this field.
    # It is published so the row is schema-valid for that gate (which scores a
    # missing/non-positive current_price as MISSING_CURRENT_PRICE and zeroes it)
    # when a caller has nothing fresher yet.
    #
    # ``prior_low`` can legitimately be null: the live Robinhood adapter stamps
    # ``low=0.0`` on a quote-derived previous-close bar as an explicit "unknown"
    # sentinel, and the executor turns that into ``previous_low=None``. The
    # downstream contract declares a real prior low REQUIRED because Gate 7's
    # pre-placement refresh re-derives the dynamic base from it, so a null there
    # would leave that gate to abort or guess (Codex review: "Handle unknown
    # prior lows before publishing cands").
    #
    # The rule that resolves it, enforced rather than merely documented: with an
    # unknown low, ``select_first_allocation_base_usd`` can only ever pick the
    # CONSERVATIVE below-close base, and no refreshed price can upgrade it --
    # the comparison that would raise it to the full base is exactly the one
    # that cannot be made. So such a row's base is already final, Gate 7's
    # refresh reduces to the prior-close check alone, and the size cannot go
    # stale in the unsafe direction. ``unknown_prior_low`` marks the row so a
    # consumer applies that rule deliberately instead of inferring it from a
    # null. A row claiming the full ($2-scale) base WITH an unknown low is a
    # contradiction that would defeat the whole argument, so it is withheld
    # from the auto-placement outputs and reported for manual review instead of
    # published.
    contradictory_base = sorted(
        row["ticker"]
        for row in buy_rows
        if row["previous_low"] is None and row["base_selection_reason"] == BASE_BELOW_CLOSE_AND_LOW
    )
    # The second reason an auto-buy-eligible row is withheld rather than
    # published: its supporting bars never reach back to the start of the
    # 60-minute window its trend was graded on. See _trend_window_uncovered.
    incomplete_trend_window = _trend_window_uncovered(
        executor, provider, [row["ticker"] for row in buy_rows], now
    )
    withheld = set(contradictory_base) | set(incomplete_trend_window)
    placeable_rows = sorted(
        (row for row in buy_rows if row["ticker"] not in withheld),
        key=_allocation_priority_key,
    )
    same_day_top_up_candidates = [
        {
            "ticker": row["ticker"],
            "desired_usd": row["suggested_amount_usd"],
            "current_price": row["target_price"],
        }
        for row in placeable_rows
    ]

    # The richer producer row the 1230ET prompt's Gate 5 specifies for its
    # cands.json, carried for every slot so one reader works across slots.
    # prior_close/prior_low/selected_base_amount_usd are REQUIRED there because
    # the final pre-placement quote refresh re-checks "still below prior close"
    # AND re-derives the dynamic $1-vs-$2 base -- the multiplier is not the only
    # half of `base * multiplier` that can go stale between Gate 5 and Gate 7.
    # Built from placeable_rows, NOT buy_rows: a row withheld above for an
    # incomplete trend window or a contradictory base must not reappear here.
    # planner_candidates is the documented input for a caller driving
    # dca_runner.plan_orders(), so leaving a withheld row in it let that
    # caller place the exact real-money order the withholding suppressed --
    # the safety check held on one output while leaking through another
    # (Codex review: "Exclude withheld rows from planner candidates";
    # reproduced before fixing, with a calibrated universe ticker whose
    # truncated-window row was absent from cands and present here).
    planner_candidates: List[List[Any]] = []
    planner_incompatible_tickers: List[str] = []
    for row in placeable_rows:
        planner_row = [
            row["ticker"],
            row["suggested_amount_usd"],
            row["buy_multiplier"],
            row["multiplier_drop_pct"],
        ]
        # dca_runner.plan_orders() validates multiplier/drop against ONLY the
        # global config/sizing_ladder.json, with no awareness of the per-ticker
        # calibration graded_buy_multiplier does respect -- so a calibrated
        # ticker's real, correct row can legitimately fall outside the global
        # ladder's range and be refused there as REFUSED_INCONSISTENT_MULTIPLIER.
        # Such rows are withheld and named rather than shipped as planner-safe
        # (Codex review, 1530ET module: "Keep planner rows compatible with
        # calibrated ladders"); their full, correct decision is still in
        # `candidates`, and `cands`/`same_day_top_up_candidates` below still
        # carry them.
        #
        # `not check_planner_compatibility` withholds for the same reason a
        # failed check does: the caller's config directory is not the one
        # dca_runner validates against (see run_cli), so the verdict cannot be
        # trusted in EITHER direction. Naming the ticker rather than emitting two
        # empty lists is what tells the caller WHICH real buys need manual
        # routing.
        if not check_planner_compatibility or _planner_incompatible(planner_row):
            planner_incompatible_tickers.append(row["ticker"])
        else:
            planner_candidates.append(planner_row)

    cands = [
        {
            "ticker": row["ticker"],
            "desired_usd": row["suggested_amount_usd"],
            "current_price": row["target_price"],
            "prior_close": row["previous_close"],
            "prior_low": row["previous_low"],
            "unknown_prior_low": row["previous_low"] is None,
            "selected_base_amount_usd": row["selected_base_amount_usd"],
            "multiplier": row["buy_multiplier"],
            "drop_pct": row["multiplier_drop_pct"],
            **_calibration_fields(
                row["ticker"], row["buy_multiplier"], row["selected_base_amount_usd"]
            ),
        }
        for row in placeable_rows
    ]

    manual_review_tickers = sorted(
        row["ticker"] for row in rows if row["execution_status"] == EXEC_MANUAL_REVIEW_CANDIDATE
    )
    provider_error_tickers = sorted(row["ticker"] for row in rows if row["provider_error"])
    no_data_tickers = sorted(row["ticker"] for row in rows if row["action"] == Action.NO_DATA.value)

    return {
        "slot": spec.slot,
        "runner_slot_id": spec.runner_slot_id,
        "stream": spec.stream,
        "cron_id": spec.cron_id,
        "trading_day": trading_day.isoformat(),
        "decision_time_et": executor.profile["scheduled_for_et"],
        "source": spec.source.value,
        # Recorded rather than assumed: a reader diffing two slots' documents
        # needs to see in the evidence whether this one's signals were graded
        # with or without a validated 60-minute downtrend.
        "trend_validation_exempt": not executor.profile["validate_trend"],
        "deployed_last_7d_usd": deployed_last_7d_usd,
        # Two distinct facts, deliberately not collapsed into one flag: whether
        # the producer applied the guard at all, and whether the threshold is
        # actually crossed. A slot whose contract requires the guard reports
        # evaluated=True every run; engaged=True is the day sizing was capped.
        "kill_switch_evaluated": deployed_last_7d_usd is not None,
        "kill_switch_engaged": guard_engaged,
        # Gates PLACEMENT inside the executor's run(), which this never calls.
        # Reported so a reader can see it; it does not make these candidates
        # hypothetical.
        "live_execution_enabled": getattr(config, "live_execution_enabled", True),
        "evaluated_count": len(rows),
        "buy_count": len(buy_rows),
        "manual_review_count": len(manual_review_tickers),
        "manual_review_tickers": manual_review_tickers,
        # Auto-buy-eligible rows withheld from cands/same_day_top_up_candidates
        # because their base and their prior-low evidence contradict each other
        # (see the cands construction). Never silently dropped: named here so a
        # caller routes them to manual handling rather than reading a shorter
        # list as a quieter tape.
        "withheld_contradictory_base_tickers": contradictory_base,
        # Auto-buy-eligible rows withheld because the payload's bars never reach
        # back to the start of the 60-minute window their trend was graded on --
        # the executor's own rule counts bars in that window without requiring
        # them to span it, so a truncated fetch grades a real downtrend on a
        # fraction of the evidence. Empty on a trend-exempt slot (0935ET), where
        # there is no window to cover. Like the list above these are NAMED, never
        # silently dropped: a caller must be able to tell a short cands list from
        # a quieter tape, and these tickers need a wider re-fetch, not a manual
        # judgement call.
        "withheld_incomplete_trend_window_tickers": incomplete_trend_window,
        "provider_error_count": len(provider_error_tickers),
        "provider_error_tickers": provider_error_tickers,
        "no_data_count": len(no_data_tickers),
        "no_data_tickers": no_data_tickers,
        "candidates": rows,
        "cands": cands,
        "planner_candidates": planner_candidates,
        "planner_incompatible_tickers": planner_incompatible_tickers,
        "planner_config_mismatch": not check_planner_compatibility,
        "same_day_top_up_candidates": same_day_top_up_candidates,
        # Echoes the applied Gate 4 exclusion: these tickers never reach
        # `candidates` at all, so this is the only place the document shows the
        # stand-down was honored.
        "already_covered_tickers": sorted(already_covered_tickers),
        # The 10:30 morning stream narrows its universe to the same-day 9:45
        # pre-flight verdict before scoring (MorningDCAExecutor._apply_preflight_gate).
        # That gate is stream-scoped, so this is None for every other slot --
        # emitted regardless so a reader can tell "no gate applies here" from "a
        # gate applied and admitted these tickers", and so Gate 9 can report
        # which pre-flight artifact the run actually consumed.
        "preflight_gate": getattr(executor, "preflight_gate", None),
    }


def _require_deployment_figure(value: Any, label: str) -> None:
    """Reject anything that is not a real, finite, non-negative dollar figure.

    ``None`` passes: an absent figure is the documented state for a slot whose
    producer contract does not require the guard, and the required-ness check is
    a separate one above.

    ``bool`` is rejected FIRST and explicitly, before the numeric test, because
    Python makes ``bool`` a subclass of ``int``: ``math.isfinite(True)`` is True
    and ``True >= 0`` is True, so a mistyped ``deployed_last_7d_usd=True`` sailed
    through as $1.00 of trailing deployment. That is the worst possible failure
    shape for this particular argument -- $1 leaves the kill switch disengaged,
    so malformed evidence silently produces UNCAPPED candidates, while the run's
    own document serializes the figure back as the JSON literal ``true``, so the
    evidence a human would audit shows a boolean where a dollar amount belongs
    and still claims ``kill_switch_evaluated: true`` (Codex review: "Reject
    boolean deployment values in the public helper"). The CLI path is not
    exposed to this -- argparse hands ``_finite_non_negative_float`` a string and
    ``float("True")`` raises -- but the public helper is directly importable, and
    that is exactly the caller this whole function exists for.
    """
    if value is None:
        return
    if isinstance(value, bool):
        raise ValueError(
            f"{label} must be a number, got the boolean {value!r}: Python treats bool as an int, "
            f"so this would be read as ${float(value):.2f} of trailing deployment -- leaving the "
            "kill switch disengaged on malformed evidence while the document reports the guard as "
            "evaluated"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number, got {type(value).__name__}")
    if not (math.isfinite(value) and value >= 0):
        raise ValueError(
            f"{label} must be a finite, non-negative number, got {value!r}: a NaN or negative "
            "figure compares False against every kill-switch threshold, which disables the guard "
            "instead of tripping it"
        )


def _allocation_priority_key(row: Mapping[str, Any]) -> tuple:
    """``config/budgets.json``'s documented allocation priority, for a published row.

    "Allocations are prioritized by higher multiplier first, then larger percent
    drop, then ticker (alphabetical) for a deterministic tie-break."

    ``cands`` and ``same_day_top_up_candidates`` are consumed IN ORDER by a Gate
    6 that stops at the first cap boundary ("place the orders that fit, skip the
    rest"), so publishing them in ``config/tickers.json`` order let an unsorted
    shallow candidate claim headroom a deeper one needed -- reversing the
    strategy's prescribed allocation on exactly the days the cap binds (Codex
    review: "Route market-open candidates through the canonical planner"). A
    caller that drives ``dca_runner.plan_orders()`` instead is unaffected either
    way: that function sorts internally by its own
    ``_allocation_priority_key``. This is for the callers that do not.

    Mirrors that function deliberately rather than importing it: it takes a
    ``dca_runner.Candidate`` and routes ``drop_pct`` through
    ``_estimated_multiplier``, which exists to guess a multiplier for rows that
    lack one. Every row here carries a real, executor-computed multiplier and
    drop, so constructing a Candidate just to sort would add a lossy conversion
    between two representations of the same numbers. The non-finite fallback is
    kept identical for the identical reason: NaN compares False against
    everything, so sorting on a raw NaN makes the result depend on input order.
    """
    drop = row["multiplier_drop_pct"]
    if drop is None or not math.isfinite(drop):
        drop = 0.0
    return (-row["buy_multiplier"], -drop, row["ticker"])


#: The lookback the executors' own trend rule uses, in minutes. Mirrors the
#: literal inside ``MorningDCAExecutor._trend_validated`` (``decision_dt -
#: timedelta(minutes=60)``), which every trend-validating stream inherits
#: unchanged. Kept as a named constant here rather than a bare 60 so the two
#: places are greppable together; ``test_trend_window_matches_the_executors_own``
#: pins them to the same value.
TREND_LOOKBACK_MINUTES = 60


def _trend_window_uncovered(
    executor: Any,
    provider: "StreamMarketData",
    tickers: Sequence[str],
    decision_dt: datetime,
) -> List[str]:
    """Tickers whose supplied bars never reach back to the trend window's start.

    ``MorningDCAExecutor._trend_validated`` asks two questions of the 60 minutes
    before the decision anchor: are there at least ``trend_min_bars`` usable bars
    in it, and is the last close below the first. It does NOT ask whether those
    bars SPAN the window -- six consecutive 5-minute bars covering 12:00-12:25
    satisfy both tests for a 12:30 anchor whose window is 11:30-12:30, so a
    payload carrying only the last 25 minutes yields ``trend_validated=True`` and
    an ``auto_buy_eligible`` row at full ladder size, on a quarter of the
    evidence the config asks for (Codex review: "Require the full midday trend
    window before auto-buying"; reproduced before fixing).

    That rule belongs to the live executor and is shared by every trend-validating
    stream, so it is deliberately NOT changed here -- tightening it would alter
    live eligibility for the in-repo executor path too, which is a sizing-policy
    decision with money attached rather than a fix to this entry point. What IS
    this entry point's job is refusing to PUBLISH an auto-placement row whose
    supporting evidence it can see is short, which is what the caller was told to
    supply ("fetch bars from 11:30 ET onward") and what nothing until now checked.

    The check is deliberately coarse and one-sided: a single usable bar at or
    before ``window_start + one interval`` proves the fetch reached back far
    enough. The one-interval tolerance exists because a provider that stamps bars
    at interval END rather than start puts a correct window's first bar one
    interval late, and rejecting that would fail a complete payload.

    Note this cannot distinguish "the caller fetched too narrow a range" from
    "this ticker did not trade early in the window", and does not try to: both
    mean the 60-minute downtrend was never actually observed over 60 minutes, so
    both get the same conservative treatment. It is therefore PER-TICKER and
    withholds rather than aborting -- one thinly-traded symbol must not take the
    whole universe down with it, unlike the payload-completeness check above,
    where an absent bar list is unambiguously a broken fetch.
    """
    # Trend-exempt slots have no window to cover. 0935ET is the live case: at
    # 09:35 the window's start would be 08:35, pre-market, so a "full" window is
    # impossible by construction and demanding one would withhold every candidate
    # on every fire.
    if not executor.profile["validate_trend"]:
        return []
    interval = timedelta(minutes=executor.config.interval_minutes)
    window_start = decision_dt - timedelta(minutes=TREND_LOOKBACK_MINUTES)
    # The same anchor the executor grades against: the decision bar, not the
    # wall clock. Asking the provider for everything up to it keeps this reading
    # exactly the bars _trend_validated would have seen.
    anchor = completed_bar_anchor(
        decision_dt, executor.config.interval_minutes, executor.sizing_config
    )
    reach_by = window_start + interval
    uncovered: List[str] = []
    for ticker in tickers:
        # Bounded to [window_start, anchor] -- the SAME interval
        # _trend_validated counts bars over -- and not to everything the payload
        # happens to carry. Probing from datetime.min instead let a single bar
        # older than the window (a prior-session intraday bar, a stale cached
        # row) become the minimum timestamp, so the probe reported full coverage
        # for a payload whose in-window evidence was still only the last 25
        # minutes, and the truncated signal published at full size -- the exact
        # failure this function exists to stop, reintroduced through its own
        # lookup (Codex review: "Restrict the coverage probe to the trend
        # window"; reproduced before fixing).
        bars = provider.get_intraday_bars(
            ticker, window_start, anchor, executor.config.interval_minutes
        )
        usable = [b for b in bars if math.isfinite(b.close) and b.close > 0]
        if not usable or min(b.timestamp for b in usable) > reach_by:
            uncovered.append(ticker)
    return sorted(uncovered)


def _finite_non_negative_float(raw: str) -> float:
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        raise argparse.ArgumentTypeError(
            f"must be a finite, non-negative number, got {raw!r}: a NaN or negative figure "
            "compares False against every kill-switch threshold, silently disabling the guard "
            "instead of tripping it"
        )
    return value


def build_parser(
    spec: StreamSpec,
    *,
    slot_choices: Optional[Sequence[str]] = None,
    description: Optional[str] = None,
) -> argparse.ArgumentParser:
    """Build the CLI parser for one slot -- or for a stream serving several.

    When ``slot_choices`` is given, ``spec`` is only a TEMPLATE: it supplies the
    flags every one of those slots shares, and ``--slot`` becomes a required
    argument that :func:`run_multi_slot_cli` resolves back into the real spec.
    The intraday stream is the only such case today: 11:30, 1:30 and 2:30 are
    three fires of ONE cron (``7be82b97``) over one executor class and one
    config, differing only in decision anchor and artifact folder, so they get
    one command with a selector rather than three near-identical modules.
    """
    parser = argparse.ArgumentParser(
        description=description or f"Gate 5 candidate generation for the {spec.slot} slot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if slot_choices is not None:
        parser.add_argument(
            "--slot",
            required=True,
            choices=list(slot_choices),
            help=(
                "Which fire of this stream's cron this run is. No default: the fires differ in "
                "decision anchor and artifact folder, so guessing one would score the payload "
                "against another fire's time and publish it under another fire's slot label."
            ),
        )
    parser.add_argument(
        "--trading-day", required=True, help="YYYY-MM-DD, the ET calendar day this slot fires on."
    )
    parser.add_argument(
        "--market-data",
        required=True,
        type=Path,
        help="Path to the pre-fetched market-data JSON. See the module docstring for its shape.",
    )
    parser.add_argument("--config-dir", type=Path, default=get_config_dir())
    parser.add_argument(
        "--already-covered-tickers",
        nargs="*",
        default=(),
        metavar="TICKER",
        help=(
            "Tickers a compatible executor already placed an order for in THIS SAME window "
            "(Gate 4's compatible-stand-down case). Never scored, never published. Default: none."
        ),
    )
    if spec.requires_deployed_last_7d_usd:
        parser.add_argument(
            "--deployed-last-7d-usd",
            required=True,
            type=_finite_non_negative_float,
            help=(
                "Trailing-7-day AFTERNOON-scheduled deployment (dca_risk."
                "is_afternoon_scheduled_evidence), NOT a sum across all seven slots. Feeds the "
                "kill switch, which this slot's Gate 5 producer contract makes the producer's "
                "responsibility because nothing downstream enforces it. No default: silently "
                "assuming zero would emit uncapped sizing on exactly the day the guard exists "
                "to catch."
            ),
        )
    return parser


def run_cli(spec: StreamSpec, argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, read the market-data file, print the Gate 5 document.

    Exit status is 0 on success. Malformed input, an unusable ``--config-dir``,
    or a failed source raises before that, which the caller must treat as Gate
    5's abort-and-alert branch.
    """
    return _run_with_args(spec, build_parser(spec).parse_args(argv))


def run_multi_slot_cli(
    specs: Mapping[str, StreamSpec],
    argv: Optional[Sequence[str]] = None,
    *,
    description: str,
) -> int:
    """:func:`run_cli` for a stream whose one executor class serves several slots.

    ``specs`` maps the ``--slot`` token (e.g. ``"1130"``) to that slot's spec.
    The parser is built ONCE, from an arbitrary member, so every spec in the
    mapping must agree on the fields ``build_parser`` keys off. That agreement is
    CHECKED rather than assumed: a future slot that disagreed would otherwise be
    handed another slot's flag set -- silently accepting (or silently omitting)
    ``--deployed-last-7d-usd``, which is the one flag whose absence changes
    sizing rather than merely erroring.
    """
    if not specs:
        raise ValueError("run_multi_slot_cli needs at least one slot spec")
    slot_tokens = sorted(specs)
    template = specs[slot_tokens[0]]
    divergent = sorted(
        token
        for token, candidate in specs.items()
        if candidate.requires_deployed_last_7d_usd != template.requires_deployed_last_7d_usd
        or candidate.source != template.source
    )
    if divergent:
        raise ValueError(
            f"slot spec(s) {divergent!r} disagree with {slot_tokens[0]!r} on "
            "requires_deployed_last_7d_usd and/or source; these decide the shared parser's "
            "flags, so one command cannot serve both. Give the divergent slot(s) their own "
            "entry point rather than letting them inherit another slot's arguments"
        )
    args = build_parser(template, slot_choices=slot_tokens, description=description).parse_args(argv)
    return _run_with_args(specs[args.slot], args)


def _run_with_args(spec: StreamSpec, args: argparse.Namespace) -> int:
    """Shared body of :func:`run_cli` and :func:`run_multi_slot_cli`.

    Split out so the multi-slot selector can resolve ``--slot`` into a real spec
    AFTER parsing and before any of this runs -- everything below is already
    per-slot, and nothing here reads ``args.slot``.
    """
    trading_day = date.fromisoformat(args.trading_day)
    required = required_config_filenames(spec)

    # Every load_*_config only WARNS and falls back to hardcoded defaults for a
    # missing directory or file -- right for a dev caller with no config/ nearby,
    # wrong for a live entry point, where it would silently score against the
    # built-in universe/ladder/caps while the caller believes the checked-in
    # config was used (Codex review, 1530ET module: "Fail closed when the config
    # directory is unavailable").
    if not args.config_dir.is_dir():
        raise ValueError(
            f"--config-dir {args.config_dir} does not exist; refusing to score against "
            "hardcoded defaults instead of the real checked-in config"
        )
    missing_config_files = [name for name in required if not (args.config_dir / name).is_file()]
    if missing_config_files:
        raise ValueError(
            f"--config-dir {args.config_dir} is missing required file(s) {missing_config_files!r}; "
            "refusing to score with hardcoded defaults for the missing piece(s) instead of the "
            "real checked-in config"
        )

    # dca_runner resolves CONFIG_DIR at ITS OWN import time from $DCA_CONFIG_DIR
    # with no per-call override, and this module reaches it three ways: the
    # previous-trading-day calendar lookup inside _StaticMarketData, the ladder
    # validation in _planner_incompatible, and the calibration bounds in
    # _calibration_fields. Injecting the selected directory in the narrowest
    # possible window around that first import is the mechanism the 1530ET module
    # uses (Codex review: "Defer dca_runner until after honoring --config-dir");
    # os.environ is restored immediately, and a process that already imported
    # dca_runner keeps whatever CONFIG_DIR that import resolved.
    _dca_config_dir_env_backup = os.environ.get("DCA_CONFIG_DIR")
    os.environ["DCA_CONFIG_DIR"] = str(args.config_dir)
    try:
        import dca_runner
    finally:
        if _dca_config_dir_env_backup is None:
            os.environ.pop("DCA_CONFIG_DIR", None)
        else:
            os.environ["DCA_CONFIG_DIR"] = _dca_config_dir_env_backup

    # The injection above only takes effect if this process had not already
    # imported dca_runner -- Python caches modules, so an embedded caller, or a
    # second main() call with a different --config-dir, keeps whatever CONFIG_DIR
    # the FIRST import resolved. Three things in this run then read that stale
    # directory: the previous-trading-day calendar check inside _StaticMarketData,
    # the ladder validation in _planner_incompatible, and the calibration bounds
    # in _calibration_fields. The calendar one is the dangerous member: differing
    # holiday calendars can accept a stale previous-close baseline or reject a
    # correct one, and unlike a planner mismatch that verdict is neither withheld
    # nor reported anywhere in the document -- the resulting top-up rows still
    # look actionable (Codex review: "Honor a new config directory after
    # dca_runner was imported"). There is no per-call override to fix it with, so
    # this fails closed rather than scoring against two different config
    # directories at once.
    # Checked here too, before the market-data file is even read, so a mistyped
    # --config-dir fails on the directory rather than on whatever the payload
    # happens to contain. build_document repeats it for direct callers.
    _require_matching_runner_config_dir(args.config_dir)

    # object_pairs_hook rejects a duplicate key at any nesting level: plain
    # json.loads keeps only the LAST occurrence, so a malformed merge producing
    # two entries for one ticker would discard a snapshot before validation or
    # scoring ever saw it (Codex review, 1530ET module: "Reject duplicate keys in
    # market-data JSON").
    payload = json.loads(
        args.market_data.read_bytes(), object_pairs_hook=dca_runner._reject_duplicate_keys
    )
    if not isinstance(payload, dict):
        raise ValueError("--market-data must contain a top-level JSON object")
    # A missing "sources" key is malformed input, not "the fetch returned
    # nothing" -- the two must not collapse into the same schema-valid,
    # zero-candidate document a caller could read as a quiet tape.
    if "sources" not in payload or not isinstance(payload["sources"], dict):
        raise ValueError("--market-data must contain a top-level 'sources' object")
    sources = payload["sources"]
    unexpected_sources = sorted(set(sources) - {spec.source.value})
    if unexpected_sources:
        raise ValueError(
            f"--market-data's 'sources' has unsupported key(s) {unexpected_sources!r}: this "
            "slot's executor evaluates a SINGLE market-data provider "
            f"({spec.source.value}), so any other source's payload would be silently discarded "
            "rather than cross-checked. Remove it rather than letting this run look "
            "dual-sourced when it is not"
        )
    if spec.source.value not in sources:
        raise ValueError(
            f"--market-data's 'sources' is missing the required {spec.source.value!r} object; a "
            "genuinely failed fetch is spelled by listing it in source_errors (which aborts), "
            "never by omitting the key"
        )
    source_payload = sources[spec.source.value]
    source_errors = payload.get("source_errors", [])
    if (
        source_errors is None
        or isinstance(source_errors, (str, bytes))
        or not isinstance(source_errors, Sequence)
    ):
        raise ValueError("--market-data's 'source_errors', if present, must be a list")
    # An explicit `"ticker_errors": null` survives .get(..., {}) unchanged and
    # would read exactly like an omitted key downstream -- silently "no
    # per-ticker errors" instead of the malformed evidence it is. Caught here, at
    # the JSON boundary, where omitted and explicitly-null are distinguishable
    # (Codex review, 1530ET module: "Reject null ticker_errors payloads").
    if "ticker_errors" in payload and payload["ticker_errors"] is None:
        raise ValueError("--market-data's 'ticker_errors', if present, must be an object, got null")
    ticker_errors_by_source = payload.get("ticker_errors", {})
    if not isinstance(ticker_errors_by_source, Mapping):
        raise ValueError("--market-data's 'ticker_errors', if present, must be an object")
    unexpected_error_sources = sorted(set(ticker_errors_by_source) - {spec.source.value})
    if unexpected_error_sources:
        raise ValueError(
            f"--market-data's 'ticker_errors' has unsupported key(s) "
            f"{unexpected_error_sources!r}; only {spec.source.value!r} applies to this slot"
        )
    ticker_errors = ticker_errors_by_source.get(spec.source.value, [])
    if ticker_errors is None:
        raise ValueError(
            f"--market-data's 'ticker_errors.{spec.source.value}', if present, must be a list, "
            "got null"
        )

    _require_populated_config(args.config_dir)
    _require_parseable_stream_config(args.config_dir / spec.stream_config_filename, spec.stream)
    # allow_unpinned_account=True matches each executor's own default loader:
    # this module never touches a broker, so requiring
    # $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER to be exported just to GENERATE a
    # candidate list would block Gate 5 on a credential Gate 5 does not use.
    config = spec.config_loader(args.config_dir, allow_unpinned_account=True)
    covered = set(args.already_covered_tickers)
    remaining_tickers = [
        t for t in config.tickers if t not in config.excluded_tickers and t not in covered
    ]
    if remaining_tickers and not source_payload:
        raise ValueError(
            f"--market-data has no usable 'sources.{spec.source.value}' payload "
            "(absent, empty, or listed in 'source_errors'); this slot has exactly one "
            "market-data source, so this is a data-evidence failure to abort on, not a quiet tape"
        )

    document = build_document(
        spec,
        trading_day=trading_day,
        config=config,
        market_data=source_payload,
        source_errors=source_errors,
        ticker_errors=ticker_errors,
        already_covered_tickers=args.already_covered_tickers,
        deployed_last_7d_usd=getattr(args, "deployed_last_7d_usd", None),
        config_dir=args.config_dir,
        # dca_runner validates planner rows and calibration bounds against ITS
        # OWN CONFIG_DIR, fixed at the import above. When --config-dir points
        # elsewhere, rows were sized from a different ladder than the one being
        # validated against, so the verdict is untrustworthy in both directions.
        check_planner_compatibility=True,
    )

    # allow_nan=False: defense in depth alongside the payload's own finite-value
    # checks -- a NaN reaching this document from any future code path would
    # otherwise serialize as a bare, non-standard JSON token instead of failing.
    json.dump(document, sys.stdout, sort_keys=True, default=str, allow_nan=False)
    sys.stdout.write("\n")
    return 0
