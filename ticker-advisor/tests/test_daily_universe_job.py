"""Tests for the not-yet-written ``ticker-advisor/daily_universe_job.py`` (step 7b-2, the Mac daily job).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

The job is the thin glue the scheduler runs once per weekday evening on the operator's Mac. It chains the two
pieces already built and merged: ``daily_ranking`` (propose-only, step 7b-1) and ``universe_apply.apply_proposal``
(the gated writer), under the standing approval in docs/adr-program-maintained-universe.md ("auto-apply when every
safeguard passes").

Assumption the operator has not confirmed (ADR open question 9, my recommendation): the job writes the live list
into THIS checkout's ``config/tickers.json`` and ``ticker_history.csv`` and commits nothing. Everything below is a
file-path argument, so a different answer changes the scheduler's arguments, not this contract.

Contract:

* Options are ``daily_ranking``'s, plus ``--history FILE`` (the ``ticker_history.csv`` to append to) and ``--apply``.
  ``--as-of`` is optional here: omitted, it is the latest bar date in the price archive for the snapshot's symbols
  (a scheduler cannot compute "the last completed session").
* WITHOUT ``--apply`` it is exactly ``daily_ranking``: outputs in ``--out-dir``, no other file written.
* WITH ``--apply`` and a proposal that passed every safeguard, it calls the gated writer with an approval id naming
  the standing ADR and the proposal hash prefix, ``auto:adr-program-maintained-universe:<first 12 hex of the
  hash>``. The list changes; pinned names stay; only ``active`` changes in ``tickers.json``.
* Exit codes: 0 passed (applied, or nothing to change, or proposed without ``--apply``); 1 the proposal was REJECTED
  by a safeguard (nothing written to the live files); 2 bad input; 3 the writer REFUSED a passing proposal
  (nothing written to the live files; the writer's reason code is on stderr).
* A proposal with no adds and no drops is "unchanged": the writer is not called and no history rows are appended,
  so a same-day rerun never duplicates history.
* It is a BUY-list job only. ``stopped_buying`` names are reported and are NOT sales; the module imports no broker,
  order, runner or network code.

The last stdout line is one JSON summary. All data is SYNTHETIC (no run of 6 or more digits).
"""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_daily_ranking_cli import AS_OF, FETCHED, SMALL_FLOORS, write_inputs

import universe_apply
from daily_universe_job import main

APPROVAL_PREFIX = "auto:adr-program-maintained-universe:"


@pytest.fixture(autouse=True)
def small_active_bounds(monkeypatch):
    """The fixture list has 5 names; the writer's real floor of 90 is exercised by its own test below."""
    monkeypatch.setattr(universe_apply, "MIN_ACTIVE", 5)


def job_args(tmp_path: Path, *extra, size="5", with_as_of=True):
    base = [
        "--snapshot", str(tmp_path / "snapshot.json"),
        "--prices", str(tmp_path / "prices.csv"),
        "--tickers-config", str(tmp_path / "tickers.json"),
        "--exchanges", str(tmp_path / "exchanges.json"),
        "--out-dir", str(tmp_path / "out"),
        "--history", str(tmp_path / "ticker_history.csv"),
        "--size", size,
        "--generated-at", "2026-06-30T13:00:00+00:00",
        *SMALL_FLOORS,
    ]
    if with_as_of:
        base += ["--as-of", AS_OF.isoformat()]
    return [*base, *extra]


def run(tmp_path, capsys, *extra, **kwargs):
    code = main(job_args(tmp_path, *extra, **kwargs))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def summary_of(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


def live_files(tmp_path: Path) -> dict[str, bytes]:
    return {name: (tmp_path / name).read_bytes()
            for name in ("tickers.json", "ticker_history.csv") if (tmp_path / name).exists()}


@pytest.fixture
def inputs(tmp_path):
    write_inputs(tmp_path)
    (tmp_path / "ticker_history.csv").write_text("date,ticker\n", encoding="utf-8")
    return tmp_path


# --- without --apply it is propose-only ---------------------------------------------------------------------------


def test_without_apply_no_live_file_is_written(inputs, capsys):
    before = live_files(inputs)
    code, out, _ = run(inputs, capsys)
    assert code == 0 and live_files(inputs) == before
    assert summary_of(out)["status"] == "pending_approval"
    assert sorted(p.name for p in (inputs / "out").iterdir()) == ["ranking_report.json", "universe_proposal.json"]


# --- with --apply -----------------------------------------------------------------------------------------------


def test_apply_rewrites_only_the_active_array_and_keeps_pinned_names(inputs, capsys):
    before = json.loads((inputs / "tickers.json").read_text(encoding="utf-8"))
    code, out, _ = run(inputs, capsys, "--apply")
    after = json.loads((inputs / "tickers.json").read_text(encoding="utf-8"))
    proposal = json.loads((inputs / "out" / "universe_proposal.json").read_text(encoding="utf-8"))
    assert code == 0 and after["active"] == proposal["active"] and "PIN" in after["active"]
    assert {k: v for k, v in after.items() if k != "active"} == {k: v for k, v in before.items() if k != "active"}
    assert summary_of(out)["status"] == "applied"


def test_apply_appends_the_new_list_to_history_under_the_as_of_date(inputs, capsys):
    run(inputs, capsys, "--apply")
    rows = (inputs / "ticker_history.csv").read_text(encoding="utf-8").splitlines()
    active = json.loads((inputs / "tickers.json").read_text(encoding="utf-8"))["active"]
    assert rows[0] == "date,ticker" and rows[1:] == [f"{AS_OF.isoformat()},{t}" for t in active]


def test_the_approval_id_names_the_standing_adr_and_the_proposal_hash_prefix(inputs, capsys):
    _, out, _ = run(inputs, capsys, "--apply")
    proposal = json.loads((inputs / "out" / "universe_proposal.json").read_text(encoding="utf-8"))
    assert summary_of(out)["approval_id"] == APPROVAL_PREFIX + proposal["content_sha256"][:12]


def test_the_summary_names_added_and_stopped_buying_and_stopped_buying_is_not_a_sale(inputs, capsys):
    _, out, _ = run(inputs, capsys, "--apply")
    summary = summary_of(out)
    proposal = json.loads((inputs / "out" / "universe_proposal.json").read_text(encoding="utf-8"))
    assert summary["added"] == sorted(set(proposal["active"]) - set(proposal["prior_active"]))
    assert summary["stopped_buying"] == sorted(set(proposal["prior_active"]) - set(proposal["active"]))
    assert {"AAG", "AAH"} <= set(summary["added"])  # the top-ranked newcomers enter; nothing is sold


def test_a_same_day_rerun_is_unchanged_and_does_not_duplicate_history(inputs, capsys):
    run(inputs, capsys, "--apply")
    first = live_files(inputs)
    code, out, _ = run(inputs, capsys, "--apply")
    assert code == 0 and summary_of(out)["status"] == "unchanged"
    assert live_files(inputs) == first


# --- the failure paths leave the live files exactly as they were ------------------------------------------------


def test_a_rejected_proposal_exits_one_and_writes_nothing_to_the_live_files(tmp_path, capsys):
    write_inputs(tmp_path, fetched=FETCHED - timedelta(days=10))
    (tmp_path / "ticker_history.csv").write_text("date,ticker\n", encoding="utf-8")
    before = live_files(tmp_path)
    code, out, _ = run(tmp_path, capsys, "--apply")
    assert code == 1 and live_files(tmp_path) == before
    assert summary_of(out)["status"] == "rejected" and "stale_edgar" in summary_of(out)["failures"]


def test_bad_input_exits_two_with_one_stderr_line_and_writes_nothing(inputs, capsys):
    (inputs / "snapshot.json").write_text("{oops", encoding="utf-8")
    before = live_files(inputs)
    code, _, err = run(inputs, capsys, "--apply")
    assert code == 2 and live_files(inputs) == before
    assert err.startswith("error: ") and len(err.strip().splitlines()) == 1


def test_a_proposal_the_writer_refuses_exits_three_and_names_the_reason(inputs, capsys, monkeypatch):
    monkeypatch.setattr(universe_apply, "MIN_ACTIVE", 90)  # the real floor: 5 names is below it
    before = live_files(inputs)
    code, _, err = run(inputs, capsys, "--apply")
    assert code == 3 and live_files(inputs) == before
    assert "below_min_active" in err


def test_apply_without_a_history_path_is_a_usage_error(inputs):
    argv = [a for a in job_args(inputs, "--apply") if a not in ("--history", str(inputs / "ticker_history.csv"))]
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2


# --- the as-of default --------------------------------------------------------------------------------------------


def test_an_omitted_as_of_is_the_latest_bar_date_in_the_archive(inputs, capsys):
    code, out, _ = run(inputs, capsys, with_as_of=False)
    proposal = json.loads((inputs / "out" / "universe_proposal.json").read_text(encoding="utf-8"))
    assert code == 0 and proposal["as_of_date"] == AS_OF.isoformat()
    assert summary_of(out)["status"] == "pending_approval"


# --- boundaries -------------------------------------------------------------------------------------------------


def imported_modules() -> set[str]:
    source = (Path(__file__).resolve().parent.parent / "daily_universe_job.py").read_text(encoding="utf-8")
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_the_job_imports_no_broker_order_runner_or_network_code():
    forbidden = {"socket", "urllib", "http", "requests", "dca_runner", "near_close_dca", "dca_live_adapters",
                 "backup_planner", "dca_order_plan"}
    assert not imported_modules() & forbidden
