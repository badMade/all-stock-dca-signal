"""Notion sync payload builder for the near-close graded DCA strategy.

Produces a compact markdown section titled "Latest Run — Compact Sync" from a
``latest_run_summary.json`` structure (see ``dca_reporting.build_latest_run_summary``).

This module is intentionally connector-agnostic. It does not import any Notion
client and holds no secrets, tokens, page IDs, or URLs. It emits markdown plus a
neutral payload dict that the scheduled task (which owns connector runtime access
and credentials) can hand to whatever Notion integration is wired in. The target
Notion page id is supplied by the caller/scheduler, never hard-coded here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

COMPACT_SYNC_TITLE = "Latest Run — Compact Sync"


def load_latest_run_summary(path: str | Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value}{suffix}"


def build_compact_sync_markdown(summary: Dict[str, Any]) -> str:
    """Render the compact "Latest Run — Compact Sync" markdown from a run summary."""
    cov = summary.get("coverage") or {}
    exq = summary.get("execution_quality") or {}
    budget = summary.get("budget") or {}
    manual = summary.get("manual_review_tickers") or []

    lines = []
    lines.append(f"## {COMPACT_SYNC_TITLE}")
    lines.append("")
    lines.append(
        f"**Run** `{_fmt(summary.get('run_id'))}` · "
        f"{_fmt(summary.get('trading_day'))} · "
        f"broker: {_fmt(summary.get('broker'))} · "
        f"dry_run: {_fmt(summary.get('dry_run'))}"
    )
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Buy candidates | {_fmt(cov.get('buy_candidates'))} |")
    lines.append(f"| Fills / coverage | {_fmt(cov.get('fills'))} / {_fmt(cov.get('coverage_pct'), '%')} |")
    lines.append(f"| Missing fills | {_fmt(cov.get('missing_fills'))} |")
    lines.append(f"| Avg slippage | {_fmt(exq.get('avg_slippage_pct'), '%')} |")
    lines.append(f"| Max slippage | {_fmt(exq.get('max_slippage_pct'), '%')} |")
    lines.append(f"| target_price_missing | {_fmt(exq.get('target_price_missing'))} |")
    lines.append(f"| Worst ticker | {_fmt(exq.get('worst_ticker'))} |")
    lines.append(f"| Spent today | {_fmt(budget.get('day_notional_usd'), ' USD')} |")
    lines.append(f"| Planned notional | {_fmt(budget.get('planned_notional_usd'), ' USD')} |")
    lines.append(f"| Reserve carry-forward | {_fmt(budget.get('carry_forward_reserve_usd'), ' USD')} |")
    lines.append(f"| Week notional / cap | {_fmt(budget.get('week_notional_usd'), ' USD')} / {_fmt(budget.get('weekly_notional_cap_usd'), ' USD')} |")
    lines.append("")
    if manual:
        lines.append(f"**Manual review:** {', '.join(manual)}")
    else:
        lines.append("**Manual review:** none")
    lines.append("")
    return "\n".join(lines)


def build_notion_sync_payload(summary: Dict[str, Any], page_id: str | None = None) -> Dict[str, Any]:
    """Build a connector-agnostic sync payload the scheduler can pass to Notion.

    ``page_id`` is supplied by the caller (from the scheduler's own config), never
    embedded here. The payload carries the rendered markdown block plus the compact
    metrics so the scheduled task can append/replace the section however it likes.
    """
    return {
        "title": COMPACT_SYNC_TITLE,
        "page_id": page_id,
        "trading_day": summary.get("trading_day"),
        "run_id": summary.get("run_id"),
        "markdown": build_compact_sync_markdown(summary),
        "metrics": {
            "coverage": summary.get("coverage", {}),
            "execution_quality": summary.get("execution_quality", {}),
            "budget": summary.get("budget", {}),
        },
    }


def write_compact_sync(summary: Dict[str, Any], output_dir: str | Path) -> Dict[str, str]:
    """Write the compact sync markdown and payload JSON; return their paths."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "notion_latest_run_compact.md"
    md_path.write_text(build_compact_sync_markdown(summary))
    payload_path = out / "notion_latest_run_payload.json"
    payload_path.write_text(json.dumps(build_notion_sync_payload(summary), indent=2, sort_keys=True))
    return {"markdown": str(md_path), "payload": str(payload_path)}
