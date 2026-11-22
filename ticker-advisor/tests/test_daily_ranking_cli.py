"""Tests for the not-yet-written ``main`` in ticker-advisor/daily_ranking.py (step 7b-1, the command line).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

Step 7b-1 wraps ``run_daily_ranking`` in a command line that reads FILES and writes the proposal and report. It is
still PROPOSE-ONLY: it never writes ``config/tickers.json`` (nor any file outside ``--out-dir``), and it has no
scheduler, no network and no checkout logic. Those are step 7b-2, blocked on the backup-planner decision.

Inputs: a validated fundamentals snapshot file, the price archive CSV, the tickers config (``active`` is the prior
list, an optional ``pinned`` array the hand-held names), an optional exchange map (``{"AAA": "Nasdaq"}``), the
as-of date and the output directory. The pool floors are options with the ADR's values as defaults, because the
operator has not yet decided them; the report shows who fails them.

Exit codes: 0 the proposal passed the safeguards, 1 it was REJECTED (outputs are still written, so a human can
read why), 2 bad input (a one-line message on stderr, never a traceback or file contents).

All data is SYNTHETIC. No run of 6 or more digits appears below (the account-number guard).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from daily_ranking import main

AS_OF = date(2026, 6, 30)
FETCHED = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
NAMES = ["AAA", "AAB", "AAC", "AAD", "AAE", "AAF", "AAG", "AAH"]
HEADER = "ticker,date,open,high,low,close,volume,vwap,n\n"
BARS = 300
SMALL_FLOORS = ["--min-price", "1", "--min-market-cap", "1", "--min-dollar-volume", "1", "--min-history", "1"]


def stamp(document):
    content = {k: v for k, v in document.items() if k not in ("fetched_at", "content_sha256")}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
    document["content_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return document


def fact(val, start=None, end="2025-12-31"):
    return {"val": val, "start": start, "end": end, "filed": "2026-02-11", "form": "10-K", "accn": "A-1"}


def record(i):
    facts = {
        "us-gaap:GrossProfit": fact(400 + 10 * i, start="2025-01-01"),
        "us-gaap:Assets": fact(5000),
        "us-gaap:StockholdersEquity": fact(2000 + 100 * i),
        "dei:EntityCommonStockSharesOutstanding": fact(70, end="2026-02-05"),
    }
    return {
        "status": "ok",
        "cik": "c",
        "sic": "3571",
        "sic_description": "d",
        "sector": "35",
        "industry": "3571",
        "facts": facts,
    }


def write_inputs(tmp_path: Path, *, fetched=FETCHED, extra_bars_after_as_of=0):
    symbols = {n: record(i) for i, n in enumerate(NAMES)}
    symbols["PIN"] = {"status": "no_cik"}
    snapshot = stamp(
        {
            "schema_version": 1,
            "kind": "fundamentals_snapshot",
            "as_of": AS_OF.isoformat(),
            "fetched_at": fetched.isoformat(),
            "coverage": {"symbols": len(symbols)},
            "symbols": symbols,
        }
    )
    (tmp_path / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    rows = []
    for i, name in enumerate(NAMES):
        for t in range(BARS + extra_bars_after_as_of):
            day = AS_OF - timedelta(days=BARS - 1 - t)
            close = 100 + (1 + i) * t * 0.05
            rows.append(f"{name},{day.isoformat()},{close},{close},{close},{close},{1000 * 1000},{close},30\n")
    (tmp_path / "prices.csv").write_text(HEADER + "".join(rows), encoding="utf-8")
    config = {"active": ["AAA", "AAB", "AAC", "AAD", "PIN"], "excluded": ["ZZZ"], "pinned": ["PIN"]}
    (tmp_path / "tickers.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "exchanges.json").write_text(json.dumps({n: "Nasdaq" for n in NAMES}), encoding="utf-8")
    return tmp_path


def args(tmp_path: Path, *extra, size="5", floors=tuple(SMALL_FLOORS)):
    return [
        "--snapshot",
        str(tmp_path / "snapshot.json"),
        "--prices",
        str(tmp_path / "prices.csv"),
        "--tickers-config",
        str(tmp_path / "tickers.json"),
        "--exchanges",
        str(tmp_path / "exchanges.json"),
        "--as-of",
        AS_OF.isoformat(),
        "--out-dir",
        str(tmp_path / "out"),
        "--size",
        size,
        "--generated-at",
        "2026-06-30T13:00:00+00:00",
        *floors,
        *extra,
    ]


def run(tmp_path, capsys, *extra, **kwargs):
    code = main(args(tmp_path, *extra, **kwargs))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the happy path ---------------------------------------------------------------------------------------------


def test_a_passing_run_exits_zero_writes_the_two_files_and_prints_a_summary(tmp_path, capsys):
    write_inputs(tmp_path)
    code, out, _ = run(tmp_path, capsys)
    assert code == 0
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["status"] == "pending_approval" and summary["failures"] == []
    assert Path(summary["proposal"]).name == "universe_proposal.json" and Path(summary["report"]).exists()
    proposal = json.loads(Path(summary["proposal"]).read_text(encoding="utf-8"))
    assert len(proposal["active"]) == 5 and "PIN" in proposal["active"]
    assert proposal["pinned"] == ["PIN"] and set(proposal["active"]) <= set(NAMES) | {"PIN"}


def test_the_tickers_config_is_never_written_and_only_the_out_dir_gets_files(tmp_path, capsys):
    write_inputs(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    run(tmp_path, capsys)
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != "out"}
    assert after == before  # every input file, tickers.json included, is byte-identical
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["ranking_report.json", "universe_proposal.json"]


def proposal_hash(stdout: str) -> str:
    summary = json.loads(stdout.strip().splitlines()[-1])
    return json.loads(Path(summary["proposal"]).read_text(encoding="utf-8"))["content_sha256"]


def test_bars_dated_after_the_as_of_date_change_nothing(tmp_path, capsys):
    write_inputs(tmp_path)
    _, first, _ = run(tmp_path, capsys)
    write_inputs(tmp_path, extra_bars_after_as_of=5)
    _, second, _ = run(tmp_path, capsys)
    assert proposal_hash(first) == proposal_hash(second)


def test_the_same_inputs_give_the_same_proposal_hash(tmp_path, capsys):
    write_inputs(tmp_path)
    _, first, _ = run(tmp_path, capsys)
    _, second, _ = run(tmp_path, capsys)
    assert first == second


# --- a rejected proposal ---------------------------------------------------------------------------------------------


def test_a_rejected_proposal_exits_one_but_still_writes_the_files_and_names_the_failure(tmp_path, capsys):
    write_inputs(tmp_path, fetched=FETCHED - timedelta(days=10))
    code, out, _ = run(tmp_path, capsys)
    summary = json.loads(out.strip().splitlines()[-1])
    assert code == 1 and summary["status"] == "rejected" and "stale_edgar" in summary["failures"]
    assert Path(summary["proposal"]).exists() and Path(summary["report"]).exists()


def test_the_adr_floors_by_default_show_who_would_drop_out(tmp_path, capsys):
    write_inputs(tmp_path)
    code, out, _ = run(tmp_path, capsys, floors=())  # no floor options: the ADR defaults apply
    report = json.loads(Path(json.loads(out.strip().splitlines()[-1])["report"]).read_text(encoding="utf-8"))
    assert code == 1  # nothing passes a $2B market-cap floor with synthetic prices
    assert set(report["ineligible"]) == set(NAMES)
    assert set(report["floor_failures_among_current"]) == {"AAA", "AAB", "AAC", "AAD"}


def test_a_prior_pool_size_far_larger_than_this_pool_rejects(tmp_path, capsys):
    write_inputs(tmp_path)
    code, out, _ = run(tmp_path, capsys, "--prior-pool-size", "1000")
    assert code == 1 and "pool_coverage_drop" in json.loads(out.strip().splitlines()[-1])["failures"]


# --- bad input -------------------------------------------------------------------------------------------


def corrupt(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: (p / "snapshot.json").unlink(), id="snapshot_missing"),
        pytest.param(lambda p: corrupt(p, "snapshot.json", "{not json"), id="snapshot_not_json"),
        pytest.param(lambda p: corrupt(p, "tickers.json", '{"excluded": []}'), id="tickers_config_has_no_active"),
        pytest.param(
            lambda p: corrupt(p, "tickers.json", '{"active": ["AAA"], "pinned": ["ZZZ"]}'), id="pinned_not_in_active"
        ),
        pytest.param(lambda p: corrupt(p, "exchanges.json", '["not", "an", "object"]'), id="exchanges_not_an_object"),
        pytest.param(lambda p: corrupt(p, "prices.csv", "ticker,date\nAAA,2026-06-30\n"), id="prices_missing_columns"),
    ],
)
def test_bad_input_exits_two_with_a_one_line_message_and_no_traceback(tmp_path, capsys, mutate):
    write_inputs(tmp_path)
    mutate(tmp_path)
    code, out, err = run(tmp_path, capsys)
    assert code == 2 and out == ""
    assert err.strip() and "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert not (tmp_path / "out").exists()


def test_a_tampered_snapshot_is_refused(tmp_path, capsys):
    write_inputs(tmp_path)
    document = json.loads((tmp_path / "snapshot.json").read_text(encoding="utf-8"))
    document["symbols"]["AAA"]["facts"]["us-gaap:Assets"]["val"] = 1
    (tmp_path / "snapshot.json").write_text(json.dumps(document), encoding="utf-8")
    code, _, err = run(tmp_path, capsys)
    assert code == 2 and "hash" in err


@pytest.mark.parametrize("flag, value", [("--as-of", "not-a-date"), ("--size", "0"), ("--generated-at", "yesterday")])
def test_a_bad_option_value_exits_two(tmp_path, capsys, flag, value):
    write_inputs(tmp_path)
    arguments = args(tmp_path)
    arguments[arguments.index(flag) + 1] = value
    assert main(arguments) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_a_size_smaller_than_the_pinned_list_exits_two(tmp_path, capsys):
    write_inputs(tmp_path)
    code, _, err = run(tmp_path, capsys, size="1")  # one pinned name leaves no ranked seat
    assert code == 2 and "size" in err
