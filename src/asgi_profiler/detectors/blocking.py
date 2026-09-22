"""Synchronous database calls made on the event loop."""

from __future__ import annotations

from collections.abc import Iterator

from ..models import Problem, Profile
from .settings import Settings
from .utils import fingerprint, total_span_ms

TYPE = "blocking_db_call"


def detect(profile: Profile, settings: Settings) -> Iterator[Problem]:
    """Statements that stopped the event loop while they ran.

    The mistake this catches is quiet and extremely common: a synchronous
    `Session` used inside an `async def` endpoint. Nothing raises, the tests
    pass, and the endpoint looks fine in isolation -- because in isolation it
    *is* fine. The damage only appears under concurrency, where every other
    request in flight is frozen for the duration of every query, and it
    surfaces as latency on endpoints that have nothing to do with the
    offending one. That is close to impossible to attribute from the outside,
    which is why nothing else reports it: a production tracer sees the symptom
    on the wrong endpoint and the cause on none.

    `Query.blocking` is decided at capture time, where the thread and the
    greenlet are still visible -- see `instrument._blocks_the_loop`. What is
    left here is arithmetic.

    **One problem per request, not one per call site.** The first version of
    this split by the innermost frame, and a single endpoint that blocked for
    30 ms reported two findings of 1 ms and 0 ms -- both true, neither the
    thing the reader needed, which is how long their event loop was actually
    frozen. The fix is the same wherever in the request it happens (an async
    engine, `run_in_threadpool`, or a `def` endpoint), so splitting the
    finding only splits the number that makes the case for doing it.
    """
    offenders = [i for i, query in enumerate(profile.queries) if query.blocking]
    if not offenders:
        return
    queries = [profile.queries[i] for i in offenders]
    span_ms = total_span_ms(queries)
    if span_ms < settings.blocking_ms:
        return

    # Attributed by total time rather than by count: one 200 ms report query
    # is the thing to fix, not the forty 1 ms lookups around it.
    sites: dict[str, float] = {}
    for query in queries:
        frame = query.stack[-1] if query.stack else ""
        sites[frame] = sites.get(frame, 0.0) + query.duration_ms
    worst = max(sites, key=lambda frame: sites[frame])
    statements = "statement" if len(offenders) == 1 else "statements"

    yield Problem(
        type=TYPE,
        fingerprint=fingerprint(TYPE, profile.group, worst),
        # Counted, not timed. The duration is in the detail and in `time_ms`;
        # leading with it would print "Blocking database call: 0.4 ms" for a
        # sync session on a development database -- which reads as trivia, and
        # is the exact case worth catching, because the same code against a
        # real database is a hundred times that number.
        title=f"Blocking database call: {len(offenders)} {statements} on the event loop",
        detail=_detail(len(offenders), span_ms, profile.duration_ms, worst, len(sites)),
        evidence={
            "sql": queries[0].sql,
            "count": len(offenders),
            "span_ms": round(span_ms, 3),
            "frame": worst,
            "call_sites": len(sites),
        },
        offenders=offenders[:50],
        time_ms=span_ms,
    )


def _detail(count: int, span_ms: float, request_ms: float, frame: str, sites: int) -> str:
    statements = "statement" if count == 1 else "statements"
    where = f", mostly from {frame}" if frame else ""
    elsewhere = f" across {sites} call sites" if sites > 1 else ""
    share = f" ({span_ms / request_ms:.0%} of the request)" if request_ms else ""
    return (
        f"{count} {statements}{elsewhere} ran synchronously on the event loop "
        f"thread{where}, holding it for {span_ms:.1f} ms{share}. Every other request "
        f"in flight waited that long. Use an async engine with `AsyncSession`, wrap "
        f"the call in `starlette.concurrency.run_in_threadpool`, or make the endpoint "
        f"`def` rather than `async def` -- Starlette already runs those in a worker "
        f"thread."
    )
