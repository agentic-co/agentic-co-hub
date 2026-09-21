# Measurements

Numbers this project has actually taken, with the conditions attached. Recorded
because "it should scale" and "we measured it" are different claims, and only
one of them survives someone asking how.

Reproduce with `tools/loadtest.py`; it starts its own registry, runs the
population, and stops it.

## Load, 2026-09-21

One registry process. Each actor polls `GET /events?since=` at the stated
cadence and pulls work on about a tenth of its cycles. The first poll of each
actor is jittered across one cadence — without that you measure a thundering
herd rather than a fleet.

| actors | cadence | backend | req/s | p50 | p95 | p99 | max | errors |
|--:|--:|---|--:|--:|--:|--:|--:|--:|
| 130 | 15s | Postgres | 9.5 | 13.5ms | 18.5ms | 23.2ms | 36.2ms | 0 |
| 500 | 15s | Postgres | 37.0 | 11.1ms | 22.5ms | 33.5ms | 51.1ms | 0 |
| 500 | 15s | SQLite + JSONL | 36.4 | 4.1ms | 7.2ms | 10.9ms | 19.7ms | 0 |

**What this settles.** One process, with a ten-connection pool, serves five
hundred connected actors at a fifteen-second cadence with a p99 in the tens of
milliseconds and no errors. Lifting `replicas: 1` is therefore not a capacity
decision at this population, and the atomic-claim work that would allow it
should be justified by availability across a deploy — if it is wanted at all —
rather than by throughput.

**What it does not settle, stated so nobody reads more into the table than is
there.** The stores were nearly empty. `ready()` grows with the live working
set, and the `calls` table grows without bound, so a registry that has been
running for months is a different measurement — take it again then. Nothing here
ran for hours, and nothing here competed with a real work churn.

**And the awkward one: Postgres is SLOWER here, by about 20ms at p99.** That is
the expected shape — a network round trip against a local file, on small reads
over a small store — and it is worth saying plainly because it means the
Postgres move is not bought for speed. It is bought for what SQLite on a
single ReadWriteOnce volume cannot do: more than one process, state that
outlives one pod, and a backend an operator can back up and inspect with
ordinary tools. That is a good trade at twenty milliseconds. It is a bad
sentence if anyone repeats it as "Postgres made it faster".

## Metering growth, same runs

Every authenticated request writes one row to `calls`, refusals included, and
there is no retention.

| actors | cadence | rows/minute | extrapolated |
|--:|--:|--:|---|
| 130 | 15s | 569 | ~0.82M/day |
| 500 | 15s | 2222 | ~3.20M/day |

Stage-1d — the adoption gate, weekly active publishers, time-to-first-event — is
computed from that table, so this is not only a disk question: the signal the
table exists to produce degrades as poll noise fills it. A retention window and
a rolled-up aggregate are the fix, and the poll cadence is the cheapest lever on
the input side.

## Connection parallelism, 2026-09-20

Eight concurrent statements, each a quarter-second server-side sleep. Serialised
that is 2.00s.

| adapter | wall | distinct server connections |
|---|--:|--:|
| one connection behind a process-wide lock | 2.09s | 1 |
| bounded pool | 0.34s | 8 |

The lock, not the single connection, was the ceiling.

## Queue read cost, 2026-09-20

`ready()` on Postgres, against a queue shaped like one that has been running —
most rows history, a few live.

| rows | live | before (full scan) | after (working set) |
|--:|--:|--:|--:|
| 500 | 50 | 7.7ms | 2.9ms |
| 2,000 | 200 | 32.8ms | 5.0ms |
| 20,000 | 400 | ~330ms (extrapolated) | 12.3ms |

Linear in everything the queue has ever held, against flat in what is live.
