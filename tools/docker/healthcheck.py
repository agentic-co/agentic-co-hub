"""Container healthcheck: 401 is the healthy answer, and 200 is a failure.

Every endpoint on this service authenticates, so an unauthenticated request
must be refused. That makes a `401` the only response that proves two things at
once — the app is serving, and the key table loaded CLOSED rather than open.

A `200` here would mean the registry is accepting anonymous requests, which is
`auth.load_keys` failing open. A healthcheck that reported that as healthy
would be worse than no healthcheck, because it would be actively wrong about
the one property most worth knowing. So it exits non-zero on success-looking
responses, deliberately.

Stdlib only, and no `curl` in the image: the interpreter is already there, and
one fewer package in a service image is one fewer thing to patch.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8787/events"
TIMEOUT_S = 4


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT_S) as response:
            print(
                f"UNHEALTHY: unauthenticated GET returned {response.status} — "
                f"the registry is answering without credentials",
                file=sys.stderr,
            )
            return 1
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return 0
        print(f"UNHEALTHY: unauthenticated GET returned {exc.code}, expected 401", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - any transport failure is unhealthy
        print(f"UNHEALTHY: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
