You are the Integrator. Build Rock 1 of the plan in rocket-fuel/self-heal/PLAN.md (Revision 6).
Read PLAN.md and VTO.md first. You have workspace-write. Build ONLY what this contract says.

## Task

Create the configuration and reason-code foundation of the `selfheal` package:

1. `selfheal/__init__.py` — package marker, docstring, `__all__`. No logic.
2. `selfheal/reasons.py` — module-level string constants in the exact house style of
   `scripts/reason_codes.py` (read it first and match it): one constant per refusal reason, a
   heavy explanatory comment per constant saying WHY that reason exists, plus
   `is_valid_reason(code)` and `is_refusal(code)` helpers and a frozenset of all codes.
   Codes required: DISABLED, ALREADY_RUNNING, LIFETIME_BOUND_REACHED, BREAKER_LATCHED,
   RATE_LIMITED_DAILY, CLOCK_IMPLAUSIBLE, BUDGET_EXHAUSTED_MINUTES, FINGERPRINT_QUARANTINED,
   REPORT_ONLY_NO_ADAPTER, WALL_CLOCK_EXCEEDED, NO_NOVEL_INCIDENTS,
   FINGERPRINT_KEY_UNAVAILABLE, SELF_ORIGIN_DROPPED, PROVENANCE_PENDING, NOT_MAIN_REF,
   CONFIG_INVALID, STATE_UNREADABLE, ADMITTED.
3. `config/self_heal.json` — every threshold the plan names, with a `"$schema"` pointer to
   `../schemas/self_heal.schema.json`.
4. `schemas/self_heal.schema.json` — draft-07, `additionalProperties: false`, every field
   typed with min AND max.
5. `selfheal/config.py` — `load(path=None) -> SelfHealConfig` (a frozen dataclass with type
   hints and Google-style docstrings), doing STRICT FAIL-CLOSED runtime validation.
6. `tests/selfheal/__init__.py` and `tests/selfheal/test_config.py`.

## Required config keys, defaults, and bounds

These two exist specifically to close Same Page findings and MUST be present:

  * `max_repairs_per_day`: default 1, minimum 1, maximum 24. This is the operator's explicit
    once-per-day requirement. Round 5 found it was never actually specified.
  * `max_cumulative_skew_seconds`: default 604800, minimum 60, maximum 2592000. Round 5 found
    it became load-bearing in Revision 6 with no default and no ceiling. It MUST ALSO be
    cross-field validated: it must be strictly less than the shortest reset period, i.e. less
    than `budget_period_seconds`.

Also required, each with an explicit min and max in the schema and a documented rationale:
`enabled` (bool), `max_repairs_per_epoch` (10, 1..50), `breaker_failure_threshold` (3, 1..20),
`max_attempts_per_fingerprint` (3, 1..10), `max_tick_seconds` (600, 30..3600),
`max_wall_clock_seconds` (1800, 60..7200), `monthly_actions_minutes` (300, 10..2000),
`ci_worst_case_minutes` (150, 1..600), `max_forward_skew_seconds` (172800, 60..604800),
`max_archive_bytes_per_incident` (1048576, 4096..67108864),
`max_archive_bytes_per_run` (67108864, 65536..536870912),
`budget_period_seconds` (2592000, 86400..7776000),
`review_lease_days` (14, 1..90), `execution_lease_seconds` (3600, 60..21600).

## Validation rules `load()` MUST enforce, each refusing with a clear exception

Duplicate JSON keys (use `object_pairs_hook`); JSON that is valid but not an object; unknown
keys; missing required keys; a missing config file; booleans where an integer is expected
(note `isinstance(True, int)` is True in Python — you must reject it explicitly); non-finite
floats; negative values; zero where the minimum is >= 1; any value outside its declared
min/max; and the cross-field rule above. Every failure raises a `ConfigError` whose message
names the offending key and why. There must be NO code path where a malformed config yields a
usable object.

## Hard constraints

  * Standard library ONLY. Python >= 3.11. No third-party imports in `selfheal/`.
  * Type hints on every function; Google-style docstrings (Args/Returns/Raises) on every
    public function and class.
  * Match this repository's house comment style: heavy inline commentary explaining WHY a line
    exists. Read `scripts/reason_codes.py` and `dca_config.py` and imitate them.
  * **No standalone 6-12 digit literal may appear in any tracked file you create.** This repo's
    `tests/test_config_invariants.py` greps for account-shaped numbers and WILL fail your
    build. Several of the defaults above are 6-12 digits: write every one of them with Python
    underscore separators in .py files (e.g. `604_800`, `2_592_000`, `1_048_576`,
    `67_108_864`, `536_870_912`, `172_800`). JSON has no underscore syntax, so in
    config/self_heal.json and schemas/self_heal.schema.json express those particular values as
    a documented smaller unit that avoids the pattern (for example seconds-based keys in whole
    minutes or hours, byte-based keys in KiB) and convert in `config.py`, OR choose defaults
    that are not 6-12 digits. State plainly in your final message which approach you took and
    why. Verify with: `.venv/bin/python -m pytest tests/test_config_invariants.py -q`
  * Do not modify any existing file except to add nothing. Create new files only.
  * Do not add the config to pyproject.toml — that is Rock 7's job.

## Proof command (run it yourself and report the output verbatim)

.venv/bin/python -m pytest tests/selfheal/test_config.py tests/test_config_invariants.py -q && ruff check .

`tests/selfheal/test_config.py` must contain one test per validation rule above, each
asserting the specific failure, plus a test that the shipped `config/self_heal.json` loads and
that `max_repairs_per_day` is 1 by default.
