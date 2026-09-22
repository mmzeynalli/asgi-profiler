"""Stable identity for a SQL statement.

Grouping statements by their literal text is right until it isn't. Two
executions of the same ORM query differ when the `IN` list is a different
length, when a savepoint is numbered, or when a `text()` query was written
with its values inline -- and the profiler then reports one problem as six,
which is the same as reporting nothing.

:func:`sql_hash` answers "are these the same statement?" by normalising the
parts that vary per execution and hashing what is left. The normalisation is
deliberately conservative, in this order:

1. `IN (1, 2, 3)` and `IN (?, ?, ?)` collapse to `IN (%s)`. This is the one
   that matters most: a page of ten rows and a page of eleven issue the same
   query, and without this they are two statements forever.
2. `SAVEPOINT sa_savepoint_3` collapses to `SAVEPOINT %s`. The counter is
   per-transaction, so every nested block would otherwise be unique.
3. Single-quoted strings, numbers and booleans collapse to `%s`.

What is deliberately *not* normalised: double-quoted strings, because
PostgreSQL uses them for identifiers and folding `"users"` into `%s` would
merge every table in the schema into one statement. Nor are bare placeholders
rewritten between dialects -- `?` and `%s` never appear in the same process,
so translating them buys nothing and risks merging statements that differ.

Numbers are matched on word boundaries, so a table named `users_2024` or a
column alias `t1` survives: `_` and letters are word characters, which means
there is no boundary in front of those digits.
"""

from __future__ import annotations

import re
from functools import lru_cache
from hashlib import md5

__all__ = ["is_truncated", "normalise_sql", "sql_hash"]

#: Bump when the normalisation changes. It is part of the hashed input, so an
#: old capture and a new one never silently share a fingerprint -- they group
#: separately, which is visible, rather than merging, which is not.
SCHEME = "1"

#: Bound parameters, in every dialect SQLAlchemy emits: `qmark`, `format`,
#: `numeric` and `named`.
_PLACEHOLDER = r"(?:%s|\?|\$\d+|:\w+)"
_NUMBER = r"-?\b(?:[0-9]+\.)?[0-9]+(?:[eE][+-]?[0-9]+)?\b"
#: `''` is SQL's escape for a literal quote, so it must not end the match.
_STRING = r"'(?:[^']|'')*'"
_BOOL = r"\b(?:TRUE|FALSE)\b"

_IN_ITEM = f"(?:{_PLACEHOLDER}|{_NUMBER}|{_STRING}|{_BOOL})"
_IN_LIST = re.compile(
    rf"\bIN\s*\(\s*{_IN_ITEM}(?:\s*,\s*{_IN_ITEM})*\s*\)",
    re.IGNORECASE,
)
_SAVEPOINT = re.compile(
    r'\bSAVEPOINT\s+(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)',
    re.IGNORECASE,
)
_LITERAL = re.compile(f"{_STRING}|{_NUMBER}|{_BOOL}", re.IGNORECASE)

#: Appended by `instrument._normalise` when a statement is too long to keep.
#: A truncated statement cannot be compared honestly -- the part that differs
#: may be the part that was cut -- so detectors refuse to fingerprint one.
TRUNCATION_MARKER = "... [truncated,"


def is_truncated(sql: str) -> bool:
    """Was this statement stored with its tail cut off?"""
    return sql.endswith("chars]") and TRUNCATION_MARKER in sql


def normalise_sql(sql: str) -> str:
    """The statement with per-execution values replaced by `%s`.

    Readable on purpose: it is what the viewer shows when it needs to name a
    group of statements rather than one execution of it.
    """
    text = " ".join(sql.split())
    text = _IN_LIST.sub("IN (%s)", text)
    text = _SAVEPOINT.sub("SAVEPOINT %s", text)
    return _LITERAL.sub("%s", text)


@lru_cache(maxsize=4096)
def sql_hash(sql: str) -> str:
    """A short, stable identity for `sql`.

    Cached because the whole point of the caller is repetition: an N+1 asks
    this the same question five hundred times in a row, and the cache turns
    all but the first into a dictionary lookup.

    Not a security primitive -- md5 is here because it is fast and 64 bits of
    it is plenty to tell two statements apart within one application.
    """
    digest = md5(normalise_sql(sql).encode("utf-8", "replace"), usedforsecurity=False)
    return f"{SCHEME}-{digest.hexdigest()[:16]}"
