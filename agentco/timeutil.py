"""One rule for naive datetimes: they are UTC.

`dt.astimezone(timezone.utc)` on a NAIVE datetime treats it as host-local time,
so a naive `now=` was shifted by the machine's UTC offset (A13): the same call
stored different instants on a laptop and on a UTC server. The rest of the
codebase already reads an unzoned stamp as UTC when it parses one; this is the
matching rule for the write side, kept in one place so it cannot drift per module.
"""

from __future__ import annotations

from datetime import datetime, timezone


def as_utc(dt: datetime) -> datetime:
    """`dt` as an aware UTC datetime. A naive value is taken to already BE UTC."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """ISO-8601 in UTC, with an explicit `+00:00`."""
    return as_utc(dt).isoformat()
