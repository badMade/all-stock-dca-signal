"""scripts/robinhood_payload.py: the validation that stands between an LLM-copied fetch and a live slot."""

import hashlib
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
    assert not (tmp_path / "out" / "payload.json").exists()
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
    assert not (tmp_path / "out" / "payload.json").exists()


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
    # 2026-11-02 (first Monday after fall-back): the window is handed over in UTC, computed in code, so the worker never
    # has to know ET's offset. 13:30 ET on 2026-10-15 is EDT (17:30Z); 10:30 ET on 2026-11-02 is EST (15:30Z).
    assert "17:30:00Z" in text and "18:30:00Z" in text


def test_prompt_window_follows_the_dst_offset(tmp_path):
    plan = plan_for(tmp_path, [])
    est = rp.build_prompt({**plan, "trading_day": "2026-11-02", "bar_start_et": "10:30", "decision_time_et": "11:30"}, ["AAA"])
    assert "15:30:00Z" in est and "16:30:00Z" in est


def test_prompt_never_names_the_rejected_span_parameter(tmp_path):
    # 2026-11-02 15:30: get_equity_historicals is `additionalProperties: false` with `start_time` required and no `span`;
    # the worker obeyed "span day"/"span week" and the tool answered `unexpected additional properties ["span"]`.
    plan = plan_for(tmp_path, [])
    text = rp.build_prompt(plan, ["AAA"])
    assert "span" not in text
    # The range is handed over as explicit RFC3339 UTC start_time/end_time (13:30-14:30 ET on 2026-10-15 is EDT).
    assert "start_time" in text and "2026-10-15T17:30:00Z" in text and "2026-10-15T18:30:00Z" in text


def test_prompt_daily_lookback_is_a_start_time_before_the_trading_day(tmp_path):
    plan = plan_for(tmp_path, [])
    text = rp.build_prompt(plan, ["AAA"])
    assert "interval day" in text and "2026-10-08T00:00:00Z" in text  # 7 calendar days back: covers a long weekend


# -- compact worker replies (2026-11-02: 5 of 7 slots refused) ----------------------------------------------------------
# Every worker fetched its bars and quotes, then answered most tickers with "full response exceeds output capacity" or
# "could not be reliably reproduced" instead of the data, so 3:30 published 15/84 and 0935, 1130, 1230 and 1330 refused
# too. Nothing on the Mac changed since the 10-30 builds that published 91/91; the reply is what no longer fits. A bar
# written as a 6-element list carries the same numbers in about a third of the characters, and a smaller chunk halves
# what one worker has to write back. Object bars stay accepted, so a worker that ignores the format still validates.


def compact(raw):
    """``raw`` with every bar rewritten as ``[timestamp, open, high, low, close, volume]``."""
    as_list = lambda b: [b["timestamp"], b["open"], b["high"], b["low"], b["close"], b["volume"]]  # noqa: E731
    return {**raw, "previous_close_bar": as_list(raw["previous_close_bar"]),
            "intraday_bars": [as_list(b) for b in raw["intraday_bars"]]}


def test_prompt_asks_for_compact_list_bars(tmp_path):
    text = rp.build_prompt(plan_for(tmp_path, []), ["AAA", "BBB"])
    assert '[timestamp, open, high, low, close, volume]' in text
    # The object-per-bar shape is what overflowed the replies; the prompt must no longer ask for it.
    assert '{"timestamp": "<iso with offset>", "open": n' not in text


def test_a_compact_list_bar_validates_to_the_same_bars_as_an_object_bar():
    raw = good_ticker()
    assert rp.validate_ticker(compact(raw), **kw()) == rp.validate_ticker(raw, **kw())


@pytest.mark.parametrize("bad", [
    ["2026-10-15T17:30:00Z", 10.0, 10.2, 9.9, 10.1],  # five fields: volume missing
    ["2026-10-15T17:30:00Z", 10.0, 10.2, 9.9, 10.1, 100, 7],  # seven fields
    ["2026-10-15T17:30:00Z", 10.0, 9.0, 9.9, 10.1, 100],  # high below open: the range check still applies
])
def test_a_malformed_compact_bar_is_rejected(bad):
    with pytest.raises(rp.PayloadError):
        rp.validate_bar(bad)


def test_chunks_are_four_tickers_so_one_reply_stays_short():
    assert rp.DEFAULT_CHUNK_SIZE == 4


def test_live_mirror_prompts_use_the_prebuilt_payload():
    from pathlib import Path

    mirror = Path(__file__).resolve().parents[1] / "claude" / "dca-executor-prompts" / "live-mirror"
    for slot in rp.SLOTS:
        (path,) = mirror.glob(f"{slot}ET-*.txt")
        text = path.read_text(encoding="utf-8")
        assert f"scripts.robinhood_payload plan --slot {slot}" in text, path.name
        assert "do NOT hand-fetch bars" in text, path.name
    # 2026-10-30: the 3:30 PM slot moved onto the 15:31 build too (tests/test_live_prompt_1530_prebuilt_payload.py);
    # it is not in rp.SLOTS because `auto` never selects it, so its prompt is checked here by name.
    (final,) = mirror.glob("1530ET-*.txt")
    assert "scripts.robinhood_payload plan --slot 1530" in final.read_text(encoding="utf-8")


#: 2026-10-21 09:35 abort: 90 tickers are 12 chunks of 8, run 3 at a time (4 waves), so the Mac build lands several
#: minutes after its 09:36 launch while the prompts gave up after 5. The wait must cover a normal build but stay inside
#: the slot's ~25-minute window (see DEFAULT_MAX_AGE_MINUTES' comment) so Gates 6-7 still have time to place.
MIN_PAYLOAD_WAIT_MINUTES = 12
MAX_PAYLOAD_WAIT_MINUTES = 20


def test_live_mirror_prompts_wait_long_enough_for_the_prebuilt_payload():
    import re
    from pathlib import Path

    mirror = Path(__file__).resolve().parents[1] / "claude" / "dca-executor-prompts" / "live-mirror"
    waits = {}
    for slot in rp.SLOTS:
        (path,) = mirror.glob(f"{slot}ET-*.txt")
        found = re.findall(r"every 30 seconds for up to (\d+) minutes", path.read_text(encoding="utf-8"))
        assert len(found) == 1, f"{path.name}: expected one 'every 30 seconds for up to N minutes', got {found}"
        waits[slot] = int(found[0])
    assert all(MIN_PAYLOAD_WAIT_MINUTES <= n <= MAX_PAYLOAD_WAIT_MINUTES for n in waits.values()), waits
    assert len(set(waits.values())) == 1, f"the six prompts disagree on the wait: {waits}"


#: 2026-10-22: #1878 lowered the launcher's coverage floor to DEFAULT_MIN_COVERAGE, so a published payload may carry a
#: few errored tickers in ticker_errors.robinhood. The prompts still said "the launcher publishes only a complete
#: universe ... abort on a non-empty list", so the session would reject every payload the floor now lets through.
def _errored_ticker_rule(path):
    text = path.read_text(encoding="utf-8")
    start = text.index("After copying, read its ticker_errors.robinhood")
    return text, text[start : text.index("\n", start)]


def test_live_mirror_prompts_accept_the_errored_tickers_the_coverage_floor_allows():
    from pathlib import Path

    mirror = Path(__file__).resolve().parents[1] / "claude" / "dca-executor-prompts" / "live-mirror"
    rules = set()
    for slot in rp.SLOTS:
        (path,) = mirror.glob(f"{slot}ET-*.txt")
        text, rule = _errored_ticker_rule(path)
        assert "launcher publishes only a complete universe" not in text, path.name  # no longer true since #1878
        assert f"{rp.DEFAULT_MIN_COVERAGE:.0%}" in rule, f"{path.name}: the rule must name the floor it mirrors"
        assert "continue" in rule and "more than" in rule, f"{path.name}: continue within the floor, abort beyond it"
        assert "--error-class market_data_unavailable" in rule, path.name  # still aborts beyond the floor
        assert "name the count and every errored ticker" in rule, path.name  # still evidenced either way
        rules.add(rule)
    assert len(rules) == 1, "the six prompts must carry the same rule"


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
    assert not (tmp_path / "out" / "payload.json").exists()


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
    assert not (tmp_path / "out" / "payload.json").exists()


def test_published_bytes_match_the_sidecar_checksum_even_with_windows_newlines(tmp_path, monkeypatch):
    # Text mode turns "\n" into "\r\n" on Windows, so publish hashed one file and wrote another and `check` failed with
    # "payload does not match its sidecar checksum". Simulate that translation here so the regression shows on any OS.
    real_fdopen = rp.os.fdopen

    def windows_fdopen(fd, mode="r", *args, newline=None, **kwargs):
        return real_fdopen(fd, mode, *args, newline="\r\n" if newline is None else newline, **kwargs)

    monkeypatch.setattr(rp.os, "fdopen", windows_fdopen)
    plan = _published(tmp_path)
    out = tmp_path / "out"
    written = (out / "payload.json").read_bytes()
    assert b"\r" not in written
    assert hashlib.sha256(written).hexdigest() == json.loads((out / "payload.meta.json").read_text())["payload_sha256"]
    assert rp.check(plan, min_coverage=0.8, now=BUILT + timedelta(minutes=5)) == 0


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


# --- refusal marker: `check` must say WHY a payload is missing (2026-10-21: three slots aborted while the
# 13:30 alert guessed "the launchd job is probably not running"; the job had run and refused at 89/90). ---


def _refuse_one_of_ten(tmp_path):
    tickers = [f"T{i}" for i in range(10)]
    plan = plan_for(tmp_path, tickers)
    chunk = write_chunk(tmp_path, "chunk_0.json", {t: good_ticker() for t in tickers[:-1]})
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=1.0) == 2
    return plan


def test_refused_publish_writes_a_marker_naming_the_stale_ticker(tmp_path):
    plan = _refuse_one_of_ten(tmp_path)
    marker = json.loads((tmp_path / "out" / "payload.refused.json").read_text())
    assert (marker["slot"], marker["trading_day"]) == ("1430", "2026-10-15")
    assert (marker["valid"], marker["universe"]) == (9, 10)
    assert "T9" in marker["failed"] and marker["min_coverage"] == 1.0
    assert "below 100%" in marker["reason"]
    assert not (tmp_path / "out" / "payload.json").exists()
    assert plan["tickers"][-1] == "T9"


def test_check_reports_the_refusal_instead_of_a_bare_missing(tmp_path, capsys):
    plan = _refuse_one_of_ten(tmp_path)
    assert rp.check(plan, min_coverage=1.0) == 2
    err = capsys.readouterr().err
    assert "missing" in err
    assert "publisher ran and refused" in err
    assert "9/10" in err and "T9" in err


def test_check_without_a_marker_says_no_publisher_record(tmp_path, capsys):
    plan = plan_for(tmp_path, ["AAA"])
    assert rp.check(plan, min_coverage=1.0) == 2
    err = capsys.readouterr().err
    assert "no record that the publisher ran" in err
    assert "refused" not in err


def test_marker_for_another_slot_or_day_is_ignored(tmp_path, capsys):
    plan = _refuse_one_of_ten(tmp_path)
    capsys.readouterr()  # drop the refusal's own stderr, which names T9; only check's output is under test
    path = tmp_path / "out" / "payload.refused.json"
    marker = json.loads(path.read_text())
    marker["slot"] = "1330"
    path.write_text(json.dumps(marker))
    assert rp.check(plan, min_coverage=1.0) == 2
    err = capsys.readouterr().err
    assert "no record that the publisher ran" in err
    assert "T9" not in err


def test_unreadable_marker_degrades_to_no_record(tmp_path, capsys):
    plan = _refuse_one_of_ten(tmp_path)
    (tmp_path / "out" / "payload.refused.json").write_text("{not json")
    assert rp.check(plan, min_coverage=1.0) == 2
    assert "no record that the publisher ran" in capsys.readouterr().err


def test_a_successful_publish_clears_an_earlier_refusal(tmp_path):
    plan = _refuse_one_of_ten(tmp_path)
    assert (tmp_path / "out" / "payload.refused.json").is_file()
    tickers = plan["tickers"]
    chunk = write_chunk(tmp_path, "chunk_0.json", {t: good_ticker() for t in tickers})
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=1.0) == 0
    assert not (tmp_path / "out" / "payload.refused.json").exists()


def test_refusal_before_the_anchor_is_recorded_too(tmp_path):
    plan = plan_for(tmp_path, ["AAA"])
    payload, failed = rp.assemble(plan, [write_chunk(tmp_path, "chunk_0.json", {"AAA": good_ticker()})])
    early = datetime(2026, 10, 15, 14, 29, tzinfo=_ET)
    assert rp.publish(plan, payload, failed, min_coverage=0.8, now=early) == 2
    marker = json.loads((tmp_path / "out" / "payload.refused.json").read_text())
    assert "before the 14:30 anchor" in marker["reason"]


# -- launcher timing log (2026-10-21 09:35 abort: nobody knew how long a real 12-chunk build takes) -----------------

LAUNCHER = "deploy/launchd/robinhood_payload.sh"


# A ceiling, not an expectation: the 429 tests run the real fetch loop over 31 chunks (about 124 logged attempts), which takes
# ~17 s on a quiet Linux box and ~65 s with the CPU oversubscribed 6x there. The macOS CI runners spawn processes more slowly
# still and run these alongside ~12 xdist workers, and the old 120 s ceiling expired (CI 2026-11-15, "timed out after 120
# seconds"). A real hang still fails, just later.
LAUNCHER_TIMEOUT_SECONDS = 300


def _run_launcher(tmp_path, reply='echo "{}" >"$out"', *, extra_env=None, fake_script=None):
    """Run the real launcher for a past trading day with a stub `codex` (default: answers every chunk with `{}`).

    The merge then publishes nothing (no data for any ticker), which is fine: this exercises the fetch loop's logging only.
    ``fake_script`` replaces the whole stub when a test needs one that reads the prompt (``_RESCUE_STUB``).
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    fake = tmp_path / "fake_codex"
    fake.write_text(
        fake_script
        or f'#!/bin/bash\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\ncat >/dev/null\n{reply}\n'
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "DCA_REPO": str(repo),
        "DCA_PAYLOAD_PYTHON": sys.executable,
        "DCA_CODEX_BIN": str(fake),
        "DCA_TRADING_DAY": "2026-10-20",
        "DCA_CRON_TRACKING_ROOT": str(tmp_path / "ct"),
        "DCA_CODEX_PARALLEL": "3",
        # The stub publishes nothing by design, which the launcher would otherwise retry after its 90 s wait (2026-11-10);
        # the retry tests below switch it back on.
        "DCA_BUILD_RETRIES": "0",
        # Likewise the 30 s wait before re-asking a rate-limited chunk (2026-11-10): the 429 stubs would sleep on every
        # chunk. The backoff tests switch it back on with a short wait.
        "DCA_RATE_LIMIT_BACKOFF_SECONDS": "0",
        "TMPDIR": str(tmp_path),
        **(extra_env or {}),
    }
    done = subprocess.run(
        ["bash", str(repo / LAUNCHER), "0935"], env=env, capture_output=True, text=True, timeout=LAUNCHER_TIMEOUT_SECONDS
    )
    plan = subprocess.run(
        [sys.executable, "-m", "scripts.robinhood_payload", "plan", "--slot", "0935", "--day", "2026-10-20"],
        env=env, capture_output=True, text=True, cwd=repo, check=True,
    )
    return done, len(json.loads(plan.stdout)["chunks"])


def test_launcher_logs_each_chunks_duration_attempts_and_the_build_total(tmp_path):
    import re

    done, chunks = _run_launcher(tmp_path)
    log = done.stdout + done.stderr
    seen = re.findall(r"robinhood_payload: timing chunk=(\d+) attempt=(\d+) seconds=(\d+) errors=(\d+)", log)
    assert sorted(int(i) for i, *_ in seen) == list(range(chunks)), log  # one line per chunk, the stub never errs
    assert all(attempt == "1" and errors == "0" for _, attempt, _, errors in seen), seen
    total = re.search(r"robinhood_payload: timing total seconds=(\d+) chunks=(\d+) parallel=(\d+)", log)
    assert total and int(total.group(2)) == chunks and total.group(3) == "3", log


def test_launcher_logs_the_retry_attempt_of_a_chunk_whose_first_reply_had_an_error(tmp_path):
    import re

    # First call per chunk answers with a per-ticker error, the second with a clean reply; the counter lives beside
    # the chunk's output file, so it is per chunk.
    reply = (
        'n=$(cat "$out.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n >"$out.n"; '
        'if [ "$n" = 1 ]; then echo \'{"AAA": {"error": "x"}}\' >"$out"; else echo "{}" >"$out"; fi'
    )
    done, chunks = _run_launcher(tmp_path, reply)
    log = done.stdout + done.stderr
    seen = re.findall(r"robinhood_payload: timing chunk=(\d+) attempt=(\d+) seconds=\d+ errors=(\d+)", log)
    assert sorted(seen) == sorted(
        [(str(i), "1", "1") for i in range(chunks)] + [(str(i), "2", "0") for i in range(chunks)]
    ), log


# -- 429 backoff (2026-11-10: 09:35 and 10:30 refused at 60/122 and 98/122; every dead chunk's log ended "exceeded retry
# limit, last status: 429 Too Many Requests" after 3-4 s, and its retry fired at once into the same window) -------------

# First call per chunk dies like the 11-10 workers (no reply, a 429 on stderr); the second answers. The counter lives
# beside the chunk's output file, so it is per chunk.
_RATE_LIMITED_ONCE = (
    'n=$(cat "$out.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n >"$out.n"; '
    'if [ "$n" = 1 ]; then echo "ERROR: exceeded retry limit, last status: 429 Too Many Requests" >&2; exit 1; '
    'else echo "{}" >"$out"; fi'
)


def test_launcher_backs_off_before_retrying_a_rate_limited_chunk(tmp_path):
    import re

    done, chunks = _run_launcher(tmp_path, _RATE_LIMITED_ONCE, extra_env={"DCA_RATE_LIMIT_BACKOFF_SECONDS": "1", "DCA_CODEX_PARALLEL": "64"})
    log = done.stdout + done.stderr
    backoffs = re.findall(r"robinhood_payload: rate_limited chunk=(\d+) attempt=1 backoff_seconds=(\d+)", log)
    assert sorted(int(i) for i, _ in backoffs) == list(range(chunks)), log
    assert all(int(s) == 1 for _, s in backoffs), backoffs  # 1 s + jitter of at most 1 // 3 == 0
    # The retry after the wait is what fetched the data: every chunk's second attempt answered cleanly.
    retried = re.findall(r"robinhood_payload: timing chunk=(\d+) attempt=2 seconds=\d+ errors=0", log)
    assert sorted(int(i) for i in retried) == list(range(chunks)), log


def test_launcher_does_not_wait_after_a_failure_that_is_not_a_rate_limit(tmp_path):
    reply = (
        'n=$(cat "$out.n" 2>/dev/null || echo 0); n=$((n+1)); echo $n >"$out.n"; '
        'if [ "$n" = 1 ]; then echo "ERROR: stream disconnected" >&2; exit 1; else echo "{}" >"$out"; fi'
    )
    done, _ = _run_launcher(tmp_path, reply, extra_env={"DCA_RATE_LIMIT_BACKOFF_SECONDS": "1", "DCA_CODEX_PARALLEL": "64"})
    assert "rate_limited" not in done.stdout + done.stderr


def test_launcher_rate_limit_backoff_zero_turns_the_wait_off(tmp_path):
    done, _ = _run_launcher(tmp_path, _RATE_LIMITED_ONCE, extra_env={"DCA_RATE_LIMIT_BACKOFF_SECONDS": "0", "DCA_CODEX_PARALLEL": "64"})
    assert "rate_limited" not in done.stdout + done.stderr


def test_launcher_backoff_never_eats_the_retry_s_minimum_time_before_the_deadline(tmp_path):
    import re

    # A 600 s wait against a 25 s deadline: the wait shrinks so the retry still starts with MIN_ATTEMPT_SECONDS (20) left.
    done, _ = _run_launcher(
        tmp_path, _RATE_LIMITED_ONCE,
        extra_env={"DCA_RATE_LIMIT_BACKOFF_SECONDS": "600", "DCA_BUILD_DEADLINE_SECONDS": "25", "DCA_CODEX_PARALLEL": "64"},
    )
    log = done.stdout + done.stderr
    waits = [int(s) for s in re.findall(r"rate_limited chunk=\d+ attempt=1 backoff_seconds=(\d+)", log)]
    assert waits and all(s <= 5 for s in waits), log


@pytest.mark.parametrize("value", ["x", "-1", "1.5"])
def test_launcher_rejects_an_unusable_rate_limit_backoff(tmp_path, value):
    done, _ = _run_launcher(tmp_path, extra_env={"DCA_RATE_LIMIT_BACKOFF_SECONDS": value})
    assert done.returncode == 2 and "DCA_RATE_LIMIT_BACKOFF_SECONDS must be a whole number" in done.stderr


# -- a whole dead chunk (2026-10-26 09:35 abort) -----------------------------------------------------------------------
# Chunks 4 and 10 of the 0935 plan each failed both attempts, so 16 of 91 tickers (two whole chunks) went missing and
# the 95% floor refused the payload. The floor tolerates 4 misses but the unit that fails is an 8-ticker chunk, so one
# dead chunk alone was enough to cost the slot; and the launcher's cleanup deleted the workers' logs, so nobody could
# say afterwards why those two workers died. The incident's plan is replayed here (2026-10-20 has the same universe).

#: Stub ``codex``: answers each requested ticker with valid 0935 data for 2026-10-20, except that it dies (stderr line,
#: no reply, exit 1) on a request for a DEAD_CHUNKS chunk: with DEAD_MODE=whole only when the request is that whole
#: chunk, with DEAD_MODE=always whenever the request names any of its tickers.
_RESCUE_STUB = """#!{python}
import json, re, sys
out = sys.argv[sys.argv.index("-o") + 1]
tickers = re.search(r"Tickers: (.*)", sys.stdin.read()).group(1).split(", ")
dead = [set(c) for c in json.loads({dead!r})]
if any(set(tickers) == c if {mode!r} == "whole" else set(tickers) & c for c in dead):
    print("stub codex: simulated worker death for " + ",".join(tickers), file=sys.stderr)
    sys.exit(1)
bar = lambda ts: dict(timestamp=ts, open=10.0, high=10.2, low=9.9, close=10.1, volume=100)
reply = {{t: dict(previous_close_bar=bar("2026-10-19T00:00:00Z"), intraday_bars=[bar("2026-10-20T13:30:00Z")],
              quote=dict(price=10.1, as_of="2026-10-20T13:36:00Z")) for t in tickers}}
open(out, "w").write(json.dumps(reply))
"""

_DEAD = (4, 10)  # the two chunks lost on 2026-10-26


def _run_with_dead_chunks(tmp_path, mode):
    import sys

    plan = _plan_0935(tmp_path)
    dead = [plan["chunks"][i] for i in _DEAD]
    stub = _RESCUE_STUB.format(python=sys.executable, dead=json.dumps(dead), mode=mode)
    done, _ = _run_launcher(tmp_path, fake_script=stub)
    return done, plan, dead


def _plan_0935(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {**os.environ, "DCA_CRON_TRACKING_ROOT": str(tmp_path / "ct")}
    plan = subprocess.run(
        [sys.executable, "-m", "scripts.robinhood_payload", "plan", "--slot", "0935", "--day", "2026-10-20"],
        env=env, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], check=True,
    )
    return json.loads(plan.stdout)


def test_launcher_rescues_the_tickers_of_a_chunk_that_died_on_both_attempts(tmp_path):
    from pathlib import Path

    # The dead chunks' tickers are fine; only the 8-ticker request that carries them dies. Re-asking for them in
    # smaller requests must recover them, so the slot publishes its whole universe instead of aborting at Gate 5.
    done, plan, dead = _run_with_dead_chunks(tmp_path, "whole")
    log = done.stdout + done.stderr
    out = Path(plan["out"])
    assert done.returncode == 0 and out.exists(), log
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert meta["valid"] == meta["universe"] == len(plan["tickers"]) and meta["failed"] == {}, meta
    assert set(json.loads(out.read_text())["sources"]["robinhood"]) == set(plan["tickers"])


# -- a chunk that replied but left tickers out (2026-10-26 13:30 refusal) -----------------------------------------------
# Eight tickers came back "no data returned for this ticker" (coverage 83/91 < 95%) although Robinhood served every one
# of them minutes later: their chunks replied, so the dead-chunk rescue never ran and run_chunk's whole-chunk retry had
# not recovered them. They are now re-asked for alone.

#: Stub ``codex``: valid data for every requested ticker, except that a request for a whole LOSSY chunk loses its first
#: ticker (omitted, or answered with a per-ticker error when MODE=error). LOSS_ALWAYS=1 also loses it in a small request.
_PARTIAL_STUB = """#!{python}
import json, re, sys
out = sys.argv[sys.argv.index("-o") + 1]
tickers = re.search(r"Tickers: (.*)", sys.stdin.read()).group(1).split(", ")
bar = lambda ts: dict(timestamp=ts, open=10.0, high=10.2, low=9.9, close=10.1, volume=100)
reply = {{t: dict(previous_close_bar=bar("2026-10-19T00:00:00Z"), intraday_bars=[bar("2026-10-20T13:30:00Z")],
              quote=dict(price=10.1, as_of="2026-10-20T13:36:00Z")) for t in tickers}}
for chunk in json.loads({lossy!r}):
    if {always!r} and chunk[0] in tickers or set(tickers) == set(chunk):
        if {mode!r} == "error":
            reply[chunk[0]] = {{"error": "no data"}}
        else:
            del reply[chunk[0]]
open(out, "w").write(json.dumps(reply))
"""


def _run_with_lossy_chunks(tmp_path, mode, *, always=False):
    import sys

    plan = _plan_0935(tmp_path)
    lossy = [plan["chunks"][i] for i in _DEAD]
    stub = _PARTIAL_STUB.format(python=sys.executable, lossy=json.dumps(lossy), mode=mode, always=always)
    done, _ = _run_launcher(tmp_path, fake_script=stub)
    return done, plan, lossy


@pytest.mark.parametrize("mode", ["omitted", "error"])
def test_launcher_rescues_tickers_a_replying_chunk_left_out_or_errored(tmp_path, mode):
    from pathlib import Path

    # Two chunks reply, each minus its first ticker (absent, or a per-ticker error). The re-ask for just those tickers
    # succeeds, so the slot publishes the whole universe instead of 89/91.
    done, plan, lossy = _run_with_lossy_chunks(tmp_path, mode)
    log = done.stdout + done.stderr
    out = Path(plan["out"])
    assert done.returncode == 0 and out.exists(), log
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert meta["valid"] == meta["universe"] == len(plan["tickers"]) and meta["failed"] == {}, meta
    # Only the lossy chunks are rescued, and only for the one ticker each lost: no chunk that replied in full is re-asked.
    import re

    rescued = re.findall(r"robinhood_payload: rescue chunk=(\d+) tickers=(\d+)", log)
    assert sorted(rescued) == sorted((str(i), "1") for i in _DEAD), log


def test_launcher_rescue_never_makes_a_still_missing_ticker_worse(tmp_path):
    from pathlib import Path

    # The ticker is lost again in the small request, so it stays failed (one miss is inside the 95% floor, so the
    # payload still publishes) and the rest of its chunk keeps the data the first reply gave it.
    done, plan, lossy = _run_with_lossy_chunks(tmp_path, "omitted", always=True)
    log = done.stdout + done.stderr
    out = Path(plan["out"])
    assert done.returncode == 0 and out.exists(), log
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert set(meta["failed"]) == {chunk[0] for chunk in lossy}, meta
    assert set(json.loads(out.read_text())["sources"]["robinhood"]) == set(plan["tickers"]) - set(meta["failed"])


def test_launcher_keeps_a_dead_chunks_worker_log_beside_the_refusal(tmp_path):
    from pathlib import Path

    # The dead chunks' tickers fail however they are asked for, so the slot still refuses (the floor is unchanged);
    # but the workers' own output must survive the launcher's cleanup next to the refusal marker, or the next
    # incident is as undiagnosable as this one. Healthy chunks leave nothing behind.
    done, plan, dead = _run_with_dead_chunks(tmp_path, "always")
    log = done.stdout + done.stderr
    out = Path(plan["out"])
    assert done.returncode == 2 and out.with_suffix(".refused.json").exists() and not out.exists(), log
    kept = [p for p in out.parent.rglob("*") if p.is_file() and ".log" in p.name]
    assert kept, f"no worker log kept under {out.parent}: {sorted(p.name for p in out.parent.rglob('*'))}"
    texts = [p.read_text() for p in kept]
    assert all("simulated worker death" in t for t in texts), kept  # only the dead requests' logs are kept
    for chunk in dead:
        assert any(f"simulated worker death for {','.join(chunk)}" in t for t in texts), (chunk, kept)


# -- a whole chunk that came back with no intraday bars (2026-10-29 15:31 shadow build) ------------------------------
# Chunk 3 of the 1530 plan (NVDA, QBTS, FLKR, INTC, GOOGL, GOOG, AMZN, GLW) replied with every ticker's intraday_bars
# empty, so all eight were filed as "quiet" (no trade in the window) and the payload published 83/91. NVDA, GOOGL, GOOG
# and AMZN trade every minute of the session: a whole chunk with no bars is a worker that dropped the series, not eight
# names that all stopped trading at once. A chunk whose every ticker has no bars is re-asked for like a dead chunk; a
# chunk where only some names are empty (a genuinely thin name) is left alone, so it costs no rescue.

#: Stub ``codex``: valid data for every requested ticker, except that a request for a whole BLANK chunk answers every
#: ticker with ``intraday_bars: []``, and a request naming a THIN ticker answers that one ticker with no bars.
_BLANK_STUB = """#!{python}
import json, re, sys
out = sys.argv[sys.argv.index("-o") + 1]
tickers = re.search(r"Tickers: (.*)", sys.stdin.read()).group(1).split(", ")
bar = lambda ts: dict(timestamp=ts, open=10.0, high=10.2, low=9.9, close=10.1, volume=100)
reply = {{t: dict(previous_close_bar=bar("2026-10-19T00:00:00Z"), intraday_bars=[bar("2026-10-20T13:30:00Z")],
              quote=dict(price=10.1, as_of="2026-10-20T13:36:00Z")) for t in tickers}}
for chunk in json.loads({blank!r}):
    if set(tickers) == set(chunk):
        for t in tickers:
            reply[t]["intraday_bars"] = []
for t in json.loads({thin!r}):
    if t in reply:
        reply[t]["intraday_bars"] = []
for t in json.loads({liquid!r}):
    reply.get(t, {{}}).setdefault("previous_close_bar", {{}})["volume"] = 77568070.0  # NVDA's 10-28 session
    if t in reply and len(tickers) > {rescue_size}:  # dropped in the whole-chunk request, served in a small re-ask
        reply[t]["intraday_bars"] = []
open(out, "w").write(json.dumps(reply))
"""


def _run_with_blank_chunks(tmp_path, *, blank_chunks=_DEAD, thin=(), liquid=()):
    import sys

    plan = _plan_0935(tmp_path)
    blank = [plan["chunks"][i] for i in blank_chunks]
    stub = _BLANK_STUB.format(
        python=sys.executable, blank=json.dumps(blank), thin=json.dumps(list(thin)), liquid=json.dumps(list(liquid)),
        rescue_size=2,  # the launcher's default DCA_RESCUE_CHUNK_SIZE
    )
    done, _ = _run_launcher(tmp_path, fake_script=stub)
    return done, plan, blank


def test_launcher_rescues_a_chunk_whose_every_ticker_came_back_with_no_bars(tmp_path):
    import re
    from pathlib import Path

    done, plan, blank = _run_with_blank_chunks(tmp_path)
    log = done.stdout + done.stderr
    out = Path(plan["out"])
    assert done.returncode == 0 and out.exists(), log
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    # Recovered in the small re-asks: nothing is filed as quiet and nothing fails.
    assert meta["quiet"] == [] and meta["failed"] == {}, meta
    assert meta["valid"] == meta["universe"] == len(plan["tickers"]), meta
    rescued = re.findall(r"robinhood_payload: rescue chunk=(\d+) tickers=(\d+)", log)
    assert sorted(rescued) == sorted((str(i), str(len(plan["chunks"][i]))) for i in _DEAD), log


def test_launcher_leaves_a_genuinely_quiet_name_in_a_replying_chunk_alone(tmp_path):
    import re
    from pathlib import Path

    # One thin name in chunk 0 prints no bar, the rest of its chunk did: that is a quiet ticker, not a dead worker,
    # so no rescue runs for it and it is still reported as quiet (Gate 5 scores it NO_DATA).
    plan = _plan_0935(tmp_path)
    thin = plan["chunks"][0][0]
    done, plan, _ = _run_with_blank_chunks(tmp_path, blank_chunks=(), thin=[thin])
    log = done.stdout + done.stderr
    out = Path(plan["out"])
    assert done.returncode == 0 and out.exists(), log
    assert json.loads(out.with_suffix(".meta.json").read_text())["quiet"] == [thin]
    assert "0" not in {i for i, _ in re.findall(r"robinhood_payload: rescue chunk=(\d+) tickers=(\d+)", log)}, log


# -- liquid names dropped from a chunk that still answered others (2026-10-31, the 10-29 follow-up) --------------------
# The whole-chunk rule above needs EVERY answered ticker empty: had one ticker of 10-29's chunk 3 kept its bars, NVDA,
# GOOGL, GOOG and AMZN would have been filed quiet again. An empty name whose prior session traded LIQUID_PRIOR_VOLUME
# shares or more is now re-asked for alone; a thin empty name in the same chunk is still left alone and stays quiet.


def test_rescue_targets_reasks_an_empty_liquid_name_but_not_an_empty_thin_one():
    from scripts.robinhood_payload import LIQUID_PRIOR_VOLUME, rescue_targets

    def entry(volume, bars):
        return {"previous_close_bar": {"volume": volume}, "intraday_bars": bars, "quote": {}}

    bar = [{"timestamp": "x"}]
    reply = {
        "NVDA": entry(77568070.0, []),  # 10-29 15:31: no bar in the hour, ~78M shares the session before
        "AMZN": entry(LIQUID_PRIOR_VOLUME, []),  # the threshold itself is liquid
        "QTOP": entry(87085.0, []),  # a genuinely thin name: still quiet, never re-asked
        "AAPL": entry(35714331.0, bar),  # answered with bars: never re-fetched
        "ODD": {"previous_close_bar": "garbage", "intraday_bars": []},  # unreadable volume reads as not liquid
        "BOOL": entry(True, []),
        "ERR": {"error": "no data"},
    }
    tickers = ["NVDA", "AMZN", "QTOP", "AAPL", "ODD", "BOOL", "ERR", "GONE"]
    assert rescue_targets(reply, tickers) == ["NVDA", "AMZN", "ERR", "GONE"]
    # Unchanged rules: a dead chunk, and a chunk whose every answered ticker is empty, are re-asked in full.
    assert rescue_targets({}, ["A", "B"]) == ["A", "B"]
    assert rescue_targets({"A": entry(10.0, []), "B": entry(10.0, [])}, ["A", "B"]) == ["A", "B"]
    assert rescue_targets({"A": entry(10.0, bar), "B": entry(10.0, [])}, ["A", "B"]) == []


def test_launcher_rescues_an_empty_liquid_name_in_a_chunk_that_answered_others(tmp_path):
    import re
    from pathlib import Path

    # Chunk 0 answers in full, but its first two tickers come back with no bars and a ~78M-share prior session (the
    # 10-29 shape, minus the "whole chunk empty" that the earlier rule needed), and its third is a genuinely thin name.
    # The two liquid names are re-asked for and recovered; the thin one is not re-asked for and is still filed quiet.
    plan = _plan_0935(tmp_path)
    liquid, thin = plan["chunks"][0][:2], plan["chunks"][0][2]
    done, plan, _ = _run_with_blank_chunks(tmp_path, blank_chunks=(), thin=[thin], liquid=liquid)
    log = done.stdout + done.stderr
    out = Path(plan["out"])
    assert done.returncode == 0 and out.exists(), log
    meta = json.loads(out.with_suffix(".meta.json").read_text())
    assert meta["quiet"] == [thin] and set(meta["failed"]) == {thin}, meta
    assert set(liquid) <= set(json.loads(out.read_text())["sources"]["robinhood"])
    assert re.findall(r"robinhood_payload: rescue chunk=(\d+) tickers=(\d+)", log) == [("0", "2")], log


# -- opt-in overall build deadline (2026-10-22): default off, so the six scheduled slots are unchanged ----------------


def test_launcher_has_no_deadline_by_default_and_never_skips_a_chunk(tmp_path):
    import re

    done, chunks = _run_launcher(tmp_path)
    log = done.stdout + done.stderr
    assert "deadline chunk=" not in log and re.search(r"timing total .* deadline=0\b", log), log
    # Whole chunks only (2026-10-26): the stub's empty replies now also trigger rescue requests, which log
    # `timing chunk=<i>.r<k>`; those are rescue parts, not first-pass chunks, so they must not count here.
    assert len(re.findall(r"timing chunk=\d+ attempt=", log)) == chunks, log


def test_launcher_deadline_skips_chunks_that_cannot_start_in_time(tmp_path):
    import re

    # Each stub reply takes 2 s; a 4 s deadline with a 2 s minimum lets the first wave of three chunks run, and every
    # later wave starts with too little time left. The skipped chunks leave their tickers to the merge's coverage floor.
    done, chunks = _run_launcher(
        tmp_path,
        'sleep 2; echo "{}" >"$out"',
        extra_env={"DCA_BUILD_DEADLINE_SECONDS": "4", "DCA_MIN_ATTEMPT_SECONDS": "2"},
    )
    log = done.stdout + done.stderr
    ran = {int(i) for i in re.findall(r"timing chunk=(\d+) attempt=1", log)}
    skipped = {int(i) for i in re.findall(r"deadline chunk=(\d+) attempt=1 skipped", log)}
    assert {0, 1, 2} <= ran, log  # the first wave started well inside the deadline
    assert set(range(6, chunks)) <= skipped, log  # waves 3 and 4 start after it, whatever the machine's speed
    assert not ran & skipped and re.search(r"timing total .* deadline=4\b", log), log
    assert "is below 95%" in log and done.returncode == 2, log  # the merge sees the gap and publishes nothing


def test_launcher_rejects_a_non_numeric_deadline(tmp_path):
    done, _ = _run_launcher(tmp_path, extra_env={"DCA_BUILD_DEADLINE_SECONDS": "soon"})
    assert done.returncode == 2 and "must be whole numbers" in done.stderr


def test_launcher_et_cutoff_that_already_passed_fetches_nothing_and_refuses(tmp_path):
    import re

    # 2026-10-30: a relative deadline counts from whenever launchd started the build, so a late 15:31 job could fetch
    # past the 3:30 session's 12-minute re-check. The ET cutoff is absolute on the plan's trading day (2026-10-20 here,
    # long past), so every chunk is skipped, the launcher says why, and the merge refuses instead of publishing late.
    done, chunks = _run_launcher(tmp_path, extra_env={"DCA_BUILD_DEADLINE_ET": "15:40"})
    log = done.stdout + done.stderr
    assert "deadline 15:40 ET had already passed when the build started; fetching nothing" in log, log
    assert set(range(chunks)) <= {int(i) for i in re.findall(r"deadline chunk=(\d+) attempt=1 skipped", log)}, log
    assert "timing chunk=" not in log and re.search(r"timing total .* deadline_et=15:40\b", log), log
    assert "is below 95%" in log and done.returncode == 2, log


def test_launcher_et_cutoff_still_ahead_does_not_skip_any_chunk(tmp_path):
    import re

    # A future trading day's 15:40 is hours away, so no chunk is skipped: the cutoff binds only when the clock reaches it.
    done, _ = _run_launcher(tmp_path, extra_env={"DCA_BUILD_DEADLINE_ET": "15:40", "DCA_TRADING_DAY": "2027-01-04"})
    log = done.stdout + done.stderr
    assert "deadline chunk=" not in log and "already passed" not in log, log
    assert re.search(r"timing chunk=0 attempt=1", log) and re.search(r"deadline=0 deadline_et=15:40\b", log), log


@pytest.mark.parametrize("cutoff", ["3:40", "15:4", "25:00", "15:40:00", "soon"])
def test_launcher_rejects_a_malformed_et_cutoff(tmp_path, cutoff):
    done, _ = _run_launcher(tmp_path, extra_env={"DCA_BUILD_DEADLINE_ET": cutoff})
    assert done.returncode == 2 and "must be HH:MM" in done.stderr and "timing chunk=" not in done.stderr, done.stderr


@pytest.mark.parametrize("size, needle", [("two", "must be whole numbers"), ("0", "must be at least 1")])
def test_launcher_rejects_an_unusable_rescue_size(tmp_path, size, needle):
    # 0 would make the rescue loop's step zero; refuse it before any chunk is fetched.
    done, _ = _run_launcher(tmp_path, extra_env={"DCA_RESCUE_CHUNK_SIZE": size})
    assert done.returncode == 2 and needle in done.stderr and "timing chunk=" not in done.stderr, done.stderr


def test_launcher_chunk_size_env_shrinks_the_first_pass_chunks(tmp_path):
    # 2026-11-03: 8-ticker chunks drew "exceeds the output limit" on nearly every reply at 15:31; DCA_CHUNK_SIZE overrides
    # the planner default (4 since 2026-11-02). n tickers at 4 per chunk is c chunks, so at 2 per chunk there are at least 2c - 1.
    import re

    (tmp_path / "default").mkdir()
    (tmp_path / "small").mkdir()
    _, default_chunks = _run_launcher(tmp_path / "default")
    small = tmp_path / "small"
    done, _ = _run_launcher(small, extra_env={"DCA_CHUNK_SIZE": "2"})
    total = re.search(r"timing total seconds=\d+ chunks=(\d+)", done.stdout + done.stderr)
    assert total and int(total.group(1)) >= 2 * default_chunks - 1, done.stderr


@pytest.mark.parametrize("size, needle", [("four", "must be whole numbers"), ("0", "must be at least 1")])
def test_launcher_rejects_an_unusable_chunk_size(tmp_path, size, needle):
    done, _ = _run_launcher(tmp_path, extra_env={"DCA_CHUNK_SIZE": size})
    assert done.returncode == 2 and needle in done.stderr and "timing chunk=" not in done.stderr, done.stderr


# -- 3:30 PM: plannable and buildable on demand by its own 15:31 agent, never by `auto`; its live prompt reads the payload since 2026-10-30 --


def bars_from(start_h, start_m, end_h, end_m):
    """5-minute UTC bars (EDT = UTC-4) from start through the last bar before end, both ET wall-clock times."""
    out, minute = [], (start_h * 60 + start_m)
    while minute < end_h * 60 + end_m:
        h, m = divmod(minute + 240, 60)
        out.append(bar(f"2026-10-15T{h:02d}:{m:02d}:00Z"))
        minute += 5
    return out


def final_buy_ticker(bars):
    return {
        "previous_close_bar": bar("2026-10-14T00:00:00Z"),
        "intraday_bars": bars,
        "quote": {"price": 10.1, "as_of": "2026-10-15T19:31:00Z"},  # 15:31 ET
    }


def final_buy_kw(**over):
    return kw(bar_start=time(14, 25), decision=time(15, 30), **over)


def test_1530_is_plannable_but_the_scheduled_slots_stay_the_six_prompt_wired_ones():
    assert rp.SLOTS == ("0935", "1030", "1130", "1230", "1330", "1430")
    assert "1530" in rp.PLANNABLE_SLOTS and "1530" not in rp.SLOTS
    # launchd's `auto` must never pick 3:30 up: every minute of the day resolves only to a scheduled slot.
    due = {rp.due_slot(DAY, time(h, m)) for h in range(8, 17) for m in range(60)} - {None}
    assert due == set(rp.SLOTS)
    assert rp.due_slot(DAY, time(15, 31)) is None and rp.due_slot(DAY, time(15, 40)) is None


def test_1530_plan_scores_the_universe_and_interval_the_final_buy_generator_uses(monkeypatch):
    import dca_config
    import dca_runner
    from scripts.generate_buy_candidates import SLOT_SPECS

    class Open:  # a normal full trading day
        is_closed = False
        is_early_close = False

    monkeypatch.setattr(dca_runner, "market_day_status", lambda day: Open())
    monkeypatch.setattr(dca_config, "load_approved_dates", lambda config_dir: {"approved_test_dates": {DAY}})
    plan = rp.build_plan("1530", DAY)
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    assert plan["tickers"] == [t for t in config.tickers if t not in config.excluded_tickers]
    assert plan["decision_time_et"] == SLOT_SPECS["1530ET"]["decision_time_et"].strftime("%H:%M") == "15:30"
    assert plan["interval_minutes"] == config.robinhood_interval_minutes == 5
    assert plan["bar_start_et"] == "14:25" and plan["shadow"] is True
    assert plan["out"].endswith("robinhood_payload_2026-10-15_1530.json")
    assert rp.build_plan("1230", DAY)["shadow"] is False


def test_1530_plan_refuses_an_early_close_and_an_unapproved_day(monkeypatch):
    import dca_config
    import dca_runner

    class Early:
        is_closed = False
        is_early_close = True

    monkeypatch.setattr(dca_runner, "market_day_status", lambda day: Early())
    monkeypatch.setattr(dca_config, "load_approved_dates", lambda config_dir: {"approved_test_dates": {DAY}})
    with pytest.raises(SystemExit) as early:
        rp.build_plan("1530", DAY)
    assert early.value.code == rp.EXIT_NOT_TRADING_DAY

    class Open:
        is_closed = False
        is_early_close = False

    monkeypatch.setattr(dca_runner, "market_day_status", lambda day: Open())
    monkeypatch.setattr(dca_config, "load_approved_dates", lambda config_dir: {"approved_test_dates": set()})
    with pytest.raises(SystemExit) as unapproved:
        rp.build_plan("1530", DAY)
    assert unapproved.value.code == rp.EXIT_OUTSIDE_WINDOW
    with pytest.raises(SystemExit, match="cannot be planned"):
        rp.build_plan("1545", DAY)


def test_1530_window_needs_the_lookback_bar_and_the_bar_before_the_decision():
    ok = rp.validate_ticker(final_buy_ticker(bars_from(14, 25, 15, 30)), **final_buy_kw())
    assert ok["intraday_bars"][0]["timestamp"].startswith("2026-10-15T14:25") and len(ok["intraday_bars"]) == 13
    # the generator's 60-minutes-back lookback bar (14:25) is missing: bars that start late must not pass
    with pytest.raises(rp.PayloadError, match="do not cover"):
        rp.validate_ticker(final_buy_ticker(bars_from(14, 45, 15, 30)), **final_buy_kw())
    # 2026-10-30 regression: one interval late used to pass (the old `> start + interval` slack), and a 14:30-first
    # series is exactly what the generator then scored NO_DATA/provider_error for all 91 tickers
    with pytest.raises(rp.PayloadError, match=r"do not cover the 14:25 .*first bar 14:30"):
        rp.validate_ticker(final_buy_ticker(bars_from(14, 30, 15, 30)), **final_buy_kw())
    # the series stopped early: no completed decision bar
    with pytest.raises(rp.PayloadError, match="far before"):
        rp.validate_ticker(final_buy_ticker(bars_from(14, 25, 15, 10)), **final_buy_kw())


# -- quiet tickers (2026-10-26 and 2026-10-27 13:30 refusals) ----------------------------------------------------
# Eight names (DFTX, TRIN, AAAU, SPCX, EVR, LUNR, IRDM, DXYZ) printed no 5-minute bar between 12:30 and 13:30 on
# 10-27, so 82/90 (91%) refused the slot (on 10-26 the same eight were left out of the reply instead). 2026-10-31: they
# are exactly plan chunk 0 and include names trading 1-68M shares a day, so this was a dropped chunk, now re-asked by
# the launcher's rescue; the quiet rule below still holds for names that really did not trade. A name that did not trade is not a failed fetch:
# it is reported as "quiet" (Gate 5 scores it NO_DATA, never a BUY), kept out of the coverage denominator, and
# capped so a real feed outage, which also yields empty series, still refuses.


def quiet_ticker():
    raw = good_ticker()
    raw["intraday_bars"] = []
    return raw


def stale_ticker():
    # FLKR 2026-10-27: it traded, but its newest bar is far before the anchor, so this stays a real failure.
    return good_ticker(minutes=range(30, 40, 5)) | {"intraday_bars": [bar("2026-10-15T17:30:00Z")]}


def liquid_empty_ticker(volume=5_000_000):
    # 2026-11-02 10:30: AMZN, GOOG and GOOGL printed no bars in the window although each traded millions of shares the
    # session before. That is a dropped series, not a quiet hour.
    raw = good_ticker()
    raw["intraday_bars"] = []
    raw["previous_close_bar"]["volume"] = volume
    return raw


def universe_plan(tmp_path, *, quiet=0, errored=0, stale=0, liquid=0, total=90):
    tickers = [f"T{i}" for i in range(total)]
    plan = plan_for(tmp_path, tickers)
    replies = {t: good_ticker() for t in tickers}
    cursor = 0
    for count, make in (
        (quiet, quiet_ticker), (liquid, liquid_empty_ticker), (stale, stale_ticker), (errored, lambda: {"error": "x"}),
    ):
        for t in tickers[cursor : cursor + count]:
            replies[t] = make()
        cursor += count
    chunk = write_chunk(tmp_path, "chunk_0.json", replies)
    return plan, tickers, chunk


def test_a_ticker_with_no_bars_in_the_window_is_a_quiet_ticker_error():
    with pytest.raises(rp.QuietTickerError):
        rp.validate_ticker(quiet_ticker(), **kw())


def test_a_stale_newest_bar_is_a_plain_failure_not_a_quiet_ticker():
    with pytest.raises(rp.PayloadError) as excinfo:
        rp.validate_ticker(stale_ticker(), **kw())
    assert not isinstance(excinfo.value, rp.QuietTickerError)


def test_quiet_tickers_are_reported_apart_from_failed_fetches(tmp_path):
    plan, tickers, chunk = universe_plan(tmp_path, quiet=8)
    payload, failed = rp.assemble(plan, [chunk])
    assert payload["ticker_errors"] == {"robinhood": [], "quiet": sorted(tickers[:8])}
    assert sorted(payload["sources"]["robinhood"]) == sorted(tickers[8:])
    assert set(failed) == set(tickers[:8])  # the reason is still recorded for the log and the sidecar


def test_eight_quiet_tickers_of_ninety_still_publish(tmp_path):
    plan, tickers, chunk = universe_plan(tmp_path, quiet=8)
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0
    assert not (tmp_path / "out" / "payload.refused.json").exists()
    published = json.loads((tmp_path / "out" / "payload.json").read_text())
    # generate_buy_candidates rejects unknown ticker_errors keys and scores errored names NO_DATA (never a BUY),
    # so a quiet name stays under "robinhood" in the file; the sidecar says which of those did not trade.
    assert published["ticker_errors"] == {"robinhood": sorted(tickers[:8])}
    meta = json.loads((tmp_path / "out" / "payload.meta.json").read_text())
    assert meta["quiet"] == sorted(tickers[:8])
    assert rp.check(plan, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0  # re-validation must not read them as misses


def test_more_quiet_tickers_than_the_ceiling_still_refuse(tmp_path):
    # A feed that drops most of the universe can still look thin; the ceiling keeps that a refusal.
    # 23 of 90 is 25.6%, just over the 25% ceiling.
    plan, _, chunk = universe_plan(tmp_path, quiet=23)
    assert rp.MAX_QUIET_SHARE == 0.25
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 2
    assert not (tmp_path / "out" / "payload.json").exists()
    assert (tmp_path / "out" / "payload.refused.json").is_file()


def test_the_floor_applies_to_the_non_quiet_tickers(tmp_path):
    # 4 quiet leaves 86; 5 real failures give 81/86 = 94.2%, under the floor.
    plan, _, chunk = universe_plan(tmp_path, quiet=4, errored=5)
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 2


def test_real_failures_within_the_floor_publish_beside_quiet_tickers(tmp_path):
    # 4 quiet leaves 86; 4 real failures give 82/86 = 95.3%, at or over the floor.
    plan, tickers, chunk = universe_plan(tmp_path, quiet=4, errored=4)
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0
    published = json.loads((tmp_path / "out" / "payload.json").read_text())
    assert published["ticker_errors"] == {"robinhood": sorted(tickers[:8])}
    assert json.loads((tmp_path / "out" / "payload.meta.json").read_text())["quiet"] == sorted(tickers[:4])
    assert rp.check(plan, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0


def test_a_stale_newest_bar_still_counts_against_the_floor(tmp_path):
    # 2026-10-27 FLKR: 8 quiet plus 1 stale. The stale name stays under ticker_errors.robinhood, where the
    # live prompts' ">5% errored aborts" rule still sees it; only the quiet names move to their own key.
    plan, tickers, chunk = universe_plan(tmp_path, quiet=8, stale=1)
    payload, failed = rp.assemble(plan, [chunk])
    assert payload["ticker_errors"]["robinhood"] == [tickers[8]]
    assert payload["ticker_errors"]["quiet"] == sorted(tickers[:8])


def test_a_published_payload_with_quiet_tickers_is_accepted_by_the_candidate_generator(tmp_path):
    # generate_buy_candidates rejects ticker_errors keys that are not source names; the file must stay valid.
    from scripts.generate_buy_candidates import _VALID_SOURCE_NAMES

    plan, _, chunk = universe_plan(tmp_path, quiet=8)
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0
    published = json.loads((tmp_path / "out" / "payload.json").read_text())
    assert set(published["ticker_errors"]) <= set(_VALID_SOURCE_NAMES)


def test_live_mirror_prompts_leave_quiet_tickers_out_of_the_errored_count():
    from pathlib import Path

    mirror = Path(__file__).resolve().parents[1] / "claude" / "dca-executor-prompts" / "live-mirror"
    for slot in rp.SLOTS:
        (path,) = mirror.glob(f"{slot}ET-*.txt")
        _, rule = _errored_ticker_rule(path)
        assert '"quiet"' in rule and ".meta.json" in rule, path.name


# -- 3:30 PM: the payload window must reach the generator's own lookback bar (2026-10-30 Gate 5 abort) -----------------
# The first live 3:30 run on the prebuilt payload passed `check` 91/91 and still scored every ticker NO_DATA with
# provider_error: generate_buy_candidates anchors 15:30 on the COMPLETED 15:25 bar, so its 60-minute lookback is 14:25,
# but the plan fetched bars from 14:30. The shadow week only ran `check`, never the generator, so nothing caught it.
# These tests feed a payload built to the plan's own window through the generator, and derive the window from the
# generator's anchor so the two cannot drift apart again.


def _plan_window_entry(plan: dict, ticker_close_start: float = 100.0) -> dict:
    """One Robinhood entry covering exactly the plan's window: bars from bar_start_et up to the open decision bar."""
    from datetime import datetime, time, timedelta, timezone
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    day = date.fromisoformat(plan["trading_day"])
    start = datetime.combine(day, time.fromisoformat(plan["bar_start_et"]), et)
    decision = datetime.combine(day, time.fromisoformat(plan["decision_time_et"]), et)
    step = timedelta(minutes=int(plan["interval_minutes"]))

    def bar(stamp, price):
        return {"timestamp": stamp.isoformat(), "open": price, "high": price * 1.01, "low": price * 0.99,
                "close": price, "volume": 100}

    bars, stamp, price = [], start, ticker_close_start
    while stamp < decision:
        bars.append(bar(stamp, price))
        stamp, price = stamp + step, price - 0.05  # a steady intraday dip below the previous close
    # Robinhood stamps a daily bar at 00:00Z of its own session (scripts/ingest_market_data._session_date).
    previous = datetime(2026, 10, 29, tzinfo=timezone.utc)
    return {
        "previous_close_bar": bar(previous, ticker_close_start),
        "intraday_bars": bars,
        "quote": {"price": bars[-1]["close"], "as_of": (decision + timedelta(minutes=1)).isoformat()},
    }


def test_1530_plan_window_starts_at_or_before_the_generators_lookback_bar():
    from datetime import datetime, time

    from near_close_dca import StrategyConfig, _compute_signal_timestamps

    plan = rp.build_plan("1530", date(2026, 10, 30))
    decision = datetime.combine(date(2026, 10, 30), time.fromisoformat(plan["decision_time_et"]), rp.ET)
    *_, lookback_anchor = _compute_signal_timestamps(decision, StrategyConfig(), int(plan["interval_minutes"]))
    assert time.fromisoformat(plan["bar_start_et"]) <= lookback_anchor.time(), (plan["bar_start_et"], lookback_anchor)


def test_a_payload_built_to_the_1530_plan_window_scores_in_the_generator():
    from datetime import datetime, time

    from near_close_dca import StrategyConfig
    from scripts import generate_buy_candidates as gbc

    day = date(2026, 10, 30)
    plan = rp.build_plan("1530", day)
    ticker = plan["tickers"][0]
    entry = _plan_window_entry(plan)
    decision = datetime.combine(day, time.fromisoformat(plan["decision_time_et"]), rp.ET)
    # The entry is exactly what `check` accepts for this plan...
    rp.validate_ticker(entry, day=day, bar_start=time.fromisoformat(plan["bar_start_et"]), decision=decision.time(),
                       interval=int(plan["interval_minutes"]), now=decision)
    # ...and the 3:30 generator, on the Robinhood-only payload (1530ET finance_outage_fallback), must be able to score it.
    candidates = gbc.generate_candidates(
        slot="1530ET", now=decision, config=StrategyConfig(), market_data_by_source={"robinhood": {ticker: entry}},
        finance_outage_robinhood_only=True, source_errors=(), ticker_errors={"robinhood": []},
        already_covered_tickers=(),
    )
    (row,) = [c for c in candidates if c["ticker"] == ticker]
    assert row["provider_error"] is False, row
    assert row["action"] == "BUY", row


# -- quiet means thin, not merely empty (2026-11-02) -------------------------------------------------------------
# One 10% cap treated every empty series alike. On 2026-11-02 it published a payload whose AMZN, GOOG and GOOGL had no
# bars (all liquid) while a healthy fetch with ten genuinely thin names (11%) would have been refused. An empty series
# is quiet only when the prior session was thin (< LIQUID_PRIOR_VOLUME shares); a liquid name that is empty after the
# launcher's re-ask is a real failure against the 95% floor. Thin quiet names are bounded only by a higher ceiling.


@pytest.mark.parametrize("volume, quiet", [
    (rp.LIQUID_PRIOR_VOLUME - 1, True),   # just under the threshold: thin
    (rp.LIQUID_PRIOR_VOLUME, False),      # the threshold itself is liquid (as in rescue_targets)
    (5_000_000, False),
    (0, True),
])
def test_an_empty_series_is_quiet_only_when_the_prior_session_was_thin(volume, quiet):
    if quiet:
        with pytest.raises(rp.QuietTickerError):
            rp.validate_ticker(liquid_empty_ticker(volume), **kw())
        return
    with pytest.raises(rp.PayloadError) as excinfo:
        rp.validate_ticker(liquid_empty_ticker(volume), **kw())
    assert not isinstance(excinfo.value, rp.QuietTickerError)
    assert "liquid" in str(excinfo.value)  # the recorded reason says why it is not filed quiet


def test_a_prior_session_without_a_volume_reads_as_thin():
    # Same rule as rescue_targets: an unreadable prior session never turns an empty series into a failure by itself.
    raw = liquid_empty_ticker()
    del raw["previous_close_bar"]["volume"]  # validate_bar reads a missing volume as 0
    with pytest.raises(rp.QuietTickerError):
        rp.validate_ticker(raw, **kw())


def test_ten_thin_quiet_names_of_ninety_no_longer_refuse_a_healthy_fetch(tmp_path):
    # 2026-11-02 probe: ten names with no 12:30-13:30 bars (11%) refused the slot although nothing else was wrong.
    plan, tickers, chunk = universe_plan(tmp_path, quiet=10)
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0
    meta = json.loads((tmp_path / "out" / "payload.meta.json").read_text())
    assert meta["quiet"] == sorted(tickers[:10])
    assert rp.check(plan, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0


def test_liquid_empty_names_are_real_failures_in_the_published_error_list(tmp_path):
    # 4 of 90 may miss (86/90 = 95.6%); they stay in ticker_errors.robinhood and are not listed as quiet.
    plan, tickers, chunk = universe_plan(tmp_path, liquid=4)
    payload, failed = rp.assemble(plan, [chunk])
    assert payload["ticker_errors"] == {"robinhood": sorted(tickers[:4])}  # no "quiet" key: none of them is thin
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0
    meta = json.loads((tmp_path / "out" / "payload.meta.json").read_text())
    assert meta["quiet"] == []
    assert rp.check(plan, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0


def test_five_liquid_empty_names_of_ninety_refuse(tmp_path):
    # 85/90 = 94.4%, under the floor: a degraded fetch no longer hides behind the quiet classification.
    plan, _, chunk = universe_plan(tmp_path, liquid=5)
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 2
    assert not (tmp_path / "out" / "payload.json").exists()


def test_monday_10_30_replay_a_degraded_payload_is_refused(tmp_path):
    # 2026-11-02 10:30: AMZN, GOOG, GOOGL and GLW empty (liquid) plus four other failures, of 91 tickers. It published
    # because the four liquid empties were filed quiet and left the denominator (83/87 = 95.4%). They now count:
    # 83/91 = 91.2%, under the floor.
    plan, _, chunk = universe_plan(tmp_path, liquid=4, errored=4, total=91)
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 2


def test_thin_quiet_names_leave_the_denominator_while_liquid_empties_count(tmp_path):
    # 10 thin quiet leave 80; 3 liquid empties give 77/80 = 96.3%, over the floor.
    plan, tickers, chunk = universe_plan(tmp_path, quiet=10, liquid=3)
    payload, failed = rp.assemble(plan, [chunk])
    assert payload["ticker_errors"]["quiet"] == sorted(tickers[:10])
    assert payload["ticker_errors"]["robinhood"] == sorted(tickers[10:13])  # the liquid empties, not the thin names
    assert rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0
    published = json.loads((tmp_path / "out" / "payload.json").read_text())
    assert published["ticker_errors"] == {"robinhood": sorted(tickers[:13])}  # every name stays listed for Gate 5
    assert rp.check(plan, min_coverage=rp.DEFAULT_MIN_COVERAGE) == 0


# -- whole-build retry and failure diagnostics (2026-11-10: 09:35 and 10:30 refused with Codex "429 Too Many Requests") --

#: A worker that fails the way the 11-10 ones did: the 429 text on stderr, no reply file, a non-zero exit.
_ALWAYS_429 = 'echo "ERROR: exceeded retry limit, last status: 429 Too Many Requests" >&2; exit 1'


def _failure_rows(tmp_path):
    import glob

    rows = []
    for path in glob.glob(str(tmp_path / "ct" / "**" / "*.failures.jsonl"), recursive=True):
        rows += [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    return rows


def test_a_refused_build_is_retried_once_and_every_failed_attempt_is_recorded(tmp_path):
    done, chunks = _run_launcher(tmp_path, _ALWAYS_429, extra_env={"DCA_BUILD_RETRIES": "1", "DCA_BUILD_RETRY_WAIT_SECONDS": "0"})
    log = done.stdout + done.stderr
    assert "retrying in 0s (attempt 2 of 2)" in log, log
    assert log.count("robinhood_payload: timing total") == 2, log  # exactly one rebuild, never a loop
    rows = _failure_rows(tmp_path)
    assert rows and all(r["http_status"] == ["429"] for r in rows), rows[:2]
    assert {r["build_attempt"] for r in rows} == {1, 2}
    assert {"codex_exec_running", "payload_workers_running", "parallel_setting", "seconds_into_build"} <= set(rows[0])
    assert rows[0]["parallel_setting"] == 3 and rows[0]["slot"] == "0935"
    assert "http429=" in log and "failed worker attempts=" in log, log


def test_the_retry_can_be_turned_off(tmp_path):
    done, _ = _run_launcher(tmp_path, _ALWAYS_429, extra_env={"DCA_BUILD_RETRIES": "0"})
    log = done.stdout + done.stderr
    assert "retrying" not in log and log.count("robinhood_payload: timing total") == 1, log


def test_no_retry_when_the_first_attempt_began_too_long_ago(tmp_path):
    done, _ = _run_launcher(
        tmp_path, _ALWAYS_429, extra_env={"DCA_BUILD_RETRIES": "1", "DCA_BUILD_RETRY_WAIT_SECONDS": "0", "DCA_BUILD_RETRY_BY_SECONDS": "0"}
    )
    log = done.stdout + done.stderr
    assert "not retrying: the first attempt began too long ago" in log and log.count("timing total") == 1, log


def test_a_healthy_build_writes_no_failure_file_and_never_retries(tmp_path):
    done, _ = _run_launcher(tmp_path)
    log = done.stdout + done.stderr
    assert _failure_rows(tmp_path) == [] and "retrying" not in log and "failed worker attempts" not in log, log


def test_retry_settings_must_be_whole_numbers(tmp_path):
    done, _ = _run_launcher(tmp_path, extra_env={"DCA_BUILD_RETRIES": "once"})
    assert done.returncode == 2 and "DCA_BUILD_RETRIES" in done.stderr
