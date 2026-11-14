"""The nightly ingest job. Idempotent; exits non-zero on any failure.

    python -m market_data_ingest.prices.daily_job [--session YYYY-MM-DD] [--db PATH] [--bootstrap [FLOOR]]

Steps (PLAN-price-source.md §4):

1. Calendar check. A closed market records ``skipped_closed`` and exits 0. A calendar outage
   fails the run. A session that has not closed yet (before 20:00 ET) is not fetched.
2. Append ``running`` to ``ingestion_runs``.
3-6. For the session, fetch through the router (Massive, then Alpaca), validate, stage to
   Parquet, and insert with ``ON CONFLICT DO NOTHING`` in one transaction.
7. Re-pull T-1..T-5 the same way, so vendor corrections land as new rows.
8. Splits and dividends with ex-dates since T-30 (calendar days).
9. Append ``ok`` or ``failed``.

Each batch is atomic and independent: a later batch failing leaves earlier, complete batches
committed and marks the run ``failed``. The run status, not the presence of rows, says
whether the job finished. A ``ProviderError`` or ``ValidationError`` is recorded and turned
into exit code 1; anything else is recorded and re-raised, so a bug still shows a traceback.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .errors import AmbiguousActionsError, ProviderError, ValidationError
from .models import JobResult
from .provider import SessionCalendar
from .store import PriceStore

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
JOB = "daily_bars"
SESSION_CLOSED_AFTER = time(20, 0)  # ET; Massive EOD aggregates are complete by then
REPULL_SESSIONS = 5
ACTION_LOOKBACK_DAYS = 30
DEFAULT_BOOTSTRAP_FLOOR = 8000
REPO_ROOT = Path(__file__).resolve().parents[3]
MAX_DETAIL_CHARS = 500


class SessionNotClosed(Exception):
    """The requested session's end-of-day data is not final yet."""


def resolve_db_path(cli_value: str | None, env: Mapping[str, str], repo_root: Path = REPO_ROOT) -> Path:
    """``--db``, else ``$DCA_PRICE_DB``, else ``<repo>/data/backfill.duckdb``."""
    if cli_value:
        return Path(cli_value)
    if env.get("DCA_PRICE_DB"):
        return Path(env["DCA_PRICE_DB"])
    return repo_root / "data" / "backfill.duckdb"


def _default_run_id() -> str:
    return f"daily-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def run_daily_job(
    *,
    session: date,
    store: PriceStore,
    router: Any,
    calendar: SessionCalendar,
    clock: Callable[[], datetime],
    staging_dir: Path,
    run_id_factory: Callable[[], str] = _default_run_id,
) -> JobResult:
    run_id = run_id_factory()

    def finish(status: str, exit_code: int, exc: BaseException | None = None, rows: int | None = None) -> JobResult:
        error_class = type(exc).__name__ if exc is not None else None
        detail = str(exc)[:MAX_DETAIL_CHARS] if exc is not None else None
        store.record_run(
            run_id,
            job=JOB,
            status=status,
            at=clock(),
            session=session,
            row_count=rows,
            error_class=error_class,
            error_detail=detail,
        )
        log = logger.info if exit_code == 0 else logger.error
        log("run %s session %s: %s%s", run_id, session, status, f" ({error_class}: {detail})" if exc else "")
        return JobResult(run_id=run_id, status=status, exit_code=exit_code, error_class=error_class)

    try:
        is_open = calendar.is_session(session)
    except ProviderError as exc:
        return finish("failed", 1, exc)
    if not is_open:
        return finish("skipped_closed", 0)
    cutoff = datetime.combine(session, SESSION_CLOSED_AFTER, ET)
    if clock() < cutoff:
        return finish("failed", 1, SessionNotClosed(f"{session} is not final until {cutoff.isoformat()}"))

    store.record_run(run_id, job=JOB, status="running", at=clock(), session=session)
    inserted = 0
    try:
        prior = calendar.previous_sessions(session, REPULL_SESSIONS)
        for day in [session, *prior]:
            bars = router.daily_bars(day)
            inserted += store.ingest_daily_bars(bars, run_id=run_id, as_of=clock(), staging_dir=staging_dir)
        actions = router.corporate_actions(session - timedelta(days=ACTION_LOOKBACK_DAYS))
        inserted += store.ingest_corporate_actions(actions, run_id=run_id, as_of=clock(), staging_dir=staging_dir)
    except AmbiguousActionsError as exc:
        # The unambiguous actions were written; count them in the failed run's record.
        return finish("failed", 1, exc, inserted + exc.inserted)
    except (ProviderError, ValidationError) as exc:
        return finish("failed", 1, exc, inserted)
    except BaseException as exc:
        finish("failed", 1, exc, inserted)
        raise
    return finish("ok", 0, rows=inserted)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m market_data_ingest.prices.daily_job",
        description="Ingest one session of raw daily bars (Massive, Alpaca failover).",
    )
    parser.add_argument("--session", type=date.fromisoformat, help="session date (default: today, US/Eastern)")
    parser.add_argument("--db", help="DuckDB path (default: $DCA_PRICE_DB, else <repo>/data/backfill.duckdb)")
    parser.add_argument("--staging-dir", help="Parquet staging root (default: <db dir>/staging)")
    parser.add_argument(
        "--bootstrap",
        nargs="?",
        type=int,
        const=DEFAULT_BOOTSTRAP_FLOOR,
        default=None,
        metavar="FLOOR",
        help="allow a run with fewer than 20 stored sessions, requiring at least FLOOR rows "
        f"(default {DEFAULT_BOOTSTRAP_FLOOR})",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    from .alpaca import AlpacaCalendar, AlpacaProvider
    from .massive import MassiveProvider
    from .router import PriceRouter

    env = os.environ if env is None else env
    args = _parse_args(argv)
    missing = [k for k in ("MASSIVE_API_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY") if not env.get(k)]
    if missing:
        print(
            f"error: {', '.join(missing)} not set (see dca-sizing-signals/README.md, 'Price source')", file=sys.stderr
        )
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db_path = resolve_db_path(args.db, env)
    staging_dir = Path(args.staging_dir) if args.staging_dir else db_path.parent / "staging"
    session = args.session or datetime.now(ET).date()
    with PriceStore(db_path) as store:
        calendar = AlpacaCalendar.from_env(env)
        massive = MassiveProvider.from_env(
            env,
            calendar=calendar,
            bootstrap_floor=args.bootstrap,
            trailing_counts=lambda d: store.trailing_session_counts("massive", before=d),
        )
        alpaca = AlpacaProvider.from_env(env, universe=lambda: store.last_session_symbols("massive"))
        result = run_daily_job(
            session=session,
            store=store,
            router=PriceRouter(massive, alpaca),
            calendar=calendar,
            clock=lambda: datetime.now(timezone.utc),
            staging_dir=staging_dir,
        )
    print(f"{result.run_id} {session} {result.status}" + (f" ({result.error_class})" if result.error_class else ""))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
