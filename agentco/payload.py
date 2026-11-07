"""One coercion layer for caller-supplied fields.

A payload field arrives as whatever JSON the caller wrote. Left to the code
below, a wrong type surfaces as `TypeError`, `AttributeError`, `OverflowError`
or `sqlite3.ProgrammingError` from somewhere deep, and the generic handler
reports it as a registry bug. These helpers are the single place a field is
checked, and every failure is a `Refusal(http_status=400)` that names the field,
the type wanted and the type received — never the value's repr beyond a short
excerpt, and never an exception message.

Both transports can call them: the HTTP boundary uses the `payload`-keyed forms,
and core code that has only a value (a `ttl_seconds` argument) uses
`int_value` / `str_value`.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from agentco.errors import Refusal

#: Generous but finite, so `timedelta(seconds=x)` and `datetime + delta` cannot overflow.
MAX_INT = 2**31 - 1


def _excerpt(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def refuse(field: str, wanted: str, got: Any, *, code: str = "invalid_request") -> Refusal:
    return Refusal(
        code=code,
        message=f"{field} must be {wanted}, got {type(got).__name__} {_excerpt(got)}",
        remediation=f"Send {field} as {wanted}.",
        http_status=400,
    )


def int_value(
    field: str,
    value: Any,
    *,
    default: Optional[int] = None,
    lo: Optional[int] = None,
    hi: int = MAX_INT,
    code: str = "not_an_integer",
) -> int:
    """A finite whole number in [lo, hi]. `None` and `""` mean absent.

    Accepts an int, an integral float, or a digit string (a query parameter is
    always text). Rejects bool (`True` is not 1 here), inf, nan, fractions,
    lists, dicts and anything else.
    """
    if value is None or value == "":
        if default is None:
            raise refuse(field, "a whole number", value, code=code)
        return default
    number: Optional[int] = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(value) if math.isfinite(value) and value == int(value) else None
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            number = None
    if number is None:
        raise refuse(field, "a whole number", value, code=code)
    if (lo is not None and number < lo) or number > hi:
        bound = f"between {lo} and {hi}" if lo is not None else f"at most {hi}"
        raise refuse(field, f"a whole number {bound}", value, code=code)
    return number


def str_value(field: str, value: Any, *, required: bool = False, default: Optional[str] = None) -> Optional[str]:
    if value is None:
        if required:
            raise refuse(field, "a string", value)
        return default
    if not isinstance(value, str):
        raise refuse(field, "a string", value)
    return value


def str_list_value(field: str, value: Any, *, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise refuse(field, "a list of strings", value)
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise refuse(field, "a list of strings", value)
    return list(value)


def dict_value(field: str, value: Any) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise refuse(field, "an object", value)
    return value


# Payload-keyed forms, for the HTTP boundary.


def req_str(payload: Mapping, key: str) -> str:
    return str_value(key, payload.get(key), required=True) or ""


def opt_str(payload: Mapping, key: str, default: Optional[str] = None) -> Optional[str]:
    return str_value(key, payload.get(key), default=default)


def opt_str_list(payload: Mapping, key: str) -> list[str]:
    return str_list_value(key, payload.get(key))


def opt_dict(payload: Mapping, key: str) -> Optional[dict]:
    return dict_value(key, payload.get(key))


def opt_int(payload: Mapping, key: str, default: int, *, lo: Optional[int] = None, hi: int = MAX_INT) -> int:
    return int_value(key, payload.get(key), default=default, lo=lo, hi=hi)
