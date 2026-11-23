"""Tests for the not-yet-written market_data_ingest/build_fundamentals_snapshot.py (the Mac-side fetch job).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

This job chains the feed pieces into the two files ``ticker-advisor``'s ``daily_ranking`` reads: the SEC ticker
and exchange files, the bulk ``companyfacts.zip``, one SIC lookup per company that has facts, then the snapshot and
the exchange map. It is meant to run on the operator's Mac (it makes the network calls, with the operator's
``SEC_EDGAR_USER_AGENT``); here every network function is INJECTED, so these tests use fakes and a small real zip.

Rules pinned here, from the scope document and the repo's fail-closed rule:

* the pool is ``tickers.txt`` plus the tickers config's ``active``, ``excluded`` and ``whole_share`` lists;
* a SIC lookup is made only for companies that have facts, is rate-limited, is cached on disk (a SIC rarely
  changes) with a maximum age, and a transient network error is retried a few times before the run ABORTS
  (an error is never recorded as "no SIC");
* any failure leaves no half-written output, and the outputs are written atomically;
* nothing is written unless the SEC data and the User-Agent are all in hand.

The fixtures are SYNTHETIC. No run of 6 or more digits appears below (the account-number guard).
"""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from market_data_ingest.build_fundamentals_snapshot import (
    BuildError,
    Deps,
    load_pool_symbols,
    main,
    run_build,
)
from market_data_ingest.fundamentals_snapshot import snapshot_hash_matches
from market_data_ingest.sec_company_map import CompanyRef, SicInfo

AS_OF = date(2026, 6, 30)
NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
UA = "Acme Research ops@acme.example"


def cik(n: int) -> str:
    return str(n).zfill(10)


def entry(val, end="2025-12-31", filed="2026-02-11", start=None):
    return {
        "start": start,
        "end": end,
        "val": val,
        "fy": 2025,
        "fp": "FY",
        "form": "10-K",
        "filed": filed,
        "frame": None,
        "accn": "A-1",
    }


def company_zip(members: dict[int, dict | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for n, body in members.items():
            archive.writestr(f"CIK{cik(n)}.json", body if isinstance(body, str) else json.dumps(body))
    return buffer.getvalue()


def facts_doc(assets=5000):
    return {"facts": {"us-gaap": {"Assets": {"units": {"USD": [entry(assets)]}}}}}


ZIP_BYTES = company_zip({1: facts_doc(), 2: facts_doc(7000), 4: "{ not json"})  # CIK 3 has no member
COMPANIES = {
    "AAA": CompanyRef(cik=cik(1), name="Alpha"),
    "BBB": CompanyRef(cik=cik(2), name="Bravo"),
    "CCC": CompanyRef(cik=cik(3), name="Charlie"),  # in the SEC map, no companyfacts member
    "DDD": CompanyRef(cik=cik(4), name="Delta"),  # member unreadable
}
EXCHANGES = {"AAA": "Nasdaq", "BBB": "NYSE", "CCC": "Nasdaq", "DDD": "OTC"}
SICS = {
    cik(1): SicInfo(sic="3571", description="Electronic Computers", sector="35", industry="3571"),
    cik(2): None,  # a company with no SIC
}


@dataclass
class Fakes:
    sic_calls: list = field(default_factory=list)
    sleeps: list = field(default_factory=list)
    downloads: int = 0
    sic_failures: dict = field(default_factory=dict)  # cik -> number of OSErrors to raise first
    download_error: Exception | None = None

    def deps(self) -> Deps:
        def download_zip(dest, *, user_agent=None):
            self.downloads += 1
            if self.download_error:
                raise self.download_error
            Path(dest).write_bytes(ZIP_BYTES)
            return len(ZIP_BYTES)

        def fetch_sic(cik_, *, user_agent=None):
            self.sic_calls.append(cik_)
            left = self.sic_failures.get(cik_, 0)
            if left:
                self.sic_failures[cik_] = left - 1
                raise urllib.error.URLError("temporary")
            return SICS.get(cik_)

        return Deps(
            fetch_company_tickers=lambda *, user_agent=None: dict(COMPANIES),
            fetch_company_exchanges=lambda *, user_agent=None: dict(EXCHANGES),
            download_zip=download_zip,
            fetch_sic=fetch_sic,
            sleep=self.sleeps.append,
            now=lambda: NOW,
        )


def build(tmp_path, fakes=None, **overrides):
    fakes = fakes or Fakes()
    kwargs = dict(
        symbols=["AAA", "BBB", "CCC", "DDD", "EEE"],
        as_of=AS_OF,
        work_dir=tmp_path / "work",
        out_dir=tmp_path / "out",
        user_agent=UA,
        deps=fakes.deps(),
    )
    kwargs.update(overrides)
    return run_build(**kwargs), fakes


# --- the pool ----------------------------------------------------------------------------------------------------


def test_the_pool_is_tickers_txt_plus_the_config_lists_deduped_and_sorted(tmp_path):
    (tmp_path / "tickers.txt").write_text("BBB\nAAA\n\nAAA\n", encoding="utf-8")
    config = {"active": ["CCC", "AAA"], "excluded": ["DDD"], "whole_share": ["EEE"], "pinned": ["CCC"]}
    (tmp_path / "tickers.json").write_text(json.dumps(config), encoding="utf-8")
    assert load_pool_symbols(tmp_path / "tickers.txt", tmp_path / "tickers.json") == ["AAA", "BBB", "CCC", "DDD", "EEE"]


@pytest.mark.parametrize("bad", ["aaa\n", "TOO-LONG-SYMBOL\n", "A B\n", "AAA1\n"])
def test_a_symbol_that_is_not_canonical_is_refused_and_named(tmp_path, bad):
    (tmp_path / "tickers.txt").write_text(bad, encoding="utf-8")
    (tmp_path / "tickers.json").write_text('{"active": ["AAA"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="symbol"):
        load_pool_symbols(tmp_path / "tickers.txt", tmp_path / "tickers.json")


# --- a complete run --------------------------------------------------------------------------------------


def test_a_run_writes_a_valid_snapshot_and_the_exchange_map(tmp_path):
    summary, _ = build(tmp_path)
    snapshot = json.loads(summary.snapshot_path.read_text(encoding="utf-8"))
    assert summary.snapshot_path.name == "fundamentals_snapshot_2026-06-30.json"
    assert summary.exchanges_path.name == "exchanges_2026-06-30.json"
    assert snapshot_hash_matches(snapshot) and snapshot["fetched_at"] == NOW.isoformat()
    statuses = {s: r["status"] for s, r in snapshot["symbols"].items()}
    assert statuses == {"AAA": "ok", "BBB": "ok", "CCC": "no_companyfacts", "DDD": "unreadable", "EEE": "no_cik"}
    assert snapshot["symbols"]["AAA"]["sector"] == "35" and snapshot["symbols"]["BBB"]["sector"] is None
    assert json.loads(summary.exchanges_path.read_text(encoding="utf-8")) == EXCHANGES
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(
        [summary.snapshot_path.name, summary.exchanges_path.name]
    )


def test_the_summary_reports_coverage_and_what_could_not_be_read(tmp_path):
    summary, _ = build(tmp_path)
    assert summary.coverage["symbols"] == 5 and summary.coverage["ok"] == 2
    assert (
        summary.unreadable == [cik(4)]
        and summary.snapshot_sha256 == json.loads(summary.snapshot_path.read_text(encoding="utf-8"))["content_sha256"]
    )


def test_a_sic_lookup_is_made_only_for_companies_that_have_facts(tmp_path):
    _, fakes = build(tmp_path)
    assert sorted(fakes.sic_calls) == [cik(1), cik(2)]  # not CIK 3 (no member), not CIK 4 (unreadable)


def test_sic_lookups_are_rate_limited(tmp_path):
    _, fakes = build(tmp_path, min_interval_s=0.2)
    assert fakes.sleeps and all(s >= 0.2 for s in fakes.sleeps)


# --- the SIC cache ---------------------------------------------------------------------------------------


def test_a_second_run_reads_sic_from_the_cache_instead_of_the_network(tmp_path):
    first, fakes_one = build(tmp_path)
    second, fakes_two = build(tmp_path)
    assert fakes_one.sic_calls and fakes_two.sic_calls == []
    assert json.loads(second.snapshot_path.read_text(encoding="utf-8"))["symbols"]["AAA"]["sector"] == "35"
    assert first.snapshot_sha256 == second.snapshot_sha256


def test_a_stale_cache_entry_is_fetched_again(tmp_path):
    build(tmp_path)
    cache = tmp_path / "work" / "sic_cache.json"
    document = json.loads(cache.read_text(encoding="utf-8"))
    old = (NOW - timedelta(days=200)).isoformat()
    for record in document["entries"].values():
        record["fetched_at"] = old
    cache.write_text(json.dumps(document), encoding="utf-8")
    _, fakes = build(tmp_path, sic_cache_max_age_days=90)
    assert sorted(fakes.sic_calls) == [cik(1), cik(2)]


def test_a_company_with_no_sic_is_cached_as_none_not_refetched(tmp_path):
    build(tmp_path)
    _, fakes = build(tmp_path)
    assert cik(2) not in fakes.sic_calls


# --- retries and aborting --------------------------------------------------------------------------------


def test_a_transient_sic_error_is_retried_and_then_succeeds(tmp_path):
    fakes = Fakes(sic_failures={cik(1): 2})
    summary, fakes = build(tmp_path, fakes, retries=3)
    assert fakes.sic_calls.count(cik(1)) == 3
    assert json.loads(summary.snapshot_path.read_text(encoding="utf-8"))["symbols"]["AAA"]["sector"] == "35"


def test_a_persistent_sic_error_aborts_the_run_and_writes_nothing(tmp_path):
    fakes = Fakes(sic_failures={cik(1): 99})
    with pytest.raises(BuildError, match="SIC"):
        build(tmp_path, fakes, retries=3)
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []


def test_an_error_is_never_recorded_as_no_sic(tmp_path):
    fakes = Fakes(sic_failures={cik(2): 99})
    with pytest.raises(BuildError):
        build(tmp_path, fakes, retries=2)
    assert (
        not (tmp_path / "work" / "sic_cache.json").exists()
        or cik(2) not in json.loads((tmp_path / "work" / "sic_cache.json").read_text(encoding="utf-8"))["entries"]
    )


def test_a_failed_download_aborts_the_run_and_writes_nothing(tmp_path):
    fakes = Fakes(download_error=urllib.error.URLError("unreachable"))
    with pytest.raises(BuildError, match="download"):
        build(tmp_path, fakes)
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []


def test_a_given_zip_skips_the_download(tmp_path):
    zip_path = tmp_path / "given.zip"
    zip_path.write_bytes(ZIP_BYTES)
    _, fakes = build(tmp_path, zip_path=zip_path)
    assert fakes.downloads == 0


def test_a_missing_user_agent_is_refused_before_any_request(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    fakes = Fakes()
    with pytest.raises(BuildError, match="User-Agent"):
        build(tmp_path, fakes, user_agent=None)
    assert fakes.downloads == 0 and fakes.sic_calls == []
    assert not (tmp_path / "out").exists()


# --- the command line ------------------------------------------------------------------------------------


def cli_args(tmp_path, *extra):
    (tmp_path / "tickers.txt").write_text("AAA\nBBB\nCCC\nDDD\nEEE\n", encoding="utf-8")
    (tmp_path / "tickers.json").write_text('{"active": ["AAA"]}', encoding="utf-8")
    return [
        "--tickers-txt",
        str(tmp_path / "tickers.txt"),
        "--tickers-config",
        str(tmp_path / "tickers.json"),
        "--as-of",
        AS_OF.isoformat(),
        "--work-dir",
        str(tmp_path / "work"),
        "--out-dir",
        str(tmp_path / "out"),
        "--user-agent",
        UA,
        *extra,
    ]


def test_the_command_line_runs_and_prints_a_summary(tmp_path, capsys):
    assert main(cli_args(tmp_path), deps=Fakes().deps()) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["coverage"]["symbols"] == 5 and Path(summary["snapshot"]).exists()


def test_the_command_line_exits_two_with_one_line_on_a_failure(tmp_path, capsys):
    fakes = Fakes(download_error=urllib.error.URLError("unreachable"))
    assert main(cli_args(tmp_path), deps=fakes.deps()) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "Traceback" not in captured.err and len(captured.err.strip().splitlines()) == 1


def test_the_command_line_refuses_a_bad_as_of(tmp_path, capsys):
    arguments = cli_args(tmp_path)
    arguments[arguments.index("--as-of") + 1] = "not-a-date"
    assert main(arguments, deps=Fakes().deps()) == 2
