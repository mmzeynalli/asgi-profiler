# Security

!!! danger "The viewer has no authentication of its own"

    Anything recorded is visible to anyone who can reach the mount path. That
    includes full URLs with query strings, request and response headers, SQL
    statements and their bound parameters.

    Do not expose it on the public internet.

## Options, in rough order of preference

### Do not install it in production

The simplest control. Gate it on an environment variable:

```python
if settings.debug:
    install(app)
```

### Put it behind an admin you already authenticate

If you run [SQLAdmin](../integrations/sqladmin.md), register the profiler as a
view inside it. Every route then goes through SQLAdmin's authentication
backend, and there is nothing new to secure:

```python
from asgi_profiler.contrib.sqladmin import register

profiler = install(app, mount_path=None)
register(admin, profiler)
```

### Guard it yourself

`authorize` is called for every viewer request. Return `False` to deny:

```python
def only_staff(request):
    return request.user.is_staff


install(app, authorize=only_staff)
```

It may be async:

```python
async def only_staff(request):
    user = await load_user(request)
    return user.is_staff
```

!!! warning "Async guards used to fail open"

    A coroutine object is truthy, so an early version of this let every
    request through when `authorize` was async. It is awaited now, and there
    is a regression test for both directions — but if you are pinned below
    0.1.0, check.

### Network-level

Bind it somewhere only your VPN or an SSH tunnel can reach.

## What is stored

- **Headers** listed in `redacted_headers` are stored as `<redacted>` —
  `authorization`, `cookie`, `x-api-key` and friends. Set
  `capture_headers=False` to store no headers at all.
- **Query strings are stored in full.** A token in a URL will be recorded.
- **Bound parameters are stored**, truncated at 500 characters. Personal data
  in a `WHERE` clause ends up in the capture.
- **Request and response bodies are never read**, and so are never stored.

If a `SQLiteStorage` capture leaves the machine, treat the file as containing
whatever your queries contain.
