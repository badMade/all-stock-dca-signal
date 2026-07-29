"""Two hard ceilings -- turn count and cumulative tokens -- enforced by
exception rather than return code, so a breach can't be silently ignored.

This is the financial safety component of the production-safe agent loop.
"""

from dataclasses import dataclass


@dataclass
class CircuitBreakerError(Exception):
    """Raised when a turn or token ceiling is breached.

    Attributes:
        reason: Either "turn_ceiling" or "token_ceiling".
        turn_count: The turn count at the moment of the breach.
        accumulated_tokens: The cumulative token count at the moment of the
            breach.
    """

    reason: str
    turn_count: int
    accumulated_tokens: int

    def __post_init__(self) -> None:
        super().__init__(
            f"circuit breaker tripped: {self.reason} "
            f"(turn={self.turn_count}, tokens={self.accumulated_tokens})"
        )


class CircuitBreaker:
    """Enforces a turn-count ceiling and a cumulative-token ceiling.

    Args:
        turn_limit: Maximum allowed turn count (inclusive).
        token_limit: Maximum allowed cumulative token count (inclusive).
    """

    def __init__(self, turn_limit: int = 5, token_limit: int = 15000) -> None:
        self.turn_limit = turn_limit
        self.token_limit = token_limit

    def check(self, turn_count: int, accumulated_tokens: int) -> None:
        """Raise CircuitBreakerError if either ceiling is exceeded.

        Checks the turn ceiling first: if both ceilings are breached in the
        same call, "turn_ceiling" is raised and the token ceiling is never
        evaluated.

        Args:
            turn_count: The current turn count.
            accumulated_tokens: The current cumulative token count.

        Side effects:
            Prints a checkpoint banner to stdout before raising, so the
            breach is visible even if a caller catches and swallows the
            exception.
        """
        if turn_count > self.turn_limit:
            self._trip("turn_ceiling", turn_count, accumulated_tokens)
        if accumulated_tokens > self.token_limit:
            self._trip("token_ceiling", turn_count, accumulated_tokens)

    def _trip(self, reason: str, turn_count: int, accumulated_tokens: int) -> None:
        """Print the checkpoint banner and raise CircuitBreakerError."""
        print(
            "\n=== CIRCUIT BREAKER CHECKPOINT ===\n"
            f"reason         : {reason}\n"
            f"turn_count     : {turn_count} / limit {self.turn_limit}\n"
            f"tokens_used    : {accumulated_tokens} / limit {self.token_limit}\n"
            "action         : halt loop, surface to human reviewer\n"
            "=================================="
        )
        raise CircuitBreakerError(
            reason=reason,
            turn_count=turn_count,
            accumulated_tokens=accumulated_tokens,
        )
