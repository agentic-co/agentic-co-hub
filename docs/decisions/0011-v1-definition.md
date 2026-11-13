# 0011 — What version 1 is: five gates, and the six decisions taken with them

**Date:** 2026-11-09 · **Status:** ACCEPTED (mabidoli, 2026-11-09); gates 1 and 3 passed, 2, 4 and 5 open · **Relates to:**
[`0004-mcp-surface-under-asop-v3.md`](0004-mcp-surface-under-asop-v3.md),
[`0006-hub-memory-design.md`](0006-hub-memory-design.md),
[`0008-control-loop-personal-and-company-hubs.md`](0008-control-loop-personal-and-company-hubs.md),
[`../known-issues.md`](../known-issues.md)

> A decision with no revisit condition is doctrine. Every record here carries one.

## The problem

The package is `0.1.0` and no release has ever been tagged. The roadmap says what is built and
what is next, but nothing in the repo says when the hub may be called version 1. Without that,
"v1" means whatever the last conversation meant by it, and the work that would break a client
has no deadline to land before.

## The decision: v1 is a procedure with five gates

The hub's own idea, applied to itself. Version 1.0.0 is tagged when all five have passed, in
this order of dependency, not necessarily of work.

| Gate | Passes when |
|---|---|
| **1 · correct** | No open defect that loses work, reports wrong, or crosses parties. Every finding in `known-issues.md` is fixed or explicitly accepted there, and every accepted residue has a test. |
| **2 · the loop closes** | ADR 0008's own milestone: one end-to-end control loop with a real signer, pull-only. A person answers a gate from their personal hub with a signed envelope, and the company hub records it. |
| **3 · contract settled** | Every decision that changes the wire is taken and built. A 1.0 is a promise about the wire, so a change that breaks an existing client lands before it. |
| **4 · docs match code** | The roadmap status table, the ADR banners and the README say what is built, and nothing else. |
| **5 · released** | Tagged `1.0.0` with a changelog, upgrade notes from `0.1.0` covering every migration, a published container image, and the supported `asop-spec` range stated. |

**Not a gate:** the adoption gate ("two people other than the author publishing weekly, for
four consecutive weeks"). It is the project's falsification test and it runs after release.
Version 1 is what is put in front of those two people.

## The six decisions taken with it

These were the open decisions under gate 3. All six were accepted on 2026-11-09.

1. **MCP surface.** ADR 0004 option C: add `sop_run` and `adjudicate`, ceiling fourteen, schema
   byte budget 14,000. This also makes room for the idempotency key and a machine-readable
   refusal code on MCP.
2. **Finding 4, the verifier registry.** Fails closed in v1: an undeclared registry
   authenticates nobody at `attest`, as ASOP §5.3 and §9 say. A major version is the cheapest
   time to change a default. Filing a clock-only gate with no registry stays supported.
3. **6a, unattested holder conflicts.** The `holderAttested` flag travels with the conflict
   record. Additive; nobody's existing behaviour changes.
4. **6b, signed query.** Required by default at 1.0. `ASOP_REQUIRE_SIGNED_QUERY` stays for one
   release as the opt-out, then goes.
5. **Memory.** Ships in v1 off by default and labelled experimental (M1–M3 and team spaces).
   Version 1 does not wait for M4 or M5, and the 1.0 wire promise does not cover `/memory`.
6. **Signed instructions** (`asop.instruction/1`, ADR 0008 §4) are outside gate 2 and come
   after v1, unless building the first loop shows it cannot close without them.

Kept open on purpose and out of v1: the identity mechanism beyond HMAC (revisit when a second
organisation connects a hub), credit across hubs (revisit when two hubs with different owners
exchange work), and `cannot_evaluate` as its own status (stays on option A).

## Revisit condition

The definition is wrong if a gate passes while the thing it stands for is not true: most
likely gate 1 passing on "explicitly accepted" residues that a first outside user then hits, or
gate 2 passing on a loop that only ever ran between two hubs on one machine. One such report
reopens the gate, and the gate's wording is what gets fixed. Decision 6 is wrong if the first
end-to-end loop needs an instruction to travel signed in order to close at all.
