"""Architecture diagnostics page for the local console (checkout-only, never shipped).

A secondary, developer-facing view that shows how the repo's modules, classes and
functions connect, so a reviewer can judge whether the object model is going where
they want. It is deliberately *not* listed in ``pyproject.toml`` ``py-modules``
(see the "Deliberately NOT listed" note there): like the rest of the console it
only ever runs from a source checkout and is never part of a released build.

Everything here is a static ``ast`` read of the source tree -- nothing is imported
or executed, nothing is written, and no network or brokerage call is possible.
The result is one JSON-able dict (:func:`build_architecture`) that
:func:`render_page` embeds into a single self-contained HTML page. The page is
served from one route under the sandbox CSP that the other generated research
pages use, so its inline script cannot reach the console's origin.

Known limits (stated on the page too): calls are resolved only when the callee is
a module-level function or class reached through an ``import`` / ``from ... import``
of another repo module, or a same-module name. ``obj.method()`` calls on instances
are not resolved, so function-level edges under-count rather than over-count.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from pathlib import Path
from typing import Any, Iterable

# Directories whose .py files are scanned. Tests, vendored trees and the research
# subprojects are excluded so the map shows the live system, not its scaffolding.
_SCAN_DIRS = ("", "scripts")
_SKIP_PARTS = {"tests", "__pycache__", ".venv", "node_modules", "viz", "trading-strategies", "analysis"}

# Layer rules are ordered; the first matching prefix wins. These mirror the
# Architecture table in CLAUDE.md and the plan-first modules added in PR #1667.
_LAYER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("console", ("dca_console", "from")),
    ("plan_first", (
        "dca_rec_", "dca_recommender", "dca_market_", "dca_fill_compare",
        "scripts.snapshot_cycle", "scripts.plan_first_slot", "scripts.read_recommendation",
        "scripts.validate_native_placement", "scripts.robinhood_payload",
    )),
    ("backup_executor", (
        "dca_execution_router", "dca_order_plan", "dca_number_check", "scripts.backup_planner", "scripts.backup_stream_specs",
        "scripts.local_heartbeat", "scripts.slot_status", "scripts.backup_cost_ledger", "scripts.backup_dispatch", "scripts.assemble_market_data",
        "scripts.build_backup_planner",
        "scripts.stream_candidates", "scripts.generate_",
    )),
    ("entry_points", (
        "near_close_dca", "morning_dca", "market_open_dca", "midday_dca", "intraday_dca", "run_dca",
        "dca_runner", "dca_ladder_core",
    )),
    ("money_risk", ("dca_risk", "dca_cumulative", "dca_cost_tracker", "dca_inverse_martingale", "dca_whole_share")),
    ("reliability", (
        "scripts.broker_reconciliation", "scripts.run_state_machine", "scripts.run_ledger", "scripts.idempotency",
        "scripts.scheduler_lease", "scripts.reason_codes", "scripts.correlation", "scripts.same_day",
        "scripts.file_lock", "scripts.run_classification",
    )),
)
LAYERS = ("plan_first", "backup_executor", "entry_points", "money_risk", "reliability", "console", "other")

# Annotations that mean "an untyped bag of fields" -- the thing the object model is
# trying to replace at module boundaries -- are matched by _is_bag below.


def _module_name(path: Path, repo: Path) -> str:
    rel = path.relative_to(repo).with_suffix("")
    return ".".join(rel.parts)


# "Other" held 71 of 146 modules as one grey pile, so it is split into descriptive subcategories. The layer
# stays "other" (the layer rules above are unchanged); each Other module also carries a ``group``. Members are
# listed by exact module name, not prefix: these are one-off scripts and a prefix would sweep in new neighbours.
# A new Other module that is in no group lands in "unsorted", and a test fails until it is given a home.
OTHER_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("config_data", "Config & data", (
        "dca_config", "dca_env", "dca_paths", "dca_ticker_paths", "dca_fileio", "dca_provenance", "dca_intraday_archive",
        "scripts.backfill_intraday_archive", "scripts.refresh_active_ticker_histories",
        "scripts.run_ticker_maintenance", "scripts.run_tickers_daily_update",
        "scripts.daily_per_ticker_calibration", "sizing_adjustment_reader", "sizing_suggestion_history",
        "sizing_suggestion_compare", "market_open_quote_shadow",
        "scripts.backup_strategy", "scripts.backup_state_root", "dca_goals", "scripts.propose_goal_pr",
    )),
    ("reporting_ops", "Reporting & monitoring", (
        "dca_alerts", "dca_average_cost_snapshot", "dca_goal_progress", "dca_notion_sync", "dca_reporting",
        "dca_weekly_dashboard",
        "scripts.alert_digest", "scripts.check_live_prompt_drift", "scripts.check_slot_fills",
        "scripts.close_window_signal", "scripts.drift_detection", "scripts.finalize_run_evidence",
        "scripts.foreground_primary", "scripts.gate_failure_diagnostics", "scripts.model_usage_log",
        "scripts.purchase_history", "scripts.schedule_watchdog", "scripts.claude_liveness", "scripts.scheduled_artifacts",
        "scripts.sync_live_prompts", "scripts.triage_log_summary", "scripts.triage_reconcile_batch", "scripts.verify_schedule", "scripts.evidence_publish", "scripts.cloud_preflight", "scripts.workspace_probe",
        "scripts.backfill_afternoon_evidence", "scripts.planner_stamp_check", "scripts.planner_repaste_notice", "scripts.slot_attribution",
        "scripts.abort_markers", "scripts.kill_switch_gap_proof", "scripts.local_ci_sweep",
        "scripts.index_run_states_sqlite",
    )),
    ("broker_market", "Broker, orders & market data", (
        "dca_live_adapters", "dca_slot_guard", "build_raw_data", "run_market_open_signals",
        "scripts.ingest_market_data", "scripts.build_robinhood_market_data", "scripts.order_refetch",
        "scripts.prior_reference_bindings", "scripts.assemble_order_pages", "scripts.adhoc_orders",
        "scripts.emit_order_plan", "scripts.refresh_market_open_candidates", "scripts.backup_gates",
        "scripts.primary_gates", "scripts.account_arg",
        "scripts.backup_procedure", "scripts.merge_order_pages", "scripts.check_per_order_record",
        "scripts.reshape_order_pages", "scripts.find_order_baseline", "scripts.backup_week_history", "dca_retry",
    )),
    ("research", "Research & analysis", (
        "scripts.auto_resume_walkforward", "scripts.shard_heartbeat", "scripts.evaluate_deferral_params",
        "scripts.export_octopus_ledger", "scripts.publish_octopus_sizing",
        "scripts.run_90day_prediction_experiment", "scripts.run_phase4_backtest",
        "scripts.sequestered_truth_comparator",
    )),
    ("utilities", "Dev tools & utilities", (
        "dca_bootstrap", "dca_common", "dca_net_utils", "find_issues",
        "snap", "scripts.__init__", "scripts.agent_entry_points", "scripts.compute_schedule",
        "scripts.fill_perplexity_backup_prompts", "scripts.github_mcp_login", "scripts.healthcheck",
        "scripts.self_heal", "scripts.verify_perplexity_pair", "scripts.workflow_policy", "scripts.promote_console",
    )),
)
UNSORTED_GROUP = ("unsorted", "Unsorted (needs a category)")


def _group_for(module: str, layer: str) -> str:
    """The Other-layer subcategory id for ``module`` ("" for every other layer)."""
    if layer != "other":
        return ""
    return next((gid for gid, _, members in OTHER_GROUPS if module in members), UNSORTED_GROUP[0])


def _layer_for(module: str) -> str:
    for layer, prefixes in _LAYER_RULES:
        if any(module == p or module.startswith(p) for p in prefixes):
            return layer
    return "other"


def _iter_sources(repo: Path) -> Iterable[Path]:
    for sub in _SCAN_DIRS:
        base = repo / sub if sub else repo
        pattern = "*.py"
        for path in sorted(base.glob(pattern)):
            if _SKIP_PARTS.intersection(path.relative_to(repo).parts):
                continue
            if path.name.startswith("test_") or path.name == "conftest.py":
                continue
            yield path


def _annotation_text(node: ast.expr | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # defensive: a malformed annotation must not sink the whole scan
        return ""


_BAG_NAMES = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_bag(annotation: str) -> bool:
    """True for an untyped bag of fields anywhere in the annotation (``list[dict[str, Any]]`` counts).

    A bare ``Any`` counts, but ``Any`` inside e.g. ``Callable[..., Any]`` does not.
    """
    if annotation.replace("typing.", "").strip() == "Any":
        return True
    return any(name in _BAG_NAMES for name in _NAME.findall(annotation))


def _decorators(node: ast.ClassDef) -> list[str]:
    return [_annotation_text(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list]


def _class_kind(node: ast.ClassDef) -> str:
    bases = [_annotation_text(b) for b in node.bases]
    frozen = any(
        isinstance(d, ast.Call) and any(k.arg == "frozen" and getattr(k.value, "value", False) for k in d.keywords)
        for d in node.decorator_list
    )
    if any(b.endswith("Protocol") for b in bases):
        return "protocol"
    if any(b.endswith("Enum") for b in bases):
        return "enum"
    if any(d.endswith("dataclass") for d in _decorators(node)):
        return "frozen dataclass" if frozen else "dataclass"
    if any(b.endswith(("Exception", "Error")) for b in bases):
        return "exception"
    return "class"


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, Any]]:
    args = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
    out = []
    for arg in args:
        if arg.arg in {"self", "cls"}:
            continue
        text = _annotation_text(arg.annotation)
        out.append({"name": arg.arg, "type": text, "bag": bool(text) and _is_bag(text)})
    return out


def _statements(tree: ast.AST) -> Iterable[ast.AST]:
    """Yield statement-level nodes only (imports are statements), skipping expression subtrees.

    A full ``ast.walk`` over a 40k-line module is slow enough under CI's coverage
    tracing to time out the route tests; statements are ~10x fewer than nodes.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            stack.extend(getattr(node, field, None) or ())


class _ModuleScan:
    """One module's classes, functions, import aliases and unresolved call names."""

    def __init__(self, module: str, tree: ast.Module, internal: set[str], *, detailed: bool = False) -> None:
        self.module = module
        self.internal = internal
        self.detailed = detailed
        self.aliases: dict[str, tuple[str, str | None]] = {}  # local name -> (module, symbol|None)
        # Names this module defines at top level: a bare call resolves to one of these,
        # to an internal import alias, or not at all (builtins, external imports and
        # callbacks must not become phantom ``module:isinstance`` edges).
        self.local_defs = {
            n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        self.classes: list[dict[str, Any]] = []
        self.functions: list[dict[str, Any]] = []
        self.imports: dict[str, dict[str, Any]] = {}  # target module -> {"names": set, "private": set}
        self.calls: list[tuple[str, str, str | None]] = []  # (caller id, callee module, callee symbol)
        self._collect_imports(tree)
        self._collect_defs(tree)

    def _resolve_module(self, name: str | None, level: int) -> str | None:
        if level:
            base = self.module.rsplit(".", level)[0] if "." in self.module else ""
            name = f"{base}.{name}".strip(".") if name else base
        if not name:
            return None
        if name in self.internal:
            return name
        if f"scripts.{name}" in self.internal and name.split(".")[0] not in {"scripts"}:
            # scripts/ modules import each other both as ``scripts.x`` and bare ``x``.
            return f"scripts.{name}" if self.module.startswith("scripts.") else None
        return None

    def _note_import(self, target: str, names: list[str]) -> None:
        entry = self.imports.setdefault(target, {"names": set(), "private": set()})
        for n in names:
            entry["names"].add(n)
            if n.startswith("_") and not n.startswith("__"):
                entry["private"].add(n)

    def _collect_imports(self, tree: ast.Module) -> None:
        for node in _statements(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = self._resolve_module(alias.name, 0)
                    if target:
                        self.aliases[alias.asname or alias.name.split(".")[-1]] = (target, None)
                        self._note_import(target, [])
            elif isinstance(node, ast.ImportFrom):
                target = self._resolve_module(node.module, node.level)
                if target:
                    names = [a.name for a in node.names]
                    self._note_import(target, names)
                    for alias in node.names:
                        sub = f"{target}.{alias.name}"
                        if sub in self.internal:
                            self.aliases[alias.asname or alias.name] = (sub, None)
                        else:
                            self.aliases[alias.asname or alias.name] = (target, alias.name)
                elif node.module == "scripts" or (node.module is None and node.level):
                    for alias in node.names:
                        sub = f"scripts.{alias.name}"
                        if sub in self.internal:
                            self.aliases[alias.asname or alias.name] = (sub, None)
                            self._note_import(sub, [])

    def _callee(self, call: ast.Call) -> tuple[str, str | None] | None:
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in self.aliases:
                return self.aliases[func.id]
            if func.id in self.local_defs:
                return (self.module, func.id)
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            alias = self.aliases.get(func.value.id)
            if alias and alias[1] is None:
                return (alias[0], func.attr)
        return None

    def _scan_calls(self, caller: str, body_node: ast.AST) -> None:
        if not self.detailed:
            return
        seen: set[tuple[str, str | None]] = set()
        for node in ast.walk(body_node):
            if isinstance(node, ast.Call):
                callee = self._callee(node)
                if callee and callee not in seen and callee[0] in self.internal:
                    seen.add(callee)
                    self.calls.append((caller, callee[0], callee[1]))

    def _collect_defs(self, tree: ast.Module) -> None:
        # The whole-repo scan stays a cheap skeleton (names, kinds, import edges):
        # unparsing every annotation is the slow part under CI's coverage tracing.
        # Fields, signatures and call edges are filled in per module, on demand.
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                row: dict[str, Any] = {"name": node.name, "kind": _class_kind(node), "line": node.lineno}
                if self.detailed:
                    row["fields"] = [
                        {"name": s.target.id, "type": _annotation_text(s.annotation), "bag": _is_bag(_annotation_text(s.annotation))}
                        for s in node.body
                        if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                    ]
                    row["methods"] = []
                    for s in node.body:
                        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            row["methods"].append({
                                "name": s.name, "params": _params(s), "returns": _annotation_text(s.returns), "line": s.lineno,
                                "doc": _doc_summary(s),
                            })
                            self._scan_calls(f"{self.module}:{node.name}.{s.name}", s)
                self.classes.append(row)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                row = {"name": node.name, "line": node.lineno, "private": node.name.startswith("_")}
                if self.detailed:
                    row.update({"params": _params(node), "returns": _annotation_text(node.returns), "doc": _doc_summary(node)})
                    self._scan_calls(f"{self.module}:{node.name}", node)
                self.functions.append(row)


PLAIN_NOTES_FILE = "docs/architecture_plain_english.json"


def _plain_notes(repo: Path) -> dict[str, str]:
    """Hand-written plain-English note per module; a missing or broken file just means no notes."""
    try:
        data = json.loads((Path(repo) / PLAIN_NOTES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}


def _doc_summary(node: ast.AST) -> str:
    """First sentence of a function's docstring (<=160 chars), so the pane can say what it does."""
    text = " ".join((ast.get_docstring(node) or "").split())  # type: ignore[arg-type]
    if not text:
        return ""
    sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return sentence if len(sentence) <= 160 else sentence[:157].rstrip() + "..."


def build_architecture(repo: Path) -> dict[str, Any]:
    """Scan ``repo`` and return the module/class/function graph as plain JSON data."""
    repo = Path(repo)
    parsed: dict[str, tuple[Path, ast.Module, int]] = {}
    for path in _iter_sources(repo):
        try:
            source = path.read_text(encoding="utf-8")
            parsed[_module_name(path, repo)] = (path, ast.parse(source), source.count("\n") + 1)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue  # a broken file is simply absent from the map, never a crash
    internal = set(parsed)
    notes = _plain_notes(repo)
    scans = {name: _ModuleScan(name, tree, internal) for name, (_, tree, _) in parsed.items()}

    modules, edges = [], []
    incoming: dict[str, int] = {name: 0 for name in scans}
    for name, scan in scans.items():
        for target in scan.imports:
            if target != name:
                incoming[target] += 1
    for name, scan in scans.items():
        _, _, lines = parsed[name]
        has_main = any(  # a CLI entry point: only top-level ``if __name__ == "__main__"`` counts
            isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and _annotation_text(n.test.left) == "__name__"
            for n in parsed[name][1].body
        )
        modules.append({
            "id": name, "layer": _layer_for(name), "lines": lines, "classes": scan.classes,
            "functions": scan.functions, "in_degree": incoming[name], "has_main": has_main,
            "unreferenced": incoming[name] == 0 and not has_main,
            "plain": notes.get(name, ""), "group": _group_for(name, _layer_for(name)),
        })
        for target, info in scan.imports.items():
            if target == name:
                continue
            edges.append({
                "from": name, "to": target, "names": sorted(info["names"])[:12],
                "private": sorted(info["private"]),
            })

    class_count = sum(len(m["classes"]) for m in modules)
    stats = {
        "modules": len(modules), "classes": class_count, "import_edges": len(edges),
        "private_cross_imports": sum(len(e["private"]) for e in edges),
        "unreferenced_modules": sum(1 for m in modules if m["unreferenced"]),
    }
    groups = [{"id": gid, "label": label, "layer": "other"} for gid, label, _ in OTHER_GROUPS]
    if any(m["group"] == UNSORTED_GROUP[0] for m in modules):
        groups.append({"id": UNSORTED_GROUP[0], "label": UNSORTED_GROUP[1], "layer": "other"})
    return {"layers": list(LAYERS), "groups": groups, "modules": modules, "edges": edges, "stats": stats}


class ScanCache:
    """Builds the whole-repo scan on a background thread so a request never blocks on it.

    The first request starts the scan and gets ``{"status": "building"}``; the page polls
    until ``ready``. Under CI's coverage tracing the scan can take several seconds, longer
    than a request should wait, and this is a diagnostic page, not a hot path.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._data: dict[str, Any] | None = None
        self._error: str | None = None
        self._repo: Path | None = None

    def _run(self, repo: Path) -> None:
        try:
            data, error = build_architecture(repo), None
        except Exception as exc:  # a scan bug must show on the page, not die silently in the thread
            data, error = None, f"{type(exc).__name__}: {exc}"
        with self._lock:
            if self._repo == repo:
                self._data, self._error = data, error

    def get(self, repo: Path, *, refresh: bool = False) -> dict[str, Any]:
        repo = Path(repo)
        with self._lock:
            if refresh or self._repo != repo:
                self._repo, self._data, self._error, self._thread = repo, None, None, None
            if self._data is not None:
                return {"status": "ready", **self._data}
            if self._error is not None:
                return {"status": "error", "error": self._error}
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, args=(repo,), daemon=True, name="architecture-scan")
                self._thread.start()
            return {"status": "building"}

    def wait(self, timeout: float = 60.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


SCAN_CACHE = ScanCache()


def build_detail(repo: Path, module: str) -> dict[str, Any] | None:
    """Fields, signatures and cross-module calls for one module, or None if it is not scanned.

    Computed on demand (selecting a module in the page); ``module`` is matched
    against the scanned names and never used as a path.
    """
    repo = Path(repo)
    paths = {_module_name(p, repo): p for p in _iter_sources(repo)}
    if module not in paths:
        return None
    try:
        tree = ast.parse(paths[module].read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return {"classes": [], "functions": [], "calls": []}
    scan = _ModuleScan(module, tree, set(paths), detailed=True)
    calls = [
        {"from": caller.split(":", 1)[1], "to": f"{target}:{symbol}" if symbol else target, "local": target == module}
        for caller, target, symbol in scan.calls
        if not (target == module and symbol is None)
    ]
    return {"classes": scan.classes, "functions": scan.functions, "calls": calls}


PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Architecture map</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/diagnostics/architecture.css"></head><body>
<aside>
<h1>Architecture map</h1>
<div class="banner">Diagnostic view &middot; static read of the source tree &middot; never part of a shipped build.</div>
<input type="search" id="q" placeholder="Find module, class, function" aria-label="Search">
<label for="pick" class="banner">Jump to module</label><select id="pick" aria-label="Select a module"></select>
<h2>Layers</h2><div id="layers"></div>
<h2>Show</h2>
<label><input type="checkbox" id="onlyhot"> Only edges of selected module</label>
<label><input type="checkbox" id="privonly"> Highlight private cross-imports</label>
<h2>Totals</h2><div id="stats"></div>
<h2>Needs a look</h2><div id="problems"></div>
<div class="banner gap">Limit: <code>obj.method()</code> calls on instances are not resolved, so call edges under-count.</div>
</aside>
<div class="grip" id="grip-l" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Resize left sidebar (drag, arrow keys, double-click to reset)"></div>
<div class="grip" id="grip-r" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Resize right sidebar (drag, arrow keys, double-click to reset)"></div>
<section id="map"><svg id="svg" role="group" aria-label="Module dependency map. Press Tab to move between modules, Enter to select."></svg></section>
<section id="detail"><div class="banner">Click a module to see its classes, fields, functions and who connects to it.</div></section>
<script src="/diagnostics/architecture.js" defer></script></body></html>
"""

PAGE_CSS = """:root{--bg:#fff;--fg:#1d2433;--mute:#667085;--line:#d0d5dd;--panel:#f6f7f9;--hi:#2563eb;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#0f141c;--fg:#e6e9ef;--mute:#98a2b3;--line:#344054;--panel:#161d28;--hi:#6ea8ff;--bad:#f97066}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;display:grid;grid-template-columns:var(--lw,260px) minmax(0,1fr) var(--rw,360px);height:100vh;position:relative}
aside,section{overflow:auto;padding:12px 14px}aside{background:var(--panel);border-right:1px solid var(--line)}#detail{background:var(--panel);border-left:1px solid var(--line)}
h1{font-size:16px;margin:0 0 4px}h2{font-size:13px;margin:14px 0 6px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
.banner{font-size:12px;color:var(--mute);margin-bottom:8px}label{display:flex;gap:6px;align-items:center;margin:3px 0;cursor:pointer}
input[type=search]{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.g-config_data{background:#b4c3d9}.g-reporting_ops{background:#c8bfd9}.g-broker_market{background:#b3cfc6}.g-research{background:#d9c9b3}.g-utilities{background:#cbd5e1}.g-unsorted{background:#e2e8f0}.subs{margin:0 0 4px 22px;font-size:12px;color:var(--mute)}.subs div{display:flex;gap:6px;align-items:center;margin:1px 0}.lane.sub{font-size:10px;font-weight:500}
.gap{margin-top:12px}.c-plan_first{background:#2563eb}.c-backup_executor{background:#7c3aed}.c-entry_points{background:#d97706}.c-money_risk{background:#059669}.c-reliability{background:#0891b2}.c-console{background:#98a2b3}.c-other{background:#cbd5e1}
.sw{width:10px;height:10px;border-radius:50%;display:inline-block}
svg{width:100%;display:block}.node{cursor:pointer}.node:focus{outline:none}.node:focus rect:first-child{stroke:var(--hi);stroke-width:3}.node text{font-size:11px;fill:var(--fg);pointer-events:none}
.node rect{stroke:var(--line);stroke-width:1}.node.sel rect{stroke:var(--hi);stroke-width:2.5}.node.dim{opacity:.18}.node.unref rect{stroke-dasharray:3 2}
.edge{fill:none;stroke:var(--mute);stroke-opacity:.35}.edge.hot{stroke:var(--hi);stroke-opacity:.9;stroke-width:1.6}.edge.priv{stroke:var(--bad);stroke-opacity:.9}
.lane{font-size:12px;font-weight:600;fill:var(--mute)}
table{border-collapse:collapse;width:100%;font-size:12px}td,th{border-bottom:1px solid var(--line);padding:3px 4px;text-align:left;vertical-align:top}
code{font:12px ui-monospace,monospace}.bag{color:var(--bad)}.pill{font-size:11px;border:1px solid var(--line);border-radius:10px;padding:0 6px;margin-left:4px}
button{border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:6px;padding:4px 8px;cursor:pointer}a.lk,button.lk{border:0;background:none;padding:0;font:inherit;color:var(--hi);cursor:pointer;text-decoration:underline}
/* Sidebar drag handles: a 10px hit area centred on each column edge, a 2px visible rule on hover/focus/drag. */
.grip{position:absolute;top:0;bottom:0;width:10px;margin-left:-5px;cursor:col-resize;z-index:5;touch-action:none}
#grip-l{left:var(--lw,260px)}#grip-r{left:calc(100% - var(--rw,360px))}
.grip::after{content:"";position:absolute;top:0;bottom:0;left:4px;width:2px;background:transparent}
.grip:hover::after,.grip:focus-visible::after,.grip.drag::after{background:var(--hi)}.grip:focus{outline:none}
body.resizing{cursor:col-resize;user-select:none}
.plain{background:var(--bg);border:1px solid var(--line);border-left:3px solid var(--hi);border-radius:6px;padding:8px 10px;margin:8px 0 4px;font-size:13px}.plain h2{margin:8px 0 4px}.plain h2:first-child{margin-top:0}.plain p{margin:0 0 4px}.plain ul{margin:2px 0 6px;padding-left:18px}.plain li{margin:2px 0}.plain .why{color:var(--mute)}#detail td,#detail th{overflow-wrap:anywhere}
@media (max-width:900px){body{grid-template-columns:1fr;height:auto}.grip{display:none}}
"""

PAGE_JS = r"""function load(){fetch('/api/diagnostics/architecture',{credentials:'same-origin'}).then(r=>r.json()).then(DATA=>{if(DATA.status==='building'){document.getElementById('detail').textContent='Scanning the source tree\u2026';setTimeout(load,800);return}if(DATA.status!=='ready'){document.getElementById('detail').textContent='Scan failed: '+(DATA.error||'unknown');return}
const detailBox=document.getElementById('detail');if(detailBox.textContent.startsWith('Scanning'))detailBox.innerHTML='<div class="banner">Click a module to see its plain-English summary, classes, fields, functions and who connects to it.</div>';
const COLORS = {plan_first:"#2563eb",backup_executor:"#7c3aed",entry_points:"#d97706",money_risk:"#059669",reliability:"#0891b2",console:"#98a2b3",other:"#cbd5e1"};
const GCOLORS = {config_data:"#b4c3d9",reporting_ops:"#c8bfd9",broker_market:"#b3cfc6",research:"#d9c9b3",utilities:"#cbd5e1",unsorted:"#e2e8f0"};
const GROUPS = DATA.groups||[];const gIndex=m=>GROUPS.findIndex(g=>g.id===m.group);const gLabel=id=>(GROUPS.find(g=>g.id===id)||{}).label||id;
const colorOf=m=>(m.group&&GCOLORS[m.group])||COLORS[m.layer];
const LABELS = {plan_first:"Plan-first (new objects)",backup_executor:"Backup / executor",entry_points:"Entry points (legacy)",money_risk:"Money & risk",reliability:"Reliability",console:"Console",other:"Other"};
const mods = new Map(DATA.modules.map(m=>[m.id,m])); let selected=null,refocus=false; const visible=new Set(DATA.layers.filter(l=>l!=="console"&&l!=="other"));
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function legend(){const box=document.getElementById("layers");box.innerHTML="";for(const l of DATA.layers){const n=DATA.modules.filter(m=>m.layer===l).length;if(!n)continue;
 const lab=document.createElement("label");lab.innerHTML=`<input type="checkbox" ${visible.has(l)?"checked":""}><span class="sw c-${l}"></span>${LABELS[l]} (${n})`;
 lab.firstChild.onchange=e=>{e.target.checked?visible.add(l):visible.delete(l);draw()};box.appendChild(lab);
 if(l==="other"&&GROUPS.length){const subs=document.createElement("div");subs.className="subs";subs.innerHTML=GROUPS.map(g=>`<div><span class="sw g-${g.id}"></span>${esc(g.label)} (${DATA.modules.filter(m=>m.group===g.id).length})</div>`).join("");box.appendChild(subs)}}}
function stats(){const s=DATA.stats;document.getElementById("stats").innerHTML=`<table>${Object.entries(s).map(([k,v])=>`<tr><td>${k.replace(/_/g," ")}</td><td><b>${v}</b></td></tr>`).join("")}</table>`}
function problems(){const priv=DATA.edges.filter(e=>e.private.length);const unref=DATA.modules.filter(m=>m.unreferenced&&m.layer!=="console");
 document.getElementById("problems").innerHTML=`<b>Private cross-module imports (${priv.length})</b><br>`+priv.slice(0,25).map(e=>`<button type="button" class="lk" data-m="${e.from}">${esc(e.from)}</button> &rarr; ${esc(e.to)}: <code>${esc(e.private.join(", "))}</code><br>`).join("")+
 `<br><b>Not imported and no CLI (${unref.length})</b><br>`+unref.slice(0,40).map(m=>`<button type="button" class="lk" data-m="${m.id}">${esc(m.id)}</button><br>`).join("")+`<div class="banner">No importer is not always dead code: external agent sessions call some modules (see CLAUDE.md).</div>`;
 document.querySelectorAll("#problems button[data-m]").forEach(a=>a.onclick=()=>select(a.dataset.m))}
function visibleMods(){const q=document.getElementById("q").value.trim().toLowerCase();
 return DATA.modules.filter(m=>visible.has(m.layer)).map(m=>({m,hit:!q||m.id.toLowerCase().includes(q)||m.classes.some(c=>c.name.toLowerCase().includes(q))||m.functions.some(f=>f.name.toLowerCase().includes(q))}))}
function draw(){const svg=document.getElementById("svg");const rows=visibleMods();const lanes=DATA.layers.filter(l=>visible.has(l));const W=1000,colW=W/Math.max(lanes.length,1),H=22;
 const pos=new Map();let maxY=60;const subheads=[];
 lanes.forEach((l,i)=>{const items=rows.filter(r=>r.m.layer===l).sort((a,b)=>(gIndex(a.m)-gIndex(b.m))||b.m.lines-a.m.lines);let slot=0,prev=null;items.forEach(r=>{
  if(l==="other"&&r.m.group&&r.m.group!==prev){prev=r.m.group;subheads.push({x:i*colW+8,y:40+slot*(H+4)+14,label:gLabel(r.m.group)});slot++}
  pos.set(r.m.id,{x:i*colW+8,y:40+slot*(H+4),w:colW-16,r});slot++;maxY=Math.max(maxY,40+slot*(H+4))})});
 svg.setAttribute("viewBox",`0 0 ${W} ${maxY+20}`);let h="";lanes.forEach((l,i)=>{h+=`<text class="lane" x="${i*colW+8}" y="22">${LABELS[l]}</text>`});subheads.forEach(t=>{h+=`<text class="lane sub" x="${t.x}" y="${t.y}">${esc(t.label)}</text>`});
 const only=document.getElementById("onlyhot").checked,po=document.getElementById("privonly").checked;
 for(const e of DATA.edges){const a=pos.get(e.from),b=pos.get(e.to);if(!a||!b)continue;const hot=selected&&(e.from===selected||e.to===selected);if(only&&selected&&!hot)continue;
  const x1=a.x+a.w,y1=a.y+H/2,x2=b.x,y2=b.y+H/2,back=b.x<=a.x;const cls=`edge${hot?" hot":""}${po&&e.private.length?" priv":""}`;
  h+=back?`<path class="${cls}" d="M${a.x+a.w/2},${a.y+H} C${a.x+a.w/2},${a.y+H+40} ${b.x+b.w/2},${b.y-40} ${b.x+b.w/2},${b.y}"/>`:`<path class="${cls}" d="M${x1},${y1} C${x1+60},${y1} ${x2-60},${y2} ${x2},${y2}"/>`}
 for(const [id,p] of pos){const m=p.r.m;const w=Math.max(4,Math.min(p.w,p.w*Math.sqrt(m.lines/6000)));h+=`<g tabindex="0" role="button" aria-label="${esc(id)}, ${m.lines} lines" class="node${selected===id?" sel":""}${p.r.hit?"":" dim"}${m.unreferenced?" unref":""}" data-id="${id}"><rect x="${p.x}" y="${p.y}" width="${p.w}" height="${H}" rx="4" fill="${colorOf(m)}" fill-opacity=".22"/><rect x="${p.x}" y="${p.y}" width="${w}" height="${H}" rx="4" fill="${colorOf(m)}" fill-opacity=".55" stroke="none"/><text x="${p.x+5}" y="${p.y+15}">${esc(id.replace("scripts.","s."))}</text></g>`}
 svg.innerHTML=h;svg.querySelectorAll(".node").forEach(n=>{n.onclick=()=>select(n.dataset.id);n.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();select(n.dataset.id)}}});
 if(selected&&refocus){const f=svg.querySelector('.node[data-id="'+selected+'"]');if(f)f.focus()}
 const pick=document.getElementById("pick");const opt=m=>`<option value="${m.id}"${m.id===selected?" selected":""}>${esc(m.id)}${visible.has(m.layer)?"":" (hidden layer)"}</option>`;
 const sections=[];DATA.layers.forEach(l=>{if(l==="other"&&GROUPS.length)GROUPS.forEach(g=>sections.push([`${LABELS.other} \u00b7 ${g.label}`,DATA.modules.filter(m=>m.group===g.id)]));else sections.push([LABELS[l],DATA.modules.filter(m=>m.layer===l)])});
 pick.innerHTML='<option value="">Choose&hellip;</option>'+sections.filter(x=>x[1].length).map(([label,list])=>`<optgroup label="${esc(label)}">${list.map(opt).join("")}</optgroup>`).join("")}
const sig=x=>`${esc(x.name)}(${x.params.map(p=>`<span class="${p.bag?"bag":""}">${esc(p.name)}${p.type?": "+esc(p.type):""}</span>`).join(", ")})`;
const LAYER_PLAIN={plan_first:"the newer plan-first design, where the program decides what to buy and a simple agent only places the orders",backup_executor:"the backup system that steps in when the Mac's normal run cannot",entry_points:"the main buying runners",money_risk:"the money and risk controls",reliability:"the safety layer that stops duplicate, racing or stale runs",console:"the local control panel",other:"the general helpers and tools"};
const short=t=>{if(!t)return"";const first=t.split(/(?<=[.!?])\s/)[0];return first.length>150?first.slice(0,147)+"...":first};
const plural=(n,w)=>`${n} ${w}${n===1?"":"s"}`;
function plainBox(id,m,out,inn){
 const note=t=>short((mods.get(t)||{}).plain)||"(no note yet)";
 const CAP=12;
 const rows=(edges,key,takes)=>{const shown=edges.slice(0,CAP).map(e=>`<li><button type="button" class="lk" data-m="${esc(e[key])}">${esc(e[key])}</button> &mdash; ${esc(note(e[key]))}${takes&&e.names&&e.names.length?` <span class="why">(it takes: ${e.names.map(n=>`<code>${esc(n)}</code>`).join(", ")})</span>`:""}</li>`).join("");
  return `<ul>${shown}${edges.length>CAP?`<li class="why">&hellip;and ${edges.length-CAP} more, listed below.</li>`:""}</ul>`};
 const uses=out.length?`<p>It relies on ${plural(out.length,"other file")}:</p>${rows(out,"to",true)}`:`<p>It does not rely on any other file in this repo, so it works on its own.</p>`;
 const usedBy=inn.length?`<p>${plural(inn.length,"file")} rely on it:</p>${rows(inn,"from",false)}`:`<p>${m.has_main?"Nothing imports it, but you can run it directly from the command line.":"Nothing imports it and it has no command line, so it is either unused or run only from outside this repo."}</p>`;
 return `<div class="plain"><h2>In plain English</h2><p>${esc(m.plain||"No plain-English note has been written for this file yet.")}</p>
 <p class="why">It is part of ${LAYER_PLAIN[m.layer]||"the repo"}.</p><h2>What it connects to</h2>${uses}<h2>What connects to it</h2>${usedBy}</div>`}
function select(id){const a=document.activeElement;refocus=!!(a&&a.closest&&a.closest("svg"));selected=id;
 const layer=(mods.get(id)||{}).layer;if(layer&&!visible.has(layer)){visible.add(layer);legend()}draw();refocus=false;const m=mods.get(id);if(!m)return;
 const out=DATA.edges.filter(e=>e.from===id),inn=DATA.edges.filter(e=>e.to===id);
 const lnk=l=>l.map(e=>`<button type="button" class="lk" data-m="${e}">${esc(e)}</button>`).join(", ")||"<i>none</i>";
 const names=m.classes.map(c=>`<code>${esc(c.name)}</code><span class="pill">${c.kind}</span>`).join(" ")||"<i>none</i>";
 document.getElementById("detail").innerHTML=`<h1>${esc(id)}</h1><div class="banner">${LABELS[m.layer]}${m.group?" &middot; "+esc(gLabel(m.group)):""} &middot; ${m.lines} lines${m.unreferenced?" &middot; not imported, no CLI":""}</div>
 ${plainBox(id,m,out,inn)}
 <h2>Imports (${out.length})</h2>${lnk(out.map(e=>e.to))}<h2>Imported by (${inn.length})</h2>${lnk(inn.map(e=>e.from))}
 <h2>Classes (${m.classes.length})</h2>${names}<div id="more" class="banner">Loading fields, signatures and calls&hellip;</div>`;
 document.querySelectorAll("#detail button[data-m]").forEach(a=>a.onclick=()=>select(a.dataset.m));
 fetch('/api/diagnostics/architecture/module?module='+encodeURIComponent(id),{credentials:'same-origin'}).then(r=>r.json()).then(d=>{if(selected!==id)return;const box=document.getElementById('more');if(!d||!Array.isArray(d.classes)){box.textContent='Details unavailable'+(d&&d.error?': '+d.error:'')+'. Reload the page; if this server was restarted, the old tab can no longer ask it.';return}
  const cls=d.classes.map(c=>`<h2>${esc(c.name)} <span class="pill">${c.kind}</span></h2>`+(c.fields.length?`<table>${c.fields.map(f=>`<tr><td><code>${esc(f.name)}</code></td><td><code class="${f.bag?"bag":""}">${esc(f.type)}</code></td></tr>`).join("")}</table>`:"")+
   (c.methods.length?`<table>${c.methods.filter(x=>!x.name.startsWith("__")).map(x=>`<tr><td><code>${sig(x)}</code>${x.doc?`<div class="banner">${esc(x.doc)}</div>`:""}</td><td>${esc(x.returns)}</td></tr>`).join("")}</table>`:"")).join("");
  const fns=d.functions.length?`<h2>Functions (${d.functions.length})</h2><table>${d.functions.map(f=>`<tr><td><code>${sig(f)}</code>${f.doc?`<div class="banner">${esc(f.doc)}</div>`:""}</td><td>${esc(f.returns)}</td></tr>`).join("")}</table>`:"";
  const callTable=(title,list)=>list.length?`<h2>${title} (${list.length})</h2><table>${list.map(c=>`<tr><td><code>${esc(c.from)}</code></td><td>&rarr; <code>${esc(c.to)}</code></td></tr>`).join("")}</table>`:"";
  const cl=callTable("Calls into other modules",d.calls.filter(c=>!c.local))+callTable("Calls within this module",d.calls.filter(c=>c.local));
  box.outerHTML=cls+fns+cl}).catch(()=>{})}
document.getElementById("pick").onchange=e=>{if(e.target.value)select(e.target.value)};document.getElementById("q").oninput=draw;document.getElementById("onlyhot").onchange=draw;document.getElementById("privonly").onchange=draw;
legend();stats();problems();draw();

}).catch(e=>{document.getElementById('detail').textContent='Could not load the scan: '+e})}
load();
(function(){
const root=document.documentElement,MIN=180,KEY="arch-sidebar-widths";
const cfg={l:{grip:document.getElementById("grip-l"),prop:"--lw",def:260},r:{grip:document.getElementById("grip-r"),prop:"--rw",def:360}};
const width={l:cfg.l.def,r:cfg.r.def};
function limit(side,w){const other=side==="l"?width.r:width.l;return Math.max(MIN,Math.min(w,window.innerWidth-other-MIN))}
function apply(side,w){width[side]=limit(side,w);root.style.setProperty(cfg[side].prop,width[side]+"px")}
function save(){try{localStorage.setItem(KEY,JSON.stringify(width))}catch(e){}}
try{const saved=JSON.parse(localStorage.getItem(KEY)||"{}");if(saved.l)apply("l",saved.l);if(saved.r)apply("r",saved.r)}catch(e){}
["l","r"].forEach(side=>{const g=cfg[side].grip;if(!g)return;
 g.addEventListener("pointerdown",e=>{e.preventDefault();g.setPointerCapture(e.pointerId);g.classList.add("drag");document.body.classList.add("resizing")});
 g.addEventListener("pointermove",e=>{if(!g.classList.contains("drag"))return;apply(side,side==="l"?e.clientX:window.innerWidth-e.clientX)});
 const stop=()=>{if(!g.classList.contains("drag"))return;g.classList.remove("drag");document.body.classList.remove("resizing");save()};
 g.addEventListener("pointerup",stop);g.addEventListener("pointercancel",stop);
 g.addEventListener("dblclick",()=>{apply(side,cfg[side].def);save()});
 g.addEventListener("keydown",e=>{const dir=e.key==="ArrowLeft"?-1:e.key==="ArrowRight"?1:0;if(!dir)return;e.preventDefault();
  apply(side,width[side]+(side==="l"?dir:-dir)*(e.shiftKey?10:40));save()});
});
window.addEventListener("resize",()=>{apply("l",width.l);apply("r",width.r)});
})();
"""


def render_page() -> bytes:
    """The static HTML shell; data comes from ``/api/diagnostics/architecture`` so no inline script is needed."""
    return PAGE_HTML.encode("utf-8")
