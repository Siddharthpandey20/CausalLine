"""
Retry with exponential backoff and jitter.

Built in from the start because counterfactual replay multiplies the call
count: one flagged source on one event is one extra call, and docs/04 asks
for three repeats of every comparison across roughly 30 runs per scenario.
Free-tier Gemini rate limits will be hit, and a run that dies at event 40 of
60 wastes the whole run's tokens.

Deliberately transport-agnostic: the caller decides what is retryable and
raises Retryable, so this module has no HTTP knowledge and can be tested with
plain functions.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

T = TypeVar("T")


class Retryable(Exception):
    """Raise to ask for another attempt.

    retry_after: seconds the server asked us to wait (Retry-After header).
    Honoured over our own backoff when it is longer -- ignoring it is how a
    temporary 429 turns into a hard block on the API key.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.25  # +/- fraction, so parallel workers do not sync up
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Delay before `attempt` (1-based). Server's Retry-After wins if larger."""
        backoff = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        spread = backoff * self.jitter
        delay = backoff + self.rng.uniform(-spread, spread)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return max(0.0, delay)


@dataclass
class RateLimiter:
    """Keep a minimum gap between calls.

    Retrying is how you recover from a rate limit; pacing is how you avoid
    one. They are not the same tool, and pacing is the one that matters here:
    a 429 retry spends another request against the same per-minute quota, so
    backing off *after* the fact digs the hole deeper. Free-tier Gemini allows
    20 requests per minute on Flash, and counterfactual replay in week 2 will
    sit right against that ceiling.

    Not thread-safe. One run is sequential; if that changes, this needs a lock.
    """

    min_interval_s: float = 0.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    total_waited_s: float = 0.0
    _last_call: float | None = None

    def wait(self) -> float:
        """Block until the next call is allowed. Returns seconds waited."""
        if self.min_interval_s <= 0:
            return 0.0
        now = self.clock()
        if self._last_call is not None:
            remaining = self.min_interval_s - (now - self._last_call)
            if remaining > 0:
                self.sleep(remaining)
                self.total_waited_s += remaining
                self._last_call = self.clock()
                return remaining
        self._last_call = now
        return 0.0


@dataclass
class Attempt:
    """What it took to get an answer. Recorded per call so we can report how
    much of the wall-clock cost was rate limiting rather than model latency."""

    attempts: int
    slept_s: float


def with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> tuple[T, Attempt]:
    """Call fn(), retrying while it raises Retryable.

    Returns (result, Attempt). Anything that is not Retryable propagates
    immediately: a 400 or a bad api key will not get better by waiting, and
    burning five attempts on it just hides the error message.
    """
    policy = policy or RetryPolicy()
    slept = 0.0
    last: Exception | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn(), Attempt(attempts=attempt, slept_s=slept)
        except Retryable as exc:
            last = exc
            if attempt == policy.max_attempts:
                break
            delay = policy.delay_for(attempt, exc.retry_after)
            if on_retry:
                on_retry(attempt, delay, exc)
            policy.sleep(delay)
            slept += delay

    raise RuntimeError(
        f"gave up after {policy.max_attempts} attempts ({slept:.1f}s waiting): {last}"
    ) from last
