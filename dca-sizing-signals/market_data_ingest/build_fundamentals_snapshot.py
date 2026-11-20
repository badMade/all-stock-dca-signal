"""The Mac-side fetch job: SEC files in, the snapshot and exchange map out.

Specified first by tests/test_build_fundamentals_snapshot.py (docs/fundamentals_feed_scope.md). It chains the feed
pieces into the two files ``ticker-advisor``'s ``daily_ranking`` reads: the SEC ticker and exchange files, the bulk
``companyfacts.zip``, one SIC lookup per company that has facts, then ``fundamentals_snapshot_<date>.json`` and
``exchanges_<date>.json``. It is meant to run on the operator's Mac with their ``SEC_EDGAR_USER_AGENT``. Every
network function is injected through :class:`Deps` (the real ones are the defaults), so the tests use fakes.

Rules, from the scope document and the repo's fail-closed rule:

* the pool is ``tickers.txt`` plus the tickers config's ``active``, ``excluded`` and ``whole_share`` lists;
* a SIC lookup is made only for companies that have facts, is rate-limited, and is cached on disk (a SIC rarely
  changes) with a maximum age. A transient network error is retried before the run ABORTS; an error is never
  recorded as "no SIC". Entries fetched before an abort stay in the cache, so a rerun resumes;
* outputs are written atomically and only after every network step has succeeded, so a failure leaves none;
* nothing is requested without a contact-identifying User-Agent.

Research ingest only: nothing on the live DCA side imports this (tests/test_architecture_boundaries.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import sec_company_map
from .edgar_bulk import download_companyfacts_zip, read_bulk_companyfacts
from .edgar_fundamentals import USER_AGENT_ENV
from .fundamentals_snapshot import build_snapshot, write_snapshot
from .sec_company_map import SicInfo

SYMBOL_PATTERN = re.compile(r"^[A-Z.]{1,6}$")
SIC_CACHE_NAME = "sic_cache.json"
_SIC_CACHE_VERSION = 1
CONFIG_LISTS = ("active", "excluded", "whole_share")


class BuildError(Exception):
    """A run problem with a one-line, body-free message."""


@dataclass
class Deps:
    """The network and clock functions this job uses; the defaults are the real ones."""

    fetch_company_tickers: Callable[..., Any] = sec_company_map.fetch_company_tickers
    fetch_company_exchanges: Callable[..., Any] = sec_company_map.fetch_company_exchanges
    download_zip: Callable[..., Any] = download_companyfacts_zip
    fetch_sic: Callable[..., Any] = sec_company_map.fetch_sic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731


@dataclass(frozen=True)
class BuildSummary:
    coverage: dict[str, int]
    snapshot_path: Path
    exchanges_path: Path
    snapshot_sha256: str
    unreadable: list[str]
    sic_requests: int


def load_pool_symbols(tickers_txt: str | Path, tickers_config: str | Path) -> list[str]:
    """``tickers.txt`` plus the config's ``active``, ``excluded`` and ``whole_share`` lists, deduped and sorted.

    Raises ``ValueError`` naming a symbol that is not canonical (``^[A-Z.]{1,6}$``).
    """
    symbols: set[str] = {line.strip() for line in Path(tickers_txt).read_text(encoding="utf-8").splitlines()}
    symbols.discard("")
    document = json.loads(Path(tickers_config).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("the tickers config is not a JSON object")
    for key in CONFIG_LISTS:
        values = document.get(key) or []
        if not isinstance(values, list):
            raise ValueError(f"the tickers config's {key!r} is not a list")
        symbols.update(v for v in values if isinstance(v, str))
    bad = sorted(s for s in symbols if not SYMBOL_PATTERN.match(s))
    if bad:
        raise ValueError(f"a symbol is not canonical: {bad[0]!r}")
    return sorted(symbols)


def _sic_to_json(sic: SicInfo | None) -> dict[str, str] | None:
    return None if sic is None else {"sic": sic.sic, "description": sic.description, "sector": sic.sector,
                                     "industry": sic.industry}


def _sic_from_json(value: Any) -> SicInfo | None:
    return None if value is None else SicInfo(**value)


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = document["entries"]
        return entries if isinstance(entries, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}  # a missing or unreadable cache is just an empty one


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _fresh(entry: Any, now: datetime, max_age_days: int) -> bool:
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return fetched.tzinfo is not None and now - fetched <= timedelta(days=max_age_days)


def _fetch_one_sic(deps: Deps, cik: str, user_agent: str, retries: int, min_interval_s: float) -> SicInfo | None:
    for attempt in range(1, retries + 1):
        try:
            return deps.fetch_sic(cik, user_agent=user_agent)
        except OSError:
            if attempt == retries:
                raise BuildError(f"the SIC lookup for CIK {cik} failed after {retries} attempts") from None
            deps.sleep(max(min_interval_s, float(attempt)))
    raise AssertionError("unreachable")  # pragma: no cover


def _collect_sics(
    ciks: Iterable[str], cache_path: Path, deps: Deps, user_agent: str, *, max_age_days: int, retries: int,
    min_interval_s: float,
) -> tuple[dict[str, SicInfo | None], int]:
    now = deps.now()
    entries = _load_cache(cache_path)
    sics: dict[str, SicInfo | None] = {}
    requests = 0
    try:
        for cik in sorted(ciks):
            if _fresh(entries.get(cik), now, max_age_days):
                sics[cik] = _sic_from_json(entries[cik].get("sic"))
                continue
            if requests:
                deps.sleep(min_interval_s)
            requests += 1
            sic = _fetch_one_sic(deps, cik, user_agent, retries, min_interval_s)
            sics[cik] = sic
            entries[cik] = {"fetched_at": now.isoformat(), "sic": _sic_to_json(sic)}
    finally:
        # Keep what was fetched even when the run aborts, so a rerun resumes instead of starting over.
        _write_atomically(cache_path, json.dumps({"schema_version": _SIC_CACHE_VERSION, "entries": entries},
                                                 sort_keys=True, indent=2) + "\n")
    return sics, requests


def run_build(
    *,
    symbols: Iterable[str],
    as_of: date,
    work_dir: str | Path,
    out_dir: str | Path,
    user_agent: str | None,
    zip_path: str | Path | None = None,
    deps: Deps | None = None,
    sic_cache_max_age_days: int = 90,
    min_interval_s: float = 0.12,
    retries: int = 3,
) -> BuildSummary:
    """Fetch, read and write. Raises :class:`BuildError` on any failure, leaving no output files."""
    resolved = (user_agent if user_agent is not None else os.environ.get(USER_AGENT_ENV) or "").strip()
    if not resolved:
        raise BuildError(f"a contact-identifying User-Agent is required (--user-agent or {USER_AGENT_ENV})")
    deps = deps or Deps()
    work = Path(work_dir)
    pool = sorted(set(symbols))
    started = deps.now()
    try:
        all_companies = deps.fetch_company_tickers(user_agent=resolved)
        all_exchanges = deps.fetch_company_exchanges(user_agent=resolved)
    except (OSError, LookupError, ValueError):
        raise BuildError("could not fetch the SEC ticker or exchange file") from None
    companies = {s: all_companies[s] for s in pool if s in all_companies}
    exchanges = {s: all_exchanges[s] for s in pool if s in all_exchanges}

    work.mkdir(parents=True, exist_ok=True)
    if zip_path is None:
        zip_file = work / "companyfacts.zip"
        try:
            deps.download_zip(zip_file, user_agent=resolved)
        except (OSError, LookupError, ValueError):
            raise BuildError("the companyfacts download failed") from None
    else:
        zip_file = Path(zip_path)
    try:
        bulk = read_bulk_companyfacts(zip_file, ciks=[c.cik for c in companies.values()])
    except (OSError, ValueError):
        raise BuildError("the companyfacts zip could not be read") from None

    with_facts = [cik for cik, facts in bulk.facts.items() if facts]
    sics, requests = _collect_sics(
        with_facts, work / SIC_CACHE_NAME, deps, resolved, max_age_days=sic_cache_max_age_days, retries=retries,
        min_interval_s=min_interval_s,
    )
    snapshot = build_snapshot(as_of=as_of, fetched_at=started, symbols=pool, companies=companies, sic_by_cik=sics,
                              bulk=bulk)
    out = Path(out_dir)
    snapshot_path = out / f"fundamentals_snapshot_{as_of.isoformat()}.json"
    exchanges_path = out / f"exchanges_{as_of.isoformat()}.json"
    write_snapshot(snapshot_path, snapshot)
    _write_atomically(exchanges_path, json.dumps(exchanges, sort_keys=True, indent=2) + "\n")
    return BuildSummary(
        coverage=snapshot["coverage"],
        snapshot_path=snapshot_path,
        exchanges_path=exchanges_path,
        snapshot_sha256=snapshot["content_sha256"],
        unreadable=sorted(bulk.unreadable),
        sic_requests=requests,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_fundamentals_snapshot",
        description="Fetch SEC data and write the fundamentals snapshot and exchange map for daily_ranking.",
    )
    parser.add_argument("--tickers-txt", required=True, help="tickers.txt (one symbol per line)")
    parser.add_argument("--tickers-config", required=True, help="config/tickers.json (active, excluded, whole_share)")
    parser.add_argument("--as-of", required=True, help="snapshot date, YYYY-MM-DD")
    parser.add_argument("--work-dir", required=True, help="cache directory (the zip and the SIC cache)")
    parser.add_argument("--out-dir", required=True, help="directory for the snapshot and exchange files")
    parser.add_argument("--user-agent", help=f"contact-identifying User-Agent (default: ${USER_AGENT_ENV})")
    parser.add_argument("--zip", help="use this companyfacts.zip instead of downloading one")
    parser.add_argument("--min-interval", type=float, default=0.12, help="seconds between SIC requests")
    parser.add_argument("--sic-cache-max-age-days", type=int, default=90)
    parser.add_argument("--retries", type=int, default=3)
    return parser


def main(argv: list[str] | None = None, *, deps: Deps | None = None) -> int:
    """Command line. Returns 0 on success, 2 on any failure (one line on stderr, nothing on stdout)."""
    options = _build_parser().parse_args(argv)
    try:
        try:
            as_of = date.fromisoformat(options.as_of)
        except ValueError:
            raise BuildError("--as-of must be an ISO date (YYYY-MM-DD)") from None
        summary = run_build(
            symbols=load_pool_symbols(options.tickers_txt, options.tickers_config),
            as_of=as_of,
            work_dir=options.work_dir,
            out_dir=options.out_dir,
            user_agent=options.user_agent,
            zip_path=options.zip,
            deps=deps,
            sic_cache_max_age_days=options.sic_cache_max_age_days,
            min_interval_s=options.min_interval,
            retries=options.retries,
        )
    except (BuildError, ValueError, OSError) as exc:
        print("error: " + " ".join(str(exc).split()), file=sys.stderr)
        return 2
    print(json.dumps({
        "coverage": summary.coverage,
        "snapshot": str(summary.snapshot_path),
        "exchanges": str(summary.exchanges_path),
        "snapshot_sha256": summary.snapshot_sha256,
        "unreadable": len(summary.unreadable),
        "sic_requests": summary.sic_requests,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
