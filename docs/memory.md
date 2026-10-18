# Shared memory

The design is [ADR 0006](decisions/0006-hub-memory-design.md). This page states what is
**built**, slice by slice, and what is not. Everything not listed as built is not built.

## The switch: `AGENTCO_MEMORY`, off by default

Shared memory is **off** unless an operator sets `AGENTCO_MEMORY=on` (owner decision 2026-10-18):
its slices land on main before the whole of ADR 0006 does, and a hub must not start holding and
serving owner-labeled items because it was upgraded. Any value other than `on`/`off` (also `true`,
`1`, `yes` / `false`, `0`, `no`, unset) refuses to start.

While **off**:

- every `/memory` route answers **`404 memory_disabled`** — one uniform body whatever the route, after
  authentication, with the remediation naming the switch. 404 rather than 503 because the feature does
  not exist on this hub, as opposed to being temporarily unavailable;
- the memory store is **never opened** (no JSONL file, no SQL store object), so nothing can be written,
  read or announced;
- the migration still applies, harmlessly: the memory tables exist and stay empty;
- the pulse reports `memory: disabled` in its plane facts, and MCP `whoami` reports `memory: disabled`.

While **on**: everything below, exactly as built.

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

This slice applies clearance to the memory routes above. The wall across every other read path
(the change feed, digests, MCP, export) is the next slice; until it lands, memory writes emit
**no** change-feed events, so nothing about an item reaches those paths.

## Not built yet

The wall on every read path and its known limits (M2); the usefulness and decay engine (M3);
the residuals pipeline (M4); MCP tools, CLI, capture and the lesson → ASOP hand-off (M5); the
memory channel between hubs (ADR 0007).
