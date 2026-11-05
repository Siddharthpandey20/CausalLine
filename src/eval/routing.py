"""Provider routing with retry, fallback, quota tracking and USD accounting.

WHAT THIS IS FOR
----------------
A campaign that runs for hours across three providers will meet a 429, a 503
and a dead daemon. None of those may destroy the run, and none of them may
quietly corrupt the science: a fallback model is a DIFFERENT model, and an
experiment that hides the switch has stopped being reproducible.

So every invocation produces a `CallRecord` naming the provider it was assigned
to, the provider that actually ran it, whether a fallback happened and why, how
many attempts it took, what each attempt cost, and whether the call succeeded,
failed or was deferred. The record is written whether the call worked or not.

THE RULES, AND WHY EACH ONE IS THERE
-------------------------------------
* **A fallback is never equivalence.** `CallRecord.fallback_occurred` is carried
  into the trace and into the results, so a reader can exclude or annotate the
  invocations that did not run on their assigned model. `src/eval/
  provider_budget.py` already refuses to substitute silently for the same
  reason (D-058); this module keeps that rule and adds the audit trail that
  makes a *declared* substitution usable.
* **Transient and permanent are different.** A 429 or 5xx is retried with
  exponential backoff and jitter, honouring `Retry-After`. A bad key, an
  invalid request or a safety block is not retried at all -- retrying it burns
  quota to learn nothing.
* **A provider-level failure removes the provider; a call-level failure does
  not.** An expired key means every later call to that provider would fail too,
  so the circuit opens. A malformed prompt is about this call, so falling back
  to another model would only get the same refusal from a second provider.
* **Failed attempts still cost.** Any attempt that reached the provider is
  charged, because the provider charged for it.
* **Nothing retries forever.** Attempts are bounded per provider and the
  fallback chain is finite. When it is exhausted the caller is told, so the
  campaign can checkpoint and pause rather than spin.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from src.common.llm import LLMError, LLMResponse, QuotaExhausted
from src.common.retry import Retryable

__all__ = [
    "PRICES", "ProviderLimits", "ProviderState", "CallRecord", "CallLedger",
    "FallbackRouter", "AllProvidersFailed", "classify",
]

# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------
# USD per 1M tokens. CONFIGURABLE and documented, never authoritative: list
# prices move, and a campaign's own estimate must say where its numbers came
# from. Override with `prices=` or the `*_USD_PER_MTOK_IN/OUT` variables.
#
# Local inference is $0.00 by construction -- the GPU is already paid for --
# but its runtime is still recorded, because wall clock is the real local cost.
PRICES: dict[str, tuple[float, float]] = {
    "gemini": (0.075, 0.300),
    "nvidia": (0.000, 0.000),    # account credits in this project; see notes
    "local": (0.000, 0.000),
}


def prices_from_env(base: dict[str, tuple[float, float]] | None = None
                    ) -> dict[str, tuple[float, float]]:
    out = dict(base or PRICES)
    for provider in list(out):
        prefix = provider.upper()
        try:
            cin = os.environ.get(f"{prefix}_USD_PER_MTOK_IN")
            cout = os.environ.get(f"{prefix}_USD_PER_MTOK_OUT")
            if cin is not None or cout is not None:
                out[provider] = (float(cin) if cin else out[provider][0],
                                 float(cout) if cout else out[provider][1])
        except ValueError:
            pass                 # a typo must not stop a campaign
    return out


def usd_for(provider: str, prompt_tokens: int, output_tokens: int,
            prices: dict[str, tuple[float, float]] | None = None) -> float:
    table = prices or PRICES
    rate_in, rate_out = table.get(provider, (0.0, 0.0))
    return (prompt_tokens / 1e6) * rate_in + (output_tokens / 1e6) * rate_out


# --------------------------------------------------------------------------
# error classification
# --------------------------------------------------------------------------

TRANSIENT = "transient"          # retry this provider
OVER_QUOTA = "over_quota"        # provider is done for now; fall back
PROVIDER_DOWN = "provider_down"  # auth, missing key, model gone; fall back
CALL_INVALID = "call_invalid"    # bad request, safety block; do NOT fall back


def classify(exc: BaseException) -> tuple[str, float | None, str]:
    """(category, retry_after, reason). The whole retry/fallback policy reads
    this, so the policy lives in one readable place."""
    if isinstance(exc, Retryable):
        return TRANSIENT, getattr(exc, "retry_after", None), str(exc)[:200]
    if isinstance(exc, QuotaExhausted):
        return OVER_QUOTA, None, str(exc)[:200]
    name = type(exc).__name__
    if name in ("NoUsableKey", "ModelUnavailable", "MissingAPIKey"):
        return PROVIDER_DOWN, None, f"{name}: {str(exc)[:160]}"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return TRANSIENT, None, f"{name}: {str(exc)[:160]}"
    if isinstance(exc, LLMError):
        text = str(exc).lower()
        if any(k in text for k in ("api key", "unauthorized", "permission",
                                   "forbidden", "401", "403")):
            return PROVIDER_DOWN, None, str(exc)[:200]
        return CALL_INVALID, None, str(exc)[:200]
    return CALL_INVALID, None, f"{name}: {str(exc)[:160]}"


# --------------------------------------------------------------------------
# per-provider limits and live state
# --------------------------------------------------------------------------


@dataclass
class ProviderLimits:
    """Ceilings for one provider. READ FROM CONFIGURATION, not hardcoded.

    `source` records whether a number was observed or assumed, because
    `provider_budget` verified on 17-09-2026 that neither hosted provider
    returns a quota header. A limit this module cannot verify is still
    enforced, but it is reported as assumed.
    """

    name: str
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    max_usd: float | None = None
    max_calls: int | None = None
    max_tokens: int | None = None
    attempts: int = 3
    source: str = "configured (not exposed by the provider API)"

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any] | None) -> "ProviderLimits":
        data = data or {}
        return cls(
            name=name,
            rpm=data.get("rpm"), rpd=data.get("rpd"), tpm=data.get("tpm"),
            max_usd=data.get("max_usd"), max_calls=data.get("max_calls"),
            max_tokens=data.get("max_tokens"),
            attempts=int(data.get("attempts", 3)),
            source=data.get("source", cls.source))


@dataclass
class ProviderState:
    """What we currently know about a provider's health and consumption."""

    limits: ProviderLimits
    requests: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    failures: int = 0
    consecutive_failures: int = 0
    refusals: int = 0                       # calls not made, for a limit
    fallbacks_in: int = 0                   # calls this provider absorbed
    fallbacks_out: int = 0                  # calls it could not serve
    open_until: float = 0.0                 # circuit breaker
    cooldown_until: float = 0.0             # Retry-After / backoff
    statuses: dict[str, int] = field(default_factory=dict)
    _minute: list[float] = field(default_factory=list, repr=False)
    _minute_tokens: list[tuple[float, int]] = field(default_factory=list,
                                                    repr=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    # Consecutive failures that open the circuit. Three is enough to tell a
    # dead provider from one unlucky call, and small enough that a campaign
    # stops hammering it quickly.
    breaker_threshold: int = 3
    breaker_seconds: float = 120.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def available(self, now: float | None = None) -> tuple[bool, str]:
        """Can this provider take a call right now?"""
        now = now if now is not None else time.time()
        with self._lock:
            if now < self.open_until:
                return False, (f"{self.limits.name}: circuit open for "
                               f"{self.open_until - now:.0f}s after "
                               f"{self.consecutive_failures} failures")
            if now < self.cooldown_until:
                return False, (f"{self.limits.name}: cooling down for "
                               f"{self.cooldown_until - now:.0f}s")
            lim = self.limits
            if lim.rpd is not None and self.requests >= lim.rpd:
                return False, f"{lim.name}: daily cap {lim.rpd} reached"
            if lim.max_calls is not None and self.requests >= lim.max_calls:
                return False, f"{lim.name}: call budget {lim.max_calls} spent"
            if lim.max_tokens is not None and self.total_tokens >= lim.max_tokens:
                return False, f"{lim.name}: token budget {lim.max_tokens} spent"
            if lim.max_usd is not None and self.usd >= lim.max_usd:
                return False, (f"{lim.name}: spend cap ${lim.max_usd:.2f} "
                               f"reached (${self.usd:.4f})")
            return True, ""

    def wait_for_slot(self, sleep: Callable[[float], None] = time.sleep,
                      now: Callable[[], float] = time.time) -> float:
        """Block until an RPM/TPM slot frees. Returns seconds slept.

        `sleep` and `now` are injectable so the tests can exercise the limiter
        without spending a minute of real time.
        """
        slept = 0.0
        lim = self.limits
        if not lim.rpm and not lim.tpm:
            return 0.0
        while True:
            with self._lock:
                t = now()
                self._minute = [x for x in self._minute if t - x < 60.0]
                self._minute_tokens = [(x, n) for x, n in self._minute_tokens
                                       if t - x < 60.0]
                rpm_ok = (not lim.rpm) or len(self._minute) < lim.rpm
                used = sum(n for _, n in self._minute_tokens)
                tpm_ok = (not lim.tpm) or used < lim.tpm
                if rpm_ok and tpm_ok:
                    self._minute.append(t)
                    return slept
                marks = [x for x, _ in self._minute_tokens] + self._minute
                oldest = min(marks) if marks else t
            pause = max(0.5, 60.0 - (now() - oldest) + 0.5)
            sleep(pause)
            slept += pause

    def record(self, prompt: int, output: int, usd: float, status: str,
               ok: bool) -> None:
        with self._lock:
            self.requests += 1
            self.prompt_tokens += prompt
            self.output_tokens += output
            self.usd += usd
            self.statuses[status] = self.statuses.get(status, 0) + 1
            self._minute_tokens.append((time.time(), prompt + output))
            if ok:
                self.consecutive_failures = 0
            else:
                self.failures += 1
                self.consecutive_failures += 1
                if self.consecutive_failures >= self.breaker_threshold:
                    self.open_until = time.time() + self.breaker_seconds

    def cool_down(self, seconds: float) -> None:
        with self._lock:
            self.cooldown_until = max(self.cooldown_until,
                                      time.time() + max(0.0, seconds))

    def mark_unavailable(self, seconds: float | None = None) -> None:
        with self._lock:
            self.open_until = time.time() + (seconds or self.breaker_seconds)

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        return {
            "provider": self.limits.name,
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "usd": round(self.usd, 6),
            "failures": self.failures,
            "refusals": self.refusals,
            "fallbacks_in": self.fallbacks_in,
            "fallbacks_out": self.fallbacks_out,
            "statuses": dict(self.statuses),
            "circuit_open": now < self.open_until,
            "cooling_down": now < self.cooldown_until,
            "limit_rpm": self.limits.rpm,
            "limit_rpd": self.limits.rpd,
            "limit_tpm": self.limits.tpm,
            "limit_max_usd": self.limits.max_usd,
            "limit_source": self.limits.source,
            "remaining_today": (None if self.limits.rpd is None
                                else max(0, self.limits.rpd - self.requests)),
        }


# --------------------------------------------------------------------------
# per-invocation record
# --------------------------------------------------------------------------


@dataclass
class Attempt:
    provider: str
    model: str
    outcome: str                 # "ok" | category from classify()
    status: str
    latency_s: float
    prompt_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    delay_before_s: float = 0.0
    reason: str = ""


@dataclass
class CallRecord:
    """One logical invocation, with every attempt it took.

    This is the reproducibility record. A result that quotes a token count
    without it cannot say which model produced the output.
    """

    key: str                     # architecture/trial/agent, for resume
    agent: str
    assigned_provider: str
    assigned_model: str = ""
    actual_provider: str = ""
    actual_model: str = ""
    fallback_occurred: bool = False
    fallback_reason: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    outcome: str = "deferred"    # "ok" | "failed" | "deferred"
    error_type: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    latency_s: float = 0.0
    prompt_sha: str = ""

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def retry_delays(self) -> list[float]:
        return [a.delay_before_s for a in self.attempts]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["attempt_count"] = self.attempt_count
        data["retry_delays"] = self.retry_delays
        return data


@dataclass
class CallLedger:
    """Every invocation in the campaign, and the totals that follow from it."""

    records: list[CallRecord] = field(default_factory=list)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def add(self, record: CallRecord) -> None:
        with self._lock:
            self.records.append(record)

    def totals(self) -> dict[str, Any]:
        by_provider: dict[str, dict[str, float]] = {}
        for record in self.records:
            for attempt in record.attempts:
                slot = by_provider.setdefault(
                    attempt.provider,
                    {"attempts": 0, "ok": 0, "prompt_tokens": 0,
                     "output_tokens": 0, "usd": 0.0})
                slot["attempts"] += 1
                slot["ok"] += 1 if attempt.outcome == "ok" else 0
                slot["prompt_tokens"] += attempt.prompt_tokens
                slot["output_tokens"] += attempt.output_tokens
                slot["usd"] += attempt.usd
        return {
            "invocations": len(self.records),
            "ok": sum(1 for r in self.records if r.outcome == "ok"),
            "failed": sum(1 for r in self.records if r.outcome == "failed"),
            "deferred": sum(1 for r in self.records if r.outcome == "deferred"),
            "fallbacks": sum(1 for r in self.records if r.fallback_occurred),
            "total_attempts": sum(r.attempt_count for r in self.records),
            "usd": round(sum(r.usd for r in self.records), 6),
            "by_provider": {k: {**v, "usd": round(v["usd"], 6)}
                            for k, v in sorted(by_provider.items())},
        }


class AllProvidersFailed(RuntimeError):
    """Every provider in the chain refused or failed.

    The campaign catches this, checkpoints, and pauses with instructions. It is
    deliberately not an `LLMError`: it is a campaign-control signal, not a
    model error, and nothing should retry it in an inner loop.
    """

    def __init__(self, message: str, record: CallRecord) -> None:
        super().__init__(message)
        self.record = record


# --------------------------------------------------------------------------
# the router
# --------------------------------------------------------------------------


@dataclass
class FallbackRouter:
    """Routes one invocation to its assigned provider, with fallback.

    `clients` maps a provider name to something with
    `generate(prompt, system=, json_output=, temperature=) -> LLMResponse`.
    `route` maps an agent id to its ASSIGNED provider. `chain` is the fallback
    order tried after the assigned provider is exhausted.

    The pipeline sets `active_agent` before each call, exactly as
    `provider_budget.RoutedClient` does, so no pipeline code changes.
    """

    clients: dict[str, Any]
    route: dict[str, str] = field(default_factory=dict)
    default: str = "local"
    chain: tuple[str, ...] = ("local", "nvidia", "gemini")
    states: dict[str, ProviderState] = field(default_factory=dict)
    ledger: CallLedger = field(default_factory=CallLedger)
    prices: dict[str, tuple[float, float]] = field(
        default_factory=lambda: prices_from_env())
    models: dict[str, str] = field(default_factory=dict)
    active_agent: str = ""
    key_prefix: str = ""
    # injectable for tests
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    rng: Any = field(default_factory=lambda: random.Random(20260906))
    per_provider_calls: dict[str, int] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    total_usd_cap: float | None = None

    def __post_init__(self) -> None:
        for name in self.clients:
            self.states.setdefault(
                name, ProviderState(limits=ProviderLimits(name=name)))

    # --- helpers -----------------------------------------------------------

    def provider_for(self, agent: str) -> str:
        return self.route.get(agent, self.default)

    def state(self, provider: str) -> ProviderState:
        return self.states.setdefault(
            provider, ProviderState(limits=ProviderLimits(name=provider)))

    def model_for(self, provider: str) -> str:
        if provider in self.models:
            return self.models[provider]
        client = self.clients.get(provider)
        for attr in ("model", "model_id"):
            value = getattr(client, attr, None)
            if isinstance(value, str):
                return value
        settings = getattr(client, "settings", None)
        value = getattr(settings, "model", None)
        return value if isinstance(value, str) else provider

    def budget_state(self) -> dict[str, Any]:
        return {name: st.to_dict() for name, st in sorted(self.states.items())}

    def spend(self) -> float:
        return sum(st.usd for st in self.states.values())

    def _order(self, assigned: str) -> list[str]:
        """Assigned provider first, then the chain, each named once."""
        out = [assigned]
        for name in self.chain:
            if name not in out and name in self.clients:
                out.append(name)
        return out

    def _backoff(self, attempt_index: int, retry_after: float | None) -> float:
        """Exponential backoff with jitter, floored by Retry-After.

        Retry-After wins when the provider sent one: it is the only
        authoritative number in the exchange.
        """
        base = min(30.0, 1.5 * (2 ** attempt_index))
        jitter = self.rng.uniform(0.0, base * 0.25)
        wait = base + jitter
        if retry_after is not None:
            wait = max(wait, float(retry_after))
        return round(wait, 3)

    # --- the call ----------------------------------------------------------

    def generate(self, prompt: str, system: str | None = None,
                 json_output: bool = False,
                 temperature: float | None = None) -> LLMResponse:
        import hashlib

        agent = self.active_agent or "?"
        assigned = self.provider_for(agent)
        record = CallRecord(
            key=f"{self.key_prefix}{agent}",
            agent=agent,
            assigned_provider=assigned,
            assigned_model=self.model_for(assigned),
            prompt_sha=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
        )

        if self.total_usd_cap is not None and self.spend() >= self.total_usd_cap:
            record.outcome = "deferred"
            record.error_type = "campaign_spend_cap"
            self.ledger.add(record)
            raise AllProvidersFailed(
                f"campaign spend cap ${self.total_usd_cap:.2f} reached "
                f"(${self.spend():.4f}); nothing was sent", record)

        for position, provider in enumerate(self._order(assigned)):
            client = self.clients.get(provider)
            if client is None:
                continue
            state = self.state(provider)
            ok, why = state.available(self.clock())
            if not ok:
                state.refusals += 1
                record.attempts.append(Attempt(
                    provider=provider, model=self.model_for(provider),
                    outcome="refused", status="not_attempted", latency_s=0.0,
                    reason=why))
                if position == 0:
                    record.fallback_reason = why
                continue

            for attempt_index in range(max(1, state.limits.attempts)):
                delay = 0.0
                if attempt_index:
                    delay = self._backoff(attempt_index - 1, self._last_retry_after)
                    self.sleep(delay)
                slept = state.wait_for_slot(self.sleep, self.clock)
                started = self.clock()
                try:
                    response = client.generate(
                        prompt, system=system, json_output=json_output,
                        temperature=temperature)
                except BaseException as exc:  # noqa: BLE001 -- classified below
                    category, retry_after, reason = classify(exc)
                    self._last_retry_after = retry_after
                    latency = self.clock() - started
                    # A failed attempt that reached the provider is still
                    # charged: the provider charged for it.
                    state.record(0, 0, 0.0, category, ok=False)
                    record.attempts.append(Attempt(
                        provider=provider, model=self.model_for(provider),
                        outcome=category, status=category, latency_s=latency,
                        delay_before_s=delay + slept, reason=reason))
                    record.error_type = category
                    if category == CALL_INVALID:
                        # About this call, not this provider. Another provider
                        # would refuse the same request, so stop here.
                        record.outcome = "failed"
                        self.ledger.add(record)
                        raise
                    if category in (OVER_QUOTA, PROVIDER_DOWN):
                        state.mark_unavailable()
                        if provider not in self.degraded:
                            self.degraded.append(f"{provider}:{category}")
                        break                      # to the next provider
                    if retry_after is not None:
                        state.cool_down(0.0)       # honoured by _backoff
                    continue                       # transient: retry provider
                else:
                    latency = self.clock() - started
                    ptok = getattr(response, "prompt_tokens", 0) or 0
                    otok = getattr(response, "output_tokens", 0) or 0
                    if not (ptok or otok):
                        ptok = getattr(response, "total_tokens", 0) or 0
                    cost = usd_for(provider, ptok, otok, self.prices)
                    state.record(ptok, otok, cost, "ok", ok=True)
                    record.attempts.append(Attempt(
                        provider=provider, model=self.model_for(provider),
                        outcome="ok", status="ok", latency_s=latency,
                        prompt_tokens=ptok, output_tokens=otok, usd=cost,
                        delay_before_s=delay + slept))
                    record.actual_provider = provider
                    record.actual_model = self.model_for(provider)
                    record.outcome = "ok"
                    record.prompt_tokens = sum(a.prompt_tokens
                                               for a in record.attempts)
                    record.output_tokens = sum(a.output_tokens
                                               for a in record.attempts)
                    record.usd = round(sum(a.usd for a in record.attempts), 8)
                    record.latency_s = round(
                        sum(a.latency_s for a in record.attempts), 3)
                    if provider != assigned:
                        record.fallback_occurred = True
                        self.state(assigned).fallbacks_out += 1
                        state.fallbacks_in += 1
                        if not record.fallback_reason:
                            record.fallback_reason = (
                                record.attempts[0].reason or "assigned "
                                "provider exhausted its attempts")
                    self.per_provider_calls[provider] = (
                        self.per_provider_calls.get(provider, 0) + 1)
                    self.ledger.add(record)
                    return response

        record.outcome = "deferred"
        if not record.error_type:
            record.error_type = "no_provider_available"
        self.ledger.add(record)
        raise AllProvidersFailed(
            f"every provider refused or failed for agent {agent!r}: "
            + "; ".join(f"{a.provider}={a.outcome}" for a in record.attempts),
            record)

    _last_retry_after: float | None = None

    # `announce` and `last_issued_prompt` keep this swappable with the
    # existing clients, so no pipeline code changes to use it.
    def announce(self, agent_id: str, kind: str) -> None:
        self.active_agent = agent_id

    def fingerprint(self) -> dict[str, Any]:
        """No key, no key length, nothing derived from a key."""
        return {
            "providers": sorted(self.clients),
            "assigned_route_size": len(self.route),
            "fallback_chain": list(self.chain),
            "models": {p: self.model_for(p) for p in sorted(self.clients)},
            "prices_usd_per_mtok": {k: list(v) for k, v in
                                    sorted(self.prices.items())},
        }
