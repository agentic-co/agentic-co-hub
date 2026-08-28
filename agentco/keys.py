"""One uniqueness rule on the ingest path.

WHY THIS IS ONE MODULE AND NOT SIX: a queue that accepts work from several
sources grows one idempotency mechanism per source, each invented when that
source was written — a watermark here, a seen-set there, a "does an open item
already exist" scan somewhere else. They disagree, and the weakest one ends up
on the highest-volume path. Measured on a real store before this existed: one
day where every single costed run was a duplicate.

So there is exactly one derivation, enforced at the single point where an item
is born, under the write lock that already serialises appends. **The lock is
the unique index.** No source can opt out by forgetting to call something, and
no future source has to invent a seventh mechanism.

Three forms, precedence explicit > generated > external:

  ``ext|<source>|<source_id>``      the item MIRRORS an external record, so the
                                    external thing's identity is the item's.
  ``gen|<kind>|<subject>|<period>`` generated work. The period is what makes a
                                    nightly job idempotent per night rather
                                    than forever.
  an explicit key                   supplied by the caller, wins over both.

`source` folds case; the external id does **not**. A message id or ticket key
can be case-significant, and folding it would merge two records that are not
the same record.

**A malformed or partial key is refused at authoring time, before any I/O.**
Control characters are refused rather than stripped, because a repaired key is
a silent duplicate and a stripped one is a silent merge. A `kind` and `subject`
with no `period` raises rather than guessing between "key it forever" and "do
not key it at all".

**An item with no derivable key is unconstrained**, exactly as if this module
did not exist. Two identical hand-created items are two pieces of work, and
nothing here may decide otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


#: Where the key lives on a bead. ``metadata`` is the v1 carrier for what v2
#: models as a first-class indexed column.
NATURAL_KEY_FIELD = "natural_key"

#: Stamped by the backfill on the LATER members of a historical collision, so
#: the duplicates that predate enforcement stay queryable instead of merely
#: being counted once and forgotten.
DUPLICATE_OF_FIELD = "natural_key_duplicate_of"

#: Component separator. Escaped inside components so the key parses back
#: unambiguously — a key is a compound identity, not a display string.
SEPARATOR = "|"
_ESCAPED_SEPARATOR = "%7C"

#: Namespace prefixes for the two derived forms. Explicit keys carry no
#: prefix, which is what keeps a caller-owned key from ever colliding with a
#: derived one by accident.
EXTERNAL_PREFIX = "ext"
GENERATED_PREFIX = "gen"

#: Longest a single component may be before it is folded to a bounded, stable
#: digest form. Free-text subjects (bead titles) are the reason this exists.
MAX_COMPONENT_LEN = 160

#: Control characters are refused rather than stripped. This is the same defect
#: class as the ``--blocked-by 'ac-aaa\nac-bbb'`` incident (see
#: ``beads.TaskReferenceError``): a key with an embedded newline is a key that
#: will never match again, and a silently-stripped one is a key that matches
#: something it should not.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RUN = re.compile(r"\s+")


class NaturalKeyError(ValueError):
    """A natural key could not be derived from what the caller supplied.

    Raised at authoring time, which is the only cheap moment. A key that is
    wrong is worse than no key at all: it either collapses two distinct pieces
    of work into one (silently dropping the second) or fails to match the
    thing it was supposed to match (silently duplicating). Both are invisible
    at read time, so both are refused at write time.
    """


# --------------------------------------------------------------------------- #
# Component normalisation
# --------------------------------------------------------------------------- #

def normalize_component(name: str, value: object, *, fold_case: bool = False) -> str:
    """Normalise ONE component of a compound key.

    ``fold_case`` is deliberately opt-in and defaults to False. External ids
    are case-SIGNIFICANT (a Gmail ``Message-Id``, an ADO revision token), so
    lowercasing them would merge distinct records. Free-text subjects are not,
    so the generated form folds those.
    """
    if value is None:
        raise NaturalKeyError(f"natural key component {name!r} is None — nothing to key on")
    text = value if isinstance(value, str) else str(value)
    if _CONTROL_CHARS.search(text):
        raise NaturalKeyError(
            f"natural key component {name!r} contains a control character "
            f"({text!r}). A key with an embedded newline or NUL never matches "
            f"again — refusing rather than stripping, because a silently "
            f"repaired key is a silent duplicate."
        )
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    if not text:
        raise NaturalKeyError(
            f"natural key component {name!r} is empty after normalisation — "
            f"an empty component would make every keyless bead of this shape "
            f"collide with every other."
        )
    if fold_case:
        text = text.casefold()
    if len(text) > MAX_COMPONENT_LEN:
        # Bounded but still injective in practice: the readable prefix keeps
        # the key greppable, the digest keeps it unique. Deterministic, so the
        # same subject always folds to the same key.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        text = f"{text[: MAX_COMPONENT_LEN - 13]}~{digest}"
    return text.replace(SEPARATOR, _ESCAPED_SEPARATOR)


def normalize_natural_key(value: object) -> str:
    """Normalise a caller-supplied (explicit) key.

    The separator is NOT escaped here: an explicit key is already a whole key,
    and a caller who writes ``ext|gmail|<id>`` means to target exactly that.
    """
    if value is None:
        raise NaturalKeyError("explicit natural key is None")
    text = value if isinstance(value, str) else str(value)
    if _CONTROL_CHARS.search(text):
        raise NaturalKeyError(
            f"explicit natural key contains a control character ({text!r}) — refused"
        )
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    if not text:
        raise NaturalKeyError("explicit natural key is empty after normalisation")
    if len(text) > MAX_COMPONENT_LEN * 4:
        raise NaturalKeyError(
            f"explicit natural key is {len(text)} chars (max "
            f"{MAX_COMPONENT_LEN * 4}) — a key that long is a payload, not an "
            f"identity; hash it yourself so the hashing is yours to explain."
        )
    return text


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #

def external_key(source: object, source_id: object) -> str:
    """``ext|<source>|<source_id>`` — the bead mirrors an external record."""
    return SEPARATOR.join(
        (
            EXTERNAL_PREFIX,
            normalize_component("source", source, fold_case=True),
            normalize_component("source_id", source_id),
        )
    )


def generated_key(kind: object, subject: object, period: object) -> str:
    """``gen|<kind>|<subject>|<period>`` — generated work, per period.

    ``subject`` is case-folded because it is free text (a schedule id, a bead
    title). ``period`` is not: it is usually an ISO instant or a date, where
    case never varies and folding would only hide a malformed value.
    """
    return SEPARATOR.join(
        (
            GENERATED_PREFIX,
            normalize_component("kind", kind, fold_case=True),
            normalize_component("subject", subject, fold_case=True),
            normalize_component("period", period),
        )
    )


def derive_natural_key(
    *,
    explicit: object | None = None,
    source: object | None = None,
    source_id: object | None = None,
    kind: object | None = None,
    subject: object | None = None,
    period: object | None = None,
) -> str | None:
    """The one derivation. Returns ``None`` when nothing is keyable.

    Precedence: explicit > generated > external. ``explicit`` wins on purpose —
    a caller that states a key has more information than this function does,
    and the common case (a ``source``/``source_id`` pair that means something
    other than identity) needs an override that is obvious in the call.

    A PARTIALLY-supplied form is an error, never a silent downgrade. If a
    caller names a ``kind`` and a ``subject`` but forgets the ``period``, the
    honest answers are "key it forever" or "do not key it", and picking either
    one for them produces a bug they cannot see.
    """
    if explicit is not None:
        return normalize_natural_key(explicit)

    generated_parts = {"kind": kind, "subject": subject, "period": period}
    supplied = {k: v for k, v in generated_parts.items() if v is not None}
    if supplied:
        missing = sorted(set(generated_parts) - set(supplied))
        if missing:
            raise NaturalKeyError(
                f"generated natural key needs kind+subject+period; missing "
                f"{', '.join(missing)}. Supply all three, or none — a partial "
                f"key would either dedup work that should recur or fail to "
                f"dedup work that should not."
            )
        return generated_key(kind, subject, period)

    if source_id is not None:
        if source is None:
            raise NaturalKeyError(
                "source_id was given without source — an external id with no "
                "namespace collides across systems (an ADO id 4211 and a "
                "Transkriptor order 4211 are not the same work)."
            )
        return external_key(source, source_id)

    return None


def natural_key_of(task_or_dict: object) -> str | None:
    """Read the stored key off a ``Task`` or a raw decoded JSONL row."""
    # A first-class column wins over metadata. Newer schemas store the key as
    # a real field; older rows carry it in metadata because the store had no
    # column to add it to. Both must read, or a backfilled store and a fresh
    # one would disagree about which items are duplicates.
    direct = (
        task_or_dict.get(NATURAL_KEY_FIELD)
        if isinstance(task_or_dict, dict)
        else getattr(task_or_dict, NATURAL_KEY_FIELD, None)
    )
    if isinstance(direct, str) and direct:
        return direct

    metadata = (
        task_or_dict.get("metadata")
        if isinstance(task_or_dict, dict)
        else getattr(task_or_dict, "metadata", None)
    )
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(NATURAL_KEY_FIELD)
    return value if isinstance(value, str) and value else None


def derive_for_row(row: dict) -> str | None:
    """Best-effort key for an ALREADY-STORED row (used by the backfill).

    Only the external form is derivable after the fact: ``source``/``source_id``
    are stored on every bead, whereas a generated bead's period was never
    written down as such. Anything not derivable is reported, not guessed.
    """
    source = row.get("source")
    source_id = row.get("source_id")
    if not source or not source_id:
        return None
    try:
        return external_key(source, source_id)
    except NaturalKeyError:
        return None


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #
