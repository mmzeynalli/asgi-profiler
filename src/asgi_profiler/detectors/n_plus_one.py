"""The same statement, over and over, from the same line."""

from __future__ import annotations

from collections.abc import Iterator

from ..models import Problem, Profile, Query
from .settings import Settings
from .utils import common_frames, fingerprint, total_span_ms

TYPE = "n_plus_one_db"

#: Offender lists are stored with the profile and rendered into the page. A
#: 500-row N+1 does not need 500 indices to make its point.
_MAX_OFFENDERS = 50

#: Transaction and session bookkeeping, which is repetitive by nature and
#: never an N+1. `SAVEPOINT sa_1` and `SAVEPOINT sa_2` normalise to the same
#: statement -- correctly, they *are* the same statement -- so without this
#: five nested `begin_nested()` blocks would be reported as a loop issuing
#: the same query five times. Which is true, and is not a finding.
_BOOKKEEPING = frozenset(
    {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "SET", "PRAGMA", "USE"}
)


def detect(profile: Profile, settings: Settings) -> Iterator[Problem]:
    """Repeated identical statements sharing a call site.

    `Profile.duplicate_count` already counts repeats, and counting is not the
    same as concluding: two identical `SELECT`s are a coincidence, and five
    hundred from inside one loop are a bug. Three conditions separate them.

    **Enough of them.** `n_plus_one_count` repeats, default five, matching the
    threshold Sentry settled on.

    **Costing enough.** `n_plus_one_ms` of *non-overlapping* time, default
    fifty. The union rather than the sum, so a deliberate `asyncio.gather`
    fan-out is not reported as a problem for being fast.

    **From one place.** Every execution must share a stack prefix, and the
    deepest shared frame is what the problem is named after. This is the test
    Sentry cannot make -- its database spans carry no Python stack, so it
    falls back to the parent span and reports the query. Here the finding can
    name the line, which is the difference between "you have an N+1" and
    "line 20 of repository.py is your N+1".

    When stacks are off (`capture_stacks=False`) the shared-call-site test is
    skipped rather than failed, and the problem is fingerprinted on the route
    instead. Less precise, still true.
    """
    groups: dict[str, list[int]] = {}
    for index, query in enumerate(profile.queries):
        if not query.truncated and query.operation not in _BOOKKEEPING:
            groups.setdefault(query.sql_hash, []).append(index)

    for key, indices in groups.items():
        if len(indices) < settings.n_plus_one_count:
            continue
        queries = [profile.queries[i] for i in indices]
        span_ms = total_span_ms(queries)
        if span_ms < settings.n_plus_one_ms:
            continue

        frame = _call_site(queries)
        if settings.stacks and not frame:
            # Stacks were being captured and these executions share no
            # application frame -- either they came from unrelated places (a
            # serialiser and a permission check that happen to ask the same
            # question are not a loop) or from inside a library, where there
            # is no line of the user's to change. Reporting either would send
            # someone looking for a loop that does not exist.
            continue

        source = _source_query(profile, indices[0], key)
        total_ms = sum(q.duration_ms for q in queries)
        yield Problem(
            type=TYPE,
            fingerprint=fingerprint(
                TYPE,
                profile.group,
                frame,
                source.sql_hash if source else "",
                key,
            ),
            title=f"N+1 query: {len(indices)} identical statements",
            detail=_detail(len(indices), span_ms, total_ms, frame),
            evidence={
                "sql": queries[0].sql,
                "count": len(indices),
                "span_ms": round(span_ms, 3),
                "total_ms": round(total_ms, 3),
                "avg_ms": round(total_ms / len(queries), 3),
                "frame": frame,
                "source_sql": source.sql if source else "",
                "source_frame": source.stack[-1] if source and source.stack else "",
            },
            offenders=indices[:_MAX_OFFENDERS],
            time_ms=span_ms,
        )


def _call_site(queries: list[Query]) -> str:
    """The deepest frame every one of these executions came through.

    Empty when they do not all have stacks, or share no frame at all. The
    caller decides what that means: with stacks off it is expected, and with
    stacks on it is a reason not to report.
    """
    stacks = [q.stack for q in queries if q.stack]
    if len(stacks) != len(queries):
        return ""
    shared = common_frames(stacks)
    return shared[-1] if shared else ""


def _source_query(profile: Profile, first: int, key: str) -> Query | None:
    """The last different statement before the repeats began.

    Almost always the query that loaded the rows being looped over, which is
    the other half of the story: these five hundred statements exist because
    that one returned five hundred rows.
    """
    for index in range(first - 1, -1, -1):
        candidate = profile.queries[index]
        if candidate.sql_hash != key:
            return candidate
    return None


def _detail(count: int, span_ms: float, total_ms: float, frame: str) -> str:
    where = f" issued from {frame}" if frame else ""
    return (
        f"The same statement ran {count} times{where}, taking {span_ms:.1f} ms "
        f"of wall time ({total_ms:.1f} ms summed). Load the relationship in one "
        f"query -- `selectinload`/`joinedload` for a read, a bulk insert for a write."
    )
