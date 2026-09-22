"""Recorded data structures."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .fingerprint import is_truncated, sql_hash

#: Statements longer than this are stored truncated. An ORM bulk insert can
#: produce a single statement tens of kilobytes long, and `max_requests`
#: bounds the number of requests kept, not their size -- without a cap one
#: pathological endpoint can hold hundreds of megabytes in the ring buffer.
MAX_SQL_CHARS = 4000


@dataclass(slots=True)
class Query:
    """One SQL statement executed during a request."""

    sql: str
    params: str
    duration_ms: float
    stack: list[str] = field(default_factory=list)
    #: Milliseconds from the start of the request to the start of this
    #: statement. Two statements overlap when one starts before the other
    #: ends, which is the whole basis of telling "six queries that ran one
    #: after another" apart from "six queries that ran at once" -- the first
    #: is worth 300 ms of someone's afternoon and the second is not.
    started_ms: float = 0.0
    #: The statement ran on the event loop thread, outside SQLAlchemy's async
    #: bridge: a synchronous driver call that stopped the loop dead for its
    #: whole duration. See :mod:`asgi_profiler.detectors.blocking`.
    blocking: bool = False
    #: Set by :meth:`Profile.finalise` once the whole request is known.
    is_duplicate: bool = False
    #: The exception text, when the statement failed. `None` means it succeeded.
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def ended_ms(self) -> float:
        return self.started_ms + self.duration_ms

    @property
    def sql_hash(self) -> str:
        """Identity of the statement, ignoring per-execution values."""
        return sql_hash(self.sql)

    @property
    def truncated(self) -> bool:
        return is_truncated(self.sql)

    @property
    def operation(self) -> str:
        head = self.sql.lstrip().split(" ", 1)[0].upper()
        return head if head.isalpha() else "SQL"


@dataclass(slots=True)
class Problem:
    """Something the detectors concluded about a request.

    The difference between a profiler and a report: `duplicate_count = 11` is
    a number the reader has to interpret, and "11 identical queries issued
    from repository.py:20, 57.6 ms -- load the relationship instead" is a
    finding they can act on.

    `fingerprint` is the identity of the *problem*, not of the request: the
    same N+1 seen on four hundred requests has one fingerprint, which is what
    lets the viewer collapse them into a single row and what lets CI say "this
    is not new".
    """

    type: str
    fingerprint: str
    title: str
    detail: str = ""
    #: Named facts for the template: the source statement, the offending
    #: frame, how many times it repeated. Display data, not API.
    evidence: dict[str, Any] = field(default_factory=dict)
    #: Indices into :attr:`Profile.queries`, so the detail page can highlight
    #: the statements this is about without storing them twice.
    offenders: list[int] = field(default_factory=list)
    #: Time attributable to the problem. Not always time you would get back by
    #: fixing it -- for consecutive queries that is a separate number -- but
    #: always the size of the thing being pointed at.
    time_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "offenders": self.offenders,
            "time_ms": round(self.time_ms, 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Problem:
        return cls(
            type=str(data.get("type", "")),
            fingerprint=str(data.get("fingerprint", "")),
            title=str(data.get("title", "")),
            detail=str(data.get("detail", "")),
            evidence=dict(data.get("evidence") or {}),
            offenders=list(data.get("offenders") or []),
            time_ms=float(data.get("time_ms") or 0.0),
        )


@dataclass(slots=True)
class QueryGroup:
    """Identical statements within one request, collapsed into a single row.

    A 500-row N+1 renders 500 near-identical list items with the same stack
    repeated under each -- 447 kB of HTML that you have to scroll past to
    find the line that caused it. Collapsing is the difference between
    "I saw the N+1" and "I found it".

    Grouped by :attr:`Query.sql_hash` rather than by literal text, so a page
    of ten rows and a page of eleven -- identical queries with different `IN`
    lists -- are one row here instead of two.
    """

    sql: str
    sql_hash: str = ""
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    #: The first occurrence, for its stack and parameters.
    first: Query | None = None
    #: The first occurrence that *failed*, if any. Kept separately because the
    #: failure is rarely the first execution, and showing the first execution's
    #: (empty) error next to a "failed" badge hides the only thing you needed.
    first_failure: Query | None = None
    #: A sample of the distinct parameter renderings, for display.
    params: list[str] = field(default_factory=list)
    #: How many distinct renderings there actually were, so the "and N more"
    #: hint counts what was elided rather than what was executed.
    distinct_params: int = 0
    failures: int = 0
    #: How many of the executions blocked the event loop.
    blocking: int = 0
    #: Ordinal positions in the request, so the order of execution is legible.
    positions: list[int] = field(default_factory=list)
    #: Distinct stacks. More than one means the same SQL was issued from
    #: different places, and one shared stack would be a lie.
    call_sites: int = 0

    @property
    def is_duplicate(self) -> bool:
        return self.count > 1

    @property
    def operation(self) -> str:
        return self.first.operation if self.first else "SQL"

    @property
    def _representative(self) -> Query | None:
        return self.first_failure or self.first

    @property
    def stack(self) -> list[str]:
        query = self._representative
        return query.stack if query else []

    @property
    def error(self) -> str | None:
        return self.first_failure.error if self.first_failure else None

    @property
    def failed(self) -> bool:
        return self.failures > 0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


#: Parameter renderings that carry no information and are not worth the row.
_EMPTY_PARAMS = ("()", "{}", "[]", "", "None")

#: How many distinct parameter renderings and positions to keep per group.
_PARAM_SAMPLE = 5
_POSITION_SAMPLE = 50


def group_queries(queries: list[Query]) -> list[QueryGroup]:
    """Collapse repeated statements, preserving first-execution order."""
    groups: dict[str, QueryGroup] = {}
    seen_params: dict[str, set[str]] = {}
    seen_stacks: dict[str, set[tuple[str, ...]]] = {}
    for position, query in enumerate(queries, start=1):
        key = query.sql_hash
        group = groups.get(key)
        if group is None:
            group = groups[key] = QueryGroup(sql=query.sql, sql_hash=key, first=query)
            seen_params[key] = set()
            seen_stacks[key] = set()
        group.count += 1
        group.total_ms += query.duration_ms
        group.max_ms = max(group.max_ms, query.duration_ms)
        if query.blocking:
            group.blocking += 1
        if query.failed:
            group.failures += 1
            if group.first_failure is None:
                group.first_failure = query
        if len(group.positions) < _POSITION_SAMPLE:
            group.positions.append(position)
        if query.params not in _EMPTY_PARAMS:
            distinct = seen_params[key]
            if query.params not in distinct:
                distinct.add(query.params)
                group.distinct_params = len(distinct)
                if len(group.params) < _PARAM_SAMPLE:
                    group.params.append(query.params)
        if query.stack:
            stacks = seen_stacks[key]
            stacks.add(tuple(query.stack))
            group.call_sites = len(stacks)
    return list(groups.values())


@dataclass(slots=True)
class Profile:
    """One HTTP request.

    The counters at the bottom are filled in by :meth:`finalise`, which the
    middleware calls once when the request ends. They are stored rather than
    computed on access because the viewer reads them once per row per render,
    and because a storage backend needs to filter and group on them without
    rehydrating every statement.
    """

    id: str
    method: str
    path: str
    #: The matched route *pattern* -- ``/users/{user_id}`` rather than
    #: ``/users/42``. Empty when routing did not expose one.
    route: str = ""
    query_string: str = ""
    status_code: int = 0
    duration_ms: float = 0.0
    recorded_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    client: str = ""
    queries: list[Query] = field(default_factory=list)
    #: What the detectors concluded. Filled by the middleware after
    #: :meth:`finalise`, because a detector needs configured thresholds and a
    #: dataclass should not reach for configuration.
    problems: list[Problem] = field(default_factory=list)

    # -- derived, filled by finalise() ------------------------------------
    query_count: int = 0
    query_ms: float = 0.0
    #: How many queries repeat a statement already seen in this request. The
    #: N+1 signal: a relationship loaded once per row produces the same
    #: statement many times over.
    duplicate_count: int = 0
    error_count: int = 0
    #: How many statements stopped the event loop.
    blocking_count: int = 0

    def finalise(self) -> None:
        """Mark duplicated statements and compute the counters.

        Called once, when the request ends. Mutating `queries` afterwards
        leaves the counters stale.
        """
        counts = Counter(q.sql_hash for q in self.queries)
        for query in self.queries:
            query.is_duplicate = counts[query.sql_hash] > 1
        self.query_count = len(self.queries)
        self.query_ms = sum(q.duration_ms for q in self.queries)
        self.duplicate_count = sum(n - 1 for n in counts.values() if n > 1)
        self.error_count = sum(1 for q in self.queries if q.failed)
        self.blocking_count = sum(1 for q in self.queries if q.blocking)

    # -- presentation -----------------------------------------------------
    @property
    def group(self) -> str:
        """The label this request aggregates under: its route, else its path."""
        return self.route or self.path

    @property
    def query_groups(self) -> list[QueryGroup]:
        return group_queries(self.queries)

    @property
    def python_ms(self) -> float:
        return max(0.0, self.duration_ms - self.query_ms)

    @property
    def full_path(self) -> str:
        return f"{self.path}?{self.query_string}" if self.query_string else self.path

    @property
    def status_class(self) -> str:
        if self.status_code >= 500:
            return "err"
        if self.status_code >= 400:
            return "warn"
        if self.status_code >= 300:
            return "info"
        return "ok"

    def as_dict(self, *, with_queries: bool = True) -> dict[str, object]:
        """A JSON-serialisable view, for the viewer's JSON endpoints.

        This is what makes the profiler scriptable: assert in CI that an
        endpoint stays under N queries, diff two runs, attach a trace to a
        bug report, fail a build when a detector finds something new.
        """
        data: dict[str, object] = {
            "id": self.id,
            "method": self.method,
            "path": self.path,
            "route": self.route,
            "group": self.group,
            "query_string": self.query_string,
            "status_code": self.status_code,
            "duration_ms": round(self.duration_ms, 3),
            "query_ms": round(self.query_ms, 3),
            "python_ms": round(self.python_ms, 3),
            "query_count": self.query_count,
            "duplicate_count": self.duplicate_count,
            "error_count": self.error_count,
            "blocking_count": self.blocking_count,
            "recorded_at": self.recorded_at.isoformat(),
            "client": self.client,
            "problems": [p.as_dict() for p in self.problems],
        }
        if with_queries:
            data["request_headers"] = self.request_headers
            data["response_headers"] = self.response_headers
            data["queries"] = [
                {
                    "sql": q.sql,
                    "sql_hash": q.sql_hash,
                    "params": q.params,
                    "duration_ms": round(q.duration_ms, 3),
                    "started_ms": round(q.started_ms, 3),
                    "is_duplicate": q.is_duplicate,
                    "blocking": q.blocking,
                    "error": q.error,
                    "stack": q.stack,
                }
                for q in self.queries
            ]
        return data


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile. No numpy, and exact on small samples."""
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(slots=True)
class PathSummary:
    """Aggregate row on the summary page, keyed by route pattern where known."""

    path: str
    method: str
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    total_queries: int = 0
    total_duplicates: int = 0
    total_errors: int = 0
    total_problems: int = 0
    #: Percentiles matter more than the average, which one outlier ruins and
    #: which tells you nothing about what most users experienced.
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0

    def set_percentiles(self, durations: list[float]) -> None:
        ordered = sorted(durations)
        self.p50_ms = _percentile(ordered, 0.50)
        self.p95_ms = _percentile(ordered, 0.95)
        self.p99_ms = _percentile(ordered, 0.99)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    @property
    def avg_queries(self) -> float:
        return self.total_queries / self.count if self.count else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "route": self.path,
            "method": self.method,
            "count": self.count,
            "total_ms": round(self.total_ms, 3),
            "avg_ms": round(self.avg_ms, 3),
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "total_queries": self.total_queries,
            "avg_queries": round(self.avg_queries, 3),
            "total_duplicates": self.total_duplicates,
            "total_errors": self.total_errors,
            "total_problems": self.total_problems,
        }


@dataclass(slots=True)
class StatementSummary:
    """One SQL statement aggregated across every request that ran it."""

    sql: str
    sql_hash: str = ""
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    #: How many distinct requests ran it.
    requests: int = 0
    #: How many distinct routes ran it. More than one means a shared query --
    #: often a serialiser or a permission check nobody thinks about.
    route_count: int = 0
    sample_route: str = ""
    sample_method: str = "GET"
    failures: int = 0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    @property
    def per_request(self) -> float:
        return self.count / self.requests if self.requests else 0.0

    @property
    def operation(self) -> str:
        head = self.sql.lstrip().split(" ", 1)[0].upper()
        return head if head.isalpha() else "SQL"

    def as_dict(self) -> dict[str, object]:
        return {
            "sql": self.sql,
            "sql_hash": self.sql_hash,
            "count": self.count,
            "total_ms": round(self.total_ms, 3),
            "avg_ms": round(self.avg_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "requests": self.requests,
            "per_request": round(self.per_request, 2),
            "route_count": self.route_count,
            "sample_route": self.sample_route,
            "sample_method": self.sample_method,
            "failures": self.failures,
        }
