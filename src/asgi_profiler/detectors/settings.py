"""Thresholds the detectors run against.

Separate from :class:`~asgi_profiler.config.ProfilerConfig` so a detector can
be called on a profile without constructing an application's worth of
configuration -- in a test, from a script reading a capture file, or from the
CLI. `ProfilerConfig.detector_settings()` builds one.

The defaults are Sentry's where Sentry has one and the reasoning carries over,
and lower where it does not. Sentry calls a query slow at 1000 ms because it
is watching production and wants a handful of issues a day. This runs on a
development machine against a development database, where 1000 ms means the
slow query has to be catastrophic before anyone hears about it.

`n_plus_one_ms` is zero for a stronger version of the same reason. Sentry
requires 50 ms of database time before it calls repetition an N+1, which is
sound when the database it is watching holds the production dataset. It is
exactly wrong here. The whole value of finding an N+1 in development is that
it has not reached production yet -- and in development the table has twelve
rows, so five hundred repeated queries return in four milliseconds and a
duration floor is a filter that removes the findings you most wanted. The
count and the shared call site are the signal; duration is available to raise
when profiling against production-sized data, where the noise floor is real.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Settings"]

#: The defaults, as values rather than only as dataclass fields.
#:
#: `ProfilerConfig` exposes the same four thresholds to users and has to
#: default them to the same numbers. Writing the numbers twice meant they
#: could disagree, and they promptly did: a default changed here went on
#: being overridden by the stale copy there, so the detector silently ran
#: with the old threshold and the only symptom was a finding that never
#: appeared. One definition, referenced twice.
N_PLUS_ONE_COUNT = 5
N_PLUS_ONE_MS = 0.0
SLOW_QUERY_MS = 100.0
BLOCKING_MS = 0.0


@dataclass(frozen=True, slots=True)
class Settings:
    """Detector thresholds.

    Args:
        enabled: detector type names to run. `None`, the default, runs all of
            them. An unknown name is ignored rather than raising, so removing
            a detector in a later release cannot break an application that
            named it.
        n_plus_one_count: how many identical statements make an N+1.
        n_plus_one_ms: how much non-overlapping time they must occupy. Zero
            by default, and deliberately: see below.
        stacks: whether stacks were captured. When they were, a repeated
            statement with no shared application frame is not reported --
            that is library bookkeeping, not a loop in your code.
        slow_query_ms: a single statement at or over this is a problem.
        blocking_ms: how much loop-blocking time a request needs before it
            is reported. Zero by default: a synchronous driver call inside an
            `async def` endpoint is the wrong shape whether the development
            database answers in one millisecond or the production one takes a
            hundred, and the millisecond version is the one there is still
            time to fix. Raise it to silence a call you have decided to keep.
    """

    enabled: Sequence[str] | None = None
    n_plus_one_count: int = N_PLUS_ONE_COUNT
    n_plus_one_ms: float = N_PLUS_ONE_MS
    slow_query_ms: float = SLOW_QUERY_MS
    blocking_ms: float = BLOCKING_MS
    stacks: bool = True

    def runs(self, name: str) -> bool:
        return self.enabled is None or name in self.enabled
