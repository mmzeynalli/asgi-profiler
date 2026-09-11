"""Regressions for the bugs fixed in 0.2.0, one test per bug.

Each of these fails on 0.1.0.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asgi_profiler import install, summarise


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = 'widget'
    id = Column(Integer, primary_key=True)
    name = Column(String)


@pytest.fixture
def session_maker(tmp_path):
    engine = create_engine(
        f'sqlite:///{tmp_path / "r.db"}',
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    with maker() as session:
        session.add_all([Widget(id=i, name=f'w{i}') for i in range(1, 4)])
        session.commit()
    yield maker
    engine.dispose()


# ------------------------------------------------------------------- B1
def test_mount_prefix_does_not_swallow_sibling_paths():
    """`/profiler` must not exclude an application's own `/profiler-admin`."""

    async def endpoint(request):
        return PlainTextResponse('real endpoint')

    app = Starlette(
        routes=[
            Route('/profiler-admin/thing', endpoint),
            Route('/profilerish', endpoint),
        ]
    )
    profiler = install(app)

    with TestClient(app) as client:
        client.get('/profiler-admin/thing')
        client.get('/profilerish')
        client.get('/profiler/')  # the viewer itself: still excluded

    assert sorted(p.path for p in profiler.profiles) == [
        '/profiler-admin/thing',
        '/profilerish',
    ]


def test_exclude_paths_match_on_segment_boundaries():
    async def endpoint(request):
        return PlainTextResponse('ok')

    app = Starlette(
        routes=[
            Route('/health', endpoint),
            Route('/health/db', endpoint),
            Route('/healthcheck-v2', endpoint),
        ]
    )
    profiler = install(app, exclude_paths=['/health'])

    with TestClient(app) as client:
        client.get('/health')
        client.get('/health/db')
        client.get('/healthcheck-v2')

    assert [p.path for p in profiler.profiles] == ['/healthcheck-v2']


# ------------------------------------------------------------------- B2
def test_capture_stacks_is_honoured_on_every_install(session_maker):
    """The listeners are attached once; their settings must stay live."""

    async def query(request):
        with session_maker() as session:
            session.scalars(select(Widget)).all()
        return JSONResponse({})

    first = Starlette(routes=[Route('/q', query)])
    install(first, capture_stacks=True)  # claims the process-wide listeners

    second = Starlette(routes=[Route('/q', query)])
    profiler = install(second, capture_stacks=False, mount_path='/p2')

    with TestClient(second) as client:
        client.get('/q')

    assert profiler.profiles[0].queries[0].stack == []


def test_stack_depth_is_honoured_on_every_install(session_maker):
    async def query(request):
        with session_maker() as session:
            session.scalars(select(Widget)).all()
        return JSONResponse({})

    app = Starlette(routes=[Route('/q', query)])
    install(app, stack_depth=8)

    other = Starlette(routes=[Route('/q', query)])
    profiler = install(other, stack_depth=2, mount_path='/p3')

    with TestClient(other) as client:
        client.get('/q')

    assert len(profiler.profiles[0].queries[0].stack) <= 2


# ------------------------------------------------------------------- B3
def test_stack_frames_are_ordered_innermost_last(session_maker):
    async def query(request):
        with session_maker() as session:
            session.scalars(select(Widget)).all()
        return JSONResponse({})

    app = Starlette(routes=[Route('/q', query)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get('/q')

    stack = profiler.profiles[0].queries[0].stack
    assert stack
    assert 'in query' in stack[-1], 'the line that issued the query comes last'
    assert not any('sqlalchemy' in frame for frame in stack)


# ------------------------------------------------------------------- B5
def test_failed_statements_are_recorded(session_maker):
    async def boom(request):
        with session_maker() as session:
            try:
                session.execute(text('SELECT * FROM does_not_exist'))
            except Exception:
                session.rollback()
            session.execute(text('SELECT 1'))
        return JSONResponse({})

    app = Starlette(routes=[Route('/boom', boom)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get('/boom')

    profile = profiler.profiles[0]
    assert profile.query_count == 2
    failed = [q for q in profile.queries if q.failed]
    assert len(failed) == 1
    assert 'does_not_exist' in failed[0].sql
    assert 'OperationalError' in failed[0].error
    assert profile.error_count == 1
    assert not profile.queries[1].failed


def test_failed_statements_do_not_strand_a_timestamp(session_maker):
    """The start time must be popped, or it accumulates on the connection."""
    engine = session_maker.kw['bind']

    async def boom(request):
        with session_maker() as session:
            for _ in range(5):
                try:
                    session.execute(text('SELECT * FROM nope'))
                except Exception:
                    session.rollback()
        return JSONResponse({})

    app = Starlette(routes=[Route('/boom', boom)])
    install(app)

    with TestClient(app) as client:
        client.get('/boom')

    with engine.connect() as conn:
        assert not conn.info.get('_profiler_started')


def test_failed_query_shows_in_the_viewer(session_maker):
    async def boom(request):
        with session_maker() as session:
            try:
                session.execute(text('SELECT * FROM does_not_exist'))
            except Exception:
                session.rollback()
        return JSONResponse({})

    app = Starlette(routes=[Route('/boom', boom)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get('/boom')
        page = client.get(f'/profiler/request/{profiler.profiles[0].id}')
        assert 'failed' in page.text
        assert 'OperationalError' in page.text

        listing = client.get('/profiler/?only=errors')
        assert '/boom' in listing.text


# ------------------------------------------------------------------- B6
def test_clear_rejects_cross_site_requests():
    async def endpoint(request):
        return PlainTextResponse('ok')

    app = Starlette(routes=[Route('/', endpoint)])
    profiler = install(app)

    with TestClient(app) as client:
        client.get('/')
        assert profiler.profiles

        denied = client.post(
            '/profiler/clear',
            headers={'origin': 'https://evil.example'},
            follow_redirects=False,
        )
        assert denied.status_code == 403
        assert profiler.profiles, 'history must survive a cross-site POST'

        denied = client.post(
            '/profiler/clear',
            headers={'sec-fetch-site': 'cross-site'},
            follow_redirects=False,
        )
        assert denied.status_code == 403

        allowed = client.post(
            '/profiler/clear',
            headers={'sec-fetch-site': 'same-origin'},
            follow_redirects=False,
        )
        assert allowed.status_code == 303
    assert profiler.profiles == []


# ------------------------------------------------------------------- G1
def test_summary_groups_parameterised_routes():
    async def user(request):
        return JSONResponse({'id': request.path_params['user_id']})

    app = Starlette(routes=[Route('/users/{user_id}', user)])
    profiler = install(app)

    with TestClient(app) as client:
        for i in range(3):
            client.get(f'/users/{i}')

    rows = summarise(profiler.profiles)
    assert len(rows) == 1
    assert rows[0].path == '/users/{user_id}'
    assert rows[0].count == 3
    # the concrete path is still recorded on each profile
    assert sorted(p.path for p in profiler.profiles) == [
        '/users/0',
        '/users/1',
        '/users/2',
    ]


def test_route_pattern_from_fastapi():
    fastapi = pytest.importorskip('fastapi')

    app = fastapi.FastAPI()

    @app.get('/items/{item_id}/tags/{tag}')
    def item(item_id: int, tag: str):
        return {}

    profiler = install(app)
    with TestClient(app) as client:
        client.get('/items/7/tags/red')

    assert profiler.profiles[0].route == '/items/{item_id}/tags/{tag}'


def test_route_filter_drills_into_one_endpoint():
    async def user(request):
        return JSONResponse({})

    async def other(request):
        return JSONResponse({})

    app = Starlette(routes=[Route('/users/{user_id}', user), Route('/other', other)])
    install(app)

    with TestClient(app) as client:
        client.get('/users/1')
        client.get('/users/2')
        client.get('/other')

        page = client.get('/profiler/?route=/users/{user_id}')
        assert '/users/1' in page.text
        assert '/users/2' in page.text
        assert '/other' not in page.text

        summary = client.get('/profiler/summary')
        assert '/users/{user_id}' in summary.text


def test_paths_without_parameters_group_by_path():
    async def endpoint(request):
        return JSONResponse({})

    app = Starlette(routes=[Route('/static-path', endpoint)])
    profiler = install(app)
    with TestClient(app) as client:
        client.get('/static-path')

    profile = profiler.profiles[0]
    assert profile.route in ('', '/static-path')
    assert profile.group == '/static-path'


def test_route_reconstruction_handles_repeated_values():
    """`/2/users/2` must not rewrite the wrong segment."""

    async def endpoint(request):
        return JSONResponse({})

    app = Starlette(
        routes=[
            Route('/{tenant}/users/{user_id}', endpoint),
            Route('/files/{rest:path}', endpoint),
        ]
    )
    profiler = install(app)

    with TestClient(app) as client:
        client.get('/2/users/2')
        client.get('/7/users/9')
        client.get('/files/a/b/c.txt')

    by_path = {p.path: p.route for p in profiler.profiles}
    assert by_path['/2/users/2'] == '/{tenant}/users/{user_id}'
    assert by_path['/7/users/9'] == '/{tenant}/users/{user_id}'
    # the route's own declaration, converter and all -- what you would grep for
    assert by_path['/files/a/b/c.txt'] == '/files/{rest:path}'

    rows = summarise(profiler.profiles)
    assert {r.path for r in rows} == {
        '/{tenant}/users/{user_id}',
        '/files/{rest:path}',
    }


def test_route_pattern_is_exact_for_typed_converters():
    """`{uid:int}` matching `007` cannot be recovered from the value alone."""

    async def endpoint(request):
        return JSONResponse({})

    app = Starlette(
        routes=[
            Route('/u/{uid:int}', endpoint),
            Route('/x/{v:float}', endpoint),
            Route('/users/{name}', endpoint),
        ]
    )
    profiler = install(app)

    with TestClient(app) as client:
        client.get('/u/007')
        client.get('/x/1.50')
        client.get('/users/users')  # value collides with a literal segment

    by_path = {p.path: p.route for p in profiler.profiles}
    assert by_path['/u/007'] == '/u/{uid:int}'
    assert by_path['/x/1.50'] == '/x/{v:float}'
    assert by_path['/users/users'] == '/users/{name}'

    # and /u/007 and /u/7 must land on the same summary row
    with TestClient(app) as client:
        client.get('/u/7')
    rows = [r for r in summarise(profiler.profiles) if r.path == '/u/{uid:int}']
    assert len(rows) == 1 and rows[0].count == 2


def test_mounted_apps_keep_the_full_path():
    """`root_path` must not be prepended to a path that already carries it."""
    from starlette.routing import Mount

    async def endpoint(request):
        return JSONResponse({})

    child = Starlette(routes=[Route('/users/{name}', endpoint)])
    profiler = install(child)
    parent = Starlette(routes=[Mount('/api', app=child)])

    with TestClient(parent) as client:
        client.get('/api/users/alice')
        client.get('/api/profiler/')  # the viewer: must stay excluded

    assert [p.path for p in profiler.profiles] == ['/api/users/alice']


def test_proxy_root_path_is_not_doubled():
    async def endpoint(request):
        return JSONResponse({})

    app = Starlette(routes=[Route('/users/{name}', endpoint)])
    profiler = install(app)

    with TestClient(app, root_path='/api') as client:
        client.get('/users/bob')

    assert profiler.profiles[0].path == '/api/users/bob'
