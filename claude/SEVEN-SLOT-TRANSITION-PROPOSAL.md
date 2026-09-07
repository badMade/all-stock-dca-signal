# Seven-slot Claude/Perplexity coexistence: closing the remaining gaps

**Status: design proposal, not implemented.** Written in response to a direct request
("can Idempotency, locking, leasing be per LLM... so they can back each other up and not
compete for buy slots") plus "make the intended replacement 7-slot workflow for both
Perplexity and Claude and deprecate the 5-slot workflow." No code in this repo changes as a
result of this document. It exists so the actual code change (§3) and the rollout sequencing
(§4) can be reviewed before anyone touches `dca_runner.py` or flips a live scheduler.

## 0. What already exists — read this first

Most of the per-LLM coordination model this was asked for is **already designed and
partially wired**: see `claude/PERPLEXITY-CLAUDE-COORDINATION.md` (written 2026-08-02, per
Geoff's direction: "first come, first serve... if one fails, it notifies the other to run as
a backup, independently per slot"). Its §3/§4 already specify:

- **Claiming a slot = placing a real order in that slot's window.** The broker's own
  timestamped order history is the only real shared medium between the two engines — there is
  no channel to push a message between them.
- **A third Gate 4 classification, "recognized compatible executor."** Today Gate 4 in the
  six standard live-mirror prompts only has two buckets: Geoff's own manual activity
  (`placed_agent` in `{'user','recurring','drip'}`, never aborts) and everything else
  (competing executor, always aborts). The proposed third bucket splits "everything else"
  into COMPATIBLE (a confirmed Perplexity/Claude marker) and ROGUE (still aborts
  unconditionally, even alongside a compatible order — a third, unauthorized executor is
  still a real collision).
- **Per-ticker exclusion, not slot-level stand-down.** When the window contains zero rogue
  signals and at least one compatible signal, the standing-down engine doesn't abort — it
  excludes only the tickers the compatible executor already covered *in that same window*
  from its own candidate list, and proceeds normally on everything else. This is what makes a
  **partial**-batch failure recoverable: if the compatible executor's own Gate 7 stopped
  mid-batch on an earlier rejection, the backing engine picks up the rest instead of standing
  down entirely.
- **Paste-ready mirrored text for Perplexity's own prompts** (§4), since Perplexity can't read
  this repo directly.

Read that document in full before this one — everything below assumes it and only addresses
what it leaves open.

## 1. The two gaps this proposal closes

**Gap A — the coordination model is prose, and only exists in six of seven slots.**
`claude/PERPLEXITY-CLAUDE-COORDINATION.md` §5.5 says this explicitly: the midday (1230ET)
slot's Gate 4 does not use the `placed_agent`-classification text described in §3 at all — it
calls `dca_runner.gate_sole_executor()` directly (real Python, not prose), whose current
signature has no compatible-executor concept:

```python
def gate_sole_executor(orders, slot: SlotSpec, day: date) -> list[BrokerOrder]:
    """Return orders already submitted inside this slot's window.
    Non-empty means a second writer is live and the run must abort.
    ``placed_agent`` is intentionally ignored.
    """
```

Any order in the window — compatible or not — collides today. §5.5 calls this "a real code
change to `dca_runner.py`, not a prompt-text edit... left as a separate, explicit follow-up."
§3 below is that follow-up.

**Gap B — `PERPLEXITY_PLACED_AGENT_VALUE` is still an unconfirmed placeholder.** Even with
Gap A closed, *nothing* in the coordination model activates until someone confirms the actual
literal value Robinhood's `get_equity_orders` populates for Perplexity's own orders. This
requires reading real order history (`get_equity_orders` against known Perplexity-placed fill
timestamps) — broker access this repo does not have. §5 covers what to do about this; it
cannot be resolved by writing more code or docs.

**Confirming the value is not the same as confirming it's SAFE to use alone.** §4's own
paste-ready Perplexity-side text already establishes why: it explicitly rejects a bare
`placed_agent` of `'agentic'` as insufficient evidence on its own, "that value is generic
enough that another automation could share it, so treating it alone as Claude's mark could
silently suppress a genuine collision with a third, unrecognized executor" (its own Codex
review citation: "Require Claude-specific evidence before standing down"). `'agentic'` is not
a hypothetical example here — it is `BrokerOrder.placed_agent`'s own dataclass default in this
module. If Gap B's confirmed `PERPLEXITY_PLACED_AGENT_VALUE` turns out to be that same generic
value (or any other value multiple automations could plausibly share), §3's
`compatible_placed_agent_values` membership test is exactly as unsafe as the bare check §4
already rejects: it would classify every malformed-or-default-`placed_agent` order, including a
genuine unrecognized third executor, as compatible rather than rogue (Codex review: "Require
executor-specific evidence for compatibility"). §3's implementation MUST carry the same bar §4
already sets for the reverse direction — see §3's own note on this below — not a weaker one just
because this side of the check runs in Python instead of prose.

## 2. Why this can't be "just implement it"

This whole system is a live-money trading pipeline (CLAUDE.md: "no test coverage on the
live-wiring seams," per the existing "three entry points" caveat) and the coordination model
touches Gate 4 — the exact gate that exists to prevent two writers from double-buying the same
account concurrently. Two things make this specifically not a drop-in change:

- **`gate_sole_executor()`'s return contract has to change shape, not just gain a parameter.**
  Today it returns "the list of colliding orders" (empty = proceed, non-empty = abort). The
  coordination model needs a three-way split (rogue / compatible / manual) *and* needs to
  expose which **tickers** the compatible executor already covered, so the caller can exclude
  just those tickers rather than aborting or blindly proceeding. That is new information the
  function has never returned before, to five other call sites (the six live-mirror prompts +
  `run_dca.run_slot()`'s own preflight) that all currently treat "return value truthy" as
  "abort." A signature change here is exactly the "three entry points" problem CLAUDE.md
  already flags — this function is shared, tested, load-bearing code, and every caller needs
  auditing, not just the one this proposal cares about.
- **Backward compatibility must be exact when the new parameter is unused.** The six
  live-mirror prompts already have their own working prose version of this logic (§3/§4 of the
  coordination doc). `dca_runner.gate_sole_executor()` must not change behavior for any caller
  that doesn't opt in, or every existing test and every one of those six prompts' documented
  behavior needs re-verification as a side effect of a change meant only for the midday slot.

## 3. Proposed code change (spec only — do not implement without review)

```python
@dataclass(frozen=True)
class SoleExecutorResult:
    """Split of every order found in the slot's window.

    ``rogue`` non-empty is an unconditional abort, exactly like today's
    plain non-empty return -- even when ``compatible`` is also non-empty,
    since a rogue order alongside a compatible one means a THIRD,
    unauthorized executor may be live.

    ``compatible_tickers`` is the set of tickers a recognized compatible
    executor already covered in this same window -- exclude these from
    this run's own candidate list (Gate 5) rather than resizing them via
    the same-day top-up guard, which governs cross-slot repeats, not
    same-window overlaps (config/budgets.json's
    one_order_per_ticker_per_stream already blocks a same-window repeat
    regardless of size).

    A ``compatible`` row with a non-string, empty, or whitespace-only
    ``symbol`` is NOT silently dropped from ``compatible_tickers`` --
    filtering it out (or accepting an unusable blank value) would let the
    caller proceed believing it knows every ticker the compatible executor
    already covered, when in fact one is unaccounted for and a same-window
    duplicate buy is possible. ``compatible_tickers`` raises ``ValueError``
    on any row whose symbol cannot identify what was actually bought,
    forcing the caller to treat that as it would any other Gate 4
    uncertainty -- abort and alert -- rather than silently proceeding on an
    incomplete or unusable exclusion set (Codex review: "Fail closed on
    malformed compatible symbols", "Reject malformed string symbols in
    compatible orders").

    __bool__, __iter__, and __eq__ together mirror today's plain-list return
    for every EXISTING caller that does `if collisions:`, `for o in
    collisions:`, and/or `collisions == []`/`collisions == [...]`, without
    knowing this richer type exists -- an empty result must stay falsy, and
    `dataclass` gives no __bool__/__iter__ for free, so every current call
    site would otherwise start treating every result (even
    SoleExecutorResult(rogue=[], compatible=[])) as truthy (Codex review:
    "Make wrapped empty results falsey for existing callers"); every
    nonempty result would raise TypeError on iteration instead of walking
    the rogue orders it documents (Codex review: "Preserve iteration over
    rogue collisions"); and `dataclass`'s auto-generated __eq__ only compares
    against another SoleExecutorResult, so it would return False for the
    list-literal comparisons `test_gate4_empty_window_passes`,
    `test_gate4_orders_just_outside_boundary_do_not_trip`, and
    `test_gate4_ignores_orders_from_other_days` already make against
    `gate_sole_executor(...)`'s return value (Codex review: "Preserve list
    equality in the compatibility wrapper") -- until every caller is
    migrated to `.rogue` directly.
    """
    rogue: list[BrokerOrder]
    compatible: list[BrokerOrder]

    def __bool__(self) -> bool:
        return bool(self.rogue)

    def __iter__(self):
        return iter(self.rogue)

    def __len__(self) -> int:
        # Rounds out the list-emulation surface alongside __bool__/__iter__
        # (Antigravity review: "Missing __len__ on SoleExecutorResult
        # Sequence Wrapper") -- no existing caller calls len() on this
        # today, but one that does should see the same count `for o in
        # collisions` would iterate, not a TypeError.
        return len(self.rogue)

    def __getitem__(self, index):
        # Completes the sequence-emulation surface alongside __iter__/__len__
        # (Antigravity review: "Sequence Emulation Gap: Missing
        # __getitem__") -- none of the three existing dca_runner.py call
        # sites index into `collisions` today (they only use truthiness,
        # `for o in collisions`, or a list comprehension), but `result[0]`
        # should see the same rogue order that indexing the plain list
        # returned before this wrapper existed, not a TypeError.
        return self.rogue[index]

    # Deliberately NOT given __hash__ = None or unsafe_hash=True. The
    # Antigravity review flagged this class as unhashable ("Data Class
    # Hashability Warning") and attributed it to `frozen=True` plus a
    # custom __eq__ setting __hash__ to None -- that specific mechanism is
    # wrong (dataclass still generates a __hash__ here, since `frozen=True`
    # requests one and a class-body __eq__ doesn't suppress it), but the
    # practical symptom it predicted is real for a different reason:
    # `rogue`/`compatible` are `list[BrokerOrder]`, and hashing a tuple
    # that contains a list raises `TypeError: unhashable type: 'list'` at
    # the first `hash(result)` or `{result}`/`{result: ...}` call, not at
    # class-definition time. No existing or proposed caller needs
    # SoleExecutorResult as a dict key or set member (every call site above
    # only checks truthiness, iterates, or compares against a list
    # literal), and forcing `unsafe_hash=True` would be actively wrong here
    # since the two fields are mutable lists -- a hash taken before either
    # list is appended to would silently disagree with one taken after,
    # corrupting any hash-based collection this went into. Left alone, the
    # failure mode for a future caller that tries anyway is a loud
    # TypeError, not silent corruption, which is the same fail-closed
    # direction every other edge case in this class takes.

    def __eq__(self, other) -> bool:
        if isinstance(other, SoleExecutorResult):
            return self.rogue == other.rogue and self.compatible == other.compatible
        if isinstance(other, list):
            # Restricted to compatible == [] on purpose: unrestricted
            # `self.rogue == other` broke equality's transitivity requirement
            # (Antigravity review: "Equality transitivity violation") --
            # two results sharing an empty `rogue` but carrying DIFFERENT
            # `compatible` orders would otherwise both equal `[]` without
            # equalling each other. Only the OLD callers this shim exists
            # for ever compare against a bare list, and they never populate
            # `compatible` (they don't pass compatible_placed_agent_values,
            # so every order lands in `rogue`), so this restriction costs
            # those callers nothing.
            #
            # This deliberately makes `==[]` DISAGREE with `__bool__`/`__len__`
            # whenever `compatible` is non-empty: `SoleExecutorResult(rogue=[],
            # compatible=[x])` is falsy and has length 0, but does NOT equal
            # `[]` (Antigravity review: "Contradictory equality vs. truthiness
            # in proposed SoleExecutorResult" -- suggested making `==[]` ignore
            # `compatible` too, i.e. `self.rogue == other` unconditionally, but
            # that is exactly the transitivity violation the restriction above
            # was added to close: two DIFFERENT compatible-carrying results
            # would then both equal `[]` without equalling each other). No
            # OLD caller (the only ones this shim exists for) ever populates
            # `compatible` or compares with `!=`/`==` against a list in the
            # first place -- they only use truthiness (`if collisions:`) --
            # so this asymmetry is unreachable from any caller this proposal
            # is backward-compatible for; a NEW caller that opts into
            # `compatible_placed_agent_values` must use `.rogue`/`.compatible`
            # directly rather than comparing the whole result against a bare
            # list, exactly as the compatible-aware call-site examples in this
            # document already do.
            return not self.compatible and self.rogue == other
        return NotImplemented

    @property
    def compatible_tickers(self) -> frozenset[str]:
        tickers: set[str] = set()
        for o in self.compatible:
            if not isinstance(o.symbol, str) or not o.symbol.strip():
                # Never embed the raw malformed value: callers of this
                # property are told to abort-and-alert on the exception, and
                # a non-string symbol can carry an account identifier, token,
                # or other connector-internal data that must not reach a
                # transcript or notification channel (Codex review: "Redact
                # malformed symbols from compatibility errors"), matching
                # the collision-side redaction the live-mirror prompt already
                # applies via _redact_symbol().
                raise ValueError(f"malformed compatible order symbol, type={type(o.symbol).__name__}")
            # Normalize to the canonical uppercase form Gate 5 candidates use
            # (Codex review: "Normalize compatible tickers before returning them")
            # so a raw "aapl" from broker history still excludes "AAPL".
            normalized = o.symbol.strip().upper()
            if normalized not in ACTIVE_TICKERS:
                # A well-formed but bogus string ("TOKEN", "UNKNOWN", a typo)
                # passes the isinstance/strip check above yet still doesn't
                # identify a real security this account trades -- accepting
                # it as a covered ticker leaves the true exclusion set
                # incomplete (whatever ticker the compatible executor
                # actually bought is still missing from it), the same
                # incomplete-exclusion failure mode the malformed-symbol
                # check above exists to prevent, just from a different input
                # shape. Fail closed the same way: raise, forcing the caller
                # to abort-and-alert rather than proceed on data it can't
                # verify (Codex review: "Reject unknown compatible-order
                # symbols"). Still redacted the same way as the malformed-
                # symbol branch above -- being a well-formed string doesn't
                # make it safe to embed via !r: an unrecognized "ticker" is
                # exactly the shape a leaked credential, account identifier,
                # or connector token would take, and this exception is
                # still bound for abort-and-alert transcripts/notifications
                # (Codex review: "Redact unknown symbols from compatibility
                # errors").
                raise ValueError(f"compatible order symbol not a configured active ticker, length={len(o.symbol)}")
            tickers.add(normalized)
        return frozenset(tickers)


def gate_sole_executor(
    orders,
    slot: SlotSpec,
    day: date,
    *,
    compatible_placed_agent_values: frozenset[str] = frozenset(),
) -> SoleExecutorResult:
    """Classify every order in the slot's window.

    Backward compatible in BOTH senses an existing caller can rely on:
    (1) truthiness -- `if gate_sole_executor(...):` behaves exactly like
    today's `if collisions:`, via __bool__ above; (2) manual-order
    handling -- placed_agent in {'user','recurring','drip'} is NOT
    special-cased here. The current gate_sole_executor()'s own docstring
    is explicit that it deliberately does NOT carve manual orders out
    ("a manual order inside the window is still a second writer against
    the same account"); the six standard live-mirror prompts' matching
    carve-out lives in THEIR OWN prose, one layer above this function,
    not inside it. An earlier draft of this proposal added an
    unconditional manual-order carve-out directly here, which would have
    silently weakened every existing Python caller's sole-executor
    interlock the moment this change shipped, whether or not that caller
    ever opts into compatible_placed_agent_values (Codex review:
    "Preserve manual-order collisions for default callers"). If a manual
    carve-out inside this function is ever wanted, that is a separate,
    deliberate, opt-in decision -- not bundled into this change.

    SAFE TO CALL WITH A NON-EMPTY compatible_placed_agent_values ONLY IF
    every value in it is confirmed executor-UNIQUE -- not merely confirmed
    to be what the compatible executor uses. §1's Gap B and §4's own
    paste-ready Perplexity-side text both establish why a bare
    `placed_agent` match isn't sufficient on its own: `'agentic'` is
    generic enough that another, unrecognized automation (including this
    module's own `BrokerOrder.placed_agent` default) could share it, and
    treating it alone as proof of the compatible executor would silently
    reclassify a genuine third-writer collision as compatible instead of
    rogue (Codex review: "Require executor-specific evidence for
    compatibility"). If Gap B's confirmed value is NOT proven unique this
    way, `compatible_placed_agent_values` as implemented below is not
    safe to populate -- this function would need a second field to key
    off instead (a Claude/Perplexity-specific order or client identifier,
    the same kind of value `dca_runner.py`'s own `expected_ref_id`
    mechanism already matches elsewhere in this module, mirroring §4's
    `dca-runner-<slot_id>-` prefix check), which `BrokerOrder` does not
    currently carry as a field. That extension is out of scope for this
    already-large change and is deferred to whoever resolves Gap B and
    finds the confirmed value is not unique.
    """
    start, end = slot.window_bounds_utc(day)
    in_window = [o for o in orders if start <= _require_aware(o.created_at).astimezone(timezone.utc) <= end]
    rogue, compatible = [], []
    for o in in_window:
        # isinstance() first, not just a membership test: a malformed row's
        # placed_agent can be unhashable (a dict or list of connector
        # metadata), and `x in a_frozenset` raises TypeError on an unhashable
        # x rather than returning False -- escaping every existing caller's
        # exception handling (assess()'s Gate 4 block catches
        # only AttributeError/ValueError, and execute_plan()'s own Gate 4
        # wrapper is no different), not just this function's callers (Codex
        # review: "Classify unhashable agent metadata as rogue"). An unusable
        # placed_agent classifies as rogue, the same fail-safe direction
        # every other malformed field in this class takes -- never silently
        # compatible, and never a crash.
        is_recognized_agent = isinstance(o.placed_agent, str) and o.placed_agent in compatible_placed_agent_values
        if is_recognized_agent:
            try:
                # The coordination carve-out exists only for tickers the
                # OTHER DCA executor already BOUGHT in this window -- a sell
                # (or a row whose side is too malformed for is_buy_attempt to
                # even classify, which raises ValueError the same way
                # _normalize_side() does elsewhere in this module) from a
                # recognized compatible agent is not that, and letting it
                # into `compatible` would wrongly exclude its ticker from
                # this run's own Gate 5 candidates as "already covered" when
                # nothing was bought. Every other unexpected writer on this
                # account is already treated as a collision; a malformed- or
                # wrong-side row from a compatible agent gets the same
                # fail-closed rogue classification, not a silent pass-through
                # (Codex review: "Require a valid buy side for compatible
                # orders").
                is_recognized_buy = o.is_buy_attempt
            except ValueError:
                is_recognized_buy = False
            if is_recognized_buy:
                compatible.append(o)
                continue
        rogue.append(o)
    return SoleExecutorResult(rogue=rogue, compatible=compatible)
```

Call sites that don't pass `compatible_placed_agent_values` get `SoleExecutorResult(rogue=<old
return value>, compatible=[])` — same abort/no-abort outcome as today, just wrapped. The
midday live-mirror prompt is the only proposed adopter initially:

```python
result = r.gate_sole_executor(orders, slot, day, compatible_placed_agent_values=COMPATIBLE_VALUES)
if result.rogue:
    # abort and alert, exactly as today
if result.compatible:
    # note it, then exclude result.compatible_tickers from Gate 5's candidate list
```

This exactly mirrors the six standard prompts' §3 prose, expressed as tested code instead of
duplicated text — and it's now available to any *future* Python-code call site too, not just
prompt text.

**A second call site needs the same parameter, or this carve-out doesn't survive to placement.**
`execute_plan()` reruns `gate_sole_executor(orders, slot, trading_day)` itself, undocumented above
until now, immediately before submitting (its own docstring: "Gate 4 ... is rerun on the freshly
materialized `orders` below"). Without a matching `compatible_placed_agent_values` there, the same
rows Gate 4 above just classified as `compatible` are reclassified as `rogue` at this second,
placement-time interlock, and the batch aborts before submitting anything — silently defeating the
partial-batch recovery this proposal exists to enable (Codex review: "Plumb compatibility through
the placement-time interlock"). `execute_plan()` needs its own `compatible_placed_agent_values:
frozenset[str] = frozenset()` keyword-only parameter, threaded into its internal
`gate_sole_executor()` call exactly as shown above, defaulting to the same empty set so every
existing caller that doesn't pass it keeps today's behavior unchanged. The midday caller must pass
the identical `COMPATIBLE_VALUES` to both calls; passing it to Gate 4 above but not to
`execute_plan()` (or vice versa) reintroduces this same bug in one direction or the other.

**Threading the parameter through alone is not sufficient — it closes the wrong failure mode.**
If the compatible executor places a NEW order, on a ticker Gate 4's earlier check never saw, in
the gap between Gate 4 and this placement-time rerun, `__bool__` on the fresh
`gate_sole_executor()` result correctly stays falsy (the new row lands in `.compatible`, not
`.rogue`) — but the `plan` being submitted was already built from Gate 4's OLDER exclusion set,
which didn't know about that ticker yet, so it can still be sitting in `plan.to_place`. Checking
only `.rogue`/`__bool__` decides whether to abort the WHOLE batch; it does nothing to remove one
newly-covered ticker from a plan built before that coverage existed, so the batch proceeds and
submits a duplicate same-window buy anyway (Codex review: "Recheck compatible tickers at placement
time").

**Refusing the stale `plan.to_place` entry by itself is not enough either — it collides with
`execute_plan()`'s own existing consistency check.** `execute_plan()` already rebuilds an
`expected_plan` by calling `plan_orders()` again on its own `candidates` argument (the untouched
full list passed in at the top of the call, not `plan.to_place`), then requires
`plan.to_place == expected_plan.to_place`, in order, cent-exact (`dca_runner.py`'s own
`submitted_sequence != expected_sequence` check). Removing a newly-compatible ticker from
`plan.to_place` alone, without also removing its `Candidate` from `candidates`, makes
`expected_plan` still contain it — the two sequences no longer match, and that check raises
`ValueError`, aborting the ENTIRE batch exactly like an unhandled `.rogue` collision would (Codex
review: "Rebuild the verified plan after compatibility exclusions").

**Filtering both lists in parallel is still not sufficient — a cap-boundary promotion breaks it
too.** Say `candidates` = [A, B], the ORIGINAL `plan_orders()` call selected A (headroom allows it)
and skipped B (no headroom left), so `plan.to_place` = [A]. If the compatible executor's own
fresh A order is smaller than this run's planned A amount, filtering A out of both `candidates`
(-> [B]) and `plan.to_place` (-> []) does NOT produce a match: `expected_plan` is rebuilt from
`plan_orders([B], ...)`, and with A no longer competing for headroom, B can now fit -- promoting
`expected_plan.to_place` to [B], while the merely-trimmed old `plan.to_place` is still []. `[] !=
[B]`, and the exact-sequence check aborts the entire batch again, exactly the failure this fix
was meant to close (Codex review: "Rebuild the plan after compatibility exclusions"). Filtering the
OLD plan's entries can never account for a promotion `plan_orders()` itself would only discover by
actually re-running over the reduced candidate set -- there is no way to patch the old `Plan`
object into consistency after the fact. The caller must instead REBUILD its own submitted `plan`
by calling `plan_orders()` itself, fresh, on the compatible-ticker-filtered `candidates`, using the
SAME `verified_snapshot`/`tier`/cap arguments `execute_plan()` will independently use for
`expected_plan` -- not merely trim the ORIGINAL `plan.to_place`. Because both the caller's plan and
`execute_plan()`'s own `expected_plan` are then deterministic functions of the identical filtered
`candidates` (and the identical snapshot/tier/caps), they necessarily agree, cap-boundary
promotions included, without `execute_plan()` itself needing to change at all -- for a ticker the
CALLER already knew was compatible. Report each excluded ticker the same way as any other
placement-time refusal — this is a real, if partial, batch reduction, not a silent drop. Both
checks are required together: `.rogue` non-empty still aborts the entire batch (a genuine
third-executor collision), while a ticker newly reclassified as `.compatible` since Gate 4 is
excluded from the candidate set the resubmitted plan is built from, letting the rest of the batch
— cap-boundary promotions and all — proceed correctly.

**A prior draft of this section claimed a THIRD gap here that genuinely needed
`execute_plan()` itself to change (Codex review: "Filter fresh compatible orders inside
execute_plan"). That claim rested on a false premise and has been corrected (Codex review: "Do
not assume execute_plan refetches order history").** `execute_plan()` has no fetch callable of
its own: its "internal Gate 4 rerun" only reruns `gate_sole_executor()` against whatever `orders`
iterable the CALLER passes as the `orders` argument (materialized once via `orders = list(orders)`
in `dca_runner.py`), the exact same object the caller already used for its own rebuild above --
there is no independent, later fetch happening inside `execute_plan()` for a compatible order to
land in the gap before. Consequently, no order that lands after the caller's own snapshot can ever
appear inside `execute_plan()`'s rerun UNLESS the caller itself fetches a second, fresher snapshot
and passes THAT to `execute_plan()` instead of the one it filtered `candidates` against. If the
caller does exactly that -- re-fetches once more immediately before calling `execute_plan()` -- it
can close this residual gap entirely on the caller's own side, with no `execute_plan()` code
change required: repeat the SAME `.compatible`/`.rogue` filter-and-rebuild step (per the two
sections above) against that final fetch, and pass that identical final `orders` snapshot to
`execute_plan()` as well, so the caller's own `candidates`/`plan.to_place` and `execute_plan()`'s
internally-rebuilt `expected_plan` are deterministic functions of the SAME data by construction.
The residual risk this leaves is only the ordinary one already acknowledged elsewhere in this
proposal: an order landing in the (bounded, freshness-checked) interval between that final fetch
and the actual broker submission, not a distinct gap requiring new `execute_plan()` machinery.

**Explicitly out of scope for this proposal:** migrating the six existing prompts from their
working prose version to this new function parameter. They function correctly today; touching
them is a second, separate, lower-priority follow-up, not a prerequisite.

## 4. Five-task → seven-slot transition sequencing

`claude/PERPLEXITY-CLAUDE-COORDINATION.md` §6 already superseded the old "transitional, then
hard cutover" framing with "the two executors are meant to coexist indefinitely." This section
proposes the concrete sequence to get there, since "deprecate the 5-slot workflow" implies an
end state that doesn't exist as an ordered plan yet:

1. **Confirm `PERPLEXITY_PLACED_AGENT_VALUE` (Gap B).** Blocking, first, and not doable from
   this repo — needs a session with live `get_equity_orders` access cross-referenced against
   known Perplexity fill timestamps. Nothing below can be verified as *working* (as opposed to
   merely written) until this lands.
2. **Wire Gate 5 for at least one slot.** Separate, larger, already-tracked problem, but not
   uniform across the roster: `scripts/generate_buy_candidates.py` is already a committed Gate 5
   producer, registered in `scripts/agent_entry_points.py`, for the 1530ET slot only. The other
   six slots (0935/1030/1130/1230/1330/1430ET) have no signal-generation entry point at all —
   `run_dca.py`'s CLI is dry-run only, and none of `MorningDCAExecutor`'s subclasses have a live
   caller (this is what `GATE5-PROPOSAL.md` used to document before its removal). This proposal
   does not resolve either gap. Two independent tracks follow from that: wire the existing
   1530ET producer into its live prompt, and separately pick one low-risk slot among the other
   six (midday, since its placement is already OFF pending validation regardless) as the pilot
   for building a new producer. [Historical: as of 2026-09-02 the midday slot has its own
   committed producer (`scripts/generate_midday_candidates.py`) and its live-mirror placement
   has been authorized on, per Geoff's direction that placement be on for all scheduled buy
   runs -- see `docs/safeguards.md#live-execution-roster` and
   `claude/dca-executor-prompts/README.md`. This proposal's sequencing predates that; left
   as written for history.]
3. **Implement §3's `gate_sole_executor()` change**, with its own tests, independent of (1)/(2)
   — it's pure logic and can be verified with synthetic `BrokerOrder` fixtures before any real
   coordination is live.
4. **Turn on the midday slot's placement** only after (1)-(3) are all independently true, per
   the existing Gate 7 placement-integration gap this same PR has been documenting (the
   `place_fn`/MCP-callable-across-process-boundary problem) — that gap is unrelated to
   coordination and still needs its own resolution regardless of this proposal.
5. **Repeat (2)-(4) per slot**, not all seven at once. Each slot going live is an independent
   decision with its own review, matching CLAUDE.md's "consolidating is a decision, not a
   cleanup" standard applied to activation, not just refactoring.
6. **Only after every slot Perplexity currently covers has a verified, live, coordinating
   Claude counterpart** does retiring the 5-task roster become a live option, and even then
   `docs/safeguards.md`'s roster section should be updated as a deliberate step, not an
   automatic consequence of the last slot going live — per the coordination doc's own §6, the
   long-term intent is coexistence with Perplexity as backup, not full retirement, unless
   Geoff decides otherwise at that point.

**What "deprecate the 5-slot workflow" should NOT mean:** turning off Perplexity's tasks
before step 6. `docs/safeguards.md` is explicit that Perplexity's role must not be retired
without a working replacement, "or the account is left with zero functioning scheduled
execution." Every step above preserves Perplexity as the working fallback until its Claude
counterpart is independently verified, not just written.

## 5. What this document does not do

It does not implement §3's code change, does not wire Gate 5 for any slot, does not confirm
`PERPLEXITY_PLACED_AGENT_VALUE`, and does not change any live scheduled task. It is the design
for review requested before any of that happens.
