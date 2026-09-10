"""Attribution under concurrency, and the `Storage` Protocol as a seam.

The contextvar-holding-a-mutable-list is the load-bearing trick in this
library. If it is ever broken, queries bleed between in-flight requests and
every number in the UI becomes a lie -- so it is worth a test that actually
runs requests at the same time rather than one after another.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from starlette_profiler import BaseStorage, Profile, install


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widget"
    id = Column(Integer, primary_key=True)
    name = Column(String)


@pytest.fixture
def maker(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'c.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_concurrent_requests_do_not_bleed_queries(maker):
    """Each request runs a different number of queries, all at once."""

    async def run_n(request):
        n = int(request.path_params["n"])

        def work():
            with maker() as session:
                for _ in range(n):
                    session.execute(text("SELECT 1"))

        await asyncio.to_thread(work)
        return JSONResponse({"n": n})

    app = Starlette(routes=[Route("/n/{n}", run_n)])
    profiler = install(app)

    async def hammer():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await asyncio.gather(*[c.get(f"/n/{n}") for n in range(1, 9)])

    asyncio.run(hammer())

    recorded = {p.path: p.query_count for p in profiler.profiles}
    assert recorded == {f"/n/{n}": n for n in range(1, 9)}


def test_queries_from_a_gather_land_on_one_profile(maker):
    async def fan_out(request):
        def work(i):
            with maker() as session:
                session.execute(text(f"SELECT {i}"))

        await asyncio.gather(*[asyncio.to_thread(work, i) for i in range(5)])
        return JSONResponse({})

    app = Starlette(routes=[Route("/fan", fan_out)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get("/fan")

    assert profiler.profiles[0].query_count == 5


def test_a_custom_storage_only_needs_the_core_methods():
    """`BaseStorage` supplies search/summarise, so a backend stays small."""

    class ListStorage(BaseStorage):
        def __init__(self) -> None:
            self.items: list[Profile] = []

        def add(self, profile: Profile) -> None:
            self.items.insert(0, profile)

        def get(self, profile_id: str) -> Profile | None:
            return next((p for p in self.items if p.id == profile_id), None)

        def list(self, *, limit=None, offset=0) -> list[Profile]:
            items = self.items[offset:]
            return items[:limit] if limit is not None else items

        def count(self) -> int:
            return len(self.items)

        def clear(self) -> None:
            self.items.clear()

    async def endpoint(request):
        return JSONResponse({})

    app = Starlette(routes=[Route("/thing/{n}", endpoint)])
    storage = ListStorage()
    install(app, storage=storage, page_size=2)

    with TestClient(app) as client:
        for i in range(5):
            client.get(f"/thing/{i}")

        listing = client.get("/profiler/")
        assert listing.status_code == 200
        assert "Page 1 of 3" in listing.text

        summary = client.get("/profiler/summary")
        assert summary.status_code == 200
        assert "/thing/{n}" in summary.text

    assert storage.count() == 5
