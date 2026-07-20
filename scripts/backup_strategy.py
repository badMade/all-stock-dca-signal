#!/usr/bin/env python3
"""Extract graded DCA strategy state from a conversation/export transcript.

Parses a plain-text or markdown transcript and pulls out the current:
  * ticker universe (active, excluded, symbol corrections)
  * graded sizing ladder (drop-percent tiers -> buy multiplier)
  * budget / reserve rules
  * weekday schedule logic (cron -> ET, task ids)

then writes/updates the config JSON files under a target directory. It is a
reusable backup utility: run it again whenever the strategy changes.

Design notes:
  * Standard library only. No secrets, tokens, or network calls.
  * Robust to imperfect transcripts: every extractor degrades gracefully and
    returns whatever it can find rather than raising.
  * Dry-run by default; pass --write to persist files.

Usage:
    python scripts/backup_strategy.py --transcript thread.md
    python scripts/backup_strategy.py --transcript thread.md --write --out-dir config
    python scripts/backup_strategy.py --transcript thread.md --only tickers ladder
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Tokens that look like tickers but are noise we never want in the universe.
_TICKER_STOPWORDS = {
    "ET", "UTC", "EDT", "EST", "USD", "PM", "AM", "DCA", "API", "JSON", "YAML",
    "URL", "PR", "CLI", "CSV", "GFD", "ID", "NO", "OK", "TBD", "N", "A",
}

_MULTIPLIER_WORD = {"x": 1}


def _clean_symbol(sym: str) -> str:
    return sym.strip().upper().strip(".,;:")


def extract_tickers(text: str) -> Dict[str, object]:
    """Extract active/excluded tickers and symbol corrections.

    Strategy: locate the "universe" line and read the comma-separated symbols
    that follow it; also honor an explicit "Excluded:" list and
    "X -> Y" correction pairs anywhere in the text.
    """
    active: List[str] = []
    excluded: List[str] = []
    corrections: Dict[str, str] = {}

    # Symbol corrections: "IRDMI -> IRDM", "GLWR -> GLW".
    for m in re.finditer(r"\b([A-Z]{2,6})\s*-+>\s*([A-Z]{1,6})\b", text):
        corrections[m.group(1)] = m.group(2)

    # Excluded list: "Excluded: BYDDY, FOO" (single line, does not cross newline).
    for m in re.finditer(r"[Ee]xcluded[:\s]*([A-Z0-9., \t]+)", text):
        chunk = m.group(1)
        for sym in re.split(r"[,\s]+", chunk):
            sym = _clean_symbol(sym)
            if sym and sym not in _TICKER_STOPWORDS and re.fullmatch(r"[A-Z.]{1,6}", sym):
                excluded.append(sym)
            else:
                break  # stop at first non-symbol token to avoid eating prose

    # Active universe: prefer an explicit "universe" block (which may span
    # several lines), else the densest run of comma-separated symbols.
    universe_block = None
    m = re.search(r"(?:active\s+)?(?:ticker\s+)?universe[:\s]+", text, re.IGNORECASE)
    if m:
        rest = text[m.end():]
        # Stop at a blank line or an "Excluded"/"Corrected" marker.
        stop = re.search(r"\n\s*\n|[Ee]xcluded|[Cc]orrected", rest)
        universe_block = rest[: stop.start()] if stop else rest
    candidate = universe_block if universe_block else _densest_symbol_run(text)
    if candidate:
        for sym in re.split(r"[,\s]+", candidate):
            sym = _clean_symbol(sym)
            if re.fullmatch(r"[A-Z.]{1,6}", sym) and sym not in _TICKER_STOPWORDS:
                active.append(sym)

    # De-dupe preserving order; apply corrections; drop excluded symbols from active.
    active = _dedupe(active)
    excluded = _dedupe(excluded)

    # Apply symbol corrections (e.g. IRDMI -> IRDM) so the extracted universe is canonical.
    if corrections:
        active = [corrections.get(s, s) for s in active]
        excluded = [corrections.get(s, s) for s in excluded]
        active = _dedupe(active)
        excluded = _dedupe(excluded)

    excluded_set = set(excluded)
    active = [s for s in active if s not in excluded_set]

    return {"active": active, "excluded": excluded, "symbol_corrections": corrections}


def _densest_symbol_run(text: str) -> Optional[str]:
    """Return the comma-list line containing the most ticker-like tokens."""
    best = None
    best_count = 0
    for line in text.splitlines():
        if "," not in line:
            continue
        tokens = [t for t in re.split(r"[,\s]+", line) if re.fullmatch(r"[A-Z.]{1,6}", _clean_symbol(t))]
        symbolish = [t for t in tokens if _clean_symbol(t) not in _TICKER_STOPWORDS]
        if len(symbolish) > best_count:
            best_count = len(symbolish)
            best = line
    return best if best_count >= 5 else None


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def extract_sizing_ladder(text: str) -> Dict[str, object]:
    """Extract graded ladder tiers from lines like:

        drop <0.1% = 0x
        0.1% to <1% = 1x
        >=8% = 5x
    """
    tiers = []
    no_buy_below = None
    for line in text.splitlines():
        low = line.lower()
        if "%" not in low or "x" not in low:
            continue
        mult_m = re.search(r"(?:=|\bis\b|->|:|\s)\s*([0-9]+(?:\.[0-9]+)?)\s*x", low)
        if not mult_m:
            continue
        multiplier = float(mult_m.group(1))
        # Collect the percent numbers mentioned before the multiplier. Slice on
        # the multiplier's position rather than splitting on "=" (which would
        # also split the ">=" in a ">=8%" tier).
        left = low[: mult_m.start()]
        pcts = [float(p) for p in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*%", left)]
        is_ge = (">=" in left) or ("≥" in left) or ("or more" in left)
        is_lt_only = ("<" in left and "to" not in left and len(pcts) == 1)

        if is_lt_only and multiplier == 0.0:
            no_buy_below = pcts[0] if pcts else no_buy_below
            continue
        if not pcts:
            continue
        if is_ge or len(pcts) == 1:
            tiers.append({"min_drop_pct": pcts[0], "max_drop_pct": None, "multiplier": multiplier})
        else:
            lo, hi = min(pcts), max(pcts)
            tiers.append({"min_drop_pct": lo, "max_drop_pct": hi, "multiplier": multiplier})

    tiers.sort(key=lambda t: t["min_drop_pct"])
    # Fill max where a following tier starts (ranges may be given as "0.1% to <1%").
    for i in range(len(tiers) - 1):
        if tiers[i]["max_drop_pct"] is None:
            tiers[i]["max_drop_pct"] = tiers[i + 1]["min_drop_pct"]
    if no_buy_below is None and tiers:
        no_buy_below = tiers[0]["min_drop_pct"]

    return {
        "status": "active",
        "base_amount_usd": 3.0,
        "no_buy_below_pct": no_buy_below,
        "tiers": tiers,
    }


# A currency amount, tolerating thousands separators ("1,800") and decimals.
_NUMBER = r"[0-9][0-9,]*(?:\.[0-9]+)?"


def _to_float(raw: str) -> float:
    """Parse a captured numeric token, stripping any thousands separators."""
    return float(raw.replace(",", ""))


_BUDGET_PATTERNS = {
    "base_amount_usd": rf"base\s+amount[^$]*\$\s*({_NUMBER})",
    "weekly_rollover_reserve_cap_usd": rf"weekly\s+rollover\s+reserve[^$]*\$\s*({_NUMBER})",
}

# Tier lines look like "Normal: ... daily cap $180, weekly cap $900".
_TIER_PATTERN = re.compile(
    rf"\b(normal|elevated|high[- ]?volatility|stress)\b[^\n]*?"
    rf"daily\s+cap\s*\$?\s*({_NUMBER})[^\n]*?"
    rf"weekly\s+cap\s*\$?\s*({_NUMBER})",
    re.IGNORECASE,
)


def extract_budget_tiers(text: str) -> List[Dict[str, object]]:
    """Extract tiered daily/weekly caps from lines naming a tier and its caps.

    The stress tier's capacity depends on the weekly rollover reserve, so when the
    transcript mentions that reserve the ``requires_rollover_reserve`` safeguard is
    carried on the stress tier. Note that ``write_configs`` also preserves this and
    any other existing per-tier metadata when merging into an existing config.
    """
    mentions_rollover_reserve = bool(re.search(r"rollover\s+reserve", text, re.IGNORECASE))
    tiers: List[Dict[str, object]] = []
    seen: set = set()
    for m in _TIER_PATTERN.finditer(text):
        name = re.sub(r"[- ]", "_", m.group(1).lower())
        if name in seen:
            continue
        seen.add(name)
        tier: Dict[str, object] = {
            "name": name,
            "daily_cap_usd": _to_float(m.group(2)),
            "weekly_cap_usd": _to_float(m.group(3)),
        }
        if name == "stress" and mentions_rollover_reserve:
            tier["requires_rollover_reserve"] = True
        tiers.append(tier)
    return tiers


def extract_budgets(text: str) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for key, pat in _BUDGET_PATTERNS.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            out[key] = _to_float(m.group(1))
    tiers = extract_budget_tiers(text)
    if tiers:
        out["budget_tiers"] = tiers
    return out


def extract_schedule(text: str) -> Dict[str, object]:
    """Extract cron schedules and derive weekday-check ET times.

    Recognizes patterns like:
        10ce362c - live graded DCA ... schedule 20 19 * * 1-5 UTC = 3:20 PM ET weekdays
    and standalone cron expressions with ET annotations.
    """
    tasks = []
    seen_ids = set()
    seen_crons = set()
    # id + cron together on a line.
    for line in text.splitlines():
        cron_m = re.search(r"\b([0-9*/,-]+\s+[0-9*/,-]+\s+[0-9*/,-]+\s+[0-9*/,-]+\s+[0-9*/,-]+)\b", line)
        if not cron_m:
            continue
        cron = cron_m.group(1).strip()
        id_m = re.search(r"\b([0-9a-f]{8})\b", line)
        task_id = id_m.group(1) if id_m else None
        if task_id:
            if task_id in seen_ids:
                continue
            seen_ids.add(task_id)
        else:
            if cron in seen_crons:
                continue
            seen_crons.add(cron)
        et_m = re.search(r"=\s*([^=]*?ET[^=]*)", line)
        weekday = _is_weekday_cron(cron)
        tasks.append({
            "id": task_id,
            "cron_utc": cron,
            "et_equivalent": et_m.group(1).strip() if et_m else None,
            "weekday_only": weekday,
        })
    return {"tasks": tasks}


def _is_weekday_cron(cron: str) -> bool:
    parts = cron.split()
    if len(parts) != 5:
        return False
    dow = parts[4].lower()
    return dow in {"1-5", "1,2,3,4,5", "mon-fri"}


def extract_all(text: str) -> Dict[str, object]:
    return {
        "tickers": extract_tickers(text),
        "sizing_ladder": extract_sizing_ladder(text),
        "budgets": extract_budgets(text),
        "schedule": extract_schedule(text),
    }


def _is_named_dict_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and "name" in item for item in value
    )


def _merge_named_list(existing: list, incoming: list) -> list:
    """Merge two lists of dicts keyed by ``name``, preserving existing-only fields.

    Used for ``budget_tiers`` so a refresh updates the extracted caps without
    dropping metadata that lives only in the config (e.g. the stress tier's
    ``requires_rollover_reserve`` safeguard, ``note``, and ``select_when``).
    """
    order: List[str] = []
    by_name: Dict[str, dict] = {}
    for item in existing:
        by_name[item["name"]] = dict(item)
        order.append(item["name"])
    for item in incoming:
        name = item["name"]
        if name in by_name:
            by_name[name].update(item)
        else:
            by_name[name] = dict(item)
            order.append(name)
    return [by_name[name] for name in order]


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        elif _is_named_dict_list(v) and _is_named_dict_list(dst.get(k)):
            dst[k] = _merge_named_list(dst[k], v)
        else:
            dst[k] = v


def _merge_into_file(path: Path, updates: Dict[str, object]) -> None:
    """Merge extracted values into an existing config file, preserving keys."""
    existing: Dict[str, object] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = {}
    filtered_updates = {k: v for k, v in updates.items() if v not in (None, [], {})}
    _deep_merge(existing, filtered_updates)
    path.write_text(json.dumps(existing, indent=2) + "\n")


def write_configs(extracted: Dict[str, object], out_dir: Path, only: Optional[List[str]]) -> List[str]:
    written = []
    want = set(only) if only else {"tickers", "ladder", "budgets", "schedule"}

    if "tickers" in want and extracted["tickers"]["active"]:
        _merge_into_file(out_dir / "tickers.json", extracted["tickers"])
        written.append("tickers.json")
    if "ladder" in want and extracted["sizing_ladder"]["tiers"]:
        _merge_into_file(out_dir / "sizing_ladder.json", extracted["sizing_ladder"])
        written.append("sizing_ladder.json")
    if "budgets" in want and extracted["budgets"]:
        _merge_into_file(out_dir / "budgets.json", extracted["budgets"])
        written.append("budgets.json")
    if "schedule" in want and extracted["schedule"]["tasks"]:
        _merge_into_file(out_dir / "schedule_extracted.json", extracted["schedule"])
        written.append("schedule_extracted.json")
    return written


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract and back up graded DCA strategy state from a transcript.")
    p.add_argument("--transcript", required=True, type=Path, help="Path to conversation/export transcript (text or markdown).")
    p.add_argument("--out-dir", type=Path, default=Path("config"), help="Directory to write config files into.")
    p.add_argument("--write", action="store_true", help="Persist config files. Without this flag the tool only prints what it found.")
    p.add_argument("--only", nargs="+", choices=["tickers", "ladder", "budgets", "schedule"], help="Restrict extraction to specific sections.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.transcript.exists():
        print(f"error: transcript not found: {args.transcript}", file=sys.stderr)
        return 2
    text = args.transcript.read_text(errors="replace")
    extracted = extract_all(text)

    print(json.dumps(extracted, indent=2))

    if args.write:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        written = write_configs(extracted, args.out_dir, args.only)
        if written:
            print(f"\nwrote/updated: {', '.join(written)} in {args.out_dir}", file=sys.stderr)
        else:
            print("\nnothing extracted to write", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
