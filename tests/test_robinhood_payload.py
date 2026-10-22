"""scripts/robinhood_payload.py: the validation that stands between an LLM-copied fetch and a live slot."""

import json
from datetime import date, time

import pytest

from scripts import robinhood_payload as rp

DAY = date(2026, 10, 15)


def bar(ts, o=10.0, h=10.2, low=9.9, c=10.1, v=100):
    return {"timestamp": ts, "open": o, "high": h, "low": low, "close": c, "volume": v}


def good_ticker(start_utc_hour=17, minutes=None):
    if minutes is None:
        minutes = range(30, 60, 5)
    bars = [bar(f"2026-10-15T{start_utc_hour:02d}:{m:02d}:00Z") for m in minutes]
    bars += [bar(f"2026-10-15T{start_utc_hour + 1:02d}:{m:02d}:00Z") for m in range(0, 30, 5)]
    return {
        "previous_close_bar": bar("2026-10-14T00:00:00Z"),
        "intraday_bars": bars,
        "quote": {"price": 10.1, "as_of": "2026-10-15T18:31:00Z"},
    }


def kw(**over):
    base = {"day": DAY, "bar_start": time(13, 30), "decision": time(14, 30), "interval": 5}
    base.update(over)
    return base


def test_valid_ticker_passes_and_drops_the_still_open_decision_bar():
    raw = good_ticker()
    raw["intraday_bars"].append(bar("2026-10-15T18:30:00Z"))  # 14:30 ET: the still-open bar
    out = rp.validate_ticker(raw, **kw())
    assert out["intraday_bars"][-1]["timestamp"].startswith("2026-10-15T14:25")
    assert all("14:30" not in b["timestamp"] for b in out["intraday_bars"])


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda r: r["intraday_bars"].__setitem__(0, bar("2026-10-15T17:30:00")), "not timezone-aware"),
        (lambda r: r["intraday_bars"].__setitem__(0, bar("2026-10-15T17:30:00Z", h=9.0)), "inconsistent range"),
        (lambda r: r["intraday_bars"].__setitem__(1, bar("2026-10-15T17:35:00Z", o=30, h=31, low=29, c=30)), "median"),
        (lambda r: r["quote"].update(price=20.0), "disagrees"),
        (lambda r: r["quote"].update(as_of="2026-10-14T18:31:00Z"), "not from"),
        (lambda r: r["quote"].update(as_of="2026-10-15T16:00:00Z"), "predates"),  # 12:00 ET, before the 13:30 start
        (lambda r: r["quote"].update(as_of="2026-10-15T19:00:01Z"), "too long after"),  # 15:00:01 ET, post-window
        (lambda r: r.update(previous_close_bar=bar("2026-10-15T14:00:00Z")), "prior session"),
        (lambda r: r.update(intraday_bars=r["intraday_bars"][:3]), "far before"),
        (lambda r: r.update(intraday_bars=[]), "no intraday bars"),
        (lambda r: r.update(error="tool failed"), "agent reported"),
    ],
)
def test_bad_ticker_data_is_rejected(mutate, needle):
    raw = good_ticker()
    mutate(raw)
    with pytest.raises(rp.PayloadError, match=needle):
        rp.validate_ticker(raw, **kw())


def test_bars_before_the_slot_window_are_dropped_not_fatal():
    raw = good_ticker()
    raw["intraday_bars"].insert(0, bar("2026-10-15T16:00:00Z"))  # 12:00 ET, before the 13:30 start
    out = rp.validate_ticker(raw, **kw())
    assert out["intraday_bars"][0]["timestamp"].startswith("2026-10-15T13:30")


def plan_for(tmp_path, tickers):
    return {
        "slot": "1430",
        "trading_day": "2026-10-15",
        "decision_time_et": "14:30",
        "bar_start_et": "13:30",
        "interval_minutes": 5,
        "tickers": tickers,
        "chunks": [tickers],
        "out": str(tmp_path / "out" / "payload.json"),
    }


def write_chunk(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return str(path)


def test_merge_publishes_valid_and_records_failures(tmp_path):
    plan = plan_for(tmp_path, ["AAA", "BBB", "CCC", "DDD", "EEE"])
    chunk = write_chunk(
        tmp_path, "chunk_0.json", {t: good_ticker() for t in ("AAA", "BBB", "CCC", "DDD")} | {"EEE": {"error": "x"}}
    )
    payload, failed = rp.assemble(plan, [chunk])
    assert sorted(payload["sources"]["robinhood"]) == ["AAA", "BBB", "CCC", "DDD"]
    assert payload["ticker_errors"] == {"robinhood": ["EEE"]}
    assert payload["source_errors"] == []
    assert rp.publish(plan, payload, failed, min_coverage=0.8) == 0
    assert json.loads((tmp_path / "out" / "payload.json").read_text())["sources"]["robinhood"].keys() == {
        "AAA", "BBB", "CCC", "DDD"
    }
    assert (tmp_path / "out" / "payload.meta.json").is_file()
    assert rp.check(plan, min_coverage=0.8) == 0


def test_low_coverage_publishes_nothing(tmp_path):
    plan = plan_for(tmp_path, ["AAA", "BBB", "CCC"])
    chunk = write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker()})
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=0.8) == 2
    assert not (tmp_path / "out").exists()
    assert rp.check(plan, min_coverage=0.8) == 2


def test_default_floor_tolerates_a_few_missing_tickers(tmp_path):
    # 2026-10-21: one stale thin ticker (89/90) aborted three slots. The default floor is 95%, so 4 of 90 may miss.
    tickers = [f"T{i}" for i in range(90)]
    plan = plan_for(tmp_path, tickers)
    chunk = write_chunk(tmp_path, "chunk_0.json", {t: good_ticker() for t in tickers[:-4]})
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.DEFAULT_MIN_COVERAGE == 0.95
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0
    published = json.loads((tmp_path / "out" / "payload.json").read_text())
    # The skipped tickers stay in ticker_errors, so Gate 5 scores them NO_DATA instead of treating them as unseen.
    assert published["ticker_errors"] == {"robinhood": sorted(tickers[-4:])}
    assert not set(tickers[-4:]) & set(published["sources"]["robinhood"])
    assert rp.check(plan, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0


def test_default_floor_still_refuses_a_stale_feed(tmp_path):
    # 2026-10-20 13:30: 8 of 90 tickers had a stale newest bar. 82/90 (91%) is below the floor: publish nothing.
    tickers = [f"T{i}" for i in range(90)]
    plan = plan_for(tmp_path, tickers)
    chunk = write_chunk(tmp_path, "chunk_0.json", {t: good_ticker() for t in tickers[:-8]})
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 2
    assert not (tmp_path / "out").exists()


def test_default_floor_boundary_is_ninety_five_percent(tmp_path):
    # 5 of 90 missing is 94.4%: just under the floor, so nothing is published.
    tickers = [f"T{i}" for i in range(90)]
    plan = plan_for(tmp_path, tickers)
    chunk = write_chunk(tmp_path, "chunk_0.json", {t: good_ticker() for t in tickers[:-5]})
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 2


def test_fenced_and_garbage_chunks(tmp_path):
    plan = plan_for(tmp_path, ["AAA"])
    fenced = write_chunk(tmp_path, "chunk_0.json", "```json\n" + json.dumps({"AAA": good_ticker()}) + "\n```")
    assert list(rp.assemble(plan, [fenced])[0]["sources"]["robinhood"]) == ["AAA"]
    junk = write_chunk(tmp_path, "chunk_0.json", "no json here")
    payload, failed = rp.assemble(plan, [junk])
    assert payload["sources"]["robinhood"] == {} and "AAA" in failed


def test_existing_payload_is_never_overwritten(tmp_path):
    plan = plan_for(tmp_path, ["AAA"])
    chunk = write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker()})
    payload, failed = rp.assemble(plan, [chunk])
    out = tmp_path / "out"
    out.mkdir()
    (out / "payload.json").write_text("{}")
    assert rp.publish(plan, payload, failed, min_coverage=0.8) == 0
    assert (out / "payload.json").read_text() == "{}"


def test_due_slot_window():
    assert rp.due_slot(DAY, time(14, 31)) == "1430"
    assert rp.due_slot(DAY, time(14, 44)) == "1430"
    assert rp.due_slot(DAY, time(14, 45)) is None
    assert rp.due_slot(DAY, time(14, 30)) is None
    assert rp.due_slot(DAY, time(15, 31)) is None  # 15:30 is Finance-primary and not covered


def test_prompt_names_window_and_forbids_orders(tmp_path):
    plan = plan_for(tmp_path, [])
    text = rp.build_prompt(plan, ["AAA", "BBB"])
    assert "AAA, BBB" in text and "13:30 ET" in text and "14:30 ET" in text
    assert "Never call any order" in text


def test_live_mirror_prompts_use_the_prebuilt_payload():
    from pathlib import Path

    mirror = Path(__file__).resolve().parents[1] / "claude" / "dca-executor-prompts" / "live-mirror"
    for slot in rp.SLOTS:
        (path,) = mirror.glob(f"{slot}ET-*.txt")
        text = path.read_text(encoding="utf-8")
        assert f"scripts.robinhood_payload plan --slot {slot}" in text, path.name
        assert "do NOT hand-fetch bars" in text, path.name
    assert not any("robinhood_payload" in p.read_text() for p in mirror.glob("1530ET-*.txt"))


# -- chunk ownership (Codex adversarial review): a reply counts only for the tickers its own chunk was assigned ----


def two_chunk_plan(tmp_path):
    plan = plan_for(tmp_path, ["AAA", "BBB", "CCC", "DDD"])
    plan["chunks"] = [["AAA", "BBB"], ["CCC", "DDD"]]
    return plan


def test_a_reply_never_overwrites_another_chunks_ticker(tmp_path, capsys):
    plan = two_chunk_plan(tmp_path)
    impostor = good_ticker()
    impostor["quote"]["price"] = 10.05  # plausible, so validation alone would accept it
    first = write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker(), "BBB": good_ticker()})
    second = write_chunk(tmp_path, "chunk_1.json", {"CCC": good_ticker(), "DDD": good_ticker(), "AAA": impostor})
    payload, failed = rp.assemble(plan, [first, second])
    assert failed == {}
    assert payload["sources"]["robinhood"]["AAA"]["quote"]["price"] == 10.1  # chunk 0's own fetch, not chunk 1's
    assert "chunk_1.json: ignored tickers it was not assigned: AAA" in capsys.readouterr().err


def test_an_extra_key_is_dropped_even_when_its_owner_returned_nothing(tmp_path):
    plan = two_chunk_plan(tmp_path)
    first = write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker(), "BBB": good_ticker(), "CCC": good_ticker()})
    payload, failed = rp.assemble(plan, [first])
    assert sorted(payload["sources"]["robinhood"]) == ["AAA", "BBB"]
    assert failed["CCC"] == "no data returned for this ticker"


def test_a_ticker_carried_by_two_accepted_replies_fails_instead_of_last_write_wins(tmp_path):
    plan = two_chunk_plan(tmp_path)
    first = write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker(), "BBB": good_ticker()})
    copy = tmp_path / "again"
    copy.mkdir()
    again = write_chunk(copy, "chunk_0.json", {"AAA": good_ticker(), "BBB": good_ticker()})
    payload, failed = rp.assemble(plan, [first, again])
    assert "AAA" not in payload["sources"]["robinhood"]
    assert failed["AAA"].startswith("returned by more than one chunk reply")


@pytest.mark.parametrize("name", ["c.json", "chunk_7.json", "chunk_x.json"])
def test_a_reply_that_names_no_chunk_of_this_plan_is_not_merged(tmp_path, name):
    plan = two_chunk_plan(tmp_path)
    stray = write_chunk(tmp_path, name, {"AAA": good_ticker()})
    payload, failed = rp.assemble(plan, [stray])
    assert payload["sources"]["robinhood"] == {} and "AAA" in failed


@pytest.mark.parametrize("chunks", [None, [], [["AAA", "BBB"], ["BBB", "CCC", "DDD"]], [["AAA", "BBB", "CCC"]],
                                    [["AAA", "BBB"], ["CCC", "DDD", "ZZZ"]]])
def test_a_plan_whose_chunks_do_not_partition_its_tickers_merges_nothing(tmp_path, chunks):
    plan = two_chunk_plan(tmp_path)
    if chunks is None:
        del plan["chunks"]
    else:
        plan["chunks"] = chunks
    with pytest.raises(rp.PayloadError):
        rp.assemble(plan, [])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    assert rp.main(["merge", "--plan", str(plan_path)]) == 2
    assert not (tmp_path / "out").exists()


def _stale(raw, as_of):
    raw["quote"]["as_of"] = as_of
    return raw


def test_a_quote_just_before_the_decision_bar_makes_the_payload_stale(tmp_path):
    # Every quote is inside the trend window (13:30 ET onward) so each passes alone, but none is near 14:30 ET.
    plan = plan_for(tmp_path, ["AAA", "BBB", "CCC"])
    chunk = write_chunk(
        tmp_path, "chunk_0.json", {t: _stale(good_ticker(), "2026-10-15T17:45:00Z") for t in ("AAA", "BBB", "CCC")}
    )
    payload, failed = rp.assemble(plan, [chunk])
    assert payload["sources"]["robinhood"] == {}
    assert all("stale payload" in reason for reason in failed.values())
    assert rp.publish(plan, payload, failed, min_coverage=0.8) == 2
    assert not (tmp_path / "out").exists()


def test_one_thin_ticker_with_an_old_last_trade_does_not_fail_a_fresh_payload(tmp_path):
    plan = plan_for(tmp_path, ["AAA", "BBB"])
    thin = _stale(good_ticker(), "2026-10-15T17:40:00Z")  # last traded 13:40 ET, inside the trend window
    chunk = write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker(), "BBB": thin})
    payload, failed = rp.assemble(plan, [chunk])
    assert sorted(payload["sources"]["robinhood"]) == ["AAA", "BBB"] and not failed

# --------------------------------------------------------------------------- freshness (2026-10-15 review)

from datetime import datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

_ET = ZoneInfo("America/New_York")
BUILT = datetime(2026, 10, 15, 14, 31, tzinfo=_ET)


def _published(tmp_path, built=BUILT):
    plan = plan_for(tmp_path, ["AAA"])
    chunk = write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker()})
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=0.8, now=built) == 0
    return plan


def test_fresh_payload_passes_within_the_age_bound(tmp_path):
    plan = _published(tmp_path)
    assert rp.check(plan, min_coverage=0.8, now=BUILT + timedelta(minutes=10)) == 0


def test_payload_older_than_the_bound_is_stale(tmp_path, capsys):
    plan = _published(tmp_path)
    assert rp.check(plan, min_coverage=0.8, now=BUILT + timedelta(minutes=46)) == rp.EXIT_STALE
    assert "older than" in capsys.readouterr().err


def test_publish_refuses_before_the_decision_anchor(tmp_path):
    plan = plan_for(tmp_path, ["AAA"])
    payload, failed = rp.assemble(plan, [write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker()})])
    early = datetime(2026, 10, 15, 14, 29, tzinfo=_ET)
    assert rp.publish(plan, payload, failed, min_coverage=0.8, now=early) == 2
    assert not (tmp_path / "out").exists()


def test_missing_or_mismatched_sidecar_fails_closed(tmp_path):
    plan = _published(tmp_path)
    out = tmp_path / "out"
    meta = out / "payload.meta.json"
    good = meta.read_text()
    now = BUILT + timedelta(minutes=5)
    meta.unlink()
    assert rp.check(plan, min_coverage=0.8, now=now) == rp.EXIT_STALE
    data = json.loads(good)
    meta.write_text(json.dumps({**data, "trading_day": "2026-10-14"}))
    assert rp.check(plan, min_coverage=0.8, now=now) == rp.EXIT_STALE
    meta.write_text(good)
    assert rp.check(plan, min_coverage=0.8, now=now) == 0


def test_payload_swapped_under_a_fresh_sidecar_fails_the_checksum(tmp_path, capsys):
    plan = _published(tmp_path)
    (tmp_path / "out" / "payload.json").write_text(json.dumps({"sources": {"robinhood": {}}}))
    assert rp.check(plan, min_coverage=0.8, now=BUILT + timedelta(minutes=1)) == rp.EXIT_STALE
    assert "checksum" in capsys.readouterr().err


def test_sidecar_built_before_the_anchor_or_in_the_future_is_stale(tmp_path):
    plan = _published(tmp_path)
    meta = tmp_path / "out" / "payload.meta.json"
    data = json.loads(meta.read_text())
    meta.write_text(json.dumps({**data, "generated_at_et": "2026-10-15T14:00:00-04:00"}))
    assert rp.check(plan, min_coverage=0.8, now=BUILT) == rp.EXIT_STALE
    meta.write_text(json.dumps({**data, "generated_at_et": (BUILT + timedelta(minutes=30)).isoformat()}))
    assert rp.check(plan, min_coverage=0.8, now=BUILT) == rp.EXIT_STALE

# -- quote freshness (Codex adversarial review): a same-day quote must describe the price at the decision ----------


@pytest.mark.parametrize(
    "as_of, needle",
    [
        ("2026-10-15T15:10:00Z", "predates"),          # 11:10 ET: before the 13:30 trend-window start
        ("2026-10-15T19:00:01Z", "too long after"),   # 15:00:01 ET: past decision + 30 min
    ],
)
def test_a_quote_outside_the_decision_window_is_rejected(as_of, needle):
    raw = good_ticker()
    raw["quote"]["as_of"] = as_of
    with pytest.raises(rp.PayloadError, match=needle):
        rp.validate_ticker(raw, **kw())


@pytest.mark.parametrize("as_of", ["2026-10-15T18:25:00Z", "2026-10-15T18:30:00Z", "2026-10-15T19:00:00Z"])
def test_a_quote_inside_the_decision_window_passes(as_of):
    raw = good_ticker()
    raw["quote"]["as_of"] = as_of
    assert rp.validate_ticker(raw, **kw())["quote"]["price"] == 10.1


def test_a_payload_whose_newest_quote_predates_the_decision_bar_is_rejected(tmp_path):
    plan = plan_for(tmp_path, ["AAA", "BBB"])
    chunk = write_chunk(
        tmp_path,
        "chunk_0.json",
        {
            "AAA": _stale(good_ticker(), "2026-10-15T18:24:59Z"),
            "BBB": _stale(good_ticker(), "2026-10-15T17:45:00Z"),
        },
    )
    payload, failed = rp.assemble(plan, [chunk])
    assert payload["sources"]["robinhood"] == {}
    assert all("stale payload" in reason for reason in failed.values())


def test_a_future_stamped_quote_is_rejected_against_the_validating_clock():
    from datetime import datetime

    raw = good_ticker()  # quote at 14:31 ET
    now = datetime(2026, 10, 15, 14, 28, tzinfo=rp.ET)  # more than the 2-minute skew before it
    with pytest.raises(rp.PayloadError, match="in the future"):
        rp.validate_ticker(raw, **kw(), now=now)
    assert rp.validate_ticker(raw, **kw(), now=datetime(2026, 10, 15, 14, 30, tzinfo=rp.ET))  # within the skew
