"""Async engines, and the class-level listener that makes SQLModel work."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asgi_profiler import install


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = 'widget'
    id = Column(Integer, primary_key=True)
    name = Column(String)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_async_engine_queries_are_captured(tmp_path):
    """AsyncEngine drives a sync Engine underneath, where our events live."""
    db = tmp_path / 'async.db'

    sync_engine = create_engine(f'sqlite:///{db}')
    Base.metadata.create_all(sync_engine)
    with sessionmaker(bind=sync_engine)() as session:
        session.add_all([Widget(id=i, name=f'w{i}') for i in range(1, 4)])
        session.commit()
    sync_engine.dispose()

    async_engine = create_async_engine(f'sqlite+aiosqlite:///{db}')
    async_session = async_sessionmaker(bind=async_engine, class_=AsyncSession)

    async def widgets(request):
        async with async_session() as session:
            rows = (await session.scalars(select(Widget))).all()
            return JSONResponse([{'id': w.id} for w in rows])

    app = Starlette(routes=[Route('/widgets', widgets)])
    profiler = install(app)

    with TestClient(app) as client:
        assert client.get('/widgets').status_code == 200

    profile = profiler.profiles[0]
    assert profile.query_count >= 1
    assert any(q.sql.upper().startswith('SELECT') for q in profile.queries)
    assert profile.query_ms > 0


def test_every_engine_in_the_process_is_captured(tmp_path):
    """The listener is on the Engine *class*, not on one engine instance.

    This is why SQLModel needs no special support: `sqlmodel.create_engine`
    returns a plain SQLAlchemy `Engine` and `sqlmodel.Session` subclasses
    `sqlalchemy.orm.Session`, so the same cursor events fire. The same goes for
    a second engine the application creates later.
    """
    first = create_engine(f'sqlite:///{tmp_path / "one.db"}')
    second = create_engine(f'sqlite:///{tmp_path / "two.db"}')
    for engine in (first, second):
        Base.metadata.create_all(engine)

    maker_one = sessionmaker(bind=first)
    maker_two = sessionmaker(bind=second)

    async def both(request):
        with maker_one() as s:
            s.scalars(select(Widget)).all()
        with maker_two() as s:
            s.scalars(select(Widget)).all()
        return JSONResponse({'ok': True})

    app = Starlette(routes=[Route('/both', both)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get('/both')

    profile = profiler.profiles[0]
    selects = [q for q in profile.queries if q.sql.upper().startswith('SELECT')]
    assert len(selects) == 2, 'expected one SELECT from each engine'


def test_queries_outside_a_request_are_ignored(tmp_path):
    """Startup work, migrations and shell use must not leak into a profile."""
    engine = create_engine(f'sqlite:///{tmp_path / "x.db"}')
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)

    async def noop(request):
        return JSONResponse({'ok': True})

    app = Starlette(routes=[Route('/noop', noop)])
    profiler = install(app)

    # outside any request
    with maker() as session:
        session.scalars(select(Widget)).all()

    with TestClient(app) as client:
        client.get('/noop')

    assert profiler.profiles[0].query_count == 0


def test_sqlmodel_is_captured_with_no_special_support(tmp_path):
    """The README claims SQLModel support; this is what makes that true.

    `sqlmodel.create_engine` returns a SQLAlchemy `Engine` and
    `sqlmodel.Session` subclasses `sqlalchemy.orm.Session`, so the class-level
    cursor listeners fire unchanged. That is the whole argument -- worth an
    executed assertion rather than a comment.
    """
    sqlmodel = pytest.importorskip('sqlmodel')

    class Item(sqlmodel.SQLModel, table=True):
        __tablename__ = 'sqlmodel_item'
        id: int | None = sqlmodel.Field(default=None, primary_key=True)
        name: str

    engine = sqlmodel.create_engine(f'sqlite:///{tmp_path / "sm.db"}')
    sqlmodel.SQLModel.metadata.create_all(engine)
    with sqlmodel.Session(engine) as session:
        session.add(Item(id=1, name='one'))
        session.add(Item(id=2, name='two'))
        session.commit()

    async def items(request):
        with sqlmodel.Session(engine) as session:
            rows = session.exec(sqlmodel.select(Item)).all()
            return JSONResponse([r.name for r in rows])

    app = Starlette(routes=[Route('/items', items)])
    profiler = install(app)

    try:
        with TestClient(app) as client:
            assert client.get('/items').json() == ['one', 'two']

        profile = profiler.profiles[0]
        assert profile.query_count >= 1
        assert any(q.sql.upper().startswith('SELECT') for q in profile.queries)
        assert any('sqlmodel_item' in q.sql for q in profile.queries)
        # and the stack still names this test, not sqlmodel's internals
        stack = profile.queries[0].stack
        assert any('test_async_and_engines' in frame for frame in stack)
        assert not any('sqlmodel' in frame for frame in stack)
    finally:
        engine.dispose()
