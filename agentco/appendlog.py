"""Appending whole lines to a JSONL log, even after a torn one.

Every append-only log here (the outbox, the memory audit and credits logs, the
write-back dead letters) is read one line per record, and a reader quarantines
or skips a line it cannot parse. A process that dies inside a `write` can leave
the file ending in half a line. Appending the next record straight after it
glues the two together, and the reader then loses BOTH: one crash cost a good
record (known issues F3, F4, found by tests/test_fault_injection.py).

So an append first looks at the file's last byte and, when it is not a newline,
writes one: the torn fragment stays its own unparseable line, which the reader
already handles, and the new record lands whole after it. One `write` per call,
then `fsync`, as the callers did before.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def append_lines(path: Path | str, lines: Iterable[bytes]) -> None:
    """Append each of `lines` (without its newline) as one line, durably."""
    data = b"".join(line + b"\n" for line in lines)
    if not data:
        return
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() > 0:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                data = b"\n" + data
        handle.write(data)  # "a" mode: every write lands at the end
        handle.flush()
        os.fsync(handle.fileno())
