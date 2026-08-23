# production-safe-agent-loop

Two small, independent safety primitives for running an AI agent loop against something that
costs real money if it runs away: a hard circuit breaker, and a forced pre-flight spec.

This is a sibling of `ticker-advisor/` and `trading-strategies/`: a self-contained subproject
with its own `pyproject.toml`/`requirements.txt`/tests, excluded from the core repo's
stdlib-only ruff and coverage gates (see the root `CLAUDE.md`). Neither module here is imported
by any core DCA strategy code today — this is a reusable library an operator wires into an
*agent session*, not a component the trading strategy itself calls.

## What's here

### `circuit_breaker.py` — turn/token ceilings

Two hard ceilings — turn count and cumulative tokens — enforced by raising `CircuitBreakerError`
rather than returning a status code, so a breach can't be silently ignored or logged-and-continued
by accident.

```python
from circuit_breaker import CircuitBreaker, CircuitBreakerError

breaker = CircuitBreaker(turn_limit=5, token_limit=15000)  # both default to these values
try:
    breaker.check(turn_count=current_turn, accumulated_tokens=tokens_so_far)
except CircuitBreakerError as exc:
    # exc.reason is "turn_ceiling" or "token_ceiling"; exc.turn_count / exc.accumulated_tokens
    # are the values at the moment of the breach. Stop the loop here.
    ...
```

Call `.check()` immediately before doing anything that costs money or places an order, with
`accumulated_tokens` already including every token-producing call made so far *this turn* —
not the total as it stood when the turn began. `check()` is a stateless comparison against
whatever the caller passes in; it has no way to know a turn's own model call pushed the total
over `token_limit` if that call's tokens aren't folded into `accumulated_tokens` before the
check runs. A single call at the top of the turn, using last turn's total, lets that turn's own
token-producing call complete and its costly action fire before the breach is ever seen — the
ceiling is only enforced one turn late. The turn ceiling is checked first — if both ceilings are
breached in the same call, `"turn_ceiling"` is raised and the token ceiling is never evaluated.

### `spec_writer.py` — forced pre-flight spec

Interviews the operator for three one-sentence answers — what a task does, what it explicitly
does *not* do, and what "done" looks like — before an agent loop is allowed to start, and
persists the answers to SQLite keyed by a fresh session id. The point isn't the storage; it's
that a loop can't begin without those three sentences existing somewhere, which forces scope to
be stated instead of assumed.

```python
from spec_writer import SpecWriter

writer = SpecWriter(db_path="specs.sqlite3", input_fn=input)  # input_fn is any Callable[[str], str]
spec = writer.run()
# spec.session_id, spec.what_it_does, spec.what_it_does_not, spec.done_looks_like
```

## Install & test

```bash
cd production-safe-agent-loop
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/
```

## Where this could plug in (not wired today)

Nothing in this repo currently instantiates either class. The natural fit, if wired, is around
the *agent sessions* that drive the live-mirror executor prompts
(`claude/dca-executor-prompts/live-mirror/*.txt`) — not the DCA strategy code itself, which is
already a plain function pipeline with its own gate sequence (see `docs/live_wiring.md`) and has
no concept of "turns" or "tokens" to begin with. Wiring either primitive into a live executor
session is a deliberate integration decision with its own review, the same way the seven-slot
roster's Gate 5/Gate 7 wiring is (see `PRODUCTION_READINESS.md`) — not something this README
proposes doing unilaterally.
