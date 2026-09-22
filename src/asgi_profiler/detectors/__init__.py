"""Turning recorded statements into findings.

A detector is a function `(Profile, Settings) -> Iterable[Problem]`. It reads
a finalised profile and concludes something about it; it never touches
storage, configuration or the request.

Detectors run in :meth:`asgi_profiler.middleware.ProfilerMiddleware.__call__`
after the response has been sent, so their cost is off the latency path.

Adding one: write the module, export `TYPE` and `detect`, and add it to
`DETECTORS` below. The name in `TYPE` is public -- it appears in the JSON, in
`ProfilerConfig.detectors`, and in anything a user has written a CI assertion
against -- so it is chosen once and not renamed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from ..models import Problem, Profile
from . import blocking, n_plus_one, slow_query
from .settings import (
    BLOCKING_MS,
    N_PLUS_ONE_COUNT,
    N_PLUS_ONE_MS,
    SLOW_QUERY_MS,
    Settings,
)

logger = logging.getLogger("asgi_profiler")

Detector = Callable[[Profile, Settings], Iterable[Problem]]

#: In the order they are defined, though the result is sorted by cost.
DETECTORS: tuple[tuple[str, Detector], ...] = (
    (n_plus_one.TYPE, n_plus_one.detect),
    (blocking.TYPE, blocking.detect),
    (slow_query.TYPE, slow_query.detect),
)

#: Every detector name, for validating `ProfilerConfig.detectors` and for
#: documenting what can be turned off.
TYPES: tuple[str, ...] = tuple(name for name, _ in DETECTORS)

#: Short names for the badges. Defined beside the detectors rather than in a
#: template so that adding a detector updates every page that lists them.
LABELS: dict[str, str] = {
    n_plus_one.TYPE: "N+1",
    blocking.TYPE: "Blocking",
    slow_query.TYPE: "Slow query",
}

#: Which of the viewer's existing pill colours each one wears. Blocking reads
#: as an error because it damages requests other than the one being looked at,
#: which is worse than being slow.
SEVERITY: dict[str, str] = {
    n_plus_one.TYPE: "warn",
    blocking.TYPE: "err",
    slow_query.TYPE: "warn",
}

__all__ = [
    "BLOCKING_MS",
    "DETECTORS",
    "LABELS",
    "N_PLUS_ONE_COUNT",
    "N_PLUS_ONE_MS",
    "SEVERITY",
    "SLOW_QUERY_MS",
    "TYPES",
    "Detector",
    "Settings",
    "detect",
]


def detect(profile: Profile, settings: Settings | None = None) -> list[Problem]:
    """Every problem found in `profile`, most expensive first.

    Never raises. A detector that throws is logged and skipped, and the other
    detectors still run: a profiler that turns a served request into a 500
    because it could not decide whether the request was slow has failed at the
    only thing it must not do.
    """
    settings = settings or Settings()
    found: list[Problem] = []
    for name, detector in DETECTORS:
        if not settings.runs(name):
            continue
        try:
            found.extend(detector(profile, settings))
        except Exception:  # pragma: no cover - a detector bug
            logger.warning("Detector %s failed on profile %s", name, profile.id, exc_info=True)
    found.sort(key=lambda p: p.time_ms, reverse=True)
    return found
