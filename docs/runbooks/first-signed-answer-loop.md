# Runbook: the first signed-answer loop, across two machines

[ADR 0011](../decisions/0011-v1-definition.md) gate 2 is not met by a loop that only ever ran on one
machine. `tests/test_gate2_loop.py` and `tests/network/test_rig_signed_answer.py` prove the mechanism
with separate processes. This runbook is the part a person has to do: run the loop for real, once,
between a **company hub** on machine A and a **person's partition** on machine B, and record that it
worked. Design: [ADR 0008](../decisions/0008-control-loop-personal-and-company-hubs.md). Envelope
contract: [`../envelope.md`](../envelope.md).

**Who does what.** The *operator* runs the company hub on machine A. The *person* is the human who
answers the gate, on machine B. They can be one human wearing two hats, but the two machines must be
different hosts, and nothing may be copied between them except what a step below says is handed over.

**The direction of travel.** The company hub never calls the person. Machine B calls machine A: it
pulls the pending gate, then carries the signed answer. Machine A needs no address, port or
credential for machine B.

Placeholders: `company-hub.example` (machine A's name), `<your-actor>` (the person's name as the
hub declares it), `<state-dir>` (a directory on the machine that uses it). Replace them; never commit
the real values anywhere.

## 0. What you need

- Machine A: this repo's package with the `server` and `envelope` extras (`uv sync --extra server --extra envelope`),
  and a TLS terminator in front of the hub (a reverse proxy; the hub speaks plain HTTP and `serve`
  binds loopback by default). Machine B must reach `https://company-hub.example`.
- Machine B: the same package with the `envelope` extra. No database, no key table, no company data.
- Two channels between the operator and the person: one for files (the public key, the transport secret)
  and a **second** one to read out two short strings (the key fingerprint and the hub id). A phone call
  or a message in another application will do; the point is that a substitution on the first channel
  shows on the second.

## 1. Machine A: configure and start the company hub

Pick a name for the hub that is not `local` (envelopes cannot be enabled while the name is the shared default).

```sh
export AGENTCO_HUB_ID=acme                        # a label for people; the signed id is generated
export AGENTCO_DB=/var/lib/agentco/hub.sqlite3    # registry, work store and SOPs in one file
export AGENTCO_REGISTRY_KEYS=/etc/agentco/keys.json   # mode 600, never committed
export AGENTCO_REGISTRY_OPERATOR=<operator>       # the operator's own actor
export ASOP_HUMANS=<your-actor>                   # who may answer human gates: declared, or nobody can
export ASOP_VERIFIERS=                            # no agent verifiers: only declared humans answer
# optional: AGENTCO_ENVELOPE_FRESH_SECONDS (default 86400) and AGENTCO_ENVELOPE_GRACE_SECONDS (default half)
```

`ASOP_HUMANS` and `ASOP_VERIFIERS` fail closed: with neither set, no route can answer a human gate.
Set the same variables in every shell that runs an `agentco` command against this hub (the CLI reads
the same database and the same declarations).

Mint the credentials the loop needs and merge them into `keys.json`. `agentco keygen` prints an entry
and never writes the file:

```sh
agentco keygen <operator>                      # the operator's own HMAC key
agentco keygen worker --label ci               # the actor that executes the gated step
agentco keygen <your-actor> --label laptop     # the person's own HMAC key (used here only to author the SOP)
agentco keygen relay-<your-actor> --owner <your-actor> --label partition
```

For the last one, edit its entry in `keys.json` to add `"class": "transport"`:

```json
"relay-<your-actor>": {"secret": "<printed secret>", "owner": "<your-actor>", "class": "transport", "label": "partition"}
```

The transport credential can reach only: the hub id, its party's pending gates, the item each one
verifies, the answer route and its own receipts. It cannot read the queue, file work or write a verdict
unsigned. Check the table loads: `agentco keycheck`. Start the hub behind TLS:

```sh
agentco serve --host 127.0.0.1 --port 8787     # the reverse proxy publishes it as https://company-hub.example
```

Read out the hub's generated id (this is what an envelope names; it is not `AGENTCO_HUB_ID`):

```sh
agentco hub-id --json        # {"hubId": "<32 hex>", "label": "acme"}
```

## 2. Machine B: make the person's key and hand over the public half

One key per person per company hub; never reuse it for another hub.

```sh
agentco partition keygen <state-dir>/acme       # writes signing-key.pem (mode 600) and signing-key.pub.pem
```

It prints `fingerprint` and `keyId`. Send **only** `signing-key.pub.pem` to the operator over channel 1,
and read the `fingerprint` to the operator over channel 2. The private key never leaves machine B.
The operator sends back the transport secret `relay-<your-actor>` (channel 1) and the hub id (channel 2).

## 3. Machine A: enrol the key (operator)

```sh
agentco human-key enrol <your-actor> /path/to/signing-key.pub.pem --fingerprint <fingerprint read on channel 2>
agentco human-key list --human <your-actor>
```

Enrolment refuses a mismatched fingerprint, a person who is not in `ASOP_HUMANS`, and a key already
enrolled for someone else. There is no enrolment verb on the wire. From this moment an unsigned
`POST /work/{id}/attest` on a gate naming `<your-actor>` is refused `envelope_required`.

## 4. Machine B: configure the partition and pin the hub id

```sh
export AGENTCO_PARTITION_LABEL=acme
export AGENTCO_PARTITION_URL=https://company-hub.example
export AGENTCO_PARTITION_ACTOR=relay-<your-actor>
export AGENTCO_PARTITION_SECRET=<the transport secret>      # in the environment, never in argv
export AGENTCO_PARTITION_SIGNER=<your-actor>
export AGENTCO_PARTITION_KEY=<state-dir>/acme/signing-key.pem
export AGENTCO_PARTITION_STATE=<state-dir>/acme/state

agentco partition hub-id                      # what the hub reports now
agentco partition pin <hub id read on channel 2>
```

Pin the id the operator read out, not whatever the hub reports: a mismatch here is the point of the
second channel. From now on `partition answer` refuses with `hub_id_changed` if the hub's id ever
differs, and with `hub_not_pinned` before a pin.

## 5. Machine A: file a run that reaches a human gate

Author a one-step procedure whose gate is `kind: human` and names the person, activate it, run it, let
the worker execute the step, then run the pulse, which files the question (the "vehicle"). Requests are
HMAC-signed, so use the shipped client rather than curl:

```python
from agentco.publish import Registry

person = Registry("<your-actor>", "<the person's HMAC secret>", "http://127.0.0.1:8787")
worker = Registry("worker", "<the worker's secret>", "http://127.0.0.1:8787")

sop = person._call("POST", "/sops", {
    "title": "Confirm a rollback", "task_type": "rollback-check",
    "purpose": "roll a release back and have a person confirm it works",
    "trigger": "a release is rolled back", "roles": {"operator": {"kind": "agent"}},
    "steps": [{"name": "roll back", "role": "operator", "purpose": "roll the release back",
               "definition_of_done": "the previous release serves traffic",
               "gate": {"kind": "human", "check": "the rollback was exercised on staging",
                        "verifier": "<your-actor>", "max_park_seconds": 86400,
                        "on_timeout": "escalate", "escalate_to": "<your-actor>"}}]})["sop"]
person._call("POST", f"/sops/{sop['asop_id']}/activate", {"version": 1})
run = person._call("POST", f"/sops/{sop['asop_id']}/run",
                   {"title": "rollback check", "inputs": {}, "bindings": {"operator": "worker"}})["run"]
pulled = worker._call("POST", "/work/pull", {"ttlSeconds": 600})
worker._call("POST", f"/work/{pulled['item']['id']}/report", {"status": "done", "attempt": pulled["attempt"]})
print(run["runId"], pulled["item"]["id"])     # note both
```

Then, still on machine A (this is the same call the scheduled pulse makes):

```sh
agentco pulse --apply
```

Confirm: `GET /work/<item>` as the operator shows `"status": "awaiting_verify"`, and a vehicle titled
`Verify: ...` with natural key `verify:<item>:0` exists, assigned to `<your-actor>`.

## 6. Machine B: pull the question, answer it signed

```sh
agentco partition pending
```

Expect one row: `{"label": "acme", "ref": "verify:<item>:0", "title": "Verify: ...", "ageSeconds": N, "item": "<item>"}`.
If it is empty, the pulse has not run since the step parked, or the gate names someone else.

```sh
agentco partition answer verify:<item>:0 pass --reason "exercised on staging"
```

Expect exit status 0 and `"status": 200` with `"answer": "applied"` in the body. Note `keyId` and `nonce`.
A `"status": 202` with `"answer": "held"` means the envelope was older than the fresh window (clock skew
on machine B, most likely): fix the clock and answer again. The partition kept the envelope it signed:
`agentco partition sent`.

```sh
agentco partition receipt <keyId> <nonce>      # the hub's receipt: status applied
```

## 7. Machine A: confirm the company hub recorded it

As the operator, read the item and the run (Python client as above, or any signed `GET`):

```python
op = Registry("<operator>", "<the operator's secret>", "http://127.0.0.1:8787")
item = op._call("GET", "/work/<item>")["item"]
```

The loop worked only if **all** of these hold:

1. `item["status"] == "done"`.
2. `item["attestation"]["submitted_by"] == "<your-actor>"` and `["verdict"]["passed"] is True`.
3. `item["attestation"]["envelope"]` is present, with `key_id` equal to the one machine B printed in
   step 2, `via == "relay-<your-actor>"`, and `payload["hub"]` equal to the id read out in step 1. An
   attestation with no `envelope` reads as legacy: it was not signed.
4. `agentco pulse --apply` retires the vehicle, and `agentco partition pending` on machine B is empty.
5. `GET /runs/<runId>` shows every step `done`.
6. Machine A made no outbound call to machine B (check its firewall or proxy log: nothing leaves A towards B).

## 8. Check the refusals on the real network

Run these from machine B against a fresh gate (repeat step 5) and confirm each is refused with the
code shown and leaves the item `awaiting_verify`. The first needs no setup:

- **Second answer.** `agentco partition answer verify:<item>:0 fail --reason x` on an answered gate:
  refused `not_terminal`.
- **Wrong key.** Run `agentco partition keygen <state-dir>/stranger`, point `AGENTCO_PARTITION_KEY` at
  it, answer a fresh gate: refused `key_unknown`.
- **Revoked key.** On A: `agentco human-key revoke <keyId> --reason test`; answer again: `key_revoked`.
  (Enrol a new key, step 2 and 3, before relying on this person again.)

The replay, wrong-hub, tampered-body, undeclared-human and wrong-attempt cases are covered by the
automated tests and need no second machine.

## 9. Record the result

Gate 2 passes when a person other than the code's author has done steps 1 to 7 across two hosts and
written down the following. Keep it in the project's tracker with the gate; do not paste secrets.

```
Gate 2 loop record
date (UTC):
machine A (company hub): OS / host class, agentco version (git commit)
machine B (partition):   OS / host class, agentco version (git commit)
network between them:    e.g. "different sites, A behind TLS proxy, B outbound-only"
hub id (first 8 hex):
key id:                  fingerprint compared on a second channel: yes / no
run id / item id:
envelope nonce / receipt status:
steps 1-7: pass / fail (with the first failing step and its output)
step 8 refusals tried:   (list, with codes seen)
anything surprising:
```

If any of steps 1 to 7 needed a change to the code or to this runbook to work, say what: that is the
finding gate 2 exists to produce, and the gate stays open until the fix is merged and the loop is run again.
