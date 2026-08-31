"""The LiteLLM seam — one call shape, several vendors, a cap that binds.

Every model this harness touches goes through here, for three reasons that are
each a failure mode rather than a preference.

**Roles, not model names, at the call site.** The ASOP contract says a judged
gate is evaluated "by a route different from the executor's". If the runner
names models directly, that clause survives only as long as whoever edits the
config remembers it. Here the call site asks for a *role* — `executor` or
`judge` — and `Fleet.load()` refuses at construction if the two resolve to the
same model. A contract clause that can be violated by a typo in an env var is
not enforced, it is documented.

**A cap checked before the call, not after.** A budget compared against spend
after the response has arrived has already spent the money. `Fleet.complete()`
prices the worst case first — real prompt tokens plus the full `max_tokens` it
is about to authorise — and refuses if that would cross the line. The estimate
is deliberately pessimistic: an optimistic pre-flight check is a cap that leaks
on exactly the runs that overrun.

**Unreported cost is `None`, never `0`.** LiteLLM cannot price every model, and
a provider may return no usage block at all. Recording zero would make an
unpriced arm look free and would silently understate the total that the cap is
computed from — so an unpriced call is recorded as unknown, counted in a
separate column, and `Fleet.spend` reports both numbers. This is the same rule
`metrics.py` applies to unreported usage and `outcomes_by_version` applies to
`successRate`: an unmeasured value must never read as a measured zero.

Pointing at a LiteLLM **proxy** instead of calling vendors directly is one
variable — `AGENTCO_EVAL_API_BASE`. The call shape is identical either way,
which is the reason to route through LiteLLM at all rather than three SDKs.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

EXECUTOR = "executor"
JUDGE = "judge"
ROLES = (EXECUTOR, JUDGE)

# Env vars, all optional except the models themselves.
ENV_MODEL = {EXECUTOR: "AGENTCO_EVAL_EXECUTOR", JUDGE: "AGENTCO_EVAL_JUDGE"}
ENV_API_BASE = "AGENTCO_EVAL_API_BASE"
ENV_API_KEY = "AGENTCO_EVAL_API_KEY"
ENV_BUDGET = "AGENTCO_EVAL_BUDGET_USD"
ENV_PROVIDER = "AGENTCO_EVAL_PROVIDER"

# No default models. A harness that silently picks a model for you produces
# numbers whose meaning depends on a default the reader never saw.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_S = 300
DEFAULT_RETRIES = 2


class LlmError(Exception):
    """Base for every refusal in this module."""


class BudgetExceeded(LlmError):
    """The next call would cross the cap. Raised before the call is made."""


class FleetContractError(LlmError, ValueError):
    """The fleet configuration violates a contract clause the harness enforces."""


@dataclass(frozen=True)
class Completion:
    """One model response, plus what it cost to learn it.

    `cost_usd` is `None` when the model could not be priced — see the module
    docstring. `usage` is `None` when the provider returned none.
    """

    role: str
    model: str
    text: str
    usage: Optional[dict]
    cost_usd: Optional[float]
    latency_s: float
    finish_reason: Optional[str] = None


@dataclass
class Spend:
    """Running total, with the unpriced calls kept visible rather than folded in."""

    usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0

    def as_dict(self) -> dict:
        return {
            "usd": round(self.usd, 6),
            "calls": self.calls,
            "unpricedCalls": self.unpriced_calls,
            # The honest caveat, carried with the number rather than in a
            # footnote: a total computed from priced calls only is a floor.
            "isFloor": self.unpriced_calls > 0,
        }


def _route(model: Optional[str], api_base: Optional[str]) -> Optional[str]:
    """Resolve a bare model name against a proxy, leaving explicit routes alone.

    A LiteLLM **proxy** is addressed by the SDK as `litellm_proxy/<name>`,
    where `<name>` is whatever the proxy's own config calls that deployment —
    not the vendor's model id. Requiring every config to spell that prefix out
    is a papercut that produces a confusing 404 from the vendor rather than the
    proxy when it is forgotten, so a bare name plus an `api_base` is resolved
    here.

    A model string that already names its provider (`anthropic/…`,
    `openai/…`, `litellm_proxy/…`) is passed through untouched: someone who
    wrote the prefix meant it, and silently rewriting it would route a
    deliberate direct-to-vendor call through the proxy without saying so.
    """
    if not model or not api_base:
        return model
    if "/" in model:
        return model
    return f"litellm_proxy/{model}"


def _load_litellm():
    try:
        import litellm  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by the fake provider
        raise LlmError(
            "litellm is not installed. Install the eval extra: "
            "`uv sync --extra eval`, or run with AGENTCO_EVAL_PROVIDER=fake "
            "for the hermetic tests, which make no network calls."
        ) from exc
    # Verbose logging on a several-hundred-call run buries the one line that
    # matters. Failures still raise; they are not being hidden.
    litellm.suppress_debug_info = True
    litellm.drop_params = True
    return litellm


@dataclass
class Fleet:
    """The set of routes this run may use, and the cap across all of them."""

    models: dict
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    budget_usd: Optional[float] = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_s: int = DEFAULT_TIMEOUT_S
    retries: int = DEFAULT_RETRIES
    provider: str = "litellm"
    spend: Spend = field(default_factory=Spend)
    # Swapped for a stub in the harness's own tests. Real runs leave it None.
    _transport: Optional[Callable] = None

    @classmethod
    def load(cls, **overrides) -> "Fleet":
        """Build from the environment, then enforce the no-self-grading clause."""
        provider = overrides.pop("provider", None) or os.environ.get(ENV_PROVIDER) or "litellm"
        models = overrides.pop("models", None) or {
            role: os.environ.get(ENV_MODEL[role]) for role in ROLES
        }
        missing = [role for role in ROLES if not models.get(role)]
        if missing and provider != "fake":
            raise FleetContractError(
                f"no model configured for role(s): {', '.join(missing)}. Set "
                + " and ".join(ENV_MODEL[r] for r in missing)
                + " to a LiteLLM model string (e.g. 'anthropic/claude-sonnet-5'). "
                "There is no default: a harness that picks a model for you "
                "produces numbers whose meaning depends on a default the "
                "reader never saw."
            )
        if provider == "fake":
            models = {role: models.get(role) or f"fake/{role}" for role in ROLES}

        if models[EXECUTOR] == models[JUDGE]:
            raise FleetContractError(
                f"executor and judge are both {models[EXECUTOR]!r}. A judged gate "
                f"evaluated by the executor's own route measures agreement, not "
                f"quality — ASOP Part I forbids it and this harness enforces it. "
                f"Set {ENV_MODEL[JUDGE]} to a different model, ideally a "
                f"different vendor."
            )

        budget = overrides.pop("budget_usd", None)
        if budget is None:
            raw = os.environ.get(ENV_BUDGET)
            budget = float(raw) if raw else None

        api_base = overrides.pop("api_base", None) or os.environ.get(ENV_API_BASE)
        models = {role: _route(name, api_base) for role, name in models.items()}

        return cls(
            models=models,
            api_base=api_base,
            api_key=overrides.pop("api_key", None) or os.environ.get(ENV_API_KEY),
            budget_usd=budget,
            provider=provider,
            **overrides,
        )

    # -- pricing ---------------------------------------------------------

    def _estimate_usd(self, model: str, messages: list) -> Optional[float]:
        """Worst case for the call about to be made, or None if unpriceable."""
        if self.provider == "fake":
            return 0.0
        litellm = _load_litellm()
        try:
            prompt_tokens = litellm.token_counter(model=model, messages=messages)
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=self.max_tokens,
            )
            return float(prompt_cost + completion_cost)
        except Exception:
            # An unpriceable model is not a reason to stop; it is a reason to
            # say so. The cap still binds on everything that CAN be priced.
            return None

    def _check_budget(self, model: str, messages: list) -> None:
        if self.budget_usd is None:
            return
        estimate = self._estimate_usd(model, messages)
        projected = self.spend.usd + (estimate or 0.0)
        if projected > self.budget_usd:
            priced = (
                f"this call is estimated at ${estimate:.4f}"
                if estimate is not None
                else "this call cannot be priced, so it is counted as free "
                "against the cap and the total below is a floor"
            )
            raise BudgetExceeded(
                f"stopping before the next {model} call: spent "
                f"${self.spend.usd:.4f} over {self.spend.calls} call(s), "
                f"{priced}, cap is ${self.budget_usd:.2f}. Raise "
                f"{ENV_BUDGET} or resume later — the ledger is append-only, "
                f"so a resumed run re-spends nothing already recorded."
            )

    # -- the one call ----------------------------------------------------

    def complete(self, role: str, messages: list, **kwargs) -> Completion:
        """One completion for one role. Priced, capped, and timed."""
        if role not in ROLES:
            raise FleetContractError(
                f"unknown role {role!r}; legal roles are {', '.join(ROLES)}"
            )
        model = self.models[role]
        self._check_budget(model, messages)

        started = time.monotonic()
        if self.provider == "fake":
            text, usage, cost, finish = self._fake(role, messages)
        else:
            text, usage, cost, finish = self._live(model, messages, **kwargs)
        latency = time.monotonic() - started

        self.spend.calls += 1
        if cost is None:
            self.spend.unpriced_calls += 1
        else:
            self.spend.usd += cost

        return Completion(
            role=role,
            model=model,
            text=text,
            usage=usage,
            cost_usd=cost,
            latency_s=round(latency, 3),
            finish_reason=finish,
        )

    def _live(self, model: str, messages: list, **kwargs):
        litellm = _load_litellm()
        response = litellm.completion(
            model=model,
            messages=messages,
            api_base=self.api_base,
            api_key=self.api_key,
            max_tokens=kwargs.pop("max_tokens", self.max_tokens),
            temperature=kwargs.pop("temperature", 0.0),
            timeout=self.timeout_s,
            num_retries=self.retries,
            **kwargs,
        )
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        usage = None
        if getattr(response, "usage", None) is not None:
            usage = (
                response.usage.model_dump()
                if hasattr(response.usage, "model_dump")
                else dict(response.usage)
            )
        try:
            cost = float(litellm.completion_cost(completion_response=response))
        except Exception:
            cost = None
        return text, usage, cost, getattr(choice, "finish_reason", None)

    def _fake(self, role: str, messages: list):
        """Deterministic stand-in so the harness's own suite costs nothing.

        The stub is injectable rather than hardcoded because the interesting
        harness tests are about how it handles what a model returns — an empty
        answer, a refusal to grade, a truncated response — and those need to be
        chosen per test, not guessed here.
        """
        if self._transport is not None:
            text = self._transport(role, messages)
        else:
            text = json.dumps({"role": role, "echo": messages[-1]["content"][:80]})
        return text, {"prompt_tokens": 0, "completion_tokens": 0}, 0.0, "stop"
