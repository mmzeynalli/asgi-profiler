# JSON

Every viewer page has a JSON twin, for scripting, dashboards or a CI check.
They are served from the mount path and are subject to the same `authorize`
guard as the HTML.

| Endpoint | Returns |
| --- | --- |
| `GET {mount}/requests.json` | One page of requests, with the pager |
| `GET {mount}/summary.json` | Per-route summary rows |
| `GET {mount}/statements.json` | Statements aggregated across requests |
| `GET {mount}/request/{id}.json` | One request, with every statement |

The list endpoints take the same query parameters as the pages: `q`, `method`,
`status`, `route`, `min_ms`, `only`, `order`, `page`.

```console
curl -s localhost:8000/profiler/summary.json | jq '.routes[0]'
```

```json
{
  "method": "GET",
  "path": "/orders/{order_id}",
  "count": 19,
  "p50_ms": 58.4,
  "p95_ms": 68.9,
  "p99_ms": 68.9,
  "total_ms": 1059.3,
  "avg_queries": 12.8,
  "total_duplicates": 189,
  "total_errors": 0
}
```

## Failing a build on an N+1

The handle is usually easier than HTTP for this:

```python
n_plus_one = [p for p in profiler.profiles if p.duplicate_count]
assert not n_plus_one, [p.full_path for p in n_plus_one]
```

Run your integration suite with the profiler installed, then assert on what it
recorded.
