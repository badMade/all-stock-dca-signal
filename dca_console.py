"""Local DCA control panel and single-command test launcher.

Core runtime is stdlib-only. Research commands run in a separate interpreter;
the console reads their documented artifacts and never imports their models.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from dca_console_jobs import Command, JobBusy, JobManager, redact
from dca_fileio import atomic_write_text
from dca_paths import cron_tracking_root, stream_root

REPO = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/styles.css": ("styles.css", "text/css")}


@dataclass(frozen=True)
class Settings:
    """Explicit data roots and Python runtimes for one local console session."""

    mode: str
    root: Path
    output: Path
    day: date
    python: str
    research_python: str
    octopus_run: Path | None = None
    config_dir: Path = REPO / "config"


def python_packages(executable: str) -> dict:
    """Probe optional package availability without importing research modules."""
    names = ("numpy", "pandas", "sklearn", "torch", "pytest", "ruff")
    code = (
        "import importlib.util,json; "
        f"print(json.dumps({{n: importlib.util.find_spec(n) is not None for n in {names!r}}}))"
    )
    try:
        result = subprocess.run([executable, "-c", code], capture_output=True, text=True, timeout=15)
        return json.loads(result.stdout) if result.returncode == 0 else {"error": "Python probe failed"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"error": "Python runtime is unavailable"}


def default_demo_day() -> date:
    """Choose a recent full market session using the core's holiday calendar."""
    from dca_runner import market_day_status

    day = datetime.now(ET).date() - timedelta(days=1)
    for _ in range(15):
        status = market_day_status(day)
        if not status.is_closed and not status.is_early_close:
            return day
        day -= timedelta(days=1)
    raise ValueError("No recent full demo session could be selected; supply --date explicitly.")


def module_catalog(python: str) -> list[dict]:
    """Expose the repository's canonical entry points without executing them."""
    from scripts.agent_entry_points import ENTRY_POINTS

    def runnable(command: str) -> str:
        for prefix in ("python3 ", "python "):
            if command.startswith(prefix):
                return shlex.quote(python) + " " + command[len(prefix):]
        return command

    rows = [{"id": f"entry-{i}", "name": e.need, "description": e.notes,
             "command": runnable(e.command), "docs": e.doc_ref, "status": "Existing entry point"}
            for i, e in enumerate(ENTRY_POINTS)]
    extras = [
        ("Octopus replay", "Walk-forward OHLC predictions and regime validation.",
         "wondrous-octopus/run_walkforward.py --preset quick --dry-run", "docs/wondrous_octopus.md"),
        ("Octopus daily update", "Score matured forecasts, update models, predict the next trading row.",
         "wondrous-octopus/daily_update.py --run-dir <run-dir>", "docs/wondrous_octopus.md"),
        ("Octopus inspection", "Inspect a ticker, predictions, or model report.",
         "wondrous-octopus/inspect_run.py --run-dir <run-dir> --summary", "docs/wondrous_octopus.md"),
        ("Ridge sizing research", "Train and evaluate sizing research without publishing to live configuration.",
         "ml-forecaster/train.py --dry-run --models-dir <research-output>", "ml-forecaster/README.md"),
        ("Market graph", "Create the existing interactive market graph from your local archive.",
         "-m scripts.generate_market_graph --source archive --out <output.html>", "docs/market_graph.md"),
        ("Market breadth", "Optional external market breadth data collector; requires its own setup.",
         "ticker-market-breadth/fetch_tick.py --help", "ticker-market-breadth/README.md"),
        ("Strategy research", "Independent backtesting and sentiment tools with optional dependencies.",
         "trading-strategies/backtest.py --help", "trading-strategies/README.md"),
        ("Agent loop primitives", "Specification and bounded-agent-loop utilities; no live wiring by default.",
         "", "production-safe-agent-loop/README.md"),
        ("Sizing signal interfaces", "Research-side forecasting and sizing interfaces.",
         "", "dca-sizing-signals/README.md"),
    ]
    rows.extend({"id": f"extra-{i}", "name": name, "description": description,
                 "command": f"{shlex.quote(python)} {command}" if command else "Library API; see documentation",
                 "docs": docs, "status": "Optional research / manual inputs"}
                for i, (name, description, command, docs) in enumerate(extras))
    return rows


class ConsoleApp:
    """Bridge local artifact readers, fixed jobs, daily summaries, and the UI."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs = JobManager(settings.output / "jobs")
        self.token = secrets.token_urlsafe(32)
        self.packages = python_packages(settings.research_python)
        self.dev_packages = python_packages(settings.python)
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.lock = threading.RLock()
        self.signatures: dict[str, str] = {}
        self.octopus_run = settings.octopus_run
        self._load_demo_pointer()

    def _load_demo_pointer(self) -> None:
        if self.settings.mode != "demo" or self.octopus_run is not None:
            return
        pointer = self.settings.output / "demo" / "octopus-current.json"
        try:
            value = json.loads(pointer.read_bytes())
            path = Path(value["path"]).resolve()
            if path.is_relative_to((self.settings.output / "demo" / "octopus").resolve()) and path.is_dir():
                self.octopus_run = path
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def actions(self) -> list[dict]:
        """List fixed actions, with explicit dependency/setup availability."""
        research = all(self.packages.get(key) is True for key in ("numpy", "pandas", "sklearn"))
        research_reason = "Install wondrous-octopus/requirements.txt in --research-python's environment."
        definitions = [
            ("demo-all", "Run full demo", "DCA pipeline + Octopus replay + daily summary.", True, ""),
            ("demo-dca", "Test DCA pipeline", "Run the real dry-run pipeline and reporting with synthetic market data.", True, ""),
            ("demo-octopus", "Test Octopus pipeline", "Replay synthetic data through persistence and GBM baselines.", research, research_reason),
            ("demo-neural", "Test neural Octopus", "Replay synthetic data through the actual PyTorch Octopus model.",
             research and self.packages.get("torch") is True, research_reason),
            ("reports", "Save daily reports", "Generate dated JSON / Markdown daily summaries and confirmed-purchase exports.",
             self.settings.root.is_dir(), "No artifact directory is available yet."),
            ("test-signals", "Test all seven signal slots", "Run the existing seven-slot regression tests locally.",
             self.dev_packages.get("pytest") is True, "Install requirements-dev.txt in the Python environment."),
            ("octopus-update", "Update Octopus day", "Score, update and forecast using the configured research checkpoint and local archive.",
             research and self.octopus_run is not None, "Supply --octopus-run and install research dependencies."),
            ("octopus-estimate", "Estimate archive replay", "Read the archive and estimate a quick replay; fits no models.",
             research, research_reason),
        ]
        # Demo actions always write under output/demo, never into the observed local root.
        return [{"id": action, "label": label, "description": description,
                 "available": bool(available), "reason": "" if available else reason}
                for action, label, description, available, reason in definitions]

    def snapshot(self, day: date | None = None, ticker: str = "", period: str = "all") -> dict:
        """Read current evidence, cache briefly, and save a sanitized daily digest."""
        from dca_console_data import build_daily_snapshot
        from dca_console_octopus import build_octopus_snapshot

        day = day or self.settings.day
        key = (day.isoformat(), ticker, period)
        with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 12:
                data = dict(cached[1])
            else:
                now = datetime.now(ET)
                daily = build_daily_snapshot(
                    self.settings.root, day, now, self.settings.config_dir,
                    excluded_roots=(self.settings.output / "demo",) if self.settings.mode == "local" else (),
                )
                octopus = build_octopus_snapshot(self.octopus_run, ticker=ticker, period=period)
                data = {
                    "mode": self.settings.mode, "now": now.isoformat(), "default_date": self.settings.day.isoformat(),
                    "daily": daily, "octopus": octopus, "warnings": [],
                    "modules": module_catalog(self.settings.python),
                    "paths": {"artifacts": str(self.settings.root), "summaries": str(self.settings.output / "summaries")},
                }
                try:
                    overview = build_octopus_snapshot(self.octopus_run) if ticker or period != "all" else octopus
                    self._save_summary(daily, overview)
                except OSError as error:
                    data["warnings"].append(redact(f"Could not save daily summary: {error}"))
                if len(self.cache) >= 32:
                    self.cache.clear()
                self.cache[key] = (time.monotonic(), data)
        return dict(data, jobs=self.jobs.snapshot(), actions=self.actions())

    def _save_summary(self, daily: dict, octopus: dict) -> None:
        # Include the forecasting overview only; filters do not change the saved daily summary.
        doc = {"mode": self.settings.mode, "daily": daily,
               "forecast_summary": {"status": octopus.get("status"), "summary": octopus.get("summary", []),
                                    "warnings": octopus.get("warnings", [])}}
        content = json.dumps(doc, indent=2, allow_nan=False)
        day = daily["date"]
        signature = hashlib.sha256(content.encode()).hexdigest()
        if self.signatures.get(day) == signature:
            return
        folder = self.settings.output / "summaries" / self.settings.mode
        folder.mkdir(parents=True, exist_ok=True)
        lines = [f"# DCA daily summary — {day}", "", f"Mode: {self.settings.mode}", ""]
        lines.extend(str(line) for line in daily.get("summary", []))
        lines.extend(["", "## Observed runs", ""])
        lines.extend(f"- {r.get('time', 'Unknown time')} · {r.get('stream', 'unknown')} · "
                     f"{r.get('status', 'unknown')} · {r.get('mode', 'unknown')}"
                     for r in daily.get("runs", []))
        lines.extend(["", "## Forecast research", ""] + octopus.get("summary", []))
        lines.extend(["", "## Evidence warnings", ""] + daily.get("warnings", []) + octopus.get("warnings", []))
        atomic_write_text(folder / f"{day}.json", content)
        atomic_write_text(folder / f"{day}.md", "\n".join(lines) + "\n")
        self.signatures[day] = signature

    def submit(self, action: str) -> dict:
        """Dispatch an allowlisted action; API requests cannot supply commands or paths."""
        match = next((item for item in self.actions() if item["id"] == action), None)
        if not match:
            raise ValueError("Unknown action")
        if not match["available"]:
            raise ValueError(match["reason"])
        return self.jobs.submit(action, match["label"], lambda job_id, log: self._run(action, job_id, log))

    def _command(self, argv: list[str], log, *, demo: bool = False, research: bool = False, timeout: float = 600) -> int:
        env = dict(os.environ)
        env["DCA_CRON_TRACKING_ROOT"] = str(self.settings.output / "demo" / "dca" if demo else self.settings.root)
        env["PYTHONUNBUFFERED"] = "1"
        if demo:
            # External artifact overrides would defeat the isolated demo root.
            for key in list(env):
                if key.startswith("DCA_") and key not in {"DCA_CRON_TRACKING_ROOT", "DCA_TICKER_HISTORY_DIR"}:
                    del env[key]
            env.pop("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", None)
        executable = self.settings.research_python if research else self.settings.python
        return self.jobs.run_command(Command(tuple([executable, *argv]), REPO, env, timeout), log)

    def _run(self, action: str, job_id: str, log) -> int:
        results = []
        if action in {"demo-all", "demo-dca"}:
            day = self.settings.day if self.settings.mode == "demo" else default_demo_day()
            target = self.settings.output / "demo" / "dca" / "ce16ba6b" / f"{day:%Y%m%d}_demo_{job_id}"
            results.append(self._command([
                "run_dca.py", "--demo", "--report", "--now", f"{day}T15:45",
                "--artifacts-dir", str(target), "--run-label", "console-demo",
            ], log, demo=True))
            if results[-1] == 0 and not (target / "run_state.json").exists():
                log("DCA produced no run_state.json: the configured date/window gate may have skipped this date.\n")
                results[-1] = 1
        if action in {"demo-all", "demo-octopus", "demo-neural"} and not self.jobs.stop.is_set():
            if not all(self.packages.get(k) for k in ("numpy", "pandas", "sklearn")):
                log("SKIPPED Octopus: research dependencies unavailable. DCA demo and summaries remain usable.\n")
                results.append(1)
            else:
                target = self.settings.output / "demo" / "octopus" / job_id
                argv = ["wondrous-octopus/run_walkforward.py", "--demo", "--run-dir", str(target)]
                if action == "demo-neural":
                    argv.extend(["--model", "octopus", "--device", "cpu", "--end", "2019-09-13", "--max-hours", "0.02"])
                result = self._command(argv, log, demo=True, research=True)
                results.append(result)
                if result == 0:
                    daily_args = ["wondrous-octopus/daily_update.py", "--run-dir", str(target)]
                    if action == "demo-neural":
                        daily_args.extend(["--now", "2019-09-14"])
                    results.append(self._command(
                        daily_args, log, demo=True, research=True,
                    ))
                if result == 0 and self.settings.mode == "demo":
                    with self.lock:
                        self.octopus_run = target
                    atomic_write_text(self.settings.output / "demo" / "octopus-current.json",
                                      json.dumps({"path": str(target)}))
                elif result == 0:
                    log("Demo forecast artifacts saved. Use demo mode to inspect them; local view keeps your configured run.\n")
        if action == "octopus-update":
            argv = ["wondrous-octopus/daily_update.py", "--run-dir", str(self.octopus_run)]
            # A synthetic neural smoke run stops at a fixed historical cursor.
            # The full update is still available for genuine local research runs.
            if self.settings.mode == "demo" and self.octopus_run:
                meta = json.loads((self.octopus_run / "run_meta.json").read_bytes())
                if meta.get("model_selection") == "octopus":
                    argv.extend(["--now", "2019-09-14"])
            results.append(self._command(argv, log, research=True, timeout=1800))
            if results[-1] == 0:
                results.append(self._command(["wondrous-octopus/run_walkforward.py", "--report-only", str(self.octopus_run)],
                                             log, research=True))
        if action == "octopus-estimate":
            results.append(self._command(["wondrous-octopus/run_walkforward.py", "--preset", "quick", "--dry-run"],
                                         log, research=True))
        if action == "test-signals":
            files = ["market_open", "morning", "midday", "intraday", "buy"]
            results.append(self._command(["-m", "pytest", "-q", *[
                f"scripts/tests/test_generate_{name}_candidates.py" for name in files]], log, demo=True, timeout=900))
        if action == "reports":
            results.append(self._command(["-m", "scripts.purchase_history", "--cron-tracking-root",
                                         str(self.settings.root), "--output-dir",
                                         str(self.settings.output / "reports" / job_id)], log))
        with self.lock:
            self.cache.clear()
        snapshot = self.snapshot()
        log("\n" + "\n".join(snapshot["daily"]["summary"]) + "\n")
        log(f"Daily summaries: {self.settings.output / 'summaries' / self.settings.mode}\n")
        return next((code for code in results if code != 0), 0)


def make_server(app: ConsoleApp, port: int) -> ThreadingHTTPServer:
    """Serve only loopback, fixed assets and allowlisted APIs with same-origin writes."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def _reply(self, code: int, body: bytes, kind: str = "application/json", *, download: str = "") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value: dict) -> None:
            self._reply(code, json.dumps(value, allow_nan=False).encode())

        def _trusted(self) -> bool:
            port_number = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port_number}", f"localhost:{port_number}"}
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") in hosts
                    and (origin is None or origin in {f"http://{host}" for host in hosts})
                    and self.headers.get("Sec-Fetch-Site") not in {"cross-site"})

        def do_GET(self) -> None:
            if not self._trusted():
                self._json(403, {"error": "This console accepts same-origin loopback requests only."})
                return
            url = urlsplit(self.path)
            try:
                if url.path in ASSETS:
                    name, kind = ASSETS[url.path]
                    self._reply(200, (REPO / "console" / name).read_bytes(), kind)
                elif url.path == "/api/bootstrap":
                    self._json(200, {"token": app.token, "default_date": app.settings.day.isoformat()})
                elif url.path in {"/api/snapshot", "/api/export"}:
                    query = parse_qs(url.query, max_num_fields=8)
                    raw_day = query.get("date", [app.settings.day.isoformat()])[0]
                    day = date.fromisoformat(raw_day)
                    ticker = query.get("ticker", [""])[0][:32]
                    period = query.get("period", ["all"])[0][:80]
                    data = app.snapshot(day, ticker, period)
                    if url.path == "/api/export":
                        export = {key: data[key] for key in ("mode", "now", "daily", "octopus", "warnings")}
                        self._reply(200, json.dumps(export, indent=2, allow_nan=False).encode(),
                                    download=f"dca-summary-{day}.json")
                    else:
                        self._json(200, data)
                elif url.path == "/api/health":
                    self._json(200, {"status": "ok", "mode": app.settings.mode})
                elif url.path == "/api/docs":
                    query = parse_qs(url.query, max_num_fields=1)
                    name = query.get("path", [""])[0]
                    allowed = {row["docs"] for row in module_catalog(app.settings.python)}
                    path = (REPO / name).resolve()
                    if name not in allowed or not path.is_relative_to(REPO) or not path.is_file():
                        self._json(404, {"error": "Documentation not found"})
                    else:
                        self._reply(200, path.read_bytes(), "text/plain")
                else:
                    self._json(404, {"error": "Not found"})
            except ValueError:
                self._json(400, {"error": "Invalid date or query. Use date=YYYY-MM-DD."})
            except Exception as error:
                self._json(500, {"error": redact(f"Could not read local artifacts: {type(error).__name__}: {error}")})

        def do_POST(self) -> None:
            if not self._trusted() or not secrets.compare_digest(self.headers.get("X-Console-Token", ""), app.token):
                self._json(403, {"error": "Missing or invalid console token."})
                return
            if self.path != "/api/jobs":
                self._json(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError("Request body must be between 1 and 1024 bytes")
                self.connection.settimeout(5)
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) != {"action"} or not isinstance(payload["action"], str):
                    raise ValueError("Supply only a string action")
                self._json(202, {"job": app.submit(payload["action"])})
            except JobBusy as error:
                self._json(409, {"error": str(error)})
            except (ValueError, TimeoutError) as error:
                self._json(400, {"error": str(error)})
            except OSError as error:
                self._json(500, {"error": redact(str(error))})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    """Launch the browser UI, run a single action, or produce one daily snapshot."""
    parser = argparse.ArgumentParser(description="Local DCA console. Default: launch a synthetic demo and open the browser.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Use isolated synthetic demo artifacts (default)")
    mode.add_argument("--local", action="store_true", help="Observe existing artifacts; no automatic demo or broker calls")
    parser.add_argument("--root", type=Path, help="Existing DCA artifact root (implies --local)")
    parser.add_argument("--output-dir", type=Path, help="Console artifacts; default dca_paths.stream_root('console')")
    parser.add_argument("--octopus-run", type=Path, help="Existing Octopus run directory (use with --local)")
    parser.add_argument("--research-python", help="Python with Octopus dependencies; auto-detect repository .venv")
    parser.add_argument("--date", type=date.fromisoformat, help="Day to inspect; demo defaults to previous market session")
    parser.add_argument("--port", type=int, default=8765, help="Loopback HTTP port; use 0 for an available port")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--once", action="store_true", help="Run demo (unless local), save summaries, print JSON and exit")
    parser.add_argument("--run", metavar="ACTION", help="Run one allowlisted action, print its result and exit")
    parser.add_argument("--check", action="store_true", help="Show runtime and action readiness without starting jobs")
    args = parser.parse_args(argv)
    if args.demo and (args.root or args.octopus_run):
        parser.error("Use --local with existing --root/--octopus-run; --demo keeps synthetic data isolated.")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    local = bool(args.local or args.root or args.octopus_run)
    root = (args.root or Path(cron_tracking_root())).expanduser().resolve()
    output = (args.output_dir or Path(stream_root("console"))).expanduser().resolve()
    venv_python = REPO / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = str(venv_python) if venv_python.is_file() else sys.executable
    try:
        selected_day = args.date or (datetime.now(ET).date() if local else default_demo_day())
    except (RuntimeError, ValueError) as error:
        parser.error(f"Cannot select a demo session: {error}. Supply --date YYYY-MM-DD.")
    settings = Settings("local" if local else "demo", root if local else output / "demo" / "dca",
                        output, selected_day,
                        python, args.research_python or python,
                        args.octopus_run.expanduser().resolve() if args.octopus_run else None)
    app = ConsoleApp(settings)
    if args.check:
        print(json.dumps({"mode": settings.mode, "python": python, "research_python": settings.research_python,
                          "packages": app.packages, "actions": app.actions(), "modules": module_catalog(python)}, indent=2))
        return 0
    if args.run or args.once:
        try:
            if args.run or not local:
                app.submit(args.run or "demo-all")
                assert app.jobs.worker is not None
                app.jobs.worker.join()
            print(json.dumps(app.snapshot(), indent=2, allow_nan=False))
            return app.jobs.snapshot()[0]["returncode"] if (args.run or not local) else 0
        except (OSError, ValueError, JobBusy) as error:
            print(redact(str(error)), file=sys.stderr)
            return 1
        finally:
            app.jobs.close()
    try:
        server = make_server(app, args.port)
    except OSError as error:
        print(f"Cannot open loopback port {args.port}: {error}. Try --port 0.", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"DCA console ({settings.mode}): {url}\nDaily summaries: {output / 'summaries' / settings.mode}\nCtrl+C stops the console.", flush=True)

    def refresh() -> None:
        while not app.jobs.stop.is_set():
            try:
                # Continue collecting today's summary across midnight without needing an open browser.
                app.snapshot(datetime.now(ET).date() if local else settings.day)
            except Exception as error:
                print(redact(f"Summary refresh failed: {error}"), file=sys.stderr)
            app.jobs.stop.wait(15)

    updater = threading.Thread(target=refresh, daemon=True)
    updater.start()
    try:
        if not local:
            app.submit("demo-all")
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping local console.", flush=True)
    finally:
        app.jobs.close()
        server.server_close()
        updater.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
