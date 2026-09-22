# Findings

The profiler records what happened. The detectors say what is wrong with it.

`duplicate_count = 11` is a number you have to interpret. *"The same statement
ran twelve times from `repository.py:20`, and the query before it returned
twelve rows"* is a finding you can act on. Every recorded request is run
through the detectors once, after the response has gone out, and what they
conclude appears as **Findings** at the top of the request detail page, as a
badge in the request list, and under `problems` in the JSON.

## What is detected

### N+1 query

The same statement, repeated from the same line.

Three conditions have to hold, and the third is the one nothing else can do:

- at least `n_plus_one_count` executions (five by default),
- occupying at least `n_plus_one_ms` of **non-overlapping** time (zero by
  default — see below),
- **all issued through one shared application frame**.

That last test is why the finding can name a line rather than a query. The
deepest frame every execution passed through is the loop, and the finding is
fingerprinted on it. The statement that ran immediately before the repeats is
reported as well: those twelve queries exist because *that* one returned
twelve rows, and seeing both together is usually the whole diagnosis.

Repetition that fails the shared-frame test is not reported. A serialiser and
a permission check that happen to ask the same question are not a loop, and
sending someone to look for one wastes their afternoon. Transaction
bookkeeping — `SAVEPOINT`, `COMMIT`, `BEGIN` — is excluded for the same
reason: it repeats by nature.

### Blocking database call

A synchronous driver call made **on the event loop**.

This is the quiet one. A sync `Session` inside an `async def` endpoint raises
nothing, passes its tests, and looks fine in isolation — because in isolation
it *is* fine. Under concurrency it freezes every other request in flight for
the duration of every query, and the latency lands on endpoints that have
nothing to do with it. A production tracer sees the symptom on the wrong
endpoint and the cause on none.

The profiler can tell the three cases apart because it sees the thread and
the greenlet at the moment the query runs:

| How the query ran | Blocking? |
| --- | --- |
| `def` endpoint (Starlette's worker thread) | No |
| `async def` + async engine (SQLAlchemy's greenlet yields) | No |
| `async def` + sync `Session` | **Yes** |

The fix is an async engine with `AsyncSession`, or
`starlette.concurrency.run_in_threadpool`, or simply making the endpoint `def`
instead of `async def`.

### Slow query

One statement at or over `slow_query_issue_ms` (100 ms by default), reported
once per distinct statement however many times it ran.

## Why two thresholds default to zero

Sentry requires 50 ms of database time before it calls repetition an N+1.
That is right for a service watching production, and exactly wrong here.

The value of finding an N+1 in development is that it *has not reached
production yet* — and in development the table has twelve rows, so five
hundred repeated queries come back in four milliseconds. A duration floor
tuned for production data filters out precisely the findings you still have
time to act on. The count and the shared call site are the signal; duration is
there to be raised when you profile against a production-sized database.

The same reasoning applies to `blocking_query_ms`. A synchronous call on the
event loop is the wrong shape whether your development database answers in one
millisecond or the production one takes a hundred.

## Fingerprints

Every finding carries one:

```text
1-n_plus_one_db-20837a7b58c1f5cd
```

It is the identity of the *problem*, not of the request. The same N+1 seen on
four hundred requests has one fingerprint — built from the route, the shared
frame, and the normalised text of both the repeated statement and the one
before it. That is what will let a later release group findings across
requests, and what lets CI tell a new problem from a known one today:

```python
known = {"1-n_plus_one_db-20837a7b58c1f5cd"}
found = {p["fingerprint"] for r in requests for p in r["problems"]}
assert not (found - known), "new performance problem"
```

The leading `1-` is the scheme version. It changes if the grouping rule ever
changes, so old and new findings separate visibly instead of merging quietly.

## Configuring them

```python
install(
    app,
    detectors=["n_plus_one_db"],  # only this one; () for none, None for all
    n_plus_one_count=10,  # stricter
    n_plus_one_ms=100.0,  # profiling against production-sized data
    slow_query_issue_ms=250.0,
)
```

Names are in `asgi_profiler.DETECTOR_TYPES`. An unknown name is ignored rather
than raising — a detector removed in a later release cannot break your
configuration — and `ProfilerConfig.unknown_detectors()` will tell you which
names went unrecognised.

A detector that raises is logged and skipped, and the rest still run. A
profiler that turns a served request into a 500 because it could not decide
whether the request was slow has failed at the only thing it must not do.

## Running them yourself

They are ordinary functions over a recorded profile, with no dependency on
configuration, storage or the request:

```python
from asgi_profiler import detect, DetectorSettings

for problem in detect(profile, DetectorSettings(n_plus_one_count=3)):
    print(problem.type, problem.title, problem.evidence["frame"])
```

Which is also how you would use them over a capture file, without the
application running at all — see [Storage](storage.md).
