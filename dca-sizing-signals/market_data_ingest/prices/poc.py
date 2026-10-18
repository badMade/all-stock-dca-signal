"""Opt-in proof of concept against the live APIs (driven by tests/prices/contract/test_live_poc.py).

Ingests the last ``n_sessions`` closed sessions from Massive into a throwaway store, discovers
four role tickers from that data (a reverse split, a high-dividend name, a delisting and an
IPO inside the window; they post-date the author's knowledge, so they cannot be hard-coded),
reconciles every chosen symbol against its Yahoo CSV, spot-checks Alpaca's failover bars
and IEX trades, and writes a Markdown report. Live payloads are re-recorded, with every
credential string removed, for ``tests/prices/test_fixture_shapes.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .alpaca import AlpacaCalendar, AlpacaProvider
from .daily_job import SESSION_CLOSED_AFTER
from .errors import ProviderError, ValidationError
from .legacy_yahoo import import_csv
from .massive import MassiveProvider
from .models import CorporateAction
from .reconcile import reconcile_symbol
from .store import PriceStore

ET = ZoneInfo("America/New_York")
ROLES = ("reverse_split", "high_dividend", "delisted", "ipo")
_COMMON_TICKER = re.compile(r"^[A-Z]{1,5}$")  # skips warrants, units, preferreds, test symbols
EDGE_SESSIONS = 5
MAX_PLAUSIBLE_YIELD = 0.25
POC_BOOTSTRAP_FLOOR = 5000
RECORDED_ROWS = 50


@dataclass(frozen=True)
class PocDiff:
    close_status: str
    worst_close_diff_pct: float | None
    adj_close_worst_diff_pct: float | None
    volume_flag: bool
    sessions: int


@dataclass
class PocReport:
    symbols: list[str]
    roles: dict[str, str]
    sessions: list[date]
    present_sessions: dict[str, list[date]]
    actions: dict[str, list[CorporateAction]]
    delisted_last_session: date
    yahoo_diffs: dict[str, PocDiff] = field(default_factory=dict)
    no_yahoo_reason: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def discover_roles(
    *,
    sessions: Sequence[date],
    presence: Mapping[str, Sequence[date]],
    actions: Sequence[CorporateAction],
    last_close: Mapping[str, float],
    has_yahoo: Callable[[str], bool],
    exclude: Sequence[str],
) -> dict[str, str]:
    """Pick one ticker per role, deterministically. Raises if a role has no candidate."""
    window = list(sessions)
    first, last = window[0], window[-1]
    excluded = set(exclude)
    eligible = {s for s in presence if _COMMON_TICKER.match(s) and s not in excluded}
    full = {s for s in eligible if list(presence[s]) == window}

    def pick(role: str, candidates: Sequence[tuple[Any, ...]]) -> str:
        if not candidates:
            raise ProviderError(f"PoC found no candidate for the {role!r} role in this window", code="poc_no_candidate")
        return sorted(candidates)[0][-1]

    reverse = [
        (-len(presence[a.symbol]), a.symbol)
        for a in actions
        if a.action_type == "reverse_split"
        and first < a.ex_date <= last
        and a.symbol in eligible
        and first in presence[a.symbol]
        and last in presence[a.symbol]
    ]
    chosen: dict[str, str] = {"reverse_split": pick("reverse_split", reverse)}

    paid: dict[str, float] = {}
    for a in actions:
        if a.action_type == "cash_dividend" and first <= a.ex_date <= last and a.cash_amount:
            paid[a.symbol] = paid.get(a.symbol, 0.0) + a.cash_amount
    dividend = [
        (-paid[s] / last_close[s], s)
        for s in paid
        if s in full
        and s not in chosen.values()
        and last_close.get(s)
        and has_yahoo(s)
        and paid[s] / last_close[s] < MAX_PLAUSIBLE_YIELD
    ]
    chosen["high_dividend"] = pick("high_dividend", dividend)

    gone = []
    for s in eligible - set(chosen.values()):
        seen = list(presence[s])
        if seen and seen[0] == first and seen[-1] <= window[-1 - EDGE_SESSIONS] and seen == window[: len(seen)]:
            gone.append((not has_yahoo(s), -len(seen), s))
    chosen["delisted"] = pick("delisted", gone)

    listed = []
    for s in eligible - set(chosen.values()):
        seen = list(presence[s])
        if seen and seen[0] >= window[EDGE_SESSIONS] and seen == window[window.index(seen[0]) :]:
            listed.append((-len(seen), s))
    chosen["ipo"] = pick("ipo", listed)
    return chosen


def _floatify_large_counts(node: Any) -> bool:
    """Rewrite integer v/n fields of six or more digits as floats, in place. True if any changed."""
    changed = False
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("v", "n") and isinstance(value, int) and not isinstance(value, bool) and value >= 100_000:
                node[key] = float(value)
                changed = True
            else:
                changed = _floatify_large_counts(value) or changed
    elif isinstance(node, list):
        for item in node:
            changed = _floatify_large_counts(item) or changed
    return changed


class _RecordingSession:
    """Wraps a requests.Session; keeps the first body of each kind, credentials stripped."""

    def __init__(self, inner: Any, record_dir: Path, secrets: Sequence[str]) -> None:
        self._inner, self._dir, self._secrets = inner, record_dir, [s for s in secrets if s]
        self._saved: set[str] = set()

    @staticmethod
    def _kind(url: str, params: Mapping[str, Any]) -> str | None:
        if "/v2/aggs/grouped/" in url:
            return f"massive_grouped_{url.rstrip('/').rsplit('/', 1)[-1]}"
        if "/stocks/v1/splits" in url:
            return "massive_splits"
        if "/stocks/v1/dividends" in url:
            return "massive_dividends"
        if url.endswith("/v2/stocks/trades/latest"):
            return f"alpaca_latest_trades_{params.get('feed', 'iex')}"
        if url.endswith("/v2/stocks/bars"):
            return "alpaca_bars"
        return None

    def get(self, url: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        response = self._inner.get(url, params=params, **kwargs)
        kind = self._kind(url, params or {})
        if (
            kind
            and kind not in self._saved
            and response.status_code == 200
            and (
                not kind.startswith("massive_grouped") or not any(k.startswith("massive_grouped") for k in self._saved)
            )
        ):
            self._save(kind, response.text)
        return response

    def _save(self, kind: str, text: str) -> None:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        try:
            body = json.loads(text)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        note = f"recorded live {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ} by market_data_ingest.prices.poc"
        if isinstance(body.get("results"), list) and len(body["results"]) > RECORDED_ROWS:
            body["results"] = body["results"][:RECORDED_ROWS]
            note += f"; results truncated to {RECORDED_ROWS} rows for size (resultsCount kept as delivered)"
        if _floatify_large_counts(body):
            # tests/test_config_invariants.py flags any bare 6-12 digit integer as an
            # account-shaped literal, and live volumes/trade counts are exactly that.
            note += "; integer volumes and trade counts of six or more digits written as floats (account guard)"
        body["_provenance"] = note
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{kind}.json").write_text(json.dumps(body, indent=2) + "\n")
        self._saved.add(kind)


def _closed_sessions(calendar: AlpacaCalendar, now: datetime, n: int) -> list[date]:
    today = now.astimezone(ET).date()
    cutoff = datetime.combine(today, SESSION_CLOSED_AFTER, ET)
    last = today if calendar.is_session(today) and now >= cutoff else calendar.previous_sessions(today, 1)[0]
    return sorted([last, *calendar.previous_sessions(last, n - 1)])


def run_poc(
    *,
    env: Mapping[str, str],
    db_path: Path,
    yahoo_dir: Path,
    core_symbols: Sequence[str],
    n_sessions: int,
    report_path: Path,
    record_dir: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> PocReport:
    import requests

    secrets = [env.get(k, "") for k in ("MASSIVE_API_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY")]
    http = _RecordingSession(requests.Session(), Path(record_dir), secrets)
    run_id = f"poc-{clock():%Y%m%dT%H%M%SZ}"
    staging = Path(db_path).parent / "staging"
    with PriceStore(db_path) as store:
        calendar = AlpacaCalendar.from_env(env, session=http)
        massive = MassiveProvider.from_env(
            env,
            session=http,
            calendar=calendar,
            bootstrap_floor=POC_BOOTSTRAP_FLOOR,
            trailing_counts=lambda d: store.trailing_session_counts("massive", before=d),
        )
        sessions = _closed_sessions(calendar, clock(), n_sessions)
        for day in sessions:
            store.ingest_daily_bars(massive.daily_bars(day), run_id=run_id, as_of=clock(), staging_dir=staging)
        actions = massive.corporate_actions(sessions[0] - timedelta(days=30))
        store.ingest_corporate_actions(actions, run_id=run_id, as_of=clock(), staging_dir=staging)

        presence: dict[str, list[date]] = {}
        for symbol, session in store.con.execute(
            "SELECT DISTINCT symbol, session FROM daily_bars_raw WHERE source = 'massive' ORDER BY symbol, session"
        ).fetchall():
            presence.setdefault(symbol, []).append(session)
        last_close = {
            s: c
            for s, c in store.con.execute(
                "SELECT symbol, close FROM bars_known_at(?) WHERE session = ?", [datetime(9999, 12, 31), sessions[-1]]
            ).fetchall()
        }

        roles = discover_roles(
            sessions=sessions,
            presence=presence,
            actions=actions,
            last_close=last_close,
            has_yahoo=lambda s: (Path(yahoo_dir) / f"{s}.csv").exists(),
            exclude=core_symbols,
        )
        symbols = list(core_symbols) + [roles[r] for r in ROLES]
        delisted_last = presence[roles["delisted"]][-1]
        report = PocReport(
            symbols=symbols,
            roles=roles,
            sessions=sessions,
            present_sessions={s: presence.get(s, []) for s in symbols},
            actions={s: [a for a in actions if a.symbol == s] for s in symbols},
            delisted_last_session=delisted_last,
        )

        for symbol in symbols:
            path = Path(yahoo_dir) / f"{symbol}.csv"
            if not path.exists():
                report.no_yahoo_reason[symbol] = "no Yahoo CSV in the archive"
                continue
            try:
                import_csv(store, path, run_id=run_id, now=clock())
            except ValidationError as exc:
                report.no_yahoo_reason[symbol] = f"Yahoo CSV rejected: {exc}"
                continue
            result = reconcile_symbol(store, symbol, run_id=run_id, now=clock(), max_sessions=n_sessions)
            report.yahoo_diffs[symbol] = PocDiff(
                result.status,
                result.worst_close_diff_pct,
                result.adj_close_worst_diff_pct,
                result.volume_flag,
                result.sessions,
            )

        alpaca = AlpacaProvider.from_env(
            env, session=http, universe=lambda: [s for s in symbols if sessions[-1] in presence.get(s, [])]
        )
        try:
            alpaca_bars = {b.symbol: b.close for b in alpaca.daily_bars(sessions[-1])}
            diffs = [abs(alpaca_bars[s] - last_close[s]) / last_close[s] for s in alpaca_bars if s in last_close]
            report.notes.append(
                f"Alpaca failover bars for {sessions[-1]}: {len(alpaca_bars)} symbols, worst close "
                f"diff vs Massive {max(diffs) * 100 if diffs else float('nan'):.3f}%"
            )
        except ProviderError as exc:
            report.notes.append(f"Alpaca failover bars failed: {type(exc).__name__} ({exc.code})")
        try:
            trades = alpaca.last_trade(list(core_symbols), feed="iex")
            report.notes.append(f"Alpaca IEX last trade returned {len(trades)}/{len(core_symbols)} core symbols")
        except ProviderError as exc:
            report.notes.append(f"Alpaca IEX last trade failed: {type(exc).__name__} ({exc.code})")

    Path(report_path).write_text(render_report(report, clock()))
    return report


def render_report(report: PocReport, generated_at: datetime) -> str:
    role_of = {v: k for k, v in report.roles.items()}
    lines = [
        "# Price source PoC report (Massive + Alpaca vs yahoo_legacy)",
        "",
        f"Generated {generated_at:%Y-%m-%d %H:%M} UTC by `market_data_ingest.prices.poc` "
        f"(tests/prices/contract/test_live_poc.py). Window: {len(report.sessions)} sessions, "
        f"{report.sessions[0]} to {report.sessions[-1]}.",
        "",
        "| Symbol | Role | Sessions present | First | Last | Yahoo close tier | Worst close diff % "
        "| Worst Adj Close diff % | Volume flag |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in report.symbols:
        seen = report.present_sessions.get(s, [])
        d = report.yahoo_diffs.get(s)
        tier = d.close_status if d else f"n/a ({report.no_yahoo_reason.get(s, 'unknown')})"

        def pct(v: float | None) -> str:
            return "" if v is None else f"{v:.3f}"

        lines.append(
            f"| {s} | {role_of.get(s, 'S&P 500')} | {len(seen)} | {seen[0] if seen else ''} | "
            f"{seen[-1] if seen else ''} | {tier} | {pct(d.worst_close_diff_pct) if d else ''} | "
            f"{pct(d.adj_close_worst_diff_pct) if d else ''} | {d.volume_flag if d else ''} |"
        )
    lines += [
        "",
        f"Delisted check: {report.roles['delisted']} appears in every grouped batch through "
        f"{report.delisted_last_session} and in none after it.",
        "",
    ]
    lines += [f"- {note}" for note in report.notes]
    return "\n".join(lines) + "\n"
