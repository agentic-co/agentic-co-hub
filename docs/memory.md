# Shared memory

The design is [ADR 0006](decisions/0006-hub-memory-design.md). This page states what is
**built**, slice by slice, and what is not. Everything not listed as built is not built.

## Built: the item store (M1)

A memory item is a short authored record held next to the ASOPs (`agentco/memory.py`):

| Field | Meaning |
|---|---|
| `item_id` | assigned by the hub (`mem-…`) |
| `label` `{owner, level}` | the owner label. **Fixed for the item's life**: content for another label is a new item, never a relabel |
| `layer` | `personal`, `shared` or `public`. `session` and `episodic` are refused: they stay in the harness. The `public` layer holds exactly the items whose owner is `public` |
| `authority` | the one hub that owns the item. An item written here is authoritative here; an item applied from another hub is a **read-only replica** that only moves forward by a newer version from its authority |
| `version` | +1 per authoritative write |
| `kind`, `title`, `body`, `tags` | the content (`kind`: `lesson`, `constraint`, `fact`, `note`) |
| `applies_to` | optional `{asop_id, step?}`: the procedure the lesson belongs in (ADR 0006 §6) |
| `provenance` | **required** `{kind, ref}`: where it came from, as a pointer |
| `pinned`, `review_by` | the class exempt from decay and reinforcement, with a calendar review date |
| `status` | `active` or `retired`. Nothing is deleted |

Every write appends the whole record to an audit (`memory_versions`, or `<store>.audit` for JSONL).

Backends: JSONL at `AGENTCO_MEMORY_STORE` (default `memory.jsonl`) when `AGENTCO_DB` is unset;
the `memory_items` / `memory_versions` tables (migration 0013) on SQLite or Postgres when it is set.
This hub's id is `AGENTCO_HUB_ID` (default `local`).

### Routes

| Route | Who |
|---|---|
| `POST /memory` `{label, layer, title, body, provenance, kind?, tags?, applies_to?}` | any actor cleared for the label |
| `GET /memory?layer=&kind=&retired=1` | returns only what the caller is cleared for |
| `GET /memory/{id}`, `GET /memory/{id}/history` | same; an item you are not cleared for answers exactly like one that does not exist (`memory_unknown`, 404) |
| `POST /memory/{id}/revise` `{fields…, expected_version?}` | cleared actor; authoritative items only (`replica_read_only` otherwise) |
| `POST /memory/{id}/pin` `{pinned?, review_by}` | declared human only |
| `POST /memory/{id}/retire` | declared human only |

### Clearance (`agentco/clearance.py`)

A clearance is a level per data owner. An item opens when its owner is granted and its level is at
or below the grant; levels compare only within one owner. Two apply and both must say yes:

- **the hub's**: `AGENTCO_HUB_CLEARANCE=acme:confidential,globex:internal` (or JSON);
- **the actor's**: the key table's object form, `{"kofi": {"secret": "…", "clearance": {"acme": "internal"}}}`.

The effective clearance is their per-owner minimum. **Unstated is not cleared**: a hub or an actor
with no clearance reads only `public`. Levels are `AGENTCO_MEMORY_LEVELS`, low to high, default
`all,internal,confidential,private` — a choice, not a finding; the ADR gives no order.

## Built: the wall on every read path (M2)

Clearance is applied **in code, before** anything ranks, searches, filters or counts. Every path
below is tried by an under-cleared actor in `tests/test_memory_wall.py`.

| Read path | How it is walled |
|---|---|
| `GET /memory`, `GET /memory/{id}`, `/history` | the store's read methods take the effective clearance; counts exclude what it does not open |
| `GET /memory/search?q=` | the candidate set is the cleared list; match, order and count never see a walled item |
| `/revise`, `/pin`, `/retire` on an item you cannot see | the same `memory_unknown` 404, same body, as an id that does not exist |
| `GET /events` (HTTP) and the MCP `events` tool | `MemoryChanged` events carry the label, never content; `events.read` drops any the reader's clearance does not open, from the page and the count |
| internal feed readers: write-back, conformance, anything calling `events.read` with no viewer | no viewer → no memory events at all |
| the divergence digest | built from snapshots; it never reads memory |
| the pulse | its published event count excludes memory events |
| hub level | the hub's clearance narrows every actor's: lowering it hides items already stored |

`GET /memory/clearance` returns the caller's effective clearance — what a harness partitions by.

**Connector requirement (ADR 0006 §2).** An agent cleared for two owners can carry one owner's
content into the other's work through its own memory, and the hub cannot see that. A harness
cleared for more than one owner must run **one partition and one session per owner**, and open
each session only with the items that owner's clearance returns. This is the harness's to enforce.

### Known limits — open, documented, not claimed closed

The ADR walls **content**. These leak metadata, timing or existence and stay open; each one that a
test can observe has a test pinning the current behaviour, so closing it flips a test.

1. **Feed sequence gaps.** A reader does not see a walled event, but the `seq` numbers it sees
   skip over it and `nextCursor` moves past it: it learns that something it cannot read happened.
   (`test_known_limit_a_feed_sequence_gap_…`)
2. **Per-verb call metrics.** `/metrics` reports latency and counts per verb, `memory_create`
   included, to any authenticated reader. (`test_known_limit_metrics_…`)
3. **Metadata to the cleared.** A reader cleared for a label sees who wrote each version
   (`history`), the authority and the version numbers.
4. **Timing.** Response time is not equalised between a walled item and a missing one, and a write
   to a label changes nothing a reader can time today but is not designed against.
5. **Pulse liveness.** The pulse's per-actor "last seen" includes memory writes: that an actor was
   active, not what it wrote.
6. **Agent memory.** Whatever a cleared agent's harness does with an item after it is read.

## Not built yet

The usefulness and decay engine (M3);
the residuals pipeline (M4); MCP tools, CLI, capture and the lesson → ASOP hand-off (M5); the
memory channel between hubs (ADR 0007).
