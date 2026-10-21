"""On-disk store for plan-first recommendations, and the agent's freshness rule.

Layout, under ``<cron root>/recommendations/<YYYY-MM-DD>/``:

* ``rec_<slot>_<HHMM>.json``: the agent payload (``RecommendationSet``). It holds ticker, usd
  and ref_id only. This is the one file a buying agent ever reads.
* ``expected_<slot>_<HHMM>.json``: the program-only ``ExpectedPriceSet``. It holds the
  decision price, prior close and so on, which the fill comparison reads later. It is
  never published and never handed to an agent.

Keyed by slot, not by time alone. A 12:25 cycle computes for both the open 11:30 window
and the 12:30 slot that opens five minutes later (``lookahead_minutes``), so the 12:30
agent finds a file for its own slot instead of an 11:30 one.

It lives outside every slot's artifact directory on purpose:
``scripts/verify_schedule.py`` and the same-day scans read those directories as evidence
that the slot ran (deploy/launchd/PREFETCH.md, "Output").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import dca_config
import dca_paths
from dca_fileio import atomic_write_text
from dca_rec_models import SLOT_IDS, ExpectedPriceSet, RecommendationSet
from scripts.validate_native_placement import _order_of

ET = ZoneInfo("America/New_York")
SETTINGS_FILE = "recommendations.json"
_REC_NAME = re.compile(r"^rec_(\d{4})_(\d{4})\.json$")
# A saved placement response is <TICKER>.json; prior_ids.json and other helpers are not.
_PLACEMENT_NAME = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}\.json$")


@dataclass(frozen=True)
class RecommendationSettings:
    """``config/recommendations.json``, validated and fail-closed (dca_config's rules)."""

    max_age: timedelta
    lookahead: timedelta
    publish: dict[str, Any]


def load_settings(config_dir: str | Path | None = None) -> RecommendationSettings:
    """Load the settings; a missing, malformed or out-of-range file raises ``ValueError``.

    There is no silent default. A missing freshness rule must stop buying, not quietly fall
    back to a looser window.
    """
    directory = Path(config_dir) if config_dir is not None else dca_config.get_config_dir()
    data = dca_config._read_json(directory / SETTINGS_FILE)
    if data is None:
        raise ValueError(f"missing {SETTINGS_FILE} in {directory}")
    max_age, lookahead = data.get("max_age_minutes"), data.get("lookahead_minutes")
    for name, value, low in (("max_age_minutes", max_age, 1), ("lookahead_minutes", lookahead, 0)):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= 60:
            raise ValueError(f"{SETTINGS_FILE}: {name} must be an integer in [{low}, 60]")
    publish = data.get("publish")
    if not isinstance(publish, dict):
        raise ValueError(f"{SETTINGS_FILE}: publish must be an object")
    # Each channel's "enabled" must be a real boolean: the string "false" is truthy and would
    # silently switch on an external publisher (PR #1667 review). Fail closed instead.
    for channel in ("notion", "github_branch"):
        block = publish.get(channel)
        if not isinstance(block, dict) or not isinstance(block.get("enabled"), bool):
            raise ValueError(f"{SETTINGS_FILE}: publish.{channel}.enabled must be true or false")
    return RecommendationSettings(timedelta(minutes=max_age), timedelta(minutes=lookahead), publish)


@dataclass(frozen=True)
class Selection:
    """What the agent may act on: ``OK`` with a set, or ``NONE`` with the reason it may not."""

    status: str
    reason: str
    recommendation: RecommendationSet | None = None
    age_seconds: float | None = None


def _canonical(document: dict[str, Any]) -> str:
    # Sorted, compact, NaN-free: the same set always writes the same bytes, so a published
    # copy can be compared byte-for-byte with the local one.
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _et(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(ET)


class RecommendationStore:
    """Write recommendation pairs and select the one an agent may use."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else Path(dca_paths.stream_root("recommendations"))

    def day_dir(self, trading_day: date) -> Path:
        return self.root / trading_day.isoformat()

    def write(self, recommendation: RecommendationSet, expected: ExpectedPriceSet) -> Path:
        """Atomically write the expected file FIRST, then the agent file; return the agent file.

        Order matters. An agent file with no expected-price twin would make that buy
        impossible to compare later. The reverse, an orphan expected file, is harmless
        because no agent ever reads it.
        """
        if (recommendation.trading_day, recommendation.as_of, recommendation.slot) != (
            expected.trading_day, expected.as_of, expected.slot
        ):
            raise ValueError("recommendation and expected prices describe different runs")
        if {item.ticker for item in recommendation.items} != {item.ticker for item in expected.items}:
            raise ValueError("recommendation and expected prices name different tickers")
        stamp = _et(recommendation.as_of).strftime("%H%M")
        directory = self.day_dir(recommendation.trading_day)
        directory.mkdir(parents=True, exist_ok=True)
        expected_path = directory / f"expected_{recommendation.slot}_{stamp}.json"
        target = directory / f"rec_{recommendation.slot}_{stamp}.json"
        expected_text, target_text = _canonical(expected.to_json()), _canonical(recommendation.to_json())
        # Names carry only HH:MM, so two cycles in the same minute collide. An identical rewrite is
        # harmless; a DIFFERENT set under an existing name would hand an earlier reader's set the
        # retry's prices, so refuse it before touching either file (PR #1815 review).
        for path, text in ((expected_path, expected_text), (target, target_text)):
            if path.exists() and path.read_text(encoding="utf-8") != text:
                raise ValueError(f"non_identical_overwrite:{path.name}")
        atomic_write_text(expected_path, expected_text)
        atomic_write_text(target, target_text)
        return target

    def select_for(self, slot: str, now: datetime, max_age: timedelta) -> Selection:
        """The newest valid file for ``slot`` stamped at or before ``now``, today, within ``max_age``.

        This is the operator's rule (2026-10-13). At 12:07 with only a 12:00 file, use the
        12:00 one. Never use a file older than ``max_age``, never one from another day, and
        never one stamped after ``now``, since that would be a clock or ordering fault.

        If the newest file is unreadable or invalid, selection fails closed. It never falls back to
        older data around a file it cannot read.
        """
        if slot not in SLOT_IDS:
            return Selection("NONE", "unknown_slot")
        current = _et(now)
        directory = self.day_dir(current.date())
        candidates = []
        for path in directory.glob(f"rec_{slot}_*.json") if directory.is_dir() else ():
            if _REC_NAME.match(path.name):
                candidates.append(path)
        if not candidates:
            return Selection("NONE", "no_file_for_slot_today")
        # Names sort by HHMM within one day, so newest-first is a reverse name sort.
        for path in sorted(candidates, reverse=True):
            try:
                document = json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)
                recommendation = RecommendationSet.from_json(document)
            except (OSError, ValueError, UnicodeError):
                return Selection("NONE", "newest_file_invalid")
            as_of = _et(recommendation.as_of)
            # The directory, the timestamp AND the recorded trading day must all agree: a file in today's
            # directory stamped today but computed for yesterday would otherwise read as OK and have its
            # evidence recorded under yesterday (PR #1815 review).
            if (recommendation.slot != slot or as_of.date() != current.date()
                    or recommendation.trading_day != current.date()):
                return Selection("NONE", "file_identity_mismatch")
            if as_of > current:
                continue  # stamped after now: not yet usable; an older file may still be
            age = (current - as_of).total_seconds()
            if age > max_age.total_seconds():
                return Selection("NONE", "stale", age_seconds=age)
            return Selection("OK", "fresh", recommendation, age)
        return Selection("NONE", "only_future_stamped_files")

    def handed_path(self, trading_day: date, slot: str) -> Path:
        return self.day_dir(trading_day) / f"handed_{slot}.json"

    def placements_dir(self, trading_day: date, slot: str) -> Path:
        return self.day_dir(trading_day) / "placements" / slot

    def record_handed(self, recommendation: RecommendationSet, *, replace_unplaced: bool = False) -> Path:
        """Record which recommendation a slot's agent was given, and create its placements dir.

        The fill comparison joins on this record, so the agent never has to report back which
        file it used.

        The first set handed for a slot OWNS the slot for the day. Handing orders out is the point
        of no return: an earlier reader may already be placing them, so a DIFFERENT set is refused
        (``ValueError``) whether or not a placement file exists yet. A second read that would
        overwrite it would make the first run's ``finish`` attribute its responses to the second
        set's amounts and prices (PR #1815 review, extending PR #1667's placements-exist check). The same set may
        be re-recorded.

        Recovery is explicit, never implicit: ``replace_unplaced=True`` (an operator decision,
        ``plan_first_slot begin --replace-unplaced``) may replace the record only while NO placement
        evidence exists for the slot.
        """
        directory = self.placements_dir(recommendation.trading_day, recommendation.slot)
        path = self.handed_path(recommendation.trading_day, recommendation.slot)
        if path.exists():
            current = RecommendationSet.from_json(
                json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)
            )
            if current != recommendation:
                if self._placed_tickers(directory):
                    raise ValueError("placements_exist_for_an_earlier_recommendation")
                if not replace_unplaced:
                    raise ValueError("slot_already_handed_a_different_recommendation")
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, _canonical(recommendation.to_json()))
        return path

    def handed(self, trading_day: date, slot: str) -> RecommendationSet | None:
        """The set this slot's agent was handed today, or None when nothing was handed."""
        path = self.handed_path(trading_day, slot)
        if not path.exists():
            return None
        return RecommendationSet.from_json(
            json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)
        )

    @staticmethod
    def _placed_tickers(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        return [path for path in directory.iterdir() if _PLACEMENT_NAME.match(path.name)]

    def logged_references(self, trading_day: date) -> dict[str, tuple[str, float]]:
        """``{ticker: (order_id, decision_price)}`` for today's plan-first buys, latest per ticker.

        This is the "logged reference" ``same_day_top_up_gate`` binds to the broker's latest
        order for a repeat buy. It comes from the program's own records: the handed set's
        expected twin and the agent's saved placement response. The gate itself refuses any
        reference whose order id is not the broker's latest, so a stale entry here can only
        fall back to the gate's conservative $0, never over-buy.
        """
        best: dict[str, tuple[str, str, float]] = {}
        for slot in SLOT_IDS:
            handed = self.handed(trading_day, slot)
            if handed is None:
                continue
            try:
                prices = {item.ticker: item.decision_price for item in self.expected_for(handed).items}
            except (OSError, ValueError):
                continue
            for path in self._placed_tickers(self.placements_dir(trading_day, slot)):
                ticker = path.stem
                try:
                    order = _order_of(json.loads(path.read_bytes(),
                                                 object_pairs_hook=dca_config._reject_duplicate_keys))
                except (OSError, ValueError, UnicodeError):
                    continue
                order_id = str(order.get("id") or "").strip() if order else ""
                created = str(order.get("created_at") or "") if order else ""
                if not order_id or ticker not in prices:
                    continue
                if ticker not in best or created > best[ticker][0]:
                    best[ticker] = (created, order_id, prices[ticker])
        return {ticker: (order_id, price) for ticker, (_created, order_id, price) in best.items()}

    def expected_for(self, recommendation: RecommendationSet) -> ExpectedPriceSet:
        """The program-only twin of ``recommendation`` (fill comparison reads this)."""
        stamp = _et(recommendation.as_of).strftime("%H%M")
        path = self.day_dir(recommendation.trading_day) / f"expected_{recommendation.slot}_{stamp}.json"
        document = json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)
        expected = ExpectedPriceSet.from_json(document)
        # Verify the twin really belongs to this set: same run identity and the same tickers.
        if (expected.trading_day, expected.as_of, expected.slot) != (
                recommendation.trading_day, recommendation.as_of, recommendation.slot) or (
                {item.ticker for item in expected.items} != {item.ticker for item in recommendation.items}):
            raise ValueError("expected_prices_do_not_match_recommendation")
        return expected
