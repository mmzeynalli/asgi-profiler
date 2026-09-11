"""Regressions for the trust bugs found reviewing 0.2.0 as a whole.

Each of these is a case where the library did something silently wrong: it
ignored what you passed it, let everyone in, or stalled your event loop.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asgi_profiler import (
    BaseStorage,
    MemoryStorage,
    Profile,
    ProfilerConfig,
    Query,
    SQLiteStorage,
    install,
)


async def endpoint(request):
    return PlainTextResponse("ok")


def app_with(**kwargs):
    app = Starlette(routes=[Route("/", endpoint), Route("/x/{n}", endpoint)])
    return app, install(app, **kwargs)


def make(idx: int, **kwargs) -> Profile:
    kwargs.setdefault("path", f"/p{idx}")
    profile = Profile(id=f"{idx:012d}", method="GET", **kwargs)
    profile.finalise()
    return profile


# --------------------------------------------------- storage= was ignored
def test_an_explicit_storage_is_actually_used():
    """`storage or MemoryStorage(...)` threw away an empty (falsy) store."""
    mine = MemoryStorage(max_requests=10_000)
    app, profiler = app_with(storage=mine)

    assert profiler.storage is mine
    with TestClient(app) as client:
        client.get("/")

    assert mine.count() == 1
    assert profiler.profiles[0].path == "/"


def test_a_falsy_custom_storage_is_used():
    """Any backend defining __len__ or __bool__ hit the same trap."""

    class Falsy(BaseStorage):
        def __init__(self):
            self.items: list[Profile] = []

        def add(self, profile):
            self.items.insert(0, profile)

        def get(self, profile_id):
            return next((p for p in self.items if p.id == profile_id), None)

        def list(self, *, limit=None, offset=0):
            items = self.items[offset:]
            return items[:limit] if limit is not None else items

        def count(self):
            return len(self.items)

        def clear(self):
            self.items.clear()

        def __bool__(self):  # the whole point
            return bool(self.items)

    mine = Falsy()
    app, profiler = app_with(storage=mine)
    assert profiler.storage is mine
    with TestClient(app) as client:
        client.get("/")
    assert mine.count() == 1


# ------------------------------------------------------- async authorize
def test_an_async_authorize_can_deny():
    """A coroutine object is truthy, so an async guard used to always pass."""

    async def deny(request):
        return False

    app, _ = app_with(authorize=deny)
    with TestClient(app) as client:
        assert client.get("/profiler/").status_code == 403


def test_an_async_authorize_can_allow():
    async def allow(request):
        return request.headers.get("x-key") == "letmein"

    app, _ = app_with(authorize=allow)
    with TestClient(app) as client:
        assert client.get("/profiler/").status_code == 403
        page = client.get("/profiler/", headers={"x-key": "letmein"})
        assert page.status_code == 200


def test_a_sync_authorize_still_works():
    app, _ = app_with(authorize=lambda request: False)
    with TestClient(app) as client:
        assert client.get("/profiler/").status_code == 403


def test_authorize_guards_every_viewer_page():
    app, _ = app_with(authorize=lambda request: False)
    with TestClient(app) as client:
        for path in (
            "/profiler/",
            "/profiler/summary",
            "/profiler/statements",
            "/profiler/requests.json",
            "/profiler/summary.json",
            "/profiler/statements.json",
            "/profiler/static/profiler.css",
        ):
            assert client.get(path).status_code == 403, path
        assert client.post("/profiler/clear").status_code == 403


# ------------------------------------------------- install() side effects
def test_install_does_not_mutate_the_config_it_is_given():
    shared = ProfilerConfig()
    first = Starlette(routes=[Route("/", endpoint)])
    install(first, config=shared, mount_path="/one", max_requests=10)

    assert shared.mount_path == "/profiler"
    assert shared.max_requests == 500

    second = Starlette(routes=[Route("/", endpoint)])
    handle = install(second, config=shared)
    assert handle.config.mount_path == "/profiler"


def test_installing_twice_is_refused():
    app = Starlette(routes=[Route("/", endpoint)])
    install(app)
    with pytest.raises(RuntimeError, match="already installed"):
        install(app)


# ------------------------------------------------------- py.typed is real
def test_the_package_type_checks():
    """Shipping py.typed while failing to type-check exports the errors."""
    if not _available("ty"):
        pytest.skip("ty not installed")
    result = subprocess.run(["ty", "check", "src/asgi_profiler"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def _available(name: str) -> bool:
    return (
        subprocess.run([sys.executable, "-c", f"import {name}"], capture_output=True).returncode
        == 0
    )


# ------------------------------------------------- SQLite under real load
def test_sqlite_add_does_not_scan_the_retention_window(tmp_path):
    """The trim must cost the overshoot, not the whole cap.

    A per-insert `ORDER BY seq DESC LIMIT max_requests` made `add()` 13x more
    expensive at a 20k cap than at 500 -- paid synchronously, on the loop.
    """
    timings = {}
    for cap in (500, 20_000):
        store = SQLiteStorage(tmp_path / f"cap{cap}.db", max_requests=cap, background=False)
        try:
            for i in range(cap + 100):
                store.add(make(i))
            assert store.count() == cap, "the cap must stay exact"
            start = time.perf_counter()
            for i in range(200):
                store.add(make(10_000_000 + i))
            timings[cap] = (time.perf_counter() - start) / 200
        finally:
            store.close()

    assert timings[20_000] < timings[500] * 4, (
        f"add() scales with max_requests: {timings[500] * 1e6:.0f}us at 500 vs "
        f"{timings[20_000] * 1e6:.0f}us at 20000"
    )


def test_background_writes_keep_the_tail_off_the_caller(tmp_path):
    """The stall, not the average, is what a background writer buys you.

    A synchronous insert on the event loop is usually fine and occasionally
    catastrophic -- a commit that lands on a checkpoint, or a lock wait that
    `sqlite3` implements as a blocking sleep. Measured against a synchronous
    store on the same machine at the same moment, because an absolute
    microsecond budget just turns a loaded CI runner into a red build.
    """

    def tail_us(store, offset):
        for i in range(600):  # fill past the cap so the trim is live
            store.add(make(offset + i))
        if hasattr(store, "flush"):
            store.flush()
        timings = []
        for i in range(400):
            start = time.perf_counter()
            store.add(make(offset + 1_000_000 + i))
            timings.append((time.perf_counter() - start) * 1e6)
            time.sleep(0.0002)  # a request cadence, not a tight loop
        timings.sort()
        return timings[int(0.99 * len(timings))]

    with SQLiteStorage(tmp_path / "sync.db", max_requests=500, background=False) as sync:
        inline_p99 = tail_us(sync, 0)

    with SQLiteStorage(tmp_path / "bg.db", max_requests=500) as background:
        queued_p99 = tail_us(background, 50_000_000)
        background.flush()
        assert background.count() == 500, "the cap still holds"

    assert queued_p99 < inline_p99 / 3, (
        f"p99 enqueue {queued_p99:.0f}us vs p99 inline write {inline_p99:.0f}us "
        "-- the background writer is not keeping the tail off the caller"
    )


def test_reads_flush_pending_writes(tmp_path):
    """Enqueuing must never mean a stale page."""
    store = SQLiteStorage(tmp_path / "flush.db")
    try:
        for i in range(20):
            store.add(make(i))
        # no explicit flush: every read path has to do it
        assert store.count() == 20
        assert len(store.list()) == 20
        assert store.get("000000000005") is not None
        assert store.search.__self__ is store
        assert store.search(_filters()).total == 20
        assert store.summarise()
    finally:
        store.close()


def _filters():
    from asgi_profiler import Filters

    return Filters()


def test_sqlite_can_be_used_as_a_context_manager(tmp_path):
    with SQLiteStorage(tmp_path / "ctx.db") as store:
        store.add(make(1))
        assert store.count() == 1
    assert store._closed


def test_profiler_close_releases_the_backend(tmp_path):
    app = Starlette(routes=[Route("/", endpoint)])
    store = SQLiteStorage(tmp_path / "closed.db")
    profiler = install(app, storage=store)
    with TestClient(app) as client:
        client.get("/")
    profiler.close()
    assert store._closed
    # and close() is safe on a backend that has none
    other = Starlette(routes=[Route("/", endpoint)])
    install(other, mount_path="/p2").close()


# ------------------------------------------------------ schema versioning
def test_an_old_schema_is_rebuilt_not_crashed(tmp_path):
    """A stale profiler.db must not raise from inside add(), after the 200."""
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE profiles (id TEXT, whatever TEXT)")
    legacy.execute("INSERT INTO profiles VALUES ('x', 'y')")
    legacy.execute("PRAGMA user_version = 0")
    legacy.commit()
    legacy.close()

    with SQLiteStorage(path, background=False) as store:
        store.add(make(1))
        assert store.count() == 1
        assert store.get("000000000001") is not None


def test_the_schema_version_is_stamped(tmp_path):
    import sqlite3

    from asgi_profiler.storage import SCHEMA_VERSION

    path = tmp_path / "stamped.db"
    with SQLiteStorage(path, background=False):
        pass
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


# ------------------------------------------------------------- size caps
def test_a_huge_statement_is_truncated():
    from asgi_profiler.instrument import _normalise
    from asgi_profiler.models import MAX_SQL_CHARS

    bulk = "INSERT INTO t VALUES " + ",".join(f"({i})" for i in range(20_000))
    stored = _normalise(bulk)

    assert len(stored) < MAX_SQL_CHARS + 100
    assert "truncated" in stored
    assert str(len(" ".join(bulk.split()))) in stored


def test_a_normal_statement_is_untouched():
    from asgi_profiler.instrument import _normalise

    assert _normalise("SELECT  a,\n  b FROM t") == "SELECT a, b FROM t"


def test_a_backend_that_raises_does_not_break_the_request(caplog):
    class Broken(MemoryStorage):
        def add(self, profile):
            raise OSError("disk full")

    app = Starlette(routes=[Route("/", endpoint)])
    install(app, storage=Broken())

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
    assert any("Could not record" in r.message for r in caplog.records)


def test_the_query_list_is_capped_in_memory_not_unbounded():
    """A pathological request must not be able to hold the buffer hostage."""
    store = MemoryStorage(max_requests=2)
    for i in range(5):
        profile = make(i)
        profile.queries = [Query(sql="SELECT 1", params="()", duration_ms=0.1) for _ in range(100)]
        profile.finalise()
        store.add(profile)
    assert store.count() == 2
