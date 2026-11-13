# 0014 — Deterministic checks are a versioned library beside the spec, not runtime code

**Date:** 2026-11-13 · **Status:** PROPOSED, **cross-repo** (changes what the `asop-spec` package
family publishes; needs agreement in the spec repo before either product adopts it) ·
**Supersedes:** nothing

> A decision with no revisit condition is doctrine. Every record here carries one.

## Context

A deterministic gate names a check. Where that check lives differs by product:

- The spec defines gate kinds and refusal codes but not the checks themselves.
- The harness implements checks inside its runtime (for example `lesson_triage.py`), so a
  procedure that names a check is only portable to runtimes that happen to ship the same code.
- The hub validates gate *shape* but cannot tell whether a named check exists or which revision
  of its rules produced an attestation.

An internal prototype runner solves this with a **check registry**: each check is a name plus
an implementation plus a rules version, shipped in a library both its server and its runner load.
An attestation records the check name and rules version, so two runs of "the same" gate on
different rule revisions are distinguishable.

The harness's own evidence makes this matter: the composite gate refused 31 of 31 correct runs on
its best procedure (~50% false refusal per gate). Fixing a check therefore means shipping a new
rules version and being able to tell which runs used the old one.

## Decision

**(b).**

1. **A check library is published beside `asop-spec`**, versioned on its own. It holds a registry
   (name → rules version, input schema, refusal codes) and a reference implementation.
2. **Runtimes execute checks; the hub validates names.** The harness runs a check from the
   library at the version the procedure pins. The hub refuses an attestation that names a check,
   or a rules version, the registry does not know.
3. **Attestations carry the check name and rules version.** This is an additive change to the
   attestation shape, proposed to the spec the same way 0008 proposed the answer envelope.
4. **Product-specific checks stay possible** under a namespaced name (for example
   `local.<name>`). They are valid only on the runtime that registers them, and the hub labels them
   non-portable.

## Alternatives

- **(a) Keep checks in each runtime.** Portability is then accidental, and a fixed check is
  invisible in evidence.
- **(c) Put checks inside `asop-spec` itself.** That couples the contract's release cadence to
  bug fixes in check rules, which change far more often.

## Revisit condition

Revisit if after two releases fewer than three checks are shared between runtimes (the library
would be ceremony), or if rules-version churn makes old attestations unreadable rather than merely
distinguishable.
