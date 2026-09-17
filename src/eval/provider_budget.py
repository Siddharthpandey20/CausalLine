"""Quota-aware routing across local LLaMA, Gemini and NVIDIA.

WHY THIS EXISTS
---------------
The external APIs are a constrained experimental resource, and neither provider
returns a usable quota header -- verified on 17-09-2026: the Gemini
`models.list` and `generateContent` responses and the NVIDIA
`chat/completions` response all carry **no** rate-limit or quota headers. So
remaining quota cannot be read programmatically and must be tracked locally
against a configured ceiling.

The ceiling is therefore **documented, not verified**, and every number this
module reports says which it is. `BudgetLedger.note` carries that provenance
into the results file so a reader never has to guess whether a limit was
observed or assumed.

THE RULE
--------
A call that would exceed the ceiling is **not made**. It is recorded as
`skipped_quota` and the caller is told, so a degraded run is visible in the
results rather than silently different. Nothing here retries past a limit and
nothing substitutes a different model for a refused one -- `docs/09` and D-058
are explicit that a silent substitution makes the result uninterpretable.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any


def load_env(path: str = ".env") -> None:
    """Read `.env` into the environment. Never logs a value."""
    env = pathlib.Path(path)
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class ProviderLimit:
    """A ceiling, and where it came from."""

    name: str
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    source: str = "documented (not exposed by the API)"


@dataclass
class BudgetLedger:
    """Per-provider accounting. One instance per experiment run."""

    limit: ProviderLimit
    requests: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    failures: int = 0
    skipped_quota: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    _minute_marks: list[float] = field(default_factory=list)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def remaining_today(self) -> int | None:
        if self.limit.rpd is None:
            return None
        return max(0, self.limit.rpd - self.requests)

    def may_call(self) -> tuple[bool, str]:
        with self._lock:
            if self.limit.rpd is not None and self.requests >= self.limit.rpd:
                return False, f"{self.limit.name}: daily cap {self.limit.rpd} reached"
            return True, ""

    def wait_for_slot(self) -> float:
        """Block until an RPM slot is free. Returns seconds slept."""
        if not self.limit.rpm:
            return 0.0
        slept = 0.0
        while True:
            with self._lock:
                now = time.time()
                self._minute_marks = [t for t in self._minute_marks if now - t < 60.0]
                if len(self._minute_marks) < self.limit.rpm:
                    self._minute_marks.append(now)
                    return slept
                oldest = min(self._minute_marks)
            pause = max(0.5, 60.0 - (time.time() - oldest) + 0.5)
            time.sleep(pause)
            slept += pause

    def record(self, prompt: int, output: int, status: str = "ok") -> None:
        with self._lock:
            self.requests += 1
            self.prompt_tokens += prompt
            self.output_tokens += output
            self.statuses[status] = self.statuses.get(status, 0) + 1
            if status != "ok":
                self.failures += 1

    def refuse(self) -> None:
        with self._lock:
            self.skipped_quota += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.limit.name,
            "limit_rpm": self.limit.rpm,
            "limit_rpd": self.limit.rpd,
            "limit_tpm": self.limit.tpm,
            "limit_source": self.limit.source,
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "failures": self.failures,
            "skipped_quota": self.skipped_quota,
            "statuses": dict(self.statuses),
            "remaining_today": self.remaining_today(),
        }


class QuotaExhausted(RuntimeError):
    """The configured ceiling refused this call. Not a transport failure."""


# --- budget accounting around the clients the repository already has --------


@dataclass
class BudgetedClient:
    """Budget accounting wrapped around an existing client. Nothing else.

    WHY THIS IS A WRAPPER AND NOT A CLIENT
    ---------------------------------------
    The first version of this module spoke HTTP to both providers directly.
    That was wrong twice over. `CLAUDE.md` states plainly that **all NVIDIA
    traffic goes through `src/common/nvidia.py` -- do not add an API call
    anywhere else**, and the rule is not bureaucratic: that module carries a
    key pool with rotation and cooldown, a retry policy, a rate limiter,
    `CallStats`, and `redact()`, which strips key material out of any error
    text before it can reach a log. A second hand-rolled client re-implements
    all of that badly and gets none of it right by accident.

    `src/common/llm.GeminiClient` is the same story on the Gemini side: it
    already handles the empty-candidate case, the MAX_TOKENS-with-thinking
    case, and thought-token accounting (D-015).

    So this wraps them. It adds exactly one thing neither has, because neither
    needs it outside this experiment: a ceiling that refuses a call before it
    is made, and a record of what was spent against it.
    """

    inner: Any
    ledger: BudgetLedger
    throttled_s: float = 0.0

    @property
    def model(self) -> str:
        """What actually answered.

        `GeminiClient` keeps the model on its settings rather than on itself,
        and returning "?" here would put a trace header one step away from
        naming the model that produced it -- the D-057 failure.
        """
        own = getattr(self.inner, "model", None)
        if own:
            return str(own)
        return str(getattr(getattr(self.inner, "settings", None), "model", "?"))

    @property
    def spec(self) -> Any:
        return getattr(self.inner, "spec", None)

    @property
    def total_tokens(self) -> int:
        return getattr(self.inner, "total_tokens", 0)

    def fingerprint(self) -> dict[str, Any]:
        own = getattr(self.inner, "fingerprint", None)
        return dict(own()) if callable(own) else {"model": self.model}

    def generate(self, prompt: str, system: str | None = None,
                 json_output: bool = False, temperature: float | None = None):
        from src.common.llm import LLMError

        allowed, why = self.ledger.may_call()
        if not allowed:
            self.ledger.refuse()
            raise QuotaExhausted(why)
        self.throttled_s += self.ledger.wait_for_slot()
        try:
            response = self.inner.generate(prompt, system=system,
                                           json_output=json_output,
                                           temperature=temperature)
        except LLMError as exc:
            # The status, never the text: an error body can echo the request,
            # and `nvidia.redact()` protects its own path but this record goes
            # into a results file that is read and shared.
            self.ledger.record(0, 0, status=_status_of(exc))
            raise
        except Exception as exc:  # noqa: BLE001
            self.ledger.record(0, 0, status=type(exc).__name__)
            raise
        # Thought tokens are billed and are not free (D-015), so they are
        # counted with the output rather than dropped into a bucket nobody
        # reports.
        self.ledger.record(
            int(getattr(response, "prompt_tokens", 0) or 0),
            int(getattr(response, "output_tokens", 0) or 0)
            + int(getattr(response, "thoughts_tokens", 0) or 0),
        )
        self.throttled_s += float(getattr(response, "slept_s", 0.0) or 0.0)
        return response


def _status_of(exc: Exception) -> str:
    """An HTTP status or an exception name -- never the provider's message."""
    import re

    match = re.search(r"\b([45]\d\d)\b", str(exc))
    return f"http_{match.group(1)}" if match else type(exc).__name__


def gemini_client(model: str, ledger: BudgetLedger,
                  thinking_level: str = "low",
                  max_output_tokens: int = 220) -> BudgetedClient:
    """`GeminiClient` pointed at `model`, with a budget around it.

    `thinking_level` is "low", NOT the repository default of "minimal", and
    that is a measured model-version difference rather than a preference.
    `src/common/config.py` pins `minimal` for gemini-3.6-flash, where D-015
    records it producing 0 thought tokens on every prompt tried.
    gemini-3.8-flash REJECTS it outright:

        HTTP 400 INVALID_ARGUMENT -- "Thinking level MINIMAL is not supported
        for this model. Please retry with other thinking level."

    observed 17-09-2026 on the first external call of this experiment's smoke
    test. `low` is the least this model accepts.
    """
    from dataclasses import replace

    from src.common.config import load_settings
    from src.common.llm import GeminiClient

    settings = replace(load_settings(), model=model,
                       thinking_level=thinking_level,
                       max_output_tokens=max_output_tokens)
    return BudgetedClient(GeminiClient(settings), ledger)


def nvidia_client(handle: str, ledger: BudgetLedger,
                  max_output_tokens: int = 220) -> BudgetedClient:
    """`NVIDIAClient` for a registry handle, with a budget around it.

    Goes through `src/common/nvidia.py` as `CLAUDE.md` requires, which is also
    where the registry records that nemotron needs
    `chat_template_kwargs={"thinking": False}` -- without it the model returns
    its chain of thought where the answer should be, which this experiment's
    first smoke run reproduced exactly ("Here's a thinking process:" in place
    of an access code).
    """
    from src.common.nvidia import NVIDIAClient, load_nvidia_settings

    settings = load_nvidia_settings(max_output_tokens=max_output_tokens)
    return BudgetedClient(NVIDIAClient(settings, model=handle), ledger)


# --- the router -------------------------------------------------------------


@dataclass
class RouterSpec:
    """What `run_generated` reads off a client to label the run's model.

    A mixed run has no single execution model, and writing one provider's name
    into that field would be the D-057/D-059 failure: a trace that records a
    model which did not produce most of it. The handle names the mixture and
    `providers` says exactly which models were in it.
    """

    handle: str = "mixed"
    providers: dict[str, str] = field(default_factory=dict)


@dataclass
class RoutedClient:
    """One `generate()` surface over several providers, chosen per agent.

    `GeminiPipeline._call` reads `self.client`, so the pipeline swaps this
    router's `active` agent before each call. Everything the pipeline records --
    usage, prompt, attribution -- flows through unchanged.
    """

    clients: dict[str, Any]
    routing: dict[str, str]
    default: str = "local"
    active_agent: str = ""
    model: str = "mixed"
    total_tokens: int = 0
    throttled_s: float = 0.0
    per_provider_calls: dict[str, int] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)

    @property
    def spec(self) -> RouterSpec:
        return RouterSpec(
            handle="mixed(" + "+".join(sorted(self.clients)) + ")",
            providers={k: getattr(v, "model", "?")
                       for k, v in self.clients.items()},
        )

    def provider_for(self, agent: str) -> str:
        return self.routing.get(agent, self.default)

    def fingerprint(self) -> dict[str, Any]:
        return {
            "model": "mixed",
            "providers": {k: getattr(v, "model", "?") for k, v in self.clients.items()},
            "routing_summary": {
                p: sum(1 for a in self.routing.values() if a == p)
                for p in set(self.routing.values())
            },
        }

    # Provider-side refusals worth one retry before the agent is degraded.
    # A 429 or a 5xx is the endpoint asking us to wait, not an answer; a
    # timeout is the same thing with less information.
    TRANSIENT = ("429", "500", "502", "503", "504", "timeout", "Timeout")

    def generate(self, prompt: str, system: str | None = None,
                 json_output: bool = False, temperature: float | None = None):
        import time as _time

        from src.common.llm import LLMError

        provider = self.provider_for(self.active_agent)
        client = self.clients[provider]
        reason = ""
        try:
            response = client.generate(prompt, system=system,
                                       json_output=json_output,
                                       temperature=temperature)
        except QuotaExhausted:
            reason = "quota"
        except LLMError as exc:
            # THE SELF-IMPOSED CEILING IS NOT THE ONLY CEILING.
            # Neither provider publishes its real limit, so this experiment can
            # be refused by a limit it never knew about. Treating that as fatal
            # would throw away a 20-minute run over one 429 in its 400th call.
            detail = str(exc)
            if any(t in detail for t in self.TRANSIENT):
                _time.sleep(20.0)
                try:
                    response = client.generate(prompt, system=system,
                                               json_output=json_output,
                                               temperature=temperature)
                except Exception:  # noqa: BLE001
                    reason = "transient"
                else:
                    reason = ""
            else:
                reason = "error"
        if reason:
            # NEVER substitute a different model silently (D-058). The agent is
            # recorded as degraded and the local backend answers, so the run is
            # interpretable and the substitution is visible in the results --
            # the reader can see exactly which agents stopped being what the
            # topology says they are, and why.
            self.degraded.append(f"{self.active_agent}:{provider}:{reason}")
            client = self.clients[self.default]
            response = client.generate(prompt, system=system,
                                       json_output=json_output,
                                       temperature=temperature)
            provider = f"{self.default}(fallback)"
        self.per_provider_calls[provider] = self.per_provider_calls.get(provider, 0) + 1
        self.total_tokens += response.total_tokens
        self.throttled_s = sum(getattr(c, "throttled_s", 0.0)
                               for c in self.clients.values())
        return response
