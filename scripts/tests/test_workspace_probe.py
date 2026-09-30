"""Tests for scripts/workspace_probe.py: does a scheduled cloud run keep its files between fires?

Written first, from the behaviour the plan specifies, before any implementation.
The cloud-clone plan hinges on one unknown: whether the Perplexity workspace
keeps ``cron_tracking`` from one scheduled fire to the next. If it does, the
same-day handoff already works; if not, the evidence store is essential. This
probe answers that with evidence and nothing else: each run writes one marker
file and lists the markers earlier runs left behind. It never touches a broker,
an order path, the network or the account number.

Contract under test (exit 0 = ok, exit 2 = refused; stdout ends with one JSON
object):

    main(["--root", ROOT, ["--now", "YYYY-MM-DDTHH:MM:SSZ"]]) -> int
      -> {"root": str, "marker": "workspace_probe/<stamp>[-N].json",
          "written_at_utc": str, "prior_count": int,
          "prior_markers": [{"name": str, "written_at_utc": str | None,
                             "age_seconds": int | None, "readable": bool}]}

Markers are ``<UTC stamp>.json`` files under ``<root>/workspace_probe/``, each
holding ``{"written_at_utc": ...}``. A second marker written in the same second
gets a ``-1``, ``-2`` ... suffix instead of overwriting. Prior markers are
listed oldest first, and age is measured from the time recorded inside each
marker, never from file modification time (a copied or restored file keeps it).
Without ``--root`` the root is ``$DCA_CRON_TRACKING_ROOT``.
"""

import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import workspace_probe  # noqa: E402

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
START = datetime(2026, 9, 30, 9, 40, 0, tzinfo=timezone.utc)
PROBE_DIR = "workspace_probe"


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(moment):
    # Built at runtime so no digit-run literal lands in the tree (the repo's
    # account-literal invariant scans tracked files for them).
    return moment.strftime("%Y%m%dT%H%M%SZ")


def probe(root, moment=START, *extra):
    return workspace_probe.main(["--root", str(root), "--now", iso(moment), *extra])


def output(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def markers(root):
    return sorted(path.name for path in (Path(root) / PROBE_DIR).iterdir())


def test_first_run_creates_the_marker_and_reports_no_prior_markers(tmp_path, capsys):
    assert probe(tmp_path) == 0

    result = output(capsys)
    assert result["prior_count"] == 0 and result["prior_markers"] == []
    assert result["written_at_utc"] == iso(START)
    assert result["marker"] == f"{PROBE_DIR}/{stamp(START)}.json"
    written = json.loads((tmp_path / result["marker"]).read_text(encoding="utf-8"))
    assert written["written_at_utc"] == iso(START)


def test_a_later_run_lists_the_earlier_marker_with_its_age(tmp_path, capsys):
    probe(tmp_path, START)
    capsys.readouterr()

    assert probe(tmp_path, START + timedelta(hours=1)) == 0

    result = output(capsys)
    assert result["prior_count"] == 1
    assert result["prior_markers"] == [
        {"name": f"{stamp(START)}.json", "written_at_utc": iso(START), "age_seconds": 3600, "readable": True}
    ]


def test_several_prior_markers_are_listed_oldest_first(tmp_path, capsys):
    moments = [START, START + timedelta(minutes=30), START + timedelta(minutes=90)]
    for moment in moments:
        probe(tmp_path, moment)
    capsys.readouterr()

    probe(tmp_path, START + timedelta(hours=2))

    result = output(capsys)
    assert [entry["name"] for entry in result["prior_markers"]] == [f"{stamp(m)}.json" for m in moments]
    assert [entry["age_seconds"] for entry in result["prior_markers"]] == [7200, 5400, 1800]


def test_two_runs_in_the_same_second_do_not_overwrite_each_other(tmp_path, capsys):
    probe(tmp_path, START)
    first = (tmp_path / PROBE_DIR / f"{stamp(START)}.json").read_bytes()
    capsys.readouterr()

    assert probe(tmp_path, START) == 0

    result = output(capsys)
    assert result["marker"] == f"{PROBE_DIR}/{stamp(START)}-1.json"
    assert (tmp_path / PROBE_DIR / f"{stamp(START)}.json").read_bytes() == first
    assert result["prior_markers"][0]["age_seconds"] == 0


def test_an_unreadable_marker_is_reported_and_does_not_abort_the_probe(tmp_path, capsys):
    (tmp_path / PROBE_DIR).mkdir()
    (tmp_path / PROBE_DIR / "garbage.json").write_text("not json", encoding="utf-8")

    assert probe(tmp_path) == 0

    result = output(capsys)
    assert result["prior_count"] == 1
    assert result["prior_markers"] == [
        {"name": "garbage.json", "written_at_utc": None, "age_seconds": None, "readable": False}
    ]


def test_only_json_markers_are_listed_and_no_temp_files_are_left_behind(tmp_path, capsys):
    (tmp_path / PROBE_DIR).mkdir()
    (tmp_path / PROBE_DIR / "notes.txt").write_text("not a marker", encoding="utf-8")

    probe(tmp_path)

    assert output(capsys)["prior_count"] == 0
    assert markers(tmp_path) == [f"{stamp(START)}.json", "notes.txt"]


def test_refuses_a_root_that_is_an_existing_file(tmp_path, capsys):
    not_a_directory = tmp_path / "a-file"
    not_a_directory.write_text("x", encoding="utf-8")

    assert probe(not_a_directory) == 2

    assert "error" in output(capsys)
    assert not_a_directory.read_text(encoding="utf-8") == "x"


def test_refuses_the_filesystem_root(capsys):
    assert probe("/") == 2

    assert "error" in output(capsys)


def test_the_root_defaults_to_the_cron_tracking_environment_variable(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))

    assert workspace_probe.main(["--now", iso(START)]) == 0

    assert (tmp_path / PROBE_DIR / f"{stamp(START)}.json").is_file()
    assert output(capsys)["root"] == str(tmp_path)


def test_an_unparseable_now_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as raised:
        workspace_probe.main(["--root", str(tmp_path), "--now", "yesterday"])

    assert raised.value.code == 2


def test_output_is_one_json_object_and_never_contains_the_account_number(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", ACCOUNT)

    probe(tmp_path)

    captured = capsys.readouterr()
    assert len(captured.out.strip().splitlines()) == 1
    assert ACCOUNT not in captured.out and ACCOUNT not in captured.err
    assert ACCOUNT not in "".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / PROBE_DIR).iterdir()
    )


def test_the_module_has_no_network_broker_or_subprocess_dependency():
    source = inspect.getsource(workspace_probe)

    for banned in ("import socket", "import urllib", "import http", "import subprocess", "import requests",
                   "dca_live_adapters", "near_close_dca", "place_equity_order"):
        assert banned not in source, banned
