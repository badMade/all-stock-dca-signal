"""Per-(day, slot) placement claim for the simultaneous-start guard.

DCA Issues keys ``duplicate_fills`` and ``competing_executor``: two executors that start at the same moment
can both read an empty same-window order history and both place the same buys. ``dca_runner.execute_plan``
takes this claim before its first order so a second in-repo engine for the same day and slot is refused
instead of racing it (``docs/adr-simultaneous-start-guard.md``, layer A). The claim is a
``scheduler_lease`` file under the caller's claim root, so acquisition is atomic and a crashed holder's claim
is reclaimable only after its TTL.

Fail closed: a claim root that cannot be created, a lock that cannot be taken, or a lease that is held all
raise :class:`SlotClaimUnavailable`, and the caller places nothing. Standard library only; no network and no
credentials. The claim covers engines that share this filesystem; a cloud-only backup has no shared medium
and relies on the per-order broker-history re-pull instead.
"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path
from typing import Iterator, Optional

from scripts.scheduler_lease import SchedulerLease, window_key

STREAM = "placement-claim"
#: Longer than a slot's acceptance window, so a live batch never loses its claim; a crashed holder blocks
#: only its own (day, slot) until this lapses.
CLAIM_TTL_SECONDS = 1800


class SlotClaimUnavailable(RuntimeError):
    """The slot's claim is held elsewhere or cannot be proven; place nothing."""


@contextlib.contextmanager
def slot_claim(
    root: str | Path,
    trading_day: str,
    slot: str,
    *,
    holder: Optional[str] = None,
    ttl_seconds: int = CLAIM_TTL_SECONDS,
    now: Optional[float] = None,
) -> Iterator[str]:
    """Hold the (day, slot) placement claim for the body; yields the holder id.

    A unique suffix is appended to ``holder`` so two runs given the same label still exclude each other
    (``scheduler_lease`` treats a same-holder re-acquire as success).
    """
    holder_id = f"{holder or 'run'}-{uuid.uuid4().hex}"
    try:
        lease = SchedulerLease(Path(root), window_key(trading_day, STREAM, slot))
        result = lease.acquire(holder_id, ttl_seconds=ttl_seconds, now=now)
    except (OSError, TimeoutError) as exc:
        raise SlotClaimUnavailable(f"slot claim cannot be proven ({type(exc).__name__})") from exc
    if not result.acquired:
        raise SlotClaimUnavailable(f"slot claim held by another run ({result.reason})")
    try:
        yield holder_id
    finally:
        with contextlib.suppress(OSError, TimeoutError):
            lease.release(holder_id)
