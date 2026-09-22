"""Shared arithmetic for the detectors."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from hashlib import sha1

from ..models import Query

__all__ = ["common_frames", "fingerprint", "total_span_ms"]

#: Bump when the *meaning* of a fingerprint changes, so that problems recorded
#: by an older version never silently merge with problems recorded by a newer
#: one. Two groups where there should be one is visible and fixable; one group
#: where there should be two is neither.
SCHEME = "1"


def total_span_ms(queries: Iterable[Query]) -> float:
    """Wall-clock time these statements occupied, counting overlap once.

    Summing durations is the obvious thing and it is wrong here. Under
    `asyncio.gather` twenty queries can run concurrently in 40 ms of real
    time and sum to 700 ms; a threshold on the sum would call that an N+1
    worth fixing, when the concurrency *is* the fix. The union of the
    intervals is what the user actually waited.
    """
    spans = sorted((q.started_ms, q.ended_ms) for q in queries)
    if not spans:
        return 0.0
    total = 0.0
    start, end = spans[0]
    for next_start, next_end in spans[1:]:
        if next_start > end:
            total += end - start
            start, end = next_start, next_end
        else:
            end = max(end, next_end)
    return total + (end - start)


def common_frames(stacks: Sequence[Sequence[str]]) -> list[str]:
    """The frames every one of these stacks begins with.

    Stacks are stored innermost-last, so the shared *prefix* is the outer
    frames -- the request handler, the service, the function containing the
    loop -- and the last frame of that prefix is the deepest line all the
    executions came through. For `for row in rows: row.owner.name` that is the
    loop itself, which is the line someone has to edit.
    """
    if not stacks:
        return []
    shortest = min(len(s) for s in stacks)
    shared: list[str] = []
    for depth in range(shortest):
        frame = stacks[0][depth]
        if any(stack[depth] != frame for stack in stacks):
            break
        shared.append(frame)
    return shared


def fingerprint(kind: str, *parts: str) -> str:
    """A stable identity for a problem, across requests and across runs.

    The `SCHEME-kind-` prefix is not decoration. It makes the hash readable
    enough to grep for, keeps two detectors from ever colliding, and gives a
    place to record that the grouping rule changed.
    """
    digest = sha1("|".join(parts).encode("utf-8", "replace"), usedforsecurity=False)
    return f"{SCHEME}-{kind}-{digest.hexdigest()[:16]}"
