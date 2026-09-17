"""
NVIDIA NIM client: three hosted models, three API keys, one integration point.

Stdlib HTTP only, for the same reason `src/common/llm.py` is (D-013). The
endpoint is OpenAI-compatible -- POST /v1/chat/completions -- which is a
different wire format from Gemini's generateContent, so this is a sibling of
GeminiClient rather than a subclass of it. What it shares is the contract the
rest of the project depends on:

    generate(prompt, system=, json_output=, temperature=) -> LLMResponse

Every client in this repository answers that call and returns token counts, so
the pipeline, the estimator, the replayer and the verifier take an NVIDIA
client wherever they took a Gemini one or a ScriptedClient. Nothing else in
src/ needs to know which provider is behind it. That is the whole of the
"do not scatter API calls through the repository" requirement: authentication,
model configuration, retry, backoff, cooldown and logging live here.

WHY THERE ARE THREE KEYS AND WHAT THEY ARE FOR
----------------------------------------------
Resilience, not rate-limit circumvention. A key is used until it says it
cannot serve us right now (429, or a transient service error attributed to
it); then it goes on a cooldown and the next key takes over. The pool is
sticky on purpose -- rotating on every request would spread load across three
accounts to go faster, which is not what the pool is for and is not what we do.
A key that comes back 401/403 is *permanently* wrong, so it leaves the rotation
entirely rather than being retried on a timer.

SECRETS
-------
Keys are read from the environment, held in memory, and never written anywhere.
`fingerprint()` excludes them by construction, every error body passes through
`redact()` before it reaches an exception message or a log line, and the pool
reports keys by slot number ("key 2 of 3"), never by value.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from src.common.config import DEFAULT_ENV_PATH, read_env_file
from src.common.llm import LLMError, LLMResponse
from src.common.retry import RateLimiter, Retryable, RetryPolicy, with_retry

API_ROOT = "https://integrate.api.nvidia.com/v1"

# Status codes that mean "later, not never".
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# Status codes that mean the *key* is the problem, not the request.
AUTH_STATUS = frozenset({401, 403})

# Ceiling on the doubling model cooldown. An hour is longer than any campaign
# we run, so a model that has failed repeatedly is out for the duration --
# which is the intent -- without the number becoming meaningless.
MAX_MODEL_COOLDOWN_S = 3600.0

ENV_PREFIX = "NVIDIA_"
KEY_VARS = ("NVIDIA_API_KEY_1", "NVIDIA_API_KEY_2", "NVIDIA_API_KEY_3")


class NoUsableKey(LLMError):
    """Every configured key is invalid or on cooldown with nothing left to try.

    A subclass of LLMError so a caller that already handles "this call will not
    get better" handles this too, but named separately because the remedy is
    different: not a smaller prompt or a different model, but a key.
    """


class ModelUnavailable(LLMError):
    """The model identifier is correct and the model is not there.

    Distinct from a bad request. NVIDIA retires hosted models and answers
    410 Gone with an end-of-life date afterwards, which is a permanent
    condition that must never be retried and must never be silently papered
    over by substituting a different model -- see MODELS below.
    """


# --- the model registry -------------------------------------------------------
#
# VERIFIED AGAINST THE LIVE API ON 10-09-2026, not taken from a display name.
# The three models this evaluation was specified against are named here by the
# identifier `GET /v1/models` and `POST /v1/chat/completions` actually accept.
#
# `minimaxai/minimax-m3` is the correct identifier and the model is GONE: the
# endpoint answers
#
#     410 {"title":"Gone","detail":"The model 'minimaxai/minimax-m3' has
#          reached its end of life on 2026-09-09T09:00:00Z and is no longer
#          available."}
#
# It stays in the registry, marked `retired`, with the identifier we verified
# and the reason we verified it. Deleting the entry would lose the finding;
# quietly swapping in another vendor's model under the name "MiniMax M3" would
# be worse -- it would change the research design and report the change
# nowhere. The evaluation runs on the models that answer and says, in its own
# results, which one did not.


@dataclass(frozen=True)
class NVIDIAModel:
    """One hosted model and the request shape it wants.

    `extra_body` exists because these models are not interchangeable at the
    wire level. Nemotron 3.5 Lightning is a reasoning model that, left alone,
    spends its output budget on a visible chain of thought and returns it in
    `reasoning_content`; `{"thinking": false}` turns that off. That is the same
    decision D-015 records for Gemini's `thinking_level="minimal"` and it is
    made for the same two reasons: thought tokens are billed and appear nowhere
    in the trace, and they are a second source of run-to-run variation.
    """

    handle: str          # short name used in config, filenames and results
    model_id: str        # the identifier the API accepts
    display: str         # the name a human asked for
    status: str = "available"      # "available" | "retired"
    status_detail: str = ""
    extra_body: dict[str, Any] = field(default_factory=dict)
    supports_json_mode: bool = True
    # Reasoning models need headroom: the visible answer comes after the
    # thinking, and a budget sized for the answer alone truncates it.
    max_output_tokens: int = 2048
    # A separate, much smaller ceiling for JSON requests. Every JSON answer
    # this project asks for is small -- a plan of three questions, or a
    # self-report over a handful of source ids -- so the headroom above buys
    # nothing here and costs a great deal when the model degenerates.
    #
    # Measured: a degenerate answer is an opening brace followed by whitespace
    # until the budget runs out. At 2048 that is ~80 seconds per rejected
    # attempt, and five attempts with backoff is seven minutes to fail one
    # call. At 768 the same failure costs ~25s and the retry that succeeds
    # arrives while the run is still worth having.
    json_max_tokens: int = 768
    # Per-model ceiling on how long one request may take, overriding the
    # global timeout when it is lower. Not a nicety: a model whose endpoint
    # stalls rather than erroring will otherwise hold every attempt for the
    # full global timeout, and five attempts at 300s is 25 minutes spent
    # discovering that one model is down. A short ceiling turns a stall into a
    # transient error the pool can act on.
    timeout_s: float | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"


MODELS: dict[str, NVIDIAModel] = {
    "minimax": NVIDIAModel(
        handle="minimax",
        model_id="minimaxai/minimax-m3",
        display="MiniMax M3",
        status="retired",
        status_detail=(
            "410 Gone: reached end of life on 2026-09-09T09:00:00Z on "
            "integrate.api.nvidia.com. Identifier verified against the live "
            "API on 10-09-2026; the model is not served and no substitute is "
            "made under its name."
        ),
    ),
    "nemotron": NVIDIAModel(
        handle="nemotron",
        model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
        display="Nemotron-3.5-Lightning-30B-A3B",
        extra_body={"chat_template_kwargs": {"thinking": False}},
    ),
    "deepseek": NVIDIAModel(
        handle="deepseek",
        model_id="deepseek-ai/deepseek-v4-flash-0731",
        display="DeepSeek-V4-Flash-0731",
        # INTERMITTENT, and the registry says so rather than picking a story.
        #
        # 10-09-2026: a trivial 8-token request stalled past 300s on all three
        # keys, repeatedly, with no status code and no body. Not a rate limit
        # (a 429 has a body) and not an entitlement problem (that returns 404
        # with a "not found for account" detail, which a sibling model does
        # return).
        #
        # 11-09-2026: the same request returns in 0.7s. A 768-token JSON
        # request returns in 0.7s. And in between, a generation call inside a
        # campaign still exhausted its retry budget on timeouts and fell back
        # to Nemotron -- with the preflight having passed minutes earlier.
        #
        # So this endpoint is flaky rather than gone, on a timescale of
        # minutes, and neither "works" nor "is down" is a true description of
        # it. That is why the pool preflights, why the model cooldown doubles,
        # and why `GenerationRecord.fallback_from` exists: a scenario this
        # model was asked for and did not produce is recorded as such instead
        # of quietly appearing under whoever picked it up.
        #
        # The ceiling stays low. It is what turns a stall into a transient
        # error the pool can act on within 90 seconds instead of 25 minutes,
        # and a model that genuinely needs longer than this for a 768-token
        # answer is not one a campaign of this size can afford anyway.
        timeout_s=90.0,
    ),
}

# Order matters: it is the fallback order for the model pool and the
# round-robin order for generation diversity.
MODEL_ORDER: tuple[str, ...] = ("nemotron", "deepseek", "minimax")


def model_for(handle: str) -> NVIDIAModel:
    if handle in MODELS:
        return MODELS[handle]
    for model in MODELS.values():
        if model.model_id == handle:
            return model
    raise ValueError(f"unknown NVIDIA model {handle!r}; have {sorted(MODELS)}")


def available_models() -> list[NVIDIAModel]:
    """The registry entries we can actually call, in fallback order."""
    return [MODELS[h] for h in MODEL_ORDER if MODELS[h].available]


def retired_models() -> list[NVIDIAModel]:
    return [MODELS[h] for h in MODEL_ORDER if not MODELS[h].available]


# --- settings -----------------------------------------------------------------


class MissingAPIKey(RuntimeError):
    """Raised with instructions rather than a bare KeyError."""


@dataclass(frozen=True)
class NVIDIASettings:
    """Everything the client needs, minus anything it must not print.

    Frozen for the same reason Settings is: a run that changed model or
    temperature halfway through would not be reproducible even to the limited
    extent a hosted model allows.
    """

    # `repr=False` IS THE POINT OF THE DOCSTRING ABOVE, AND IT WAS MISSING.
    # The class says it holds "nothing it must not print" and then the default
    # dataclass repr printed every key: `print(settings)`, a REPL echo, an
    # f-string in a log line, or an exception whose args include the settings
    # object all leaked the pool verbatim. It happened on 17-09-2026 while
    # checking this module's rate limiter, into a terminal transcript.
    #
    # `redact()` exists a few lines down to keep keys out of provider error
    # text; this keeps them out of our own.
    api_keys: tuple[str, ...] = field(repr=False)
    model: str = "nemotron"
    temperature: float = 0.0
    max_output_tokens: int = 2048
    timeout_s: float = 300.0
    max_attempts: int = 5
    # 0 disables pacing. NVIDIA does not publish a single free-tier number and
    # ours has not been observed to 429 at this rate; pacing is still cheaper
    # than discovering the ceiling with a rejected request (see RateLimiter).
    requests_per_minute: int = 40
    # How long a key sits out after a 429 before it is tried again.
    key_cooldown_s: float = 60.0
    # How long a model sits out after failing in a way attributable to it.
    #
    # MEASURED, AND THE FIRST NUMBER WAS AN ORDER OF MAGNITUDE TOO SMALL.
    # 120s was chosen to match the key cooldown, which is wrong because the two
    # failures do not cost the same. A rate-limited key costs one request. A
    # model that will not serve a whole test costs the test: on DeepSeek that
    # was 22 minutes of stalled retries for a void row. A 120s window expired
    # between one test and the next, so the campaign handed the same dead
    # endpoint the very next test it had.
    #
    # The cooldown should be on the order of what the failure cost, so the
    # first one is 15 minutes and it doubles from there. A model having a bad
    # minute loses one slot in the rotation; a model that is down loses the
    # campaign, which is the correct outcome for it.
    model_cooldown_s: float = 900.0
    max_concurrency: int = 2

    def key_count(self) -> int:
        return len(self.api_keys)

    def min_call_interval_s(self) -> float:
        if self.requests_per_minute <= 0:
            return 0.0
        return 60.0 / self.requests_per_minute

    def fingerprint(self) -> dict[str, object]:
        """The configured defaults. Never includes a key -- there is no code
        path from this method to `api_keys`, which is the point of writing it
        out field by field instead of asdict().

        `model` here is the DEFAULT model handle, which is not necessarily the
        model a particular client is pointed at: `ModelPool.client_for()`
        overrides it per client. Use `NVIDIAClient.fingerprint()` for a trace
        header -- see the note there.
        """
        return {
            "provider": "nvidia",
            "model": MODELS[self.model].model_id if self.model in MODELS else self.model,
            "model_handle": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "requests_per_minute": self.requests_per_minute,
            "api_keys_configured": len(self.api_keys),
        }


def load_nvidia_settings(
    path: str | Path = DEFAULT_ENV_PATH, **overrides: object
) -> NVIDIASettings:
    """Build settings from .env plus the real environment.

    Precedence matches `load_settings()`: real environment variables beat
    .env, keyword overrides beat both.
    """
    values = read_env_file(path)
    values.update({k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)})

    supplied = overrides.pop("api_keys", None)
    if supplied is not None:
        keys = tuple(str(k) for k in supplied if str(k).strip())  # type: ignore[union-attr]
    else:
        keys = tuple(
            values[name].strip() for name in KEY_VARS if values.get(name, "").strip()
        )
    if not keys:
        raise MissingAPIKey(
            "No NVIDIA API key found. Copy .env.example to .env and set "
            f"{', '.join(KEY_VARS)}, or export them. Keys are never committed: "
            ".env is gitignored."
        )

    def get(name: str, cast, default):
        if name in overrides:
            return cast(overrides.pop(name))
        raw = values.get(ENV_PREFIX + name.upper())
        return cast(raw) if raw not in (None, "") else default

    settings = NVIDIASettings(
        api_keys=keys,
        model=get("model", str, "nemotron"),
        temperature=get("temperature", float, 0.0),
        max_output_tokens=get("max_output_tokens", int, 2048),
        timeout_s=get("timeout_s", float, 300.0),
        max_attempts=get("max_attempts", int, 5),
        requests_per_minute=get("requests_per_minute", int, 40),
        key_cooldown_s=get("key_cooldown_s", float, 60.0),
        model_cooldown_s=get("model_cooldown_s", float, 120.0),
        max_concurrency=get("max_concurrency", int, 2),
    )
    if overrides:
        raise TypeError(f"unknown settings: {sorted(overrides)}")
    if settings.model not in MODELS:
        raise ValueError(
            f"NVIDIA_MODEL {settings.model!r} not one of {sorted(MODELS)}"
        )
    return settings


# --- secret hygiene -----------------------------------------------------------


def redact(text: str, secrets: Iterable[str]) -> str:
    """Remove any configured key from a string before it is shown to anyone.

    Keys travel in a header and not in a body, so a key appearing in a
    response is not expected. That is exactly why this runs anyway: the
    expected case needs no defence and the unexpected one is a credential in a
    log file. Cheap, unconditional, and applied at the single point where a
    server string enters our own error text.
    """
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in out:
            out = out.replace(secret, "***REDACTED***")
    return out


# --- the key pool -------------------------------------------------------------


@dataclass
class KeyState:
    slot: int                  # 1-based, for logging. Never the key itself.
    cooldown_until: float = 0.0
    invalid: bool = False
    invalid_reason: str = ""
    uses: int = 0
    rate_limits: int = 0

    def usable(self, now: float) -> bool:
        return not self.invalid and now >= self.cooldown_until


@dataclass
class KeyPool:
    """Three keys, used one at a time, in order.

    STICKY BY DESIGN. `current()` returns the same key on every call until
    something marks it unavailable. The alternative -- rotate per request --
    would be a way of getting more throughput out of three accounts than one
    account allows, which is not what this pool is for.

    Two ways out of the rotation, and they are not the same:

        cool(seconds)     temporary. A 429, or a transient service error while
                          this key was in use. Comes back when the timer runs out.
        invalidate(why)   permanent. 401/403 -- the key is wrong, and a wrong
                          key is wrong in an hour too.

    Thread-safe, because one pool is shared by every client in a campaign and
    the campaign may run two tests at once (§17). Without the lock, two threads
    reading `_index` between one's read and its `_advance()` both pick the key
    that has just been rate-limited.
    """

    keys: tuple[str, ...]
    cooldown_s: float = 60.0
    clock: Callable[[], float] = time.monotonic
    states: list[KeyState] = field(default_factory=list)
    rotations: int = 0

    def __post_init__(self) -> None:
        if not self.keys:
            raise MissingAPIKey("KeyPool needs at least one key")
        self.states = [KeyState(slot=i + 1) for i in range(len(self.keys))]
        self._index = 0
        self._lock = threading.Lock()

    # --- selection -------------------------------------------------------

    def current(self) -> tuple[int, str]:
        """(index, key) of the key to use now. Raises when there is none."""
        with self._lock:
            now = self.clock()
            n = len(self.keys)
            for offset in range(n):
                index = (self._index + offset) % n
                if self.states[index].usable(now):
                    if index != self._index:
                        self.rotations += 1
                    self._index = index
                    self.states[index].uses += 1
                    return index, self.keys[index]
            raise NoUsableKey(self._exhausted_message(now))

    def _exhausted_message(self, now: float) -> str:
        invalid = [s.slot for s in self.states if s.invalid]
        cooling = [
            (s.slot, round(s.cooldown_until - now, 1))
            for s in self.states
            if not s.invalid and s.cooldown_until > now
        ]
        parts = [f"all {len(self.keys)} NVIDIA API keys are unusable"]
        if invalid:
            parts.append(f"permanently rejected: slot(s) {invalid}")
        if cooling:
            parts.append(
                "cooling down: "
                + ", ".join(f"slot {slot} for {secs}s" for slot, secs in cooling)
            )
        return "; ".join(parts)

    # --- feedback --------------------------------------------------------

    def cool(self, index: int, seconds: float | None = None) -> None:
        """This key cannot serve us right now. Try the next one."""
        with self._lock:
            state = self.states[index]
            state.rate_limits += 1
            state.cooldown_until = self.clock() + (
                self.cooldown_s if seconds is None else max(0.0, seconds)
            )
            self._advance()

    def invalidate(self, index: int, reason: str) -> None:
        """This key is wrong. It leaves the rotation for good."""
        with self._lock:
            state = self.states[index]
            state.invalid = True
            state.invalid_reason = reason
            self._advance()

    def _advance(self) -> None:
        self._index = (self._index + 1) % len(self.keys)

    # --- reporting -------------------------------------------------------

    def usable_count(self) -> int:
        now = self.clock()
        return sum(1 for s in self.states if s.usable(now))

    def report(self) -> list[dict[str, Any]]:
        """Per-slot usage, safe to print and safe to serialise."""
        now = self.clock()
        return [
            {
                "slot": s.slot,
                "uses": s.uses,
                "rate_limits": s.rate_limits,
                "invalid": s.invalid,
                "invalid_reason": s.invalid_reason,
                "cooling_for_s": round(max(0.0, s.cooldown_until - now), 1),
            }
            for s in self.states
        ]


# --- the client ---------------------------------------------------------------


@dataclass
class CallStats:
    """What a run of calls cost in reliability terms, not just tokens.

    Recorded because §12 of the evaluation spec asks for API failure and retry
    counts per test, and because a campaign that silently retried forty times
    is a different measurement from one that did not.
    """

    calls: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    retries: int = 0
    rate_limited: int = 0
    transient_errors: int = 0
    malformed_json: int = 0
    key_rotations: int = 0
    latency_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "transient_errors": self.transient_errors,
            "malformed_json": self.malformed_json,
            "key_rotations": self.key_rotations,
            "latency_s": round(self.latency_s, 2),
        }


class NVIDIAClient:
    """One client per model per run. Drop-in for GeminiClient.

    The model is fixed at construction. Falling back to a *different model*
    mid-run would change what the trace is a trace of, so that decision is the
    caller's and lives in ModelPool, one level up.
    """

    def __init__(
        self,
        settings: NVIDIASettings,
        model: str | None = None,
        policy: RetryPolicy | None = None,
        limiter: RateLimiter | None = None,
        pool: KeyPool | None = None,
    ) -> None:
        self.settings = settings
        self.spec = model_for(model or settings.model)
        if not self.spec.available:
            raise ModelUnavailable(
                f"{self.spec.display} ({self.spec.model_id}) is not available: "
                f"{self.spec.status_detail}"
            )
        self.pool = pool or KeyPool(
            keys=settings.api_keys, cooldown_s=settings.key_cooldown_s
        )
        self.policy = policy or RetryPolicy(
            max_attempts=settings.max_attempts, base_delay=2.0, max_delay=60.0
        )
        self.limiter = limiter or RateLimiter(
            min_interval_s=settings.min_call_interval_s()
        )
        self.stats = CallStats()

    # Names the rest of the project already reads off a client.
    @property
    def model(self) -> str:
        return self.spec.model_id

    def fingerprint(self) -> dict[str, object]:
        """What goes in the trace header for a run driven by THIS client.

        The settings fingerprint carries the *configured default* model, and a
        client built by `ModelPool.client_for()` is routinely pointed at a
        different one. Without this override a DeepSeek client wrote
        `model: nvidia/nemotron-3.5-lightning-30b-a3b` into its trace header --
        the same class of mistake D-057 records for the Gemini path, one level
        further down, and with the same consequence: a per-model comparison
        built on the header would attribute every run to whichever model
        happened to be the default.
        """
        return {**self.settings.fingerprint(), **self.spec_fingerprint()}

    def spec_fingerprint(self) -> dict[str, object]:
        """The fields this client owns, as opposed to the ones it inherits."""
        return {"model": self.spec.model_id, "model_handle": self.spec.handle}

    @property
    def calls(self) -> int:
        return self.stats.calls

    @property
    def total_tokens(self) -> int:
        return self.stats.total_tokens

    @property
    def throttled_s(self) -> float:
        return self.limiter.total_waited_s

    # --- the call ---------------------------------------------------------

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        json_output: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        """One turn, no history -- the same contract GeminiClient offers.

        No hidden conversation state, for the reason stated there: a
        counterfactual replay has to be able to remove one source from a stored
        prompt and re-issue the call exactly.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": self.spec.model_id,
            "messages": messages,
            "temperature": (
                self.settings.temperature if temperature is None else temperature
            ),
            "max_tokens": min(
                self.settings.max_output_tokens, self.spec.max_output_tokens
            ),
        }
        body.update(self.spec.extra_body)
        if json_output:
            body["max_tokens"] = min(body["max_tokens"], self.spec.json_max_tokens)
            if self.spec.supports_json_mode:
                body["response_format"] = {"type": "json_object"}

        started = time.time()
        # The whole attempt is inside the retry, not just the POST: a 200 whose
        # body is unusable is a failed attempt exactly as a 503 is. See
        # `_attempt`.
        #
        # `wasted` collects what the rejected attempts cost. Those tokens were
        # spent and the trace has to say so -- the cost metric splits recovery
        # into analysis and replay (open issue #7), and a resampled call that
        # reported only its final attempt would make the analysis look cheaper
        # than it was, in our own favour.
        wasted = [0, 0, 0]
        payload, attempt = with_retry(
            lambda: self._attempt(body, json_output, wasted), self.policy
        )
        latency = time.time() - started

        choice = (payload.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason")

        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0)) + wasted[0]
        output_tokens = int(usage.get("completion_tokens", 0)) + wasted[1]
        total = (
            int(usage.get("total_tokens", 0))
            or (int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0)))
        ) + wasted[2]

        self.stats.calls += 1
        self.stats.total_tokens += total
        self.stats.prompt_tokens += prompt_tokens
        self.stats.output_tokens += output_tokens
        self.stats.retries += attempt.attempts - 1
        self.stats.latency_s += latency
        self.stats.key_rotations = self.pool.rotations

        return LLMResponse(
            text=text,
            model=self.spec.model_id,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            # NVIDIA does not bill a separate reasoning bucket on this
            # endpoint; where a model returns reasoning_content it is inside
            # completion_tokens. Reported as 0 rather than guessed.
            thoughts_tokens=0,
            total_tokens=total,
            attempts=attempt.attempts,
            latency_s=latency,
            slept_s=attempt.slept_s,
            finish_reason=finish,
        )

    def _attempt(
        self, body: dict[str, Any], json_output: bool, wasted: list[int]
    ) -> dict[str, Any]:
        """One request, checked far enough to know whether it is usable.

        WHY THE CHECK IS HERE AND NOT IN THE CALLER
        --------------------------------------------
        Nemotron 3.5 Lightning intermittently answers a JSON request with an
        opening brace followed by a few thousand tab characters, at
        temperature 0, `finish_reason=stop`, and a plausible token count. The
        same prompt to the same model a minute later returns correct JSON.

        The caller sees that as `LLMError: expected JSON` and the run is over:
        it killed a live pipeline at its second call, having already spent the
        Planner's tokens. But it is a transient generation failure -- the same
        category as a 503 -- and the retry loop is right here. So the response
        is validated before it leaves the client, and a degenerate one is
        raised as Retryable, bounded by the same `max_attempts` as everything
        else. A model that keeps producing garbage still fails, loudly, after
        a bounded number of attempts.

        The tokens a rejected attempt burned are counted. They were spent.
        """
        payload = self._post(body)

        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(
                f"no choices in response: {self._safe(json.dumps(payload))[:300]}"
            )
        choice = choices[0]
        text = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason")

        if not text and finish and finish != "stop":
            # Same failure GeminiClient guards: a truncated or filtered answer
            # is an empty string, and an empty agent output looks like a
            # legitimate result once it is in the trace. Retryable rather than
            # fatal, because `length` and a content filter are both things a
            # second sample can get past.
            _charge(payload, wasted)
            raise Retryable(
                f"empty response from {self.spec.model_id}, finish_reason={finish}"
            )

        if json_output and not _parses_as_json(text):
            self.stats.malformed_json += 1
            _charge(payload, wasted)
            raise Retryable(
                f"{self.spec.model_id} returned {len(text)} characters that are "
                f"not JSON for a JSON request: {text[:60]!r}"
            )
        return payload

    # --- transport --------------------------------------------------------

    def _safe(self, text: str) -> str:
        return redact(text, self.settings.api_keys)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        index, key = self.pool.current()
        request = urllib.request.Request(
            f"{API_ROOT}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        self.limiter.wait()
        timeout = min(
            self.settings.timeout_s, self.spec.timeout_s or self.settings.timeout_s
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self._raise_for_http(exc, index)
            raise  # unreachable; _raise_for_http always raises

        except urllib.error.URLError as exc:
            self.stats.transient_errors += 1
            raise Retryable(f"network error: {self._safe(str(exc.reason))}") from exc
        except OSError as exc:
            # A read timeout after the connection is established arrives as a
            # bare TimeoutError -- an OSError but not a URLError. D-020 records
            # what missing this cost on the Gemini path; the same clause is
            # here so it cannot cost it again.
            self.stats.transient_errors += 1
            raise Retryable(
                f"network error: {type(exc).__name__}: {self._safe(str(exc))}"
            ) from exc

    def _raise_for_http(self, exc: urllib.error.HTTPError, index: int) -> None:
        """Turn one HTTP failure into the right kind of exception. Always raises.

        A method rather than a block inside `_post` so the test suite can drive
        it with a synthetic HTTPError and be testing *this* rule rather than a
        copy of it. Which failures are retried, and which take a key out of the
        rotation, is the whole substance of this module; a test that restated
        the rule would go on passing whatever the rule became.
        """
        raw = self._safe(exc.read().decode("utf-8", "replace"))
        message = _error_message(raw)
        slot = self.pool.states[index].slot

        if exc.code in AUTH_STATUS:
            # PERMANENT, and specific to this key. Drop it and let the next
            # attempt pick another -- but if it was the last one, the
            # NoUsableKey that `current()` then raises is not Retryable and
            # ends the call immediately, which is correct: no amount of waiting
            # produces a valid key.
            self.pool.invalidate(index, f"HTTP {exc.code}: {message}")
            raise Retryable(
                f"key slot {slot} rejected (HTTP {exc.code}: {message}); "
                f"{self.pool.usable_count()} key(s) left"
            ) from exc

        if exc.code == 410:
            raise ModelUnavailable(
                f"HTTP 410 for {self.spec.model_id}: {message}"
            ) from exc

        if exc.code == 429:
            self.stats.rate_limited += 1
            delay = _retry_after(exc)
            self.pool.cool(index, delay)
            raise Retryable(
                f"HTTP 429 on key slot {slot}: {message}", retry_after=delay
            ) from exc

        if exc.code in RETRY_STATUS:
            self.stats.transient_errors += 1
            raise Retryable(
                f"HTTP {exc.code}: {message}", retry_after=_retry_after(exc)
            ) from exc

        # 400, 404, 422 and friends: the request is wrong and will be wrong
        # next time. Never retried -- burning five attempts on a malformed body
        # only hides the message that explains it.
        raise LLMError(f"HTTP {exc.code}: {message}") from exc



def _charge(payload: dict[str, Any], wasted: list[int]) -> None:
    """Add a discarded attempt's tokens to this call's running waste.

    [prompt, output, total]. A list rather than a return value because the
    accumulator has to survive across the retry loop, which owns the call
    stack in between.
    """
    usage = payload.get("usage") or {}
    prompt = int(usage.get("prompt_tokens", 0))
    output = int(usage.get("completion_tokens", 0))
    wasted[0] += prompt
    wasted[1] += output
    wasted[2] += int(usage.get("total_tokens", 0)) or (prompt + output)


def _parses_as_json(text: str) -> bool:
    """Would `LLMResponse.json()` succeed on this?

    Mirrors that method's fence-tolerance deliberately: a check stricter than
    the parser would reject answers the caller can read, and a looser one would
    let through answers it cannot.
    """
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _error_message(raw: str) -> str:
    """A readable line out of an error body, whatever shape it came in.

    NVIDIA answers with RFC 7807 problem documents ("title"/"detail") on some
    paths and an OpenAI-style {"error": {"message": ...}} on others.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:300]
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if isinstance(error, str) and error:
            return error[:300]
        detail = parsed.get("detail") or parsed.get("title") or parsed.get("message")
        if detail:
            return str(detail)[:300]
    return raw.strip()[:300]


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None  # HTTP-date form; our own backoff covers it


# --- model-level fallback -----------------------------------------------------


@dataclass
class ModelPool:
    """Model fallback, one level above key fallback.

    The key pool answers "this key cannot serve this request". This answers
    "this model cannot serve this request" -- a 410, a model-side 5xx, or a
    key pool that has run dry while this model was in use. It exists so a
    campaign continues when one endpoint is down, which §13 asks for
    explicitly.

    It does NOT swap models inside a single agent run. A trace is a trace of
    one model, and splicing two models into one workflow would make the
    trace's `model` header a lie and the per-model comparison meaningless.
    The unit of fallback is a whole test.
    """

    settings: NVIDIASettings
    order: tuple[str, ...] = MODEL_ORDER
    clock: Callable[[], float] = time.monotonic
    cooldown_until: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    # One key pool shared across models: a rate-limited key is rate-limited
    # whatever we point it at.
    pool: KeyPool | None = None
    # ONE rate limiter for every client this pool hands out. A limiter per
    # client paces each client against the full budget, so two tests in flight
    # issue twice the configured rate -- which is how a campaign discovers the
    # ceiling with a 429 instead of staying under it.
    limiter: RateLimiter | None = None

    def __post_init__(self) -> None:
        if self.pool is None:
            self.pool = KeyPool(
                keys=self.settings.api_keys, cooldown_s=self.settings.key_cooldown_s
            )
        if self.limiter is None:
            self.limiter = RateLimiter(
                min_interval_s=self.settings.min_call_interval_s()
            )

    def candidates(self) -> list[str]:
        """Model handles that are registered, available, and not cooling."""
        now = self.clock()
        return [
            h
            for h in self.order
            if MODELS[h].available and now >= self.cooldown_until.get(h, 0.0)
        ]

    def client_for(self, handle: str) -> NVIDIAClient:
        return NVIDIAClient(
            self.settings, model=handle, pool=self.pool, limiter=self.limiter
        )

    def cool(self, handle: str, seconds: float | None = None) -> None:
        """Take a model out of the rotation, for longer each time it fails.

        THE COOLDOWN DOUBLES, AND THAT IS THE POINT.
        A fixed cooldown is right for a model having a bad minute and badly
        wrong for one that is simply down. Measured: with a flat 120s, a
        six-scenario generation pass preferred the dead DeepSeek endpoint for
        three of them, and each attempt cost five bounded requests that stall
        for 90 seconds apiece -- about seven minutes of nothing, three times
        over, in a pass whose useful work took under a minute per scenario.
        The cooldown expired between scenarios every time, so the pool
        cheerfully tried again.

        Doubling means a model that is genuinely down is tried once, briefly
        again, and then effectively left alone for the rest of the campaign,
        while a model that hiccups once is back almost immediately. The cap
        stops it growing past the length of a campaign.
        """
        self.failures[handle] = self.failures.get(handle, 0) + 1
        if seconds is None:
            base = self.settings.model_cooldown_s
            seconds = min(base * (2 ** (self.failures[handle] - 1)), MAX_MODEL_COOLDOWN_S)
        self.cooldown_until[handle] = self.clock() + seconds

    def preflight(
        self,
        on_result: Callable[[str, bool, str], None] | None = None,
        timeout_s: float = 45.0,
    ) -> dict[str, bool]:
        """One tiny call per candidate model, before a campaign commits to any.

        Cheap by construction: one attempt, a short timeout, four tokens of
        output. A model that fails here is cooled, so the campaign never
        prefers it.

        Worth two requests because the alternative was measured. A dead
        endpoint that stalls rather than erroring costs the full retry budget
        every time it is preferred -- five bounded requests at 90 seconds each
        -- and the doubling cooldown only limits how often that recurs. Paying
        one short request up front removes the first penalty as well.
        """
        # A short deadline of its own: this call exists to answer "is anything
        # there", and a model that needs 45 seconds to say "ok" is not a model
        # this campaign can afford either way.
        brisk = replace(self.settings, timeout_s=min(timeout_s, self.settings.timeout_s))
        results: dict[str, bool] = {}
        for handle in self.candidates():
            client = NVIDIAClient(
                brisk,
                model=handle,
                pool=self.pool,
                limiter=self.limiter,
                policy=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0),
            )
            try:
                client.generate("Reply with the single word: ok")
                results[handle] = True
                if on_result:
                    on_result(handle, True, "")
            except NoUsableKey:
                raise
            except Exception as exc:  # noqa: BLE001 -- the verdict is the point
                results[handle] = False
                self.cool(handle)
                if on_result:
                    on_result(handle, False, f"{type(exc).__name__}: {exc}"[:160])
        return results

    def run(
        self,
        fn: Callable[[NVIDIAClient], Any],
        prefer: str | None = None,
        on_failure: Callable[[str, Exception], None] | None = None,
    ) -> tuple[Any, str]:
        """Run `fn` against the preferred model, falling back down the order.

        Returns (result, handle actually used). Raises the last error when
        every candidate has been tried -- a caller that catches this can skip
        one test and keep the campaign going, which is the point.
        """
        order = self.candidates()
        if prefer and prefer in order:
            order = [prefer] + [h for h in order if h != prefer]
        if not order:
            raise ModelUnavailable(
                "no NVIDIA model is currently available: "
                + ", ".join(
                    f"{h} ({MODELS[h].status_detail or 'cooling down'})"
                    for h in self.order
                )
            )
        last: Exception | None = None
        for handle in order:
            try:
                return fn(self.client_for(handle)), handle
            except NoUsableKey:
                # Not the model's fault, and no other model will fare better.
                raise
            except (LLMError, RuntimeError) as exc:
                last = exc
                self.cool(handle)
                if on_failure:
                    on_failure(handle, exc)
        raise last if last else ModelUnavailable("no model produced a result")


# --- connectivity check -------------------------------------------------------


def doctor() -> int:
    """Check the setup and say what to do next.

        python -m src.common.nvidia --doctor

    Exists because the first five minutes with a research repo decide whether
    anyone runs it at all, and every failure in those five minutes looks the
    same from the outside: a stack trace. This answers the four questions a
    newcomer actually has -- is my .env found, are my keys loaded, does the
    provider answer, and what do I type next -- and it never prints a key.

    Returns 0 when the real-LLM mode is ready to run.
    """
    print("CausalLine setup check")
    print("=" * 60)

    root = Path(".").resolve()
    print(f"working directory   {root}")

    # --- 1. is there a .env, and is it safe -----------------------------
    env_path = Path(DEFAULT_ENV_PATH)
    if env_path.exists():
        print(f".env                found ({env_path.resolve()})")
    else:
        print(".env                MISSING")
        print()
        print("  Fix it:")
        print("    cp .env.example .env      # Windows: copy .env.example .env")
        print("    # then put your NVIDIA key in NVIDIA_API_KEY_1")
        print()
        print("  A key is free from https://build.nvidia.com -- sign in, pick a")
        print("  model, and copy the key from the code sample.")
        return 1

    # `git check-ignore` outside a repository writes "fatal: not a git
    # repository" to stderr, which is noise in the middle of a setup report.
    # Captured, not silenced with a shell redirect, so this works on Windows.
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            capture_output=True,
            timeout=10,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ignored = False  # no git here; not a reason to fail the check
    print(f".env gitignored     {'yes' if ignored else 'NO -- do not commit it'}")

    # --- 2. keys -------------------------------------------------------
    try:
        settings = load_nvidia_settings()
    except MissingAPIKey:
        print("NVIDIA keys         NONE FOUND")
        print()
        print("  Fix it: put at least one key in .env")
        print("    NVIDIA_API_KEY_1=nvapi-...")
        print()
        print("  One key is enough. The second and third are only a fallback")
        print("  chain for long campaigns; nothing needs them.")
        return 1

    n = settings.key_count()
    print(f"NVIDIA keys         {n} loaded (values never printed)")
    if n == 1:
        print("                    one is enough; 2-3 add resilience, not speed")

    # --- 3. the offline half, which needs no key at all -----------------
    print()
    print("offline mode (no API key needed, no quota):")
    try:
        from src.eval.scripted import ScriptedClient  # noqa: F401

        print("  scripted evaluation  ready   python -m src.eval.experiment")
    except Exception as exc:  # noqa: BLE001
        print(f"  scripted evaluation  BROKEN  {type(exc).__name__}: {exc}")
        return 1

    # --- 4. does the provider actually answer ---------------------------
    print()
    print("live models (one short request each):")
    pool = ModelPool(settings)
    results: dict[str, bool] = {}

    def note(handle: str, ok: bool, why: str) -> None:
        spec = MODELS[handle]
        mark = "ok" if ok else f"UNREACHABLE  {why}"
        print(f"  {spec.display:<34} {mark}")
        results[handle] = ok

    try:
        pool.preflight(on_result=note)
    except NoUsableKey as exc:
        print(f"  every key was rejected: {exc}")
        print()
        print("  Fix it: the key in .env is not valid. Copy it again from")
        print("  https://build.nvidia.com -- it starts with 'nvapi-'.")
        return 1

    for spec in retired_models():
        print(f"  {spec.display:<34} RETIRED      {spec.status_detail[:60]}")

    print()
    if not any(results.values()):
        print("No model answered. The keys load, so this is the provider, not you.")
        print("Try again shortly, or run the offline evaluation meanwhile:")
        print("    python -m src.eval.experiment")
        return 1

    live = [h for h, ok in results.items() if ok]
    print(f"READY. {len(live)} model(s) answering: {', '.join(live)}")
    print()
    print("What to run next, cheapest first:")
    print("  python -m src.eval.real_campaign --plan --n 3   # free, shows the plan")
    print("  python -m src.eval.real_campaign --n 3          # ~30-60 min, live")
    print()
    print("Read docs/09-real-llm-evaluation.md section 9 before quoting a number.")
    return 0


def smoke(models: Iterable[str] | None = None) -> int:
    """One minimal live call per model, plus a key-pool report.

        python -m src.common.nvidia --smoke

    Prints the request shape and the token counts, never a key. Exists for the
    same reason `src/common/llm.py --smoke` does: a model change breaks the
    request body rather than the pipeline, and finding that out inside a
    campaign costs the campaign.
    """
    try:
        settings = load_nvidia_settings()
    except MissingAPIKey as exc:
        print(f"FAILED: {exc}")
        return 1

    print(f"endpoint  {API_ROOT}")
    print(f"keys      {settings.key_count()} configured (values never printed)")
    print(f"settings  {settings.fingerprint()}")
    print()

    handles = list(models) if models else list(MODEL_ORDER)
    ok = 0
    unavailable = 0
    for handle in handles:
        spec = MODELS[handle]
        if not spec.available:
            print(f"{spec.display}")
            print(f"  id      {spec.model_id}")
            print(f"  SKIPPED {spec.status}: {spec.status_detail}")
            print()
            unavailable += 1
            continue
        client = NVIDIAClient(settings, model=handle)
        print(f"{spec.display}")
        print(f"  id      {spec.model_id}")
        try:
            response = client.generate(
                "Reply with the single word: ok", system="You are terse."
            )
        except Exception as exc:  # noqa: BLE001 -- the report is the point
            print(f"  FAILED  {type(exc).__name__}: {exc}")
            print()
            continue
        print(f"  text    {response.text.strip()[:60]!r}")
        print(f"  finish  {response.finish_reason}")
        print(
            f"  tokens  prompt={response.prompt_tokens} "
            f"output={response.output_tokens} total={response.total_tokens}"
        )
        print(
            f"  call    attempts={response.attempts} "
            f"latency={response.latency_s:.2f}s"
        )
        print(f"  keys    {client.pool.report()}")
        print()
        ok += 1

    live = len([h for h in handles if MODELS[h].available])
    print(f"{ok}/{live} available model(s) answered; {unavailable} retired")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if "--doctor" in argv:
        raise SystemExit(doctor())
    if "--smoke" in argv:
        picked = [a for a in argv if a in MODELS]
        raise SystemExit(smoke(picked or None))
    if "--models" in argv:
        for spec in MODELS.values():
            mark = "OK " if spec.available else "GONE"
            print(f"{mark} {spec.handle:<10} {spec.model_id:<45} {spec.display}")
            if spec.status_detail:
                print(f"     {spec.status_detail}")
        raise SystemExit(0)
    print("usage: python -m src.common.nvidia [--doctor | --smoke [model...] | --models]")
    raise SystemExit(2)
