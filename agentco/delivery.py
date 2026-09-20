"""Getting a digest to where people will read it — without knowing where that is.

The change feed is the record; delivery is one subscriber reading it. This
module exists so that "post the digest to chat" does not drag a particular
chat vendor's payload format into the core, the same way `snapshots` does not
know what a document store is.

**The built-in sender posts plain JSON** — `{"text": ..., "generatedAt": ...}` —
to a configured URL. That works with any endpoint somebody controls, and it is
the honest default: a coordination layer has no business knowing what an
Adaptive Card is.

**A second built-in, `--via hub`, files this digest with the hub above this
one** (ADR 0005 — federation is optional, opt in by setting three env vars;
see `post_to_hub` below). It is a built-in rather than a connector because it
speaks this project's own signed-request protocol, not a third-party vendor's
— the same reason `webhook` is a built-in and a chat integration is not.

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


# Same three variables `mcp_server.py` reads to connect this process AS a
# worker to a registry — not imported from there (that module pulls in the
# optional `mcp` extra, and webhook delivery must not need it installed) but
# declared here as the same three strings on purpose: a deployment that
# already points a harness at a hub via these variables gets `--via hub`
# delivery for free, no separate configuration surface to learn.
HUB_URL_ENV_VAR = "AGENTCO_REGISTRY_URL"
HUB_ACTOR_ENV_VAR = "AGENTCO_ACTOR"
HUB_SECRET_ENV_VAR = "AGENTCO_SECRET"


def post_to_hub(text: str, digest: dict) -> None:
    """The federation sender (ADR 0005): file this digest with the hub above.

    Reuses `Registry.digest` — the same signed-request client a leaf agent
    uses to talk to any hub — pointed one level up. A hub that never set
    `AGENTCO_REGISTRY_URL` (i.e. never opted into federation) raises
    `DeliveryNotConfigured` here exactly as `post_json` does when no webhook
    is set: absent means absent, never a guessed destination.
    """
    url = os.environ.get(HUB_URL_ENV_VAR, "").strip()
    actor = os.environ.get(HUB_ACTOR_ENV_VAR, "").strip()
    secret = os.environ.get(HUB_SECRET_ENV_VAR, "").strip()
    missing = [
        name
        for name, value in (
            (HUB_URL_ENV_VAR, url),
            (HUB_ACTOR_ENV_VAR, actor),
            (HUB_SECRET_ENV_VAR, secret),
        )
        if not value
    ]
    if missing:
        raise DeliveryNotConfigured(
            f"{', '.join(missing)} not set — this hub has not opted into "
            f"federation. Set all three to file digests with the hub above "
            f"it, or run without --post to keep this digest local."
        )

    # Imported here, not at module scope: `publish.py` is meant to be
    # copy-pasted standalone by a colleague with nothing installed, and this
    # module importing it at load time would make an ordinary `--via webhook`
    # deployment depend on a file it never uses.
    from agentco.publish import RegistryError
    from agentco.publish import Registry as _Registry

    registry = _Registry(actor, secret, base_url=url)
    try:
        registry.digest(text, generated_at=digest.get("generatedAt"))
    except RegistryError as exc:
        raise DeliveryFailed(exc.status, str(exc)) from exc


SENDERS: dict[str, Sender] = {"webhook": post_json, "hub": post_to_hub}


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
