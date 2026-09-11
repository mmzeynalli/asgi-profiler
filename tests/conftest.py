from __future__ import annotations

import pytest

from asgi_profiler import install_sql_hooks


@pytest.fixture(autouse=True)
def _reset_instrumentation():
    """Put the global capture settings back to their defaults after each test.

    The SQLAlchemy listeners live on the `Engine` class, so they are process
    global by design. Their *settings* must not leak between tests -- a test
    that installs with `capture_stacks=False` used to dictate that for every
    test after it.
    """
    yield
    install_sql_hooks(capture_stacks=True, stack_depth=8)


@pytest.fixture
def anyio_backend():
    return "asyncio"
