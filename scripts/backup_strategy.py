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
from typing import Any, Dict, List, Optional

# Tokens that look like tickers but are noise we never want in the universe.
# NONE/NULL/NA cover a common empty-category sentinel -- "Whole-share: NONE"
# (unlike the parenthesized "(none)" example elsewhere in this module, whose
# parens already fail the uppercase-symbol regex) uppercases to a bare
# "NONE" that DOES pass the same regex as a real ticker, so without this it
# gets persisted into tickers.json as a bogus symbol instead of clearing the
# category (Codex review: "Treat NONE as an empty category marker").
_TICKER_STOPWORDS = {
    "ET", "UTC", "EDT", "EST", "USD", "PM", "AM", "DCA", "API", "JSON", "YAML",
    "URL", "PR", "CLI", "CSV", "GFD", "ID", "NO", "OK", "TBD", "N", "A",
    "NONE", "NULL", "NA",
}

_MULTIPLIER_WORD = {"x": 1}


def _clean_symbol(sym: str) -> str:
    return sym.strip().upper().strip(".,;:")


def extract_tickers(text: str) -> Dict[str, Any]:
    """Extract active/excluded/whole-share tickers and symbol corrections.

    Strategy: locate the "universe" line and read the comma-separated symbols
    that follow it; also honor an explicit "Excluded:" list, a "Whole-share:"
    list, and "X -> Y" correction pairs anywhere in the text.
    """
    active: List[str] = []
    excluded: List[str] = []
    whole_share: List[str] = []
    corrections: Dict[str, str] = {}
    # Tracked separately from the lists themselves so a caller merging this
    # into an existing config file can tell "this transcript explicitly said
    # there are NONE" (a real, deliberate empty list -- e.g. the last
    # whole-share symbol moved back into active) apart from "this transcript
    # never mentioned this category at all" (an abbreviated/older transcript
    # that says nothing either way, where an existing value must be left
    # alone). Without this distinction, a transcript that simply omits a
    # "Whole-share:" section would look identical to one that explicitly
    # clears it, and restoring from the former would silently wipe out an
    # existing whole-share ticker the transcript never asked to remove
    # (Codex review: "Preserve unmentioned ticker categories during
    # transcript restores").
    # "Mentioned" is a separate, looser existence check from the
    # symbol-extraction regexes below -- the marker itself ("Excluded:"/
    # "Whole-share:") counts as a mention even when NO valid symbol follows
    # it (e.g. "Whole-share: (none)"), which is exactly how a transcript
    # explicitly clears a category. Tying "mentioned" to the same regex that
    # extracts symbols would miss that case: a transcript saying "Whole-share:
    # (none)" produces zero extracted symbols AND (with a symbol-required
    # capture group) zero regex matches, so it would be indistinguishable
    # from a transcript that never mentions whole-share at all.
    # Anchored to the literal section-header COLON, not just any trailing
    # whitespace -- "[:\s]" alone treats ordinary prose mentioning the word
    # ("BYDDY is a whole-share ticker") as an explicit section marker too
    # (the space after "share" matches "\s"), setting *_mentioned True with
    # an empty extracted list (the symbol-extraction regex below requires an
    # UPPERCASE run right after the marker, which "ticker" isn't, so nothing
    # gets extracted). write_configs would then overwrite an existing
    # whole_share/excluded membership with [] whenever such prose appears
    # alongside an active-universe block, even though the transcript never
    # intended to clear anything (Codex review: "Require explicit category
    # markers during transcript restore").
    excluded_mentioned = bool(re.search(r"[Ee]xcluded\s*:", text))
    whole_share_mentioned = bool(re.search(r"[Ww]hole[\s-]?share\s*:", text))

    # Symbol corrections: "IRDMI -> IRDM", "GLWR -> GLW".
    for m in re.finditer(r"\b([A-Z]{2,6})\s*-+>\s*([A-Z]{1,6})\b", text):
        corrections[m.group(1)] = m.group(2)

    # Excluded list: "Excluded: BYDDY, FOO" (single line, does not cross newline).
    # Requires the literal colon, matching excluded_mentioned's own anchor above --
    # [:\s]* previously also matched bare whitespace, so ordinary prose like
    # "the excluded ticker is BYDDY" (a space right after "excluded") would
    # extract "BYDDY" as if it were an explicit "Excluded:" section, even
    # when no such section exists elsewhere in the transcript (same class of
    # bug as the whole-share regex just below; Codex review: "Require a
    # colon when extracting whole-share symbols", applied here too for
    # consistency).
    for m in re.finditer(r"[Ee]xcluded\s*:\s*([A-Z0-9., \t]+)", text):
        chunk = m.group(1)
        for sym in re.split(r"[,\s]+", chunk):
            sym = _clean_symbol(sym)
            if sym and sym not in _TICKER_STOPWORDS and re.fullmatch(r"[A-Z.]{1,6}", sym):
                excluded.append(sym)
            else:
                break  # stop at first non-symbol token to avoid eating prose

    # Whole-share list: "Whole-share: BYDDY" / "Whole share: BYDDY" (same
    # single-line convention as Excluded above) -- these tickers are NOT
    # excluded from buying (see docs/whole_share_accumulation.md): they are
    # evaluated by the same graded ladder as active but bought via a
    # quantity-based order once accumulated contributions cross one share's
    # price, a third category distinct from both active and excluded (Codex
    # review: "Teach transcript backups about whole-share membership" --
    # without this, this transcript-based restore path had no way to
    # recreate/update tickers.json's "whole_share" array at all).
    # Requires the literal colon, matching whole_share_mentioned's own anchor
    # above -- [:\s]* previously also matched bare whitespace, so ordinary
    # prose like "the whole-share XYZ is special" (a space right after
    # "whole-share") would extract "XYZ" even when no explicit "Whole-share:"
    # section existed at that point in the transcript. If a REAL "Whole-share:"
    # section appeared elsewhere (setting whole_share_mentioned=True),
    # write_configs would then persist this unintended, prose-extracted
    # symbol into tickers.json, where it could enter live strategy evaluation
    # (Codex review: "Require a colon when extracting whole-share symbols").
    for m in re.finditer(r"[Ww]hole[\s-]?share\s*:\s*([A-Z0-9., \t]+)", text):
        chunk = m.group(1)
        for sym in re.split(r"[,\s]+", chunk):
            sym = _clean_symbol(sym)
            if sym and sym not in _TICKER_STOPWORDS and re.fullmatch(r"[A-Z.]{1,6}", sym):
                whole_share.append(sym)
            else:
                break  # stop at first non-symbol token to avoid eating prose

    # Active universe: prefer an explicit "universe" block (which may span
    # several lines), else the densest run of comma-separated symbols.
    universe_block = None
    m = re.search(r"(?:active\s+)?(?:ticker\s+)?universe[:\s]+", text, re.IGNORECASE)
    if m:
        rest = text[m.end():]
        # Stop at a blank line or an "Excluded"/"Whole-share"/"Corrected" marker.
        stop = re.search(r"\n\s*\n|[Ee]xcluded|[Ww]hole[\s-]?share|[Cc]orrected", rest)
        universe_block = rest[: stop.start()] if stop else rest
    candidate = universe_block if universe_block else _densest_symbol_run(text)
    if candidate:
        for sym in re.split(r"[,\s]+", candidate):
            sym = _clean_symbol(sym)
            if re.fullmatch(r"[A-Z.]{1,6}", sym) and sym not in _TICKER_STOPWORDS:
                active.append(sym)

    # De-dupe preserving order; apply corrections; drop excluded/whole-share
    # symbols from active.
    active = _dedupe(active)
    excluded = _dedupe(excluded)
    whole_share = _dedupe(whole_share)

    # Apply symbol corrections (e.g. IRDMI -> IRDM) so the extracted universe is canonical.
    if corrections:
        active = [corrections.get(s, s) for s in active]
        excluded = [corrections.get(s, s) for s in excluded]
        whole_share = [corrections.get(s, s) for s in whole_share]
        active = _dedupe(active)
        excluded = _dedupe(excluded)
        whole_share = _dedupe(whole_share)

    excluded_set = set(excluded)
    whole_share_set = set(whole_share)
    active = [s for s in active if s not in excluded_set and s not in whole_share_set]
    # A symbol explicitly listed in BOTH "Excluded:" and "Whole-share:" is a
    # genuinely contradictory transcript, not merely an active-list overlap
    # the block above already resolves -- there is no correct symbol to drop
    # here (unlike active, where excluded/whole_share are the authoritative,
    # explicitly-marked categories that always win). write_configs would
    # otherwise persist an overlapping partition straight into tickers.json,
    # which dca_config.load_strategy_config then rejects on its very next
    # load, preventing the strategy from starting entirely (Codex review:
    # "Reject overlap between restored ticker categories"). Fail loudly
    # here instead, before any file is written, so the operator fixes the
    # source transcript rather than debugging a broken live config.
    both = excluded_set & whole_share_set
    if both:
        raise ValueError(
            f"transcript lists the same symbol(s) in both 'Excluded:' and 'Whole-share:': {sorted(both)!r}"
        )

    return {
        "active": active,
        "excluded": excluded,
        "whole_share": whole_share,
        "symbol_corrections": corrections,
        # See excluded_mentioned/whole_share_mentioned's own comment above --
        # consumed by write_configs to decide whether an empty "excluded"/
        # "whole_share" list should CLEAR an existing config value (the
        # transcript explicitly said so) or be left out of the merge
        # entirely (the transcript never mentioned this category).
        "excluded_mentioned": excluded_mentioned,
        "whole_share_mentioned": whole_share_mentioned,
    }


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


def extract_sizing_ladder(text: str) -> Dict[str, Any]:
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

    # Reuse extract_budgets' own selection logic rather than hardcoding the
    # base amount, so a future base-amount change only needs updating in one
    # place.
    base_amount_usd = _select_base_amount_usd(text)

    return {
        "status": "active",
        "base_amount_usd": base_amount_usd,
        "no_buy_below_pct": no_buy_below,
        "tiers": tiers,
    }


# A currency amount, tolerating thousands separators ("1,800") and decimals.
_NUMBER = r"[0-9][0-9,]*(?:\.[0-9]+)?"


def _to_float(raw: str) -> float:
    """Parse a captured numeric token, stripping any thousands separators."""
    return float(raw.replace(",", ""))


def _first_group(m: "re.Match[str]") -> Optional[str]:
    """Return the first non-None capture group from a multi-alternative match."""
    return next((g for g in m.groups() if g is not None), None)


def _last_match_value(pattern: str, text: str) -> Optional[str]:
    """Return the first non-None capture group of the LAST match of pattern in text."""
    matches = list(re.finditer(pattern, text, re.IGNORECASE))
    return _first_group(matches[-1]) if matches else None


# An explicit "base amount ... $2" or short "base $2" declaration -- more
# deliberate, and so more authoritative, than an incidental "$2-base ladder"
# mention (which is often just describing the ladder, not stating policy).
_EXPLICIT_BASE_AMOUNT_PATTERN = (
    rf"base\s+amount[^$]*\$\s*({_NUMBER})"
    rf"|base\s*\$\s*({_NUMBER})"
)
# The common "$2-base"/"$2 base" ladder-wording ordering, used only when no
# explicit declaration is present anywhere in the transcript.
_INCIDENTAL_BASE_AMOUNT_PATTERN = rf"\$\s*({_NUMBER})[\s-]+base\b"


def _select_base_amount_usd(text: str) -> Optional[float]:
    """Find the current graded-DCA base amount in a messy transcript.

    Transcripts often carry a superseded mention (e.g. an old "$3-base
    ladder" line) ahead of the current "Base amount $2" declaration, so a
    plain re.search would return whichever comes first in the text -- wrong
    when the superseded mention comes first. Prefer an explicit declaration
    over an incidental ladder-wording mention, and within either tier prefer
    the LAST match, since later text in a running transcript/doc is more
    likely to reflect the current policy than earlier, possibly-superseded
    text.
    """
    for pattern in (_EXPLICIT_BASE_AMOUNT_PATTERN, _INCIDENTAL_BASE_AMOUNT_PATTERN):
        value = _last_match_value(pattern, text)
        if value is not None:
            return _to_float(value)
    return None


_BUDGET_PATTERNS = {
    "weekly_rollover_reserve_cap_usd": rf"weekly\s+rollover\s+reserve[^$]*\$\s*({_NUMBER})",
}

# Tier lines look like "Normal: ... daily cap $180, weekly cap $900".
_TIER_PATTERN = re.compile(
    rf"\b(normal|elevated|high[- ]?volatility|stress)\b[^\n]*?"
    rf"daily\s+cap\s*\$?\s*({_NUMBER})[^\n]*?"
    rf"weekly\s+cap\s*\$?\s*({_NUMBER})",
    re.IGNORECASE,
)


def extract_budget_tiers(text: str) -> List[Dict[str, Any]]:
    """Extract tiered daily/weekly caps from lines naming a tier and its caps.

    The stress tier's capacity depends on the weekly rollover reserve, so when the
    transcript mentions that reserve the ``requires_rollover_reserve`` safeguard is
    carried on the stress tier. Note that ``write_configs`` also preserves this and
    any other existing per-tier metadata when merging into an existing config.
    """
    mentions_rollover_reserve = bool(re.search(r"rollover\s+reserve", text, re.IGNORECASE))
    tiers: List[Dict[str, Any]] = []
    seen: set = set()
    for m in _TIER_PATTERN.finditer(text):
        name = re.sub(r"[- ]", "_", m.group(1).lower())
        if name in seen:
            continue
        seen.add(name)
        tier: Dict[str, Any] = {
            "name": name,
            "daily_cap_usd": _to_float(m.group(2)),
            "weekly_cap_usd": _to_float(m.group(3)),
        }
        if name == "stress" and mentions_rollover_reserve:
            tier["requires_rollover_reserve"] = True
        tiers.append(tier)
    return tiers


def extract_budgets(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    base_amount_usd = _select_base_amount_usd(text)
    if base_amount_usd is not None:
        out["base_amount_usd"] = base_amount_usd
    for key, pat in _BUDGET_PATTERNS.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            value = _first_group(m)
            if value is not None:
                out[key] = _to_float(value)
    tiers = extract_budget_tiers(text)
    if tiers:
        out["budget_tiers"] = tiers
    return out


def extract_schedule(text: str) -> Dict[str, Any]:
    """Extract cron schedules and derive weekday-check ET times.

    Recognizes patterns like:
        10ce362c - live graded DCA ... schedule 30 19 * * 1-5 UTC = 3:30 PM ET weekdays
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


def extract_all(text: str) -> Dict[str, Any]:
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


def _merge_into_file(
    path: Path, updates: Dict[str, Any], *, preserve_empty_list_keys: frozenset = frozenset()
) -> None:
    """Merge extracted values into an existing config file, preserving keys.

    An empty ``[]``/``{}``/``None`` value is normally filtered out here rather
    than merged -- for most extracted fields (sizing-ladder tiers, budgets,
    schedule entries) an empty value just means "the extractor found nothing
    in this transcript," not "clear the existing value," so merging it in
    would silently wipe out an existing config's real content whenever a
    transcript happened not to mention that section.

    ``preserve_empty_list_keys`` (e.g. ``{"excluded", "whole_share"}`` for
    tickers.json) opts specific keys OUT of that filtering: for a ticker
    category, an empty list IS a meaningful, deliberate signal -- "this
    transcript's universe no longer has any excluded/whole-share tickers,"
    which extract_tickers always returns explicitly (never omits the key
    outright) -- not "the extractor didn't look at this category." Without
    this, moving the LAST symbol out of `whole_share` (e.g. back into
    `active`) would leave the file's stale `whole_share` array untouched,
    potentially overlapping the newly-written `active` list and violating
    the disjointness invariant `dca_config.load_tickers` enforces (Codex
    review: "Allow transcript restores to clear whole-share membership").
    """
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_bytes())
        except json.JSONDecodeError:
            existing = {}
    filtered_updates = {
        k: v for k, v in updates.items() if v not in (None, [], {}) or k in preserve_empty_list_keys
    }
    _deep_merge(existing, filtered_updates)
    path.write_text(json.dumps(existing, indent=2) + "\n")


def _strip_moved_symbols_from_retained_categories(
    existing_path: Path,
    tickers_updates: Dict[str, Any],
    *,
    moved_from_categories: tuple,
    actively_written_categories: frozenset,
) -> Dict[str, Any]:
    """Remove symbols the transcript is moving INTO a category from every
    OTHER, retained (unmentioned) category in the existing file, so the
    merge this update feeds into can never leave the same symbol in two
    categories at once.

    ``_merge_into_file`` leaves a category NOT present in ``tickers_updates``
    completely untouched -- correct for a transcript that genuinely never
    mentions it, but wrong when the transcript instead MOVES a symbol from
    that untouched category into one it DOES mention (e.g. "Whole-share:
    AAPL" while AAPL is still sitting in the existing, retained ``active``
    list): the merge would then write AAPL into ``whole_share`` while
    leaving the stale ``active`` entry in place, violating the pairwise
    disjointness invariant ``dca_config.load_strategy_config`` enforces and
    preventing the strategy from starting on its very next load (Codex
    review: "Reconcile retained categories during partial ticker
    restores").

    ``moved_from_categories`` restricts which of ``tickers_updates``'s own
    category values are trusted as "symbols genuinely being moved" --
    deliberately EXCLUDES ``"active"``: unlike ``excluded``/``whole_share``
    (each gated on an explicit ``*_mentioned`` marker requiring a literal
    section colon), the active-universe list has no equivalent "genuinely
    mentioned" signal and is already known to over-extract capitalized
    words from ordinary prose (see ``extract_tickers``'s own
    "BYDDY is a whole-share ticker" false-positive-marker precedent).
    Sourcing moved-symbol stripping from "active" too would let that same
    over-extraction silently wipe a real, unrelated whole-share/excluded
    entry out of the retained category it was never meant to touch. Only
    the caller passes in categories that ARE reliably "mentioned".

    ``actively_written_categories`` is which of ``tickers_updates``'s keys
    will actually survive ``_merge_into_file``'s own empty-value filter
    (mirrors that filter's own ``v not in (None, [], {}) or k in
    preserve_empty_list_keys`` logic) -- ``tickers_updates`` itself always
    carries an ``"active"`` key (even an EMPTY one, when the transcript's
    active-universe section is empty/absent), so checking key presence
    alone would wrongly treat "active" as already being written and skip
    correcting it even when its empty value is about to be filtered out
    and its existing content left untouched.

    Returns extra entries (for the categories that need correcting) to
    merge alongside ``tickers_updates`` -- an empty dict when nothing needs
    correcting, or the existing file is unreadable/absent (nothing to
    reconcile against).
    """
    if not existing_path.exists():
        return {}
    try:
        existing = json.loads(existing_path.read_bytes())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(existing, dict):
        return {}
    moved_symbols: set = set()
    for category in moved_from_categories:
        value = tickers_updates.get(category)
        if isinstance(value, list):
            moved_symbols.update(s for s in value if isinstance(s, str))
    if not moved_symbols:
        return {}
    corrections: Dict[str, Any] = {}
    for category in ("active", "excluded", "whole_share"):
        if category in actively_written_categories:
            continue  # this category is already part of the update itself
        existing_value = existing.get(category)
        if not isinstance(existing_value, list):
            continue
        cleaned = [s for s in existing_value if s not in moved_symbols]
        if len(cleaned) != len(existing_value):
            corrections[category] = cleaned
    return corrections


def write_configs(extracted: Dict[str, Any], out_dir: Path, only: Optional[List[str]]) -> List[str]:
    written = []
    want = set(only) if only else {"tickers", "ladder", "budgets", "schedule"}

    if "tickers" in want and (
        extracted["tickers"]["active"]
        or extracted["tickers"]["excluded_mentioned"]
        or extracted["tickers"]["whole_share_mentioned"]
    ):
        # A transcript containing ONLY a category update (e.g. "Whole-share:
        # (none)" or "Excluded: (none)") with no active-universe section at
        # all is a supported partial-transcript case -- gating on a nonempty
        # active list alone would skip the merge entirely, leaving the
        # deliberately-cleared category stale in tickers.json even though
        # *_mentioned correctly distinguishes it from an unmentioned one
        # (Codex review: "Allow category-only transcript restores").
        #
        # excluded_mentioned/whole_share_mentioned are extract_tickers's own
        # metadata (see that function's docstring), not tickers.json content
        # -- drop them from the merge payload. A category the transcript
        # never mentioned at all is dropped from the payload ENTIRELY (not
        # just exempted from the empty-list filter) so _merge_into_file
        # leaves an existing tickers.json's value for it completely
        # untouched; only a category the transcript DID mention (even as an
        # explicit empty list) goes through preserve_empty_list_keys, so a
        # transcript that simply omits a "Whole-share:"/"Excluded:" section
        # can never be mistaken for one that deliberately clears it (Codex
        # review: "Preserve unmentioned ticker categories during transcript
        # restores").
        tickers_updates = {
            k: v for k, v in extracted["tickers"].items() if k not in ("excluded_mentioned", "whole_share_mentioned")
        }
        preserve_empty_list_keys = set()
        if extracted["tickers"]["excluded_mentioned"]:
            preserve_empty_list_keys.add("excluded")
        else:
            tickers_updates.pop("excluded", None)
        if extracted["tickers"]["whole_share_mentioned"]:
            preserve_empty_list_keys.add("whole_share")
        else:
            tickers_updates.pop("whole_share", None)
        # A symbol this transcript is moving INTO a mentioned category must
        # be removed from whichever OTHER, retained category the existing
        # file still has it in, or the merge below would leave it in BOTH
        # (Codex review: "Reconcile retained categories during partial
        # ticker restores"). preserve_empty_list_keys is extended too, so a
        # correction that empties a retained category still gets written
        # rather than being filtered out as "no update" by _merge_into_file.
        moved_from_categories = tuple(
            c for c in ("excluded", "whole_share") if c in preserve_empty_list_keys
        )
        # A category present in tickers_updates only as an empty/falsy value
        # that ISN'T in preserve_empty_list_keys is dropped by
        # _merge_into_file's own empty-value filter -- it isn't really being
        # written, so it must stay eligible for correction below (this is
        # exactly the "active" case: always a dict key, frequently an
        # over-extracted false positive, never truly "actively written"
        # unless it's actually nonempty).
        actively_written_categories = frozenset(
            k for k, v in tickers_updates.items() if v not in (None, [], {}) or k in preserve_empty_list_keys
        )
        corrections = _strip_moved_symbols_from_retained_categories(
            out_dir / "tickers.json",
            tickers_updates,
            moved_from_categories=moved_from_categories,
            actively_written_categories=actively_written_categories,
        )
        if corrections:
            tickers_updates.update(corrections)
            preserve_empty_list_keys.update(corrections.keys())
        _merge_into_file(
            out_dir / "tickers.json",
            tickers_updates,
            preserve_empty_list_keys=frozenset(preserve_empty_list_keys),
        )
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
