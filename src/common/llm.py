"""
Gemini client.

Stdlib HTTP only (D-013). One POST to generateContent, retried on the status
codes that mean "later, not never", with token counts pulled off every
response.

Two things this client is strict about, because the paper depends on them:

  * every call returns its token counts, and the caller must say what the
    call was *for* (UsagePurpose). Cost split by purpose is the whole of
    open issue #7.
  * temperature and thinking level come from Settings and are recorded in
    the trace header, so a run's numbers can be tied to its configuration.
"""

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from src.common.config import Settings
from src.common.retry import RateLimiter, Retryable, RetryPolicy, with_retry

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# Status codes worth another attempt: rate limit, and the transient 5xx family.
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """A call that will not get better by retrying (bad key, bad request,
    safety block). Carries the server's message: hiding it costs hours."""


class QuotaExhausted(LLMError):
    """The per-day quota is gone. A 429, but not a retryable one.

    Gemini returns the same 429 for "too fast this minute" and "done for the
    day", and attaches a RetryInfo either way -- 41s in the daily case, which
    is nonsense advice: the window resets tomorrow, not in 41 seconds. Backing
    off against it wastes minutes and spends more rejected requests. The
    quotaId in the QuotaFailure detail is what separates the two."""


@dataclass
class ErrorInfo:
    """What we could learn from an error body."""

    message: str
    retry_after: float | None = None
    daily_quota_exhausted: bool = False


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    output_tokens: int
    thoughts_tokens: int
    total_tokens: int
    attempts: int
    latency_s: float
    slept_s: float
    finish_reason: str | None = None

    def json(self) -> Any:
        """Parse the response as JSON, tolerating ```json fences."""
        text = self.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text.rsplit("```", 1)[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"expected JSON, got: {self.text[:300]}") from exc


class GeminiClient:
    """One client per run. Keeps a running token total for the run summary."""

    def __init__(
        self,
        settings: Settings,
        policy: RetryPolicy | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy or RetryPolicy(
            max_attempts=settings.max_attempts, base_delay=2.0, max_delay=90.0
        )
        self.limiter = limiter or RateLimiter(
            min_interval_s=settings.min_call_interval_s()
        )
        self.calls = 0
        self.total_tokens = 0

    @property
    def throttled_s(self) -> float:
        """Wall-clock spent waiting for our own rate limiter. Reported next to
        token counts: at free-tier quotas this, not model latency, is what a
        batch of runs costs in time."""
        return self.limiter.total_waited_s

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        json_output: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        """One turn, no history. The pipeline passes whole context explicitly
        so that a counterfactual replay can remove one source and reproduce
        the call exactly -- hidden conversation state would make that
        impossible."""
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": (
                    self.settings.temperature if temperature is None else temperature
                ),
                "maxOutputTokens": self.settings.max_output_tokens,
                "thinkingConfig": {"thinkingLevel": self.settings.thinking_level},
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_output:
            body["generationConfig"]["responseMimeType"] = "application/json"

        started = time.time()
        payload, attempt = with_retry(lambda: self._post(body), self.policy)
        latency = time.time() - started

        usage = payload.get("usageMetadata", {})
        candidates = payload.get("candidates") or []
        if not candidates:
            raise LLMError(f"no candidates in response: {json.dumps(payload)[:300]}")
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        finish = candidate.get("finishReason")
        if not text and finish and finish != "STOP":
            # MAX_TOKENS with thinking on, or a safety block. Both are silent
            # empty strings otherwise, and an empty agent output would look
            # like a legitimate result in the trace.
            raise LLMError(f"empty response, finishReason={finish}")

        self.calls += 1
        total = int(usage.get("totalTokenCount", 0))
        self.total_tokens += total
        return LLMResponse(
            text=text,
            model=self.settings.model,
            prompt_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
            thoughts_tokens=int(usage.get("thoughtsTokenCount", 0)),
            total_tokens=total,
            attempts=attempt.attempts,
            latency_s=latency,
            slept_s=attempt.slept_s,
            finish_reason=finish,
        )

    # --- transport ---------------------------------------------------------

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_ROOT}/{self.settings.model}:generateContent"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.settings.api_key,
            },
            method="POST",
        )
        self.limiter.wait()
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            info = _parse_error(raw)
            if info.daily_quota_exhausted:
                raise QuotaExhausted(
                    f"HTTP {exc.code}: {info.message} "
                    "-- this is the per-day free-tier quota, not a per-minute "
                    "limit, so retrying will not help until the window resets."
                ) from exc
            if exc.code in RETRY_STATUS:
                raise Retryable(
                    f"HTTP {exc.code}: {info.message}",
                    retry_after=_retry_after(exc, info.retry_after),
                ) from exc
            raise LLMError(f"HTTP {exc.code}: {info.message}") from exc
        except urllib.error.URLError as exc:
            # DNS, connection refused, TLS failure. Transient by nature.
            raise Retryable(f"network error: {exc.reason}") from exc
        except OSError as exc:
            # A read timeout *after* the connection is established arrives as a
            # bare TimeoutError. That is an OSError but not a URLError, so the
            # clause above never saw it: it escaped the retry loop entirely and
            # then escaped the partial-trace handler in __main__ as well. That
            # is how a recoverable network stall killed the first live run four
            # calls in, with a stack trace instead of the trace report. D-020.
            raise Retryable(f"network error: {type(exc).__name__}: {exc}") from exc


# "Please retry in 4.458328969s." -- the number we actually need, buried in
# prose in the error message rather than in a header.
_RETRY_IN = re.compile(r"retry in ([0-9.]+)s", re.IGNORECASE)


def _parse_error(raw: str) -> ErrorInfo:
    """Pull a readable message and any server-suggested delay out of an error
    body.

    Gemini puts the wait in three possible places and the header is the one it
    tends not to use: a RetryInfo entry in error.details, the sentence "Please
    retry in Ns." inside error.message, or Retry-After. Missing it means our
    backoff guesses, and a guess that is short spends another request against
    the very quota that is exhausted.
    """
    try:
        error = json.loads(raw)["error"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return ErrorInfo(message=raw.strip()[:300])

    message = str(error.get("message", "")).strip()
    delay: float | None = None
    daily = False

    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        kind = detail.get("@type", "")
        if kind.endswith("RetryInfo"):
            raw_delay = str(detail.get("retryDelay", "")).rstrip("s")
            try:
                delay = float(raw_delay)
            except ValueError:
                pass
        elif kind.endswith("QuotaFailure"):
            for violation in detail.get("violations") or []:
                quota_id = str(violation.get("quotaId", ""))
                if "PerDay" in quota_id:
                    daily = True
                    limit = violation.get("quotaValue")
                    if limit:
                        message = f"{message} (daily limit {limit} requests)"
    if delay is None:
        found = _RETRY_IN.search(message)
        if found:
            delay = float(found.group(1))

    status = error.get("status")
    # Keep the quota line, drop the two documentation URLs that follow it.
    short = message.split("For more information")[0].strip()
    if status:
        short = f"{status}: {short}"
    return ErrorInfo(
        message=short[:300], retry_after=delay, daily_quota_exhausted=daily
    )


def _retry_after(
    exc: urllib.error.HTTPError, server_delay: float | None = None
) -> float | None:
    """Prefer whatever the server told us, from body or header."""
    if server_delay is not None:
        return server_delay
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None  # HTTP-date form; our own backoff will cover it


def smoke() -> int:
    """One minimal live call, to check the API contract without a full run.

        python -m src.common.llm --smoke

    Exists because a model change breaks the request body, not the pipeline,
    and finding that out costs a whole run's tokens otherwise. Prints the
    request body it sent (never the key) so a 400 can be read against it.
    """
    from src.common.config import load_settings

    settings = load_settings()
    client = GeminiClient(settings)
    print(f"model    {settings.model}")
    print(f"settings {settings.fingerprint()}")

    sent: dict[str, Any] = {}
    original = client._post

    def capture(body: dict[str, Any]) -> dict[str, Any]:
        sent.clear()
        sent.update(body)
        return original(body)

    client._post = capture  # type: ignore[method-assign]

    try:
        response = client.generate("Reply with the word ok.", system="You are terse.")
    except Exception as exc:
        print("\nrequest body:")
        print(json.dumps(sent, indent=2))
        print(f"\nFAILED: {exc}")
        return 1

    print(f"\ntext     {response.text.strip()[:60]!r}")
    print(f"finish   {response.finish_reason}")
    print(
        f"tokens   prompt={response.prompt_tokens} output={response.output_tokens} "
        f"thoughts={response.thoughts_tokens} total={response.total_tokens}"
    )
    print(f"call     attempts={response.attempts} latency={response.latency_s:.2f}s")
    if response.thoughts_tokens:
        # Not fatal, but it means the cost metric carries tokens that do not
        # appear anywhere in the trace. Worth knowing before a batch of runs.
        print(
            f"\nWARNING: {response.thoughts_tokens} thought tokens were billed at "
            f"thinking_level={settings.thinking_level!r}. See D-015."
        )
    print("\nOK")
    return 0


if __name__ == "__main__":
    import sys

    if "--smoke" in sys.argv:
        raise SystemExit(smoke())
    print("usage: python -m src.common.llm --smoke")
    raise SystemExit(2)
