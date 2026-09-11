"""Where recorded profiles live.

Two backends ship: :class:`MemoryStorage`, the zero-config default, and
:class:`SQLiteStorage`, which survives a restart and is shared by every worker
in a multi-process deployment.

`Storage` is a Protocol, so a Redis or Postgres backend can be dropped in
without touching the middleware or the viewer. Subclass :class:`BaseStorage`
and you only have to implement the five core methods -- searching, paging,
summarising and statement aggregation come for free, and can be overridden
where the backend can do them better than Python can.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import math
import queue
import sqlite3
import threading
import time
import urllib.parse
import weakref
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .models import PathSummary, Profile, Query, StatementSummary

logger = logging.getLogger('asgi_profiler')

#: Aliases resolved at module scope. Inside the storage classes the name
#: `list` is bound to the method, so a bare `-> list[Profile]` annotation
#: resolves to that method instead of the builtin -- which type checkers
#: rightly reject, and which made the shipped `py.typed` marker a lie.
Profiles = list[Profile]
PathSummaries = list[PathSummary]
StatementSummaries = list[StatementSummary]

_ORDERINGS: dict[str, Any] = {
    'recent': None,
    'slowest': lambda p: -p.duration_ms,
    'queries': lambda p: -p.query_count,
    'sql': lambda p: -p.query_ms,
}


@dataclass(frozen=True)
class Filters:
    """The viewer's filter state, in one object both backends understand."""

    q: str = ''
    method: str = ''
    status: str = ''  # "ok" | "warn" | "err"
    route: str = ''  # exact match on the route pattern
    min_ms: float | None = None
    only_duplicates: bool = False
    only_errors: bool = False
    order: str = 'recent'

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> Filters:
        raw = (params.get('min_ms') or '').strip()
        try:
            min_ms = float(raw) if raw else None
        except ValueError:
            min_ms = None
        if min_ms is not None and not math.isfinite(min_ms):
            # `?min_ms=nan` compares False everywhere in Python and True
            # nowhere in SQL, so the two backends would disagree.
            min_ms = None
        only = params.get('only', '')
        order = params.get('order', 'recent')
        return cls(
            # Capped: SQLite raises "LIKE or GLOB pattern too complex" past a
            # few thousand characters, which would 500 the page rather than
            # return nothing.
            q=(params.get('q') or '').strip()[:200],
            method=(params.get('method') or '').strip().upper(),
            status=(params.get('status') or '').strip(),
            route=(params.get('route') or '').strip(),
            min_ms=min_ms,
            only_duplicates=only == 'duplicates',
            only_errors=only == 'errors',
            order=order if order in _ORDERINGS else 'recent',
        )

    @property
    def active(self) -> bool:
        return bool(
            self.q
            or self.method
            or self.status
            or self.route
            or self.min_ms is not None
            or self.only_duplicates
            or self.only_errors
        )

    def matches(self, profile: Profile) -> bool:
        if self.q and self.q.lower() not in profile.full_path.lower():
            return False
        if self.method and profile.method != self.method:
            return False
        if self.route and profile.group != self.route:
            return False
        if self.status == 'err' and profile.status_code < 500:
            return False
        if self.status == 'warn' and not (400 <= profile.status_code < 500):
            return False
        if self.status == 'ok' and not (200 <= profile.status_code < 300):
            return False
        if self.only_duplicates and not profile.duplicate_count:
            return False
        if self.only_errors and not profile.error_count:
            return False
        return not (self.min_ms is not None and profile.duration_ms < self.min_ms)


@dataclass
class Page:
    """One page of results, plus enough context to render the pager.

    The items are *listing* rows: counters are always populated, but a backend
    may leave `queries` and the header dicts empty because the request list
    never shows them. Use `Storage.get()` for a complete profile.
    """

    items: Profiles = field(default_factory=list)
    #: Matching the filters, across all pages.
    total: int = 0
    number: int = 1
    size: int = 50

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size)) if self.size else 1

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages

    @property
    def first_index(self) -> int:
        return (self.number - 1) * self.size + 1 if self.total else 0

    @property
    def last_index(self) -> int:
        return min(self.number * self.size, self.total)


class Storage(Protocol):
    def add(self, profile: Profile) -> None: ...

    def get(self, profile_id: str) -> Profile | None: ...

    def list(self, *, limit: int | None = None, offset: int = 0) -> Profiles: ...

    def count(self) -> int: ...

    def clear(self) -> None: ...

    def search(self, filters: Filters, *, page: int = 1, size: int = 50) -> Page: ...

    def summarise(self) -> PathSummaries: ...

    def statements(self, limit: int = 100) -> StatementSummaries: ...


class BaseStorage:
    """Generic `search`, `summarise` and `statements` on top of `list()`.

    Correct for any backend; a backend that can push the work down to a query
    engine should override them.
    """

    def list(  # pragma: no cover - overridden
        self, *, limit: int | None = None, offset: int = 0
    ) -> Profiles:
        raise NotImplementedError

    def count(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    def search(self, filters: Filters, *, page: int = 1, size: int = 50) -> Page:
        matched = [p for p in self.list() if filters.matches(p)]
        key = _ORDERINGS.get(filters.order)
        if key is not None:
            matched.sort(key=key)
        return _paginate(matched, page=page, size=size)

    def summarise(self) -> PathSummaries:
        return summarise(self.list())

    def statements(self, limit: int = 100) -> StatementSummaries:
        return aggregate_statements(self.list(), limit=limit)


def _paginate(profiles: Profiles, *, page: int, size: int) -> Page:
    size = max(1, size)
    total = len(profiles)
    pages = max(1, -(-total // size))
    number = min(max(1, page), pages)
    start = (number - 1) * size
    return Page(items=profiles[start : start + size], total=total, number=number, size=size)


class MemoryStorage(BaseStorage):
    """Bounded ring buffer, newest first. Thread-safe.

    Per-process: with multiple workers each holds its own history. Use
    :class:`SQLiteStorage` if that matters.
    """

    def __init__(self, max_requests: int = 500) -> None:
        self._items: deque[Profile] = deque(maxlen=max_requests)
        self._lock = threading.Lock()

    def add(self, profile: Profile) -> None:
        with self._lock:
            self._items.appendleft(profile)

    def get(self, profile_id: str) -> Profile | None:
        with self._lock:
            for item in self._items:
                if item.id == profile_id:
                    return item
        return None

    def list(self, *, limit: int | None = None, offset: int = 0) -> Profiles:
        with self._lock:
            items = list(self._items)
        if offset:
            items = items[offset:]
        return items[:limit] if limit is not None else items

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return self.count()


#: Stores with a live writer thread, so the interpreter can flush them on the
#: way out. A daemon thread is killed at exit with whatever is still queued,
#: and nothing calls `close()` for a store handed to `install()` -- without
#: this the tail of every capture is lost, which for "profile in staging, read
#: the file later" is exactly the part you were waiting for.
#:
#: One handler for the process over a weak set, rather than one registration
#: per store: a per-store hook has to be unregistered on close, and a leaked
#: `atexit` entry per store is its own slow problem.
_OPEN_STORES: weakref.WeakSet[Any] = weakref.WeakSet()


def _flush_open_stores() -> None:
    for store in list(_OPEN_STORES):
        with contextlib.suppress(Exception):
            store.close()


atexit.register(_flush_open_stores)


class IncompatibleCapture(RuntimeError):
    """A capture file written by an incompatible schema version."""


#: Bumped whenever the table layout changes. A mismatch rebuilds the file
#: rather than failing with an opaque OperationalError from inside `add()`,
#: after the response has already gone out.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,
    route           TEXT NOT NULL DEFAULT '',
    grp             TEXT NOT NULL DEFAULT '',
    query_string    TEXT NOT NULL DEFAULT '',
    search_path     TEXT NOT NULL DEFAULT '',
    status_code     INTEGER NOT NULL DEFAULT 0,
    duration_ms     REAL NOT NULL DEFAULT 0,
    query_ms        REAL NOT NULL DEFAULT 0,
    query_count     INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    recorded_at     TEXT NOT NULL,
    client          TEXT NOT NULL DEFAULT '',
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS profiles_seq ON profiles (seq DESC);
CREATE INDEX IF NOT EXISTS profiles_group ON profiles (method, grp);

CREATE TABLE IF NOT EXISTS statements (
    profile_seq  INTEGER NOT NULL,
    sql          TEXT NOT NULL,
    grp          TEXT NOT NULL DEFAULT '',
    method       TEXT NOT NULL DEFAULT 'GET',
    duration_ms  REAL NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS statements_profile ON statements (profile_seq);
CREATE INDEX IF NOT EXISTS statements_sql ON statements (sql);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('rows', 0);
"""

_INSERT = (
    'INSERT OR REPLACE INTO profiles (id, method, path, route, grp,'
    ' query_string, search_path, status_code, duration_ms, query_ms,'
    ' query_count, duplicate_count, error_count, recorded_at, client, payload)'
    ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
)


class SQLiteStorage(BaseStorage):
    """Profiles in a SQLite file.

    Why you would want it: history survives a restart, and every `uvicorn`
    worker writes to the same file, so the viewer shows *all* of your traffic
    rather than the fraction that happened to hit the worker you are talking
    to. That single fact is what makes the profiler trustworthy under the
    default multi-worker deployment.

    **Writes go through a background thread.** `add()` is called from the
    middleware while the event loop is running, and a synchronous insert there
    means an `fsync` -- and, with several workers on one WAL file, a lock wait
    that `sqlite3` implements as a blocking sleep -- happening *on the loop*.
    Measured at up to 800 ms of stall in a single `add()`. Enqueueing instead
    keeps the request path at a few microseconds, and batching the drain makes
    the writes themselves cheaper. Reads flush first, so you never see a stale
    page.

    Pass ``background=False`` for synchronous writes (simpler in tests, and
    fine for a single-worker development server).
    """

    def __init__(
        self,
        path: str | Path = 'profiler.db',
        max_requests: int = 5000,
        *,
        background: bool = True,
        batch_size: int = 64,
        linger_seconds: float = 0.02,
        read_only: bool = False,
    ) -> None:
        self.path = str(path)
        self.read_only = read_only
        self.max_requests = max_requests
        self.batch_size = max(1, batch_size)
        self.linger_seconds = max(0.0, linger_seconds)
        self._lock = threading.RLock()
        self._closed = False

        if read_only:
            uri = f'file:{urllib.parse.quote(self.path)}?mode=ro'
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.create_function('py_lower', 1, _lower, deterministic=True)
                if read_only:
                    self._check_schema()
                else:
                    # WAL so several worker processes can write concurrently.
                    self._conn.execute('PRAGMA journal_mode=WAL')
                    self._conn.execute('PRAGMA synchronous=NORMAL')
                    self._prepare_schema()
        except BaseException:
            # An __init__ that raises leaves no object for the caller to close,
            # so the connection would leak -- visible as a ResourceWarning
            # under `-W error` and as a held file handle on Windows.
            self._conn.close()
            self._closed = True
            raise

        self._queue: queue.Queue[Profile | None] | None = None
        self._writer: threading.Thread | None = None
        if background:
            self._queue = queue.Queue()
            self._writer = threading.Thread(
                target=self._drain_forever,
                name='asgi-profiler-writer',
                daemon=True,
            )
            self._writer.start()
            # A daemon thread is killed at interpreter exit with whatever is
            # still queued. Nothing calls close() for a store handed to
            # install(), so without this the tail of every capture is lost --
            # which for "profile in staging, read the file later" is the part
            # you were waiting for.
            _OPEN_STORES.add(self)

    # -- schema -----------------------------------------------------------
    def _check_schema(self) -> None:
        """Read-only: report an incompatible file, never rewrite it.

        `_prepare_schema` drops and recreates on a version mismatch, which is
        the right call for a live capture and catastrophic for the offline
        viewer -- opening someone's staging capture to look at it must not be
        able to delete it.
        """
        version = self._conn.execute('PRAGMA user_version').fetchone()[0]
        if version != SCHEMA_VERSION:
            raise IncompatibleCapture(
                f'{self.path} was written by a different version of '
                f'asgi-profiler (schema {version}, expected '
                f'{SCHEMA_VERSION}). Re-capture with this version.'
            )

    def _prepare_schema(self) -> None:
        version = self._conn.execute('PRAGMA user_version').fetchone()[0]
        existing = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='profiles'"
        ).fetchone()
        if existing and version != SCHEMA_VERSION:
            # An old file from a previous release. Profiling history is
            # disposable by nature, so rebuild rather than write a migration
            # for data nobody would miss.
            self._conn.executescript(
                'DROP TABLE IF EXISTS profiles; DROP TABLE IF EXISTS statements;'
            )
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        self._conn.execute(
            'INSERT OR REPLACE INTO meta (key, value) VALUES'
            " ('rows', (SELECT COUNT(*) FROM profiles))"
        )
        self._conn.commit()

    # -- writing ----------------------------------------------------------
    def add(self, profile: Profile) -> None:
        if self._closed or self.read_only:
            return
        if self._queue is not None:
            self._queue.put(profile)
            return
        with self._lock:
            self._write([profile])

    def flush(self, timeout: float = 30.0) -> None:
        """Block until every queued profile has been written.

        Bounded: `queue.join()` alone waits forever, and this is called from
        the viewer's request handlers, so a wedged writer would take the
        application down rather than showing a stale page.
        """
        if self._queue is None or self._closed:
            return
        deadline = time.monotonic() + timeout
        # `unfinished_tasks` is undocumented but stable since 2.5 -- reading it
        # is what lets this poll with a deadline instead of `join()`, which
        # cannot time out.
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline or not self._alive():
                logger.warning('Timed out flushing profiles to %s', self.path)
                return
            time.sleep(0.001)

    def _alive(self) -> bool:
        return self._writer is None or self._writer.is_alive()

    def _drain_forever(self) -> None:  # pragma: no cover - thread body
        """Drain the queue until the sentinel arrives.

        Every exit path has to call `task_done()` for each item taken, and no
        exception may escape: `flush()` is a `queue.join()` with no timeout, so
        a writer that dies with items outstanding parks every future flush --
        and every viewer request -- forever.
        """
        # Not an `assert`: `python -O` strips those, and this one is the
        # narrowing the rest of the body relies on. A real check costs one
        # comparison once per thread.
        if self._queue is None:  # pragma: no cover - unreachable by construction
            return
        while True:
            batch: Profiles = []
            stop = False
            first = self._queue.get()
            if first is None:
                stop = True
            else:
                batch.append(first)
                # Linger briefly to coalesce. Without it the writer wakes for
                # every single profile whenever it is keeping up, and each wake
                # is its own transaction -- which puts the producer back in
                # contention with SQLite work and throws away the whole point
                # of writing off the request path.
                deadline = time.monotonic() + self.linger_seconds
                while len(batch) < self.batch_size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        nxt = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if nxt is None:
                        stop = True
                        self._queue.task_done()
                        break
                    batch.append(nxt)

            taken = len(batch)
            try:
                if batch:
                    with self._lock:
                        self._write(batch)
            except Exception:
                # Losing a batch is bad; losing the writer is worse. Say so --
                # a diagnostic tool that under-reports traffic in silence is
                # the one failure nobody would think to check for.
                logger.warning(
                    'Could not write %d profile(s) to %s',
                    len(batch),
                    self.path,
                    exc_info=True,
                )
            finally:
                for _ in range(taken):
                    self._queue.task_done()
                if first is None:
                    self._queue.task_done()
            if stop:
                return

    def _write(self, profiles: Profiles) -> None:
        # Last write wins within a batch, so the row-count bookkeeping below
        # matches what SQLite will actually store.
        deduped = {p.id: p for p in profiles}
        already = {
            r['id']
            for r in self._conn.execute(
                f'SELECT id FROM profiles WHERE id IN ({",".join("?" * len(deduped))})',  # nosec hardcoded_sql_expressions
                list(deduped),
            )
        }
        rows = [(_row_values(p), p) for p in deduped.values()]
        if already:
            # INSERT OR REPLACE allocates a *new* seq, so the replaced
            # profile's statement rows survive it -- unreachable, but still
            # aggregated, inflating every count on the statements page.
            self._conn.execute(
                'DELETE FROM statements WHERE profile_seq IN'  # nosec hardcoded_sql_expressions
                f' (SELECT seq FROM profiles WHERE id IN'
                f' ({",".join("?" * len(already))}))',
                list(already),
            )
        self._conn.executemany(_INSERT, [values for values, _ in rows])
        seqs = {
            r['id']: r['seq']
            for r in self._conn.execute(
                'SELECT id, seq FROM profiles WHERE id IN'  # nosec hardcoded_sql_expressions
                f' ({",".join("?" * len(rows))})',
                [p.id for _, p in rows],
            )
        }
        self._conn.executemany(
            'INSERT INTO statements'
            ' (profile_seq, sql, grp, method, duration_ms, failed)'
            ' VALUES (?,?,?,?,?,?)',
            [
                (
                    seqs[p.id],
                    q.sql,
                    p.group,
                    p.method,
                    q.duration_ms,
                    1 if q.failed else 0,
                )
                for _, p in rows
                if p.id in seqs
                for q in p.queries
            ],
        )

        # An INSERT OR REPLACE over an existing id does not add a row. Adding
        # one to the count anyway makes the trim evict a row too many, and the
        # history shrinks a little every time an id repeats.
        # An INSERT OR REPLACE over an existing id does not add a row. Adding
        # one to the count anyway makes the trim evict a row too many, and the
        # history shrinks a little every time an id repeats.
        self._bump_rows(len(deduped) - len(already))
        self._trim()
        self._conn.commit()

    def _bump_rows(self, delta: int) -> int:
        if delta:
            self._conn.execute(
                "UPDATE meta SET value = MAX(0, value + ?) WHERE key = 'rows'",
                (delta,),
            )
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'rows'").fetchone()
        return int(row['value']) if row else 0

    def _trim(self) -> None:
        """Drop the oldest rows above the cap, in O(overshoot).

        The obvious `DELETE ... WHERE seq < (SELECT MIN(seq) FROM (... ORDER BY
        seq DESC LIMIT n))` walks the whole retention window on every insert --
        390 us per request at the default cap of 5000, paid on the event loop.
        This walks only as far as the number of rows actually being evicted,
        which in the steady state is one per insert, while keeping the cap
        exact: a soft cap that overshoots by a batch is a worse trade than a
        cheap exact one.

        The count it works from lives in the file rather than in this process.
        A per-process counter goes stale the moment another worker calls
        `clear()`, and a stale-high counter makes every subsequent insert
        delete the row it just wrote -- a worker that silently stops recording
        anything at all.
        """
        excess = self._bump_rows(0) - self.max_requests
        if excess <= 0:
            return
        row = self._conn.execute(
            'SELECT seq FROM profiles ORDER BY seq ASC LIMIT 1 OFFSET ?',
            (excess - 1,),
        ).fetchone()
        if row is None:  # pragma: no cover - another process trimmed first
            return
        cutoff = row['seq']
        self._conn.execute('DELETE FROM statements WHERE profile_seq <= ?', (cutoff,))
        deleted = self._conn.execute('DELETE FROM profiles WHERE seq <= ?', (cutoff,)).rowcount
        self._bump_rows(-max(0, deleted))

    def clear(self) -> None:
        if self.read_only:
            return
        self.flush()
        with self._lock:
            self._conn.execute('DELETE FROM profiles')
            self._conn.execute('DELETE FROM statements')
            self._conn.execute("UPDATE meta SET value = 0 WHERE key = 'rows'")
            self._conn.commit()

    def close(self) -> None:
        """Flush pending writes, stop the writer thread and close the file."""
        if self._closed:
            return
        _OPEN_STORES.discard(self)
        if self._queue is not None:
            self._queue.put(None)
            if self._writer is not None:
                self._writer.join(timeout=10)
        self._closed = True
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SQLiteStorage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading ----------------------------------------------------------
    def count(self) -> int:
        self.flush()
        with self._lock:
            row = self._conn.execute('SELECT COUNT(*) AS n FROM profiles').fetchone()
        return int(row['n'])

    def get(self, profile_id: str) -> Profile | None:
        self.flush()
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM profiles WHERE id = ?', (profile_id,)
            ).fetchone()
        return _row_to_profile(row, with_queries=True) if row else None

    def list(self, *, limit: int | None = None, offset: int = 0) -> Profiles:
        self.flush()
        sql = 'SELECT * FROM profiles ORDER BY seq DESC LIMIT ? OFFSET ?'
        with self._lock:
            rows = self._conn.execute(sql, (-1 if limit is None else limit, offset)).fetchall()
        return [_row_to_profile(r, with_queries=True) for r in rows]

    def search(self, filters: Filters, *, page: int = 1, size: int = 50) -> Page:
        self.flush()
        where, params = _where(filters)
        with self._lock:
            total = int(
                self._conn.execute(
                    f'SELECT COUNT(*) AS n FROM profiles {where}',  # nosec hardcoded_sql_expressions
                    params,
                ).fetchone()['n']
            )
            size = max(1, size)
            pages = max(1, -(-total // size))
            number = min(max(1, page), pages)
            # `seq DESC` as a tiebreaker everywhere: without it, equal
            # durations page in an arbitrary order here and newest-first in
            # MemoryStorage, so the same data reads differently per backend.
            order = {
                'recent': 'seq DESC',
                'slowest': 'duration_ms DESC, seq DESC',
                'queries': 'query_count DESC, seq DESC',
                'sql': 'query_ms DESC, seq DESC',
            }.get(filters.order, 'seq DESC')
            rows = self._conn.execute(
                f'SELECT * FROM profiles {where} ORDER BY {order} LIMIT ? OFFSET ?',  # nosec hardcoded_sql_expressions
                (*params, size, (number - 1) * size),
            ).fetchall()
        # The listing shows counters, never individual statements, so skip the
        # JSON payload entirely -- that is most of the row.
        return Page(
            items=[_row_to_profile(r, with_queries=False) for r in rows],
            total=total,
            number=number,
            size=size,
        )

    def summarise(self) -> PathSummaries:
        self.flush()
        with self._lock:
            rows = self._conn.execute(
                'SELECT method, grp, duration_ms, query_count, duplicate_count,'
                ' error_count FROM profiles'
            ).fetchall()
        # Aggregated in Python rather than SQL because percentiles need the
        # whole distribution and SQLite has no PERCENTILE_CONT. One narrow
        # column scan is cheaper than a correlated subquery per group.
        buckets: dict[tuple[str, str], PathSummary] = {}
        durations: dict[tuple[str, str], list[float]] = defaultdict(list)
        for row in rows:
            key = (row['method'], row['grp'])
            summary = buckets.get(key)
            if summary is None:
                summary = buckets[key] = PathSummary(path=row['grp'], method=key[0])
            duration = float(row['duration_ms'])
            summary.count += 1
            summary.total_ms += duration
            summary.max_ms = max(summary.max_ms, duration)
            summary.total_queries += int(row['query_count'])
            summary.total_duplicates += int(row['duplicate_count'])
            summary.total_errors += int(row['error_count'])
            durations[key].append(duration)
        for key, summary in buckets.items():
            summary.set_percentiles(durations[key])
        return sorted(buckets.values(), key=lambda r: r.total_ms, reverse=True)

    def statements(self, limit: int = 100) -> StatementSummaries:
        self.flush()
        with self._lock:
            rows = self._conn.execute(
                'SELECT sql, COUNT(*) AS n, SUM(duration_ms) AS total,'
                ' MAX(duration_ms) AS worst, SUM(failed) AS failures,'
                ' COUNT(DISTINCT profile_seq) AS requests,'
                ' COUNT(DISTINCT grp) AS routes,'
                ' MIN(grp) AS a_route, MIN(method) AS a_method'
                ' FROM statements GROUP BY sql'
                # `sql` as a tiebreaker so that a LIMIT returns the same rows
                # here as in MemoryStorage. Equal totals are common with
                # coarse timers, and a limit that depends on the backend is
                # not an interchangeable API.
                ' ORDER BY total DESC, sql ASC LIMIT ?',
                (max(0, limit),),
            ).fetchall()
        return [
            StatementSummary(
                sql=r['sql'],
                count=int(r['n']),
                total_ms=float(r['total'] or 0.0),
                max_ms=float(r['worst'] or 0.0),
                requests=int(r['requests']),
                route_count=int(r['routes']),
                sample_route=r['a_route'] or '',
                sample_method=r['a_method'] or 'GET',
                failures=int(r['failures'] or 0),
            )
            for r in rows
        ]


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _search_text(profile: Profile) -> str:
    return profile.full_path.lower()


def _row_values(profile: Profile) -> tuple[Any, ...]:
    payload = json.dumps(
        {
            'request_headers': profile.request_headers,
            'response_headers': profile.response_headers,
            'queries': [
                {
                    'sql': q.sql,
                    'params': q.params,
                    'duration_ms': q.duration_ms,
                    'stack': q.stack,
                    'is_duplicate': q.is_duplicate,
                    'error': q.error,
                }
                for q in profile.queries
            ],
        }
    )
    return (
        profile.id,
        profile.method,
        profile.path,
        profile.route,
        profile.group,
        profile.query_string,
        _search_text(profile),
        profile.status_code,
        profile.duration_ms,
        profile.query_ms,
        profile.query_count,
        profile.duplicate_count,
        profile.error_count,
        profile.recorded_at.isoformat(),
        profile.client,
        payload,
    )


def _where(filters: Filters) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filters.q:
        # Matched against a column folded at insert time. Calling py_lower per
        # row turned this into a Python callback over a full table scan --
        # 16 ms at 20k rows against 3.7 ms for the pure-Python backend, which
        # is the opposite of the point of pushing the filter down.
        clauses.append("search_path LIKE ? ESCAPE '\\'")
        needle = filters.q.lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        params.append(f'%{needle}%')
    if filters.method:
        clauses.append('method = ?')
        params.append(filters.method)
    if filters.route:
        clauses.append('grp = ?')
        params.append(filters.route)
    if filters.status == 'err':
        clauses.append('status_code >= 500')
    elif filters.status == 'warn':
        clauses.append('status_code >= 400 AND status_code < 500')
    elif filters.status == 'ok':
        clauses.append('status_code >= 200 AND status_code < 300')
    if filters.only_duplicates:
        clauses.append('duplicate_count > 0')
    if filters.only_errors:
        clauses.append('error_count > 0')
    if filters.min_ms is not None:
        clauses.append('duration_ms >= ?')
        params.append(filters.min_ms)
    return ('WHERE ' + ' AND '.join(clauses)) if clauses else '', tuple(params)


def _row_to_profile(row: sqlite3.Row, *, with_queries: bool) -> Profile:
    payload = json.loads(row['payload']) if with_queries else {}
    profile = Profile(
        id=row['id'],
        method=row['method'],
        path=row['path'],
        route=row['route'],
        query_string=row['query_string'],
        status_code=int(row['status_code']),
        duration_ms=float(row['duration_ms']),
        recorded_at=_parse_datetime(row['recorded_at']),
        request_headers=payload.get('request_headers', {}),
        response_headers=payload.get('response_headers', {}),
        client=row['client'],
        queries=[
            Query(
                sql=q['sql'],
                params=q['params'],
                duration_ms=q['duration_ms'],
                stack=q.get('stack', []),
                is_duplicate=q.get('is_duplicate', False),
                error=q.get('error'),
            )
            for q in payload.get('queries', [])
        ],
    )
    # Read the counters back rather than recomputing them, so a listing row
    # stays correct even though its statements were not loaded.
    profile.query_count = int(row['query_count'])
    profile.query_ms = float(row['query_ms'])
    profile.duplicate_count = int(row['duplicate_count'])
    profile.error_count = int(row['error_count'])
    return profile


def _parse_datetime(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:  # pragma: no cover - defensive
        return datetime.now(tz=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def summarise(profiles: Iterable[Profile]) -> PathSummaries:
    """Group profiles for the summary page.

    Keyed by the route *pattern* where one is known, so `/users/1` and
    `/users/2` land on one row instead of two. Grouping by the literal path
    turns the summary into a deduplicated request log, which is exactly what
    the page is not for.
    """
    rows: dict[tuple[str, str], PathSummary] = {}
    durations: dict[tuple[str, str], list[float]] = defaultdict(list)
    for profile in profiles:
        key = (profile.method, profile.group)
        row = rows.get(key)
        if row is None:
            row = rows[key] = PathSummary(path=profile.group, method=profile.method)
        row.count += 1
        row.total_ms += profile.duration_ms
        row.max_ms = max(row.max_ms, profile.duration_ms)
        row.total_queries += profile.query_count
        row.total_duplicates += profile.duplicate_count
        row.total_errors += profile.error_count
        durations[key].append(profile.duration_ms)
    for key, row in rows.items():
        row.set_percentiles(durations[key])
    return sorted(rows.values(), key=lambda r: r.total_ms, reverse=True)


def aggregate_statements(profiles: Iterable[Profile], limit: int = 100) -> StatementSummaries:
    """Group every recorded statement by its SQL, across all requests.

    The per-request view answers "why is *this* endpoint slow". This answers
    "what is costing me across the whole application" -- the statement that
    runs three times on eleven different endpoints is invisible to the former
    and obvious here.
    """
    rows: dict[str, StatementSummary] = {}
    routes: dict[str, set[str]] = defaultdict(set)
    methods: dict[str, set[str]] = defaultdict(set)
    requests: dict[str, set[str]] = defaultdict(set)
    for profile in profiles:
        for query in profile.queries:
            row = rows.get(query.sql)
            if row is None:
                row = rows[query.sql] = StatementSummary(sql=query.sql)
            row.count += 1
            row.total_ms += query.duration_ms
            row.max_ms = max(row.max_ms, query.duration_ms)
            if query.failed:
                row.failures += 1
            routes[query.sql].add(profile.group)
            methods[query.sql].add(profile.method)
            requests[query.sql].add(profile.id)
    for sql, row in rows.items():
        row.route_count = len(routes[sql])
        row.requests = len(requests[sql])
        # `min` rather than "the first one seen": SQLite groups with MIN(), and
        # a sample that depends on insertion order is not the same API.
        row.sample_route = min(routes[sql]) if routes[sql] else ''
        row.sample_method = min(methods[sql]) if methods[sql] else 'GET'
    ordered = sorted(rows.values(), key=lambda r: (-r.total_ms, r.sql))
    return ordered[: max(0, limit)]
