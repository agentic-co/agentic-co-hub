#!/usr/bin/env python3
"""Read every procedure from a registry, read it back through a consumer, diff the fields.

The check that did not exist, and the absence of which cost five bugs in one day (2026-09-07).
All five were the same shape: a consumer reading a contract that MOVED — a field renamed, a field
relocated into a step, a field deleted, a flat model replaced by a sequence — and returning **null
rather than failing**.

Why unit tests could not catch any of them: a test written by the same hand that wrote the reader
replays the reader's own assumption. `AgentcoWorkQueueTests` ASSERTED `sop_id` and passed happily
for as long as the bug existed. A test can only disagree with the code if something outside both
supplies the truth — here, the live registry.

Why validating on the way OUT is not enough either: authoring checked every payload against
`validate_asop` and all of them passed, because an outbound check asks "will the store accept
this", never "does the consumer get it back". A silently dropped field is invisible to an
outbound check by construction. Both directions or neither.

    AGENTCO_URL=… AGENTCO_ACTOR=… AGENTCO_SECRET=… \
        python3 tools/roundtrip_check.py --consumer http://localhost:5290/sops

Exit 0 if every populated field on the wire arrives at the consumer, 1 otherwise, so it can be a
gate. The consumer is expected to expose `<base>` (a list) and `<base>/{id}` (one procedure), and
to name fields in camelCase — the ordinary shape of a .NET or JS reader. `--map` extends the field
mapping for a consumer that spells them differently.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentco.publish import Registry  # noqa: E402

#: wire (snake_case, as the registry sends it) → consumer (camelCase, as a reader usually spells it).
#: Every one of these is a field a stale reader has actually dropped or could drop; `gate` and
#: `after` are the two that matter most, because a gate is how the work will be JUDGED and `after`
#: is the only remaining way to express step ordering since `next_sop` was removed.
STEP_FIELDS = {
    "name": "name", "step": "step", "role": "role", "purpose": "purpose",
    "entry_check": "entryCheck", "definition_of_done": "definitionOfDone",
    "validation": "validation", "write_back": "writeBack",
    "common_mistakes": "commonMistakes",
    "gate": "gate", "after": "after", "inputs": "inputs", "uses": "uses", "tags": "tags",
}


def fetch(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--consumer", required=True,
                        help="base URL of the consumer's procedure read surface")
    parser.add_argument("--map", action="append", default=[], metavar="WIRE=CONSUMER",
                        help="extra field mapping, repeatable")
    args = parser.parse_args()

    fields = dict(STEP_FIELDS)
    for pair in args.map:
        wire, _, consumer = pair.partition("=")
        fields[wire] = consumer

    missing = [v for v in ("AGENTCO_URL", "AGENTCO_ACTOR", "AGENTCO_SECRET") if not os.environ.get(v)]
    if missing:
        sys.exit(f"FAIL  missing env: {', '.join(missing)}")
    reg = Registry(os.environ["AGENTCO_ACTOR"], os.environ["AGENTCO_SECRET"], os.environ["AGENTCO_URL"])

    base = args.consumer.rstrip("/")
    drops: list[tuple[str, int, str]] = []
    shortfalls: list[str] = []
    procedures = reg.sop_list().get("sops", [])

    for entry in procedures:
        sop_id = entry["asop_id"]
        source = reg.sop_get(sop_id)["sop"]
        try:
            seen = fetch(f"{base}/{sop_id}")
        except Exception as exc:  # noqa: BLE001 — any transport failure is a failed round trip
            shortfalls.append(f"{sop_id}: consumer could not return it — {type(exc).__name__}: {exc}")
            continue

        src_steps = source.get("steps") or []
        dst_steps = seen.get("steps") or []
        if len(dst_steps) != len(src_steps):
            shortfalls.append(f"{sop_id}: {len(src_steps)} step(s) on the wire, {len(dst_steps)} at the consumer")

        for index, step in enumerate(src_steps):
            got = dst_steps[index] if index < len(dst_steps) else {}
            for wire, consumer_name in fields.items():
                # Only POPULATED fields are checked. A field absent on the wire proves nothing
                # about the reader, and asserting on it would fail every procedure that simply
                # does not use it.
                if step.get(wire) and not got.get(consumer_name):
                    drops.append((sop_id, index + 1, wire))

    print(f"      {len(procedures)} procedure(s) round-tripped through {base}")
    for line in shortfalls:
        print(f"      {line}", file=sys.stderr)
    if drops or shortfalls:
        for sop_id, step_no, field in drops:
            print(f"      {sop_id} step{step_no}: '{field}' is on the wire and does not arrive", file=sys.stderr)
        print(f"FAIL  {len(drops)} dropped field(s), {len(shortfalls)} unreadable procedure(s)", file=sys.stderr)
        return 1
    print("PASS  every populated field on the wire arrives at the consumer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
