#!/usr/bin/env python3
"""Regenerate ``docs/module_graph.md``: the live import graph of the core modules.

WHY THIS EXISTS
---------------
``docs/codebase_roadmap.md`` was built once from an ad-hoc AST scan, and ``docs/repo_map.md`` carries
hand-typed line counts, so both describe the program as it was the day they were written. This script
is the repeatable version of that scan: it reads every core Python file, resolves imports between them,
and writes a deterministic table (no timestamp, so the file only changes when the code does). A workflow
(``.github/workflows/refresh-generated-docs.yml``) re-runs it on every push to ``main`` and opens a
draft PR when the committed copy has drifted.

    python -m scripts.generate_module_graph                 # rewrite docs/module_graph.md
    python -m scripts.generate_module_graph --check         # exit 1 if the committed copy is stale
    python -m scripts.generate_module_graph --impact dca_cumulative
                                                            # who breaks if this module changes?

Like the roadmap it cannot see string dispatch (``importlib``, prompts that name a script), so a module
with no in-repo importer is a lead, not a verdict: the "external refs" column counts mentions in the
prompt mirror, workflows, ``pyproject.toml`` and config, which is how the live agent entry points show up.
Standard library only; never imports the modules it scans.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "module_graph.md"

# Core scan roots. Top-level ``*.py`` plus these packages; tests and the independent research trees
# (own deps, own conventions, excluded from the core ruff/coverage gate) are deliberately out.
CORE_PACKAGES = ("scripts", "all_stock_dca", "guardrails", "selfheal", "viz")
EXCLUDED_PARTS = {"tests", "__pycache__", ".venv", "venv", "build", "node_modules"}
# Places that reach a module without importing it: live prompts, workflows, packaging, config, console.
REFERENCE_ROOTS = ("claude", ".github", "config", "console", "deploy", "agent-vault")
REFERENCE_FILES = ("pyproject.toml",)
REFERENCE_SUFFIXES = {".txt", ".md", ".yml", ".yaml", ".json", ".toml", ".sh", ".plist", ".html", ".js"}


def discover_modules(root: Path = REPO_ROOT) -> dict[str, Path]:
    """Map dotted module name -> file for every core module (sorted, so output is deterministic)."""
    found: dict[str, Path] = {}
    candidates = list(root.glob("*.py"))
    for pkg in CORE_PACKAGES:
        candidates.extend((root / pkg).rglob("*.py"))
    for path in candidates:
        rel = path.relative_to(root)
        if EXCLUDED_PARTS & set(rel.parts) or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            found[".".join(parts)] = path
    return dict(sorted(found.items()))


def _resolve(name: str, modules: dict[str, Path]) -> str | None:
    """Longest known module that ``name`` names or lives under (``scripts.x.y`` -> ``scripts.x``)."""
    parts = name.split(".")
    while parts:
        cand = ".".join(parts)
        if cand in modules:
            return cand
        parts.pop()
    return None


def imports_of(module: str, path: Path, modules: dict[str, Path]) -> set[str]:
    """In-repo modules ``module`` imports (absolute, relative, and ``from pkg import submodule``)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    is_pkg = path.name == "__init__.py"
    package = module if is_pkg else module.rpartition(".")[0]
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hit = _resolve(alias.name, modules)
                if hit:
                    deps.add(hit)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".") if package else []
                if node.level > 1:
                    base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            hit = _resolve(base, modules) if base else None
            subs = {  # ``from scripts import run_ledger`` imports a submodule, not just the package
                sub
                for alias in node.names
                if (sub := _resolve(f"{base}.{alias.name}" if base else alias.name, modules)) and sub != hit
            }
            deps.update(subs)
            if hit and not subs:
                deps.add(hit)
    deps.discard(module)
    return deps


def build_graph(root: Path = REPO_ROOT) -> tuple[dict[str, Path], dict[str, set[str]], dict[str, set[str]]]:
    modules = discover_modules(root)
    forward = {m: imports_of(m, p, modules) for m, p in modules.items()}
    reverse: dict[str, set[str]] = {m: set() for m in modules}
    for m, deps in forward.items():
        for d in deps:
            reverse[d].add(m)
    return modules, forward, reverse


def impact(target: str, reverse: dict[str, set[str]]) -> list[tuple[int, str]]:
    """Transitive dependents of ``target`` as (distance, module), nearest first."""
    seen = {target: 0}
    queue: deque[str] = deque([target])
    while queue:
        cur = queue.popleft()
        for dep in sorted(reverse.get(cur, ())):
            if dep not in seen:
                seen[dep] = seen[cur] + 1
                queue.append(dep)
    return sorted(((d, m) for m, d in seen.items() if m != target))


def external_reference_counts(modules: dict[str, Path], root: Path = REPO_ROOT) -> dict[str, int]:
    """Count non-Python mentions of each module's file stem (prompts, workflows, config, packaging)."""
    texts: list[str] = []
    files: list[Path] = [root / f for f in REFERENCE_FILES if (root / f).exists()]
    for ref in REFERENCE_ROOTS:
        base = root / ref
        if base.is_dir():
            files.extend(p for p in base.rglob("*") if p.is_file() and p.suffix in REFERENCE_SUFFIXES)
    for p in sorted(files):
        if EXCLUDED_PARTS & set(p.relative_to(root).parts) or "reports" in p.relative_to(root).parts:
            continue
        try:
            texts.append(p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue
    corpus = "\n".join(texts)
    counts: dict[str, int] = {}
    for mod in modules:
        stem = re.escape(mod.rsplit(".", 1)[-1])
        # Only file-name (``stem.py``) or ``python -m pkg.stem`` mentions: a bare-word match counts the
        # English word for modules like ``from.py`` (2,358 false hits).
        counts[mod] = len(re.findall(rf"(?<![\w]){stem}\.py\b|-m\s+[\w.]*{stem}\b", corpus))
    return counts


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (UnicodeDecodeError, OSError):
        return 0


def render(root: Path = REPO_ROOT) -> str:
    modules, forward, reverse = build_graph(root)
    refs = external_reference_counts(modules, root)
    edges = sum(len(v) for v in forward.values())
    orphans = [m for m in modules if not reverse[m] and not m.endswith("__init__")]
    lines = [
        "# Module graph (generated)",
        "",
        "Do not edit by hand. Regenerate with `python -m scripts.generate_module_graph`; "
        "`--check` exits 1 when this copy is stale and `--impact <module>` lists everything that depends "
        "on a module. A workflow refreshes this file on every push to `main`.",
        "",
        f"{len(modules)} core modules, {edges} in-repo import edges. Scope: top-level `*.py` plus "
        f"{', '.join(f'`{p}/`' for p in CORE_PACKAGES)}; tests and the independent research trees are excluded. "
        "Static imports only: string dispatch is invisible, so \"no importer\" is a lead, not a verdict "
        "(see the external-refs column).",
        "",
        "## Most depended-on modules (change these with care)",
        "",
        "| Module | Imported by | Direct importers |",
        "| --- | ---: | --- |",
    ]
    for mod in sorted(modules, key=lambda m: (-len(reverse[m]), m))[:15]:
        who = sorted(reverse[mod])
        shown = ", ".join(f"`{w}`" for w in who[:6]) + (f", +{len(who) - 6} more" if len(who) > 6 else "")
        lines.append(f"| `{mod}` | {len(who)} | {shown} |")
    lines += [
        "",
        "## No in-repo importer",
        "",
        "Entry points (CLIs, executor classes driven by an external agent) land here by design. "
        "`External refs` counts mentions outside Python (live prompts, workflows, `pyproject.toml`, config); "
        "0 refs and 0 importers is a dead-code candidate.",
        "",
        "| Module | Lines | External refs |",
        "| --- | ---: | ---: |",
    ]
    for mod in orphans:
        lines.append(f"| `{mod}` | {_line_count(modules[mod])} | {refs[mod]} |")
    lines += [
        "",
        "## All modules",
        "",
        "| Module | Lines | Imports | Imported by | External refs | Imports (in-repo) |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for mod, path in modules.items():
        deps = sorted(forward[mod])
        shown = ", ".join(f"`{d}`" for d in deps[:8]) + (f", +{len(deps) - 8} more" if len(deps) > 8 else "")
        lines.append(
            f"| `{mod}` | {_line_count(path)} | {len(deps)} | {len(reverse[mod])} | {refs[mod]} | {shown} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs/module_graph.md from the live import graph.")
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed file differs")
    parser.add_argument("--impact", metavar="MODULE", help="list modules that transitively depend on MODULE")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    if args.impact:
        modules, _forward, reverse = build_graph()
        if args.impact not in modules:
            print(f"unknown module {args.impact!r}; known: {', '.join(modules)}", file=sys.stderr)
            return 2
        for dist, mod in impact(args.impact, reverse):
            print(f"{dist}  {mod}")
        return 0

    text = render()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print(f"{args.output} is stale; run: python -m scripts.generate_module_graph", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
