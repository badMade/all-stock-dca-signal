#!/usr/bin/env python3
"""Digest and track the executor gate alerts that pile up as unsent Gmail drafts.

Why this exists
---------------
Each live executor prompt (``claude/dca-executor-prompts/live-mirror/*.txt``,
Gate 9) requires an aborting slot to alert on THREE channels, in order:

1. the in-app message center (primary),
2. a durable repo alert file under ``dca_paths.stream_root(<stream>, 'alerts', ...)``,
3. a **sent email**.

Channel 3 used to read "EMAIL DRAFT", on the premise that the connector had no
send capability. That premise was wrong, and the prompts were corrected on
2026-08-26 to require a sent email (a draft is permitted only where the
connector genuinely cannot send, and then the executor must say so in the
primary alert). This module post-dates that correction and describes the
corrected contract; the corpus it reads was produced under the old one.

Under the old wording channel 3 became the only channel that survived a session,
because channels 1 and 2 leave nothing an operator can go back and read: the
message center is per-session, and executors repeatedly wrote channel 2 to a
bare repo-relative ``artifacts/alerts/`` directory, which ``.gitignore`` drops.
The visible symptom was a mailbox slowly filling with alert drafts that never
sent -- but the drafts are the *record*, not the failure. The failure is
whatever gate kept aborting, and it had been invisible.

This module turns that pile into something countable. It ingests alerts from any
surviving source -- a JSON export of Gmail drafts, an export of SENT alert
emails (what the corrected prompts produce, and invisible to a drafts export),
or a directory of repo alert files -- normalizes each into an
:class:`AlertRecord` (ET date, slot, gate, verdict), and reports:

* how often each gate fires, and per slot,
* the longest consecutive run of the same (slot, gate) failure, which is what
  distinguishes a one-off from a stuck slot,
* a day x slot coverage matrix, so a slot that stopped alerting entirely is
  visible next to one that alerts every day,
* **channel warnings** -- alerts whose own body claims a delivery channel the
  repo's config says is off, or a durable path ``.gitignore`` discards. This is
  the check that surfaces the "everything is a draft" problem itself.

Repeat runs persist a ledger under ``stream_root('alert_digest')`` recording
which alerts a previous digest already reported. The report itself still spans
the whole corpus -- streaks and the coverage matrix are meaningless without the
history -- but the ledger names the new arrivals, and ``--fail-on-warnings``
judges only those, so a scheduled check goes green once a known backlog stops
growing.

Standard library only. No network calls: this module never talks to Gmail. The
agent session holding the Gmail connector exports drafts to JSON and pipes them
in, matching how every other live path in this repo is driven from outside (see
``docs/live_wiring.md``).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from dca_config import get_config_dir
from dca_fileio import atomic_write_text
from dca_paths import stream_root

# The repo's own computed NYSE calendar, not a local copy. A roster-wide silent
# day can only be spotted against the days the executors were *expected* to run,
# and re-deriving "which days are trading days" here would be exactly the kind of
# duplicated logic that drifts out of agreement with the streams' own gates.
from near_close_dca import nyse_holidays

ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)


LEDGER_SCHEMA_VERSION = "alert-digest-v1"

# This module lives at scripts/alert_digest.py, so its own grandparent is the
# checkout root -- the directory .gitignore actually governs. Used to decide
# whether a loaded alert file's on-disk path is even IN the tree .gitignore
# applies to before treating its location as GITIGNORED_ALERT_PATH evidence.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_within_repo_checkout(path: Path) -> bool:
    """Whether ``path`` resolves to somewhere under this checkout's root.

    A durable artifact root can legitimately live OUTSIDE the repository
    entirely (``$DCA_CRON_TRACKING_ROOT`` defaults to a path under the
    operator's home directory, not this checkout) and still happen to be
    named ``.../artifacts/alerts`` for its own unrelated reasons. This
    repo's ``.gitignore`` has no effect on a tree it isn't part of, so a
    path outside the checkout is never GITIGNORED_ALERT_PATH evidence no
    matter what its directory is called.
    """
    try:
        path.resolve().relative_to(_REPO_ROOT)
        return True
    except ValueError:
        return False


# An absolute path (POSIX or a Windows drive) naming the gitignored artifacts
# directory. Deliberately requires an absolute form: a bare relative mention
# ("wrote to artifacts/alerts/...") carries no root to resolve against and is
# treated at face value, same as before -- only a fully-rooted path can be
# proven to sit outside this checkout.
#
# The leading `(?:^|(?<=[\s([{"'`]))` anchors the match to the START of its
# own path token (string start, right after whitespace, or right after a
# token-opening punctuation mark), not merely to SOME "/" appearing before
# "artifacts/alerts" anywhere in the text. Without it, a genuinely relative
# token like "workspace/cron_tracking/.../artifacts/alerts/x.txt" (no
# leading "/" at all) still contains an internal "/" -- e.g. after
# "workspace" -- and `/\S*` would happily start matching THERE, extracting
# "/cron_tracking/.../artifacts/alerts/x.txt" and misreading that substring
# as if it were itself a genuine absolute path rooted at "/".
#
# The punctuation marks (backtick, paren, bracket, brace, quote) are safe
# token-openers, not path characters themselves: a Markdown- or prose-quoted
# external path -- "Saved to `/mnt/durable/artifacts/alerts/x.txt`" or
# "Saved to (/mnt/durable/artifacts/alerts/x.txt)" -- otherwise had no
# whitespace immediately before its leading "/", so this pattern never
# matched it at all, `_gitignored_mention_is_external` never got a path to
# check, and the exemption it exists to grant never applied -- a false
# GITIGNORED_ALERT_PATH on a path that is, in fact, outside the checkout
# (Codex review: "Accept punctuation before external absolute paths").
#
# The second alternative recognizes the OTHER gitignored shape: dca_paths.
# stream_root(<stream>, ..., "alerts", ...), the actually-required call,
# resolves to ".../cron_tracking/<stream>/[.../]alerts/..." with
# $DCA_CRON_TRACKING_ROOT set (durable, outside this checkout) but to THIS
# checkout's own gitignored cron_tracking/ directory when it is unset
# (Codex review: "Flag the default gitignored cron-tracking root"). Any
# number of path segments -- not just exactly one -- is accepted between
# "cron_tracking" and "alerts": .gitignore's bare `cron_tracking/` entry
# discards that directory's ENTIRE subtree regardless of nesting depth, and
# the live, canonical 1530ET prompt's own required call is
# `stream_root('ce16ba6b', 'scheduled_runs', 'alerts', ...)` -- TWO segments
# (claude/dca-executor-prompts/live-mirror/1530ET-330-final-buying-run.txt),
# which an "exactly one segment" pattern never matched at all (Codex review:
# "Cover nested paths under the default cron root"). An earlier, narrower
# version of this pattern assumed a multi-segment nesting like
# ".../cron_tracking/<stream>/scheduled_runs/alerts/..." was NOT gitignored
# -- that assumption was simply wrong, as this real prompt file's own
# REQUIRED call demonstrates.
# A Windows UNC network path ("\\server\share\...") is also an absolute
# form worth recognizing as a candidate external root, alongside a POSIX
# absolute path and a drive-letter one: a durable artifact root mounted as
# a network share is exactly as legitimately "outside this checkout" as one
# under a drive letter or a POSIX mount point (Codex review: "Exempt
# external Windows UNC artifact roots").
# "artifacts"/"cron_tracking" no longer require an "alerts" component
# either, mirroring the identical widening on _CHANNEL_CHECKS'
# GITIGNORED_ALERT_PATH pattern below (Codex reviews: "Match every
# descendant of the ignored artifacts root", "Match alert files directly
# beneath ignored roots"). Left narrower here, a legitimate EXTERNAL
# absolute path like "/mnt/durable/artifacts/2026-08-26-1030-gate5.txt" --
# now matched by the widened GITIGNORED_ALERT_PATH pattern as a bare
# substring -- would no longer be found by _gitignored_mention_is_external's
# span lookup (it searches for a containing match here), so the exemption
# could never fire and a durable, correctly-external path would be wrongly
# flagged.
_BODY_ARTIFACTS_PATH = re.compile(
    r"(?:^|(?<=[\s([{\"'`]))"
    r"(?:/[^\s]*?|[A-Za-z]:[\\/][^\s]*?|\\\\[^\s]*?)"
    r"(?:artifacts|cron_tracking)[/\\]"
    r"[^\s]*[^\s\])\"'`.,:;!]",
    re.IGNORECASE,
)

# A Windows drive-letter path ("C:\..." or "C:/..."). ``Path.resolve()`` on a
# POSIX host treats a string like this as an ordinary RELATIVE path
# component, since backslashes are not separators and a bare "C:" carries no
# meaning to pathlib.PurePosixPath -- so it silently resolves as if it sat
# UNDER whatever the current directory happens to be, including this
# checkout, misreading it as internal. Detected and compared using
# ``PureWindowsPath`` (safe to instantiate on any host -- unlike
# ``WindowsPath``, it never touches the filesystem) instead of ever handing
# a drive-qualified string to ``Path.resolve()``.
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")

# A Windows UNC network path ("\\server\share\..."). Recognized the same
# way a drive-letter path is -- via PureWindowsPath, never handed to
# Path.resolve() on a POSIX host, where a leading "\\" carries no special
# meaning and would otherwise resolve as an ordinary relative path
# component sitting UNDER this checkout, misreading a genuinely external
# network share as internal.
_UNC_PATH = re.compile(r"^\\\\\S")


def _matched_path_is_external(path_text: str) -> bool:
    """Whether the absolute path text ``path_text`` sits OUTSIDE this checkout."""
    # Sentence scanning masks periods, but containment needs the actual path.
    path_text_unmasked = path_text.replace(_PATH_EXTENSION_PERIOD_MASK, ".")

    # Fail fast: Explicitly reject malformed inputs lacking a root anchor
    if not (Path(path_text_unmasked).is_absolute() or _WINDOWS_DRIVE_PATH.match(path_text_unmasked) or _UNC_PATH.match(path_text_unmasked)):
        logger.warning("Artifacts path anchored check failed: expected absolute path, got '%s'", path_text_unmasked)
        return False

    if _WINDOWS_DRIVE_PATH.match(path_text_unmasked) or _UNC_PATH.match(path_text_unmasked):
        # Compared with Windows path semantics, not assumed external
        # outright: if THIS process also happens to run on Windows,
        # `_REPO_ROOT` is itself drive-qualified, and a same-drive,
        # same-tree alert path ("C:\workspace\all-stock-dca\artifacts\
        # alerts\x.txt" naming this very checkout) must still be recognized
        # as internal, not exempted. `_REPO_ROOT` is a POSIX path on every
        # other host, which a drive-qualified OR UNC string can never be
        # relative to -- external there by definition. PureWindowsPath
        # parses a UNC path's "\\server\share" as its own drive, so the
        # same is_relative_to comparison covers both forms.
        try:
            return not PureWindowsPath(path_text_unmasked).is_relative_to(PureWindowsPath(str(_REPO_ROOT)))
        except ValueError:
            return True
    return not _is_within_repo_checkout(Path(path_text_unmasked))


def _gitignored_mention_is_external(text: str, match: "re.Match[str]") -> bool:
    """Whether THIS SPECIFIC ``artifacts[/\\\\]alerts`` occurrence names a path OUTSIDE this checkout.

    A durable artifact root can legitimately live outside the repository (see
    :func:`_is_within_repo_checkout`) and still be named in an alert's OWN
    body text, not just at its loaded location -- an alert stored under a
    supported external root can state its absolute path directly. Only an
    absolute path can be checked this way; a bare relative mention has no
    root to resolve and is left to the existing body-text match.

    Checked per-occurrence, not body-wide: a body can state one copy's
    absolute, external path AND separately narrate a SECOND, bare
    repo-relative "artifacts/alerts/..." mention for a different copy of the
    same alert -- "Saved to /mnt/durable/artifacts/alerts/x.txt. Also wrote
    artifacts/alerts/x.txt." The second is exactly the gitignored evidence
    this check exists to catch; an external match elsewhere in the body must
    exempt only its OWN occurrence, never the whole body.
    """
    for path_match in _BODY_ARTIFACTS_PATH.finditer(text):
        if path_match.start() <= match.start() < path_match.end():
            return _matched_path_is_external(path_match.group(0))
    return False


# A write/save/create outcome naming the path token as somewhere an alert was
# actually stored, not merely mentioned. Mirrors _SLACK_DELIVERY_OUTCOME's
# design: the bare path token itself is not proof anything was written
# there -- "the bare artifacts/alerts path is gitignored; used the durable
# stream_root path instead." names the path only to describe the rule that
# governs it (and to say it was AVOIDED), with no claim that this alert was
# ever stored under it. Requiring one of these verbs in the same clause is
# how the Slack check already tells a genuine delivery claim apart from
# purely informational prose naming the channel (Codex review: "Require a
# write claim before flagging ignored paths").
_PATH_WRITE_CLAIM = re.compile(
    r"\bwrit(?:e|ten|ing)\b|\bwrote\b|\bsaved?\b|\bsav(?:ing|es)\b"
    r"|\bstor(?:ed|ing|es)\b|\bcreat(?:ed|ing|es)?\b"
    # The Gate 9 fallback prompts' own prescribed verb for the durable-alert
    # write, alongside "wrote"/"saved"/"stored"/"created" -- "Appended the
    # alert to artifacts/alerts/x.txt" is exactly as much a write claim as
    # any of those, but matched none of them (Codex review: "Recognize
    # append as a path write claim").
    r"|\bappend(?:ed|ing|s)?\b"
    # "Persisted the alert to artifacts/alerts/x.txt" (similarly "logged",
    # "copied", "emitted") states the same durable-write outcome with a
    # different ordinary verb, and matched none of the alternatives above
    # (Codex review: "Recognize persisted writes to ignored alert paths").
    r"|\bpersist(?:ed|ing|s)?\b|\blogg(?:ed|ing)\b|\bcopi(?:ed|es|ying)\b|\bemitt(?:ed|ing)\b|\bemits?\b",
    re.IGNORECASE,
)


def _gitignored_mention_is_exempt(sentence: str, match: "re.Match[str]") -> bool:
    """True when this GITIGNORED_ALERT_PATH occurrence should not be flagged.

    Combines the two independent reasons a path mention is not evidence of a
    gitignored write: an absolute path provably outside this checkout (see
    :func:`_gitignored_mention_is_external`), or -- new here -- a clause that
    names the path token without ever claiming anything was written, saved,
    or created there at all.
    """
    if _gitignored_mention_is_external(sentence, match):
        return True
    clause = _clause_containing(sentence, match.start(), _CLAUSE_SPLIT)
    return not _PATH_WRITE_CLAIM.search(clause)


# A private-use placeholder standing in for a "." that sits INSIDE a matched
# GITIGNORED_ALERT_PATH span -- never whitespace, never one of _SENTENCE_SPLIT's
# own boundary characters, so substituting it changes no downstream regex's
# verdict while preventing _is_affirmative_claim's own per-sentence loop from
# treating that "." as a sentence break.
_PATH_EXTENSION_PERIOD_MASK = ""


def _mask_path_extension_periods(text: str, pattern: str) -> str:
    """Mask internal path periods while preserving genuine sentence boundaries.

    _SENTENCE_SPLIT treats every "." as a sentence boundary -- including one
    that is merely part of a file extension inside the matched path itself,
    e.g. "artifacts/alerts/x.txt". _is_affirmative_claim's own per-sentence
    loop splits the body BEFORE searching for matches at all, so a write
    claim following such a path ("...x.txt was written") lands in a
    DIFFERENT split fragment than the path match it belongs to, even though
    both are the very same sentence in the original text -- silently
    defeating the write-claim requirement above for exactly the ordinary
    case where the write verb follows the path (Codex review: "Keep
    filename extensions with their write claims").

    Absolute paths also need their parent-directory periods masked: splitting
    at a dotted checkout name separates a preceding write verb from the ignored
    root and can strip an external path of the prefix that proves it external.

    Masking only the periods actually INSIDE a path match -- never a genuine
    sentence-ending period elsewhere, such as the one that correctly isolates
    the informational half of "the bare artifacts/alerts path is gitignored;
    used the durable stream_root path." -- leaves every other sentence
    boundary untouched. Length-preserving, so match spans found against the
    masked text land at identical offsets to spans found against the
    original.

    The match's own trailing "." (its very last character, if any) is NEVER
    masked, though: the pattern's greedy ``\\S+`` does not stop at a period,
    so a path that ends a sentence -- "Wrote artifacts/alerts/x.txt. This
    path was not used." -- is matched as "artifacts/alerts/x.txt." WITH the
    sentence-ending period swept in. Masking that one too would merge the
    genuine next sentence into the same fragment, letting its "not" reach
    back and wrongly negate the match (Codex review: "Preserve sentence
    boundaries after path extensions"). Every OTHER period inside the
    match -- a directory or extension separator -- is still masked normally.
    """
    chars = list(text)
    matches = list(re.finditer(pattern, text, re.IGNORECASE))
    matches.extend(_BODY_ARTIFACTS_PATH.finditer(text))
    for match in matches:
        last_index = match.end() - 1
        for i in range(match.start(), match.end()):
            if chars[i] == "." and i != last_index:
                chars[i] = _PATH_EXTENSION_PERIOD_MASK
    return "".join(chars)


# The seven-slot Claude executor roster (docs/safeguards.md#live-execution-roster).
# Ordered, because the coverage matrix prints chronologically and "which slot went
# quiet" only reads correctly against a fixed column order.
SLOTS: Tuple[str, ...] = ("0935", "1030", "1130", "1230", "1330", "1430", "1530")

# Wall-clock spellings actually observed in the alert corpus. Executors write the
# slot a dozen different ways ("2:30 PM ET", "1430ET", "230-pm-afternoon-buying-run"),
# so matching is layered rather than one regex: explicit ET label wins, then the
# clock time, then the scheduler's own task name.
# Keyed on (clock, meridiem), not the clock alone. The digits do not identify a
# slot by themselves: "2:30 AM" is not the 2:30 PM slot, and off-schedule firings
# are specifically part of this corpus (the 2026-08-14 batch fired at 9:07 PM),
# so a meridiem-blind lookup would file an overnight alert against a real slot.
_CLOCK_TO_SLOT: Dict[Tuple[str, str], str] = {
    ("9:35", "AM"): "0935",
    ("10:30", "AM"): "1030",
    ("11:30", "AM"): "1130",
    ("12:30", "PM"): "1230",
    ("1:30", "PM"): "1330",
    ("2:30", "PM"): "1430",
    ("3:30", "PM"): "1530",
}

# The same roster, keyed on its 24-hour "H:MM"/"HH:MM ET" spelling -- the
# natural, unambiguous way to declare an ET slot alongside the compact
# 4-digit form ("1030 ET") the explicit-ET branch already accepts: "9:35 ET"
# and "13:30 ET" are both real Gate 9 subject shapes ("9:35 ET | 2026-08-26 |
# Gate 5"), and neither matched the compact-digit-only pattern at all, so the
# record fell through to weaker or no slot evidence despite an explicit,
# unambiguous declaration (Codex review: "Recognize colon-form ET slot
# declarations"). No AM/PM suffix here -- unlike _CLOCK_TO_SLOT, "ET" already
# disambiguates the hour on its own.
_COLON_ET_TO_SLOT: Dict[str, str] = {
    "9:35": "0935",
    "10:30": "1030",
    "11:30": "1130",
    "12:30": "1230",
    "13:30": "1330",
    "14:30": "1430",
    "15:30": "1530",
}

# Scheduler task names, from config/scheduled_tasks.json and the live-mirror
# filenames. Matched last: they are the least ambiguous but also the least often
# present, and one of them carries the upstream typo ("mornig") that must keep
# matching or the 11:30 slot silently drops out of every report.
_TASK_NAME_TO_SLOT: Tuple[Tuple[str, str], ...] = (
    ("935-am-dca-morning-buy", "0935"),
    ("1030-dca-recurring-buy", "1030"),
    ("1130-mornig-buying-run", "1130"),
    ("1130-morning-buying-run", "1130"),
    ("1230-pm-midday-buying-run", "1230"),
    ("130-pm-afternoon-buyiing-run", "1330"),
    ("130-pm-afternoon-buying-run", "1330"),
    ("230-pm-afternoon-buying-run", "1430"),
    ("330-final-buying-run", "1530"),
)

# Stream words, the weakest signal. "market-open" and "midday" map cleanly; the
# bare word "morning" deliberately does NOT appear here, because the 11:30 task
# is named "morning-buying-run" while the 9:35 one is "dca-morning-buy" -- one
# token, two slots, so it is not usable as evidence on its own.
_STREAM_WORD_TO_SLOT: Tuple[Tuple[str, str], ...] = (
    ("market-open", "0935"),
    ("market open", "0935"),
    ("midday", "1230"),
)

# Named reason codes that stand in for a numeric gate. GATE5_DISABLED and
# GATE4_EXPECTED_OVERLAP are documented in config/notifications.json as
# *expected* conditions rather than errors, so a digest that folded them into
# the plain "Gate 4"/"Gate 5" counts would overstate how much is actually
# going wrong. SCHEDULE_WATCHDOG (added 2026-09-26) is the opposite case -- a
# genuine failure -- but belongs in the same lookup for the same mechanical
# reason: scripts/schedule_watchdog.py writes this literal token as the gate
# field of its own alert filenames (<day>-<slot>-SCHEDULE_WATCHDOG.txt), and
# without an entry here detect_gate_evidence() has no numeric "Gate N" to
# fall back to, so every one of those files silently loaded with gate=None
# and the promised per-gate bucket never existed (caught in PR #1535 review).
_REASON_CODES: Tuple[str, ...] = ("GATE5_DISABLED", "GATE4_EXPECTED_OVERLAP", "SCHEDULE_WATCHDOG")

_REASON_CODE_PATTERNS = {
    code: re.compile(r"(?<![A-Za-z0-9_])" + re.escape(code.lower()) + r"(?![A-Za-z0-9_])")
    for code in _REASON_CODES
}

# A reason code used as a NOUN-PHRASE MODIFIER -- naming a config/routing
# object, not classifying this run's own outcome. "Unable to load the
# GATE5_DISABLED routing configuration" mentions the code the same way an
# error about a missing FILE or SETTING would, and none of the existing
# negation/prior-event screens catch it: "Unable" is not one of the
# _REASON_NEGATION_* cues, and there is no prior-event marker at all. An
# earlier-gate alert that merely can't LOAD a piece of GATE5_DISABLED
# routing config was being classified as GATE5_DISABLED itself, outranking
# its own explicit "Failing gate: Gate 2" label (Codex review: "Ignore
# informational reason-code mentions"). Checked immediately after the
# match, cheap and specific rather than broadening the shared negation
# vocabulary those other callers also rely on.
_REASON_CODE_INFORMATIONAL_MENTION = re.compile(
    r"^\s*(?:(?:alert|fallback|notification|email|slack|delivery)\s+){0,3}(?:routing\s+)?config(?:uration)?\b",
    re.IGNORECASE,
)

# Gate numbers can be fractional. The repo runs a Gate 6.5 (per-ticker resize)
# and a Gate 6.7, and they are named in the prompts far more often than most of
# the whole-numbered gates. Capturing a single digit would fold every one of
# them into "Gate 6", merging three distinct failures into one frequency count,
# one streak, and one cross-source identity -- the exact confusion this digest
# exists to resolve.
_GATE_TOKEN = r"\bgate\s*[-_]?\s*(\d+(?:\.\d+)?)\b"

# A declared ET event date, in the "Date: 2026-08-26" form. Requires the
# literal "date" label, not a bare ISO-shaped date scanned out of free text --
# a body routinely quotes an unrelated prior date ("aborted on 2026-08-19"),
# and a bare match anywhere would misdate the CURRENT alert to a date it is
# only narrating.
#
# Also excludes a handful of COMPOUND date labels naming a DIFFERENT field
# entirely -- "start date"/"end date"/"expiry date"/"window date" -- that
# happen to end in the word "date" but do not declare the alert's own event
# date at all: "Trading window end date: 2026-10-30" (a Gate 3 body quoting
# config/trading_window.json's own end_date) matched unconditionally and
# misdated the alert to that far-future date instead of its transport day
# (Codex review: "Restrict body date matches to the alert's event field").
# Ordinary prose before a genuine "Date:" label ("Previous run Date:
# 2026-08-25", "ABORT Gate 4 -- Date: 2026-08-26") is unaffected -- only
# these specific compound-label modifiers are excluded.
_DECLARED_ET_DATE = re.compile(
    r"(?<!start )(?<!end )(?<!expiry )(?<!expiration )(?<!window )"
    r"\bdate\s*:?\s*-?\s*(\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

# The canonical live-mirror prompts (dca-executor-prompts/live-mirror/*.txt,
# Gate 9) only require the SUBJECT to name "the slot, the ET date, and the
# failing gate" -- not the literal "Date:" label above, which is one
# compliant way to do that but not the only one. A bare ISO-shaped date is
# trusted when (and only when) it appears in the SUBJECT: the subject's
# entire job under that contract is to declare this alert's own slot/date/
# gate, unlike the body, which routinely narrates unrelated prior incidents
# and is never scanned for a bare date.
_BARE_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _declared_body_date(body: str) -> Optional["re.Match[str]"]:
    """The first labelled "Date:" match in ``body`` that is not itself a
    PRIOR-EVENT mention, or None.

    Shared by ``AlertRecord.et_date`` for both a Gmail record's subject-less
    fallback and an off-contract alert file's body-only search (see that
    property). Falls through to the NEXT occurrence rather than trusting the
    first bare match: a partially-parsed alert's ONLY "Date:" can be a purely
    historical one ("Previous run Date: 2026-08-25"), and an unscreened match
    there misdates the CURRENT alert to the day it is only quoting.
    """
    for candidate in _DECLARED_ET_DATE.finditer(body):
        if not _mention_has_prior_event(body, candidate.start(1), 0):
            return candidate
    return None

# Ordered most specific FIRST. Nearly every alert that breaches a cap, has an
# order rejected, or stops mid-batch also says it aborted -- "ABORTED -- cap
# breach at Gate 6", "Order rejected; aborting" -- so testing the generic abort
# wording first would file all three under ABORT and leave the three categories
# that exist to tell them apart permanently near zero. ABORT is the fallback for
# an alert that says only that it stopped.
#
# REJECTED is documented as an order outcome specifically -- Gate 7 rejecting
# an order -- not a catch-all for anything the word "reject" ever describes.
# "Email was rejected by the provider; run aborted" has no order involved at
# all, but the bare `reject(?:ed|ion)` pattern matched it anyway and outranked
# the alert's own explicit generic ABORT subject; the same is true of a
# Slack- or repository-access rejection. Required alongside the pattern
# itself wherever a body match is checked (never for an explicit SUBJECT
# verdict, which is trusted outright) -- see its uses in detect_verdict().
_REJECTED_ORDER_CONTEXT = re.compile(r"\b(?:order|broker)\b", re.IGNORECASE)

_VERDICT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("CAP_BREACH", r"\bcap breach\b|\bcap already blown\b|\bcapped by budget\b"),
    ("REJECTED", r"\breject(?:ed|ion)\b"),
    ("PARTIAL", r"\bpartial batch\b|\bstopped mid-(?:run|batch)\b|\bmid-batch\b"),
    ("ABORT", r"\babort(?:ed|ing)?\b"),
    # Gate 9 alerts on "any abort, skip, or cap breach", so a skip is a
    # first-class outcome, not an oddity. Without this pattern an alert whose
    # subject says SKIPPED lands under "unclassified" in BY VERDICT. It stays
    # BELOW abort: "aborted, skipping the rest of the batch" is an abort that
    # narrates a skip, not a skip.
    ("SKIPPED", r"\bskip(?:ped|ping)?\b"),
    ("REPORT", r"\brun report\b"),
)

# The generic labels, split out from the specific ones above. "Aborted" says the
# run stopped; "cap breach" says why. Order within each group is preserved.
_GENERIC_VERDICT_NAMES = frozenset({"ABORT", "SKIPPED", "REPORT"})
_SPECIFIC_VERDICTS: Tuple[Tuple[str, str], ...] = tuple(
    entry for entry in _VERDICT_PATTERNS if entry[0] not in _GENERIC_VERDICT_NAMES
)
_GENERIC_VERDICTS: Tuple[Tuple[str, str], ...] = tuple(
    entry for entry in _VERDICT_PATTERNS if entry[0] in _GENERIC_VERDICT_NAMES
)

# Cues that a sentence is recounting an EARLIER event rather than classifying
# this run. "ABORTED at Gate 4" over "one order was rejected earlier" is an
# abort that mentions history, not a rejection.
# NOT a bare "already": "cap already blown" is one of _VERDICT_PATTERNS' own
# CAP_BREACH spellings, describing THIS run, so treating every "already" as
# history suppressed a first-class verdict defined ten lines above. Only the
# forms that actually place the event in the past qualify.
_PRIOR_EVENT = re.compile(
    r"\b(?:earlier|previously|prior to this|before this"
    r"|already (?:happened|occurred|fired|ran|aborted)"
    r"|had already"
    # Up to two intervening tokens are tolerated between "previous" and
    # "run"/"firing"/"attempt", not just one: "Previous midday run failed"
    # (one word, a stream name) and "Previous 10:30 AM run failed" (two
    # tokens, a clock time) are exactly as historical as "previous run
    # failed", and the original literal-adjacency-only form left both
    # unrecognized (Codex reviews: "Screen historical stream words before
    # assigning a slot", "Recognize multi-token historical run qualifiers").
    # `\S+` rather than `[\w-]+` so a clock's colon ("10:30") is one token
    # too. Bounded at two, not unbounded: this only needs to reach past the
    # single qualifying word or clock phrase this corpus actually uses, and
    # an unbounded `.*` here would risk the historical cue reaching past
    # unrelated words within the same already clause-scoped search.
    r"|previous\s+(?:\S+\s+){0,2}(?:run|firing|attempt|order)"
    # "prior" as a bare adjective ("prior attempt", "prior order") is a
    # second, equally common way to mark the SAME kind of historical
    # mention "previous <noun>" already covers -- only "prior to this" was
    # recognized before, not "prior" directly modifying a noun -- so "The
    # previous order was rejected; this run aborted" and "A prior attempt
    # was rejected" both left REJECTED (from the historical clause) beating
    # the current run's own ABORT subject (Codex review: "Recognize
    # adjectival prior-event qualifiers"). Same bounded-token tolerance and
    # noun list as the "previous" form above.
    r"|prior\s+(?:\S+\s+){0,2}(?:run|firing|attempt|order)"
    r"|last (?:run|week|time))\b",
    re.IGNORECASE,
)

# Channel claims worth flagging. Each entry is (regex, warning code, explanation).
# These are the checks that catch the failure mode this module was written for:
# an alert that reports itself delivered on a channel that is off or discarded.
# The two Slack routes config/notifications.json DOES permit: the env-gated
# drift and uncaught-failure webhooks. `scripts/drift_detection.py --slack` is a
# documented flag the live prompts invoke after the artifact write, so an alert
# that mentions posting a drift finding through it is describing a supported
# route -- and warning on it would turn --fail-on-warnings red for correct
# behavior, which is how a check stops being read. Scoped to the same sentence
# as the Slack mention, so a routine-alert claim elsewhere in the body still
# warns.
#
# Each alternative has to NAME one of those two routes. The bare word "webhook"
# does not: "Gate 5 alert posted to the Slack webhook" is a routine claim
# through an unsupported route, and exempting it would suppress the warning for
# exactly the delivery this check exists to catch.
#
# The bare content-topic words "drift"/"uncaught"/"unhandled" used to be
# alternatives here too, on the theory that naming the TOPIC of an alert
# implied its permitted route. That conflated the two: "Posted the Gate 5
# alert to Slack after observing drift" posts a routine, unsupported alert
# and only mentions "drift" as context for why it fired, but the bare word
# alone exempted it as if the drift webhook had been used (Codex review:
# "Require an actual route before exempting Slack delivery"). Only concrete
# route evidence exempts now: the two webhook env var names themselves, or
# the `drift_detection.py --slack` invocation that is documented to use
# them -- never a word that merely describes what the alert is about.
_PERMITTED_SLACK = re.compile(r"\bDCA_DRIFT_SLACK_WEBHOOK\b|\bDCA_ALERT_SLACK_WEBHOOK\b", re.IGNORECASE)

# ``--slack`` is a generic CLI flag, so it is a permitted route only when it
# appears with the drift command that config/notifications.json authorizes.
# Either order is accepted to cover both a normal invocation and prose that
# reports its options before naming the command.
_DRIFT_COMMAND_WITH_SLACK = re.compile(
    r"\bdrift_detection\b[^\r\n;]*--slack\b|--slack\b[^\r\n;]*\bdrift_detection\b",
    re.IGNORECASE,
)

# A posting/sending/delivery OUTCOME, as opposed to merely naming the channel.
# The bare word "slack" fires on any affirmative, unnegated mention at all --
# "Checked Slack configuration" and "Slack status was healthy" are purely
# informational, name no attempt to route anything there, and still tripped
# SLACK_DISABLED (Codex review: "Require an actual Slack-delivery claim").
# "alert" and "notification" are restricted to VERBAL outcome forms only --
# unlike "post"/"send"/"deliver", the bare noun ("Slack alert configuration",
# "Slack notification status") is common informational phrasing that claims
# no outcome at all, and both bare "alert" (its `?`-optional suffix let it
# match with none) and the "ication" alternative (a pure noun form -- nothing
# is ever verbed as "email ications") wrongly counted as delivery evidence
# (Codex review: "Require verbs for Slack delivery outcomes").
_SLACK_DELIVERY_OUTCOME = re.compile(
    r"\bpost(?:ed|ing|s)?\b|\bsen[dt](?:s|ing)?\b|\bsent\b"
    r"|\bdeliver(?:ed|y|ing|s)?\b|\bnotif(?:y|ies|ied|ying)\b|\balert(?:ed|ing|s)\b"
    # Ordinary alternative delivery phrasing, all naming an actual outcome --
    # not just the channel's name -- the same way "post"/"deliver" already
    # do: "Routed the Gate 5 alert to Slack", "Slack received the Gate 5
    # alert", and "The alert went to Slack" are all routine claims that
    # delivery happened, and none of the verbs above matched any of them,
    # so each was wrongly read as informational and exempted (Codex review:
    # "Recognize route verbs as Slack delivery outcomes").
    r"|\brout(?:ed|ing|es)?\b|\brecei(?:ved|ving|ves|ve)\b|\b(?:went|goes?)\s+to\b|\breach(?:ed|ing|es)?\b"
    # "The Slack notification was successful", "The Slack webhook call
    # succeeded" state genuine delivery outcomes with an adjective/verb
    # neither this pattern nor its exemption logic recognized -- so a
    # routine alert's own Slack claim, this check exists to catch, read as
    # purely informational and slipped past SLACK_DISABLED (Codex review:
    # "Recognize successful Slack delivery predicates").
    r"|\bsucceed(?:ed|s|ing)?\b|\bsuccessful\b|\baccepted\b",
    re.IGNORECASE,
)


def _slack_mention_lacks_delivery_outcome(sentence: str, match: "re.Match[str]") -> bool:
    """True when a Slack mention's own CLAUSE names no posting/sending/
    delivery/notification outcome at all -- i.e. it is purely informational
    rather than a claim that an alert was actually routed there. Used as
    SLACK_DISABLED's ``exempt_match``: this returning True means the match
    is NOT a violation, same as the other checks' per-match exemptions.
    """
    clause = _clause_containing(sentence, match.start(), _CLAUSE_SPLIT)
    return not _SLACK_DELIVERY_OUTCOME.search(clause)


# config/notifications.json scopes each permitted webhook to a SPECIFIC alert
# type, not general-purpose Slack delivery: DCA_ALERT_SLACK_WEBHOOK is for
# "uncaught-exception failure alerts from dca_alerts.py's shared entrypoint
# wrapper only ... not for routine run outcomes", and DCA_DRIFT_SLACK_WEBHOOK
# is for "immediate drift alerts from scripts/drift_detection.py only". Naming
# the env var alone used to exempt ANY Slack claim in the same clause
# unconditionally, regardless of what the alert actually was.
_UNCAUGHT_EXCEPTION_TOPIC = re.compile(r"\buncaught\b|\bunhandled\b|\bexception\b|\bcrash(?:ed|es|ing)?\b", re.IGNORECASE)
_DRIFT_TOPIC = re.compile(r"\bdrift\b", re.IGNORECASE)

# The webhook actually used for THIS delivery, when the clause says so
# explicitly -- "posted ... via DCA_DRIFT_SLACK_WEBHOOK". Distinct from
# merely NAMING a webhook, which a clause can also do for the other,
# unused one ("... via DCA_DRIFT_SLACK_WEBHOOK with DCA_ALERT_SLACK_WEBHOOK
# unset") -- see _slack_route_matches_alert_type for why that distinction
# matters.
_SLACK_ROUTE_PREPOSITION = re.compile(
    r"\b(?:via|using|through)\s+(DCA_ALERT_SLACK_WEBHOOK|DCA_DRIFT_SLACK_WEBHOOK)\b", re.IGNORECASE
)


def _slack_route_matches_alert_type(clause: str) -> bool:
    """Whether a permitted-route marker found in ``clause`` actually names
    the alert type that route is scoped to.

    "Posted the Gate 5 alert to Slack via DCA_ALERT_SLACK_WEBHOOK" claims a
    routine Gate 5 outcome was routed through the uncaught-exception-only
    webhook, which config/notifications.json does not permit -- the env
    var's own presence is not, by itself, proof the alert is the type of
    failure that webhook exists for (Codex review: "Validate alert type
    before exempting Slack routes"). ``drift_detection``/``--slack`` need
    no separate check here: that tool only ever posts drift findings, so
    naming it already IS the topic, unlike a bare env var name shared by
    every alert that happens to mention it.

    DCA_DRIFT_SLACK_WEBHOOK accepts EITHER topic, not just drift:
    config/notifications.json names it explicitly as
    ``optional_failure_alert.fallback_webhook_env`` -- "falling back to
    DCA_DRIFT_SLACK_WEBHOOK if unset" -- so an uncaught-exception alert
    legitimately routes through it too when DCA_ALERT_SLACK_WEBHOOK isn't
    configured. Requiring a "drift" cue specifically wrongly flagged that
    documented fallback route as unsupported (Codex review: "Allow failure
    alerts through the fallback Slack webhook").

    A clause can name BOTH env vars without both being the route actually
    used -- "Posted the drift alert to Slack via DCA_DRIFT_SLACK_WEBHOOK
    with DCA_ALERT_SLACK_WEBHOOK unset" states which var was used (via)
    and which was merely absent, but checking DCA_ALERT_SLACK_WEBHOOK
    first, unconditionally, validated against the WRONG route's topic
    requirement and flagged a supported drift delivery as disabled (Codex
    review: "Prefer the Slack route actually used"). When the clause names
    the route with "via"/"using"/"through", that one wins; only when
    neither does (a bare, single mention) does checking DCA_ALERT_SLACK_
    WEBHOOK first still serve as the fallback disambiguation it always was.
    """
    route_match = _SLACK_ROUTE_PREPOSITION.search(clause)
    if route_match:
        webhook = route_match.group(1).upper()
    elif re.search(r"\bDCA_ALERT_SLACK_WEBHOOK\b", clause, re.IGNORECASE):
        webhook = "DCA_ALERT_SLACK_WEBHOOK"
    elif re.search(r"\bDCA_DRIFT_SLACK_WEBHOOK\b", clause, re.IGNORECASE):
        webhook = "DCA_DRIFT_SLACK_WEBHOOK"
    else:
        return True
    if webhook == "DCA_ALERT_SLACK_WEBHOOK":
        return bool(_UNCAUGHT_EXCEPTION_TOPIC.search(clause))
    return bool(_DRIFT_TOPIC.search(clause) or _UNCAUGHT_EXCEPTION_TOPIC.search(clause))


def _slack_mention_is_exempt(sentence: str, match: "re.Match[str]") -> bool:
    """True when this SLACK_DISABLED occurrence should not be flagged.

    Combines the two independent reasons a Slack mention is not evidence
    of an unsupported delivery: it names no delivery outcome at all (see
    :func:`_slack_mention_lacks_delivery_outcome`), or it names a
    permitted route (:data:`_PERMITTED_SLACK`) whose own alert-type scope
    (see :func:`_slack_route_matches_alert_type`) actually matches this
    alert.
    """
    if _slack_mention_lacks_delivery_outcome(sentence, match):
        return True
    clause = _exempt_search_text(sentence, match, _PRIOR_EVENT_CLAUSE_SPLIT)
    if not (_PERMITTED_SLACK.search(clause) or _DRIFT_COMMAND_WITH_SLACK.search(clause)):
        return False
    return _slack_route_matches_alert_type(clause)

# (regex, warning code, explanation, exemption). The exemption, where present,
# names a legitimate use of the same channel: a mention that carries it is a
# supported route rather than a contradicted claim.
_CHANNEL_CHECKS: Tuple[Tuple[str, str, str, Optional["re.Pattern[str]"]], ...] = (
    (
        # Either separator: an alert that records its discarded path in the
        # Windows form is under the same gitignored directory, and making the
        # warning depend on the producer's platform would let it through.
        # The second alternative catches the OTHER gitignored shape: this
        # checkout's own default cron_tracking/<stream>/[.../]alerts root
        # (any nesting depth -- see _BODY_ARTIFACTS_PATH), which
        # dca_paths.stream_root(...) itself resolves to when
        # $DCA_CRON_TRACKING_ROOT is unset.
        #
        # Anchored at a PATH-COMPONENT boundary -- start of string, or a
        # character that is not itself part of an identifier -- not left
        # bare. Neither alternative names one of its own, so
        # "mycron_tracking/morning_dca/alerts/x.txt" and
        # "durable_artifacts/alerts/x.txt" both matched as substrings of a
        # DIFFERENT directory name that merely ends with the same text,
        # even though .gitignore only discards the exact "cron_tracking/"
        # and "artifacts/" components (Codex review: "Require
        # path-component boundaries in ignored-path matches").
        #
        # "artifacts" allows the same arbitrary-nesting-depth suffix
        # "cron_tracking" already does: a bare .gitignore "artifacts/"
        # entry discards the WHOLE subtree at any depth, so
        # "artifacts/archive/alerts/x.txt" is just as gitignored as
        # "artifacts/alerts/x.txt" -- requiring "alerts" to be a DIRECT
        # child missed every intermediate directory (Codex review: "Match
        # every descendant of the ignored artifacts root").
        #
        # No longer requires an "alerts" COMPONENT at all, though: a bare
        # .gitignore "artifacts/"/"cron_tracking/" entry discards its whole
        # subtree regardless of what lives in it, so a fallback alert
        # written directly under either root -- "artifacts/2026-08-26-
        # 1030-gate5.txt", "artifacts/archive/2026-08-26-1030-gate5.txt",
        # neither of which ever reaches an "alerts" directory -- was just as
        # gitignored and just as unflagged (Codex review: "Match alert
        # files directly beneath ignored roots"). Every previously-matched
        # "...alerts" shape is a strict subset of this broader one.
        r"(?:^|(?<=[^A-Za-z0-9_]))(?:artifacts|cron_tracking)[/\\]\S+",
        "GITIGNORED_ALERT_PATH",
        "durable alert written to a bare repo-relative artifacts/ path, or to "
        "this checkout's own default cron_tracking/ root, either of which "
        ".gitignore discards -- Gate 9 requires dca_paths.stream_root(...) "
        "with $DCA_CRON_TRACKING_ROOT set to a durable location",
        None,
    ),
    (
        r"\bslack\b",
        "SLACK_DISABLED",
        "claims delivery to Slack for a routine alert, but config/notifications.json "
        "disables Slack for those (only the two env-gated drift/failure webhooks exist)",
        # None, not _PERMITTED_SLACK: the permitted-route check now lives
        # inside _slack_mention_is_exempt (the exempt_match below), which
        # additionally validates the route's own alert-type scope --
        # something a static clause-scoped regex here cannot express.
        None,
    ),
)

# A draft is only *evidence* of an alert, never delivery -- the prompt says so in
# as many words. Every record sourced from the draft export carries this.
_DRAFT_WARNING = (
    "EMAIL_DRAFT_NOT_DELIVERED",
    "recorded as an unsent Gmail draft; per Gate 9 a draft is not delivery",
)

# Cues that a channel is being mentioned as NOT used rather than as delivered.
# The warnings above fire on an alert that *claims* a dead channel, so an
# executor correctly reporting "Slack is disabled, so nothing was posted there"
# must not be flagged for handling it right -- that would turn a clean scheduled
# check red and teach the operator to ignore the warnings.
_NEGATION_CUES = re.compile(
    r"\b(?:no|not|never|without|skipped?|skipping|unable|could ?n[o']t|cannot|"
    r"can ?not|disabled|unavailable|omitted|suppressed|failed to)\b",
    re.IGNORECASE,
)

# A common manner phrase ("with no errors", "with no issues") that names the
# ABSENCE of a problem during an action, not a denial of the action itself.
# _is_affirmative_claim()'s negation check is clause-scoped but otherwise
# unbounded within that clause, so "Posted the Gate 5 alert to Slack with no
# errors" had the bare "no" inside this idiom count as negating the "posted
# to Slack" claim it merely follows (Codex review: "Bind negation to the
# channel outcome"). Stripped out of the negation-check text before
# _NEGATION_CUES is applied there -- specifically, not globally, since this
# idiom's "no" is not a real disclaimer.
#
# "without delay/errors/issues" is the same idiom with a different lead-in
# word: _NEGATION_CUES treats bare "without" as a disclaimer (correctly, for
# "without using Slack"), but "Posted the Gate 5 alert to Slack without
# delay" and "Wrote artifacts/alerts/x.txt without errors" both use it to
# describe the ABSENCE of a problem during a real, affirmed action, and the
# bare "without" wrongly negated the claim it follows (Codex review:
# "Preserve affirmative claims made without delay or errors"). Bounded to
# this short, specific noun list -- not unbounded -- so a genuine "without
# using Slack" disclaimer right after this same lead-in word is untouched.
#
# "with no <word>" is bounded to the SAME short noun list, not left as
# `\w+`: an unbounded form strips a GENUINE negation too, not just this
# benign idiom -- "Slack was checked with no messages posted" has "no
# messages" as the real denial that nothing was posted, and stripping the
# whole phrase left "posted" standing alone as an unnegated delivery claim
# (Codex review: "Restrict manner-phrase stripping to benign qualifiers").
_NON_NEGATING_MANNER_PHRASE = re.compile(
    r"\bwith no (?:delay|errors?|issues?|problems?)\b|\bwithout (?:delay|errors?|issues?|problems?)\b",
    re.IGNORECASE,
)

# A pronoun subject opening an "and"-joined clause signals a CONTINUATION of
# the clause before it (same claim, cause then effect) rather than a new,
# independent one. Used only by the exempt-route search in
# _is_affirmative_claim() -- see that function's docstring.
_CONTINUATION_SUBJECT = re.compile(r"^\s*(?:it|this|that|which)\b", re.IGNORECASE)

# A preposition that negates its OBJECT -- the thing named AFTER it -- never
# the thing before, same directional shape as _REASON_NEGATION_FORWARD_ONLY.
# "Used the durable stream_root path instead of artifacts/alerts/x.txt"
# names the gitignored path only to say it was AVOIDED, not written to, but
# _NEGATION_CUES has no entry for "instead of" at all, so the mention read
# as an affirmative write claim (Codex review: "Require a write claim for
# body-only ignored paths"). Checked only in the text BEFORE the match,
# within its own clause -- like "rather than", it must not suppress a
# mention that comes BEFORE it in the same clause ("Posted to Slack instead
# of using email" still affirms the Slack post).
_CHANNEL_NEGATION_FORWARD_ONLY = re.compile(r"\b(?:instead of|rather than|in place of)\b", re.IGNORECASE)

# Sentence-ish splitter for scoping the negation check. Deliberately crude: the
# alternative is reading the whole body as one context, where a single "no" 400
# characters away would suppress a real warning.
_SENTENCE_SPLIT = re.compile(r"[.\n;]")

# A deliberately NARROWER negation set for reason codes than for channel claims.
# A real GATE5_DISABLED body routinely says "no signal-generation entry point
# exists", so the broad set above -- which includes "no" -- would suppress the
# very verdict it is meant to recognize. These cues mark a genuine disclaimer
# ("this is not GATE5_DISABLED", "an earlier gate rather than GATE5_DISABLED").
#
# Split by direction. "not"/"isn't"/"wasn't"/"never" are ordinary negation
# adverbs and disclaim a mention on EITHER side ("not GATE5_DISABLED" /
# "GATE5_DISABLED was not the verdict", "never rejected" / "REJECTED was
# never the verdict") -- "never" added alongside its close synonym "not"
# (Codex review: "Recognize 'without' and 'never' as verdict negation").
# "rather than"/"instead of"/"unlike"/"as opposed to"/"without" are
# prepositions that always negate their OBJECT -- the thing named AFTER
# them -- never the thing before: "GATE5_DISABLED rather than Gate 5" affirms
# GATE5_DISABLED and rejects only "Gate 5", and likewise "completed without
# rejection" negates only "rejection". Checking these in a mention's
# AFTER-reach (as one combined set used to) reads them backwards, disclaiming
# the very mention they affirm.
_REASON_NEGATION_BIDIRECTIONAL = re.compile(r"\b(?:not|isn'?t|wasn'?t|never)\b", re.IGNORECASE)
_REASON_NEGATION_FORWARD_ONLY = re.compile(
    r"\b(?:rather than|instead of|unlike|as opposed to|without)\b", re.IGNORECASE
)

# "no" is deliberately NOT in the bidirectional set above, for the exact
# reason its own comment gives -- but "No cap breach occurred" and "no
# rejection" are genuine, common noun-phrase disclaimers this corpus uses
# too (Codex review: "Treat `no cap breach` as a negated verdict"), and
# leaving "no" out entirely missed them. The difference from the rejected
# broad form is ADJACENCY: this is anchored with `$` and checked ONLY
# against the immediate run-up to the mention (see _mention_is_negated),
# so it fires for "no cap breach" (where "no" is the word directly before
# the matched "cap breach") but not for "no signal-generation entry point
# exists" naming GATE5_DISABLED elsewhere in the same sentence -- "no"
# there is nowhere near adjacent to that separate mention.
_REASON_NEGATION_ADJACENT_NO = re.compile(r"\bno\s*$", re.IGNORECASE)

# Affirmative statements that email delivery did not happen. The corrected Gate 9
# contract requires an executor falling back to a draft to say so in the primary
# alert, so a repo alert file can carry this evidence even when no draft export
# is present -- and then the draft-source heuristic alone would miss it entirely.
#
# "no send capability" is deliberately NOT here: unqualified, it matched equally
# well against "Slack connector has no send capability; email sent successfully"
# -- see _SEND_FAILED below, which requires the same clause to name email.
_EMAIL_FAILURE = re.compile(
    r"email (?:could not|couldn'?t|was not|wasn'?t|was never) (?:be )?(?:sent|delivered)"
    # "was never sent"/"was never delivered" state non-delivery just as
    # directly as "was not" -- and matched neither this pattern nor the
    # negated-success path, so "Email was never sent" and "Email was never
    # delivered" left states_email_failure() returning False on explicit
    # non-delivery evidence (Codex review: "Recognize 'never sent' as email
    # non-delivery").
    r"|email delivery did not happen"
    # "Email delivery failed" is a simpler, equally natural way to report the
    # exact same outcome as "email delivery did not happen" -- and matched
    # neither this branch nor _SEND_FAILED, which requires the word "send"
    # rather than "delivery" (Codex review: "Recognize explicit
    # delivery-failure wording").
    r"|email delivery (?:failed|failure)"
    # The bare fallback form -- "Email failed; draft created" or "Email
    # failed and was saved as a draft" -- names neither "delivery" nor
    # "send", so it matched nothing here or in _SEND_FAILED (which requires
    # the literal word "send"), and states_email_failure() returned False on
    # explicit non-delivery evidence (Codex review: "Recognize bare
    # email-failure fallback wording"). Self-scoped like every other
    # alternative here -- "email" and "failed" sit adjacent in the phrase
    # itself, so no separate clause check is needed.
    r"|\bemail failed\b"
    # "Email delivery was rejected by the provider", "Email send was
    # blocked", "Email send errored" -- three more ordinary ways to state
    # non-delivery that named neither "could not/never be sent" nor
    # "failed"/"delivery failed", so none of the alternatives above
    # matched them (Codex review: "Recognize rejected email-delivery
    # outcomes").
    r"|email (?:delivery|send) (?:was rejected|was blocked|errored)",
    re.IGNORECASE,
)

# Marks a failure claim as describing an EARLIER, superseded attempt rather
# than this record's own final outcome. Used only for a gmail_message
# record's body (see detect_channel_warnings) -- a sent message's own
# existence is direct proof of eventual delivery, so a body that explicitly
# frames its failure as the FIRST of multiple attempts is not contradicting
# that; a bare, unqualified failure claim ("Email could not be sent.") is
# not exempted this way and is still trusted at face value.
_MESSAGE_FIRST_ATTEMPT_QUALIFIER = re.compile(r"\bon (?:the )?first attempt\b|\binitially\b", re.IGNORECASE)


def _message_failure_is_first_attempt_qualified(body: str) -> bool:
    """Whether a SENTENCE that both names email and states a failure also
    carries a first-attempt qualifier, scoped to that one sentence, AND
    binding to the specific failure(s) actually present rather than any
    qualifier occurring anywhere in it.

    A body-wide search for the qualifier is too broad: "Initially Gate 5
    failed. Email could not be sent." has "Initially" describing an
    UNRELATED earlier event (Gate 5), not the email failure in the next
    sentence, and a body-wide match wrongly exempted this genuinely
    current, unqualified email failure (Codex review: "Scope the attempt
    qualifier to the email failure"). Requiring the qualifier, an email
    mention, AND a failure indicator all in the SAME sentence -- the shape
    every worked qualifying example in this corpus actually uses ("Email
    send failed on the first attempt; draft created", itself one sentence
    since ";" is a _SENTENCE_SPLIT boundary) -- fixes that without
    requiring _MESSAGE_FIRST_ATTEMPT_QUALIFIER's own match position to be
    threaded back out of states_email_failure()'s more elaborate scan.

    Sentence-level co-occurrence alone is still too coarse, though: "Email
    failed initially, but retry email could not be sent" is ONE sentence
    (no sentence-boundary punctuation between the two clauses) naming TWO
    outcomes -- an earlier, qualified failure and a later, unqualified one
    that is the actual surviving state states_email_failure() reports.
    Sentence-wide co-occurrence saw "initially" and a failure indicator
    both present and exempted the whole sentence, even though "initially"
    modifies only the FIRST clause, not the retry's own (Codex review:
    "Bind the first-attempt exemption to the surviving failure"). Each
    failure-pattern match is now required to carry the qualifier in its
    OWN clause (split the same way the negation and exemption checks
    elsewhere in this module scope to a clause -- see _CLAUSE_SPLIT), and
    a sentence only counts when EVERY failure match in it is so qualified
    -- one unqualified failure clause is enough to treat the whole
    sentence as reporting a genuine, current non-delivery.

    Nor is it enough for the FIRST qualifying sentence found to pass the
    check: "Email send failed initially. Retry email could not be sent."
    splits into two sentences, and returning as soon as the first one (all
    qualified) passed never reached the second one's own unqualified
    failure -- the actual surviving state -- at all (Codex review: "Check
    every failure sentence before granting the exemption"). Every sentence
    that names both email and a failure is now checked, across the WHOLE
    body, and the body is exempted only when every one of them is fully
    qualified; a single unqualified sentence anywhere denies the exemption
    outright, matching the same "one unqualified clause is enough" bias
    the per-sentence check above already applies.
    """
    found_qualifying_sentence = False
    for sent_start, sent_end in _clause_spans(body, _SENTENCE_SPLIT):
        sentence = body[sent_start:sent_end]
        if not _EMAIL_MENTION.search(sentence):
            continue
        # _SEND_FAILED matches are only email evidence when the SAME CLAUSE
        # also names email -- the exact scoping states_email_failure()
        # itself already applies via the _EMAIL_MENTION.search(clause)
        # check elsewhere in that function. Collecting every _SEND_FAILED
        # match in the whole SENTENCE, unfiltered, let an unrelated OTHER
        # channel's failure sharing the sentence with a genuine, qualified
        # email failure count as an "unqualified" email failure of its own:
        # "Email send failed initially, but the backup connector send
        # failed." has "initially" in the email clause only, and the
        # backup-connector clause's own unrelated "send failed" -- which
        # never names email at all -- failed the all-qualified check and
        # denied the exemption despite the sent message proving the email
        # itself was eventually delivered (Codex review: "Scope
        # first-attempt qualification to email failures"). _EMAIL_FAILURE
        # needs no such filter -- its own docstring guarantees every one of
        # its patterns already names email.
        raw_failure_matches = list(_EMAIL_FAILURE.finditer(sentence)) + [
            match
            for match in _SEND_FAILED.finditer(sentence)
            if _EMAIL_MENTION.search(_clause_containing(sentence, match.start(), _CLAUSE_SPLIT))
        ]
        # A failure match that merely RECOUNTS an earlier run's history --
        # "The previous run email failed" -- must not be held to the
        # first-attempt-qualifier requirement below: it is not the CURRENT
        # failure states_email_failure() is reporting at all, and denying
        # the exemption over an unqualified HISTORICAL mention penalizes a
        # sentence that never needed qualifying in the first place. Same
        # _mention_has_prior_event screen states_email_failure() itself
        # applies to its own _EMAIL_FAILURE/_SEND_FAILED loops (Codex
        # review: "Screen non-current failures before denying the
        # exemption").
        failure_matches = [
            match for match in raw_failure_matches if not _mention_has_prior_event(sentence, match.start(), 0)
        ]
        if not failure_matches:
            continue
        found_qualifying_sentence = True
        if not all(
            _MESSAGE_FIRST_ATTEMPT_QUALIFIER.search(_clause_containing(sentence, match.start(), _CLAUSE_SPLIT))
            for match in failure_matches
        ):
            return False
    return found_qualifying_sentence

# A send that was attempted and failed, or a connector reporting it has no
# capability to send at all. The corrected prompts require both to be recorded:
# absent capability and a failed send leave the operator equally un-alerted, and
# naming only one left the other with no evidence anywhere for the digest to
# find.
#
# Unqualified, though -- an alert file legitimately reports OTHER channels
# failing or lacking capability ("Slack connector has no send capability; email
# sent successfully"), and reading that as an email failure raises
# --fail-on-warnings on a run whose email went through. So each of these only
# counts inside a CLAUSE that also names email, not merely the same sentence --
# one sentence routinely narrates both channels' outcomes together ("Slack
# could not send, but the alert email went out fine"), and sentence-level
# scoping alone reads the whole thing as one email failure.
_SEND_FAILED = re.compile(
    r"send (?:attempt )?failed"
    r"|failed to send"
    r"|(?:could not|couldn'?t)\s+send"
    r"|(?:was |were )?unable to send"
    r"|send attempt fails"
    # "Sending email failed" -- active voice, and just as natural a way for an
    # executor to record the attempt as the passive forms above. Bounded to the
    # sentence so it cannot reach across into unrelated text.
    r"|\bsending\b[^.;\n]*\bfailed\b"
    r"|no send capability"
    # The corrected prompts explicitly permit auth/throttling/provider/network
    # errors as the reason a send failed (Gate 9's fallback text names exactly
    # these four categories), and an executor recording one of them often
    # writes neither "send" nor "failed" together at all -- "Email
    # authentication failed", "Email provider timed out", "Email send
    # returned 401 Unauthorized".
    #
    # "failed" is not the only way an auth problem is reported, though: an
    # executor as naturally writes "Email authentication error; draft
    # created" or the shorter "Email auth error; draft created" -- neither
    # matched the "failed"-only phrasing above, so this branch and the
    # narrower draft patterns both missed it and states_email_failure()
    # returned False on explicit non-delivery evidence (Codex review:
    # "Recognize authentication-error fallback wording").
    r"|authentication (?:failed|error)"
    r"|\bauth\b\s*(?:failed|error)"
    r"|\bthrottl(?:ed|ing|e)\b"
    r"|provider (?:error|timed out)"
    r"|network (?:error|timed out)"
    r"|\btimed out\b"
    # Restricted to the 4xx/5xx failure classes -- "Email send returned 200
    # OK" is a SUCCESS response and matched this unconditionally when any
    # three-digit code counted, registering a genuine successful send as a
    # failure (Codex review: "Restrict returned status failures to error
    # codes"). The worked example this branch was added for, "401
    # Unauthorized", is itself a 4xx code.
    r"|\breturned\s+[45]\d{2}\b"
    r"|\bunauthorized\b"
    # Moved here from _EMAIL_FAILURE (Codex review: "Require draft fallbacks
    # to identify email"): _EMAIL_FAILURE's own docstring claims every one
    # of its patterns already names email, but this one didn't -- "Order
    # draft was created instead of submission" has nothing to do with
    # email at all, and matched unconditionally. _SEND_FAILED's matches
    # only ever count when the SAME CLAUSE also names email (see the
    # _EMAIL_MENTION.search(clause) check in states_email_failure()), which
    # is exactly the scoping this pattern was always missing.
    r"|(?:draft was created|created a draft) instead"
    # The narrower "... instead" forms above only ever matched when the
    # executor explicitly contrasted the draft against a submission attempt.
    # Gate 9's draft fallback is just as often stated PLAINLY, with no
    # "instead" at all -- "Email draft created", "Email was saved as a
    # draft", "Email remained in drafts" -- and each of these directly
    # establishes non-delivery on its own, the same as any other _SEND_FAILED
    # match (still subject to the SAME _EMAIL_MENTION.search(clause) scoping,
    # and to a later, genuine send-success overriding it) (Codex review:
    # "Treat explicit email drafts as non-delivery").
    r"|\bdraft (?:was |were )?created\b|\bcreated an? draft\b"
    r"|\bsaved as an? draft\b"
    r"|\bremained (?:in drafts|as an? draft)\b",
    re.IGNORECASE,
)

_EMAIL_MENTION = re.compile(r"\be-?mail\b", re.IGNORECASE)

# An explicit claim that email delivery DID happen, checked only when it
# names email in its own clause -- same reasoning as _SEND_FAILED. Exists so
# a retry's success can override an earlier failed attempt reported in the
# same sentence: "Email could not be sent on the first attempt, but the retry
# email sent successfully" narrates one outcome (success), not two, and
# reporting the failed first attempt as the current state would raise
# --fail-on-warnings on a run whose email ultimately went through.
#
# The adverb "successfully" is not the only way an executor phrases a plain
# delivery outcome: "Email failed to send initially, but retry email was
# delivered" and "retry email succeeded" report the exact same override, with
# no adverb at all (Codex review: "Recognize plain retry delivery outcomes").
# Both forms are still screened by the same before-reach negation check every
# caller already applies to this pattern's matches, so "retry email was NOT
# delivered" and "the retry never succeeded" are unaffected.
#
# Unlike the three adverb-bearing forms above, a bare "succeeded"/"was
# delivered" is not inherently about DELIVERY -- it is just as grammatical
# describing some other action the same clause happens to also mention email
# alongside: "Email send failed, but saving the email draft succeeded" has
# _EMAIL_MENTION satisfied by "email draft" while "succeeded" actually
# describes the FALLBACK action, not delivery, and reads as an override
# (Codex review: "Restrict plain success matches to actual email delivery").
# So these two alternatives require "succeeded"/"was/were delivered" to
# immediately follow an email- or send-shaped word -- "email(s) succeeded",
# "retry email was delivered", "resend succeeded" -- not merely to share a
# clause with one. "draft succeeded" or "draft was delivered" do not match.
#
# The plain, adverb-free override is not limited to "delivered"/"succeeded"
# either: "Email failed to send initially, but retry email was sent" is the
# exact same shape with the plainest verb of all -- "sent" -- and matched
# none of the alternatives above (Codex review: "Recognize plain sent
# outcomes for successful retries"). Given the same email/send-shaped-
# subject requirement as the other two plain forms, for the same reason.
#
# Split into two patterns, not one, because each caller's ADDITIONAL
# _EMAIL_MENTION.search(clause) requirement is right for the adverb forms
# above but wrong for the subject-scoped ones: "resend succeeded" is
# already fully delivery-scoped by its own "resend" subject, and demanding
# the literal word "email" ELSEWHERE in the same clause on top of that
# rejected it outright -- "Email failed to send initially, but resend
# succeeded" has no second "email" mention anywhere in its success clause
# (Codex review: "Let a successful resend clear the earlier failure").
# _EMAIL_SUCCESS (the union of both) is still what every match-finding call
# site searches with; _EMAIL_SUCCESS_SUBJECT_SCOPED is what callers check
# is ALSO true before waiving their _EMAIL_MENTION requirement.
_EMAIL_SUCCESS_ADVERB = re.compile(
    r"sent successfully|successfully (?:sent|delivered)|delivered successfully",
    re.IGNORECASE,
)
_EMAIL_SUCCESS_SUBJECT_SCOPED = re.compile(
    # "email delivery" is a two-word subject just as common as the bare
    # stems below -- "email delivery succeeded", "email delivery was
    # successful" -- and none of the alternatives matched it, since the
    # subject and its outcome verb were required directly adjacent
    # (Codex review: "Recognize email-delivery success subjects"). The
    # optional "delivery" is inserted once here, shared by every
    # alternative below that starts from this same subject stem, rather
    # than duplicated per-alternative.
    r"\b(?:e-?mail|send|resend|retry)\w*(?:\s+delivery)?\s+"
    # The perfect passive ("has been sent", "have been delivered") is just as
    # ordinary a way to report a completed retry as the simple past ("was
    # sent") already handled above -- "retry email has now been sent" -- and
    # matched neither this nor any other alternative here (Codex review:
    # "Recognize perfect-passive retry successes"). A single adverb (e.g.
    # "now", "already") is permitted between the auxiliary and "been", the
    # one shape that ordinary phrasing actually inserts there.
    # A bounded completion adverb -- "finally", "eventually",
    # "successfully" -- is ordinary phrasing here too, the same as the
    # "went out" form below already allows: "the email was eventually
    # sent" reports the exact same outcome and matched nothing, since
    # the auxiliary and "sent"/"delivered" were required adjacent
    # (Codex review: "Allow completion adverbs before passive send
    # verbs"). Same fixed, short adverb list as "went out" -- not an
    # unbounded gap, for the same negation-admitting reason.
    r"(?:was|were|has(?:\s+\w+)?\s+been|have(?:\s+\w+)?\s+been)"
    r"\s+(?:finally\s+|eventually\s+|successfully\s+)?(?:delivered|sent)\b"
    r"|\b(?:e-?mail|send|resend|retry)\w*(?:\s+delivery)?\s+succeeded\b"
    # "went out" is ordinary, common delivery-success phrasing for a final
    # retry -- "Email send failed initially, but the retry email went out"
    # -- and matched none of the forms above (Codex review: "Recognize
    # 'went out' as a retry success").
    #
    # A bounded adverb -- "finally", "eventually", "successfully" -- is
    # ordinary phrasing too: "the retry email finally went out" reports the
    # exact same outcome and matched nothing, since the subject and "went
    # out" were required adjacent (Codex review: "Allow modifiers in
    # retry-success phrases"). The alternation is a fixed, SHORT list of
    # genuinely affirming adverbs, not an unbounded `\w+` gap -- an
    # unbounded gap would just as readily admit a NEGATING word ("the retry
    # email never went out"), flipping this into a false success match for
    # exactly the failure the pattern must not clear.
    r"|\b(?:e-?mail|send|resend|retry)\w*(?:\s+delivery)?\s+(?:finally\s+|eventually\s+|successfully\s+)?went\s+out\b"
    # "was/were successful" is another plain, adjective-form outcome
    # alongside "succeeded"/"was sent"/"was delivered" above -- "retry
    # email send was successful" -- and matched none of them, since the
    # "was|were" alternative above only ever led to "delivered"/"sent",
    # never a bare "successful" (Codex review: "Recognize successful
    # retry-send outcomes").
    r"|\b(?:e-?mail|send|resend|retry)\w*(?:\s+delivery)?\s+(?:was|were)\s+successful\b",
    re.IGNORECASE,
)
_EMAIL_SUCCESS = re.compile(
    _EMAIL_SUCCESS_ADVERB.pattern + "|" + _EMAIL_SUCCESS_SUBJECT_SCOPED.pattern,
    re.IGNORECASE,
)

# A finer split than _SENTENCE_SPLIT, purely for scoping _SEND_FAILED to the
# clause that actually names email. Comma and the common coordinating
# conjunctions are where one sentence switches from narrating one channel's
# outcome to another's -- "and" included: "Slack could not send and email sent
# successfully" is two channel outcomes joined additively, not adversatively,
# but still needs splitting or _SEND_FAILED (Slack's failure) and
# _EMAIL_MENTION (the successful email) both match within one undivided clause.
#
# "then" included for the same reason: "Email failed to send initially then
# retry email sent successfully" narrates two SEQUENTIAL outcomes with no
# comma or coordinating conjunction at all -- without it as a boundary, the
# failure clause span ran to the end of the whole sentence, so the success
# scan's `start >= last_failure_end` check could never be satisfied by a
# success reported in the very same, unsplit clause.
#
# "after" included too, for a different reason than every other boundary
# here: "Email sent successfully after provider timed out on the first
# attempt" states the CURRENT outcome (success) before the word "after" and
# the now-superseded earlier problem after it -- the opposite text order
# from every other construct this splitter handles. Splitting there moves
# the trailing connector-error phrase into its OWN clause, which no longer
# names email (the "Email" token stays in the success clause before "after")
# -- so it correctly stops counting as a NEW email failure via the
# clause-scoped _EMAIL_MENTION check the failure scan already requires,
# without needing any ordering-aware logic of its own.
#
# "although"/"though" are the same adversative shape as "but"/"however"/
# "while"/"whereas"/"yet" already here, just missing: "Email could not be
# sent initially although retry email was sent successfully" left the
# whole sentence as ONE clause, so the final override check's
# `sent_start + start >= last_failure_end` compared the clause's START
# (before the failure) rather than the success match's own later position,
# and the later success could never satisfy it (Codex review: "Split retry
# outcomes introduced by 'although'").
#
# "because" is a boundary too, for the same reason a negation check needs
# it: "No alert reached the operator because it was posted only to Slack"
# and "The order was never placed because the broker rejected it" both
# have a negation cue in the MAIN clause reaching across "because" into a
# causal clause that is not itself negated -- it affirmatively explains the
# main clause's outcome. Every negation check in this module that bounds
# its reach to a clause (``_mention_is_negated``'s default splitter, and
# ``_is_affirmative_claim``'s whole-clause search) shares this splitter, so
# adding the boundary here fixes both at once (Codex reviews: "Bind channel
# negation to the Slack delivery claim", "Bind verdict negation to the
# rejected predicate").
_CLAUSE_SPLIT = re.compile(
    r",|\b(?:but|however|while|whereas|yet|although|though|and|then|after|because)\b", re.IGNORECASE
)

# A narrower split than _CLAUSE_SPLIT, only for scoping _PRIOR_EVENT. "and"
# coordinates two predicates sharing one subject as readily as it separates two
# unrelated ones: "The previous run aborted and its order was rejected" has
# both predicates describing that SAME prior run. Splitting on "and" there
# strands the second predicate without the "previous run" cue that marks it as
# history, so _mention_has_prior_event would misread it as classifying the
# current run. Channel-outcome clauses (_CLAUSE_SPLIT's own reason for
# including "and") don't share this problem -- they don't inherit a subject
# across the conjunction the way a historical marker does.
#
# ";" is included (unlike in _CLAUSE_SPLIT) for the finer split WITHIN one
# sentence: "Failing gate: Gate 3; an earlier gate rather than GATE5_DISABLED"
# joins two clauses with a semicolon, and without it as a boundary here,
# "earlier" in the second clause would reach back and mark the FIRST clause's
# "Gate 3" as history too. Redundant with _SENTENCE_SPLIT (which also splits
# on ";") for a caller that already receives one pre-split sentence at a
# time, but still load-bearing for a caller that instead passes a whole,
# unsplit subject/body straight to _mention_has_prior_event -- which itself
# bounds to the surrounding _SENTENCE_SPLIT sentence FIRST, then to the
# finer clause here; see that function's docstring.
#
# "because" IS a boundary here (unlike "and"), for the mirror-image reason
# _CLAUSE_SPLIT already treats it as one for negation: "This run ABORTED
# because the previous run left stale state" has the causal explanation's
# own "previous run" reaching BACKWARD across "because" and marking the
# CURRENT run's own stated "ABORTED" as historical too, so detect_verdict()
# returned None for an explicitly stated current outcome (Codex review:
# "Keep current outcomes before prior-event causes"). Unlike "and", a
# causal clause does not inherit its subject from the clause it explains --
# "X aborted because Y" names two different parties (this run; the cause),
# not one shared subject the way "the previous run aborted and its order
# was rejected" does -- so splitting here does not strand a historical
# cue away from a predicate it still needs to modify.
_PRIOR_EVENT_CLAUSE_SPLIT = re.compile(r",|;|\b(?:but|however|while|whereas|yet|because)\b", re.IGNORECASE)

# A subordinate-clause opener ("when", "while", "where") that can introduce
# an UNRELATED trailing clause between a negation cue and the email-success
# match it merely happens to precede within the fixed _NEGATION_REACH
# window: "no problems occurred WHEN retry email was sent" has "no
# problems occurred" as its own complete, unrelated main clause -- "no"
# there does not negate the send at all, but a blind 40-character
# before-reach still found it (Codex review: "Scope success negation to
# the delivery verb"). Used only to bound the success-negation before-
# reach in states_email_failure(): the reach is truncated at the LAST such
# opener found within it, so nothing before that subordinate boundary is
# considered.
_SUCCESS_NEGATION_REACH_BOUNDARY = re.compile(r"\b(?:when|while|where)\b", re.IGNORECASE)

# The same shape of problem, for the FAILURE side: "No delivery occurred
# because email failed" has "No" negating "delivery occurred", an entirely
# different main-clause claim, not the "because"-introduced causal clause
# that actually AFFIRMS the failure -- but a blind 40-character before-reach
# still found it and discarded the genuine failure match (Codex review:
# "Bind failure negation to the failed email claim"). Used only to bound
# the _EMAIL_FAILURE before-reach in states_email_failure(); truncated the
# same way _success_negation_before_reach truncates at a subordinate-clause
# opener, just at "because" instead.
_FAILURE_NEGATION_REACH_BOUNDARY = re.compile(r"\bbecause\b", re.IGNORECASE)


def _failure_negation_before_reach(text: str, position: int) -> str:
    """The bounded run-up to ``position`` used to screen an _EMAIL_FAILURE
    match for negation, truncated at the last causal-clause opener within it
    (see _FAILURE_NEGATION_REACH_BOUNDARY)."""
    window = text[:position][-_NEGATION_REACH:]
    boundary_end = 0
    for boundary in _FAILURE_NEGATION_REACH_BOUNDARY.finditer(window):
        boundary_end = boundary.end()
    return window[boundary_end:]


def _success_negation_before_reach(text: str, position: int, min_position: int = 0) -> str:
    """The bounded run-up to ``position`` used to screen an email-success
    match for negation, truncated at the last subordinate-clause opener
    within it (see _SUCCESS_NEGATION_REACH_BOUNDARY).

    ``min_position`` additionally clips the window's start so it never
    reaches earlier than the END of a PREVIOUS success match already
    examined in the same clause. Every caller iterating multiple
    ``_EMAIL_SUCCESS`` matches in one span must pass the previous match's
    own end here -- without it, "Email was not sent successfully: retry
    email sent successfully despite a provider error" has a colon (not a
    _CLAUSE_SPLIT boundary) joining a negated first attempt to a genuinely
    unnegated later success, and the raw 40-character window for that
    SECOND match still reached backward across the colon into the FIRST
    match's own "not" (Codex review: "Inspect every success before
    truncating resolved errors").
    """
    start = max(position - _NEGATION_REACH, min_position)
    window = text[start:position]
    boundary_end = 0
    for boundary in _SUCCESS_NEGATION_REACH_BOUNDARY.finditer(window):
        boundary_end = boundary.end()
    return window[boundary_end:]


def states_email_failure(body: str) -> bool:
    """Whether the alert says, in its own words, that email delivery did not happen.

    ``_EMAIL_FAILURE`` is sentence-scoped -- every one of its patterns already
    names email, so nothing else needs to. ``_SEND_FAILED`` is CLAUSE-scoped: a
    generic failed-send (or no-capability) phrase is only read as an email
    failure when email is named in the same clause, not merely the same
    sentence.

    The LAST failure found anywhere in the body is then checked against any
    email-success clause AFTER it, anywhere in the REST of the body -- not
    just its own sentence. Gate 9 permits free-form follow-up evidence, and
    executors naturally put a retry's outcome in its own sentence ("Email
    could not be sent. Retried, and email sent successfully."), not always
    joined to the failure by a comma or conjunction. A retry's success is the
    body's real outcome, and reporting the failed first attempt would raise
    --fail-on-warnings on a run whose email ultimately went through. The
    success clause must itself name email, for the same reason _SEND_FAILED
    requires it -- an unscoped "sent successfully" could belong to another
    channel entirely.
    """
    last_failure_end: Optional[int] = None
    for sent_start, sent_end in _clause_spans(body, _SENTENCE_SPLIT):
        sentence = body[sent_start:sent_end]
        # A sentence framed as "success ... after <earlier problem>" states
        # the CURRENT outcome (success) before "after" and a now-superseded
        # problem after it -- the opposite text order from every other
        # construct this scan handles. Splitting on "after" (_CLAUSE_SPLIT)
        # usually keeps the trailing problem from re-triggering a failure,
        # since it drops "email" out of that trailing clause -- but "Email
        # sent successfully after the first EMAIL send timed out" repeats
        # the word in its own trailing clause, so that side effect alone
        # isn't enough. The failure scan below is truncated right before
        # "after" instead: nothing past a genuine, unnegated success
        # followed by "after" can register as a NEW failure here, no matter
        # what it goes on to mention.
        #
        # "because" and "once" introduce the same resolved-cause shape, not
        # just "after": "Email sent successfully because the network error
        # was resolved" states the success FIRST and a now-superseded cause
        # after it, exactly like the "after" case -- but neither "because"
        # nor "once" is a _CLAUSE_SPLIT boundary, so without truncating
        # there too, "network error" stayed in the SAME undivided clause as
        # "Email", satisfying both _SEND_FAILED and the clause's own
        # _EMAIL_MENTION and registering as a brand new failure (Codex
        # review: "Treat resolved causal errors as superseded").
        #
        # "before" belongs here too, for a related but distinct reason: it
        # sequences a genuine email success against a DIFFERENT channel's
        # later problem -- "Email was sent before DCA_DRIFT_SLACK_WEBHOOK
        # timed out" states the email send completed, then names an
        # unrelated Slack timeout that happens to share the clause (and
        # thus _SEND_FAILED's "timed out" plus the clause's own
        # _EMAIL_MENTION), which registered as a brand new email failure
        # despite belonging to a completely different, permitted route
        # (Codex review: "Keep another channel's timeout from reopening
        # email failure").
        #
        # "despite" is the same success-first, now-superseded-problem shape
        # too, just concessive rather than causal or sequential: "Email
        # sent successfully despite a provider error" states the outcome
        # (success) FIRST and names the error only to say it did not
        # prevent delivery, exactly like "after"/"because" (Codex review:
        # "Treat errors after 'despite' as superseded").
        # Every _EMAIL_SUCCESS occurrence in the sentence is tried, not just
        # the first, stopping at the first UNNEGATED one: "Email was not
        # sent successfully: retry email sent successfully despite a
        # provider error" has a colon (not a _CLAUSE_SPLIT boundary) joining
        # a negated first attempt to a genuine later success, and searching
        # only the first match meant the negated one's own negation check
        # failing left scan_end at the whole sentence, so the later
        # success's own now-superseded "despite" error still registered as
        # a brand new failure (Codex review: "Inspect every success before
        # truncating resolved errors").
        scan_end = sent_end
        previous_success_end = 0
        for success_match in _EMAIL_SUCCESS.finditer(sentence):
            before_success = _success_negation_before_reach(sentence, success_match.start(), previous_success_end)
            previous_success_end = success_match.end()
            if _NEGATION_CUES.search(before_success):
                continue
            after_match = re.search(
                r"\b(?:after|because|once|before|despite)\b", sentence[success_match.end() :], re.IGNORECASE
            )
            if after_match:
                scan_end = sent_start + success_match.end() + after_match.start()
            break
        scan_sentence = body[sent_start:scan_end]
        # Every _EMAIL_FAILURE occurrence in the sentence is tried, not just
        # the first: "Email could not be sent, then email sent successfully,
        # but email wasn't delivered" has a failure both before AND after the
        # intervening success, and stopping at the first would let that
        # success clear the LATER failure it never actually overrides.
        #
        # Each match is screened for a NEGATION cue in its own before-reach,
        # the same _NEGATION_CUES set (which includes "no") this function
        # already uses for the success-negation checks below -- neither loop
        # screened for this at all before, so "No email delivery failure
        # occurred" matched _EMAIL_FAILURE's "email delivery failure"
        # unconditionally and registered as a genuine current failure
        # (Codex review: "Ignore explicitly negated delivery failures").
        for failure_match in _EMAIL_FAILURE.finditer(scan_sentence):
            before = _failure_negation_before_reach(scan_sentence, failure_match.start())
            if _NEGATION_CUES.search(before):
                continue
            # A failure mention that merely RECOUNTS an earlier run's history
            # must not register as the CURRENT run's own failure: "Email
            # sent successfully. The previous run's email failed." has the
            # historical failure positioned after the current sentence's own
            # success, and an unscreened match let it become last_failure_end
            # anyway, so the real current success could never override it
            # (Codex review: "Ignore historical email failures when deriving
            # current delivery") -- the mirror image of the historical-
            # success screen already applied to the override scan below.
            # scan_sentence shares sent_start's offset with sentence (it is
            # body[sent_start:scan_end], a prefix of body[sent_start:sent_end]),
            # so failure_match's position means the same thing in either.
            if _mention_has_prior_event(sentence, failure_match.start(), 0):
                continue
            end = sent_start + failure_match.end()
            last_failure_end = end if last_failure_end is None else max(last_failure_end, end)
        for start, end in _clause_spans(scan_sentence, _CLAUSE_SPLIT):
            clause = scan_sentence[start:end]
            if _EMAIL_MENTION.search(clause):
                # Every _SEND_FAILED occurrence in the clause is tried, not
                # just the first: a colon (not a _CLAUSE_SPLIT boundary) can
                # join two outcomes into one clause the same way it does for
                # _EMAIL_SUCCESS below, e.g. "Email send was not throttled:
                # retry send failed" -- stopping at the first, negated match
                # ("not throttled") skipped the whole clause via `continue`
                # and never reached the later, genuinely unnegated "send
                # failed" (Codex review: "Examine every generic failure
                # outcome in a clause").
                clause_has_unnegated_failure = False
                failure_match_end = None
                previous_match_end = 0
                for send_failed_match in _SEND_FAILED.finditer(clause):
                    # The before-reach is clipped at the END of the PREVIOUS
                    # match in this clause, not just the raw 40-char window:
                    # without this, "Email send was not throttled: retry
                    # send failed" reads the second match's before-reach as
                    # "...not throttled: retry " -- still within 40 chars --
                    # and the "not" negating the FIRST, unrelated match
                    # wrongly negates the second one too.
                    before = clause[previous_match_end : send_failed_match.start()][-_NEGATION_REACH:]
                    previous_match_end = send_failed_match.end()
                    if _NEGATION_CUES.search(before):
                        continue
                    # Same historical-mention screen as the _EMAIL_FAILURE
                    # loop above, for the identical reason: "The previous
                    # run email send failed" must not register as this
                    # sentence's own current failure.
                    if _mention_has_prior_event(sentence, start + send_failed_match.start(), 0):
                        continue
                    clause_has_unnegated_failure = True
                    failure_match_end = send_failed_match.end()
                    break
                if clause_has_unnegated_failure and failure_match_end is not None:
                    # The match's OWN end position, not the whole clause's:
                    # a colon (not a _CLAUSE_SPLIT boundary) can join a
                    # failed attempt and its successful retry into one
                    # clause -- "Email send failed initially: retry email
                    # sent successfully" -- and extending last_failure_end
                    # to the clause's end swallowed the later,
                    # already-successful retry too, so the override scan
                    # below could never see a success positioned after it
                    # (Codex review: "End generic failures at the matched
                    # outcome"). Mirrors the identical fix already applied
                    # to the negated-EMAIL_SUCCESS-is-a-failure branch just
                    # below, for the same colon-joined-clause reason.
                    abs_end = sent_start + start + failure_match_end
                    last_failure_end = abs_end if last_failure_end is None else max(last_failure_end, abs_end)
                    continue
            # A NEGATED success claim ("Retry email was not successfully
            # sent") is itself a failure statement, and neither _EMAIL_FAILURE
            # nor _SEND_FAILED matches its wording -- without this, an alert
            # whose ONLY delivery evidence is a negated-success sentence never
            # reaches the negation screening below at all, because
            # `last_failure_end` stays None and the function returns early.
            # Same before-reach negation check the override scan below uses.
            #
            # Every _EMAIL_SUCCESS occurrence in the clause is tried, not
            # just the first, and each negated one extends last_failure_end
            # only to ITS OWN end position -- not the whole clause's end.
            # "First retry email was not sent successfully: second retry
            # email sent successfully." has a colon (not a _CLAUSE_SPLIT
            # boundary) joining two outcomes into one clause; extending
            # last_failure_end to the CLAUSE's end for the first, negated
            # match swallowed the later, genuinely unnegated second success
            # too, so it could never satisfy the override scan's own
            # position check below (Codex review: "Examine every success
            # outcome in a clause").
            if _EMAIL_MENTION.search(clause) or _EMAIL_SUCCESS_SUBJECT_SCOPED.search(clause):
                previous_success_end = 0
                for success_match in _EMAIL_SUCCESS.finditer(clause):
                    before = _success_negation_before_reach(clause, success_match.start(), previous_success_end)
                    previous_success_end = success_match.end()
                    if (
                        _NEGATION_CUES.search(before)
                        # Same historical-mention screen: "The previous run
                        # email was not sent successfully" is a negated
                        # success narrating an EARLIER run's own failure, not
                        # a claim about this sentence's current one.
                        and not _mention_has_prior_event(sentence, start + success_match.start(), 0)
                    ):
                        abs_end = sent_start + start + success_match.end()
                        last_failure_end = abs_end if last_failure_end is None else max(last_failure_end, abs_end)

    if last_failure_end is None:
        return False

    # Success is checked the same nested sentence-then-clause way the failure
    # scan above is, not clause spans alone: _CLAUSE_SPLIT doesn't split on
    # "." or ";", so "Email failed to send. Retry email sent successfully."
    # left the success span starting at offset 0 -- before last_failure_end
    # -- even though it is the LATER sentence's own outcome.
    #
    # A purported success is itself screened for negation before it can
    # override anything: "Retry email was not successfully sent" or "...
    # never delivered successfully" both contain the bare _EMAIL_SUCCESS
    # substring despite explicitly denying it. _NEGATION_CUES (the same set
    # channel-claim screening uses), not _REASON_NEGATION -- "never" and
    # "failed to" belong to this delivery-claim domain, not the reason-code
    # one _REASON_NEGATION was scoped for.
    #
    # Scoped to a short reach immediately BEFORE the success match, not the
    # whole clause: "Retry email sent successfully without the fallback
    # connector" has "without" modifying the connector, well after the
    # success phrase, and a clause-wide search read that as negating the
    # send itself. English negation adverbs precede what they modify, so
    # only the run-up to the match is checked.
    overridden = False
    for sent_start, sent_end in _clause_spans(body, _SENTENCE_SPLIT):
        sentence = body[sent_start:sent_end]
        for start, end in _clause_spans(sentence, _CLAUSE_SPLIT):
            clause = sentence[start:end]
            # Every _EMAIL_SUCCESS occurrence in the clause is tried, not
            # just the first: a punctuation mark outside _CLAUSE_SPLIT's own
            # boundary set (a colon, for instance) can join two retry
            # outcomes into one clause -- "First retry email was not sent
            # successfully: second retry email sent successfully." has a
            # NEGATED success before a later, genuinely unnegated one, and
            # `.search()` stopping at the first (negated) match never
            # reached the real override (Codex review: "Examine every
            # success outcome in a clause").
            #
            # Compared against the SUCCESS MATCH's own absolute position
            # (sent_start + start + success_match.start()), not the
            # CLAUSE's start (sent_start + start) -- the clause's start
            # precedes the earlier, negated failure text in exactly this
            # two-outcome-per-clause shape, so using it let a LATER,
            # genuinely later success wrongly compare as "before
            # last_failure_end" even though the success match itself comes
            # after the failure in the text.
            previous_success_end = 0
            for success_match in _EMAIL_SUCCESS.finditer(clause):
                before = _success_negation_before_reach(clause, success_match.start(), previous_success_end)
                previous_success_end = success_match.end()
                if (
                    sent_start + start + success_match.start() >= last_failure_end
                    and (_EMAIL_MENTION.search(clause) or _EMAIL_SUCCESS_SUBJECT_SCOPED.search(clause))
                    and not _NEGATION_CUES.search(before)
                    # A success mention that merely RECOUNTS an earlier run's
                    # history must not override the CURRENT failure just
                    # because it comes later in the text: "Email could not be
                    # sent. The previous run's email sent successfully." has
                    # the historical success positioned after the current
                    # failure, satisfying the position check above on text
                    # order alone, even though it classifies a DIFFERENT run
                    # (Codex review: "Ignore historical successes when
                    # resolving email failures"). The same _PRIOR_EVENT cue
                    # this module already screens every other success/
                    # failure match against.
                    and not _mention_has_prior_event(sentence, start + success_match.start(), 0)
                ):
                    overridden = True
                    break
            if overridden:
                break
        if overridden:
            break
    return not overridden


@dataclass(frozen=True)
class AlertRecord:
    """One executor alert, normalized out of whatever channel preserved it."""

    alert_id: str
    source: str  # "gmail_draft" | "alert_file"
    timestamp_utc: datetime
    subject: str
    body: str
    slot: Optional[str]
    gate: Optional[str]
    verdict: Optional[str]
    warnings: Tuple[Tuple[str, str], ...] = ()
    # A source-side revision signal, folded into `key`. Content alone is not
    # enough for a file: Gate 9's filename is fixed per (day, slot, gate), so a
    # retry of the same gate for the same reason overwrites the first with
    # byte-identical text, and a purely content-derived key would call the new
    # firing already-seen. The loader supplies the mtime; drafts leave it empty.
    revision: str = ""
    # Which signal identified `slot` -- see detect_slot_evidence. Carried because
    # `looks_like_executor_alert` cannot judge a slot without knowing how it was
    # found: a scheduler task name is proof, a wall-clock time is not.
    slot_evidence: str = ""
    # Which signal identified `gate` -- see detect_gate_evidence. Same reasoning
    # as `slot_evidence`: a reason code or a "Failing gate:" line is the
    # executor's own label, but a bare "Gate N" scanned out of free text is
    # ordinary English ("Meet at Gate 5 before boarding") and cannot stand alone
    # as proof this is an executor alert at all.
    gate_evidence: str = ""
    # Whether `timestamp_utc` for an `alert_file` record came from the Gate 9
    # contract filename ("filename") or from a fallback -- filesystem mtime,
    # for one with no valid contract-shaped name at all. See `et_date`: only
    # the filename form is authoritative by construction (loaded at noon ET,
    # immune to the midnight-crossing problem) and exempt from a body-stated
    # override; mtime is just "whenever this file happened to be written or
    # last touched on disk" and can drift from the alert's own event date
    # (a later copy or restore, for instance) with nothing to correct it
    # (Codex review: "Use declared dates for off-contract alert files").
    # Left "" for a Gmail-sourced record, which never has this distinction.
    date_evidence: str = ""
    # Ledger identities this record ALSO answers to, carried forward from a
    # counterpart merge_cross_source() consumed into it. That merge keeps the
    # surviving copy's own (source, alert_id) -- e.g. the file's -- so a
    # draft ledgered as new on one run reappears as "new" again once its
    # alert-file counterpart shows up and the merge relabels it under the
    # file's identity, even though nothing about the underlying event or its
    # warnings actually changed (Codex review: "Keep ledger identity stable
    # across source reconciliation"). The CONSUMED counterpart's own `key`
    # (computed before the merge, from its original source/alert_id/body) is
    # recorded here so a ledger diff can recognize the merged record as
    # already-seen under either identity. Deliberately NOT the surviving
    # side's own pre-merge key: that one's `key` docstring explains why a
    # cross-source merge folding in NEW warnings must still register as a
    # genuine discovery, which this must not defeat.
    prior_keys: Tuple[str, ...] = ()
    # Gmail's thread identifier is the only transport-level identity that can
    # prove a draft and a sent message belong to the same delivery attempt.
    # Message IDs are different when a draft is retried as a new send.
    thread_id: str = ""

    @property
    def et_date(self) -> date:
        """The ET calendar day the alert belongs to.

        Deliberately ET, not UTC: a 3:30 PM ET alert lands at 19:35 UTC and a
        7:30 PM one at 23:35, but an executor firing after 8 PM ET (as the
        2026-08-14 off-schedule batch did) crosses midnight UTC and would be
        filed under the *next* day by a naive UTC read, splitting one incident
        across two rows of the coverage matrix.

        A declared date -- Gate 9 requires the subject to name it, either in
        the explicit "Date: 2026-08-26" label form or as a bare ISO-shaped
        date in the subject (the live-mirror prompts only require the
        subject to *name* the ET date, not that exact label) -- wins over
        the transport timestamp when present, for a Gmail-sourced record
        ONLY. A delayed send or a retry can deliver well after the slot's
        own ET day ends, and reading the transport time alone would then
        file the alert under the WRONG day, splitting it from its
        correctly-dated alert-file copy at merge time.

        An ``alert_file`` record whose date came from the Gate 9 contract
        filename (``date_evidence == "filename"``) is exempt: that date is
        authoritative by construction (loaded at noon ET, immune to this
        same midnight-crossing problem). The body can still narrate an
        unrelated PRIOR date under the SAME "Date:" label ("Previous run
        Date: 2026-08-25" inside a file named for 2026-08-26), and letting
        that override the filename would misdate a correctly-dated file to
        the day it is only quoting.

        A file with NO valid contract filename is not exempt, though: the
        loader falls back to the file's filesystem mtime, which is not
        authoritative at all -- it is whenever the file happened to be
        written or last touched on disk, and a later copy or restore
        reassigns it to the copy day with nothing to correct it. A body
        field such as "Date: 2026-08-20" is exactly the correction the
        Gmail-record branch already trusts, and withholding it here purely
        because the source happens to be a file corrupts coverage and blocks
        reconciliation with a correctly-dated email counterpart (Codex
        review: "Use declared dates for off-contract alert files").
        """
        if self.source != "alert_file":
            # Both SUBJECT forms -- the labelled "Date:" field and the bare
            # ISO date the live-mirror contract also permits -- are checked
            # before the body's own labelled date. The subject's entire job
            # under the contract is to declare THIS alert's own date; the
            # body can carry a labelled date of its own while narrating an
            # unrelated PRIOR incident ("Previous run Date: 2026-08-25"), and
            # checking it first let that historical mention outrank the
            # current alert's own, differently-dated subject.
            declared = _DECLARED_ET_DATE.search(self.subject) or _BARE_ISO_DATE.search(self.subject)
            if declared is not None:
                try:
                    return date.fromisoformat(declared.group(1))
                except ValueError:
                    # ISO-SHAPED but not a real calendar date -- a malformed
                    # subject like "1030 ET | 2026-13-40 | Gate 5". Falling
                    # straight through to the transport day here, as the
                    # shared block below does for every OTHER unparseable
                    # match, skipped the body's own valid labelled date
                    # entirely (Codex review: "Fall through after malformed
                    # subject dates"); retry it here instead, the same
                    # fallback the file branch below already gets.
                    declared = _declared_body_date(self.body)
            else:
                declared = _declared_body_date(self.body)
        elif self.date_evidence != "filename":
            # NEVER the "subject" here: for a file record it is just the
            # filename, not a Gate 9-contract subject line, and a bare
            # ISO-shaped substring within it (a malformed contract name like
            # "2026-13-40-1030-gate5.txt") could coincidentally match
            # _BARE_ISO_DATE without being a real date at all, blocking the
            # fall-through to the body's own genuine "Date:" field.
            declared = _declared_body_date(self.body)
        else:
            declared = None
        if declared:
            try:
                return date.fromisoformat(declared.group(1))
            except ValueError:
                pass
        return self.timestamp_utc.astimezone(ET).date()

    @property
    def key(self) -> str:
        """Stable identity for ledger diffing across runs.

        The content fingerprint is part of the identity, not decoration. Both
        containers are mutable at a fixed address: Gate 9 names an alert file
        ``<day>-<slot>-<gate>.txt``, so a second firing on the same day, slot and
        gate overwrites the first, and a Gmail draft keeps its id when edited.
        Keying on location alone would let a freshly written alert -- with a new
        body and possibly a new delivery warning -- inherit the "already seen"
        verdict of the record it replaced, and ``--fail-on-warnings`` would stay
        green through it. Erring toward re-reporting is the safe direction here.

        The warning codes are folded in for the same reason one step removed: a
        cross-source merge keeps the file copy's subject and body and only adds
        the draft's warnings, so without them a run that had just discovered
        undelivered-email evidence for an already-ledgered alert would produce no
        new key, and ``--fail-on-warnings`` would stay green through exactly that
        discovery.
        """
        codes = ",".join(sorted(code for code, _ in self.warnings))
        material = f"{self.subject}\n{self.body}\n{codes}\n{self.revision}"
        fingerprint = sha256(material.encode()).hexdigest()[:12]
        return f"{self.source}:{self.alert_id}:{fingerprint}"

    def to_json(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            # The same identity `--ledger`'s `new_since_last_digest` diffs
            # against. Without it here, a machine consumer of `--ledger --json`
            # can count how many opaque keys are new but has no way to map any
            # one of them back to the alert or warning it identifies -- the
            # very thing the ledger output exists to name.
            "key": self.key,
            "source": self.source,
            "timestamp_utc": self.timestamp_utc.isoformat().replace("+00:00", "Z"),
            "et_date": self.et_date.isoformat(),
            "subject": self.subject,
            "slot": self.slot,
            "gate": self.gate,
            "verdict": self.verdict,
            "warnings": [{"code": c, "detail": d} for c, d in self.warnings],
        }


# --- Parsing helpers ---------------------------------------------------------


def parse_timestamp(value: str) -> datetime:
    """Parse an RFC3339 timestamp into an aware UTC datetime.

    Gmail hands back a trailing ``Z`` that ``fromisoformat`` did not accept
    before 3.11; the explicit swap keeps this working regardless of which
    supported interpreter runs it. A value with no offset is *assumed* UTC
    rather than local, because guessing the runner's timezone would silently
    shift every record by however many hours the scheduler host happens to be
    from Greenwich.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # A date-only value ("2026-08-26") is valid ISO 8601, and `fromisoformat`
    # accepts it as midnight rather than raising -- but the documented contract
    # is RFC3339 date-TIME, and a Gmail item supplying a bare date is malformed
    # the same way one with an unparseable date is. Silently treating it as
    # midnight UTC files the alert under the wrong ET day once the UTC-to-ET
    # conversion runs (midnight UTC is the previous evening in ET), rather than
    # counting it as `skipped_no_timestamp` like every other unusable value.
    if "T" not in text and " " not in text:
        raise ValueError(f"not an RFC3339 date-time (missing a time component): {value!r}")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# A task name cited as naming ANOTHER run's identity, not this record's own.
# Gate 4's own alert wording marks this explicitly -- "conflict/collision/
# clash/overlap with <task>" always introduces the OTHER task's name that
# collided with this run's own window, never a self-identification. "overlap"
# is this repo's own vocabulary for exactly this condition -- the
# GATE4_EXPECTED_OVERLAP reason code names it directly -- and was missing
# from the cue, so "Expected overlap with 1030-dca-recurring-buy" in a 1530
# ET alert's body was read as self-identifying the OTHER task's slot instead
# of being screened as a reference to it (Codex review: "Treat 'overlap
# with' task names as references"). "collided" (the past-tense VERB, as
# ordinary as the noun "collision" already here) was still missing, so
# "This run collided with 1030-dca-recurring-buy" had the same gap (Codex
# review: "Treat 'collided with' task names as references").
#
# "compared ... against"/"checked ... against" is a second, distinct shape:
# Gate 4 alerts routinely name the OTHER task they cross-checked orders
# against ("Compared orders against 1030-dca-recurring-buy"), and that
# named task is exactly as much a reference to another run's identity as a
# "conflict with" mention is -- but neither verb sat directly against
# "with" the way the collision words above do, so it went unscreened
# entirely and outranked the record's own explicit "1530 ET" declaration,
# which is checked later (Codex review: "Prefer explicit slots over
# comparative task mentions"). Bounded to 40 characters between the verb
# and "against", not unbounded, matching this file's other short bounded
# reaches (e.g. _NEGATION_REACH) -- long enough for an intervening object
# ("orders", "the fill price") without risking a reach across an entirely
# unrelated later "against" elsewhere in the clause.
# "validated ... against" is a third verb in the same "checked/compared
# against" shape: Gate 4 alerts routinely validate this run's orders
# against another task's ("Validated orders against
# 1030-dca-recurring-buy"), and that named task is exactly as much a
# reference as the "compared"/"checked" forms above -- but "validat(?:ed
# |ing|es)?" was missing from the verb alternation, so the mention went
# unscreened and outranked the record's own explicit "1530 ET" subject
# declaration (Codex review: "Prefer explicit subject slots over task
# references").
_TASK_NAME_REFERENCE_CUE = re.compile(
    r"\b(?:conflict(?:s|ing)?|collision|collided|clash(?:es|ing)?|overlap(?:s|ping)?)\s+with\b"
    r"|\b(?:compar(?:ed|ing|es)?|check(?:ed|ing|s)?|validat(?:ed|ing|es)?)\b.{0,40}?\bagainst\b",
    re.IGNORECASE,
)

# A self-referential mention of THIS record's own execution, as opposed to
# some other party the task named before it collided with. Required after
# a task-first collision cue (see _task_name_is_referenced's AFTER check)
# before that cue can disqualify the task name as a reference to another
# run: "1030-dca-recurring-buy collided with THIS RUN" names 1030 as the
# OTHER party in a collision with the current execution, but "Current task
# 1030-dca-recurring-buy collided with a legacy order" describes 1030's OWN
# mishap -- "a legacy order" is not this run, so the collision cue there
# says nothing about whose identity 1030 is (Codex review: "Distinguish
# self-identifying task subjects from references").
_CURRENT_RUN_REFERENCE = re.compile(
    r"\bthis run\b|\bthe current run\b|\bthis (?:execution|firing|slot|attempt)\b", re.IGNORECASE
)


def _task_name_is_referenced(text: str, position: int, length: int = 0) -> bool:
    """Whether the task-name mention at ``position`` cites ANOTHER run's own
    identity ("Conflict with 1030-dca-recurring-buy"), not this record's.

    Bounded to the surrounding SENTENCE first (``_SENTENCE_SPLIT``), THEN to
    the finer prior-event clause within it (``_PRIOR_EVENT_CLAUSE_SPLIT``) --
    the same two-tier pattern ``_mention_has_prior_event`` uses, for the same
    reason: this operates on the whole unsplit text, and without the outer
    sentence bound the clause search alone reaches across a period into an
    EARLIER, unrelated sentence. "Conflict with a legacy order. Current task
    1030-dca-recurring-buy aborted" has no comma/semicolon/but/however/
    while/whereas/yet between the two sentences, so the two read as one
    clause and the first sentence's "Conflict with" wrongly disclaimed the
    SECOND sentence's own self-identifying task name (Codex review: "Bound
    task-reference cues to the current sentence"). Within one sentence, still
    checked at clause granularity: "Gate 4 conflict with a prior run; Slot:
    1130-mornig-buying-run" must not disclaim the second clause's own
    self-identification either.

    Checked both BEFORE and AFTER the mention, not just before: the cue
    normally introduces the other task's name ("conflict with <task>"), but
    the task name can just as naturally be the clause's own grammatical
    SUBJECT, with the cue verb following it -- "1030-dca-recurring-buy
    collided with this run" names 1030 as the run that collided with the
    CURRENT (differently-slotted) one, and a before-only search missed the
    cue entirely, attributing the record to 1030 before ever reaching the
    subject's own explicit slot declaration (Codex review: "Check reference
    cues after task names").

    The AFTER form additionally requires the cue's own OBJECT to identify
    THIS run (``_CURRENT_RUN_REFERENCE``), unlike the BEFORE form, which
    unconditionally disqualifies: "Current task 1030-dca-recurring-buy
    collided with a legacy order" explicitly labels 1030 as THIS record's
    own task, and "a legacy order" is neither another scheduled run nor a
    self-reference -- the collision cue here says nothing about whose
    identity 1030 is, and disqualifying it anyway dropped a genuine
    self-identifying task name with no other slot evidence to fall back on
    (Codex review: "Distinguish self-identifying task subjects from
    references"). The BEFORE form has no such ambiguity: "conflict with
    <task>" always introduces the OTHER party by construction, whatever
    that party turns out to be.
    """
    sentence_start, sentence_end = _span_containing(text, position, _SENTENCE_SPLIT)
    local_text = text[sentence_start:sentence_end]
    local_position = position - sentence_start
    clause_start, clause_end = _span_containing(local_text, local_position, _PRIOR_EVENT_CLAUSE_SPLIT)
    before = local_text[clause_start:local_position]
    if _TASK_NAME_REFERENCE_CUE.search(before):
        return True
    after = local_text[local_position + length : clause_end]
    cue_match = _TASK_NAME_REFERENCE_CUE.search(after)
    if cue_match is None:
        return False
    return bool(_CURRENT_RUN_REFERENCE.search(after[cue_match.end() :]))


def detect_slot(text: str) -> Optional[str]:
    """Best-effort slot identification from an alert's subject + body."""
    return detect_slot_evidence(text)[0]


def detect_slot_evidence(text: str) -> Tuple[Optional[str], str]:
    """The slot, and WHICH signal identified it.

    The caller needs the second half to decide whether a slot alone is evidence
    that this is an executor alert at all. The four signals are not equally
    telling: a scheduler task name is unmistakable, a bare wall-clock time is
    ordinary English. Returns ``(None, "")`` when nothing matched.
    """
    lowered = text.lower()

    # 1. The scheduler's own task name, first. It is the only token in an alert
    #    that *declares* which slot wrote it; every other signal can appear
    #    incidentally. An alert from 330-final-buying-run routinely quotes another
    #    slot's fill ("1230 ET", "12:30 PM"), and a scheduler-fault alert leads
    #    with the wrong hour it actually fired at -- both shapes are a documented
    #    part of this corpus, and both would otherwise be filed under the slot
    #    they are complaining about rather than the slot complaining.
    #
    #    Every name present is tried, not just the first one in table order: an
    #    alert whose subject names its own task can still mention a DIFFERENT
    #    slot's task name in its body ("330-final-buying-run ABORT\nConflict
    #    with 1030-dca-recurring-buy"), and table order has nothing to do with
    #    where each name actually appears in the text -- the leftmost
    #    occurrence is the one labelling this record.
    #    Screened for a PRIOR-EVENT mention the same way the declared-clock
    #    branches below are: "Previous run 1030-dca-recurring-buy failed;
    #    this run's Slot: 1130" has the only task-name occurrence naming a
    #    HISTORICAL run, and an unscreened leftmost-match would attribute
    #    the record to that task before the current run's own labelled slot
    #    is ever reached. Every candidate is collected first (leftmost per
    #    name), then tried in position order so a screened-out match falls
    #    through to the next leftmost candidate rather than to step 2
    #    outright, preserving "every name present is tried" above.
    #    Every OCCURRENCE of every name is collected, not just the first: "
    #    Previous run 1030-dca-recurring-buy failed; this run
    #    1030-dca-recurring-buy failed again" has the same name appear twice,
    #    the first on a historical mention and the second on the current
    #    run -- stopping at the first (screened-out) occurrence left the
    #    record with no slot at all instead of reaching the second.
    #    Also screened for being merely REFERENCED, not self-identifying:
    #    "1530 ET | 2026-08-26 | Gate 4\nConflict with 1030-dca-recurring-buy"
    #    has its only task-name occurrence naming the OTHER task this run's
    #    window collided with, per Gate 4's own alert wording -- an
    #    unscreened match would attribute the record to that other task
    #    before the subject's own explicit "1530 ET" declaration (step 3
    #    below) is ever reached.
    candidates: List[Tuple[int, int, str]] = []
    for name, slot in _TASK_NAME_TO_SLOT:
        position = lowered.find(name)
        while position >= 0:
            candidates.append((position, len(name), slot))
            position = lowered.find(name, position + 1)
    for position, length, slot in sorted(candidates):
        if not _mention_has_prior_event(text, position, 0) and not _task_name_is_referenced(text, position, length):
            return slot, "task_name"

    # 2. A clock DECLARED as the slot, checked before the plain "1430ET"-style
    #    label below: "Scheduler fired at 1030 ET instead of scheduled 11:30
    #    AM" would otherwise match the generic explicit-ET pattern on the
    #    wrong-hour firing time before ever reaching the declaration of the
    #    actual scheduled slot. English declares it four ways -- suffixed
    #    ("11:30 AM slot") or prefixed, either as an AM/PM clock ("scheduled
    #    11:30 AM", "expected 11:30 AM"), as the equally canonical four-digit
    #    ET form ("scheduled 1130 ET", "expected 1130 ET"), or as a bare
    #    labelled field with no clock at all ("Slot: 1030") -- Gate 9's own
    #    documented phrasing for stating the slot. All four are recognized, in
    #    the order they appear in the text: "instead of scheduled 11:30 AM" is
    #    the AM/PM prefix form with no trailing "slot" at all.
    for clock in re.finditer(
        r"\bslot\s*:?\s*-?\s*(?P<labelled>935|0935|1030|1130|1230|1330|1430|1530)\b"
        r"|\b(?:scheduled|expected)\s+(?P<het>935|0935|1030|1130|1230|1330|1430|1530)\s*ET\b"
        r"|\b(?:scheduled|expected)\s+(?P<h1>\d{1,2}):(?P<m1>\d{2})\s*(?P<mer1>AM|PM)\b"
        r"|\b(?P<h2>\d{1,2}):(?P<m2>\d{2})\s*(?P<mer2>AM|PM)\s+slot\b",
        text,
        re.IGNORECASE,
    ):
        if clock["labelled"] is not None:
            # Screened the same way a labelled gate value is: "Previous run's
            # slot: 1030; this run's Slot: 1130" has the first occurrence on
            # a HISTORICAL mention, and an unscreened return would attribute
            # the record to the wrong run's slot. _PRIOR_EVENT_CLAUSE_SPLIT
            # (which also splits on ";") is used for both checks, matching
            # the labelled-gate fix's reasoning: this scan operates on the
            # whole unsplit text, so a semicolon must bound negation's reach
            # too, not just prior-event's.
            start, length = clock.start("labelled"), len(clock["labelled"])
            if not _mention_has_prior_event(text, start, length) and not _mention_is_negated(
                text, start, length, splitter=_PRIOR_EVENT_CLAUSE_SPLIT
            ):
                # "935" is the 9:35 slot's natural unpadded spelling -- Gate 9
                # never forces a leading zero -- and is zero-padded here the
                # same way _slot_from_filename_token() already pads a bare
                # filename token: no other roster slot is three digits, so
                # this cannot misfire on one of them (Codex review:
                # "Normalize unpadded 09:35 slot declarations").
                declared = clock["labelled"]
                return (f"0{declared}" if len(declared) == 3 else declared), "labelled"
            continue
        if clock["het"] is not None:
            # Screened for a PRIOR-EVENT mention the same way the labelled
            # branch just above is: "Previous run was scheduled 1030 ET;
            # this run's Slot: 1130" has this declared clock on a HISTORICAL
            # mention, and an unscreened return would attribute the record
            # to the wrong run's slot before the current run's own labelled
            # slot is ever reached.
            #
            # NOT also screened for negation, unlike the labelled branch:
            # this corpus's single most common phrasing for a wrong-hour
            # firing is "fired at 1030 ET instead of expected 1130 ET",
            # where "instead of" sits directly before the declared (and
            # correct) slot -- the same cue that legitimately disclaims a
            # reason code here would wrongly disclaim the very value it is
            # declaring.
            start, length = clock.start("het"), len(clock["het"])
            if not _mention_has_prior_event(text, start, length):
                declared = clock["het"]
                return (f"0{declared}" if len(declared) == 3 else declared), "declared_clock"
            continue
        hour, minute, meridiem = (
            (clock["h1"], clock["m1"], clock["mer1"])
            if clock["h1"] is not None
            else (clock["h2"], clock["m2"], clock["mer2"])
        )
        key = (f"{int(hour)}:{minute}", meridiem.upper())
        slot = _CLOCK_TO_SLOT.get(key)
        if slot:
            # Screened for a PRIOR-EVENT mention, same reasoning as the "het"
            # branch above: "Previous run was scheduled 10:30 AM; this run's
            # Slot: 1130" must not return on the historical AM/PM declaration
            # before the current run's own labelled slot is ever reached.
            # NOT also screened for negation, for the same reason the "het"
            # branch isn't: "Scheduler fired at 1030 ET instead of scheduled
            # 11:30 AM" (an existing, already-tested case) has "instead of"
            # sitting directly before this declared, correct slot.
            position = clock.start("h1") if clock["h1"] is not None else clock.start("h2")
            if not _mention_has_prior_event(text, position, 0):
                return slot, "declared_clock"

    # 3. An explicit "1430ET"/"1430 ET" label, which is a declaration too but a
    #    weaker one: it is also how bodies cite the *other* slots they mention.
    #    Every occurrence is tried, not just the first, screened for a
    #    PRIOR-EVENT mention the same way the declared-clock branches above
    #    are: "Previous run at 1030 ET. This run failed at 1130 ET" has the
    #    unconditional first match land on the historical 1030 ET mention,
    #    filing the record's coverage, streaks, and cross-source identity
    #    under the wrong slot before the current run's own 1130 ET is ever
    #    reached (Codex review: "Skip historical explicit-ET slot mentions").
    #    NOT also screened for negation, matching the declared-clock branches'
    #    reasoning: "instead of" phrasing sits directly before the correct,
    #    current slot in this corpus, not before a disclaimed one.
    #    "935 ET" (unpadded) is accepted here too, zero-padded on return, the
    #    same normalization the labelled and declared-clock branches above
    #    already apply to the same token -- no other roster slot is three
    #    digits, so this cannot misfire on one of them (Codex review: "Accept
    #    the unpadded explicit '935 ET' label").
    #    A colon-form "H:MM ET"/"HH:MM ET" declaration is accepted too, via
    #    the same _COLON_ET_TO_SLOT roster lookup the declared-clock branch
    #    above doesn't need (that one already has AM/PM to disambiguate) --
    #    "9:35 ET | 2026-08-26 | Gate 5" and "13:30 ET | 2026-08-26 | Gate 5"
    #    are natural, unambiguous Gate 9 subjects that the compact-digit-only
    #    form never matched at all (Codex review: "Recognize colon-form ET
    #    slot declarations"). A colon time with no roster match (e.g. an
    #    off-schedule "9:07 PM ET") falls through to step 4 below rather than
    #    returning here.
    for explicit in re.finditer(
        r"\b(?P<compact>935|0935|1030|1130|1230|1330|1430|1530)\s*ET\b"
        r"|\b(?P<hh>\d{1,2}):(?P<mm>\d{2})\s*ET\b",
        text,
        re.IGNORECASE,
    ):
        if explicit["compact"] is not None:
            if not _mention_has_prior_event(text, explicit.start("compact"), 0):
                declared = explicit["compact"]
                return (f"0{declared}" if len(declared) == 3 else declared), "explicit_et"
            continue
        slot = _COLON_ET_TO_SLOT.get(f"{int(explicit['hh'])}:{explicit['mm']}")
        if slot and not _mention_has_prior_event(text, explicit.start("hh"), 0):
            return slot, "explicit_et"

    # 4. A wall-clock time. Anchored on AM/PM so a dollar amount or an order-count
    #    like "1:30" inside a duration cannot be mistaken for the 1:30 PM slot,
    #    and matched on the meridiem too so "2:30 AM" is not read as the 2:30 PM
    #    slot. Every candidate is tried, not just the first: an off-schedule alert
    #    leads with the wrong-hour firing time before naming its own slot. (The
    #    DECLARED form of a clock -- "scheduled 11:30 AM" / "the 11:30 AM slot"
    #    -- is already handled in step 2, ahead of this purely positional scan.)
    # Screened for a PRIOR-EVENT mention the same way steps 2 and 3 above are:
    # a subject-less, partially-parsed alert (strong gate evidence only) that
    # narrates a prior firing's clock before its own -- "Previous run fired
    # at 10:30 AM. This run failed at 11:30 AM" -- had the unconditional
    # first match land on the historical 10:30 AM mention, corrupting
    # coverage and streaks for both slots (Codex review: "Screen historical
    # fallback wall-clock mentions").
    for clock in re.finditer(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\b", text, re.IGNORECASE):
        # The hour is normalized, not looked up verbatim: "09:35 AM" is the
        # conventional zero-padded spelling and the keys here are unpadded, so a
        # verbatim lookup dropped the 9:35 executor's own self-identification and
        # filed its alert as unclassified.
        key = (f"{int(clock.group(1))}:{clock.group(2)}", clock.group(3).upper())
        slot = _CLOCK_TO_SLOT.get(key)
        if slot and not _mention_has_prior_event(text, clock.start(), 0):
            return slot, "clock"

    # 5. Stream words, weakest and last. Screened for a PRIOR-EVENT mention
    #    the same way every branch above it now is: a partially-parsed alert
    #    with strong Gate evidence but no OTHER slot signal can still have
    #    its only stream-word occurrence be purely historical ("Previous
    #    midday run failed; this run cannot reach the repo"), and an
    #    unscreened match there attributed the CURRENT incident to the
    #    historical slot (Codex review: "Screen historical stream words
    #    before assigning a slot").
    #
    #    Candidates are collected across the WHOLE table first, then tried
    #    in TEXTUAL POSITION order -- not table order, which the loop used
    #    to try verbatim. "Failing gate: Gate 2. Midday run failed because
    #    market-open data was unavailable" names its OWN stream ("midday")
    #    before merely REFERENCING another one ("market-open data"), but
    #    _STREAM_WORD_TO_SLOT lists "market-open" first, so the old
    #    table-order loop returned 0935 -- the referenced stream, not the
    #    incident's own -- before ever reaching "midday" (Codex review:
    #    "Prefer the current run over referenced stream words"). The same
    #    leftmost-candidate-wins fix step 1's task-name loop already uses.
    stream_word_candidates: List[Tuple[int, str, str]] = []
    for word, slot in _STREAM_WORD_TO_SLOT:
        position = lowered.find(word)
        while position >= 0:
            stream_word_candidates.append((position, word, slot))
            position = lowered.find(word, position + 1)
    for position, word, slot in sorted(stream_word_candidates):
        if not _mention_has_prior_event(text, position, len(word)):
            return slot, "stream_word"

    return None, ""


# How far back from a reason-code mention a negation cue can sit and still be
# read as disclaiming it. Long enough for "an earlier gate rather than", short
# enough that an unrelated "not" elsewhere in the same clause cannot reach it.
_NEGATION_REACH = 40

# How far AHEAD a negation cue can sit and still be read as disclaiming the
# mention, rather than something further into the sentence entirely. Shorter
# than the before-side reach on purpose: "GATE5_DISABLED was not the verdict"
# puts its "not" about 5 characters after the code, but "GATE5_DISABLED
# because the entry point is not available" -- the standard, compliant phrasing
# of the condition itself -- has one roughly 30 characters after it, negating
# "available" in the trailing explanation, not the code. 20 catches a short
# copular disclaimer ("was/is/wasn't not ...") immediately after the mention
# without reaching into an explanation clause that follows it.
_NEGATION_REACH_AFTER = 20


def _span_containing(text: str, position: int, splitter: "re.Pattern[str]") -> Tuple[int, int]:
    """The ``(start, end)`` bounds of the ``splitter``-delimited span of ``text`` containing ``position``."""
    start = 0
    for match in splitter.finditer(text):
        if match.end() <= position:
            start = match.end()
        else:
            return start, match.start()
    return start, len(text)


def _clause_containing(text: str, position: int, splitter: "re.Pattern[str]") -> str:
    """The ``splitter``-delimited clause of ``text`` that contains ``position``."""
    start, end = _span_containing(text, position, splitter)
    return text[start:end]


def _clause_spans(text: str, splitter: "re.Pattern[str]") -> List[Tuple[int, int]]:
    """The ``(start, end)`` span of every ``splitter``-delimited clause in ``text``, in order."""
    spans = []
    start = 0
    for match in splitter.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return spans


def _mention_has_prior_event(sentence: str, position: int, length: int) -> bool:
    """Whether a prior-event cue in the mention's own CLAUSE marks it as history.

    Clause-scoped, not the whole sentence: "Unlike the previous run, this run
    is GATE5_DISABLED because no entry point is available" has "previous run"
    in an EARLIER clause, contrasting with the current run rather than
    describing it, and a sentence-wide search read that contrast as the code's
    own history. Uses ``_PRIOR_EVENT_CLAUSE_SPLIT``, not ``_CLAUSE_SPLIT`` --
    see that constant's comment for why "and" must NOT be a boundary here.

    Bounded to the surrounding SENTENCE first (``_SENTENCE_SPLIT``: period,
    newline, or semicolon), THEN to the finer prior-event clause within it.
    Several callers -- ``detect_slot_evidence``'s declared-slot branches,
    ``_reason_signature`` -- scan a whole, multi-sentence subject/body at
    once rather than one pre-split sentence at a time, and
    ``_PRIOR_EVENT_CLAUSE_SPLIT`` does not itself split on periods or
    newlines (only on ";", for exactly those callers -- see that constant's
    comment). Without this outer bound, "Previous run at 1030 ET. This run
    failed at 1130 ET" has no comma/semicolon/but/however/while/whereas/yet
    anywhere in it, so the two sentences read as ONE clause and the historical
    "Previous run" cue reaches all the way across the period to screen out the
    CURRENT run's own 1130 ET mention too (Codex reviews: "Skip historical
    explicit-ET slot mentions", "Bound historical-reason screening at
    sentence breaks"). A no-op for a caller that already passes one
    pre-split sentence, since it contains no period/newline/semicolon of its
    own for this to find.
    """
    sentence_start, sentence_end = _span_containing(sentence, position, _SENTENCE_SPLIT)
    local_text = sentence[sentence_start:sentence_end]
    clause = _clause_containing(local_text, position - sentence_start, _PRIOR_EVENT_CLAUSE_SPLIT)
    return bool(_PRIOR_EVENT.search(clause))


def _first_unscreened_match(
    pattern: str,
    text: str,
    *,
    screen_prior_event: bool,
    negation_splitter: "re.Pattern[str]" = _CLAUSE_SPLIT,
) -> Optional["re.Match[str]"]:
    """The first ``pattern`` match in ``text`` that survives negation (and,
    optionally, prior-event) screening.

    ``re.search`` alone only ever inspects the FIRST occurrence: "This was not
    GATE5_DISABLED, but this run is GATE5_DISABLED" or "Run ABORTED" with body
    "The order was not rejected, but the retry was rejected by the broker"
    both carry a disclaimed mention before the affirmative one, and stopping
    at the first left the whole text unclassified (or handed to a weaker
    fallback) instead of reaching the real one. Every match is tried in order
    until one passes screening.

    ``negation_splitter`` defaults to ``_CLAUSE_SPLIT``, correct for a caller
    that receives one pre-split ``_SENTENCE_SPLIT`` sentence at a time (no
    semicolon can occur in it). A caller checking an UNSPLIT body/subject
    directly -- to protect a fractional match like "Gate 6.5" from being cut
    by a sentence splitter -- should pass ``_PRIOR_EVENT_CLAUSE_SPLIT``
    instead, which also splits on ";": without it, a "not" disclaiming an
    unrelated LATER mention in the same semicolon-joined alert ("Failing
    gate: Gate 2; this is not GATE5_DISABLED") sits close enough to wrongly
    reach back and disclaim this one too.
    """
    for match in re.finditer(pattern, text, re.IGNORECASE):
        start, length = match.start(), match.end() - match.start()
        if screen_prior_event and _mention_has_prior_event(text, start, length):
            continue
        if _mention_is_negated(text, start, length, splitter=negation_splitter):
            continue
        return match
    return None


def _mention_is_negated(
    sentence: str, position: int, length: int = 0, *, splitter: "re.Pattern[str]" = _CLAUSE_SPLIT
) -> bool:
    """Whether a negation cue near ``position`` disclaims the mention there.

    Checked on BOTH sides. Most disclaimers precede the mention ("this is not
    GATE5_DISABLED", "an earlier gate rather than GATE5_DISABLED"), but English
    just as readily places the negation immediately after it ("Failing gate:
    Gate 2; GATE5_DISABLED was not the verdict") -- a run-up-only check reads
    that as an affirmative mention. Each side is bounded to its own reach so an
    unrelated negation elsewhere in a long sentence cannot reach in -- in
    particular so an EXPLANATION following the mention ("... because the entry
    point is not available") is not misread as disclaiming it.

    Both reaches are ALSO clamped to the mention's own clause. "unlike" is one
    of the disclaimer cues below (for "unlike GATE5_DISABLED, ..."), but it
    reads just as naturally opening an unrelated contrast: "Unlike the
    previous run, this run is GATE5_DISABLED ..." puts "unlike" comfortably
    inside the character reach while modifying an entirely different clause
    across the comma. Bounding the reach to the clause first stops a cue that
    belongs to one clause from disclaiming a mention in another.

    ``splitter`` defaults to ``_CLAUSE_SPLIT`` for every caller that already
    receives one pre-split ``_SENTENCE_SPLIT`` sentence at a time (so a
    semicolon can never occur in ``sentence`` anyway). A caller checking an
    UNSPLIT body/subject directly should pass ``_PRIOR_EVENT_CLAUSE_SPLIT``
    (which also splits on ";") instead, or a semicolon-joined mention from a
    different clause -- "Failing gate: Gate 2; this is not GATE5_DISABLED" --
    would wrongly disclaim this one.
    """
    clause_start = 0
    clause_end = len(sentence)
    for match in splitter.finditer(sentence):
        if match.end() <= position:
            clause_start = match.end()
        elif match.start() >= position + length:
            clause_end = match.start()
            break
    before = sentence[clause_start:position][-_NEGATION_REACH:]
    after = sentence[position + length : clause_end][:_NEGATION_REACH_AFTER]
    # _REASON_NEGATION_FORWARD_ONLY is checked in `before` only -- see its
    # definition for why checking it in `after` disclaims the wrong side.
    # _REASON_NEGATION_ADJACENT_NO likewise: "no" only ever negates what
    # immediately follows it, and its own `$`-anchored pattern already
    # restricts it to firing on the immediate run-up, not the full reach --
    # see its definition for why an unanchored "no" is excluded instead.
    return bool(
        _REASON_NEGATION_BIDIRECTIONAL.search(before)
        or _REASON_NEGATION_FORWARD_ONLY.search(before)
        or _REASON_NEGATION_ADJACENT_NO.search(before)
        or _REASON_NEGATION_BIDIRECTIONAL.search(after)
    )


def detect_gate_evidence(subject: str, body: str) -> Tuple[Optional[str], str]:
    """Identify the gate an alert blames, and WHICH signal named it.

    The caller needs the second half for the same reason
    :func:`detect_slot_evidence` reports one: a bare "Gate N" mention anywhere
    in free text is ordinary English (a travel note saying "Meet at Gate 5
    before boarding" parses the same way an executor alert does), so
    :func:`looks_like_executor_alert` cannot treat it as proof on its own the
    way it can a reason code or the labelled "Failing gate:" line.

    Order matters for the VALUE too. An alert body routinely *narrates* several
    gates ("Gates 0-4 passed ... stopped before Gate 5"), so scanning for the
    first "Gate N" anywhere would blame Gate 0 for nearly everything. The
    explicit "Failing gate:" line the prompt asks for is authoritative when
    present; the subject line is the next best summary; free-text body scanning
    is the last resort.
    """
    # Named reason codes come FIRST, ahead of even the explicit "Failing gate:"
    # line. A compliant GATE5_DISABLED alert carries both -- Gate 9 requires the
    # labelled line, and the verdict is still GATE5_DISABLED -- so checking the
    # label first would file every one of these expected conditions as an
    # ordinary Gate 5 failure, which is exactly the conflation
    # config/notifications.json separates them to avoid.
    #
    # The mention must be affirmative, though: an alert can legitimately say
    # "Failing gate: Gate 2; this is not GATE5_DISABLED" to distinguish an
    # earlier-gate abort from the Gate 5 fallback, and an unscoped substring
    # match would invert exactly that clarification. English places that
    # disclaimer on either side of the code -- "this is not GATE5_DISABLED" but
    # also "GATE5_DISABLED was not the verdict" -- so both sides are checked.
    #
    # A mention describing an EARLIER run ("the previous run was
    # GATE5_DISABLED; this run cannot reach the repo") is excluded the same way
    # a prior-event verdict is: it is history, not a classification of this run.
    #
    # Matched case-INSENSITIVELY, like every gate and verdict pattern here. The
    # codes are a convention, not a guaranteed spelling: a natural-language alert
    # writes "verdict gate5_disabled" as readily as the shouted form, and a
    # case-sensitive test would fold exactly those back into an ordinary Gate 5.
    # The canonical uppercase spelling is what gets returned either way, so the
    # counts, streaks and merge identity stay on one label.
    sentences = _SENTENCE_SPLIT.split(f"{subject}\n{body}")
    for code in _REASON_CODES:
        lowered_code = code.lower()
        # An IDENTIFIER boundary, not `str.find`'s bare substring search: the
        # code's own spelling is full of underscores, which are WORD
        # characters, so a plain substring match let "NOT_GATE5_DISABLED"
        # count as an occurrence of "GATE5_DISABLED" -- and for the same
        # reason, `\bnot\b`'s negation screening below never caught it
        # either, since there is no word/non-word transition between "T"
        # and "_" for `\b` to anchor on. Excluding an identifier character
        # (letter, digit, or underscore) immediately before or after the
        # match closes both gaps at once (Codex review: "Require token
        # boundaries for named reason codes").
        code_pattern = _REASON_CODE_PATTERNS[code]
        for sentence in sentences:
            lowered = sentence.lower()
            # Every occurrence in the sentence is tried, not just the first:
            # "This was not GATE5_DISABLED, but this run is GATE5_DISABLED"
            # has a disclaimed mention before the affirmative one, and
            # stopping at the first would reject the whole sentence on the
            # disclaimed copy without ever reaching the real one.
            for match in code_pattern.finditer(lowered):
                if (
                    not _mention_is_negated(sentence, match.start(), len(lowered_code))
                    and not _mention_has_prior_event(sentence, match.start(), len(lowered_code))
                    and not _REASON_CODE_INFORMATIONAL_MENTION.search(sentence[match.end() :])
                ):
                    return code, "reason_code"

    for haystack in (body, subject):
        # The second "gate" is optional: "Failing gate: 5" and "Failing gate:
        # 6.5" are as natural a way to fill in Gate 9's required label as
        # "Failing gate: Gate 5" -- the label word already said "gate" once.
        #
        # Prior-event AND negation screened, the same way reason codes and the
        # free-text fallback are: "Previous run's failing gate: Gate 5, but
        # this run failed at Gate 2" has the label on a HISTORICAL mention,
        # and "Failing gate: Gate 5 was not the verdict; failing gate: Gate 2"
        # explicitly disclaims the first labelled value -- an unscreened
        # match would take either wrong answer. Uses _PRIOR_EVENT_CLAUSE_SPLIT
        # (which also splits on ";"), not the default _CLAUSE_SPLIT, because
        # this label is checked on the whole unsplit body/subject to protect
        # a fractional match like "Gate 6.5" from being cut by a sentence
        # splitter: without a semicolon boundary here too, "Failing gate:
        # Gate 2; this is not GATE5_DISABLED" would let the second clause's
        # negation wrongly reach back and disclaim the first clause's label.
        labelled = _first_unscreened_match(
            r"failing gate\s*:?\s*-?\s*(?:gate\s*)?(\d+(?:\.\d+)?)",
            haystack,
            screen_prior_event=True,
            negation_splitter=_PRIOR_EVENT_CLAUSE_SPLIT,
        )
        if labelled:
            return f"Gate {labelled.group(1)}", "labelled"

    # Free-text mentions get the same negation/prior-event screening as the
    # stronger signals above, not a bare search: "This did not fail at Gate
    # 5; repository access was unavailable" disclaims Gate 5 explicitly, and
    # an unscoped match would attribute the alert to the very gate it just
    # ruled out. NOT split by sentence first -- _SENTENCE_SPLIT treats "."
    # as a boundary, which would cut a fractional gate token like "Gate 6.5"
    # in half. Also uses _PRIOR_EVENT_CLAUSE_SPLIT for negation, same as the
    # labelled match just above: "This run failed at Gate 2; this is not
    # GATE5_DISABLED" correctly rejects the reason code, but the default
    # negation splitter has no semicolon boundary, so that same "not" was
    # reaching back across it and disclaiming "Gate 2" too.
    subject_gate = _first_unscreened_match(
        _GATE_TOKEN, subject, screen_prior_event=True, negation_splitter=_PRIOR_EVENT_CLAUSE_SPLIT
    )
    if subject_gate:
        return f"Gate {subject_gate.group(1)}", "free_text"

    body_gate = _first_unscreened_match(
        _GATE_TOKEN, body, screen_prior_event=True, negation_splitter=_PRIOR_EVENT_CLAUSE_SPLIT
    )
    if body_gate:
        return f"Gate {body_gate.group(1)}", "free_text"

    return None, ""


def detect_gate(subject: str, body: str) -> Optional[str]:
    """Identify the gate an alert blames. See :func:`detect_gate_evidence`."""
    return detect_gate_evidence(subject, body)[0]


def detect_verdict(subject: str, body: str) -> Optional[str]:
    """Classify the outcome, preferring the most specific reading.

    Two rules pull against each other here. Executors put the verdict in the
    subject, so the subject normally wins -- but the generic "aborted" wording
    co-occurs with the specific outcome constantly, and the usual shape is a
    plain subject over a detailed body: "Run ABORTED" / "cap breach at Gate 6".
    Letting the subject win outright there files every one of those under ABORT
    and leaves CAP_BREACH, REJECTED and PARTIAL counting almost nothing, which
    is the whole reason they are separate categories.

    So specificity is resolved first, across BOTH fields, and only then does the
    generic label apply -- subject before body, as before.

    The body's specific verdict is ignored where the sentence carrying it
    narrates a *prior* event ("one order was rejected earlier"). That is the one
    case where the subject genuinely is the better summary: the body is
    recounting history, not classifying this run.

    A specific-pattern match is also ignored where its own clause NEGATES it --
    "the order was not rejected", "not a cap breach", "not stopped mid-run" --
    the same disclaimer shape :func:`detect_gate_evidence` excludes for reason
    codes. Left unchecked, the generic ABORT subject would lose to a specific
    pattern matching text that is explicitly ruling that outcome OUT.
    """
    # Named reason codes are canonical VERDICT names too, not just gate
    # categories: a compliant "Verdict: GATE5_DISABLED" alert states the
    # code as its own outcome. Without this check, the code registered only
    # in detect_gate_evidence()'s own reason-code check, leaving this
    # function to fall through every verdict pattern, return None, and file
    # the alert under "BY VERDICT: unclassified" despite it being one of
    # the two explicitly named, expected outcomes this corpus tracks
    # (Codex review: "Classify named executor verdicts as verdicts").
    # Checked first, ahead of even the specific verdict patterns below,
    # mirroring detect_gate_evidence()'s own reason-code-first ordering and
    # its exact screening (token-boundary match so "NOT_GATE5_DISABLED"
    # cannot count as an occurrence, negation checked on either side of the
    # code, prior-event mentions excluded) so a mention already excluded
    # there for the gate is excluded here for the verdict too.
    sentences = _SENTENCE_SPLIT.split(f"{subject}\n{body}")
    for code in _REASON_CODES:
        lowered_code = code.lower()
        code_pattern = _REASON_CODE_PATTERNS[code]
        for sentence in sentences:
            lowered = sentence.lower()
            for match in code_pattern.finditer(lowered):
                if (
                    not _mention_is_negated(sentence, match.start(), len(lowered_code))
                    and not _mention_has_prior_event(sentence, match.start(), len(lowered_code))
                    and not _REASON_CODE_INFORMATIONAL_MENTION.search(sentence[match.end() :])
                ):
                    return code

    # The subject-scoped calls in this function operate on the whole UNSPLIT
    # subject, so they pass _PRIOR_EVENT_CLAUSE_SPLIT (which also splits on
    # ";") as the negation splitter -- the default _CLAUSE_SPLIT has no
    # semicolon boundary, so "Run ABORTED; this was not rejected" correctly
    # disclaims REJECTED but then let that same "not" reach backward across
    # the semicolon and disclaim ABORTED too, once REJECTED's screened-out
    # match fell through to the generic pass below.
    for verdict, pattern in _SPECIFIC_VERDICTS:
        if _first_unscreened_match(
            pattern, subject, screen_prior_event=False, negation_splitter=_PRIOR_EVENT_CLAUSE_SPLIT
        ):
            return verdict

    for verdict, pattern in _SPECIFIC_VERDICTS:
        for sentence in _SENTENCE_SPLIT.split(body):
            if verdict == "REJECTED":
                # _REJECTED_ORDER_CONTEXT is bound to the rejection's own
                # CLAUSE, not the whole sentence: "No order was placed, but
                # email was rejected by the provider; this run aborted" has
                # "order" in an EARLIER, unrelated clause, and a
                # sentence-wide search let that mention satisfy the
                # requirement for a rejection that has nothing to do with
                # an order -- returning REJECTED over the sentence's own
                # explicit ABORT (Codex review: "Bind rejection context to
                # the rejected clause"). Every REJECTED match in the
                # sentence is tried, not just the first: "The email was
                # rejected, but the order was rejected too" has the first
                # match's own clause carrying no order context, and
                # stopping there would miss the second match's clause,
                # which does.
                found = None
                for candidate in re.finditer(pattern, sentence, re.IGNORECASE):
                    start, length = candidate.start(), candidate.end() - candidate.start()
                    if _mention_has_prior_event(sentence, start, length):
                        continue
                    if _mention_is_negated(sentence, start, length):
                        continue
                    if not _REJECTED_ORDER_CONTEXT.search(_clause_containing(sentence, start, _CLAUSE_SPLIT)):
                        continue
                    found = candidate
                    break
                if found:
                    return verdict
                continue
            if _first_unscreened_match(pattern, sentence, screen_prior_event=True):
                return verdict

    for verdict, pattern in _GENERIC_VERDICTS:
        if _first_unscreened_match(
            pattern, subject, screen_prior_event=False, negation_splitter=_PRIOR_EVENT_CLAUSE_SPLIT
        ):
            return verdict

    # Generic matches get the SAME contextual screening the specific pass gets
    # above, not a bare whole-field search: "Previous run aborted at Gate 2.
    # This run skipped at Gate 5" would otherwise return ABORT from the first
    # sentence's history rather than SKIPPED from the second sentence's
    # current-run classification, and "the run did not abort; it skipped"
    # would return the very outcome its own clause rules out.
    for verdict, pattern in _GENERIC_VERDICTS:
        for sentence in _SENTENCE_SPLIT.split(body):
            if _first_unscreened_match(pattern, sentence, screen_prior_event=True):
                return verdict

    # Last resort: any remaining, otherwise-unscreened verdict match, with no
    # generic wording anywhere. Negation still disqualifies a match here -- a
    # specific-sounding phrase ruled out in its own clause is never a correct
    # answer, fallback tier or not.
    #
    # Prior-event screening is ALSO still applied here, not disabled --
    # "Previous run had a cap breach. Current run stopped because repository
    # access was unavailable" has CAP_BREACH ONLY in a sentence that
    # explicitly narrates the PREVIOUS run's own history, and the current
    # incident (an unrecognized wording, "repository access was
    # unavailable") genuinely has no classifiable verdict at all. Reporting
    # the historical CAP_BREACH anyway used to be this tier's whole point
    # ("better to report it than leave it unclassified"), but that
    # unconditionally assigns a HISTORICAL outcome to the CURRENT incident --
    # actively wrong, not merely uninformative, and it corrupts verdict
    # totals and can break reconciliation with a counterpart that correctly
    # reports no verdict for the same incident (Codex review: "Keep
    # prior-only outcomes unclassified"). An unclassified verdict is already
    # a legitimate, tracked outcome elsewhere in this module; a fabricated
    # one from unrelated history is not an improvement over it.
    for haystack in (subject, body):
        for verdict, pattern in _VERDICT_PATTERNS:
            if _first_unscreened_match(
                pattern, haystack, screen_prior_event=True, negation_splitter=_PRIOR_EVENT_CLAUSE_SPLIT
            ):
                return verdict
    return None


def _exempt_search_text(sentence: str, match: "re.Match[str]", splitter: "re.Pattern[str]") -> str:
    """The text searched for exempt-route evidence for THIS specific match.

    Ordinarily the match's whole ``splitter``-delimited clause -- which,
    when ``splitter`` is ``_PRIOR_EVENT_CLAUSE_SPLIT``, deliberately does
    not split on "and" so a cause-and-effect claim ("Ran drift_detection.py
    --slack and it posted to Slack") keeps its evidence in reach.

    But "and" also joins two completely independent, PARALLEL claims that
    only share a stated subject: "Posted drift via DCA_DRIFT_SLACK_WEBHOOK
    and posted the Gate 5 alert to Slack" names two separate posts, and
    reaching across "and" let the first claim's permitted route wrongly
    exempt the second, unrelated one too (Codex review: "Keep permitted
    Slack routes from exempting a separate post"). Distinguished by whether
    the "and"-segment holding the match opens with its own explicit
    subject: a pronoun ("it"/"this"/"that"/"which") signals a CONTINUATION
    of the clause before it, worth the wider search; a bare verb phrase
    with no subject at all ("posted...") is a coordinate, independent
    claim, so the search narrows to just that segment.

    "because" is truncated out entirely, unlike "and": a causal clause
    AFTER the match explains WHY something happened, never WHAT permitted
    route the match's own claim was delivered through. "Posted the Gate 5
    alert to Slack because drift detection was unavailable" has "drift"
    only in that trailing causal clause, and letting the exempt search
    reach it read an unrelated explanation as identifying a supported
    route for the actual, unsupported post (Codex review: "Bind permitted
    Slack markers to the delivered claim"). Unlike "and", this has no
    continuation exception: a causal clause never restates or continues
    the claim before it, it explains a DIFFERENT fact.

    When the match itself is in the FIRST "and"-segment, the search is
    symmetrically bounded FORWARD too, at the next "and" after the match --
    unless the segment following that "and" opens with the same
    continuation-pronoun shape checked for the backward direction above.
    Without this, a match with no "and" BEFORE it always fell through to
    returning the WHOLE clause unbounded, so "Posted the Gate 5 alert to
    Slack and then encountered an unhandled provider error" let the
    trailing, unrelated "unhandled" exempt the earlier, unsupported Slack
    post (Codex review: "Stop later Slack markers from exempting an
    earlier post").
    """
    clause_start, clause_end = _span_containing(sentence, match.start(), splitter)
    clause = sentence[clause_start:clause_end]
    match_pos = match.start() - clause_start
    because_match = re.search(r"\bbecause\b", clause[match_pos:], re.IGNORECASE)
    if because_match:
        clause = clause[: match_pos + because_match.start()]
    segment_start = 0
    for and_match in re.finditer(r"\band\b", clause, re.IGNORECASE):
        if and_match.end() <= match_pos:
            segment_start = and_match.end()
        else:
            break
    if segment_start == 0:
        forward_and = re.search(r"\band\b", clause[match_pos:], re.IGNORECASE)
        if forward_and is None:
            return clause
        following = clause[match_pos + forward_and.end() :]
        if _CONTINUATION_SUBJECT.match(following):
            return clause
        return clause[: match_pos + forward_and.start()]
    segment = clause[segment_start:]
    if _CONTINUATION_SUBJECT.match(segment):
        return clause
    return segment


def _is_affirmative_claim(
    body: str,
    pattern: str,
    *,
    exempt: Optional["re.Pattern[str]"] = None,
    exempt_match: Optional[Callable[[str, "re.Match[str]"], bool]] = None,
) -> bool:
    """True when the body claims delivery on this channel, rather than denying it.

    Naming a channel is not the same as claiming it. An executor that writes
    "Slack is disabled, so no Slack alert was posted" is doing exactly what the
    config asks, and flagging it would make the warnings useless: a scheduled
    check would go red for correct behavior, and the operator would learn to
    ignore it. So a mention only counts when the sentence carrying it holds no
    negation cue.

    This is a heuristic on free text and will not be perfect. It is biased
    toward *under*-reporting -- a negated sentence suppresses the warning even
    if a genuine claim sits elsewhere in the same sentence -- because a false
    warning costs more here than a missed one: the missed alert still shows up
    in the gate and coverage counts, while a false one erodes the signal.

    The exemption, where present, is scoped to the match's own CLAUSE, not
    the whole sentence: "Posted the Gate 5 alert to Slack, and portfolio
    drift was unchanged" has the unsupported routine claim in one clause and
    an unrelated drift observation in another, joined only because they share
    a sentence. Deliberately NOT split on "and" the way ``_CLAUSE_SPLIT`` is
    for channel outcomes, though: "Ran drift_detection.py --slack and it
    posted to Slack" narrates one cause-and-effect action across an "and",
    and splitting there would strand the Slack mention without the "--slack"
    evidence that actually justifies it.

    ``exempt_match``, where present, is an alternative, PER-MATCH exemption
    check (given the sentence and the match) for a case a static ``exempt``
    regex cannot express -- e.g. whether this specific occurrence sits inside
    an absolute path provably outside this checkout. The two are independent;
    either exempting the match is enough to skip it.

    The negation check itself is scoped to each match's own CLAUSE, not the
    whole sentence: "Slack was not used, but the durable alert was written
    to artifacts/alerts/x.txt" denies one channel and affirms a completely
    different one in the same sentence; a sentence-wide negation search let
    the "not" clear BOTH claims, silently suppressing GITIGNORED_ALERT_PATH
    evidence for a path the alert plainly states (Codex review: "Scope
    negation to each channel claim"). Checking the clause instead of the
    whole sentence still correctly suppresses the same-clause case this
    function was built for -- "Slack is disabled in config, so no Slack
    alert was posted" negates both of its own "Slack" occurrences in their
    own clause -- while no longer reaching into an unrelated clause to
    suppress an unrelated claim.

    Uses ``_CLAUSE_SPLIT``, not ``_PRIOR_EVENT_CLAUSE_SPLIT`` like the
    ``exempt`` check just above -- deliberately, unlike that check, split on
    "and": "Slack was not used and the durable alert was written to
    artifacts/alerts/x.txt" joins the same two independent claims with "and"
    instead of "but", and ``_PRIOR_EVENT_CLAUSE_SPLIT`` was built to NOT
    split there (a prior-event mention sharing a subject across "and", or an
    exempting cause-and-effect claim like "ran drift_detection.py --slack
    and it posted to Slack") -- so it let the negation reach across "and"
    just as it did across nothing at all (Codex review: "Bound channel
    negation across additive conjunctions"). ``_CLAUSE_SPLIT`` is the
    splitter this file already uses for exactly this shape: "Slack could not
    send and email sent successfully" is its own worked example for why
    "and" must separate independent channel outcomes.
    """
    # Preserve the ordinary ``drift_detection.py --slack`` spelling as one
    # sentence. `_SENTENCE_SPLIT` deliberately treats every period as a
    # boundary for prose, but the module extension here is part of the command
    # whose flag authorizes the Slack route.
    split_body = re.sub(r"\bdrift_detection\.py\b", "drift_detection-py", body, flags=re.IGNORECASE)
    for sentence in _SENTENCE_SPLIT.split(split_body):
        # Every match in the sentence is tried, not just the first: "Posted
        # drift finding to Slack via DCA_DRIFT_SLACK_WEBHOOK, but also posted
        # the Gate 5 alert to Slack" has an exempt claim before an unexempt
        # one, and stopping at the first would let the exempt copy clear the
        # whole sentence without ever reaching the unsupported claim after it.
        for match in re.finditer(pattern, sentence, re.IGNORECASE):
            if exempt is not None:
                clause = _exempt_search_text(sentence, match, _PRIOR_EVENT_CLAUSE_SPLIT)
                if exempt.search(clause):
                    # A permitted route named in the same breath as the
                    # channel. The claim is true and supported, so it is not
                    # a contradiction to flag.
                    continue
            if exempt_match is not None and exempt_match(sentence, match):
                continue
            clause_start, clause_end = _span_containing(sentence, match.start(), _CLAUSE_SPLIT)
            negation_clause = sentence[clause_start:clause_end]
            # A common manner phrase ("with no errors") stripped before the
            # negation scan, not after: its "no" describes the ABSENCE of a
            # problem during the action, not a denial of the action itself
            # (Codex review: "Bind negation to the channel outcome").
            screened_clause = _NON_NEGATING_MANNER_PHRASE.sub("", negation_clause)
            if _NEGATION_CUES.search(screened_clause):
                continue
            # A forward-only preposition ("instead of", "rather than") only
            # negates what comes AFTER it, so checked in the text BEFORE the
            # match within its own clause, not the whole clause: "Used the
            # durable stream_root path instead of artifacts/alerts/x.txt"
            # names the gitignored path only to say it was avoided, but
            # "Posted to Slack instead of using email" must still affirm the
            # earlier Slack claim (Codex review: "Require a write claim for
            # body-only ignored paths").
            if _CHANNEL_NEGATION_FORWARD_ONLY.search(sentence[clause_start : match.start()]):
                continue
            return True
    return False


def detect_channel_warnings(
    body: str, *, source: str, path_hint: str = ""
) -> Tuple[Tuple[str, str], ...]:
    """Flag delivery claims the repo's own config contradicts.

    ``path_hint``, when given, is the alert file's own on-disk location --
    checked ALONGSIDE the body, not instead of it. Where an alert's body
    happens not to mention its own path, the loader already knows the true
    storage location, and a file sitting under a bare repo-relative
    ``artifacts/alerts`` is discarded by ``.gitignore`` whether or not the
    alert says so; the location itself is the evidence.
    """
    warnings: List[Tuple[str, str]] = []
    if source == "gmail_draft":
        # Not a claim in the body -- the draft's own existence is the evidence,
        # so this one is never subject to the affirmative-claim check.
        warnings.append(_DRAFT_WARNING)
    elif states_email_failure(body) and not (
        source == "gmail_message" and _message_failure_is_first_attempt_qualified(body)
    ):
        # A file-backed alert stating its own email non-delivery, which the
        # corrected Gate 9 contract requires. Without this, a digest run from
        # --alerts-dir alone exits 0 on direct evidence the email channel failed.
        #
        # A sent gmail_message record's own EXISTENCE is direct proof the
        # email was ultimately delivered -- see the pre-existing
        # test_sent_message_is_not_flagged_as_undelivered -- but its body can
        # still narrate its own EARLIER, superseded attempt ("Email send
        # failed on the first attempt; draft created") without contradicting
        # that; the retry succeeding is exactly why this record exists as a
        # SENT message at all (Codex review: "Trust sent-message evidence
        # over stale body text"). Excluded ONLY when the body explicitly
        # qualifies the failure as describing a first/earlier attempt, not
        # unconditionally for every gmail_message: a sent message whose body
        # baldly says "Email could not be sent." with no such qualifier is
        # still flagged, per the pre-existing (and still-passing)
        # test_sent_message_stating_its_own_failure_is_flagged -- that body
        # gives no reason to distrust its own plain, current-tense claim.
        warnings.append(
            (
                _DRAFT_WARNING[0],
                "the alert itself states email delivery did not happen",
            )
        )
    haystack = f"{body}\n{path_hint}" if path_hint else body
    for pattern, code, detail, exempt in _CHANNEL_CHECKS:
        # GITIGNORED_ALERT_PATH gets a PER-MATCH exemption (a supported
        # durable root mounted elsewhere can share the "artifacts/alerts"
        # directory name for its own unrelated reasons -- the same reasoning
        # path_hint already gets via _is_within_repo_checkout, applied to a
        # path claim extracted from the body) rather than the static
        # clause-scoped `exempt` regex the other checks use: a body can
        # state one copy's absolute, external path AND separately narrate a
        # bare repo-relative mention for a different copy of the SAME alert,
        # and only the occurrence actually covered by an external path may
        # be exempted, never the whole body.
        if code == "GITIGNORED_ALERT_PATH":
            exempt_match = _gitignored_mention_is_exempt
            # Masked so a file extension's own "." (e.g. "x.txt") can never
            # be mistaken for the sentence boundary that determines which
            # text the write-claim check above is allowed to see -- see
            # _mask_path_extension_periods.
            scan_text = _mask_path_extension_periods(haystack, pattern)
        elif code == "SLACK_DISABLED":
            exempt_match = _slack_mention_is_exempt
            scan_text = haystack
        else:
            exempt_match = None
            scan_text = haystack
        flagged = _is_affirmative_claim(scan_text, pattern, exempt=exempt, exempt_match=exempt_match)
        if (
            not flagged
            and code == "GITIGNORED_ALERT_PATH"
            and path_hint
            and re.search(pattern, path_hint, re.IGNORECASE)
        ):
            # path_hint is not prose -- it is the loader's own authoritative
            # on-disk location, already filtered to sit inside this checkout
            # (see the call site's `_is_within_repo_checkout` guard) before
            # it is ever passed here. It carries no verb for the write-claim
            # check above to find, but it needs none: the file's mere
            # location under a gitignored root already proves it, whether
            # or not the body's own free text ever mentions saving it there
            # (Codex review: "Require a write claim before flagging ignored
            # paths" targeted body prose, not this already-authoritative
            # location evidence).
            flagged = True
        if not flagged:
            continue
        warnings.append((code, detail))
    return tuple(warnings)


# --- Loaders -----------------------------------------------------------------


def _load_gmail_records(
    payload: Mapping[str, Any], *, key: str, source: str
) -> List[AlertRecord]:
    """Shared normalization for a Gmail ``list_drafts`` or sent-message export."""
    if key not in payload:
        raw: Any = []
    else:
        raw = payload[key]
    if not isinstance(raw, list):
        raise ValueError(
            f"export's '{key}' must be a JSON array, got {type(raw).__name__}"
        )
    records: List[AlertRecord] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        raw_date = item.get("date")
        if not raw_date:
            continue
        try:
            timestamp = parse_timestamp(str(raw_date))
        except ValueError:
            # A present but unparseable date. Skipping matches what an absent one
            # already does, and the count surfaces in the report; raising would
            # reach collect_records, which turns it into a CLI input error, so a
            # single malformed mailbox record would suppress the whole corpus --
            # the same failure just fixed for malformed alert filenames.
            continue
        subject = str(item.get("subject") or "")
        body = str(
            item.get("plaintextBody") or item.get("plainTextBody") or item.get("body") or ""
        )
        slot, slot_evidence = detect_slot_evidence(f"{subject}\n{body}")
        gate, gate_evidence = detect_gate_evidence(subject, body)
        records.append(
            AlertRecord(
                # An export item can arrive with no id at all. Falling back to
                # an empty string made every such record share one ledger key
                # once their text matched, so the second undelivered alert was
                # read as already seen and --fail-on-warnings stayed green
                # through it. The timestamp is the one identifying field the
                # contract does guarantee.
                alert_id=str(item.get("id") or item.get("messageId") or f"no-id@{raw_date}"),
                source=source,
                timestamp_utc=timestamp,
                subject=subject,
                body=body,
                slot=slot,
                slot_evidence=slot_evidence,
                gate=gate,
                gate_evidence=gate_evidence,
                verdict=detect_verdict(subject, body),
                warnings=detect_channel_warnings(body, source=source),
                thread_id=str(item.get("threadId") or item.get("thread_id") or ""),
            )
        )
    return records


def load_gmail_messages(payload: Mapping[str, Any]) -> List[AlertRecord]:
    """Normalize an export of SENT alert emails into records.

    This is the source the digest needs once the corrected Gate 9 prompts are
    deployed. A sent email does not appear in ``list_drafts`` at all, so a
    drafts-only digest goes blind to every alert produced by the very fix this
    change ships -- and Gate 2 tells an executor with no repo access to skip the
    alert file, which leaves a sent email as the *only* surviving record. Read
    without that source, the digest would report those days as slot-silent or
    roster-silent, inventing an outage out of a working alert path.

    A sent message is evidence of delivery, so unlike a draft it carries no
    automatic non-delivery warning -- only whatever its own body claims.

    Accepts ``{"messages": [...]}`` with the same per-item fields the drafts
    export uses (``id``, ``date``, ``subject``, ``plaintextBody``).
    """
    return _load_gmail_records(payload, key="messages", source="gmail_message")


def load_gmail_drafts(payload: Mapping[str, Any]) -> List[AlertRecord]:
    """Normalize a ``list_drafts`` JSON export into records.

    The export is whatever the Gmail connector returned, verbatim, so field
    presence is not guaranteed: a draft saved with no subject still has an id
    and a date. Missing text degrades to an empty string rather than raising --
    dropping a malformed draft would hide exactly the kind of half-written
    alert an operator most wants to see in the digest.

    Raises ``ValueError`` if ``drafts`` is present but is not a list -- a
    connector response of ``{"drafts": {}}`` would otherwise read as a clean
    zero-alert corpus, and ``{"drafts": 1}`` would raise an uncaught TypeError.
    """
    # An ABSENT key is the connector's documented no-results form (`{}`) and is
    # an empty corpus. A key explicitly present as null is a malformed response,
    # and `.get()` alone cannot tell the two apart -- which would let a broken
    # connector still report a clean zero-alert run.
    if "drafts" not in payload:
        raw_drafts: Any = []
    else:
        raw_drafts = payload["drafts"]
    if not isinstance(raw_drafts, list):
        raise ValueError(
            f"draft export's 'drafts' must be a JSON array, got {type(raw_drafts).__name__}"
        )
    records: List[AlertRecord] = []
    for draft in raw_drafts:
        if not isinstance(draft, Mapping):
            continue
        raw_date = draft.get("date")
        if not raw_date:
            # Without a timestamp the record cannot be placed on the calendar,
            # and every downstream view (streaks, coverage, --since) is calendar
            # ordered. Skipping is the honest outcome; the count is reported.
            continue
        try:
            timestamp = parse_timestamp(str(raw_date))
        except ValueError:
            # Present but unparseable: same treatment, same reason. See the sent
            # message loader.
            continue
        subject = str(draft.get("subject") or "")
        body = str(draft.get("plaintextBody") or draft.get("plainTextBody") or draft.get("body") or "")
        combined = f"{subject}\n{body}"
        slot, slot_evidence = detect_slot_evidence(combined)
        gate, gate_evidence = detect_gate_evidence(subject, body)
        records.append(
            AlertRecord(
                # See the sent-message loader: an export item with no id at
                # all would otherwise share one ledger key with every other
                # id-less record whose text matched, and the second undelivered
                # alert would read as already seen.
                alert_id=str(draft.get("id") or draft.get("messageId") or f"no-id@{raw_date}"),
                source="gmail_draft",
                timestamp_utc=timestamp,
                subject=subject,
                body=body,
                slot=slot,
                slot_evidence=slot_evidence,
                gate=gate,
                gate_evidence=gate_evidence,
                verdict=detect_verdict(subject, body),
                warnings=detect_channel_warnings(body, source="gmail_draft"),
                thread_id=str(draft.get("threadId") or draft.get("thread_id") or ""),
            )
        )
    return records


# Gate 9 names alert files "<YYYY-MM-DD>-<slot>-<gate>.txt". Parsing the name
# gives a date even for a file whose body never states one.
_ALERT_FILENAME = re.compile(
    # The gate token allows an interior dot so "…-gate6.5.txt" matches: gate 6.5
    # and 6.7 are real, and excluding the dot made the loader discard the
    # filename's date and slot for exactly the alerts it can least afford to
    # misfile. Anchored on the .txt suffix so the dot cannot swallow it.
    r"^(?P<day>\d{4}-\d{2}-\d{2})-(?P<slot>[A-Za-z0-9]+)-(?P<gate>[A-Za-z0-9_]+(?:\.\d+)?)\.txt$"
)


def _slot_from_filename_token(token: str) -> Optional[str]:
    """Resolve the ``<slot>`` field of a Gate 9 filename to a roster slot.

    Gate 9 names the file ``<YYYY-MM-DD>-<slot>-<gate>.txt`` but does not pin
    the slot's spelling, so both ``1430ET`` and a bare ``1430`` occur. The bare
    form is an exact roster slot the filename already handed us; running it back
    through :func:`detect_slot` would drop it, because that function requires a
    trailing ``ET`` before trusting four bare digits in free text (in a body,
    ``1230`` is as likely to be a dollar figure or an order count).

    The 9:35 slot gets the same unpadded treatment: Gate 9's natural token for
    it is ``935``, not ``0935``, since nothing in the filename forces a leading
    zero. Left unpadded, it matches nothing in ``SLOTS`` and the record is
    retained with ``slot=None`` unless the body happens to repeat it -- dropping
    a valid alert out of 09:35 coverage and streaks. A bare three-digit token is
    zero-padded before the membership check; no other roster slot is three
    digits, so this cannot misfire on one of them.
    """
    candidate = token.upper().removesuffix("ET").strip()
    if len(candidate) == 3 and candidate.isdigit():
        candidate = f"0{candidate}"
    return candidate if candidate in SLOTS else None


def _is_alert_candidate(path: Path, root: Path) -> bool:
    """Whether a ``.txt`` found under ``root`` sits somewhere alerts are kept.

    Pointing ``--alerts-dir`` at the whole cron-tracking root is the documented
    way to cover all six streams at once, but that tree holds far more ``.txt``
    than alerts. Ingesting all of it would count unrelated artifacts by their
    mtime and whatever slot or gate words happen to appear in them.

    So: a file inside any ``alerts/`` directory beneath the root, or one directly
    inside the directory the caller named. The second is a placement test only --
    :func:`_root_child_is_alert` decides whether such a file is actually an
    alert, because "directly under the named directory" means an asserted alerts
    directory in one invocation and the root of the whole cron tree in the other.

    ``alerts/`` need not be the file's IMMEDIATE parent: an offline or
    archived export can partition alerts by day beneath it --
    ``morning_dca/alerts/2026-08-26/2026-08-26-1030-gate5.txt`` -- and
    checking only ``path.parent.name`` rejected every file one level
    deeper than that, silently omitting them from a scan pointed at the
    cron-tracking root (Codex review: "Include descendants of discovered
    alert directories"). Any ancestor between the file and ``root`` named
    ``alerts`` now qualifies, not just the immediate one.
    """
    if path.parent == root:
        return True
    return "alerts" in path.relative_to(root).parts[:-1]


def _root_child_is_alert(path: Path, root: Path, record: AlertRecord) -> bool:
    """Whether a ``.txt`` directly under the named ``root`` is a Gate 9 alert.

    Naming an ``alerts`` directory outright is an assertion about everything in
    it, and is taken at face value. Naming the cron-tracking root is not: its
    top level holds run notes and diagnostics, and one of those carrying
    incidental slot or gate wording would otherwise be counted as an alert --
    with a delivery warning -- purely because of where the scan started. Such a
    file has to look like an alert: a Gate 9 contract filename, or the same
    slot-or-gate signature the draft path already requires. That closes an
    asymmetry as much as a bug: file records bypassed
    :func:`looks_like_executor_alert` entirely.
    """
    if root.name == "alerts":
        return True
    if _ALERT_FILENAME.match(path.name):
        return True
    return looks_like_executor_alert(record)


def load_alert_files(directory: Path) -> List[AlertRecord]:
    """Normalize a directory of Gate 9 repo alert files into records."""
    records: List[AlertRecord] = []
    if not directory.is_dir():
        return records
    from dca_fileio import iter_files_by_suffix_fast
    for path in iter_files_by_suffix_fast(directory, ".txt"):
        if not _is_alert_candidate(path, directory):
            continue
        match = _ALERT_FILENAME.match(path.name)
        body = path.read_text(encoding="utf-8", errors="replace")
        slot: Optional[str] = None
        day: Optional[datetime] = None
        if match:
            try:
                # ⚡ Bolt optimization: Use string slicing to bypass slow datetime.strptime overhead
                raw_day = match.group("day")
                if len(raw_day) != 10 or raw_day[4] != '-' or raw_day[7] != '-':
                    raise ValueError
                day = datetime(int(raw_day[:4]), int(raw_day[5:7]), int(raw_day[8:]), 0, 0, 0)
            except (ValueError, IndexError):
                # A contract-SHAPED filename carrying an impossible date, such as
                # "2026-13-40-1030-gate5.txt". _ALERT_FILENAME only checks the
                # digit groups, so this reaches here. Demote it to off-contract
                # rather than raising: collect_records turns a ValueError into a
                # CLI input error, so one malformed fallback artifact would
                # suppress every valid alert in the tree.
                match = None
        if match and day is not None:
            # Filed at ET noon, not midnight: the file name carries the ET *day*
            # with no time, and noon is the one hour of the day that cannot slide
            # across a date boundary when converted to UTC and back.
            timestamp = day.replace(hour=12, tzinfo=ET).astimezone(timezone.utc)
            slot_hint = match.group("slot")
            gate_hint = match.group("gate")
            # The filename's own slot token is authoritative when it names a
            # roster slot -- it is the executor's explicit label, not an
            # inference from prose.
            slot = _slot_from_filename_token(slot_hint)
            slot_evidence = "filename" if slot else ""
            date_evidence = "filename"
        else:
            timestamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            slot_hint = ""
            gate_hint = ""
            slot_evidence = ""
            date_evidence = ""
        if slot is None:
            slot, slot_evidence = detect_slot_evidence(f"{path.name}\n{slot_hint}\n{body}")
        # Identity is the path relative to the scanned root, not the bare
        # basename: the scan recurses through per-stream subdirectories, so
        # morning_dca/ and intraday_dca/ can both hold a same-named file for the
        # same day, and a basename key would make the ledger treat the second
        # one as already seen.
        try:
            relative = path.relative_to(directory).as_posix()
        except ValueError:  # pragma: no cover - rglob always yields descendants
            relative = path.name
        gate, gate_evidence = detect_gate_evidence(gate_hint, body)
        record = AlertRecord(
            alert_id=relative,
            source="alert_file",
            timestamp_utc=timestamp,
            subject=path.name,
            body=body,
            slot=slot,
            slot_evidence=slot_evidence,
            gate=gate,
            gate_evidence=gate_evidence,
            verdict=detect_verdict(path.name, body),
            warnings=detect_channel_warnings(
                body,
                source="alert_file",
                path_hint=str(path) if _is_within_repo_checkout(path) else "",
            ),
            revision=str(path.stat().st_mtime_ns),
            date_evidence=date_evidence,
        )
        # Built before the check, not after: deciding whether a root-level file
        # is an alert needs its parsed slot and gate.
        if path.parent == directory and not _root_child_is_alert(path, directory, record):
            continue
        records.append(record)
    return records


# --- Aggregation -------------------------------------------------------------


@dataclass
class Streak:
    """A run of consecutive ET days on which one slot failed the same gate."""

    slot: str
    gate: str
    start: date
    end: date
    days: int

    def to_json(self) -> Dict[str, Any]:
        return {
            "slot": self.slot,
            "gate": self.gate,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
        }


@dataclass
class Digest:
    """Everything the report renders, computed once."""

    records: List[AlertRecord]
    skipped: int = 0
    # Drafts set aside as not being executor alerts at all (see
    # looks_like_executor_alert). Reported, never silently dropped.
    non_alerts: int = 0
    by_gate: Counter = field(default_factory=Counter)
    by_slot: Counter = field(default_factory=Counter)
    by_verdict: Counter = field(default_factory=Counter)
    by_slot_gate: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
    warning_details: Dict[str, str] = field(default_factory=dict)
    streaks: List[Streak] = field(default_factory=list)
    days: List[date] = field(default_factory=list)
    # The NYSE trading days in the window -- what the roster was expected to
    # cover -- as opposed to `days`, which also carries any off-schedule firing.
    calendar: List[date] = field(default_factory=list)
    # Trading days on which nothing alerted at all. Ambiguous by nature (seven
    # clean runs look identical to seven that never fired), so reported, not
    # interpreted.
    silent_days: List[date] = field(default_factory=list)
    coverage: Dict[date, Dict[str, int]] = field(default_factory=dict)
    unknown_slot: int = 0
    unknown_gate: int = 0
    # Days intentionally excluded from `calendar` because the roster had not
    # yet finished firing for them when this digest was built (see
    # `_calendar_window_end`/`_roster_completion_time` in main()) -- NOT
    # because they fall outside the approved trading window. A day here can
    # still appear in `days` (an alert that already fired today is unioned
    # in regardless) and must be classified separately from a day genuinely
    # past the window's bounds.
    incomplete_days: Set[date] = field(default_factory=set)

    @property
    def first_day(self) -> Optional[date]:
        return self.days[0] if self.days else None

    @property
    def last_day(self) -> Optional[date]:
        return self.days[-1] if self.days else None


def _parse_date_list(value: Any) -> frozenset:
    """Parse a JSON list of ISO date strings, skipping anything unparseable.

    Shared by every ``config/market_calendar.json`` field this module reads:
    empty on anything that isn't a list, and on any entry that isn't a valid
    ISO date -- both directions degrade toward the calendar knowing about
    fewer closures, never more, which is the same "under-report precision
    rather than crash" bias every other config fallback here takes.
    """
    if not isinstance(value, list):
        return frozenset()
    days = set()
    for item in value:
        if isinstance(item, str):
            try:
                days.add(date.fromisoformat(item))
            except ValueError:
                continue
    return frozenset(days)


def _configured_holidays() -> frozenset:
    """Closures from ``config/market_calendar.json``'s ``holidays_closed``.

    This is the SAME field ``dca_runner.market_day_status`` (Gate 0's actual
    authority for every live executor) treats as authoritative. ``nyse_holidays``
    computes its set from a formula instead of reading that config, so the two
    can disagree: a closure added to the config file after a formula-unaware
    date (an ad hoc market closure the formula has no way to predict) would
    have every executor correctly end silently under Gate 0, while this digest
    -- unaware of the same closure -- still expected the roster to fire and
    reported a fabricated roster-wide outage on a day nothing was ever
    supposed to alert. Read here and unioned into the computed set below so
    the two calendars cannot drift apart on a closure either one knows about.
    """
    return _parse_date_list(_read_calendar_config("market_calendar.json").get("holidays_closed"))


def is_trading_day(day: date) -> bool:
    """Whether the exchange was open on ``day`` -- weekday, and not a holiday
    by either the computed formula or the configured closure list.

    Deliberately separate from membership in a digest's ``calendar``: since the
    calendar is clamped to the approved trading window, absence from it no longer
    implies the market was shut. The coverage matrix needs both facts to label an
    off-calendar alert correctly.
    """
    return day.weekday() < 5 and day not in nyse_holidays(day.year) and day not in _configured_holidays()


def trading_days_between(start: date, end: date) -> List[date]:
    """Every NYSE trading day in ``[start, end]``, inclusive.

    Weekdays minus the computed NYSE holiday set AND the configured closure
    list (see :func:`_configured_holidays` for why both). This is the digest's
    calendar -- the days the seven-slot roster was *expected* to fire -- as
    distinct from the days it was actually observed to alert on. The
    difference between the two is the whole point: a day present here and
    absent from the alert record is a day the roster said nothing at all.
    """
    if end < start:
        return []
    holidays: set = set(_configured_holidays())
    for year in range(start.year, end.year + 1):
        holidays |= set(nyse_holidays(year))
    days: List[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in holidays:
            days.append(day)
        day += timedelta(days=1)
    return days


def _read_calendar_config(name: str) -> Dict[str, Any]:
    """Read one ``config/*.json`` schedule file, or ``{}`` when it is unavailable.

    Degrading rather than raising is deliberate, and specific to this module: the
    digest places no orders, and the only thing a missing config can cost here is
    calendar precision. Every caller below states which direction it falls back
    in, and each one falls back toward reporting MORE silence rather than less --
    an over-reported quiet day costs a look, an under-reported one is the outage
    this tool exists to surface going unseen.
    """
    try:
        decoded = json.loads((get_config_dir() / name).read_bytes())
    except (OSError, ValueError):
        return {}
    # Syntactically valid JSON whose top level isn't an object -- an array,
    # a bare scalar, `null` -- degrades the same way a missing or unparseable
    # file does. Returning it as-is would hand every caller's `.get(...)` an
    # AttributeError instead of the documented empty-calendar fallback, and
    # calendar aggregation runs outside the CLI's input-error handler, so that
    # would crash the whole digest rather than merely losing precision.
    return decoded if isinstance(decoded, dict) else {}


def approved_trading_window() -> Optional[Tuple[date, date]]:
    """The approved live-trading bounds from ``config/trading_window.json``.

    ``None`` when the file is missing or malformed, which the caller reads as
    "do not clamp".
    """
    cfg = _read_calendar_config("trading_window.json")
    try:
        start = date.fromisoformat(cfg["start_date"])
        end = date.fromisoformat(cfg["end_date"])
    except (KeyError, TypeError, ValueError):
        return None
    if start > end:
        # Syntactically valid dates in a semantically inverted order degrade
        # the same way a missing or unparseable file does: expected_roster_days
        # would otherwise treat [end, start) as empty and remove every trading
        # day from the expected calendar, hiding a scheduler-wide outage
        # behind an empty ROSTER-SILENT list instead of clamping nothing.
        return None
    return (start, end)


def early_close_days() -> frozenset:
    """The 1:00 PM ET early closes from ``config/market_calendar.json``.

    Empty when the file is missing or malformed -- the same direction as every
    other fallback here, since an empty set leaves the roster-wide calendar in
    place and can only over-report.
    """
    return _parse_date_list(_read_calendar_config("market_calendar.json").get("early_close_1300"))


def expected_roster_days(start: date, end: date) -> List[date]:
    """The days in ``[start, end]`` the seven-slot roster was *scheduled* to fire.

    ``trading_days_between`` answers "was the exchange open". This answers "was
    anything supposed to run", which is the question the coverage matrix and the
    ROSTER-SILENT list actually ask, and the two diverge outside
    ``config/trading_window.json``'s approved bounds: the external scheduler's
    trigger window stops there (``config/scheduled_tasks.json``), so a weekday
    before ``start_date`` or after ``end_date`` is a day nothing was scheduled,
    not a day the scheduler died. The window currently ends 2026-10-30, so an
    open-ended digest run in November would otherwise mark every ordinary
    weekday as a roster-wide outage -- turning the report's most serious finding
    into its most common one, which is how a warning stops being read.

    Alerts that landed outside the window are still counted and still get a
    coverage row: ``build_digest`` unions the observed days in. This bounds only
    what the report claims was *expected*.
    """
    days = trading_days_between(start, end)
    window = approved_trading_window()
    if window is None:
        return days
    window_start, window_end = window
    return [day for day in days if window_start <= day <= window_end]


def _slot_runs_after_early_close(slot: str) -> bool:
    """True for a roster slot whose nominal time is at or after 1:00 PM ET.

    ``config/market_calendar.json`` states the rule this reads: "early_close_1300
    days close at 1:00 PM ET; any slot at or after 1:00 PM ET must end silently
    on those days." That covers 1330, 1430 and 1530.
    """
    try:
        return int(slot[:2]) >= 13
    except (ValueError, IndexError):
        return False


def _compute_streaks(records: Sequence[AlertRecord], calendar: Sequence[date]) -> List[Streak]:
    """Find the longest run of consecutive alerting days per (slot, gate) pair.

    "Consecutive" is measured against ``calendar`` -- the NYSE trading days in
    the reported window -- not against raw calendar days. The executors only
    fire on trading days, so counting calendar days would break every real
    streak at each weekend and holiday, and a Friday-then-Monday repeat (the
    single most common shape here) would never register as a streak at all.

    The calendar is the *expected* trading days rather than the observed
    alerting days, so a trading day on which the whole roster stayed silent
    still occupies a position and correctly breaks any streak spanning it.
    Deriving it from observed alerts instead would have made a roster-wide
    alerting outage invisible in exactly the report meant to reveal it.

    The calendar is also per-slot, not one roster-wide index.
    ``config/market_calendar.json`` marks early closes at 1:00 PM ET on which the
    13:30, 14:30 and 15:30 slots are required to end silently, so those days are
    not days those slots were expected to fire. Leaving them in the index would
    put a position between two identical failures either side of an early close
    and report them as two one-day streaks instead of one two-day repeat -- the
    same false break a roster-silent day correctly causes, but here for a day the
    slot was never scheduled on. The four pre-13:00 slots keep the full calendar:
    they trade normally on an early-close day.
    """
    early_close = early_close_days()
    # Two indexes, built once. The late-slot one simply omits the early closes,
    # so the days either side of one become adjacent positions for those slots.
    full_index = {day: position for position, day in enumerate(calendar)}
    late_index = {
        day: position
        for position, day in enumerate(day for day in calendar if day not in early_close)
    }

    grouped: Dict[Tuple[str, str], set] = defaultdict(set)
    for record in records:
        if record.slot and record.gate:
            grouped[(record.slot, record.gate)].add(record.et_date)

    streaks: List[Streak] = []
    for (slot, gate), day_set in grouped.items():
        index_of = late_index if _slot_runs_after_early_close(slot) else full_index
        # An alert can land on a day this slot's calendar does not contain -- a
        # weekend or holiday firing, or a late slot firing on an early close,
        # each of which is itself a Gate 0/Gate 1 failure and appears in the gate
        # counts. Such a day has no position in the trading calendar, so it
        # cannot extend or bridge a streak; drop it from this measure only.
        ordered = sorted(day for day in day_set if day in index_of)
        if not ordered:
            continue
        best_start = best_end = ordered[0]
        best_len = 1
        run_start = ordered[0]
        run_len = 1
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if index_of[current] == index_of[previous] + 1:
                run_len += 1
            else:
                run_start = current
                run_len = 1
            if run_len > best_len:
                best_len = run_len
                best_start = run_start
                best_end = current
        streaks.append(
            Streak(slot=slot, gate=gate, start=best_start, end=best_end, days=best_len)
        )

    # Longest first: a five-day repeat of one gate on one slot is the headline,
    # and a wall of one-day entries below it is noise the operator can stop at.
    streaks.sort(key=lambda s: (-s.days, s.slot, s.gate))
    return streaks


def _coverage_map(records: Iterable[AlertRecord]) -> Dict[date, Dict[str, int]]:
    """Alerts per slot per ET day, for the records given."""
    coverage: Dict[date, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for record in records:
        if record.slot:
            coverage[record.et_date][record.slot] += 1
    return {day: dict(slots) for day, slots in coverage.items()}


def build_digest(
    records: Iterable[AlertRecord],
    *,
    skipped: int = 0,
    window_start: Optional[date] = None,
    window_end: Optional[date] = None,
    roster_observed: Optional[Set[date]] = None,
    roster_coverage: Optional[Dict[date, Dict[str, int]]] = None,
    incomplete_days: Optional[Set[date]] = None,
) -> Digest:
    """Aggregate normalized records into the reportable view."""
    ordered = sorted(records, key=lambda r: r.timestamp_utc)
    digest = Digest(records=ordered, skipped=skipped)
    digest.incomplete_days = set(incomplete_days) if incomplete_days else set()

    for record in ordered:
        digest.by_gate[record.gate or "unclassified"] += 1
        digest.by_slot[record.slot or "unclassified"] += 1
        digest.by_verdict[record.verdict or "unclassified"] += 1
        if record.slot and record.gate:
            digest.by_slot_gate[(record.slot, record.gate)] += 1
        if record.slot is None:
            digest.unknown_slot += 1
        if record.gate is None:
            digest.unknown_gate += 1
        for code, detail in record.warnings:
            digest.warnings[code] += 1
            digest.warning_details.setdefault(code, detail)

    # Coverage may be supplied from the unfiltered roster: the matrix always
    # prints all seven columns, so under --slot the other six must still show
    # what actually happened rather than a wall of misleading dots.
    coverage = roster_coverage if roster_coverage is not None else _coverage_map(ordered)

    # The report calendar is every NYSE trading day in the window, whether or not
    # anything alerted on it. A day the whole roster stayed silent is the single
    # most important thing this matrix can show -- it is what a scheduler-wide
    # alerting failure looks like -- and it only appears if the calendar is built
    # from expected days rather than observed ones.
    #
    # The window spans the caller's bounds when given (so `--since` renders its
    # full range, silent days included) and otherwise the observed records. Days
    # carrying an alert are unioned in regardless, so an off-schedule weekend or
    # holiday firing still gets a row instead of vanishing from its own report.
    #
    # "Expected" also means scheduled, not merely open: `expected_roster_days`
    # clamps the calendar to config/trading_window.json's approved bounds, so a
    # report whose range runs past them does not label days the trigger window
    # had already closed on as roster-wide outages.
    observed = {record.et_date for record in ordered}
    span_start = window_start or (min(observed) if observed else None)
    span_end = window_end or (max(observed) if observed else None)
    calendar: List[date] = []
    if span_start is not None and span_end is not None:
        calendar = expected_roster_days(span_start, span_end)
    digest.calendar = calendar
    # Also every day the coverage map has something to show. Under `--slot`
    # coverage comes from the unfiltered roster, so another slot's off-schedule
    # firing -- a weekend, a holiday, a weekday past the approved window -- is in
    # neither `calendar` nor this slot's `observed`, and its row would be dropped
    # from the matrix with a nonzero count sitting in it. Without a filter this
    # adds nothing: those keys are a subset of `observed`.
    digest.days = sorted(set(calendar) | observed | set(coverage))
    digest.coverage = dict(coverage)
    # Roster silence is judged against the WHOLE roster, never the filtered view.
    # Under `--slot 1130`, a day where 1130 was quiet but four other slots alerted
    # is a gap in one slot, not a scheduler-wide outage, and labelling it "roster
    # silent" would turn the report's most serious finding into a routine one.
    roster_days = observed if roster_observed is None else roster_observed
    digest.silent_days = [day for day in calendar if day not in roster_days]
    # Streaks get `calendar` PLUS any incomplete day(s) -- unlike silence
    # detection above, which must keep excluding them (an incomplete day is
    # never roster-silent evidence, that's the whole point of excluding it
    # from `calendar`). A record that already alerted today is still in
    # `ordered` (the record-filtering window was never clamped), so without
    # today's own position in the streak calendar, `_compute_streaks` cannot
    # see it as adjacent to yesterday's matching alert and a genuine 2-day
    # repeat reads as a 1-day one.
    streak_calendar = sorted(set(calendar) | digest.incomplete_days)
    digest.streaks = _compute_streaks(ordered, streak_calendar)
    return digest


# --- Ledger ------------------------------------------------------------------


def ledger_path(root: Optional[str] = None) -> Path:
    """Where repeat runs persist what they have already reported.

    Under ``stream_root`` like every other artifact in this repo -- never a
    literal path -- so ``$DCA_CRON_TRACKING_ROOT`` moves it with everything else.
    """
    base = root if root is not None else stream_root("alert_digest")
    return Path(base) / "seen_alerts.json"


def load_ledger(path: Path) -> Dict[str, Any]:
    """Read the prior digest's ledger, tolerating a missing or corrupt file.

    A ledger is a convenience, not evidence: if it cannot be read, the correct
    behavior is to report everything as new rather than to fail the digest and
    leave the operator with nothing.
    """
    if not path.is_file():
        return {"schema_version": LEDGER_SCHEMA_VERSION, "seen": []}
    try:
        loaded = json.loads(path.read_bytes())
    except (json.JSONDecodeError, OSError, UnicodeError):
        # UnicodeError covers a ledger corrupted with invalid UTF-8, which
        # read_text raises before json ever sees it. Without it the scheduled
        # digest crashes on exactly the case this function promises to survive.
        return {"schema_version": LEDGER_SCHEMA_VERSION, "seen": []}
    if not isinstance(loaded, dict):
        return {"schema_version": LEDGER_SCHEMA_VERSION, "seen": []}
    loaded.setdefault("schema_version", LEDGER_SCHEMA_VERSION)
    seen = loaded.get("seen")
    loaded["seen"] = [str(item) for item in seen] if isinstance(seen, list) else []
    return loaded


def save_ledger(path: Path, keys: Iterable[str], *, now: Optional[datetime] = None) -> None:
    """Persist the seen-key set atomically."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "updated_utc": stamp.isoformat().replace("+00:00", "Z"),
        "seen": sorted(set(keys)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(str(path), json.dumps(payload, indent=2, sort_keys=True) + "\n")


# --- Rendering ---------------------------------------------------------------


def _classify_coverage_day(
    day: date, silent: Set[date], calendar: Set[date], incomplete: Set[date] | frozenset[date] = frozenset()
) -> str:
    """One of ``"roster_silent"``, ``"incomplete_roster_day"``,
    ``"outside_approved_trading_window"``, ``"not_a_trading_day"``, or
    ``"expected"`` for a single coverage-matrix row.

    Shared between the text coverage matrix and the JSON payload so the two
    cannot state different things about the same day again -- round 12 caught
    exactly that gap: JSON coverage carried only slot counts, with no way for a
    machine consumer to tell a real roster-wide outage (``roster_silent``) from
    an off-schedule firing outside the approved trading window, even though the
    text report has labelled the two differently since round 7.

    ``incomplete`` is checked before falling back to the window-boundary
    labels: a day excluded from ``calendar`` because its own roster had not
    finished firing yet (see ``Digest.incomplete_days``) is inside the
    approved trading window, not past it -- ``outside_approved_trading_window``
    would misreport it as the far side of ``config/trading_window.json``'s
    ``end_date``.
    """
    if day in silent:
        return "roster_silent"
    if day in calendar:
        return "expected"
    if day in incomplete:
        return "incomplete_roster_day"
    return "outside_approved_trading_window" if is_trading_day(day) else "not_a_trading_day"


_COVERAGE_MARKERS = {
    "roster_silent": "  <- roster silent",
    "incomplete_roster_day": "  <- roster not finished yet today",
    "outside_approved_trading_window": "  <- outside the approved trading window",
    "not_a_trading_day": "  <- NOT a trading day",
    "expected": "",
}


def _coverage_lines(digest: Digest) -> List[str]:
    """The day x slot matrix and the roster-silent summary beneath it.

    Extracted so the zero-alert path can render it too: a window of trading days
    with no alerts at all is the most severe thing this report can show, and it
    has no counts to print -- only this.
    """
    lines: List[str] = ["", "COVERAGE  (alerts per slot per ET day; '.' = no alert)"]
    lines.append("  day         " + " ".join(f"{slot:>5}" for slot in SLOTS))
    silent = set(digest.silent_days)
    calendar = set(digest.calendar)
    for day in digest.days:
        row = digest.coverage.get(day, {})
        cells = " ".join(f"{row.get(slot, 0) or '.':>5}" for slot in SLOTS)
        marker = _COVERAGE_MARKERS[_classify_coverage_day(day, silent, calendar, digest.incomplete_days)]
        lines.append(f"  {day}  {cells}{marker}")

    if digest.silent_days:
        lines.append("")
        lines.append(
            f"ROSTER-SILENT TRADING DAYS: {len(digest.silent_days)} "
            f"({', '.join(str(day) for day in digest.silent_days[:10])}"
            f"{', ...' if len(digest.silent_days) > 10 else ''})"
        )
        lines.append("  Ambiguous: seven clean runs and seven that never fired look identical")
        lines.append("  here. Cross-check each stream's latest_<stream>_run.json pointer.")
    return lines


def render_text(digest: Digest, *, new_keys: Optional[Sequence[str]] = None) -> str:
    """Render the operator-facing digest."""
    lines: List[str] = []
    total = len(digest.records)

    lines.append("DCA EXECUTOR ALERT DIGEST")
    if digest.first_day and digest.last_day:
        lines.append(f"  window        : {digest.first_day} .. {digest.last_day} (ET)")
    lines.append(f"  alerts        : {total}")
    if new_keys is not None:
        lines.append(f"  new since last: {len(new_keys)}")
    if digest.skipped:
        lines.append(f"  skipped       : {digest.skipped} (no usable timestamp)")
    if digest.non_alerts:
        lines.append(f"  not alerts    : {digest.non_alerts} (no slot or gate; --keep-unmatched to include)")
    if not total:
        lines.append("")
        lines.append("No alerts in range. Note that silence is ambiguous: a slot that ran")
        lines.append("clean and a slot that never fired at all both alert on nothing.")
        if not digest.calendar and not digest.days:
            # Both empty, not just `calendar`: under `--slot`, a default run
            # before today's roster finishes can have an EMPTY calendar (the
            # unfinished-day clamp excludes today, and the window is only
            # today) while `digest.days`/`digest.coverage` still carry
            # today's own incomplete-but-observed row from another slot that
            # already alerted. Returning here on `calendar` alone hid that
            # row entirely instead of falling through to render it.
            return "\n".join(lines) + "\n"
        # A window with trading days but zero alerts is the WORST case, not a
        # trivial one: it is what a total scheduler outage looks like. Returning
        # early here would show it only in --json, hiding the most severe finding
        # from the default operator-facing report. Fall through to the coverage
        # matrix and the silent-day list, which are exactly what make it legible.
        lines.append("")
        # Keyed on `digest.silent_days`, not the bare fact that `total` (this
        # view's own record count) is zero: under `--slot`, `total` is the
        # SLOT-FILTERED count and can be zero while `roster_observed`/
        # `roster_coverage` (and so `digest.silent_days`) still reflect real
        # alerts from OTHER slots -- claiming the whole calendar is
        # roster-silent there contradicts the nonzero cells the coverage
        # matrix prints two lines below.
        if not digest.silent_days:
            lines.append(
                "No roster-wide outage in range -- see the coverage matrix below for "
                "alerts from other slots."
            )
        elif len(digest.silent_days) == len(digest.calendar):
            lines.append(f"Every one of the {len(digest.calendar)} trading days below is roster-silent.")
        else:
            lines.append(
                f"{len(digest.silent_days)} of the {len(digest.calendar)} trading days below "
                "is roster-silent; the rest show alerts from another slot."
            )
        lines.extend(_coverage_lines(digest))
        return "\n".join(lines) + "\n"

    lines.append("")
    lines.append("BY GATE")
    for gate, count in digest.by_gate.most_common():
        share = 100.0 * count / total
        lines.append(f"  {gate:<24} {count:>4}  ({share:4.1f}%)")

    lines.append("")
    lines.append("BY SLOT")
    for slot in list(SLOTS) + ["unclassified"]:
        count = digest.by_slot.get(slot, 0)
        if count:
            lines.append(f"  {slot:<24} {count:>4}")

    lines.append("")
    lines.append("BY VERDICT")
    for verdict, count in digest.by_verdict.most_common():
        lines.append(f"  {verdict:<24} {count:>4}")

    repeats = [s for s in digest.streaks if s.days > 1]
    lines.append("")
    lines.append("LONGEST REPEATS  (same slot, same gate, consecutive alerting days)")
    if repeats:
        for streak in repeats[:10]:
            lines.append(
                f"  {streak.slot} {streak.gate:<24} {streak.days:>2}d  "
                f"{streak.start} .. {streak.end}"
            )
    else:
        lines.append("  none -- no slot failed the same gate on two consecutive days")

    lines.extend(_coverage_lines(digest))

    if digest.warnings:
        lines.append("")
        lines.append("DELIVERY WARNINGS")
        for code, count in digest.warnings.most_common():
            lines.append(f"  {code} x{count}")
            lines.append(f"    {digest.warning_details.get(code, '')}")

    if digest.unknown_slot or digest.unknown_gate:
        lines.append("")
        lines.append("UNCLASSIFIED")
        lines.append(f"  no slot identified: {digest.unknown_slot}")
        lines.append(f"  no gate identified: {digest.unknown_gate}")

    return "\n".join(lines) + "\n"


def render_json(digest: Digest, *, new_keys: Optional[Sequence[str]] = None) -> str:
    silent_set = set(digest.silent_days)
    calendar_set = set(digest.calendar)
    payload: Dict[str, Any] = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "window": {
            "first_et_day": digest.first_day.isoformat() if digest.first_day else None,
            "last_et_day": digest.last_day.isoformat() if digest.last_day else None,
        },
        "totals": {
            "alerts": len(digest.records),
            "skipped_no_timestamp": digest.skipped,
            "not_executor_alerts": digest.non_alerts,
            "unclassified_slot": digest.unknown_slot,
            "unclassified_gate": digest.unknown_gate,
            "trading_days_in_window": len(digest.calendar),
            "roster_silent_trading_days": len(digest.silent_days),
        },
        "roster_silent_days": [day.isoformat() for day in digest.silent_days],
        "by_gate": dict(digest.by_gate),
        "by_slot": dict(digest.by_slot),
        "by_verdict": dict(digest.by_verdict),
        "by_slot_gate": {f"{slot}/{gate}": n for (slot, gate), n in digest.by_slot_gate.items()},
        "streaks": [s.to_json() for s in digest.streaks],
        "coverage": {
            day.isoformat(): digest.coverage.get(day, {}) for day in digest.days
        },
        # Per-row classification for `coverage`, matching what the text report's
        # markers already say -- "roster_silent" (expected, nothing alerted),
        # "outside_approved_trading_window" or "not_a_trading_day" (off-calendar,
        # split by whether the exchange was actually open), or "expected"
        # (an ordinary in-window trading day, whether or not it alerted). Without
        # this a machine consumer reading only `coverage` cannot distinguish a
        # roster-wide outage from an off-schedule firing -- exactly the
        # distinction the coverage matrix exists to draw.
        "day_classification": {
            day.isoformat(): _classify_coverage_day(day, silent_set, calendar_set, digest.incomplete_days)
            for day in digest.days
        },
        "warnings": [
            {"code": code, "count": count, "detail": digest.warning_details.get(code, "")}
            for code, count in digest.warnings.most_common()
        ],
        "alerts": [record.to_json() for record in digest.records],
    }
    if new_keys is not None:
        payload["new_since_last_digest"] = list(new_keys)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# --- CLI ---------------------------------------------------------------------


# Slot evidence that is ordinary English rather than a declaration. Both a bare
# clock time and a stream word can appear in a personal draft by accident, so
# neither identifies an executor alert on its own.
#
# Deliberately excludes "declared_clock" (a "scheduled"/"expected N ET" or
# "scheduled"/"expected H:MM AM/PM" clock, from detect_slot_evidence's step 2)
# even though it carries the word "clock": that form is itself a declaration,
# Gate 9's own documented phrasing, and reads no more incidentally than the
# "labelled" tag does -- an alert whose only recognizable slot signal is
# "Expected 1130 ET" with no gate must still be retained (with --keep-
# unmatched not required) rather than discarded as a weak, ambiguous mention.
_WEAK_SLOT_EVIDENCE = frozenset({"clock", "stream_word"})

# Gate evidence that is ordinary English rather than a declaration. A bare
# "Gate N" scanned out of free text -- the subject/body fallback in
# detect_gate_evidence -- is exactly as ordinary as a stream word: "Meet at
# Gate 5 before boarding" names a gate without being an executor alert at all.
_WEAK_GATE_EVIDENCE = frozenset({"free_text"})


def looks_like_executor_alert(record: AlertRecord) -> bool:
    """Whether a record carries any signature of a Gate 9 executor alert.

    The CLI accepts a raw ``list_drafts`` export and does not require the caller
    to have pre-filtered it, so an ordinary personal draft can arrive in the
    corpus. Treating it as an alert is not harmless: it inflates the
    unclassified counts, and because every draft carries
    ``EMAIL_DRAFT_NOT_DELIVERED`` by construction, a single unrelated draft
    would hold ``--fail-on-warnings`` red forever.

    A record counts as an alert if it names a slot or a gate. A verdict alone is
    NOT enough: the verdict vocabulary is ordinary English -- "rejected",
    "skipped", "run report" -- so a personal draft about a rejected application
    would otherwise qualify, collect ``EMAIL_DRAFT_NOT_DELIVERED`` like every
    draft, and hold ``--fail-on-warnings`` red indefinitely.

    Slot and gate are the executor-specific signatures; either one alone still
    counts, because a half-parsed alert is exactly what an operator most wants
    surfaced. Only a record with neither is set aside, and the count is always
    reported.

    A slot or gate found only by WEAK evidence is the exception -- a bare
    wall-clock time, a stream word, or a bare "Gate N" mention scanned out of
    free text. "10:30 AM", "midday", and "Gate 5" are all ordinary English, so
    "Lunch at 10:30 AM", "Midday lunch plan", and "Meet at Gate 5 before
    boarding" would each be admitted alone -- collecting
    ``EMAIL_DRAFT_NOT_DELIVERED`` like every draft and holding
    ``--fail-on-warnings`` red forever, the same hole the verdict-only rule had
    before it was tightened. Such a record needs the OTHER signal too, and that
    signal has to be a declared one in turn: two independent weak signals
    together (a clock time AND a bare gate mention, say) is corroboration a
    personal note is unlikely to produce by coincidence, but either alone is
    not.

    What remains standing alone is evidence that DECLARES the signal rather
    than happening to contain it: a scheduler task name, an explicit
    ``1030 ET`` label, a Gate 9 filename, a named reason code, a labelled
    "Failing gate:" line. None of those turn up in a personal draft.
    """
    has_declared_gate = record.gate is not None and record.gate_evidence not in _WEAK_GATE_EVIDENCE
    has_declared_slot = record.slot is not None and record.slot_evidence not in _WEAK_SLOT_EVIDENCE
    if has_declared_gate or has_declared_slot:
        return True
    return record.gate is not None and record.slot is not None


def _event_identity(record: AlertRecord) -> Optional[Tuple[date, str, str]]:
    """The Gate 9 event a record describes, or None if it cannot be pinned down."""
    if record.slot and record.gate:
        return (record.et_date, record.slot, record.gate)
    return None


def _slot_scheduled_utc(et_date: date, slot: str) -> datetime:
    """A roster slot's own nominal ET trigger time on ``et_date``, in UTC.

    Used as the ordering bound in ``merge_cross_source()``'s gmail_message
    proof-of-delivery check instead of an alert FILE record's own
    ``timestamp_utc``. A contract-named file is stamped at ET *noon*
    regardless of its slot (see ``load_alert_files`` -- the filename carries
    only the day, and noon is the one hour immune to a UTC date-boundary
    slide), so comparing a message directly against it misjudged every
    pre-noon slot (0935/1030/1130): a genuine retry sent shortly after its
    own morning trigger compared as "older than the file" and left a
    resolved failure stuck stale. Symmetrically, an afternoon slot's file
    could be cleared by an earlier, unrelated same-day firing's message
    merely for landing after noon. The slot's own scheduled trigger -- the
    earliest moment a genuine retry for THAT firing could exist -- is a
    bound anchored to the actual roster instead of an artifact of how the
    file's day happens to be stored (Codex review: "Compare retries against
    the file's actual event time").
    """
    return datetime.combine(et_date, time(hour=int(slot[:2]), minute=int(slot[2:])), tzinfo=ET).astimezone(
        timezone.utc
    )


def _file_event_utc(record: AlertRecord) -> datetime:
    """The best-available estimate of when the firing ``record`` (an alert
    file) describes actually happened.

    Every roster slot's nominal trigger time (:func:`_slot_scheduled_utc`) is
    shared by EVERY firing of that (day, slot) -- it cannot distinguish an
    early, successful firing from a later same-day retry of the SAME slot
    and gate that failed and overwrote the first firing's fixed filename. A
    12:30 slot that sends successfully at 12:35 and then fires the same
    gate/reason again at 14:30 with email delivery failing has both firings
    pass the "not older than the slot's nominal trigger" check equally, so
    the earlier, unrelated success could still wrongly clear the later
    firing's genuine failure (Codex review: "Compare delivery proof with the
    overwritten firing").

    The file's own ``revision`` -- its real filesystem mtime, always the
    LAST time Gate 9 (over)wrote it, since a retry overwrites rather than
    appends -- is the actual ground truth for which firing survives on disk,
    and is used when available. Falls back to the slot's nominal trigger
    only for a record with no revision at all (a synthetic/test record, or
    any source other than a loaded alert file), which is strictly the
    coarser, previously-established bound.

    That mtime is trusted only when it is at least PLAUSIBLE, though: this
    tool explicitly supports offline directory ingestion (``--alerts-dir``
    over a copied, restored, or extracted tree), and none of those
    operations reliably preserve the original write time -- ``st_mtime_ns``
    then reflects the copy, not the Gate 9 firing, silently misordering a
    genuine retry against it (Codex review: "Do not treat copied-file mtime
    as the alert event time"). A file whose own name declares its day
    authoritatively (``date_evidence == "filename"``) gives a cheap
    cross-check with no new data needed: a genuine live write always lands
    on the SAME trading day the filename names (or, right at a UTC
    boundary, the adjacent one) -- an mtime further than a day from that
    declared day cannot be the original write, and the coarser nominal-
    trigger bound is used instead. A file with no such authoritative name
    has nothing to cross-check against, so its mtime is trusted as before.
    """
    if record.revision:
        try:
            mtime = datetime.fromtimestamp(int(record.revision) / 1_000_000_000, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            mtime = None
        if mtime is not None and (
            record.date_evidence != "filename" or abs((mtime.astimezone(ET).date() - record.et_date).days) <= 1
        ):
            return mtime
    if record.slot is not None:
        return _slot_scheduled_utc(record.et_date, record.slot)
    return record.timestamp_utc


# The reason an executor gives for stopping, as Gate 9 requires it to be stated:
# "State the slot, the failing gate, and the reason in each" of the three
# channels. It is the only per-incident content the contract guarantees appears
# in both an email and its repo alert file, which makes it the corroborating
# signal for pairing the two.
# Ordered by strength. Gate 9 requires the reason to be *stated*, not to carry a
# literal "Reason:" label, and the corpus writes it both ways -- so recognizing
# only the labelled form left two same-day firings that said "aborted because
# quotes were stale" and "aborted because the budget was exhausted" with no
# signature at all, which _corroborates then read as "nothing contradicts" and
# merged into one incident.
# Each capture stops at the sentence, not the end of the line. The corrected
# Gate 9 contract has the executor APPEND non-delivery evidence to the alert
# file, so the file copy of one firing routinely reads "...quotes were stale.
# Email send attempt failed; draft created." while the emailed copy stops at the
# first sentence. A greedy capture makes those two signatures differ and splits
# one firing into two records -- the prompt fix defeating the merge.
_REASON_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"\breasons?\s*:\s*([^.;\n]+)", re.IGNORECASE),
    re.compile(r"\bbecause\s+(?:of\s+)?([^.;\n]+)", re.IGNORECASE),
    re.compile(r"\bdue to\s+([^.;\n]+)", re.IGNORECASE),
    re.compile(r"\bowing to\s+([^.;\n]+)", re.IGNORECASE),
    # Lowest priority: a free-form clause with none of the labelled/causal
    # cues above, immediately followed by the verdict word itself --
    # "Quotes were stale; aborted" and "Budget was exhausted; aborted" both
    # state a real, DIFFERENT reason with no "because"/"due to"/"reason:" at
    # all. Gate 9 requires the reason to be stated, not that it use one of
    # those four cue phrases, and without this fallback both bodies'
    # _reason_signature was None, which _corroborates treats as "cannot
    # contradict" and merges as if they were the same retry -- dropping one
    # of two genuinely distinct aborts (Codex review: "Preserve incidents
    # whose free-form reasons differ"). Anchored to a comma/semicolon
    # directly before the verdict word (not a period -- _REASON_PATTERNS'
    # own captures already stop there, and _SENTENCE_SPLIT would have cut a
    # period-separated clause into a different sentence before this even
    # runs), so this cannot reach an unrelated, distant clause.
    re.compile(
        r"([^,.;\n]+)[,;]\s*(?:cap breach|reject(?:ed|ion)|partial batch|"
        r"stopped mid-(?:run|batch)|mid-batch|abort(?:ed|ing)?|skip(?:ped|ping)?)\b",
        re.IGNORECASE,
    ),
)
_REASON_NOISE = re.compile(r"[^a-z0-9 ]+")

# The period/semicolon stop above does not catch a delivery-outcome clause
# APPENDED to the reason in the SAME sentence via a comma, e.g. "Aborted
# because quotes were stale, and email delivery failed" -- a wording the
# stated-not-labelled contract permits just as much as a new sentence. Left
# uncut, that whole clause becomes part of the signature while a
# corroborating copy that stops at "quotes were stale." (a plain period, the
# case the comment above already handles) does not, so the two spellings no
# longer match and _corroborates reads one retry as two distinct incidents
# (Codex review, PR #879). Truncate at the first such clause instead of
# widening the outer stop character class, which would also cut a reason
# that itself legitimately lists comma-separated causes.
_REASON_DELIVERY_CLAUSE = re.compile(
    r",\s*(?:and\s+)?(?:e-?mail|slack|notion|draft\w*|delivery|send\w*|resend\w*|retry\w*|message\w*)\b",
    re.IGNORECASE,
)

# The SAME channel vocabulary as _REASON_DELIVERY_CLAUSE, but without its
# leading comma anchor: used to recognize a channel-delivery-failure
# sentence a "because"/"due to"/"owing to" cue can belong to (see
# _reason_signature's cue_prefix screen), not to trim an appended clause.
_REASON_CUE_CHANNEL_MENTION = re.compile(
    r"\b(?:e-?mail|slack|notion|draft\w*|delivery|send\w*|resend\w*|retry\w*|message\w*)\b", re.IGNORECASE
)
# A generic failure verb, not scoped to any one channel's own wording
# (_SEND_FAILED/_EMAIL_FAILURE are email-specific -- "send failed",
# "email failed" -- and neither matches "Slack post failed" or "Notion
# sync failed", which say the delivery failed just as plainly with a
# different verb before it).
#
# "failed" alone missed two other ordinary channel-error verbs this
# corpus also uses: "errored" ("Slack post errored because the webhook
# stalled") and "was/were rejected" ("Message delivery was rejected
# because the webhook refused it") -- neither is a form of "fail" and
# both slipped through the same way "Slack post failed" originally did
# (Codex review: "Screen all channel-error verbs from reason
# extraction"). "rejected" here is scoped to this SAME narrow
# cue-prefix check (a channel word must already be present via
# _REASON_CUE_CHANNEL_MENTION), so it cannot collide with
# detect_verdict()'s own _REJECTED_ORDER_CONTEXT scoping for the ORDER
# rejection verdict -- these are two independent, differently-scoped
# checks over different text.
#
# "was/were blocked" is a third such outcome ("Slack post was blocked
# because the webhook refused it"), same shape as "was/were rejected"
# and scoped the same way -- a channel word must already be present in
# the same prefix (Codex review: "Exclude blocked channel failures
# from incident reasons").
_REASON_CUE_CHANNEL_FAILURE = re.compile(
    r"\bfail(?:ed|s|ing)?\b|\berror(?:ed|s|ing)?\b|\b(?:was|were)\s+(?:rejected|blocked)\b", re.IGNORECASE
)


def _reason_signature(record: AlertRecord) -> Optional[str]:
    """The record's stated reason, normalized, or None when it declares none.

    Normalized hard -- lowercased, punctuation stripped, whitespace collapsed,
    truncated -- because the two channels carry the same reason retyped, not the
    same bytes. The comparison this feeds only ever *separates* records, so the
    bias has to be toward calling two spellings equal.

    Every match is screened for a PRIOR-EVENT mention before being accepted,
    falling through to the next occurrence (and eventually the next, weaker
    pattern) rather than the first bare match: two same-day firings can each
    state a genuinely different CURRENT reason with "because" while both also
    narrate the same historical labelled reason ("Previous run Reason: budget
    exhausted") -- the labelled form is checked first for being the stronger
    signal, and an unscreened search on it would report that shared history
    as both records' own reason, corroborating two distinct incidents into
    one.

    Also screened for NEGATION at the cue itself ("Aborted not because
    quotes were stale; because budget was exhausted." denies the first
    cause and states the real one second) -- without it, the denied cause
    was accepted as the record's reason and could corroborate a same-day,
    same-slot firing whose real, different reason happened to match the
    denied one, collapsing two incidents into one (Codex review: "Skip
    negated reason matches during reconciliation"). Uses
    _PRIOR_EVENT_CLAUSE_SPLIT, not _CLAUSE_SPLIT: _CLAUSE_SPLIT splits ON
    "because" itself (needed elsewhere so a negation before it cannot reach
    an unrelated causal clause after it -- see _CLAUSE_SPLIT's own
    docstring), which here would sever the cue from the very "not" that
    precedes it in the same clause.
    """
    haystack = f"{record.subject}\n{record.body}"
    for pattern in _REASON_PATTERNS:
        for match in pattern.finditer(haystack):
            if _mention_has_prior_event(haystack, match.start(), 0):
                continue
            if _mention_is_negated(haystack, match.start(), 0, splitter=_PRIOR_EVENT_CLAUSE_SPLIT):
                continue
            # A labelled/causal cue can belong to an APPENDED delivery-
            # failure sentence rather than the incident's own reason: Gate
            # 9 has the executor append non-delivery evidence to the alert
            # file, so "Quotes were stale; aborted. Email send failed
            # because the provider timed out." has the file copy's
            # "because" narrate WHY THE EMAIL FAILED, not why the run
            # aborted -- and being checked ahead of the free-form fallback
            # below, it returned that instead of the shared "quotes were
            # stale" the plain email copy (with no delivery clause to
            # append) correctly falls through to, so the two copies of one
            # incident no longer corroborate and are double-counted (Codex
            # review: "Exclude delivery-failure causes from incident
            # reasons"). Skipped when the cue's own SENTENCE, up to the
            # cue, already narrates a channel send failure -- falls
            # through to the next occurrence/pattern, same as a negated or
            # historical mention.
            #
            # Not email-specific: every channel Gate 9 can append evidence
            # for -- Slack, Notion, a repo alert message -- narrates its
            # own delivery failure the same way, e.g. "Slack post failed
            # because the webhook timed out." An email-only screen missed
            # that shape entirely, since neither _EMAIL_MENTION nor the
            # email-specific failure patterns match "Slack post failed"
            # (Codex review: "Exclude every channel-failure cause from
            # incident reasons"). _REASON_CUE_CHANNEL_MENTION reuses the
            # SAME channel vocabulary _REASON_DELIVERY_CLAUSE already
            # trusts for this purpose, paired with a channel-agnostic
            # failure verb rather than email's own "send failed" wording.
            sentence_start, _ = _span_containing(haystack, match.start(), _SENTENCE_SPLIT)
            cue_prefix = haystack[sentence_start : match.start()]
            if _REASON_CUE_CHANNEL_MENTION.search(cue_prefix) and (
                _REASON_CUE_CHANNEL_FAILURE.search(cue_prefix)
                or _SEND_FAILED.search(cue_prefix)
                or _EMAIL_FAILURE.search(cue_prefix)
            ):
                continue
            captured = match.group(1)
            delivery_clause = _REASON_DELIVERY_CLAUSE.search(captured)
            if delivery_clause is not None:
                captured = captured[: delivery_clause.start()]
            text = _REASON_NOISE.sub(" ", captured.lower())
            normalized = " ".join(text.split())[:80]
            if normalized:
                return normalized
    return None


def _corroborates(draft: AlertRecord, alert_file: AlertRecord) -> bool:
    """True when these two records plausibly describe the SAME firing.

    (ET day, slot, gate) alone does not establish that. A slot can abort at one
    gate twice in a day -- a retry loop is exactly what this digest is meant to
    make visible -- and the two channels do not preserve the two firings
    symmetrically: Gate 9's alert filename is fixed per (day, slot, gate), so a
    retry OVERWRITES the first file, while both emails survive. Pairing on the
    tuple alone would then consume the earlier firing's draft as the later
    file's counterpart and collapse two real incidents into one.

    The pair must affirmatively corroborate. Two records that name different
    verdicts or reasons are distinct incidents, and an unparsed/missing reason
    cannot establish identity merely because the other tuple fields match.
    """
    if draft.verdict and alert_file.verdict and draft.verdict != alert_file.verdict:
        return False
    draft_reason = _reason_signature(draft)
    file_reason = _reason_signature(alert_file)
    # Absence is not corroboration. Gate 9 permits free-form reasons, so a
    # parser miss on both copies cannot safely mean that two otherwise similar
    # same-day firings are the same incident.
    if not draft_reason or not file_reason or draft_reason != file_reason:
        return False
    return True


_RETRY_EVIDENCE = re.compile(r"\b(?:retry|retried|re-?send|re-?sent)\b", re.IGNORECASE)
_EMAIL_EVIDENCE = re.compile(r"\b(?:e-?mail|mail)\b", re.IGNORECASE)
_DELIVERY_SUCCESS_EVIDENCE = re.compile(r"\b(?:sent|delivered|succeeded|successful(?:ly)?)\b", re.IGNORECASE)
# Codex review discussion_r3891196430 ("Keep comma-separated retry status
# together"): a bare comma is common WITHIN one compact retry-status clause
# ("Email retry, sent successfully") as much as it is between distinct
# clauses -- splitting on it unconditionally separated the retry/email terms
# from the success word into two clauses, neither of which alone satisfied
# the all-three-terms check below. Sentence punctuation and adversative
# conjunctions are unambiguous clause boundaries; a bare comma is not.
_RETRY_DELIVERY_CLAUSE_SPLIT = re.compile(
    r"[.!?;\n]|\b(?:but|however|while|whereas|yet)\b", re.IGNORECASE
)
# Codex review discussion_r3888105723 ("Scope follow-up delivery failures to
# email"): "message" alone is ambiguous -- "Message center notification was
# not delivered" reports the in-app channel, not email, but the old pattern
# matched "message ... not ... delivered" regardless. Excluding the specific
# compound "message center" (the in-app channel's own name in this corpus)
# keeps a bare "message" -- used elsewhere as a generic synonym for "email"
# -- matching, while no longer treating an explicitly different channel's
# failure as contradicting a genuinely successful email retry.
_DELIVERY_FAILURE_EVIDENCE = re.compile(
    r"\b(?:e-?mail|mail|message(?! center))\b[^.!?;\n]{0,80}"
    r"(?:\b(?:was|were|is|has been)\s+not\b|\bnever\b)[^.!?;\n]{0,20}\b(?:sent|delivered)\b"
    r"|\b(?:e-?mail|mail|message(?! center))\s+delivery\s+(?:failed|unsuccessful)\b",
    re.IGNORECASE,
)
# Codex review discussion_r3896981866 ("Reject conditional retry-success
# mentions"): "If the email retry succeeded, no draft will remain" names
# email/retry/succeeded in one clause, but frames the outcome as
# HYPOTHETICAL, not an assertion that it actually happened. A clause opening
# with "if" (optionally after leading connective words already stripped by
# the splitter) is a conditional, not delivery proof.
_CONDITIONAL_CLAUSE_CUE = re.compile(r"^\s*if\b", re.IGNORECASE)


def _same_delivery_attempt(draft: AlertRecord, message: AlertRecord) -> bool:
    """Whether transport identity or explicit prose proves a successful retry."""
    # Gmail transport identity is authoritative in both directions.  When
    # both records identify a thread, prose cannot turn two different threads
    # into the same delivery attempt.
    if draft.thread_id and message.thread_id and draft.thread_id != message.thread_id:
        return False
    text = f"{message.subject}\n{message.body}"
    # Success words are not delivery proof when the message also reports that
    # email ultimately failed.  In particular, the clause splitter below must
    # not discard a later adversative clause ("retry succeeded, but the
    # message was not delivered") and accept only the optimistic first half.
    #
    # Codex review discussion_r3895361799 ("Honor first-attempt qualifiers
    # before rejecting thread matches"): a SENT message's own existence is
    # direct proof email was ultimately delivered (the same reasoning
    # detect_channel_warnings already applies to a gmail_message record) --
    # so a failure phrase explicitly qualified as describing only the
    # FIRST/earlier attempt ("Email failed on the first attempt") must not
    # by itself reject an exact-thread retry pairing.
    if states_email_failure(text) and not (
        message.source == "gmail_message" and _message_failure_is_first_attempt_qualified(text)
    ):
        return False
    successes = list(_DELIVERY_SUCCESS_EVIDENCE.finditer(text))
    # Keep an earlier failed attempt compatible with a later successful retry,
    # but reject success prose that is subsequently contradicted by the final
    # delivery outcome.
    #
    # Codex review discussion_r3887928796 ("Ignore historical delivery
    # failures in retry proof"): a follow-up failure mention narrating an
    # OLDER, unrelated incident ("Email retry succeeded. The previous run's
    # email was not delivered") is not evidence THIS retry ultimately
    # failed -- states_email_failure() already screens exactly this kind of
    # historical mention with _mention_has_prior_event, but this raw
    # unscreened search did not, rejecting a genuinely successful retry
    # purely because the body also recounted an earlier failure.
    if successes and any(
        not _mention_has_prior_event(text, failure.start(), failure.end() - failure.start())
        for failure in _DELIVERY_FAILURE_EVIDENCE.finditer(text, successes[0].end())
    ):
        return False
    if draft.thread_id and draft.thread_id == message.thread_id:
        return True
    # A bare occurrence of "retry" is not transport evidence.  It may describe
    # an order retry, or expressly deny a send ("No retry was attempted").
    # Require one clause to affirmatively name the email, its retry, and a
    # successful delivery outcome; screen every evidentiary term for negation.
    for clause in _RETRY_DELIVERY_CLAUSE_SPLIT.split(text):
        if _CONDITIONAL_CLAUSE_CUE.search(clause):
            # "If the email retry succeeded, no draft will remain" names all
            # three terms but asserts nothing -- it is a hypothetical, not
            # delivery proof.
            continue
        patterns = (_RETRY_EVIDENCE, _EMAIL_EVIDENCE, _DELIVERY_SUCCESS_EVIDENCE)
        matches = [next(iter(pattern.finditer(clause)), None) for pattern in patterns]
        if all(matches) and all(
            not _mention_is_negated(clause, match.start(), match.end() - match.start())
            and not _mention_has_prior_event(clause, match.start(), match.end() - match.start())
            for match in matches
            if match is not None
        ):
            return True
    return False


def _reconcile_drafts_and_messages(
    drafts: Sequence[AlertRecord], messages: Sequence[AlertRecord]
) -> List[AlertRecord]:
    """Reconcile a failed-send draft with its own later successful retry.

    An initial send that fails leaves a Gmail draft (with
    ``EMAIL_DRAFT_NOT_DELIVERED``); when the retry succeeds, that SAME event
    ALSO survives as a sent message. Left unreconciled, ``--drafts`` and
    ``--messages`` supplied together would count one incident twice AND keep
    the stale draft's non-delivery warning active despite the successful
    retry -- exactly the double-count and stale-warning problems
    :func:`merge_cross_source` already solves for drafts vs. alert files,
    applied here to the two Gmail sources themselves. Matched the same way:
    (ET day, slot, gate), corroboration-checked so two genuinely different
    same-slot-day firings still both count.

    Unlike :func:`merge_cross_source`, the counterpart's ``EMAIL_DRAFT_NOT_
    DELIVERED``/undelivered-email warning is deliberately dropped rather than
    folded in: the message IS the email actually being delivered, direct
    proof that resolves exactly what that warning claims. Every other
    warning code on either copy still survives the merge, the same as
    before.

    A candidate draft only pairs when it is STRICTLY EARLIER than the message
    and the Gmail thread ID or explicit retry/resend wording ties the two
    transports together. "The retry succeeded" is not established merely by
    matching (day, slot, gate) and not contradicting on verdict/reason. A
    slot that fires the same gate for the same reason twice in one day --
    once cleanly (a message) and once into a failed send (a later draft) --
    would otherwise let the EARLIER, successful message pair with the LATER,
    still-failed draft purely because nothing else distinguishes them,
    clearing a warning that draft's own later failure earned.

    Messages are processed in TIMESTAMP order, not export order, and each is
    paired with an exact nonempty Gmail thread match first, then the NEAREST
    (latest) still-unpaired qualifying draft when only prose proves the retry,
    rather than the first one encountered. Gmail's own list APIs commonly return
    results newest-first, and this function has no other reason to assume
    either input arrives chronologically. With two retry pairs in one day --
    drafts at t1/t3, their successful retries at t2/t4 -- a newest-first
    ``messages`` array visits t4 before t2; naive first-match-in-input-order
    pairing then consumed the OLDEST compatible draft (t1) for t4, so t2
    could no longer precede t3 and both records were left stranded --
    including a stale EMAIL_DRAFT_NOT_DELIVERED on t3's draft despite t4
    being its own actual, later, successful retry (Codex review: "Pair
    retries independently of export ordering"). Processing in timestamp
    order and taking the nearest equally strong qualifying predecessor pairs t2
    with t1 and t4 with t3 correctly, regardless of the order either input
    array was given in.
    """
    by_event: Dict[Tuple[date, str, str], List[AlertRecord]] = defaultdict(list)
    for record in drafts:
        identity = _event_identity(record)
        if identity is not None:
            by_event[identity].append(record)

    merged: List[AlertRecord] = list(drafts)
    for record in sorted(messages, key=lambda message: message.timestamp_utc):
        identity = _event_identity(record)
        candidates = by_event.get(identity) if identity is not None else None
        counterpart = None
        if candidates:
            best_index = None
            best_rank = None
            for index, candidate in enumerate(candidates):
                # An exact, nonempty Gmail thread match IS the transport
                # identity proving these two records describe the same
                # delivery attempt -- stronger evidence than either the
                # strict-ordering timestamp heuristic or _corroborates'
                # content-based reason matching, both of which exist only to
                # approximate that identity when thread IDs are unavailable.
                #
                # Codex review discussion_r3888105723 ("Let shared thread
                # identity bypass reason parsing"): requiring _corroborates
                # on top of an exact thread match rejected a genuinely
                # matched pair whenever both bodies used a free-form reason
                # the parser could not read (returning None on both sides),
                # even though the shared thread already proves identity.
                #
                # Codex review discussion_r3891196433 ("Allow exact-thread
                # retries at equal timestamps"): the same reasoning applies
                # to the strict `>=` ordering check -- an export can assign a
                # failed draft and its immediately successful retry the same
                # timestamp, and an exact thread match already proves which
                # one is the retry.
                exact_thread = bool(
                    candidate.thread_id and candidate.thread_id == record.thread_id
                )
                timestamp_ok = candidate.timestamp_utc < record.timestamp_utc or (
                    exact_thread and candidate.timestamp_utc == record.timestamp_utc
                )
                if (
                    not timestamp_ok
                    or not _same_delivery_attempt(candidate, record)
                    or (not exact_thread and not _corroborates(candidate, record))
                ):
                    continue
                # Gmail's transport identity is stronger evidence than retry
                # prose. Prefer an exact nonempty thread match even when an
                # unrelated, unthreaded draft is the nearer predecessor.
                rank = (
                    bool(candidate.thread_id and candidate.thread_id == record.thread_id),
                    candidate.timestamp_utc,
                )
                if best_rank is None or rank > best_rank:
                    best_index = index
                    best_rank = rank
            if best_index is not None:
                counterpart = candidates.pop(best_index)
        if counterpart is None:
            merged.append(record)
            continue
        combined = list(record.warnings)
        seen_codes = {code for code, _ in combined}
        for code, detail in counterpart.warnings:
            if code == _DRAFT_WARNING[0]:
                continue
            if code not in seen_codes:
                combined.append((code, detail))
                seen_codes.add(code)
        merged[merged.index(counterpart)] = replace(
            record,
            warnings=tuple(combined),
            verdict=record.verdict or counterpart.verdict,
            # The consumed draft's OWN pre-merge ledger identity, carried
            # forward as an alias -- see AlertRecord.prior_keys. Without it,
            # a draft ledgered as new on an earlier run (before its
            # successful retry showed up as a message) reappears as "new"
            # again once this reconciliation relabels it under the
            # message's own identity, even though nothing about the
            # underlying event changed (Codex review: "Keep ledger identity
            # stable across source reconciliation").
            # The alias is computed from the counterpart's own identity
            # fields but THIS FINAL combined warning set, not the
            # counterpart's bare pre-merge `.key` -- otherwise a run where
            # the OTHER side contributes a genuinely NEW warning code would
            # alias to the counterpart's unchanged key, wrongly suppressing
            # that new discovery as "already seen" (the exact failure mode
            # `key`'s own docstring says this identity must never produce).
            prior_keys=(
                *record.prior_keys,
                *counterpart.prior_keys,
                replace(counterpart, warnings=tuple(combined)).key,
            ),
        )
    return merged


def merge_cross_source(
    drafts: Sequence[AlertRecord], files: Sequence[AlertRecord]
) -> List[AlertRecord]:
    """Combine the two sources, collapsing records that describe the same event.

    Using both sources together is the *expected* configuration once delivery is
    fixed, and then a single abort survives twice -- once as an email and once as
    a repo alert file. Counting both would inflate every gate frequency, slot
    count, and warning total exactly when the alerting is working best.

    Records are matched on (ET day, slot, gate) AND on not contradicting each
    other -- see :func:`_corroborates`, which keeps two same-day firings of one
    gate from collapsing into one. The file copy wins, because it is the durable
    channel and its slot/gate come from the executor's own filename rather than
    from prose; the draft's warnings are folded in so the delivery evidence from
    either copy survives the merge.

    Deduplication is strictly *across* sources. Two drafts for one (day, slot,
    gate) stay two records: a slot that alerted twice on the same gate really
    did alert twice, and collapsing that would hide a retry loop. A record
    missing a slot or gate cannot be matched at all and is always kept.
    """
    # A queue per identity, not a single record: two firings of one (day, slot,
    # gate) can survive as two drafts AND two files, and keeping only one draft
    # counterpart would pair the first file, then leave the second file and the
    # second draft both standing -- turning two real firings into three records.
    # Each file consumes at most one matching draft, in order.
    by_event: Dict[Tuple[date, str, str], deque] = defaultdict(deque)
    for record in drafts:
        identity = _event_identity(record)
        if identity is not None:
            by_event[identity].append(record)

    merged: List[AlertRecord] = list(drafts)
    for record in files:
        identity = _event_identity(record)
        # popleft, so the draft is consumed by the file that pairs with it. The
        # recursive scan can yield more files than drafts for one event (the same
        # basename under two stream directories); once the queue is empty those
        # extra files simply append, matching the stated policy that duplicates
        # within one source are retained. Leaving a consumed draft reachable
        # would send the next file looking for a record already replaced in
        # `merged` -- a ValueError from list.index that kills the whole digest.
        # The first draft in the queue that does not CONTRADICT this file, not
        # simply the first draft in it: two firings of one (day, slot, gate) can
        # describe different incidents, and pairing them on the tuple alone
        # collapses two real aborts into one. A queue entry the file contradicts
        # stays put, for a later file to claim or to stand on its own.
        #
        # A `gmail_message` candidate OLDER than this file's own event time
        # (_file_event_utc) is rejected as a counterpart outright, not just
        # excluded from clearing the file's warning: not contradicting on
        # verdict/reason is not proof of being the SAME firing when the
        # message provably predates the file it would be paired with. Gate
        # 9's alert filename is fixed per (day, slot, gate), so an earlier,
        # successful firing and a LATER, genuinely failed retry of the same
        # slot/gate/reason share an identity and corroborate on content
        # alone -- pairing them still reduced two real firings to one
        # record, undercounting gate frequency, coverage, and repeat-loop
        # evidence even after the file's own warning was correctly kept
        # (Codex review: "Keep earlier messages separate from later file
        # firings"). Scoped to gmail_message only, the same as the existing
        # timing gate below: a draft carries no comparable "this specific
        # firing was proven resolved" claim to begin with.
        queue = by_event.get(identity) if identity is not None else None
        counterpart = None
        if queue:
            file_event_utc = _file_event_utc(record)
            for position, candidate in enumerate(queue):
                if candidate.source == "gmail_message" and candidate.timestamp_utc < file_event_utc:
                    continue
                if _corroborates(candidate, record):
                    counterpart = candidate
                    del queue[position]
                    break
        if counterpart is None:
            merged.append(record)
            continue
        # Union the warnings, order-stable, so neither copy's delivery evidence
        # is lost when the two collapse into one.
        # Union by warning CODE, not by (code, detail) tuple. A draft and its
        # matching file can both report undelivered email with different wording
        # -- one from the draft's own existence, one from the file's stated
        # failure -- and a tuple-level union would report the same failure twice
        # for one alert.
        combined = list(counterpart.warnings)
        seen_codes = {code for code, _ in combined}
        # A `gmail_message` counterpart is direct proof the email was sent --
        # potentially the very retry the file's own body narrates as a
        # failure (an initial attempt failed, was recorded in the file, and
        # then succeeded on retry). Folding the file's non-delivery warning
        # in over that proof would restore a claim the message already
        # resolves, same reasoning as _reconcile_drafts_and_messages.
        #
        # That proof only holds when the message is NOT older than the
        # firing it is being used to clear. Gate 9's alert filename is fixed
        # per (day, slot, gate), so a retry loop OVERWRITES the file -- the
        # file record here can describe a SECOND, LATER firing's own genuine
        # failure, while `_corroborates` (which does not check timestamps)
        # still pairs it with an EARLIER firing's unrelated successful
        # send. An earlier message is not evidence that a later failure was
        # ever resolved (Codex review: "Keep earlier sent alerts from
        # clearing later file failures").
        #
        # Compared against the file's own actual event time (_file_event_utc:
        # its real mtime when known, else the slot's scheduled trigger as a
        # coarser fallback), not `record.timestamp_utc` directly: a
        # contract-named file is stamped at a synthetic ET noon regardless
        # of its actual slot, so comparing straight against it misjudged
        # every pre-noon slot's genuine early-morning retry as "older than
        # the file" (Codex review: "Compare retries against the file's
        # actual event time"). The slot's nominal trigger alone is still too
        # coarse, though -- every firing of one (day, slot) shares it, so it
        # cannot tell an early SUCCESSFUL firing from a later same-slot
        # retry that overwrote the file with a genuine failure -- hence
        # preferring the file's own mtime, the real ground truth for which
        # firing is the one still on disk (Codex review: "Compare delivery
        # proof with the overwritten firing"). `record.slot` is guaranteed
        # set here -- `identity` (used to find this `counterpart`) requires
        # it.
        file_warnings = record.warnings
        if counterpart.source == "gmail_message" and counterpart.timestamp_utc >= _file_event_utc(record):
            file_warnings = tuple(w for w in file_warnings if w[0] != _DRAFT_WARNING[0])
        for code, detail in file_warnings:
            if code not in seen_codes:
                combined.append((code, detail))
                seen_codes.add(code)
        # The file wins the merge, but not at the cost of a verdict it never
        # stated. Gate 9 lets the verdict live in either copy's wording -- an
        # email can carry it only in the subject while its file counterpart
        # states the slot, gate, and reason but omits verdict wording entirely
        # -- and `_corroborates` already confirmed the two do not contradict
        # each other. Discarding the counterpart's known verdict in that case
        # would count a detected ABORT/REJECTED/etc. as unclassified merely
        # because it happened to be a file, not an email, that carried the
        # gate identity.
        merged[merged.index(counterpart)] = replace(
            record,
            warnings=tuple(combined),
            verdict=record.verdict or counterpart.verdict,
            # The consumed draft/message's OWN pre-merge ledger identity,
            # carried forward as an alias -- see AlertRecord.prior_keys.
            # Without it, an alert ledgered as new (as a draft or message,
            # before its alert-file counterpart showed up) reappears as
            # "new" again once this merge relabels it under the file's own
            # identity, even though the underlying event and its warnings
            # did not change (Codex review: "Keep ledger identity stable
            # across source reconciliation"). Deliberately not
            # `record`'s (the file's) own PRE-merge key: that identity
            # SHOULD still register as new when the merge folds in a
            # genuinely new warning -- see `key`'s own docstring.
            # The alias is computed from the counterpart's own identity
            # fields but THIS FINAL combined warning set, not the
            # counterpart's bare pre-merge `.key` -- otherwise a run where
            # the OTHER side contributes a genuinely NEW warning code would
            # alias to the counterpart's unchanged key, wrongly suppressing
            # that new discovery as "already seen" (the exact failure mode
            # `key`'s own docstring says this identity must never produce).
            prior_keys=(
                *record.prior_keys,
                *counterpart.prior_keys,
                replace(counterpart, warnings=tuple(combined)).key,
            ),
        )
    return merged


def collect_records(
    *,
    drafts_path: Optional[Path],
    alerts_dir: Optional[Path],
    read_stdin: bool,
    messages_path: Optional[Path] = None,
    keep_unmatched: bool = False,
) -> Tuple[List[AlertRecord], int, int]:
    """Load records from every requested source.

    Returns ``(records, skipped, non_alerts)`` -- drafts with no usable
    timestamp, and drafts that carried no executor-alert signature at all.
    """
    records: List[AlertRecord] = []
    skipped = 0

    payloads: List[Mapping[str, Any]] = []
    if read_stdin:
        raw = sys.stdin.read().strip()
        if not raw:
            # An empty stream is not an empty export. The connector's own
            # zero-results form is `{}`, so nothing at all means the upstream
            # export command produced no output -- i.e. it failed. Treating that
            # as "no alerts" would print a clean digest for a broken ingestion
            # path, which is the same silent-success failure this whole module
            # exists to catch.
            raise ValueError("stdin was empty; expected a JSON export (use '{}' for no drafts)")
        payloads.append(json.loads(raw))
    if drafts_path is not None:
        payloads.append(json.loads(drafts_path.read_bytes()))

    draft_records: List[AlertRecord] = []
    for payload in payloads:
        if not isinstance(payload, Mapping):
            raise ValueError("draft export must be a JSON object with a 'drafts' array")
        offered = payload.get("drafts") or []
        loaded = load_gmail_drafts(payload)
        # Anything the loader declined was missing a timestamp; report the count
        # rather than letting drafts vanish silently between input and digest.
        # But MULTIPLE items ALL declined -- e.g. a schema change that renamed
        # the date field on every record -- is not "isolated malformed items"
        # any more; it is the whole ingestion path silently producing nothing.
        # Left as a plain `skipped` count, that read exactly like a clean
        # zero-drafts digest, and --fail-on-warnings could not catch it
        # either, since a skip is not a warning. Raised here the same way the
        # empty-stdin case just above is -- an input/health failure, not a
        # report of zero drafts (Codex review: "Fail when a nonempty export
        # yields no usable records"). Requires MORE than one offered item,
        # not just one: a single malformed draft is exactly the "isolated
        # item" case this function already tolerates by design (see
        # test_collect_records_counts_drafts_it_had_to_skip,
        # test_a_date_only_export_item_is_skipped_not_misfiled) -- with only
        # one item, "all of them failed" and "an isolated one failed" are the
        # same observation, and only a genuinely SYSTEMIC failure (several
        # items sharing the same broken shape) is distinguishable from that.
        if len(offered) > 1 and not loaded:
            raise ValueError(
                f"draft export offered {len(offered)} item(s) but none had a usable "
                "timestamp -- this looks like a broken export or schema change, "
                "not zero drafts"
            )
        skipped += max(0, len(offered) - len(loaded))
        draft_records.extend(loaded)

    message_records: List[AlertRecord] = []
    if messages_path is not None:
        sent_payload = json.loads(messages_path.read_bytes())
        if not isinstance(sent_payload, Mapping):
            raise ValueError("message export must be a JSON object with a 'messages' array")
        offered_sent = sent_payload.get("messages") or []
        loaded_sent = load_gmail_messages(sent_payload)
        # Same "entirely unusable nonempty source" check as the drafts loop
        # above, for the identical reason -- requiring more than one offered
        # item so a single malformed message is still tolerated.
        if len(offered_sent) > 1 and not loaded_sent:
            raise ValueError(
                f"message export offered {len(offered_sent)} item(s) but none had a "
                "usable timestamp -- this looks like a broken export or schema "
                "change, not zero messages"
            )
        skipped += max(0, len(offered_sent) - len(loaded_sent))
        message_records.extend(loaded_sent)

    # Reconciled against EACH OTHER before anything else touches them: a
    # failed-send draft and its own later successful retry are the same
    # incident surviving under two different Gmail sources, and merging them
    # here (rather than treating drafts as just another flavor of "records")
    # is what lets a resolved non-delivery warning actually clear.
    records.extend(_reconcile_drafts_and_messages(draft_records, message_records))

    # Set aside drafts that are not executor alerts at all. The CLI takes a raw
    # list_drafts export, so an ordinary personal draft can arrive in it; left
    # in, it inflates the unclassified counts and -- carrying
    # EMAIL_DRAFT_NOT_DELIVERED like every draft -- would pin
    # --fail-on-warnings red permanently. The count is always reported, and
    # --keep-unmatched restores them for anyone auditing the filter itself.
    non_alerts = 0
    if not keep_unmatched:
        kept = [record for record in records if looks_like_executor_alert(record)]
        non_alerts = len(records) - len(kept)
        records = kept

    if alerts_dir is not None:
        if not alerts_dir.is_dir():
            # Same reasoning as the empty-stdin rejection: a misspelled path, or
            # an artifact volume that was not mounted, would otherwise return an
            # empty corpus indistinguishable from a genuinely empty directory,
            # and the CLI would print a clean digest for a broken ingestion path.
            raise ValueError(f"--alerts-dir is not a directory: {alerts_dir}")
        records = merge_cross_source(records, load_alert_files(alerts_dir))

    return records, skipped, non_alerts


def filter_records(
    records: Iterable[AlertRecord],
    *,
    since: Optional[date],
    until: Optional[date],
    slot: Optional[str],
) -> List[AlertRecord]:
    """Apply the CLI's window and slot filters, on ET days."""
    selected = []
    for record in records:
        day = record.et_date
        if since is not None and day < since:
            continue
        if until is not None and day > until:
            continue
        if slot is not None and record.slot != slot:
            continue
        selected.append(record)
    return selected


def _parse_day(value: str) -> date:
    # ⚡ Bolt optimization: Use string slicing to bypass slow datetime.strptime overhead
    if not value or len(value) != 10 or value[4] != '-' or value[7] != '-':
        raise ValueError(f"time data {value!r} does not match format '%Y-%m-%d'")
    return date(int(value[:4]), int(value[5:7]), int(value[8:]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.alert_digest",
        description=(
            "Digest the executor gate alerts that accumulate as unsent Gmail "
            "drafts and as Gate 9 repo alert files."
        ),
    )
    parser.add_argument(
        "--drafts",
        type=Path,
        help="JSON file holding a Gmail list_drafts export ({'drafts': [...]}).",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read the same JSON export from stdin (for piping straight from the connector).",
    )
    parser.add_argument(
        "--alerts-dir",
        type=Path,
        help="Directory of Gate 9 repo alert files (<YYYY-MM-DD>-<slot>-<gate>.txt).",
    )
    parser.add_argument("--since", type=_parse_day, metavar="YYYY-MM-DD", help="Earliest ET day to include.")
    parser.add_argument(
        "--until",
        type=_parse_day,
        metavar="YYYY-MM-DD",
        help=(
            "Latest ET day to include. Defaults to --as-of (today in ET), NOT to the "
            "last day anything alerted -- otherwise a roster that went quiet a week ago "
            "produces a report ending on its last alert, hiding the silence entirely."
        ),
    )
    parser.add_argument(
        "--as-of",
        type=_parse_day,
        metavar="YYYY-MM-DD",
        help="Treat this as the reporting day (default: today in ET). Mainly for tests.",
    )
    parser.add_argument("--slot", choices=SLOTS, help="Restrict the digest to one slot.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of the text report.")
    parser.add_argument(
        "--ledger",
        action="store_true",
        help="Diff against (and update) the seen-alert ledger under stream_root('alert_digest').",
    )
    parser.add_argument(
        "--ledger-root",
        help="Override the ledger directory (defaults to stream_root('alert_digest')).",
    )
    parser.add_argument(
        "--messages",
        type=Path,
        help=(
            "JSON file holding an export of SENT alert emails ({'messages': [...]}). "
            "Needed once the corrected Gate 9 prompts are deployed: a sent alert never "
            "appears in a drafts export, so a drafts-only digest goes blind to it."
        ),
    )
    parser.add_argument(
        "--keep-unmatched",
        action="store_true",
        help=(
            "Keep records that carry no executor-alert signature (no slot and no "
            "gate). They are set aside by default so an unrelated personal draft "
            "cannot pin --fail-on-warnings red; use this to audit what was filtered."
        ),
    )
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help=(
            "Exit 1 if any delivery warning fired -- for wiring into a scheduled check. "
            "With --ledger, only warnings on alerts new since the last digest count."
        ),
    )
    return parser


def _warning_gate_trips(digest: Digest, new_keys: Optional[Sequence[str]]) -> bool:
    """Whether ``--fail-on-warnings`` should exit non-zero.

    Without a ledger there is no notion of "new", so any warning in the window
    trips the gate. With one, only warnings on alerts this digest had not seen
    before count. The report still spans the whole corpus either way -- streaks
    and coverage need the history -- but a scheduled check that stayed red
    forever on a backlog it has already reported is a broken check: it would
    never go green again, so it would stop being read, and the next genuinely
    undeliverable alert would land in a signal nobody trusts.
    """
    if new_keys is None:
        return bool(digest.warnings)
    fresh = set(new_keys)
    return any(record.warnings for record in digest.records if record.key in fresh)


def _last_roster_slot_time() -> time:
    """The clock time (ET) the final roster slot is scheduled to fire.

    Derived from ``SLOTS`` rather than hard-coded, so a roster change (adding
    or reordering slots) keeps this in sync automatically. ``SLOTS`` entries
    are four-digit "HHMM" strings; the roster is not considered complete for
    a day until this time has passed.
    """
    last = SLOTS[-1]
    return time(hour=int(last[:2]), minute=int(last[2:]))


# Every roster slot's own Gate 1 "TIME SANITY" window extends 30 minutes past
# its nominal trigger time -- verified against all seven
# dca-executor-prompts/*.txt files: "9:30-10:00 AM ET" for the 0935 slot,
# "10:30-11:00 AM" for 1030, ... "3:30-4:00 PM ET" for 1530, each exactly 30
# minutes past its own nominal time. An executor may still legitimately fire
# and alert anywhere in that window, so a slot is not provably done until the
# window closes, not merely once its nominal trigger time has passed.
_GATE1_WINDOW_MINUTES = 30


def _slot_window_close(slot: str) -> time:
    """The clock time (ET) a single roster slot's own Gate 1 window closes."""
    total_minutes = int(slot[:2]) * 60 + int(slot[2:]) + _GATE1_WINDOW_MINUTES
    return time(hour=total_minutes // 60, minute=total_minutes % 60)


def _roster_completion_time(day: date) -> time:
    """The clock time (ET) the roster is actually finished firing for ``day``.

    Normally the LAST slot's own Gate 1 window close (see
    ``_slot_window_close`` -- 16:00 ET for the ordinary 1530 slot, not the
    15:30 nominal trigger time ``_last_roster_slot_time()`` reports: the
    executor may still legitimately fire and alert anywhere inside that
    window). On a configured ``early_close_1300`` day the market closes at
    1:00 PM ET and the 13:30, 14:30, and 15:30 slots are required to end
    silently (see ``_slot_runs_after_early_close``) -- so the last slot that
    can actually produce an alert there is 12:30, whose own window closes at
    13:00. Waiting for the normal completion time on such a day would keep
    an already-finished, genuinely roster-silent early close out of the
    calendar for three extra hours.
    """
    if day in early_close_days():
        last = max(slot for slot in SLOTS if not _slot_runs_after_early_close(slot))
    else:
        last = SLOTS[-1]
    return _slot_window_close(last)


def _calendar_window_end(
    window_end: date,
    *,
    until_given: bool,
    as_of_given: bool,
    now_et: datetime,
) -> date:
    """The end date to use for the roster-silence CALENDAR only.

    Defaulting the implicit report window to "today" makes today enter
    ``expected_roster_days`` -- and can label it ``roster_silent`` -- before
    its own slots have had a chance to fire. The roster's actual completion
    time for the day (see ``_roster_completion_time`` -- 15:30 ET normally,
    12:30 ET on a configured early close) has to pass first; a normal
    morning run with no alerts yet would otherwise report a scheduler-wide
    outage that has not actually happened.

    Only applies when BOTH ``--until`` and ``--as-of`` were omitted: either
    one means the caller asked for that exact day, silence included, and is
    never second-guessed here. This also only ever narrows the CALENDAR span
    passed to ``build_digest``, not the record-filtering window -- an alert
    that genuinely fired today still reaches the report through
    ``digest.days``'s union with the observed and coverage sets.
    """
    if until_given or as_of_given or window_end != now_et.date():
        return window_end
    if now_et.time() < _roster_completion_time(window_end):
        return window_end - timedelta(days=1)
    return window_end


def _incomplete_roster_days(window_end: date, calendar_end: date) -> Set[date]:
    """The day(s) ``_calendar_window_end()`` excluded from the calendar purely
    because the roster had not finished yet -- see ``Digest.incomplete_days``
    / ``_classify_coverage_day``.

    Restricted to days that were themselves actually EXPECTED to run
    (``expected_roster_days(d, d)`` non-empty: a trading day inside the
    approved window). ``_calendar_window_end()`` clamps purely on time of
    day, with no notion of weekends, holidays, or the approved window's own
    bounds, so an off-schedule alert observed on a Saturday or a day outside
    the approved window must still classify as ``not_a_trading_day``/
    ``outside_approved_trading_window``, never ``incomplete_roster_day``.
    """
    return {d for d in (window_end,) if d > calendar_end and expected_roster_days(d, d)}


def _resolved_window_start(observed: Set[date], window_end: date) -> Optional[date]:
    """The lower bound of the report when ``--since`` was not given.

    ``observed`` MUST be the UNFILTERED corpus's own dates, not a set already
    narrowed to ``[.., window_end]``. Normally the answer is just the earliest
    day anything alerted -- but if that is derived from an already-windowed set,
    a corpus that starts entirely AFTER ``window_end`` (an explicit ``--until``
    or an earlier ``--as-of`` requested against a corpus that starts later) looks
    identical to a genuinely empty one: both windowed sets are empty. The two are
    not the same report. A corpus beginning 2026-08-26 read with
    ``--until 2026-08-20`` has a real, later, implicit start -- it is just later
    than what was asked for -- and returning it here (even past ``window_end``)
    lets the caller's own window math render the correct empty span, rather than
    this function silently substituting the approved trading window's start and
    reporting weeks of fabricated roster-silent days that never had a chance to
    alert in the first place.

    The approved-window fallback is reserved for a corpus with NO alerts at
    all -- the state this tool exists to make visible, a scheduler that has
    stopped entirely -- where deriving the bound from the corpus leaves no bound
    at all, and the digest would otherwise print no calendar, no coverage, and
    no roster-silent dates for the worst outcome it can report.
    ``expected_roster_days`` clamps the far end anyway.

    ``None`` only when the corpus is empty AND the window config is unreadable,
    where there is genuinely no lower bound to be had.
    """
    if observed:
        return min(observed)
    window = approved_trading_window()
    if window is None:
        return None
    start = window[0]
    return start if start <= window_end else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # An absent --until means "up to now", NOT "up to the last thing that
    # alerted". Ending the calendar at the final observed alert is precisely how
    # a roster that went quiet a week ago produces a report that stops on its
    # last good day and shows no silence at all -- the outage this tool exists
    # to surface, hidden by the report's own bounds.
    as_of = args.as_of or datetime.now(tz=ET).date()
    window_end = args.until or as_of

    if args.since and args.since > window_end:
        # Validated against the RESOLVED end, not just an explicit --until. A
        # window is equally inverted when --until is omitted and --as-of lands
        # before --since (a stale scheduled window replayed against an earlier
        # reporting day), and either way the filters drop every record and print
        # a clean zero-alert digest -- indistinguishable from a successful empty
        # run. Naming which bound resolved it keeps the error actionable.
        bound = f"--until ({args.until})" if args.until else f"the reporting day ({window_end})"
        print(
            f"error: --since ({args.since}) is later than {bound}",
            file=sys.stderr,
        )
        return 2

    if args.drafts and args.stdin:
        # Both name the same kind of export, and duplicates within the draft
        # source are deliberately retained, so piping and naming the same file
        # would double every count with nothing in the report to say so.
        print(
            "error: --drafts and --stdin are alternative forms of the same input; "
            "pass only one",
            file=sys.stderr,
        )
        return 2

    if not (args.drafts or args.stdin or args.alerts_dir or args.messages):
        # Fail closed and say what is missing, rather than printing an empty
        # digest that reads exactly like "no alerts, all clear".
        print(
            "error: give at least one source "
            "(--drafts, --stdin, --messages, or --alerts-dir)",
            file=sys.stderr,
        )
        return 2

    try:
        records, skipped, non_alerts = collect_records(
            drafts_path=args.drafts,
            alerts_dir=args.alerts_dir,
            read_stdin=args.stdin,
            messages_path=args.messages,
            keep_unmatched=args.keep_unmatched,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not read alerts: {exc}", file=sys.stderr)
        return 2

    # The UNFILTERED corpus's own earliest day, kept separate from
    # `roster_observed` below. `_resolved_window_start` needs to see alerts that
    # fall AFTER `window_end` to detect that case at all -- a corpus already
    # narrowed to the window can never reveal that its true start lies beyond it.
    #
    # Built only from records that actually look like executor alerts, same
    # as `roster_observed`/`roster_coverage` below: a --keep-unmatched
    # personal draft dated well before (or after) the real corpus would
    # otherwise shift the resolved window start to hide, or fabricate, days
    # of roster-silence coverage that have nothing to do with real alerts.
    corpus_observed = {record.et_date for record in records if looks_like_executor_alert(record)}

    # Window first, slot second, with the whole roster's days captured in between:
    # `--slot` narrows what is *counted*, but roster silence is a fact about every
    # slot and has to be measured before that narrowing. `window_end` was resolved
    # and validated above, before any input was read.
    windowed = filter_records(records, since=args.since, until=window_end, slot=None)
    # --keep-unmatched retains audit-only records (drafts with no executor-
    # alert signature) in `windowed` so they show up in totals for anyone
    # auditing the filter -- but a personal draft like "Lunch at midday"
    # still parses to a slot via the weak stream-word signal, and letting it
    # stand as roster evidence would mask a genuinely roster-silent day.
    # Roster observation and coverage are therefore built only from records
    # that actually look like executor alerts; this is a no-op without
    # --keep-unmatched, since collect_records already filtered to those.
    matched = [record for record in windowed if looks_like_executor_alert(record)]
    roster_observed = {record.et_date for record in matched}
    # Coverage is computed over the whole roster; --slot narrows the counts but
    # the matrix still prints all seven columns, so building it from the filtered
    # records alone would render six real slots as silent dots.
    roster_coverage = _coverage_map(matched)
    records = filter_records(windowed, since=None, until=None, slot=args.slot)
    # The calendar spans the ROSTER's window, not the selected slot's. Under
    # `--slot 1130`, bounding the report by that slot's own first and last alert
    # would hide the leading and trailing stretches where it went quiet -- which
    # is the single thing someone filtering to one slot is most likely looking
    # for. A slot that never alerted at all would otherwise render no calendar.
    # The CALENDAR end may be clamped to exclude an unfinished today (see
    # `_calendar_window_end`); the record-filtering `window_end` above is left
    # untouched, so this affects only which days render as ROSTER-SILENT, not
    # which alerts are counted.
    calendar_end = _calendar_window_end(
        window_end,
        until_given=args.until is not None,
        as_of_given=args.as_of is not None,
        now_et=datetime.now(tz=ET),
    )
    incomplete_days = _incomplete_roster_days(window_end, calendar_end)
    digest = build_digest(
        records,
        skipped=skipped,
        window_start=args.since or _resolved_window_start(corpus_observed, window_end),
        window_end=calendar_end,
        roster_observed=roster_observed,
        roster_coverage=roster_coverage,
        incomplete_days=incomplete_days,
    )
    digest.non_alerts = non_alerts

    new_keys: Optional[List[str]] = None
    seen: Set[str] = set()
    current: List[str] = []
    if args.ledger:
        path = ledger_path(args.ledger_root)
        seen = set(load_ledger(path).get("seen", []))
        # Every alias key, not just the surviving current one, is persisted:
        # a merged record's `prior_keys` is the identity its now-absorbed
        # counterpart (a draft, say) was ledgered under on an earlier run.
        # Saving only `record.key` loses that alias, so if the counterpart
        # that produced the CURRENT key later disappears -- the file is
        # cleaned up, or that source is omitted from a later run -- the
        # surviving record reverts to standing alone under its own
        # (unsaved) key and is reported as new again, even though it is the
        # same event already ledgered under its alias (Codex review:
        # "Persist merged records' alias keys in the ledger").
        current = [key for record in digest.records for key in (record.key, *record.prior_keys)]
        # A record already seen under any of its `prior_keys` -- an identity
        # a cross-source merge relabeled it away from -- is not new either,
        # even though its OWN current key never appeared in `seen` (Codex
        # review: "Keep ledger identity stable across source
        # reconciliation"). Checking `record.key` first, before consulting
        # `prior_keys`, is not just an optimization: a record whose current
        # key already matches `seen` is unambiguously not new regardless of
        # what its aliases say.
        new_keys = [
            record.key
            for record in digest.records
            if record.key not in seen and not any(alias in seen for alias in record.prior_keys)
        ]

    output = render_json(digest, new_keys=new_keys) if args.json else render_text(digest, new_keys=new_keys)
    sys.stdout.write(output)
    sys.stdout.flush()

    # The ledger is committed only once the report has actually reached stdout.
    # Recording these alerts as reported before the write would mean a closed
    # pipe or a failed downstream consumer loses the report while still marking
    # its warnings seen -- and the retry would then come back green.
    if args.ledger:
        save_ledger(ledger_path(args.ledger_root), seen.union(current))

    if args.fail_on_warnings and _warning_gate_trips(digest, new_keys):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
