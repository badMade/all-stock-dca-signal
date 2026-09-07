# Alert digest: tracking the executor alerts that never got delivered

`scripts/alert_digest.py` turns the pile of executor gate alerts sitting unsent in the
operator's Gmail drafts into a countable report: which gate keeps aborting which slot, how
long it has been doing it, and which alerts claimed a delivery channel that was never going
to work.

## The problem it was written for

Each live executor prompt's Gate 9 requires an aborting slot to alert on three channels:

1. **in-app message center** — primary,
2. **repo alert file** — durable fallback, under `dca_paths.stream_root(<stream>, 'alerts', …)`,
3. **email** — belt-and-braces.

Between 2026-08-06 and 2026-08-26 all three degraded at once, and the degradation was
invisible precisely because alerting was the thing that broke:

| Channel | What actually happened |
|---|---|
| Message center | Per-session. Nothing to go back and read after the session ends. |
| Repo alert file | 23 of 49 alerts reported writing to a bare repo-relative `artifacts/alerts/` — which `.gitignore` discards. Only 6 named the `stream_root(...)` path the prompt requires. |
| Email | Prompt said *"the email connector has no send capability, so a draft is NOT delivery"* and told the executor to create a draft. So every alert became a draft, and none were delivered. |

26 alerts also stated they were "posted to Slack", but `config/notifications.json` disables
Slack for routine alerts — the only Slack paths in this repo are two env-gated, off-by-default
webhooks (`DCA_DRIFT_SLACK_WEBHOOK`, `DCA_ALERT_SLACK_WEBHOOK`) that handle drift and uncaught
exceptions, not executor gate verdicts.

Net effect: the drafts were not failed sends. They were the **only surviving record** of every
aborted run, in the one channel the prompt itself documented as not-delivery.

## What the digest reports

```bash
# The Gmail-connector session exports drafts, then pipes them in.
python -m scripts.alert_digest --drafts drafts.json

# After the corrected prompts are deployed, alerts are SENT, not drafted -- so
# they never appear in a list_drafts export at all. Pass a sent-mail export too
# (or instead), or the digest goes blind to every alert the fix produces and
# reports working days as roster-silent.
python -m scripts.alert_digest --drafts drafts.json --messages sent.json

# Both surviving sources at once. Point --alerts-dir at the cron-tracking ROOT,
# not one stream: the scan recurses into every alerts/ directory beneath it, and
# the seven prompts write to six different roots between them (market_open_dca,
# morning_dca, intraday_dca, midday_dca, ce16ba6b/scheduled_runs in the live
# mirror, plus scheduled_runs in the numbered set). Naming a single stream
# silently omits the other five.
#
# Resolve the root through dca_paths rather than writing it out: it honours
# $DCA_CRON_TRACKING_ROOT and falls back to the same default every stream uses,
# so this works in a checkout and in production without a literal path.
ALERTS_ROOT="$(python -c 'from dca_paths import cron_tracking_root; print(cron_tracking_root())')"
python -m scripts.alert_digest --drafts drafts.json --alerts-dir "$ALERTS_ROOT"

# Only what is new since the last digest, and fail if any alert was undeliverable.
python -m scripts.alert_digest --drafts drafts.json --ledger --fail-on-warnings
```

* **BY GATE / BY SLOT / BY VERDICT** — frequency counts. `GATE5_DISABLED` and
  `GATE4_EXPECTED_OVERLAP` are counted as their own categories rather than folded into
  "Gate 5" / "Gate 4", because `config/notifications.json` classes both as *expected*
  conditions rather than errors; merging them would overstate how much is going wrong.
  Both are matched case-insensitively, like every gate and verdict pattern, and report
  under the canonical uppercase label whichever spelling the alert used.
  Verdicts are tested most-specific-first, across the subject *and* the body: nearly every
  alert that breaches a cap, has an order rejected, or stops mid-batch also says it
  *aborted*, and the usual shape is a plain subject over a detailed body ("Run ABORTED" /
  "cap breach at Gate 6"), so `CAP_BREACH`, `REJECTED` and `PARTIAL` are decided before the
  generic `ABORT` wording wherever they appear. `ABORT` is what is left when an alert says
  only that it stopped. A specific verdict in a body sentence that narrates a *prior* event
  ("one order was rejected earlier") is ignored — there the subject is the better summary —
  and that check, like negation, is scoped to the mention's own comma/conjunction-delimited
  clause rather than the whole sentence, so a contrastive clause elsewhere ("Unlike the
  previous run, this run is GATE5_DISABLED...") cannot disclaim a mention it isn't actually
  talking about. The generic verdicts (`ABORT`, `SKIPPED`, `REPORT`) get the same prior-event
  and negation screening as the specific ones, not a bare whole-field search: "Previous run
  aborted at Gate 2. This run skipped at Gate 5" reports `SKIPPED` from the current run's own
  sentence rather than `ABORT` from the first sentence's history, and "the run did not abort;
  it skipped" reports the outcome its own clause names rather than the one it rules out.
* **LONGEST REPEATS** — consecutive trading days on which one slot failed the same gate.
  Measured against the NYSE trading calendar, not raw calendar days, so a Friday-then-Monday
  repeat counts as continuous instead of breaking at every weekend. Because the calendar is
  the *expected* trading days rather than the observed alerting ones, a repeat spanning a
  roster-silent weekday correctly breaks in two rather than reading as unbroken — so a streak
  that looks shorter than the raw dates suggest usually means a silent day sits inside it.
  The calendar is per-slot: `config/market_calendar.json`'s `early_close_1300` days close at
  1:00 PM ET, and the 13:30, 14:30 and 15:30 slots are required to end silently on them, so
  those days are dropped from those three slots' index. A repeat spanning one of them counts
  as continuous for a late slot and correctly breaks for the four that trade normally.
* **COVERAGE** — a day × slot matrix over every trading day in the window the roster was
  *scheduled* for, whether or not anything alerted on it. This is where a slot that stopped
  alerting entirely shows up next to one that alerts daily — and where a day the *whole
  roster* went quiet gets its own `<- roster silent` row. That row only exists because the
  calendar is built from expected trading days (via `near_close_dca.nyse_holidays`, unioned
  with `config/market_calendar.json`'s own `holidays_closed` list -- the same field Gate 0
  treats as authoritative, so an ad hoc closure the computed formula has no way to predict
  cannot make this digest expect a roster that Gate 0 already knows will stay silent) rather
  than from observed alerts; deriving it from alerts would erase exactly the scheduler-wide
  outage the matrix is meant to reveal. "Expected" means scheduled, not
  merely open: the calendar is clamped to `config/trading_window.json`'s approved bounds
  (currently 2026-07-02 … 2026-10-30), which is what the external scheduler's own trigger
  window enforces, so a run whose range extends past them does not report every ordinary
  weekday after the window closed as a roster-wide outage. Alerts that landed outside the
  window are still counted and still get a row — only the *expected* calendar is clamped.
  An alert landing outside that calendar is marked two ways, because absence from it no
  longer means the market was shut: a weekend or holiday firing (itself a Gate 0/1 fault)
  reads `<- NOT a trading day`, while an ordinary NYSE weekday past the approved window
  reads `<- outside the approved trading window`.
* **DELIVERY WARNINGS** — the channel checks: `EMAIL_DRAFT_NOT_DELIVERED`,
  `GITIGNORED_ALERT_PATH`, `SLACK_DISABLED`. `EMAIL_DRAFT_NOT_DELIVERED` covers a send that
  was *attempted and failed* as well as one that was never possible — the two leave the
  operator equally un-alerted, and the corrected prompts require both to be recorded, in
  whatever free-form wording names the failure (an explicit "failed to send", or one of the
  connector-error categories Gate 9's fallback text names: auth, throttling, a provider error,
  or a network error — "authentication failed", "authentication error", "auth error",
  "timed out", "returned 401", etc.).
  `GITIGNORED_ALERT_PATH` also recognizes the *other* gitignored shape, not just a bare
  `artifacts/alerts/` path: `dca_paths.stream_root(...)` itself resolves to this checkout's own
  `cron_tracking/<stream>/[.../]alerts/...` when `$DCA_CRON_TRACKING_ROOT` is unset, and that
  default is discarded by `.gitignore` too — at *any* nesting depth under `cron_tracking/`, not
  only the single-segment `<stream>/alerts` shape, since `.gitignore`'s entry for that directory
  covers its entire subtree (the canonical 1530ET prompt's own durable-alert path nests one
  level deeper still, under `<stream>/scheduled_runs/alerts`). It is not raised for a path an
  alert's own body proves lives
  outside this checkout (POSIX, Windows-drive, or Windows-UNC network-share form), evaluated
  per mention — one copy's
  absolute, external path (including the real, configured production root) never exempts a
  *different*, genuinely gitignored bare-relative mention elsewhere in the same body.
  `SLACK_DISABLED` exempts the two env-gated webhooks
  `config/notifications.json` does
  permit: an alert that mentions posting a drift finding through `DCA_DRIFT_SLACK_WEBHOOK`
  is describing a supported route, not contradicting the config. The exemption has to *name*
  one of those routes — the bare word "webhook" does not qualify — and is scoped to the
  sentence carrying the Slack mention, so a routine-alert claim elsewhere still warns. Denying
  one channel in a sentence does not suppress a genuinely affirmed claim about a *different*
  channel elsewhere in that same sentence — negation is checked per match, within its own
  clause, not across the whole sentence.

`--json` emits the same content machine-readably, including the per-alert list. Coverage carries a
parallel `day_classification` map (`roster_silent` / `incomplete_roster_day` /
`outside_approved_trading_window` / `not_a_trading_day` / `expected`, one per day in `coverage`) so
a machine consumer can draw the same distinction the text report's markers do, without re-deriving
it from the calendar bounds.

### Reading the coverage matrix

Silence is ambiguous and the tool says so rather than papering over it: a slot that ran clean
and a slot that never fired at all both produce zero alerts. Under
`config/scheduled_tasks.json`'s daily-compact operating mode, no-op notifications are
suppressed, so a quiet row is *not* evidence the slot is healthy. Cross-check a quiet slot
against its own `latest_<stream>_run.json` pointer before concluding anything. The same
caveat applies to a `roster silent` day, which is why the digest reports those days rather
than interpreting them.

Streaks are measured against the same trading calendar, so a Friday-then-Monday repeat counts
as continuous while a repeat spanning a roster-silent Wednesday correctly breaks in two — with
the early-close exception above for the three afternoon slots.

### Sources, after the prompt fix lands

| Source | Flag | Carries `EMAIL_DRAFT_NOT_DELIVERED`? |
|---|---|---|
| Gmail drafts | `--drafts` / `--stdin` | Always — a draft is not delivery, by definition |
| Sent alert emails | `--messages` | Only if the body says delivery failed |
| Gate 9 repo alert files | `--alerts-dir` | Only if the body says delivery failed |

`--drafts` and `--stdin` are alternative forms of the same input and cannot be combined;
passing both would silently double every count.

One abort that survives on two channels is counted once. Records pair on (ET day, slot, gate)
*and* on not contradicting each other: two copies naming different verdicts, or stating
different reasons — Gate 9 requires the reason in every channel — are two incidents, not one.
That matters because the two channels do not preserve a retry symmetrically: the alert
filename is fixed per (day, slot, gate), so a second firing overwrites the first file while
both emails survive, and pairing on the tuple alone would consume the earlier firing's email
as the later file's copy. Two copies of one event within a *single* source stay two records —
a slot that alerted twice really did.

The two Gmail sources are paired with each other the same way, before either one reaches the
alert-file pairing above: an initial failed send leaves a draft, and when the retry succeeds
that same event also survives as a sent message. Supplying `--drafts` and `--messages` together
reconciles the pair into one record instead of counting the incident twice — but only when the
message is strictly LATER than the candidate draft: "the retry succeeded" is a claim about time,
not merely about matching (day, slot, gate) and not contradicting on verdict/reason. A slot that
fires the same gate for the same reason twice in one day — once cleanly, once into a failed send
— only pairs the failed draft with a message that could plausibly be ITS retry, so an earlier
successful message never resolves a later, genuinely-still-failed draft. With two retry pairs in
one day, each message pairs with its NEAREST qualifying preceding draft, and pairing is correct
regardless of which order either export lists its records in — Gmail's own list APIs commonly
return results newest-first. Unlike the
draft-vs-file merge, the draft's `EMAIL_DRAFT_NOT_DELIVERED` does **not** carry over: the
message is direct proof the email was actually sent, which is exactly what that warning
claims did not happen. Every other warning either copy carries still survives the merge. The
same reasoning applies one layer up, at the draft/message-vs-file merge: when a reconciled
Gmail record (source `gmail_message`) is the counterpart an alert file merges into, the file's
own `EMAIL_DRAFT_NOT_DELIVERED` is dropped the same way — the file can narrate an initial
failed attempt that a later, now-reconciled retry already resolves, and folding that stale
warning back in would resurrect a claim direct proof of delivery already settled.

An alert's ET day normally comes from the transport timestamp, but for a Gmail-sourced record
(`--drafts`/`--stdin`/`--messages`) a declared date wins when the alert names one, either as the
explicit `Date: 2026-08-26` label or as a bare ISO-shaped date anywhere in the *subject* — the
live-mirror Gate 9 contract only requires the subject to name the slot, the ET date, and the
failing gate, not that exact label, so a compliant subject like `1030 ET | 2026-08-26 | Gate 5`
is recognized too. Both SUBJECT forms are checked before the body's own labelled date: the
subject's entire job under the contract is to declare THIS alert's own date, while the body can
carry a labelled date of its own while narrating an unrelated PRIOR incident
("Previous run Date: 2026-08-25") — checking the body first let that historical mention outrank
the current alert's own, differently-dated subject. When no subject date exists at all, the
body's own labelled date is itself screened for a prior-event mention before being trusted, for
the same reason — an alert whose only body "Date:" is purely historical falls back to the
transport timestamp instead. A delayed send or a retry can land well
after the slot's own ET day has ended; reading the transport time alone would then file the
alert under the wrong day and split it from its correctly-dated alert-file copy at merge time. A
bare ISO-shaped date in the *body* is never read this way — the body routinely narrates an
unrelated *prior* date, and an unscoped match there would misdate the current alert to a day it
is only quoting; the subject's entire job under the contract is to declare this alert's own
date, so it alone is trusted for the bare form. A Gate 9 repo alert file (`--alerts-dir`) is
exempt from the declared-date override entirely: its day already comes from the contract filename, loaded at
noon ET and authoritative by construction, and a correctly-dated file's own body can narrate an
unrelated prior date under that same label without that being this file's actual day.

Sent mail matters more than it looks. Gate 2 tells an executor that cannot reach the repo to
skip the alert file and use the other channels — so on exactly those runs, a sent email is the
*only* surviving record. A drafts-only digest would report that day as slot- or roster-silent
and invent an outage out of a working alert path.

### The reporting window

`--since` defaults to the earliest day anything alerted — or, when the corpus is *empty*, to
`config/trading_window.json`'s own start. An empty corpus is a scheduler that has stopped
entirely, the worst thing this tool reports, and deriving the lower bound from the corpus in
that case left no bound at all: no calendar, no coverage, no roster-silent dates.

`--until` defaults to today in ET (override the reporting day with `--as-of`), **not** to the
last day anything alerted. That distinction is the whole point: a roster that went quiet a
week ago would otherwise produce a report ending on its final good day, showing no silence at
all. Bounding a report by its own contents hides precisely the outage it should surface.

An inverted window is an input error, not an empty run. `--since` is checked against the
*resolved* end of the window, so `--since` after `--as-of` is rejected too when `--until` is
omitted — otherwise a stale scheduled window replayed against an earlier reporting day filters
out every record and prints a clean zero-alert digest.

The coverage calendar is additionally clamped to `config/trading_window.json`'s approved
bounds, so widening `--since`/`--until` past them adds no phantom roster-silent days.

When both `--until` and `--as-of` are omitted, today only enters the roster-silence calendar
once the roster has actually finished for the day: the last slot that can still alert has to
have closed its own Gate 1 "TIME SANITY" window, not merely reached its nominal trigger time —
`16:00 ET` (`1530`'s own `3:30-4:00 PM ET` window) on an ordinary day, or `13:00 ET` (`1230`'s
own `12:30-1:00 PM ET` window) on a day `config/market_calendar.json` lists under
`early_close_1300`, where the `1330`/`1430`/`1530` slots are required to end silently. Every
roster slot's Gate 9 prompt (`claude/dca-executor-prompts/*.txt`) permits it to fire and alert
anywhere in a 30-minute window past its nominal time, so treating the bare nominal time as
"done" could still miss a legitimately late-firing alert. A normal morning invocation with no
alerts yet would otherwise label today `roster_silent` before its own slots ever had a chance
to fire, reading as a scheduler-wide outage that has not happened. This narrows the calendar
only — the record-filtering window is unaffected, so an alert that genuinely fired earlier
today still appears in the report — and never applies once `--until` or `--as-of` is given
explicitly: either one means the caller asked for that exact day, silence included.

A day excluded from the calendar this way but still carrying an observed alert renders as
`incomplete_roster_day` in the coverage matrix and JSON `day_classification`, not
`outside_approved_trading_window` — the latter means past `config/trading_window.json`'s
`end_date`, which today plainly is not; it is simply not finished yet. Restricted to a day that
was itself actually an EXPECTED roster day (a trading day inside the approved window): an
off-schedule alert observed on a weekend or a holiday still classifies as `not_a_trading_day`
as usual, never `incomplete_roster_day`.

LONGEST REPEATS still sees an already-fired alert on such a day: it uses the calendar that
excludes today only for roster-SILENCE detection, plus any incomplete day back in for streak
adjacency, so a real two-day repeat (yesterday and today, same slot and gate) still reads as
two days, not one — the clamp that keeps today out of ROSTER-SILENT reporting never has to cost
a genuine streak its most recent day.

Under `--slot`, a filtered view can have zero of its own alerts while the *other* six slots
alerted normally in the same window. The zero-alert summary line reflects that: it only claims
every trading day is roster-silent when `digest.silent_days` covers the whole calendar: a
partial case reports the actual count instead, and a window with no roster-wide silence at all
says so, rather than a blanket "every trading day below is roster-silent" that would contradict
the nonzero cells the coverage matrix prints immediately after it.

The zero-alert text report still renders the coverage matrix even when `digest.calendar` itself
is empty (an unfinished-today window with nothing else in range), as long as `digest.days` has
something to show — the same incomplete-but-observed row from another slot the paragraph above
describes. Only a report with *neither* a calendar nor any observed day returns the bare
"No alerts in range" summary.

### Input hygiene

The CLI takes a raw `list_drafts` export and does not require you to pre-filter it. Drafts
carrying no executor-alert signature at all — no slot and no gate — are set aside and
counted, because every draft otherwise picks up `EMAIL_DRAFT_NOT_DELIVERED` by construction
and one unrelated personal draft would hold `--fail-on-warnings` red forever. A *partially*
parsed alert is always kept: a half-written alert is what an operator most wants surfaced.
Pass `--keep-unmatched` to audit what the filter removed.

Two signals do not count on their own: a slot recognized only from a bare wall-clock time, or
only from a stream word. "10:30 AM" is ordinary English, so `Lunch at 10:30 AM` parses as the
1030 slot; "midday" is likewise ordinary enough that `Midday lunch plan` parses as the 1230
slot. Both need a gate as well — without one, `--keep-unmatched` is required to see the record
at all, and it is excluded from roster coverage even then. Every other slot signal — a
scheduler task name, an explicit `1030 ET` label, a Gate 9 filename, or a DECLARED clock
("scheduled 1130 ET", "expected 11:30 AM") — is domain vocabulary and still stands alone; only
the bare, purely positional clock mention above is weak, not a declaration naming Gate 9's own
"scheduled"/"expected" phrasing.

A `drafts` field that is not a JSON array is rejected rather than read as an empty corpus, and
an empty stdin stream is an error — the connector's zero-results form is `{}`, so no bytes at
all means the export command failed.

## Where it fits

Standard library only, and **it never talks to Gmail**. Like every other live path here
(`docs/live_wiring.md`), the connector lives in the external agent session: that session
exports `list_drafts` to JSON and hands it to this module. That keeps the repo free of
credentials and network calls, and means the same digest runs offline against an alert-file
directory with no mailbox at all.

The `--ledger` flag persists seen alert keys to
`stream_root('alert_digest')/seen_alerts.json`. The report itself still spans the whole
corpus — streaks and the coverage matrix are meaningless without the history — but the ledger
names the new arrivals, and `--fail-on-warnings` judges **only those**. That is what makes a
scheduled check usable: it goes green once a known backlog stops growing, instead of staying
red forever on drafts it has already reported until nobody reads it.

A missing or corrupt ledger degrades to "everything is new" rather than failing — a ledger is
a convenience, not evidence, and losing it must never cost the operator the digest.

When `--drafts` and `--alerts-dir` are used together — the expected configuration once
delivery is fixed — a single abort survives in both channels. Records matching on
`(ET day, slot, gate)` are collapsed across sources so gate frequencies aren't doubled
exactly when alerting is working best; the file copy wins (its slot and gate come from the
executor's own filename rather than from prose) and the draft's warnings are folded in.
Duplicates *within* one source are kept: a slot that alerted twice on the same gate really
did alert twice.

## The prompt fix that goes with it

The digest measures the problem; it does not fix delivery. As of this change all fourteen
executor prompts (`claude/dca-executor-prompts/*.txt` and the canonical
`live-mirror/*.txt`) say **send** the alert email, not draft it, with a draft permitted only
if the connector genuinely exposes no send capability — and in that case the executor must
state in the primary alert that email delivery did not happen. The channel-2 instruction now
also spells out that `artifacts/` is gitignored.

**Editing the repo copy does not change what is deployed.** The live task text lives in the
external scheduler, so each of the seven crons has to be re-pasted from its prompt file for
the fix to take effect. Until then, `--fail-on-warnings` will keep tripping on
`EMAIL_DRAFT_NOT_DELIVERED`, which is the intended behavior: it stays red while the alerts
are still undeliverable.
