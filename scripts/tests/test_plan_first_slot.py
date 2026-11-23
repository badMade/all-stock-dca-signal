"""Hermetic checks for plan-first slot helper contracts."""

import json
from datetime import datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.plan_first_slot as slot
from scripts.assemble_order_pages import _order_rows as _REAL_ORDER_ROWS

REAL_CLOCK = slot._clock_reason


@pytest.fixture(autouse=True)
def _inside_the_window(monkeypatch):
    # Most tests are about selection, caps and evidence, not the clock: pretend every call is
    # inside its slot's window. The clock rule has its own tests (REAL_CLOCK) below.
    monkeypatch.setattr(slot, "_clock_reason", lambda *_: None)

NORMAL = {"budget_tier": "normal", "daily_cap_usd": 180.0, "weekly_cap_usd": 900.0}


def _fake_store(rec, tmp_path, budget=NORMAL):
    """A store whose handed set is ``rec`` (what `begin` recorded) with its expected budget."""
    class Store:
        def __init__(self, root=None): pass
        def handed(self, *_): return rec
        def expected_for(self, *_): return SimpleNamespace(budget=budget)
        def placements_dir(self, *_): return tmp_path
    return Store


def _row(symbol, created, *, agent="user", usd="3.00", side="buy", state="filled", oid=None):
    return {"id": oid or f"{symbol}-{created}", "symbol": symbol, "side": side, "state": state,
            "placed_agent": agent, "created_at": created, "dollar_based_amount": {"amount": usd},
            "executed_notional": {"amount": usd}, "cumulative_quantity": "0.03", "average_price": "100"}


def test_now_requires_offset():
    with pytest.raises(ValueError):
        slot._now("2026-10-13T12:00")


def test_now_accepts_offset():
    assert slot._now("2026-10-13T12:00:00-04:00").utcoffset().total_seconds() == -14400


def test_emit_compact(capsys):
    slot._emit({"ok": True})
    assert capsys.readouterr().out == '{"ok":true}\n'


def test_operator_midnight_conversion():
    stamp = datetime.fromisoformat("2026-10-13T12:07:00-04:00")
    utc = datetime.combine(stamp.date(), time.min, slot.ET).astimezone(timezone.utc)
    assert utc.isoformat() == "2026-10-13T04:00:00+00:00"


def test_created_and_ticker_helpers():
    assert slot._created({"created_at": "2026-10-13T16:00:00Z"}).hour == 16
    assert slot._created({"created_at": "bad"}) is None
    assert slot._created({}) is None
    assert slot._ticker({"symbol": "aapl"}) == "AAPL"
    assert slot._is_buy({"side": "BUY"})
    assert not slot._is_buy({"side": "sell"})


def test_pages_and_prior_ids(tmp_path, monkeypatch):
    class Assembler:
        def assemble(self, paths, **kwargs):
            return ({"pages": [{"orders": [{"id": "x"}]}, {"data": {"data": {"orders": [{"id": "nested"}]}}}]}, 1)

        def _order_rows(self, page):
            return _REAL_ORDER_ROWS(page)
    monkeypatch.setitem(__import__("sys").modules, "scripts.assemble_order_pages", Assembler())
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "private")
    page = tmp_path / "orders_1.json"
    page.write_text("{}")
    pages, rows = slot._pages(str(page), now=datetime.now(slot.ET))
    # The assembler's own parser reads the nested envelope too; a one-level read saw it as empty (PR #1815 review).
    assert [row["id"] for row in rows] == ["x", "nested"]
    assert slot._prior_ids(pages) == ["x", "nested"]


def test_filter_rogue_and_operator_paths(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    # A fixed day: the rows below are dated 2026-10-13, and a wall-clock date would drift past them.
    asof = datetime.fromisoformat("2026-10-13T10:35:00-04:00")
    rec = RecommendationSet(asof.date(), asof, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 3.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(rec, tmp_path))
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: ([{"results": []}], [_row("TSLA", "2026-10-13T14:40:00Z", agent="agentic")]))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:45:00-04:00"), None) == 3
    assert "another_executor_in_window" in capsys.readouterr().out
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: ([{"results": []}], [_row("TSLA", "2026-10-13T14:40:00Z")]))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:45:00-04:00"), None) == 0
    assert json.loads(capsys.readouterr().out)["orders"][0]["ticker"] == "AAPL"


def test_page_ids_are_strings_only():
    assert slot._prior_ids([{"orders": [{"id": True}, {"id": 3}, {"id": "ok"}]}]) == ["3", "ok"]


def test_main_none_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(slot, "_handed", lambda *a: (_ for _ in ()).throw(LookupError("stale")))
    assert slot.main(["begin", "--slot", "1030", "--now", "2026-10-13T12:00:00-04:00", "--root", str(tmp_path)]) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "stale"


def test_finish_calls_finalizer_and_builds_order_rows(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    now = datetime.fromisoformat("2026-10-13T10:30:00-04:00")
    rec = RecommendationSet(now.date(), now, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 5.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    day = tmp_path / now.date().isoformat()
    (day / "placements/1030").mkdir(parents=True)
    (day / "handed_1030.json").write_text(json.dumps(rec.to_json()))
    (day / "placements/1030/AAPL.json").write_text(json.dumps({"data": {"order": {"id": "oid", "state": "confirmed"}}}))
    # The reader saves validate_native_placement check's verdict beside each response.
    (day / "placements/1030/AAPL.check.json").write_text(json.dumps({"ok": True, "order_id": "oid", "reason": None}))
    called = {}
    class Store:
        def __init__(self, root=None): pass
        def handed_path(self, *_): return day / "handed_1030.json"
        def placements_dir(self, *_): return day / "placements/1030"
        def expected_for(self, *_): return SimpleNamespace(budget={"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900})
    monkeypatch.setattr(slot, "RecommendationStore", Store)
    monkeypatch.setitem(__import__("sys").modules, "scripts.finalize_run_evidence", SimpleNamespace(main=lambda args: (called.setdefault("args", args), 0)[1]))
    assert slot._finish("1030", now.isoformat(), now, str(tmp_path), str(tmp_path / "evidence")) == 0
    args = called["args"]
    # The finalizer's --root is the EVIDENCE root, never the recommendation root (PR #1815 review).
    assert args[args.index("--root") + 1] == str(tmp_path / "evidence")
    assert "--orders-json" in args
    # `confirmed` is a broker-accepted state Gate 9 does not know; it is forwarded as in-flight "submitted".
    assert json.loads(args[args.index("--orders-json") + 1])[0]["status"] == "submitted"
    assert json.loads(args[args.index("--orders-json") + 1])[0]["submitted_amount_usd"] == 5.0
    assert json.loads(args[args.index("--budget-json") + 1]) == {
        "budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900,
    }
    assert json.loads(capsys.readouterr().out)["verdict"] == 0


def test_finish_refused_response_is_not_reported_placed(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    now = datetime.fromisoformat("2026-10-13T10:30:00-04:00")
    rec = RecommendationSet(now.date(), now, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 5.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    day = tmp_path / now.date().isoformat()
    (day / "placements/1030").mkdir(parents=True)
    (day / "handed_1030.json").write_text(json.dumps(rec.to_json()))
    (day / "placements/1030/AAPL.json").write_text(json.dumps({"data": {"order": {"state": "rejected"}}}))
    called = {}
    class Store:
        def __init__(self, root=None): pass
        def handed_path(self, *_): return day / "handed_1030.json"
        def placements_dir(self, *_): return day / "placements/1030"
    monkeypatch.setattr(slot, "RecommendationStore", Store)
    monkeypatch.setitem(__import__("sys").modules, "scripts.finalize_run_evidence", SimpleNamespace(main=lambda args: (called.setdefault("args", args), 0)[1]))
    slot._finish("1030", now.isoformat(), now, None)
    assert "--orders-json" not in called["args"]
    assert "not_submitted_error" in called["args"]


def test_pages_glob_and_missing_ids(tmp_path, monkeypatch):
    class Assembler:
        def assemble(self, paths, **kwargs):
            return ({"pages": [{"orders": [{"id": True}, {"id": "good"}]}]}, 2)

        def _order_rows(self, page):
            return _REAL_ORDER_ROWS(page)
    monkeypatch.setitem(__import__("sys").modules, "scripts.assemble_order_pages", Assembler())
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "secret")
    p = tmp_path / "orders_1.json"
    p.write_text("{}")
    pages, _ = slot._pages(str(tmp_path / "orders_*.json"), now=datetime.now(slot.ET))
    assert slot._prior_ids(pages) == ["good"]


def test_finish_missing_response_marks_refused(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    now = datetime.fromisoformat("2026-10-13T10:30:00-04:00")
    rec = RecommendationSet(now.date(), now, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 5.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    day = tmp_path / now.date().isoformat()
    (day / "placements/1030").mkdir(parents=True)
    (day / "handed_1030.json").write_text(json.dumps(rec.to_json()))
    class Store:
        def __init__(self, root=None): pass
        def handed_path(self, *_): return day / "handed_1030.json"
        def placements_dir(self, *_): return day / "placements/1030"
    captured = {}
    monkeypatch.setattr(slot, "RecommendationStore", Store)
    monkeypatch.setitem(__import__("sys").modules, "scripts.finalize_run_evidence",
                        SimpleNamespace(main=lambda args: (captured.setdefault("args", args), 0)[1]))
    assert slot._finish("1030", now.isoformat(), now, None) == 0
    assert "not_submitted_error" in captured["args"]
    assert json.loads(capsys.readouterr().out)["status"] == "OK"


def test_filter_drops_only_buys_after_as_of_and_writes_ids(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    asof = datetime.fromisoformat("2026-10-13T10:35:00-04:00")
    rec = RecommendationSet(asof.date(), asof, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 3.0, "aaaaaaaa-0000-4000-8000-00000000000a"),
                             Recommendation("MSFT", 4.0, "bbbbbbbb-0000-4000-8000-00000000000b")))
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(rec, tmp_path))
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: ([{"orders": [{"id": "prior"}]}], [
        _row("AAPL", "2026-10-13T14:36:00Z"),
        _row("MSFT", "2026-10-13T14:34:00Z"),
        _row("GOOG", "2026-10-13T14:37:00Z", side="sell"),
    ]))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:45:00-04:00"), None) == 0
    out = json.loads(capsys.readouterr().out)
    assert [o["ticker"] for o in out["orders"]] == ["MSFT"]
    assert json.loads(Path(out["prior_ids_file"]).read_text()) == ["prior"]


def test_filter_uses_the_handed_set_and_refuses_without_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(None, tmp_path))
    assert slot._filter("1030", "x", datetime.now(slot.ET), None) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "nothing_handed_run_begin_first"


def test_filter_incomplete_page_is_an_error(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    from scripts.assemble_order_pages import AssemblyRefused
    asof = datetime.fromisoformat("2026-10-13T10:35:00-04:00")
    rec = RecommendationSet(asof.date(), asof, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 3.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(rec, tmp_path))
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: (_ for _ in ()).throw(AssemblyRefused("incomplete")))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:45:00-04:00"), None) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "ERROR"


@pytest.mark.parametrize("history,budget,reason", [
    # $100 bought elsewhere after the set was computed: $100 + $90 breaches the $180 day cap.
    ([("TSLA", "100.00")], NORMAL, "daily"),
    # The day is fine but the week is not.
    ([("TSLA", "20.00")], {**NORMAL, "weekly_cap_usd": 100.0}, "weekly"),
])
def test_filter_rechecks_caps_against_fresh_history(tmp_path, monkeypatch, capsys, history, budget, reason):
    from dca_rec_models import Recommendation, RecommendationSet
    asof = datetime.fromisoformat("2026-10-13T10:35:00-04:00")
    items = tuple(Recommendation(t, 15.0, f"{c * 8}-0000-4000-8000-{c * 12}") for t, c in
                  zip(("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOG"), "abcdef", strict=True))
    rec = RecommendationSet(asof.date(), asof, "1030", "src", "v", "h", items)  # $90 total
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(rec, tmp_path, budget))
    rows = [_row(symbol, "2026-10-13T13:00:00Z", usd=usd) for symbol, usd in history]
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: ([{"results": []}], rows))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:45:00-04:00"), None) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == f"caps_exceeded_since_compute:{reason}"


def test_filter_order_count_cap(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    asof = datetime.fromisoformat("2026-10-13T10:35:00-04:00")
    rec = RecommendationSet(asof.date(), asof, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 1.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(rec, tmp_path, {**NORMAL, "daily_cap_usd": 10**6,
                                                                                 "weekly_cap_usd": 10**6}))
    monkeypatch.setattr("dca_runner.MAX_ORDERS_PER_DAY", 1)
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: ([{"results": []}], [_row("TSLA", "2026-10-13T13:00:00Z")]))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:45:00-04:00"), None) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "caps_exceeded_since_compute:order_count"


def test_filter_unparseable_history_row_fails_closed(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    asof = datetime.fromisoformat("2026-10-13T10:35:00-04:00")
    rec = RecommendationSet(asof.date(), asof, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 1.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(rec, tmp_path))
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: ([{"results": []}], [{"id": "x", "created_at": "2026-10-13T13:00:00Z"}]))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:45:00-04:00"), None) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "budget_unverifiable"


def test_filter_counts_an_order_repeated_across_pages_once(tmp_path, monkeypatch, capsys):
    from dca_rec_models import Recommendation, RecommendationSet
    asof = datetime.fromisoformat("2026-10-13T10:35:00-04:00")
    rec = RecommendationSet(asof.date(), asof, "1030", "src", "v", "h",
                            (Recommendation("AAPL", 80.0 / 10, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(rec, tmp_path))
    twice = [_row("TSLA", "2026-10-13T13:00:00Z", usd="100.00", oid="same")] * 2  # $200 if double-counted
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: ([{"results": []}], twice))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:45:00-04:00"), None) == 0


def test_begin_success_records_handed(monkeypatch, capsys, tmp_path):
    from dca_rec_models import Recommendation, RecommendationSet
    now = datetime.fromisoformat("2026-10-13T12:07:00-04:00")
    rec = RecommendationSet(now.date(), now, "1230", "src", "v", "h",
                            (Recommendation("AAPL", 3.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))
    class Store:
        def __init__(self, root=None): pass
        def record_handed(self, value, **kwargs): self.recorded = value
        def placements_dir(self, *args): return tmp_path
    monkeypatch.setattr(slot, "RecommendationStore", Store)
    monkeypatch.setattr(slot, "_handed", lambda *a: rec)
    assert slot.main(["begin", "--slot", "1230", "--now", now.isoformat()]) == 0
    result = json.loads(capsys.readouterr().out)
    # Tuesday 2026-10-13: seven days back (Tue 10-06) is earlier than Monday 10-12, so the
    # order pull covers both the ISO week and the kill switch's trailing seven days.
    assert result["orders_since"] == "2026-10-06T04:00:00+00:00"


def test_begin_refuses_a_different_set_once_placements_exist(monkeypatch, capsys, tmp_path):
    class Store:
        def __init__(self, root=None): pass
        def record_handed(self, value, **kwargs): raise ValueError("placements_exist_for_an_earlier_recommendation")
    monkeypatch.setattr(slot, "RecommendationStore", Store)
    monkeypatch.setattr(slot, "_handed", lambda *a: object())
    assert slot.main(["begin", "--slot", "1230", "--now", "2026-10-13T12:07:00-04:00"]) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "placements_exist_for_an_earlier_recommendation"


def test_main_finish_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(slot, "_finish", lambda *args: 0)
    assert slot.main(["finish", "--slot", "1530", "--started-at", "2026-10-13T15:30:00-04:00"]) == 0


def test_main_bad_timestamp_returns_error(capsys):
    assert slot.main(["begin", "--slot", "1030", "--now", "bad"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "ERROR"


# --- PR #1667 review: the real clock must be inside the slot window and the session ---------------


@pytest.mark.parametrize("stamp,reason", [
    ("2026-10-13T10:45:00-04:00", None),                     # inside 10:30's window
    ("2026-10-13T11:31:00-04:00", "outside_slot_window"),    # delayed 10:30 task wakes in 11:30's window
    ("2026-10-13T09:00:00-04:00", "outside_regular_session"),
    ("2026-10-11T10:45:00-04:00", "market_closed"),          # Sunday
])
def test_clock_reason_requires_slot_window_and_session(stamp, reason):
    assert REAL_CLOCK("1030", datetime.fromisoformat(stamp)) == reason


def test_clock_reason_honours_an_early_close():
    # 2026-11-27 closes at 13:00 ET; 15:30's window would otherwise still be open.
    assert REAL_CLOCK("1530", datetime.fromisoformat("2026-11-27T15:35:00-05:00")) in {
        "outside_regular_session", "market_closed"}


def test_clock_subcommand_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(slot, "_clock_reason", lambda *_: "outside_slot_window")
    assert slot.main(["clock", "--slot", "1030", "--now", "2026-10-13T11:31:00-04:00"]) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "outside_slot_window"
    monkeypatch.setattr(slot, "_clock_reason", lambda *_: None)
    assert slot.main(["clock", "--slot", "1030", "--now", "2026-10-13T10:45:00-04:00"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "OK"


@pytest.mark.parametrize("command", [["begin"], ["filter", "--pages", "x"]])
def test_begin_and_filter_refuse_outside_the_window(monkeypatch, capsys, command):
    monkeypatch.setattr(slot, "_clock_reason", lambda *_: "outside_slot_window")
    assert slot.main([command[0], "--slot", "1030", "--now", "2026-10-13T11:31:00-04:00", *command[1:]]) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "outside_slot_window"


# --- PR #1667 review: finish counts only validated placements ------------------------------------


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize("check,response,counted", [
    ({"ok": True, "order_id": "oid", "reason": None}, {"data": {"order": {"id": "oid", "state": "queued"}}}, True),
    ({"ok": False, "order_id": "oid", "reason": "amount mismatch"}, {"data": {"order": {"id": "oid"}}}, False),
    ({"ok": True, "order_id": "other"}, {"data": {"order": {"id": "oid"}}}, False),   # verdict for another order
    (None, {"data": {"order": {"id": "oid"}}}, False),                                # no saved check verdict
    ({"ok": True, "order_id": "oid"}, '{"data": {"order": {"id": "oid", "id": "x"}}}', False),  # duplicate key
])
def test_validated_placement_requires_a_passing_check_for_the_same_order(tmp_path, check, response, counted):
    response_path = _write(tmp_path / "AAPL.json", response)
    check_path = tmp_path / "AAPL.check.json"
    if check is not None:
        _write(check_path, check)
    order_id, _state = slot._validated_placement(response_path, check_path)
    assert (order_id == "oid") is counted


# --- PR #1815 review ---------------------------------------------------------------------------------------


def _rec(asof_text="2026-10-13T10:35:00-04:00", slot="1030"):
    from dca_rec_models import Recommendation, RecommendationSet
    asof = datetime.fromisoformat(asof_text)
    return RecommendationSet(asof.date(), asof, slot, "src", "v", "h",
                             (Recommendation("AAPL", 3.0, "aaaaaaaa-0000-4000-8000-00000000000a"),))


def test_filter_refuses_a_handed_set_that_went_stale_after_begin(tmp_path, monkeypatch, capsys):
    # begin accepted the 10:15 set at 10:30; fetching history took until 10:31:30, past max_age (15 min).
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(_rec("2026-10-13T10:15:00-04:00"), tmp_path))
    monkeypatch.setattr(slot, "_pages", lambda *a, **k: pytest.fail("history must not be fetched for a stale set"))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:31:30-04:00"), None) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "handed_set_stale"


def test_filter_refuses_a_handed_set_from_another_day(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(_rec("2026-10-12T10:35:00-04:00"), tmp_path))
    assert slot._filter("1030", "x", datetime.fromisoformat("2026-10-13T10:40:00-04:00"), None) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "handed_set_wrong_day"


def test_clock_rechecks_the_handed_sets_freshness_before_each_order(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(_rec("2026-10-13T10:15:00-04:00"), tmp_path))
    assert slot.main(["clock", "--slot", "1030", "--now", "2026-10-13T10:40:00-04:00"]) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "handed_set_stale"
    assert slot.main(["clock", "--slot", "1030", "--now", "2026-10-13T10:25:00-04:00"]) == 0


def test_clock_without_a_handed_set_only_checks_the_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(slot, "RecommendationStore", _fake_store(None, tmp_path))
    assert slot.main(["clock", "--slot", "1030", "--now", "2026-10-13T10:40:00-04:00"]) == 0


@pytest.mark.parametrize("raw,canonical", [("confirmed", "submitted"), ("queued", "submitted"), ("new", "submitted"),
                                           ("partially_filled", "submitted"), ("filled", "filled"),
                                           ("unconfirmed", "unconfirmed")])
def test_finish_forwards_a_canonical_status_the_finalizer_accepts(tmp_path, monkeypatch, raw, canonical):
    from scripts.scheduled_artifacts import ORDER_ATTEMPT_STATUSES
    rec = _rec("2026-10-13T10:30:00-04:00")
    day = tmp_path / "2026-10-13"
    (day / "placements/1030").mkdir(parents=True)
    (day / "handed_1030.json").write_text(json.dumps(rec.to_json()))
    (day / "placements/1030/AAPL.json").write_text(json.dumps({"data": {"order": {"id": "oid", "state": raw}}}))
    (day / "placements/1030/AAPL.check.json").write_text(json.dumps({"ok": True, "order_id": "oid", "reason": None}))
    captured = {}

    class Store:
        def __init__(self, root=None): pass
        def handed_path(self, *_): return day / "handed_1030.json"
        def placements_dir(self, *_): return day / "placements/1030"
        def expected_for(self, *_): return SimpleNamespace(budget=NORMAL)
    monkeypatch.setattr(slot, "RecommendationStore", Store)
    monkeypatch.setitem(__import__("sys").modules, "scripts.finalize_run_evidence",
                        SimpleNamespace(main=lambda args: (captured.setdefault("args", args), 0)[1]))
    assert slot._finish("1030", "2026-10-13T10:30:00-04:00", datetime.now(slot.ET), None) == 0
    args = captured["args"]
    status = json.loads(args[args.index("--orders-json") + 1])[0]["status"]
    assert status == canonical and status in ORDER_ATTEMPT_STATUSES
    # The saved broker response keeps its raw state.
    assert json.loads((day / "placements/1030/AAPL.json").read_text())["data"]["order"]["state"] == raw


def test_finish_requires_an_explicit_evidence_root_with_a_custom_recommendation_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(__import__("sys").modules, "scripts.finalize_run_evidence",
                        SimpleNamespace(main=lambda args: pytest.fail("must not write evidence anywhere")))
    assert slot._finish("1030", "2026-10-13T10:30:00-04:00", datetime.now(slot.ET), str(tmp_path / "recs")) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "evidence_root_required_with_custom_root"


def test_finish_never_passes_the_recommendation_root_to_the_finalizer(tmp_path, monkeypatch):
    rec = _rec("2026-10-13T10:30:00-04:00")
    day = tmp_path / "2026-10-13"
    (day / "placements/1030").mkdir(parents=True)
    (day / "handed_1030.json").write_text(json.dumps(rec.to_json()))
    captured = {}

    class Store:
        def __init__(self, root=None): pass
        def handed_path(self, *_): return day / "handed_1030.json"
        def placements_dir(self, *_): return day / "placements/1030"
    monkeypatch.setattr(slot, "RecommendationStore", Store)
    monkeypatch.setitem(__import__("sys").modules, "scripts.finalize_run_evidence",
                        SimpleNamespace(main=lambda args: (captured.setdefault("args", args), 0)[1]))
    slot._finish("1030", "2026-10-13T10:30:00-04:00", datetime.now(slot.ET), None)
    assert "--root" not in captured["args"]  # default root: the live cron tree, as before


def test_begin_refuses_a_set_the_current_config_would_refuse(tmp_path, monkeypatch, capsys):
    from dca_rec_store import RecommendationStore
    store = RecommendationStore(tmp_path)
    rec = _rec("2026-10-13T10:30:00-04:00")  # ladder "v" and universe hash "h" are not the repo config's
    store.write(rec, __import__("dca_rec_models").ExpectedPriceSet(
        rec.trading_day, rec.as_of, "1030", "run_state_artifacts",
        (__import__("dca_rec_models").ExpectedPrice("AAPL", 100, 101, 99, 1.5, 1, 3),), NORMAL))
    monkeypatch.undo()  # the autouse clock stub is not wanted here; real clock check runs on a Tuesday 10:40
    assert slot.main(["begin", "--slot", "1030", "--now", "2026-10-13T10:40:00-04:00", "--root", str(tmp_path)]) == 3
    assert json.loads(capsys.readouterr().out)["reason"].startswith("config_check_failed:")
    assert store.handed(rec.trading_day, "1030") is None


def test_finish_rejects_duplicate_json_keys_in_handed_set(tmp_path, monkeypatch):
    rec = _rec("2026-10-13T10:30:00-04:00")
    day = tmp_path / "2026-10-13"
    (day / "placements/1030").mkdir(parents=True)
    # Duplicate key in handed_1030.json must be rejected by _strict_json / store.handed() rather than last-wins.
    raw = json.dumps(rec.to_json())
    tampered = raw[:-1] + ',"items":[]}'
    (day / "handed_1030.json").write_text(tampered, encoding="utf-8")
    monkeypatch.setitem(__import__("sys").modules, "scripts.finalize_run_evidence",
                        SimpleNamespace(main=lambda args: pytest.fail("finalizer must not run on duplicate-keyed handed file")))
    with pytest.raises(ValueError, match="duplicate key"):
        slot._finish("1030", "2026-10-13T10:30:00-04:00", datetime.now(slot.ET), str(tmp_path), str(tmp_path / "evidence"))

