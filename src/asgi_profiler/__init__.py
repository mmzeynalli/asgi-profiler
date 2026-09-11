"""asgi-profiler -- request and SQL profiling for ASGI applications.

    from fastapi import FastAPI
    from asgi_profiler import install

    app = FastAPI()
    install(app)            # viewer at /profiler

Records every request the app handles along with the SQL each one ran, and
serves a browsable UI. Works with Starlette, FastAPI, SQLAlchemy and
SQLModel, sync and async.
"""

from __future__ import annotations

import contextlib
import dataclasses
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from typing import Any

from .config import ProfilerConfig
from .instrument import install as install_sql_hooks
from .instrument import uninstall as uninstall_sql_hooks
from .middleware import ProfilerMiddleware
from .models import (
    PathSummary,
    Profile,
    Query,
    QueryGroup,
    StatementSummary,
    group_queries,
)
from .routing import route_pattern
from .storage import (
    BaseStorage,
    Filters,
    MemoryStorage,
    Page,
    SQLiteStorage,
    Storage,
    aggregate_statements,
    summarise,
)
from .viewer import build_viewer

__all__ = [
    'BaseStorage',
    'Filters',
    'MemoryStorage',
    'Page',
    'PathSummary',
    'Profile',
    'Profiler',
    'ProfilerConfig',
    'ProfilerMiddleware',
    'Query',
    'QueryGroup',
    'SQLiteStorage',
    'StatementSummary',
    'Storage',
    'aggregate_statements',
    'build_viewer',
    'group_queries',
    'install',
    'install_sql_hooks',
    'route_pattern',
    'summarise',
    'uninstall_sql_hooks',
]

try:
    #: Read from the installed distribution rather than repeated here. A
    #: hand-maintained literal drifts from `pyproject.toml` silently, and the
    #: release only compares the tag against the packaged version -- so a
    #: wheel can ship saying 0.1.0 in its metadata and 0.0.1 in its code.
    __version__ = _installed_version('asgi-profiler')
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = '0.0.0.dev0'

#: Marker set on an app that already has the profiler, so a second
#: `install()` is refused rather than silently doubling every count.
#:
#: An attribute on the app rather than a set of `id()`s: CPython reuses the
#: address of a collected object, so an id-keyed set reports a false positive
#: as soon as an earlier app is garbage collected.
_MARKER = '_asgi_profiler_installed'


class Profiler:
    """Handle returned by :func:`install`, for programmatic access."""

    def __init__(self, storage: Storage, config: ProfilerConfig) -> None:
        self.storage = storage
        self.config = config

    @property
    def profiles(self) -> list[Profile]:
        """Every retained profile.

        Convenient, but it hydrates the whole history -- on a large
        :class:`SQLiteStorage` that is every statement of every request.
        Prefer :meth:`slowest` or :meth:`search` when the history is big.
        """
        return self.storage.list()

    def search(self, filters: Filters | None = None, **kwargs: Any) -> Page:
        """One page of profiles, filtered and ordered by the backend."""
        return self.storage.search(filters or Filters(), **kwargs)

    def slowest(self, count: int = 10) -> list[Profile]:
        return self.storage.search(Filters(order='slowest'), page=1, size=count).items

    def summary(self) -> list[PathSummary]:
        return self.storage.summarise()

    def statements(self, limit: int = 100) -> list[StatementSummary]:
        return self.storage.statements(limit)

    def clear(self) -> None:
        self.storage.clear()

    def close(self) -> None:
        """Release the storage backend. Safe to call on any backend."""
        closer = getattr(self.storage, 'close', None)
        if callable(closer):
            closer()


def install(
    app: Any,
    config: ProfilerConfig | None = None,
    storage: Storage | None = None,
    **options: Any,
) -> Profiler:
    """Add the middleware and mount the viewer.

    Args:
        app: a Starlette or FastAPI application. Call this before the app
            starts serving -- middleware cannot be added afterwards.
        config: a :class:`ProfilerConfig`. Keyword options are merged into a
            *copy*, so a shared config object is never rewritten under you.
        storage: a custom store; defaults to :class:`MemoryStorage`. Pass
            :class:`SQLiteStorage` to keep history across restarts and share
            it between workers.
        **options: shorthand for `ProfilerConfig` fields, e.g.
            ``install(app, mount_path="/_perf", capture_stacks=False)``.

    Returns:
        A :class:`Profiler` handle wrapping the storage.

    Raises:
        RuntimeError: if this app already has the profiler installed.
        TypeError: on an unknown option, or an `authorize` that is not usable.
    """
    config = dataclasses.replace(config) if config is not None else ProfilerConfig()
    for key, value in options.items():
        if not hasattr(config, key):
            raise TypeError(f'Unknown profiler option: {key!r}')
        setattr(config, key, value)

    if getattr(app, _MARKER, False):
        raise RuntimeError(
            'The profiler is already installed on this app. Installing twice '
            'adds a second middleware and a second mount, so every request is '
            'recorded twice into two separate stores.'
        )

    if storage is None:
        # `is None`, never `or`: an empty MemoryStorage is falsy, so `or` threw
        # away the caller's store and quietly recorded into a different one.
        storage = MemoryStorage(max_requests=config.max_requests)

    install_sql_hooks(
        capture_stacks=config.capture_stacks,
        stack_depth=config.stack_depth,
    )
    app.add_middleware(ProfilerMiddleware, storage=storage, config=config)
    app.mount(
        config.mount_path,
        build_viewer(storage, config, prefix=config.mount_path),
        name='profiler',
    )
    with contextlib.suppress(AttributeError):  # an app defining __slots__
        setattr(app, _MARKER, True)

    return Profiler(storage, config)
