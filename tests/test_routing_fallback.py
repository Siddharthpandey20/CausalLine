"""Provider failure must not destroy a long campaign, or corrupt its science.

The acceptance criteria name five scenarios. Each has a test here:
Gemini rate limiting, NVIDIA rate limiting, temporary Ollama unavailability,
exhausted retries, and complete provider failure.

Every test is free: the providers are fakes, and the router's `sleep` and
`clock` are injected so a sixty-second limiter window costs no real time.

    python -m unittest tests.test_routing_fallback -v
"""

from __future__ import annotations

import unittest

from src.common.llm import LLMError, LLMResponse, QuotaExhausted
from src.common.retry import Retryable
from src.eval.routing import (
    AllProvidersFailed,
    CALL_INVALID,
    CallLedger,
    FallbackRouter,
    OVER_QUOTA,
    PROVIDER_DOWN,
    ProviderLimits,
    ProviderState,
    TRANSIENT,
    classify,
    usd_for,
)


def response(text: str = "ok", prompt: int = 100, output: int = 10) -> LLMResponse:
    return LLMResponse(text=text, model="fake", prompt_tokens=prompt,
                       output_tokens=output, thoughts_tokens=0,
                       total_tokens=prompt + output, attempts=1,
                       latency_s=0.01, slept_s=0.0, finish_reason="stop")


class Fake:
    """A provider that fails in a prescribed way."""

    def __init__(self, name: str, fail_with: BaseException | None = None,
                 fail_times: int = 0, model: str = "") -> None:
        self.name = name
        self.model = model or f"{name}-model"
        self.fail_with = fail_with
        self.fail_times = fail_times
        self.calls = 0

    def generate(self, prompt, system=None, json_output=False,
                 temperature=None):
        self.calls += 1
        if self.fail_with is not None and self.calls <= self.fail_times:
            raise self.fail_with
        return response(f"{self.name}:{self.calls}")


def router(clients: dict, route: dict, **kw) -> FallbackRouter:
    slept: list[float] = []
    now = [1000.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    r = FallbackRouter(clients=clients, route=route,
                       chain=kw.pop("chain", ("local", "nvidia", "gemini")),
                       sleep=sleep, clock=lambda: now[0], **kw)
    r.slept = slept          # type: ignore[attr-defined]
    r.advance = lambda s: now.__setitem__(0, now[0] + s)  # type: ignore
    return r


class TestErrorClassification(unittest.TestCase):
    def test_each_error_family_lands_in_the_right_category(self) -> None:
        cases = [
            (Retryable("HTTP 429", retry_after=7.0), TRANSIENT),
            (Retryable("HTTP 503"), TRANSIENT),
            (QuotaExhausted("daily quota gone"), OVER_QUOTA),
            (LLMError("HTTP 401: API key not valid"), PROVIDER_DOWN),
            (LLMError("HTTP 403: permission denied"), PROVIDER_DOWN),
            (LLMError("HTTP 400: invalid request"), CALL_INVALID),
            (LLMError("safety block"), CALL_INVALID),
            (TimeoutError("read timed out"), TRANSIENT),
        ]
        for exc, want in cases:
            with self.subTest(error=type(exc).__name__, text=str(exc)[:30]):
                self.assertEqual(classify(exc)[0], want)

    def test_retry_after_is_carried_out_of_the_classifier(self) -> None:
        self.assertEqual(classify(Retryable("429", retry_after=11.0))[1], 11.0)


class TestGeminiRateLimiting(unittest.TestCase):
    """Scenario 1: Gemini 429s, then recovers."""

    def test_a_transient_429_is_retried_on_the_same_provider(self) -> None:
        gem = Fake("gemini", Retryable("HTTP 429", retry_after=5.0),
                   fail_times=2)
        r = router({"gemini": gem, "local": Fake("local")}, {"a": "gemini"})
        r.active_agent = "a"
        out = r.generate("p")
        self.assertEqual(out.text, "gemini:3")
        rec = r.ledger.records[-1]
        self.assertEqual(rec.outcome, "ok")
        self.assertFalse(rec.fallback_occurred,
                         "a retry on the assigned provider is not a fallback")
        self.assertEqual(rec.attempt_count, 3)
        self.assertEqual(rec.actual_provider, "gemini")

    def test_retry_after_floors_the_backoff(self) -> None:
        gem = Fake("gemini", Retryable("HTTP 429", retry_after=30.0),
                   fail_times=1)
        r = router({"gemini": gem, "local": Fake("local")}, {"a": "gemini"})
        r.active_agent = "a"
        r.generate("p")
        self.assertTrue(any(s >= 30.0 for s in r.slept),
                        f"Retry-After of 30s was not honoured: {r.slept}")

    def test_backoff_grows_and_is_jittered(self) -> None:
        gem = Fake("gemini", Retryable("HTTP 429"), fail_times=2)
        r = router({"gemini": gem, "local": Fake("local")}, {"a": "gemini"})
        r.states["gemini"].limits.attempts = 4
        r.active_agent = "a"
        r.generate("p")
        waits = [s for s in r.slept if s > 0]
        self.assertGreaterEqual(len(waits), 2)
        self.assertGreater(waits[1], waits[0], "backoff did not grow")

    def test_exhausted_gemini_falls_back_and_records_it(self) -> None:
        gem = Fake("gemini", Retryable("HTTP 429"), fail_times=99)
        loc = Fake("local")
        r = router({"gemini": gem, "local": loc}, {"a": "gemini"})
        r.active_agent = "a"
        out = r.generate("p")
        self.assertTrue(out.text.startswith("local:"))
        rec = r.ledger.records[-1]
        self.assertTrue(rec.fallback_occurred)
        self.assertEqual(rec.assigned_provider, "gemini")
        self.assertEqual(rec.actual_provider, "local")
        self.assertNotEqual(rec.assigned_model, rec.actual_model,
                            "a fallback must not look like the assigned model")
        self.assertTrue(rec.fallback_reason)

    def test_a_daily_quota_error_is_not_retried(self) -> None:
        gem = Fake("gemini", QuotaExhausted("per-day quota gone"),
                   fail_times=99)
        r = router({"gemini": gem, "local": Fake("local")}, {"a": "gemini"})
        r.active_agent = "a"
        r.generate("p")
        self.assertEqual(gem.calls, 1,
                         "a daily-quota 429 must not be retried; retrying it "
                         "burns quota to learn nothing")
        self.assertIn("gemini:over_quota", r.degraded)


class TestNvidiaRateLimiting(unittest.TestCase):
    """Scenario 2: the same policy when NVIDIA is the assigned provider."""

    def test_nvidia_falls_back_to_local(self) -> None:
        nv = Fake("nvidia", Retryable("HTTP 429"), fail_times=99)
        r = router({"nvidia": nv, "local": Fake("local"),
                    "gemini": Fake("gemini")}, {"a": "nvidia"})
        r.active_agent = "a"
        out = r.generate("p")
        self.assertTrue(out.text.startswith("local:"))
        self.assertEqual(r.ledger.records[-1].actual_provider, "local")

    def test_the_fallback_chain_order_is_honoured(self) -> None:
        r = router({"gemini": Fake("gemini", Retryable("429"), 99),
                    "local": Fake("local", Retryable("down"), 99),
                    "nvidia": Fake("nvidia")},
                   {"a": "gemini"}, chain=("local", "nvidia"))
        r.active_agent = "a"
        out = r.generate("p")
        self.assertTrue(out.text.startswith("nvidia:"),
                        "chain order local -> nvidia was not followed")


class TestOllamaUnavailable(unittest.TestCase):
    """Scenario 3: the local daemon is temporarily down."""

    def test_local_down_falls_back_to_a_hosted_provider(self) -> None:
        loc = Fake("local", ConnectionError("connection refused"),
                   fail_times=99)
        r = router({"local": loc, "nvidia": Fake("nvidia"),
                    "gemini": Fake("gemini")}, {"a": "local"},
                   chain=("nvidia", "gemini"))
        r.active_agent = "a"
        out = r.generate("p")
        self.assertTrue(out.text.startswith("nvidia:"))
        rec = r.ledger.records[-1]
        self.assertTrue(rec.fallback_occurred)
        self.assertEqual(rec.assigned_provider, "local")

    def test_local_recovers_after_a_transient_blip(self) -> None:
        loc = Fake("local", ConnectionError("refused"), fail_times=1)
        r = router({"local": loc, "nvidia": Fake("nvidia")}, {"a": "local"})
        r.active_agent = "a"
        out = r.generate("p")
        self.assertEqual(out.text, "local:2")
        self.assertFalse(r.ledger.records[-1].fallback_occurred)


class TestCircuitBreakerAndQuotaAwareness(unittest.TestCase):
    def test_the_circuit_opens_after_repeated_failures(self) -> None:
        state = ProviderState(limits=ProviderLimits(name="gemini", attempts=1))
        for _ in range(3):
            state.record(0, 0, 0.0, "transient", ok=False)
        ok, why = state.available()
        self.assertFalse(ok)
        self.assertIn("circuit open", why)

    def test_a_provider_over_its_daily_cap_is_not_routed_to(self) -> None:
        gem = Fake("gemini")
        r = router({"gemini": gem, "local": Fake("local")}, {"a": "gemini"})
        r.states["gemini"].limits.rpd = 2
        r.active_agent = "a"
        for _ in range(2):
            r.generate("p")
        r.generate("p")
        self.assertEqual(gem.calls, 2, "a call was made past the daily cap")
        rec = r.ledger.records[-1]
        self.assertEqual(rec.actual_provider, "local")
        self.assertIn("daily cap", rec.fallback_reason)

    def test_the_rpm_limiter_blocks_rather_than_overruns(self) -> None:
        state = ProviderState(limits=ProviderLimits(name="x", rpm=2))
        now = [0.0]
        slept: list[float] = []

        def sleep(s):
            slept.append(s)
            now[0] += s

        for _ in range(2):
            state.wait_for_slot(sleep, lambda: now[0])
        self.assertEqual(slept, [])
        state.wait_for_slot(sleep, lambda: now[0])
        self.assertTrue(slept and slept[0] > 0,
                        "the third call in a 2-RPM window did not wait")

    def test_a_tpm_limiter_is_enforced(self) -> None:
        state = ProviderState(limits=ProviderLimits(name="x", tpm=150))
        now = [0.0]
        slept: list[float] = []

        def sleep(s):
            slept.append(s)
            now[0] += s

        state.wait_for_slot(sleep, lambda: now[0])
        state.record(100, 10, 0.0, "ok", ok=True)
        state.wait_for_slot(sleep, lambda: now[0])
        state.record(100, 10, 0.0, "ok", ok=True)
        state.wait_for_slot(sleep, lambda: now[0])
        self.assertTrue(slept, "the token-per-minute ceiling was not enforced")


class TestExhaustedRetriesAndTotalFailure(unittest.TestCase):
    """Scenarios 4 and 5."""

    def test_every_provider_failing_raises_and_records_every_attempt(self) -> None:
        clients = {name: Fake(name, Retryable("down"), 99)
                   for name in ("gemini", "local", "nvidia")}
        r = router(clients, {"a": "gemini"})
        r.active_agent = "a"
        with self.assertRaises(AllProvidersFailed) as caught:
            r.generate("p")
        rec = caught.exception.record
        self.assertEqual(rec.outcome, "deferred",
                         "a run that could not be served is deferred, not "
                         "failed: it must be resumable")
        self.assertGreaterEqual(rec.attempt_count, 6,
                                f"attempts were lost: {rec.attempt_count}")
        self.assertEqual({a.provider for a in rec.attempts},
                         {"gemini", "local", "nvidia"})
        self.assertIs(rec, r.ledger.records[-1],
                      "the failed invocation was not written to the ledger")

    def test_retries_are_bounded(self) -> None:
        gem = Fake("gemini", Retryable("429"), fail_times=99)
        r = router({"gemini": gem}, {"a": "gemini"})
        r.states["gemini"].limits.attempts = 3
        r.active_agent = "a"
        with self.assertRaises(AllProvidersFailed):
            r.generate("p")
        self.assertEqual(gem.calls, 3, "retries were not bounded at 3")

    def test_a_call_level_error_does_not_fall_back(self) -> None:
        """A malformed request would be refused by every provider, so trying a
        second one spends quota to collect the same refusal."""
        gem = Fake("gemini", LLMError("HTTP 400: invalid request"), 99)
        loc = Fake("local")
        r = router({"gemini": gem, "local": loc}, {"a": "gemini"})
        r.active_agent = "a"
        with self.assertRaises(LLMError):
            r.generate("p")
        self.assertEqual(loc.calls, 0, "a bad request was sent to a second "
                                       "provider")
        self.assertEqual(r.ledger.records[-1].outcome, "failed")


class TestBudgetAndSpendCaps(unittest.TestCase):
    def test_usd_is_computed_from_the_price_table(self) -> None:
        self.assertAlmostEqual(
            usd_for("gemini", 1_000_000, 1_000_000,
                    {"gemini": (0.075, 0.300)}), 0.375, places=6)
        self.assertEqual(usd_for("local", 10 ** 7, 10 ** 7), 0.0)

    def test_a_provider_spend_cap_stops_routing_to_it(self) -> None:
        gem = Fake("gemini")
        r = router({"gemini": gem, "local": Fake("local")}, {"a": "gemini"},
                   prices={"gemini": (1000.0, 1000.0), "local": (0.0, 0.0)})
        r.states["gemini"].limits.max_usd = 0.15
        r.active_agent = "a"
        r.generate("p")                     # 110 tokens at $1000/Mtok = $0.11
        r.generate("p")
        self.assertEqual(gem.calls, 2)
        r.generate("p")
        self.assertEqual(gem.calls, 2, "a call was made past the spend cap")
        self.assertIn("spend cap", r.ledger.records[-1].fallback_reason)

    def test_the_campaign_cap_defers_without_sending(self) -> None:
        r = router({"gemini": Fake("gemini"), "local": Fake("local")},
                   {"a": "gemini"},
                   prices={"gemini": (1000.0, 1000.0), "local": (0.0, 0.0)})
        r.total_usd_cap = 0.05
        r.active_agent = "a"
        r.generate("p")                     # spends $0.11, over the cap
        with self.assertRaises(AllProvidersFailed) as caught:
            r.generate("p")
        self.assertEqual(caught.exception.record.error_type,
                         "campaign_spend_cap")
        self.assertEqual(caught.exception.record.attempt_count, 0,
                         "a call was attempted after the campaign cap")

    def test_failed_attempts_are_counted_in_reliability_accounting(self) -> None:
        gem = Fake("gemini", Retryable("429"), fail_times=2)
        r = router({"gemini": gem, "local": Fake("local")}, {"a": "gemini"})
        r.active_agent = "a"
        r.generate("p")
        totals = r.ledger.totals()
        self.assertEqual(totals["by_provider"]["gemini"]["attempts"], 3)
        self.assertEqual(totals["by_provider"]["gemini"]["ok"], 1)
        self.assertEqual(totals["total_attempts"], 3)


class TestTheReproducibilityRecord(unittest.TestCase):
    def test_every_field_the_audit_needs_is_present(self) -> None:
        gem = Fake("gemini", Retryable("429"), fail_times=99)
        r = router({"gemini": gem, "local": Fake("local")}, {"a": "gemini"})
        r.active_agent = "a"
        r.generate("p")
        data = r.ledger.records[-1].to_dict()
        for field_name in ("assigned_provider", "assigned_model",
                           "actual_provider", "actual_model",
                           "fallback_occurred", "fallback_reason",
                           "attempt_count", "retry_delays", "error_type",
                           "prompt_tokens", "output_tokens", "usd",
                           "latency_s", "outcome", "prompt_sha", "agent"):
            self.assertIn(field_name, data)
        self.assertTrue(data["prompt_sha"])

    def test_the_fingerprint_carries_no_key_material(self) -> None:
        r = router({"gemini": Fake("gemini")}, {})
        text = repr(r.fingerprint()).lower()
        for forbidden in ("key", "token", "secret", "nvapi", "aiza"):
            self.assertNotIn(forbidden, text,
                             f"{forbidden!r} appears in the fingerprint")

    def test_ledger_totals_separate_ok_failed_and_deferred(self) -> None:
        ledger = CallLedger()
        r = router({"gemini": Fake("gemini")}, {"a": "gemini"}, ledger=ledger)
        r.active_agent = "a"
        r.generate("p")
        self.assertEqual(ledger.totals()["ok"], 1)
        self.assertEqual(ledger.totals()["failed"], 0)
        self.assertEqual(ledger.totals()["deferred"], 0)


if __name__ == "__main__":
    unittest.main()
