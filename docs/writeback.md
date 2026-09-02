# Write-back — telling the origin its gate is waiting

> Decision: [`decisions/0002-participation-ladder.md`](decisions/0002-participation-ladder.md)
> covers the ladder; this path was chosen by the principal on 2026-09-02, over a
> stated objection, and the objection is recorded below rather than dropped.

A `human` gate parks its work item and, on its own, reaches nobody. The change
feed now carries `WorkParked` and `GateEscalated`, which is the substrate — but
a feed only helps somebody already looking at AgentCo. The people who own human
gates are looking at Azure DevOps, or Jira, or whatever filed the work.

So: one path back. When a gate parks on work that was **mirrored from an
external record**, this can post a notice to that record's origin.

## The boundaries, which are the design

**Off until configured.** No writer registered and no `AGENTCO_WRITEBACK_URL`
set means the pass reports itself as not configured and does nothing. That is
the default state and it is reported rather than raised — a scheduled job that
fails loudly because an optional feature is switched off is a job whose owner
disables the alerting and then misses the failure that mattered.

**A notice, never a mutation.** One shape:

```json
{"kind": "WorkParked", "itemId": "w-3f9c", "source": "ado",
 "sourceId": "acme/91060", "title": "fix the retry path",
 "check": "the release owner signs off on the wording",
 "assignedTo": "dana", "dueAt": "2026-09-09T00:00:00+00:00",
 "waitedSeconds": null, "occurredAt": "2026-09-02T08:00:00+00:00"}
```

There is no path from here to changing a state, closing a ticket or editing a
field, and the test suite asserts the notice's keys exactly. A connector that
wants to do more is writing its own integration rather than extending this one.

**AgentCo does not speak your vendor's API.** The built-in writer POSTs the
notice to a URL you control, the same way `delivery` posts a digest. What turns
it into a comment on a work item is *your* endpoint, holding *your* credential,
applying *your* rules. That is not a technicality — it keeps the blast radius
inside the org that chose it, and it means this module cannot grow an opinion
about what a work item looks like.

A connector that wants to write directly registers a writer:

```python
from agentco import writeback

def to_ado(notice: dict) -> None:
    ...

writeback.register_writer("ado", to_ado)
```

**Only work with an origin.** The origin is read off the natural key, where
`keys.external_key` already puts it when a connector mirrors an external record
(`ext|ado|acme/91060`). An item filed locally has nowhere to write back to and
is skipped — the correct answer, not a delivery failure.

**Idempotency is the feed cursor.** This reads the change feed like any other
subscriber and persists where it got to. The cursor advances only over events
that were actually handled, so a delivery failure is retried on the next run
rather than skipped by a watermark that moved regardless — and a pass that
re-read from the beginning would put the same comment on the same ticket every
five minutes, which is how a channel becomes one people filter.

**A poison event does not stall the pass, or erase the cursor.** A notice
that fails to deliver splits two ways, and not by 4xx-versus-everything-else —
four 4xx statuses are the exception. A **permanent** failure — the writer
raises `WritebackFailed` with a 4xx status other than 401, 403, 408 or 429,
because the ticket was deleted or the URL never existed — is not going to
succeed on the next identical POST, so it is appended to a dead-letter file
instead of retried forever, and the pass continues past it: the events after
it still get their notice. A **transient** failure — 5xx, unreachable, a
401/403/408/429, or anything else the writer raises with no 4xx status —
stops the pass, but the cursor is persisted up to the last event actually
handled before the exception propagates — so the next run retries starting at
the event that failed, not at the beginning. 401 and 403 are the operator's to
fix (rotate the token, grant access) and then they want the same notice
replayed, not permanently dropped next to a ticket that no longer exists; 408
and 429 say the endpoint is asking to be asked again, not that anything about
this notice is wrong. The dead-letter file lives at
`~/.agentco/writeback.deadletter.jsonl` unless `AGENTCO_WRITEBACK_DEADLETTER`
says otherwise, one JSON object per line: `{"notice": {...}, "status": 400,
"detail": "..."}`. If that path itself cannot be written, the failure that
caused the dead-letter is treated as transient instead — cursor persisted, the
run raises naming the unwritable path — rather than silently drop the notice
that triggered it.

## Running it

```
AGENTCO_WRITEBACK_URL=https://your-endpoint.example/agentco  agentco writeback
```

`--dry-run` prints the notices it would send and moves nothing. The cursor lives
at `~/.agentco/writeback.cursor` unless `AGENTCO_WRITEBACK_CURSOR` says
otherwise. Schedule it beside `agentco verifiers --route --sweep`, which is what
produces the events it reads.

## The objection, recorded

"AgentCo never writes to your system of record" appears in the README, the
roadmap and the design principles, and the reason given is that adopting AgentCo
must not be able to damage what you already trust. A notification is a small
write; it is still a write, and an absolute promise with one exception is not
absolute.

The decision was to build it anyway, on the grounds that a channel nobody checks
is not a channel — the alternative considered was an AgentCo-native surface,
which keeps the promise perfectly and is one more place nobody looks.

What the objection bought is the shape above: opt-in, append-only, and executed
by the operator's own code at the far end of an HTTP call. The README no longer
claims the absolute version. It states the exception, names this document, and
says the promise holds without qualification when nothing is configured — which
is the only honest way to keep making it.
