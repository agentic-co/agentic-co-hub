"""Getting a digest to where people will read it — without knowing where that is.

The change feed is the record; delivery is one subscriber reading it. This
module exists so that "post the digest to chat" does not drag a particular
chat vendor's payload format into the core, the same way `snapshots` does not
know what a document store is.

**The built-in sender posts plain JSON** — `{"text": ..., "generatedAt": ...}` —
to a configured URL. That works with any endpoint somebody controls, and it is
the honest default: a coordination layer has no business knowing what an
Adaptive Card is.

A connector that wants native formatting registers a sender:

    from agentco import delivery

    def send_to_widgetchat(text: str, digest: dict) -> None:
        ...

    delivery.register_sender("widgetchat", send_to_widgetchat)

**Nothing here is called unless the operator asked twice** — the CLI requires
`--deliver --post`. Delivery reaches other people, and a tool that messages
colleagues as a side effect of a default is a tool that gets uninstalled.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable, Optional

WEBHOOK_ENV_VAR = "AGENTCO_DIGEST_WEBHOOK"

Sender = Callable[[str, dict], None]


class DeliveryNotConfigured(RuntimeError):
    """No destination. Raised rather than defaulted — see `webhook_url`."""


class DeliveryFailed(RuntimeError):
    def __init__(self, status: Optional[int], detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"digest delivery failed (HTTP {status or '?'}): {detail}")


def webhook_url() -> Optional[str]:
    """The destination, from the environment. **No default, no fallback.**

    A hardcoded or guessed destination is how a digest ends up in the wrong
    channel, which is a mistake other people see. Absent means absent, and the
    caller raises.
    """
    value = os.environ.get(WEBHOOK_ENV_VAR)
    return value.strip() if value else None


def post_json(text: str, digest: dict, url: Optional[str] = None, timeout: int = 20) -> None:
    """The built-in sender: plain JSON to a configured URL. Raises on any non-2xx.

    Loud on failure rather than best-effort. A digest that silently failed to
    send is indistinguishable from a digest with nothing to report, and those
    two must never look alike — the whole feature is about not mistaking
    silence for good news.
    """
    target = url or webhook_url()
    if not target:
        raise DeliveryNotConfigured(
            f"{WEBHOOK_ENV_VAR} is not set — refusing to post. Set it to a URL you "
            f"control, or run without --post to print the digest instead."
        )
    body = json.dumps({"text": text, "generatedAt": digest.get("generatedAt")}).encode()
    request = urllib.request.Request(
        target, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not (200 <= response.status < 300):
                raise DeliveryFailed(
                    response.status, response.read().decode(errors="replace")[:300]
                )
    except urllib.error.HTTPError as exc:
        raise DeliveryFailed(exc.code, exc.read().decode(errors="replace")[:300]) from exc
    except urllib.error.URLError as exc:
        raise DeliveryFailed(None, str(exc)) from exc


SENDERS: dict[str, Sender] = {"webhook": post_json}


def register_sender(name: str, sender: Sender) -> None:
    """Let a connector add a delivery target with native formatting."""
    SENDERS[name] = sender


def send(text: str, digest: dict, via: str = "webhook") -> None:
    known = ", ".join(sorted(SENDERS))
    sender = SENDERS.get(via)
    if sender is None:
        raise DeliveryNotConfigured(
            f"no sender named {via!r} is registered (known: {known}). Install the "
            f"connector that provides it, or use --via webhook."
        )
    sender(text, digest)
