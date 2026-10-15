"""Guard: no repo-held prompt may hardcode a UTC clock window.

A literal like ``14:01 UTC`` is right in EDT and an hour wrong after the
2026-11-01 fall-back, so every slot window must be derived from ET boundaries at
run time (docs/schedule_manifest.md, "DST rule"). Historical drafts/notes are
exempt; this covers the prompt files the schedulers actually load.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_GLOBS = (
    "claude/dca-executor-prompts/live-mirror/[0-9]*.txt",
    "claude/dca-executor-prompts/drafts/*.draft.txt",
    "claude/perplexity-prompts/*.txt",
)
# A wall-clock time in the 13:00-22:59 range tagged UTC/Z, the shape of a DST-fragile window bound.
FIXED_UTC = re.compile(r"\b(?:1[3-9]|2[0-2]):[0-5]\d(?::\d{2})?\s?(?:UTC|Z)\b")


def test_prompts_do_not_hardcode_utc_windows():
    offenders = []
    for pattern in PROMPT_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if FIXED_UTC.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert not offenders, "hardcoded UTC window literal(s): " + ", ".join(offenders)


def test_manifest_documents_dst_rule():
    text = (ROOT / "docs" / "schedule_manifest.md").read_text(encoding="utf-8")
    assert "DST rule for the Cron (UTC) column" in text
