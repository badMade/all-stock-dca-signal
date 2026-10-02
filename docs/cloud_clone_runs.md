# Running the live slots from a cloud clone

Why this exists: on 2026-09-28 (from the 14:30 slot) and 2026-09-29 the scheduled
executor sessions ran in a cloud container that had no checkout of this repo, so
Gate 2 aborted ("executor ran without repo access") and no evidence could be
written. A cloud container starts with a fresh checkout and an empty
`cron_tracking/`, and it is thrown away after every fire. Two commands make that
safe. Neither contains signal, sizing or budget logic, and neither installs
anything (the live path is standard library only, Python 3.11 or newer).

| Command | Runs | Purpose |
|---|---|---|
| `python3 -m scripts.cloud_preflight` | at Gate 2, before anything else | Prove the checkout is the pinned release and pull the evidence store |
| `python3 -m scripts.evidence_publish publish` | after Gate 9 | Push the slot's evidence, or leave a marker and an alert text if the push fails |
| `python3 -m scripts.evidence_publish claim` | after Gate 4, before Gate 6 | Compare-and-swap so a second executor for the same slot and day is refused |

## Environment

| Variable | Meaning |
|---|---|
| `DCA_PINNED_SHA` | The full lower-case 40-character commit SHA a human promoted. Held in the run environment, never in the repo: a file in the checkout cannot vouch for itself |
| `DCA_CRON_TRACKING_ROOT` | Set to the same absolute path in every container (recommended: `/home/user/workspace/cron_tracking`, the bundled root, so `config/*.json` paths need no rebase). Pointers store absolute paths and are rejected as `outside_root` if the root differs between containers. Export it before Python starts; several modules read it at import time |
| `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` | As before. Also used by `evidence_publish` to refuse any evidence that contains it; `publish` refuses (exit 4) when it is unset or shorter than 6 characters, because the exact-match scan cannot run without it |
| `DCA_ALERT_EMAIL_ADDRESS` | Alert routing; the operator address otherwise lives only in repo config |

## Preflight

```bash
python3 -m scripts.cloud_preflight --repo-root . \
    --evidence-remote <private evidence repo> --evidence-root "$DCA_CRON_TRACKING_ROOT" \
    --trading-day YYYY-MM-DD [--expect-prior-slots 0935,1030] [--init]
```

Exit 0 prints `PREFLIGHT_OK <sha>`. Exit 2 prints `GATE2_ABORT: <reason>`, which is
a Gate 2 abort (keep the literal `Failing gate: Gate 2` in the alert so
`scripts/alert_digest.py` buckets it correctly). It refuses when:

- `DCA_PINNED_SHA` is missing or is not a full lower-case SHA;
- HEAD is not the pinned SHA, a tracked file is modified, the checkout holds an
  untracked file that git does not ignore (it could shadow pinned code), or the pinned
  commit is not an ancestor of `origin/main`;
- the evidence store is unreachable, has no `MANIFEST.json` (unless a human passes
  `--init`), or has an unusable one (invalid JSON, a wrong shape, or a duplicate key);
- `--expect-prior-slots` names a slot the manifest does not list for the day, or a
  listed slot whose run directory does not pass the `scripts/verify_schedule.py`
  audit (a manifest entry is a claim, not proof: an empty stream root would
  otherwise read as a complete prior-allocation scan);
- Python is older than 3.11.

An unreachable store never looks like a new day, and a refusal removes any clone
it created. On success the stream artifact roots exist, because git does not
track empty directories and `scripts/prior_reference_bindings.py` aborts on a
missing root.

## Evidence store

A private git repository (it holds tickers, amounts and order IDs). `main` carries
`MANIFEST.json`:

```json
{"version": 1, "days": {"2026-09-29": {"slots": ["0935", "1030"]}},
 "last_publish": {"day": "2026-09-29", "slot": "1030", "code_sha": "<40 hex>"}}
```

Git cannot push without read access, so the credential is a token or GitHub App
limited to that one repository with Contents read and write and nothing else.

## Publish

Gate 9 writes the evidence locally first, exactly as before. Then:

```bash
python3 -m scripts.evidence_publish publish --evidence-root "$DCA_CRON_TRACKING_ROOT" \
    --remote <evidence repo> --trading-day YYYY-MM-DD --slot 1330 --code-sha "$DCA_PINNED_SHA"
```

| Exit | Status | Meaning |
|---|---|---|
| 0 | `published` | Pushed and manifest updated |
| 3 | `push_failed` | Local artifacts untouched and committed locally, marker at `evidence_push_failed/<day>_<slot>.json`, `alert_text` carries SHA, slot, orders, tickers, dollars and order IDs. Send it by email. Never re-run Gate 7 |
| 4 | `refused_secret` | The account number (exact, or an account-shaped literal) was found; only the relative paths are printed, nothing is pushed |
| 7 | `refused_provenance` | `--code-sha` is not `$DCA_PINNED_SHA`, `$DCA_PINNED_SHA` is unset, or the slot's `run_state.json` / `run_summary.json` records a different `code_version.git_sha`; nothing is pushed |

A competing publish from another container is merged: `MANIFEST.json` is the union
of both sides and, for `latest_*_run.json`, the more recently written pointer wins.
Any other conflicting file fails the publish rather than guessing.

## Claim

```bash
python3 -m scripts.evidence_publish claim --evidence-root "$DCA_CRON_TRACKING_ROOT" \
    --remote <evidence repo> --trading-day YYYY-MM-DD --slot 1330 --claimant <run id>
```

The remote rejecting a non-fast-forward push is the mutex (git runs with `LC_ALL=C`, because the lost-race and non-fast-forward checks read its English error text). Exit 0 means this run
owns the slot; 5 means another run already does (abort); 6 means the store is
unreachable or kept changing (abort, fail closed). Any existing claim is refused,
including the same claimant, so a scheduler retry cannot double-trade. `--slot` must
be a roster slot and `--claimant` a run id (1-80 letters, digits, `.`, `_`, `:`, `-`);
a claimant holding the account number is refused with exit 4 before anything is
committed. Robinhood's
own `ref_id` (`dca-runner-<slot>-<day>-<TICKER>`) remains the second line of
defence; whether the broker rejects a repeated `ref_id` is not verified.

## Change control: only merged and pinned code trades

1. Protect `main` (pull request and CI required, no force push).
2. A human tags a green `main` commit `live-YYYYMMDD-N`.
3. A human sets `DCA_PINNED_SHA` in the run environment to that commit. Preflight
   enforces equality plus ancestry of `origin/main`.

Each run records what it traded from as the optional `code_version` object in
`run_state.json` and `run_summary.json` (`--code-version-json` on
`scripts/finalize_run_evidence.py`, inline or a file path):
`{"git_sha": "<40 hex>", "tag": "live-YYYYMMDD-N", "pinned_by": "DCA_PINNED_SHA"}`.
Duplicate keys are refused, and when `DCA_PINNED_SHA` is set the `git_sha` must equal
it. When an executor already wrote `run_state.json`, the rest of that state is kept
unchanged and only `code_version` is added to it and to `run_summary.json`; a state that
already records a different `code_version` is refused.

## Not covered here

The seven live-mirror prompts and the live task bodies are not changed by this
code. Adding the Gate 2 paragraph, the claim step and the Gate 9 publish step to
them, and repasting the live tasks, is a separate, separately approved change that
must build on the distinct "repo readable only via the device bridge" Gate 2 abort
already added in #1569.
