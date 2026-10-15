"""Step 1 (TDD, failing first): execution routing for the primary buy.

Contract under test (proposed module ``dca_execution_router``; does not exist yet):

* LOCAL is the sole executor of the buy whenever it is reachable.
* CLOUD is calculation-only: it may never reach order placement.
* PERPLEXITY is the backup buyer: it may place only when the local run is unreachable,
  off, or errored, and never after the local run succeeded or is still in flight.
* Every routing decision is logged as (path, reason, timestamp), path being
  ``local`` or ``perplexity-fallback``.

Only the routing decision is tested here. HOW "local failed" is detected (heartbeat or
timeout) is step 2 and is deliberately an input (``LocalStatus``) so this file does not
presuppose it. Tier logic (1x/1x/2x/3x ladder) and the SizingSuggester are not touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import dca_execution_router as router
from dca_execution_router import ExecutorRole, LocalStatus

NOW = datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc)

# Local states where the backup is allowed to act.
LOCAL_FAILED = [LocalStatus.UNREACHABLE, LocalStatus.OFF, LocalStatus.ERRORED]
# Local states where the backup must stand down.
LOCAL_OK_OR_BUSY = [LocalStatus.SUCCEEDED, LocalStatus.REACHABLE, LocalStatus.IN_PROGRESS]


class SpyBroker:
    """Records every call so tests can assert order placement never happened."""

    def __init__(self):
        self.placed = []

    def place_order(self, *args, **kwargs):
        self.placed.append((args, kwargs))
        return {"id": "spy-1"}


# --- 1a. local buys when reachable ---------------------------------------------------


@pytest.mark.parametrize("status", [LocalStatus.REACHABLE, LocalStatus.SUCCEEDED, LocalStatus.IN_PROGRESS])
def test_local_role_is_the_buyer_when_reachable(status):
    d = router.decide(ExecutorRole.LOCAL, status, now=NOW)
    assert d.may_place is True
    assert d.path == "local"


def test_local_role_is_the_buyer_even_if_status_unknown():
    # The local run observes its own state first-hand; it never defers to a probe of itself.
    d = router.decide(ExecutorRole.LOCAL, LocalStatus.UNKNOWN, now=NOW)
    assert d.may_place is True and d.path == "local"


# --- 1b. cloud never calls order placement -------------------------------------------


@pytest.mark.parametrize("status", list(LocalStatus))
def test_cloud_role_never_may_place_for_any_local_status(status):
    d = router.decide(ExecutorRole.CLOUD, status, now=NOW)
    assert d.may_place is False
    assert d.path == "none"


@pytest.mark.parametrize("status", list(LocalStatus))
def test_cloud_broker_wrapper_blocks_place_order_without_touching_the_broker(status):
    spy = SpyBroker()
    guarded = router.guard_broker(spy, ExecutorRole.CLOUD, status, now=NOW)
    with pytest.raises(router.OrderPlacementForbidden):
        guarded.place_order("MU", 5.0)
    assert spy.placed == []


def test_guard_broker_passes_through_for_a_permitted_role():
    spy = SpyBroker()
    guarded = router.guard_broker(spy, ExecutorRole.LOCAL, LocalStatus.REACHABLE, now=NOW)
    guarded.place_order("MU", 5.0)
    assert len(spy.placed) == 1


# --- 1c. perplexity falls back only on local failure ---------------------------------


@pytest.mark.parametrize("status", LOCAL_FAILED)
def test_perplexity_acts_when_local_is_unreachable_off_or_errored(status):
    d = router.decide(ExecutorRole.PERPLEXITY, status, now=NOW)
    assert d.may_place is True
    assert d.path == "perplexity-fallback"
    assert status.value in d.reason


def test_perplexity_fallback_requires_cloud_calculation_outputs():
    d = router.decide(ExecutorRole.PERPLEXITY, LocalStatus.OFF, now=NOW, cloud_calc_available=False)
    assert d.may_place is False  # fail closed: no sizing inputs, no buy (never invent sizes)
    assert d.path == "none"
    assert "calculation" in d.reason


# --- 1d. no fallback after local success ----------------------------------------------


@pytest.mark.parametrize("status", LOCAL_OK_OR_BUSY)
def test_perplexity_stands_down_when_local_succeeded_or_is_still_running(status):
    d = router.decide(ExecutorRole.PERPLEXITY, status, now=NOW)
    assert d.may_place is False
    assert d.path == "none"


def test_perplexity_stands_down_on_unknown_local_status():
    # Unknown is NOT failure: acting on it risks a duplicate buy. Step 2 decides how unknown resolves.
    d = router.decide(ExecutorRole.PERPLEXITY, LocalStatus.UNKNOWN, now=NOW)
    assert d.may_place is False


def test_fallback_decision_is_idempotent_per_slot_and_day(tmp_path):
    first = router.record(router.decide(ExecutorRole.PERPLEXITY, LocalStatus.OFF, now=NOW),
                          log_dir=tmp_path, trading_day="2026-10-08", slot="1530")
    assert first.may_place is True
    # A second fallback evaluation for the same slot/day after one was already recorded must not buy again.
    second = router.decide(ExecutorRole.PERPLEXITY, LocalStatus.OFF, now=NOW,
                           log_dir=tmp_path, trading_day="2026-10-08", slot="1530")
    assert second.may_place is False
    assert "already" in second.reason


def test_fallback_blocked_once_local_recorded_a_buy_for_the_slot(tmp_path):
    router.record(router.decide(ExecutorRole.LOCAL, LocalStatus.REACHABLE, now=NOW),
                  log_dir=tmp_path, trading_day="2026-10-08", slot="1530")
    d = router.decide(ExecutorRole.PERPLEXITY, LocalStatus.OFF, now=NOW,
                      log_dir=tmp_path, trading_day="2026-10-08", slot="1530")
    assert d.may_place is False  # the log is a second line of defence against duplicate buys


# --- 1e. fallback is logged ------------------------------------------------------------


def test_fallback_is_logged_with_path_reason_and_timestamp(tmp_path):
    d = router.decide(ExecutorRole.PERPLEXITY, LocalStatus.ERRORED, now=NOW)
    router.record(d, log_dir=tmp_path, trading_day="2026-10-08", slot="1530")
    rows = []
    for f in tmp_path.rglob("*.jsonl"):
        with f.open("r", encoding="utf-8") as _f:
            rows.extend(json.loads(line) for line in _f)
    assert len(rows) == 1
    row = rows[0]
    assert row["path"] == "perplexity-fallback"
    assert "errored" in row["reason"]
    assert row["timestamp"] == NOW.isoformat()
    assert row["slot"] == "1530" and row["trading_day"] == "2026-10-08"


def test_local_buy_is_logged_as_local(tmp_path):
    router.record(router.decide(ExecutorRole.LOCAL, LocalStatus.REACHABLE, now=NOW),
                  log_dir=tmp_path, trading_day="2026-10-08", slot="1530")
    rows = []
    for f in tmp_path.rglob("*.jsonl"):
        with f.open("r", encoding="utf-8") as _f:
            rows.extend(json.loads(line) for line in _f)
    assert [r["path"] for r in rows] == ["local"]


def test_non_placing_decisions_are_not_logged_as_a_buy_path(tmp_path):
    router.record(router.decide(ExecutorRole.CLOUD, LocalStatus.OFF, now=NOW),
                  log_dir=tmp_path, trading_day="2026-10-08", slot="1530")
    rows = []
    for f in tmp_path.rglob("*.jsonl"):
        with f.open("r", encoding="utf-8") as _f:
            rows.extend(json.loads(line) for line in _f)
    assert all(r["path"] not in ("local", "perplexity-fallback") for r in rows)


# --- constraints -----------------------------------------------------------------------


def test_module_reads_no_secrets_and_leaves_the_ladder_alone():
    import inspect

    src = inspect.getsource(router)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in src
    assert "BUDGET_TIERS" not in src and "SizingSuggester" not in src


# --- step 2 (approved 2026-10-08): probe + heartbeat -> LocalStatus ---------------------


class _Lease:
    def __init__(self, acquired_at_epoch):
        self.acquired_at_epoch = acquired_at_epoch


@pytest.mark.parametrize(
    "mac_status,expected",
    [("offline", LocalStatus.OFF), ("not_found", LocalStatus.OFF), ("timeout", LocalStatus.UNREACHABLE),
     ("device_error", LocalStatus.ERRORED), ("banana", LocalStatus.UNKNOWN)],
)
def test_probe_status_maps_to_local_status(mac_status, expected):
    assert router.local_status_from_probe(mac_status, None, now_epoch=1000) == expected


@pytest.mark.parametrize(
    "hb,expected",
    [
        (None, LocalStatus.UNKNOWN),  # lease not read: stand down
        (router.Heartbeat(marker="success"), LocalStatus.SUCCEEDED),
        (router.Heartbeat(marker="failure"), LocalStatus.ERRORED),
        (router.Heartbeat(lease=None), LocalStatus.OFF),  # no lease at backup time: act
        (router.Heartbeat(lease=_Lease(1000 - 299)), LocalStatus.IN_PROGRESS),
        (router.Heartbeat(lease=_Lease(1000 - 300)), LocalStatus.ERRORED),  # 300s threshold, inclusive
    ],
)
def test_online_mac_status_comes_from_the_heartbeat(hb, expected):
    assert router.local_status_from_probe("online", hb, now_epoch=1000) == expected


def test_stale_threshold_defaults_to_300_and_honours_the_env_var(monkeypatch):
    monkeypatch.delenv("DCA_LOCAL_HEARTBEAT_STALE_S", raising=False)
    assert router.heartbeat_stale_seconds() == 300
    monkeypatch.setenv("DCA_LOCAL_HEARTBEAT_STALE_S", "120")
    assert router.heartbeat_stale_seconds() == 120
    monkeypatch.setenv("DCA_LOCAL_HEARTBEAT_STALE_S", "junk")
    assert router.heartbeat_stale_seconds() == 300


def test_fallback_plan_source_delegates_to_choose_plan_path():
    import dca_order_plan as dop

    d = router.decide(ExecutorRole.PERPLEXITY, LocalStatus.OFF, now=NOW)
    assert router.fallback_plan_source(d, "offline") == dop.choose_plan_path("offline", plan=None, elapsed_s=0)
    stand_down = router.decide(ExecutorRole.PERPLEXITY, LocalStatus.SUCCEEDED, now=NOW)
    assert router.fallback_plan_source(stand_down, "online") is None


# --- Copilot's review of #1643 (written test-first) -------------------------------------------------------------
# Finding: guard_broker only wrapped ``place_order``, but this repo's brokers place through ``place_market_buy`` and
# ``place_whole_share_buy`` (near_close_dca.BrokerAdapter / dca_live_adapters.RobinhoodBroker), which ``__getattr__``
# forwarded straight to the wrapped broker even when the routing decision forbids placement.


class RepoShapedSpyBroker:
    """The placement surface the real brokers expose."""

    def __init__(self):
        self.calls = []

    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.calls.append(("place_market_buy", ticker, notional_usd, ref_id))
        return "order-1"

    def place_whole_share_buy(self, ticker, quantity, ref_id):
        self.calls.append(("place_whole_share_buy", ticker, quantity, ref_id))
        return "order-2"

    def get_account(self):  # a read: must stay reachable through the wrapper
        return {"ok": True}


@pytest.mark.parametrize("status", list(LocalStatus))
def test_a_cloud_wrapped_broker_cannot_reach_the_repos_real_placement_methods(status):
    spy = RepoShapedSpyBroker()
    guarded = router.guard_broker(spy, ExecutorRole.CLOUD, status, now=NOW)
    with pytest.raises(router.OrderPlacementForbidden):
        guarded.place_market_buy("MU", 5.0, "ref-1")
    with pytest.raises(router.OrderPlacementForbidden):
        guarded.place_whole_share_buy("BYDDY", 1, "ref-2")
    assert spy.calls == []


def test_a_standing_down_backup_cannot_place_through_the_repos_real_methods():
    spy = RepoShapedSpyBroker()
    guarded = router.guard_broker(spy, ExecutorRole.PERPLEXITY, LocalStatus.IN_PROGRESS, now=NOW)  # local is alive
    with pytest.raises(router.OrderPlacementForbidden):
        guarded.place_market_buy("MU", 5.0, "ref-1")
    assert spy.calls == []


def test_a_permitted_role_still_places_through_the_real_methods_and_reads_stay_reachable():
    spy = RepoShapedSpyBroker()
    guarded = router.guard_broker(spy, ExecutorRole.LOCAL, LocalStatus.REACHABLE, now=NOW)
    assert guarded.place_market_buy("MU", 5.0, "ref-1") == "order-1"
    assert guarded.place_whole_share_buy("BYDDY", 1, "ref-2") == "order-2"
    assert guarded.get_account() == {"ok": True}
    assert [c[0] for c in spy.calls] == ["place_market_buy", "place_whole_share_buy"]


def test_a_forbidden_wrapper_still_lets_reads_through():
    guarded = router.guard_broker(RepoShapedSpyBroker(), ExecutorRole.CLOUD, LocalStatus.OFF, now=NOW)
    assert guarded.get_account() == {"ok": True}
