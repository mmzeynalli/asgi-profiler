"""One statement that is slow on its own."""

from __future__ import annotations

from collections.abc import Iterator

from ..models import Problem, Profile
from .settings import Settings
from .utils import fingerprint

TYPE = "slow_db_query"


def detect(profile: Profile, settings: Settings) -> Iterator[Problem]:
    """Statements over `slow_query_ms`, one problem per distinct statement.

    Reported per statement rather than per execution: an endpoint that runs
    the same slow query three times has one problem to fix, not three.

    Truncated statements are skipped. Their stored text ends in the original
    character count, so two executions of the same oversized statement hash
    differently and the "problem" would be a new one every time -- worse than
    silence, because it would fill the issues page with noise that never
    resolves.
    """
    threshold = settings.slow_query_ms
    worst: dict[str, tuple[int, float]] = {}
    hits: dict[str, list[int]] = {}
    for index, query in enumerate(profile.queries):
        if query.duration_ms < threshold or query.truncated:
            continue
        key = query.sql_hash
        hits.setdefault(key, []).append(index)
        if key not in worst or query.duration_ms > worst[key][1]:
            worst[key] = (index, query.duration_ms)

    for key, (index, duration) in worst.items():
        query = profile.queries[index]
        occurrences = hits[key]
        times = "once" if len(occurrences) == 1 else f"{len(occurrences)} times"
        yield Problem(
            type=TYPE,
            fingerprint=fingerprint(TYPE, key),
            title=f"Slow {query.operation}: {duration:.0f} ms",
            detail=(
                f"One statement took {duration:.1f} ms, which is "
                f"{duration / profile.duration_ms:.0%} of the request. "
                f"It ran {times}."
            ),
            evidence={
                "sql": query.sql,
                "duration_ms": round(duration, 3),
                "occurrences": len(occurrences),
                "frame": query.stack[-1] if query.stack else "",
                "operation": query.operation,
            },
            offenders=occurrences[:50],
            time_ms=duration,
        )
