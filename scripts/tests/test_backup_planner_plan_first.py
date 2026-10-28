"""Tests for the plan-first paths of ``scripts/backup_planner.py`` (PR #1667 follow-up).

The Perplexity backups must keep working once the Claude slots become plan-first readers:

* ``plan --source mac_rec|published_rec`` reads the primary's own {ticker, usd, ref_id} set under the local
  reader's freshness rule (``dca_rec_store.RecommendationStore.select_for``) and emits a plan ``finalize`` accepts;
* ``finalize`` places such a plan like a Claude plan (this slot's window only, no ``--local-status`` needed; the
  kill switch is rechecked from the set's ``sizing_guard`` stamp, minus the set's own orders) but still proves rule 6 against a fresh prior close, and keeps every cap all-or-nothing;
* ``classify`` recognises Claude's plan-first orders, which carry no ``dca-runner-`` reference at the broker (the
  broker echoes its own ref_id), and still stands down on anything else;
* ``executor-ids`` lists the order ids the reader saved on the Mac.

Expected values come from the reader's rule and the native ref_id derivation, not from the planner's own output.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import dca_config
import dca_order_plan as dop
from dca_rec_models import Recommendation, RecommendationSet
from dca_rec_publish import build_document
from dca_rec_store import RecommendationStore
from dca_runner import ET, BrokerOrder
from scripts import backup_planner
from scripts.validate_native_placement import native_ref_id

DAY = "2026-10-07"
SLOT = "1130"
NOW = "2026-10-07T11:49:30-04:00"
ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
CONFIG = Path(__file__).resolve().parents[2] / "config"


def _uuid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"backup-planner-plan-first-test/{label}"))


def _raw(symbol, *, amount="2.00", agent="agentic", ref=None, side="buy", state="filled",
         created=f"{DAY}T15:40:00+00:00", order_id=None):
    """A raw ``get_equity_orders`` row. 15:40 UTC is 11:40 ET, inside the 11:30 slot's window."""
    return {
        "id": order_id or _uuid(f"id/{symbol}/{created}/{amount}"),
        "ref_id": ref if ref is not None else _uuid(f"broker-ref/{symbol}/{created}"),  # the broker's own UUID
        "symbol": symbol, "side": side, "type": "market", "state": state,
        "dollar_based_amount": {"amount": amount, "currency_code": "USD"},
        "time_in_force": "gfd", "market_hours": "regular_hours", "trigger": "immediate",
        "placed_agent": agent, "created_at": created,
    }


def _envelope(rows=()):
    return {"account_number": ACCOUNT, "trading_day": DAY, "pages": [{"orders": list(rows)}]}


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(path)


def _run(capsys, argv):
    code = backup_planner.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out), out


def _common(extra=()):
    return ["--trading-day", DAY, "--slot", SLOT, *extra]


@pytest.fixture(scope="module")
def config():
    return dca_config.load_strategy_config(dca_config.get_config_dir())


_UNSET = object()


def _set(items, *, as_of="2026-10-07T11:45:00-04:00", slot=SLOT, day=DAY, ladder=None, universe=None, config=None,
         max_multiplier=1.0, guard=_UNSET):
    """A RecommendationSet built the way dca_recommender builds one: native UUID ref_ids, current version stamps.

    Stamped at 1x by default, as a recommender writes it; ``guard=None`` builds an older, unstamped set."""
    if guard is _UNSET:
        guard = {"max_multiplier": max_multiplier, "deployed_7d_usd": 0.0}
    config = config or dca_config.load_strategy_config(dca_config.get_config_dir())
    return RecommendationSet(
        date.fromisoformat(day), datetime.fromisoformat(as_of), slot, "robinhood_agent_fetch",
        ladder or dop.current_ladder_version(), universe or dop.universe_hash(config.tickers),
        tuple(Recommendation(item[0], item[1], item[2] if len(item) > 2 else native_ref_id(slot, day, item[0]))
              for item in items),
        sizing_guard=guard,
    )


def _published(tmp_path, sets, name="published.json"):
    document = build_document({s.slot: s for s in sets}, generated_at=datetime.fromisoformat(NOW))
    return _write(tmp_path, name, document)


# ---------------------------------------------------------------------------
# plan --source published_rec / mac_rec
# ---------------------------------------------------------------------------


def test_a_fresh_published_set_becomes_a_plan_first_plan(tmp_path, capsys):
    published = _published(tmp_path, [_set([("MSFT", 4.0), ("AAPL", 2.5)])])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert (code, report["status"], report["errors"]) == (0, "VALID_PLAN", [])
    plan = report["plan"]
    assert (plan["generator"], plan["source"], plan["slot"], plan["trading_day"]) == ("plan_first", "published_rec", SLOT, DAY)
    assert plan["orders"] == [
        {"symbol": "MSFT", "notional_usd": 4.0, "ref_id": native_ref_id(SLOT, DAY, "MSFT")},
        {"symbol": "AAPL", "notional_usd": 2.5, "ref_id": native_ref_id(SLOT, DAY, "AAPL")},
    ]


@pytest.mark.parametrize(("as_of", "reason"), [
    ("2026-10-07T11:30:00-04:00", "stale"),                        # 19.5 minutes old; the rule allows 15
    ("2026-10-07T11:55:00-04:00", "only_future_stamped_files"),    # stamped after now
])
def test_the_published_set_obeys_the_local_readers_freshness_rule(tmp_path, capsys, as_of, reason):
    published = _published(tmp_path, [_set([("MSFT", 4.0)], as_of=as_of)])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert (code, report["status"], report["plan"]) == (2, "NO_VALID_PLAN", None)
    assert [e["code"] for e in report["errors"]] == [reason]


def test_exactly_fifteen_minutes_old_is_still_fresh(tmp_path, capsys):
    published = _published(tmp_path, [_set([("MSFT", 4.0)], as_of="2026-10-07T11:34:30-04:00")])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert (code, report["status"]) == (0, "VALID_PLAN")


def test_yesterdays_set_is_never_used(tmp_path, capsys):
    published = _published(tmp_path, [_set([("MSFT", 4.0)], as_of="2026-10-06T11:45:00-04:00", day="2026-10-06")])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert (code, report["status"]) == (2, "NO_VALID_PLAN")
    assert [e["code"] for e in report["errors"]] == ["file_identity_mismatch"]


def test_a_payload_without_this_slot_is_no_valid_plan(tmp_path, capsys):
    published = _published(tmp_path, [_set([("MSFT", 4.0)], slot="1230")])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert (code, [e["code"] for e in report["errors"]]) == (2, ["no_set_for_slot"])


@pytest.mark.parametrize("payload", ["not json", json.dumps([1]), json.dumps({"schema_version": 1, "slots": {}}),
                                     json.dumps({"schema_version": True, "generated_at": NOW, "slots": {}})])
def test_an_unreadable_or_malformed_copy_falls_back_instead_of_being_read_around(tmp_path, capsys, payload):
    published = _write(tmp_path, "published.json", payload)
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert (code, report["status"], report["plan"]) == (2, "NO_VALID_PLAN", None)
    assert report["errors"][0]["code"] in {"published_corrupt", "published_invalid"}


def test_a_set_with_a_price_field_is_refused(tmp_path, capsys):
    document = json.loads(Path(_published(tmp_path, [_set([("MSFT", 4.0)])])).read_text())
    document["slots"][SLOT]["items"][0]["prior_close"] = 101.0
    published = _write(tmp_path, "tampered.json", document)
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert (code, report["status"]) == (2, "NO_VALID_PLAN")


@pytest.mark.parametrize(("items", "kwargs", "code"), [
    ([("MSFT", 4.0, str(uuid.uuid4()))], {}, "bad_ref_id"),                 # not this slot/day/ticker's native UUID
    ([("ZZZZ", 4.0)], {}, "unknown_symbol"),
    ([("MSFT", 999.0)], {}, "notional_over_cap"),
    ([("MSFT", 4.0)], {"ladder": "old-ladder"}, "stale_ladder_version"),
    ([("MSFT", 4.0)], {"universe": "0" * 64}, "universe_hash_mismatch"),
])
def test_a_set_the_readers_own_config_rejects_is_no_valid_plan(tmp_path, capsys, items, kwargs, code):
    published = _published(tmp_path, [_set(items, **kwargs)])
    exit_code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert (exit_code, report["status"]) == (2, "NO_VALID_PLAN")
    assert code in [e["code"] for e in report["errors"]]


def test_published_rec_without_the_file_argument_fails(capsys):
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--now", NOW, *_common()])
    assert (code, report["status"]) == (2, "FAILED")


def test_now_must_be_on_the_trading_day(tmp_path, capsys):
    published = _published(tmp_path, [_set([("MSFT", 4.0)])])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published,
                                    "--now", "2026-10-08T11:49:30-04:00", *_common()])
    assert (code, report["status"], [e["code"] for e in report["errors"]]) == (2, "FAILED", ["bad_now"])


def test_a_missing_freshness_rule_fails_closed_instead_of_defaulting(tmp_path, capsys):
    config_dir = tmp_path / "config"
    shutil.copytree(CONFIG, config_dir)
    (config_dir / "recommendations.json").unlink()
    published = _published(tmp_path, [_set([("MSFT", 4.0)])])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW,
                                    *_common(["--config-dir", str(config_dir)])])
    assert (code, report["status"], [e["code"] for e in report["errors"]]) == (2, "FAILED", ["settings_invalid"])


def test_mac_rec_reads_the_macs_local_store_by_the_same_rule(tmp_path, capsys):
    root = tmp_path / "recommendations"
    day_dir = root / DAY
    day_dir.mkdir(parents=True)
    older, newer = _set([("MSFT", 4.0)], as_of="2026-10-07T11:40:00-04:00"), _set([("AAPL", 3.0)])
    for rec in (older, newer):
        stamp = rec.as_of.astimezone(ET).strftime("%H%M")
        (day_dir / f"rec_{SLOT}_{stamp}.json").write_text(json.dumps(rec.to_json()))
    code, report, _ = _run(capsys, ["plan", "--source", "mac_rec", "--rec-root", str(root), "--now", NOW, *_common()])
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert report["plan"]["source"] == "mac_rec"
    assert [o["symbol"] for o in report["plan"]["orders"]] == ["AAPL"]  # the newest set, never a merge


def test_mac_rec_with_an_empty_store_is_no_valid_plan(tmp_path, capsys):
    code, report, _ = _run(capsys, ["plan", "--source", "mac_rec", "--rec-root", str(tmp_path), "--now", NOW, *_common()])
    assert (code, report["status"], [e["code"] for e in report["errors"]]) == (2, "NO_VALID_PLAN", ["no_file_for_slot_today"])


def test_the_planner_selects_exactly_what_the_local_reader_selects(tmp_path):
    """Parity: the published path and RecommendationStore.select_for agree on every boundary case."""
    store = RecommendationStore(tmp_path)
    now = datetime.fromisoformat(NOW)
    settings_age = timedelta(minutes=15)
    for minutes_old in (-1, 0, 5, 15, 16, 60):
        rec = _set([("MSFT", 4.0)], as_of=(now - timedelta(minutes=minutes_old)).isoformat())
        day_dir = store.day_dir(rec.trading_day)
        shutil.rmtree(day_dir, ignore_errors=True)
        day_dir.mkdir(parents=True)
        (day_dir / f"rec_{SLOT}_{rec.as_of.astimezone(ET).strftime('%H%M')}.json").write_text(json.dumps(rec.to_json()))
        selection = store.select_for(SLOT, now, settings_age)
        reason = backup_planner._freshness_reason(rec, SLOT, now, settings_age)
        assert (selection.status == "OK") == (reason is None), minutes_old
        if reason is not None:
            assert selection.reason == reason, minutes_old


# ---------------------------------------------------------------------------
# finalize on a plan-first plan
# ---------------------------------------------------------------------------


def _plan_first_plan(tmp_path, capsys, items=(("MSFT", 4.0), ("AAPL", 2.5))):
    published = _published(tmp_path, [_set(list(items))])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert code == 0, report
    return _write(tmp_path, "plan.json", report["plan"])


def _finalize_argv(tmp_path, plan, rows=(), *, quotes=None, prior=None, spreads=None, bp="1000", extra=()):
    quotes = {"MSFT": 100.0, "AAPL": 100.0} if quotes is None else quotes
    spreads = {s: {"bid": 99.9, "ask": 100.1} for s in ("MSFT", "AAPL")} if spreads is None else spreads
    argv = ["finalize", *_common(["--plan", plan, "--orders-pages", _write(tmp_path, "pages.json", _envelope(rows)),
                                  "--fresh-quotes", _write(tmp_path, "fresh.json", quotes),
                                  "--fresh-spreads", _write(tmp_path, "spreads.json", spreads),
                                  "--buying-power", bp, "--now", NOW, *extra])]
    if prior is not False:
        prior = {"MSFT": 101.0, "AAPL": 101.0} if prior is None else prior
        argv += ["--fresh-prior-closes", _write(tmp_path, "prior.json", prior)]
    return argv


def test_a_plan_first_plan_proceeds_with_the_native_ref_ids_and_no_local_status(tmp_path, capsys):
    plan = _plan_first_plan(tmp_path, capsys)
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, plan))
    assert (code, report["status"], report["errors"]) == (0, "PROCEED", [])
    assert report["to_place"] == [
        {"symbol": "MSFT", "notional_usd": 4.0, "ref_id": native_ref_id(SLOT, DAY, "MSFT")},
        {"symbol": "AAPL", "notional_usd": 2.5, "ref_id": native_ref_id(SLOT, DAY, "AAPL")},
    ]


def _plan_first_plan_from(tmp_path, capsys, recommendation):
    published = _published(tmp_path, [recommendation])
    code, report, _ = _run(capsys, ["plan", "--source", "published_rec", "--published", published, "--now", NOW, *_common()])
    assert code == 0, report
    return report["plan"], _write(tmp_path, "plan.json", report["plan"])


def test_an_above_1x_set_aborts_when_spend_since_sizing_engages_the_kill_switch(tmp_path, capsys):
    """The 2026-10-15 Codex finding: the set was sized above 1x, then $1,500 of other spend landed in the 7 days."""
    _, plan = _plan_first_plan_from(tmp_path, capsys, _set([("MSFT", 4.0)], max_multiplier=3.0))
    rows = [_raw("VT", amount="1500.00", created="2026-10-03T14:00:00+00:00")]
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, plan, rows))
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert [e["code"] for e in report["errors"]] == ["kill_switch_engaged_since_plan"]


def test_a_set_sized_at_1x_needs_no_kill_switch_recheck(tmp_path, capsys):
    """At 1x nothing can be over the cap, so the same history does not trip the recheck (other caps may still bind)."""
    _, plan = _plan_first_plan_from(tmp_path, capsys, _set([("MSFT", 4.0)], max_multiplier=1.0))
    rows = [_raw("VT", amount="1500.00", created="2026-10-03T14:00:00+00:00")]
    _, report, _ = _run(capsys, _finalize_argv(tmp_path, plan, rows))
    assert "kill_switch_engaged_since_plan" not in [e["code"] for e in report["errors"]]


def test_an_unstamped_plan_first_set_fails_closed_at_finalize(tmp_path, capsys):
    _, plan = _plan_first_plan_from(tmp_path, capsys, _set([("MSFT", 4.0)], guard=None))
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, plan))
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert [e["code"] for e in report["errors"]] == ["sizing_guard_missing"]


def test_the_recheck_excludes_the_sets_own_orders_so_a_partly_placed_batch_is_not_aborted():
    """$1,400 of other spend is under the $1,440 threshold; the set's own $100 fill would cross it if it counted."""
    own_ref = native_ref_id(SLOT, DAY, "MSFT")
    plan = {"sizing_guard": {"max_multiplier": 3.0, "deployed_7d_usd": 0.0},
            "orders": [{"symbol": "MSFT", "notional_usd": 4.0, "ref_id": own_ref}]}
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    other = BrokerOrder("VT", "buy", 1400.0, datetime.fromisoformat("2026-10-03T14:00:00+00:00"), state="filled")
    own = BrokerOrder("MSFT", "buy", 100.0, datetime.fromisoformat(f"{DAY}T15:40:00+00:00"), state="filled", ref_id=own_ref)
    assert backup_planner._plan_first_kill_switch_error(plan, [other, own], config, date.fromisoformat(DAY), NOW) is None
    over = BrokerOrder("VT", "buy", 1500.0, datetime.fromisoformat("2026-10-03T14:00:00+00:00"), state="filled")
    error = backup_planner._plan_first_kill_switch_error(plan, [over, own], config, date.fromisoformat(DAY), NOW)
    assert error is not None and error.code == "kill_switch_engaged_since_plan"


def test_a_plan_first_plan_needs_the_fresh_prior_closes(tmp_path, capsys):
    plan = _plan_first_plan(tmp_path, capsys)
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, plan, prior=False))
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert [e["code"] for e in report["errors"]] == ["fresh_prior_closes_missing"]


def test_rule_6_still_binds_a_plan_first_plan(tmp_path, capsys):
    plan = _plan_first_plan(tmp_path, capsys)
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, plan, prior={"MSFT": 99.0}))
    assert (code, report["status"]) == (0, "PROCEED")
    assert report["to_place"] == []
    assert {(s["symbol"], s["reason"]) for s in report["skipped"]} == {
        ("MSFT", "quote_not_below_prior_close"), ("AAPL", "no_prior_close")}


def test_a_ticker_claude_bought_in_this_slot_is_dropped_but_an_earlier_slots_buy_is_topped_up(tmp_path, capsys):
    plan = _plan_first_plan(tmp_path, capsys)
    rows = [_raw("MSFT", amount="4.00"),                                    # Claude's 11:40 buy, this slot
            _raw("AAPL", amount="1.00", created=f"{DAY}T14:40:00+00:00")]   # 10:40 ET, the 10:30 slot
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, plan, rows))
    assert (code, report["status"]) == (0, "PROCEED")
    assert [o["symbol"] for o in report["to_place"]] == ["AAPL"]
    assert report["skipped"] == [{"symbol": "MSFT", "reason": "already_placed"}]


def test_a_plan_first_plan_trims_to_the_caps_instead_of_aborting(tmp_path, capsys):
    """Trim to fit (2026-10-22, reverses ADR D4's all-or-nothing): the first order does not fit $2 of headroom, so
    nothing is placed, every order is reported, and the run is a PROCEED with nothing to place, not an ABORT."""
    plan = _plan_first_plan(tmp_path, capsys)
    rows = [_raw("VT", amount="178.00", created=f"{DAY}T14:00:00+00:00")]  # $178 of the $180 day already spent
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, plan, rows))
    assert (code, report["status"], report["to_place"], report["errors"]) == (0, "PROCEED", [], [])
    assert {s["reason"] for s in report["skipped"]} == {"over_daily_cap"}


def test_a_tampered_plan_first_file_is_refused_at_finalize(tmp_path, capsys):
    plan_path = Path(_plan_first_plan(tmp_path, capsys))
    document = json.loads(plan_path.read_text())
    document["orders"][0]["notional_usd"] = 40.0
    document["orders"][0]["ref_id"] = native_ref_id("1230", DAY, "MSFT")  # another slot's key
    plan_path.write_text(json.dumps(document))
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, str(plan_path)))
    assert (code, report["status"]) == (2, "ABORT")
    assert "bad_ref_id" in [e["code"] for e in report["errors"]]


def test_finalize_output_never_carries_the_account_number(tmp_path, capsys):
    plan = _plan_first_plan(tmp_path, capsys)
    _, _, out = _run(capsys, _finalize_argv(tmp_path, plan, [_raw("MSFT", amount="4.00")]))
    assert ACCOUNT not in out


# ---------------------------------------------------------------------------
# classify and executor-ids
# ---------------------------------------------------------------------------


def _classify(tmp_path, capsys, rows, extra=()):
    pages = _write(tmp_path, "poll1.json", _envelope(rows))
    return _run(capsys, ["classify", *_common(["--orders-pages", pages, *extra])])


def _executor_ids(tmp_path, ids, *, day=DAY, slot=SLOT, status="OK"):
    return _write(tmp_path, "executor_ids.json", {"status": status, "trading_day": day, "slot": slot,
                                                  "order_ids": ids, "errors": []})


def test_claudes_plan_first_order_is_unrecognized_without_evidence(tmp_path, capsys):
    """The broker echoes its own ref_id, so nothing in the row says Claude placed it: fail closed."""
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", amount="4.00")])
    assert (code, report["status"]) == (2, "UNRECOGNIZED")
    assert report["unrecognized_orders"][0]["symbol"] == "MSFT"


def test_the_order_id_claudes_reader_saved_recognises_its_order(tmp_path, capsys):
    row = _raw("MSFT", amount="4.00")
    code, report, _ = _classify(tmp_path, capsys, [row], ["--executor-ids", _executor_ids(tmp_path, [row["id"]])])
    assert (code, report["status"]) == (0, "CLEAR")
    assert [(o["symbol"], o["basis"]) for o in report["executor_orders"]] == [("MSFT", "plan_first_placement")]


def test_the_published_set_recognises_an_exact_symbol_and_amount_match_only(tmp_path, capsys):
    published = _published(tmp_path, [_set([("MSFT", 4.0)], as_of="2026-10-07T11:00:00-04:00")])  # stale is fine here
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", amount="4.000000")], ["--published", published])
    assert (code, report["status"], report["published_set_used"]) == (0, "CLEAR", True)
    assert report["executor_orders"][0]["basis"] == "published_recommendation"
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", amount="4.01")], ["--published", published])
    assert (code, report["status"]) == (2, "UNRECOGNIZED")


def test_a_published_set_from_another_day_recognises_nothing(tmp_path, capsys):
    published = _published(tmp_path, [_set([("MSFT", 4.0)], as_of="2026-10-06T11:45:00-04:00", day="2026-10-06")])
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", amount="4.00")], ["--published", published])
    assert (code, report["status"], report["published_set_used"]) == (2, "UNRECOGNIZED", False)


def test_a_sell_is_never_recognised_from_the_published_set(tmp_path, capsys):
    published = _published(tmp_path, [_set([("MSFT", 4.0)])])
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", amount="4.00", side="sell")], ["--published", published])
    assert (code, report["status"]) == (2, "UNRECOGNIZED")


@pytest.mark.parametrize("agent", ["user", "recurring", "drip"])
def test_the_users_own_activity_never_blocks(tmp_path, capsys, agent):
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", agent=agent)])
    assert (code, report["status"], report["user_orders"]) == (0, "CLEAR", 1)


def test_legacy_runner_references_are_still_recognised(tmp_path, capsys):
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", ref=f"dca-runner-{SLOT}-{DAY}-MSFT")])
    assert (code, report["status"], report["executor_orders"][0]["basis"]) == (0, "CLEAR", "runner_reference")
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", ref=f"dca-runner-1230-{DAY}-MSFT")])
    assert (code, report["status"]) == (2, "UNRECOGNIZED")  # another slot's reference is not this slot's traffic


def test_the_native_reference_is_recognised_should_the_broker_ever_echo_it(tmp_path, capsys):
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", ref=native_ref_id(SLOT, DAY, "MSFT"))])
    assert (code, report["executor_orders"][0]["basis"]) == (0, "native_reference")


def test_orders_outside_this_slots_window_are_not_classified(tmp_path, capsys):
    rows = [_raw("MSFT", created=f"{DAY}T14:40:00+00:00"), _raw("AAPL", created=f"{DAY}T16:30:00+00:00")]  # 10:40, 12:30 ET
    code, report, _ = _classify(tmp_path, capsys, rows)
    assert (code, report["status"], report["executor_orders"], report["unrecognized_orders"]) == (0, "CLEAR", [], [])


@pytest.mark.parametrize(("kwargs", "bad"), [({"slot": "1230"}, True), ({"day": "2026-10-06"}, True),
                                             ({"status": "FAILED"}, True), ({}, False)])
def test_executor_ids_for_another_day_or_slot_are_refused(tmp_path, capsys, kwargs, bad):
    row = _raw("MSFT")
    code, report, _ = _classify(tmp_path, capsys, [row], ["--executor-ids", _executor_ids(tmp_path, [row["id"]], **kwargs)])
    if bad:
        assert (code, report["status"], [e["code"] for e in report["errors"]]) == (2, "FAILED", ["executor_ids_invalid"])
    else:
        assert (code, report["status"]) == (0, "CLEAR")


def test_classify_output_names_no_account_number_and_no_order_id(tmp_path, capsys):
    row = _raw("MSFT", amount="4.00")
    _, _, out = _classify(tmp_path, capsys, [row, _raw("AAPL")], ["--executor-ids", _executor_ids(tmp_path, [row["id"]])])
    assert ACCOUNT not in out and row["id"] not in out


def test_incomplete_pages_fail_classify(tmp_path, capsys):
    pages = _write(tmp_path, "poll1.json", {"account_number": ACCOUNT, "trading_day": DAY,
                                           "pages": [{"orders": [_raw("MSFT")], "next": "cursor-2"}]})
    code, report, _ = _run(capsys, ["classify", *_common(["--orders-pages", pages])])
    assert (code, report["status"]) == (2, "FAILED")


def _placement(order_id, state="queued"):
    return {"data": {"order": {"id": order_id, "symbol": "X", "side": "buy", "state": state}}}


def test_executor_ids_lists_the_order_ids_the_reader_saved(tmp_path, capsys):
    store = RecommendationStore(tmp_path)
    directory = store.placements_dir(date.fromisoformat(DAY), SLOT)
    directory.mkdir(parents=True)
    (directory / "MSFT.json").write_text(json.dumps(_placement("order-b")))
    (directory / "AAPL.json").write_text(json.dumps(_placement("order-a")))
    (directory / "NVDA.json").write_text(json.dumps({"error": "refused before reaching the broker"}))
    (directory / "prior_ids.json").write_text(json.dumps(["order-from-earlier"]))  # history, not placements
    (directory / "orders_01.json").write_text(json.dumps({"orders": []}))
    code, report, _ = _run(capsys, ["executor-ids", "--rec-root", str(tmp_path), *_common()])
    assert (code, report["status"], report["order_ids"]) == (0, "OK", ["order-a", "order-b"])
    assert (report["trading_day"], report["slot"]) == (DAY, SLOT)


def test_executor_ids_with_no_placements_is_an_empty_list(tmp_path, capsys):
    code, report, _ = _run(capsys, ["executor-ids", "--rec-root", str(tmp_path), *_common()])
    assert (code, report["order_ids"]) == (0, [])


def test_executor_ids_refuses_an_unreadable_placement(tmp_path, capsys):
    directory = RecommendationStore(tmp_path).placements_dir(date.fromisoformat(DAY), SLOT)
    directory.mkdir(parents=True)
    (directory / "MSFT.json").write_text("{not json")
    code, report, _ = _run(capsys, ["executor-ids", "--rec-root", str(tmp_path), *_common()])
    assert (code, report["status"], [e["code"] for e in report["errors"]]) == (2, "FAILED", ["placement_corrupt"])


# 2026-10-26: the 09:35 stand-down (on 25 orders a manual backup run placed, not Claude's: see
# docs/incident_2026-10-26_0935_unrecognized_orders.md) exposed a gap. The live slots still run the gate-sequence
# prompt (claude/dca-executor-prompts/live-mirror/), which saves each response as `$ROOT/place_<TICKER>.json` in the
# slot's run directory, never under recommendations/<day>/placements/<slot>/, so executor-ids would print an empty
# list for a day Claude did buy. These pin that the live prompt's own responses are read too.

def _legacy_run_dir(cron_root, *, slot=SLOT, folder=None):
    from scripts.verify_schedule import slot_run_dir_relative

    relative = slot_run_dir_relative(slot, date.fromisoformat(DAY))
    directory = cron_root / relative.parent / (folder or relative.name)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _executor_ids_argv(rec_root, cron_root, slot=SLOT):
    return ["executor-ids", "--rec-root", str(rec_root), "--cron-root", str(cron_root),
            "--trading-day", DAY, "--slot", slot]


def test_executor_ids_lists_the_live_prompts_saved_responses(tmp_path, capsys):
    cron_root = tmp_path / "cron"
    directory = _legacy_run_dir(cron_root)
    (directory / "place_MSFT.json").write_text(json.dumps(_placement("order-live-b")))
    (directory / "place_BRK.B.json").write_text(json.dumps(_placement("order-live-a")))
    (directory / "prior_ids_MSFT.json").write_text(json.dumps(["order-from-earlier"]))  # history, not placements
    (directory / "cands_2026-10-07_1130.json").write_text(json.dumps([]))
    code, report, _ = _run(capsys, _executor_ids_argv(tmp_path / "rec", cron_root))
    assert (code, report["status"], report["order_ids"]) == (0, "OK", ["order-live-a", "order-live-b"])
    assert report["sources"] == {"plan_first_placements": 0, "live_prompt_placements": 2}


def test_executor_ids_merges_both_sources(tmp_path, capsys):
    cron_root = tmp_path / "cron"
    (_legacy_run_dir(cron_root) / "place_MSFT.json").write_text(json.dumps(_placement("order-live")))
    directory = RecommendationStore(tmp_path / "rec").placements_dir(date.fromisoformat(DAY), SLOT)
    directory.mkdir(parents=True)
    (directory / "AAPL.json").write_text(json.dumps(_placement("order-reader")))
    code, report, _ = _run(capsys, _executor_ids_argv(tmp_path / "rec", cron_root))
    assert (code, report["order_ids"]) == (0, ["order-live", "order-reader"])
    assert report["sources"] == {"plan_first_placements": 1, "live_prompt_placements": 1}


def test_executor_ids_reads_the_1530_prompts_dashed_run_directory(tmp_path, capsys):
    """The 15:30 prompt resolves $ROOT as `<YYYY-MM-DD>_1530ET`, unlike the compact folder Gate 9 writes."""
    cron_root = tmp_path / "cron"
    (_legacy_run_dir(cron_root, slot="1530", folder=f"{DAY}_1530ET") / "place_MSFT.json").write_text(
        json.dumps(_placement("order-1530")))
    code, report, _ = _run(capsys, _executor_ids_argv(tmp_path / "rec", cron_root, slot="1530"))
    assert (code, report["order_ids"]) == (0, ["order-1530"])


def test_executor_ids_ignores_another_slots_live_responses(tmp_path, capsys):
    cron_root = tmp_path / "cron"
    (_legacy_run_dir(cron_root, slot="1330") / "place_MSFT.json").write_text(json.dumps(_placement("order-1330")))
    code, report, _ = _run(capsys, _executor_ids_argv(tmp_path / "rec", cron_root))
    assert (code, report["order_ids"]) == (0, [])


def test_executor_ids_refuses_an_unreadable_live_response(tmp_path, capsys):
    cron_root = tmp_path / "cron"
    (_legacy_run_dir(cron_root) / "place_MSFT.json").write_text("{not json")
    code, report, _ = _run(capsys, _executor_ids_argv(tmp_path / "rec", cron_root))
    assert (code, report["status"], [e["code"] for e in report["errors"]]) == (2, "FAILED", ["placement_corrupt"])


def test_live_prompt_order_id_feeds_classify(tmp_path, capsys):
    """The 2026-10-26 case end to end: Claude's live order, no plan-first files at all, classifies as executor."""
    cron_root = tmp_path / "cron"
    row = _raw("MSFT", amount="4.00")
    (_legacy_run_dir(cron_root) / "place_MSFT.json").write_text(json.dumps(_placement(row["id"])))
    _, _, printed = _run(capsys, _executor_ids_argv(tmp_path / "rec", cron_root))
    saved = _write(tmp_path, "executor_ids.json", printed)
    code, report, _ = _classify(tmp_path, capsys, [row], ["--executor-ids", saved])
    assert (code, report["status"]) == (0, "CLEAR")
    assert report["executor_orders"][0]["basis"] == "plan_first_placement"


def test_executor_ids_output_saved_verbatim_feeds_classify(tmp_path, capsys):
    """The prompt's round trip: the Mac prints, the task saves the text untouched, classify reads it."""
    directory = RecommendationStore(tmp_path).placements_dir(date.fromisoformat(DAY), SLOT)
    directory.mkdir(parents=True)
    row = _raw("MSFT", amount="4.00")
    (directory / "MSFT.json").write_text(json.dumps(_placement(row["id"])))
    _, _, printed = _run(capsys, ["executor-ids", "--rec-root", str(tmp_path), *_common()])
    saved = _write(tmp_path, "executor_ids.json", printed)
    code, report, _ = _classify(tmp_path, capsys, [row], ["--executor-ids", saved])
    assert (code, report["status"]) == (0, "CLEAR")


# ---------------------------------------------------------------------------
# Review on #1702: an invalid optional publication is no evidence, not a failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["not json", json.dumps({"schema_version": 1, "generated_at": NOW, "slots": []})])
def test_an_invalid_publication_with_empty_history_is_clear(tmp_path, capsys, payload):
    published = _write(tmp_path, "published.json", payload)
    code, report, _ = _classify(tmp_path, capsys, [], ["--published", published])
    assert (code, report["status"], report["published_set_used"]) == (0, "CLEAR", False)


def test_an_invalid_publication_never_recognises_an_order(tmp_path, capsys):
    published = _write(tmp_path, "published.json", json.dumps({"schema_version": 1, "generated_at": NOW, "slots": []}))
    code, report, _ = _classify(tmp_path, capsys, [_raw("MSFT", amount="4.00")], ["--published", published])
    assert (code, report["status"], report["published_set_used"]) == (2, "UNRECOGNIZED", False)


# ---------------------------------------------------------------------------
# Review on #1702: step 7e validates with the native rule (the broker echoes its own ref_id)
# ---------------------------------------------------------------------------

_SUBMITTED = "2026-10-07T15:49:30+00:00"


def _response(order_id="new-order-1", *, symbol="MSFT", amount="4.00", state="queued", ref=None,
              created="2026-10-07T15:49:31+00:00"):
    return {"data": {"order": {"id": order_id, "symbol": symbol, "side": "buy", "state": state, "type": "market",
                               "dollar_based_amount": {"amount": amount}, "ref_id": ref or _uuid(f"broker/{order_id}"),
                               "created_at": created, "account_number": ACCOUNT}}}


def _check(tmp_path, capsys, response, *, rows=(), placed=(), amount="4.00", submitted=_SUBMITTED):
    argv = ["check-placement", *_common([
        "--response", _write(tmp_path, "placement_MSFT.json", response), "--symbol", "MSFT", "--amount", amount,
        "--sent-ref-id", native_ref_id(SLOT, DAY, "MSFT"), "--submitted-after", submitted,
        "--prior-orders-pages", _write(tmp_path, "poll2.json", _envelope(rows))])]
    if placed:
        argv += ["--placed-responses", *[_write(tmp_path, f"placement_{i}.json", r) for i, r in enumerate(placed)]]
    return _run(capsys, argv)


def test_an_accepted_order_with_the_brokers_own_ref_id_counts_as_placed(tmp_path, capsys):
    code, report, out = _check(tmp_path, capsys, _response())
    assert (code, report["status"], report["order_id"]) == (0, "PLACED", "new-order-1")
    assert ACCOUNT not in out


@pytest.mark.parametrize(("response", "kwargs"), [
    (_response("old-order"), {"rows": [_raw("MSFT", order_id="old-order")]}),           # was already in poll 2
    (_response("earlier"), {"placed": [_response("earlier")]}),                          # this run's earlier order
    (_response(created="2026-10-07T15:40:00+00:00"), {}),                                # created before the call
    (_response(amount="5.00"), {}),                                                      # another amount
    (_response(state="rejected"), {}),
    (_response(symbol="AAPL"), {}),
])
def test_anything_but_a_new_order_for_exactly_the_request_is_not_placed(tmp_path, capsys, response, kwargs):
    code, report, _ = _check(tmp_path, capsys, response, **kwargs)
    assert (code, report["status"], report["order_id"]) == (2, "NOT_PLACED", None)
    assert [e["code"] for e in report["errors"]] == ["placement_unconfirmed"]


def test_a_naive_submit_instant_fails_closed(tmp_path, capsys):
    code, report, _ = _check(tmp_path, capsys, _response(), submitted="2026-10-07T15:49:30")
    assert (code, report["status"]) == (2, "FAILED")


def test_a_plan_first_plan_finalizes_under_the_normal_tier_after_1699(tmp_path, capsys):
    """#1699 picks finalize's tier from each order's multiplier; a price-blind set has none, so it counts no 3x
    signals and keeps the Normal caps (and since 2026-10-22 no plan escalates at all). $175 spent today plus this
    $6.50 set fits Elevated's $240 but not Normal's $180, so only the prefix that fits is placed (the first order,
    $4), and it must not fail on the missing multiplier."""
    plan = _plan_first_plan(tmp_path, capsys)
    rows = [_raw("VT", amount="175.00", created=f"{DAY}T14:00:00+00:00")]
    code, report, _ = _run(capsys, _finalize_argv(tmp_path, plan, rows))
    assert (code, report["status"], report["errors"]) == (0, "PROCEED", [])
    assert [o["symbol"] for o in report["to_place"]] == ["MSFT"]
    assert {s["reason"] for s in report["skipped"]} == {"over_daily_cap"}
