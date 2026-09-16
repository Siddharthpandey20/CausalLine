"""
The local-LLaMA backend's contract, checked without a model.

WHY THIS FILE EXISTS, AND IT IS NOT A FORMALITY
------------------------------------------------
The client is a drop-in for `GeminiClient` / `NVIDIAClient`, and "drop-in" turned
out to mean more than `generate()`. Two attributes the shared code reads off
whatever client it is handed were missing, and both failed in the worst possible
place -- *after* the work:

  * `client.total_tokens`, read by `run_pipeline` on the last line of a run.
    Twelve real inference calls had already been made when it raised
    `AttributeError`; the run was discarded and reported as VOID with no cause.
  * `stats.rate_limited` / `stats.key_rotations`, read by
    `real_llm._attach_api_stats` at the reporting step, with the same result.

Three local tests lost to that, at four to five minutes each, before the reason
was even printed. Every assertion below costs nothing and would have caught it
before a single token was spent.

    python -m unittest tests.test_local_llama -v
"""

import unittest
from unittest import mock

from src.common.local_llama import (
    LocalLlamaClient,
    LocalLlamaSettings,
    LocalLlamaUnavailable,
    _parses_as_json,
)


class TestTheClientContract(unittest.TestCase):
    """Everything the shared pipeline and harness read off a client.

    Written as an explicit list rather than by running the pipeline, so the
    failure names the missing attribute instead of surfacing as an
    `AttributeError` three call frames down.
    """

    def setUp(self) -> None:
        self.client = LocalLlamaClient()

    def test_it_reports_its_own_identity(self) -> None:
        """D-057, D-059, D-075: the trace header asks the client, and a client
        with nothing to say gets stamped `gemini-3.6-flash`."""
        self.assertEqual(self.client.model, "llama3")
        fingerprint = self.client.fingerprint()
        self.assertEqual(fingerprint["model"], "llama3")
        self.assertEqual(fingerprint["provider"], "ollama-local")
        # The runtime configuration a result has to be reproducible against.
        for key in ("endpoint", "temperature", "num_ctx", "seed"):
            with self.subTest(key=key):
                self.assertIn(key, fingerprint)

    def test_run_pipeline_can_read_the_token_total(self) -> None:
        """The attribute that cost three runs."""
        self.assertEqual(self.client.total_tokens, 0)
        self.client.stats.prompt_tokens = 100
        self.client.stats.output_tokens = 40
        self.assertEqual(self.client.total_tokens, 140)

    def test_attach_api_stats_can_read_every_field_it_wants(self) -> None:
        """`real_llm._attach_api_stats` is shared with the NVIDIA frontier and
        the brief forbids changing it, so the local client carries the fields
        instead. Two of them are structurally zero and that is the
        measurement, not a placeholder: no quota, no keys."""
        from src.eval.real_llm import RealRunResult, _attach_api_stats

        result = RealRunResult(
            test_id="t", design={}, execution_model="llama3.2:3b",
            execution_model_id="llama3.2:3b", generator_model="nemotron",
            detector="oracle",
        )
        _attach_api_stats(result, self.client)
        self.assertEqual(result.api_rate_limited, 0)
        self.assertEqual(result.key_rotations, 0)
        self.assertEqual(result.throttled_s, 0.0)

    def test_it_answers_the_generate_signature(self) -> None:
        import inspect

        parameters = inspect.signature(self.client.generate).parameters
        for name in ("prompt", "system", "json_output", "temperature"):
            with self.subTest(name=name):
                self.assertIn(name, parameters)


class TestTransportBehaviour(unittest.TestCase):
    """The failure modes, without a model."""

    def test_a_dead_endpoint_is_not_retried(self) -> None:
        """Waiting does not start Ollama, so retrying is only slower."""
        client = LocalLlamaClient(
            settings=LocalLlamaSettings(endpoint="http://localhost:1", max_attempts=3)
        )
        with self.assertRaises(LocalLlamaUnavailable):
            client.generate("anything")
        self.assertEqual(client.stats.retries, 0, "an absent endpoint was retried")

    def test_a_transient_error_is_retried_then_raised(self) -> None:
        from src.common.llm import LLMError

        client = LocalLlamaClient(settings=LocalLlamaSettings(max_attempts=2))
        with mock.patch.object(
            LocalLlamaClient, "_post", side_effect=LLMError("HTTP 503: busy")
        ), mock.patch("time.sleep"):
            with self.assertRaises(LLMError):
                client.generate("anything")
        self.assertEqual(client.stats.retries, 1)
        self.assertEqual(client.stats.transient_errors, 2)

    def test_tokens_are_recorded_from_the_backend_not_invented(self) -> None:
        """A backend reporting zero would make the local analysis look free,
        which is the most flattering possible lie about a cost metric."""
        client = LocalLlamaClient()
        with mock.patch.object(
            LocalLlamaClient,
            "_post",
            return_value={
                "response": "ok",
                "prompt_eval_count": 321,
                "eval_count": 12,
                "done_reason": "stop",
            },
        ):
            response = client.generate("anything")
        self.assertEqual(response.prompt_tokens, 321)
        self.assertEqual(response.output_tokens, 12)
        self.assertEqual(response.total_tokens, 333)
        self.assertEqual(client.total_tokens, 333)

    def test_a_degenerate_json_answer_is_retried_and_still_charged(self) -> None:
        """D-056's rule, applied locally: a rejected attempt's tokens were
        still spent, so hiding them would make the analysis look cheaper than
        it was -- in our own favour."""
        from src.common.llm import LLMError

        client = LocalLlamaClient(settings=LocalLlamaSettings(max_attempts=2))
        with mock.patch.object(
            LocalLlamaClient,
            "_post",
            return_value={
                "response": "not json at all",
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        ):
            with self.assertRaises(LLMError):
                client.generate("anything", json_output=True)
        self.assertEqual(client.stats.malformed_json, 2)
        self.assertEqual(client.total_tokens, 30, "rejected attempts went uncharged")


class TestJsonTolerance(unittest.TestCase):
    def test_a_fenced_object_counts_as_json(self) -> None:
        self.assertTrue(_parses_as_json('```json\n{"a": 1}\n```'))

    def test_prose_does_not(self) -> None:
        self.assertFalse(_parses_as_json("I think the answer is 1"))


class TestTheNvidiaFrontierIsUntouched(unittest.TestCase):
    """The brief's hard rule, asserted rather than promised."""

    def test_the_local_modules_do_not_import_the_hosted_one(self) -> None:
        import ast
        from pathlib import Path

        for module in (
            "src/common/local_llama.py",
            "src/eval/local_campaign.py",
            "src/eval/local_bench.py",
        ):
            with self.subTest(module=module):
                tree = ast.parse(Path(module).read_text(encoding="utf-8"))
                imported: list[str] = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported += [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.append(node.module)
                for name in imported:
                    self.assertNotIn(
                        name,
                        ("src.common.nvidia", "src.eval.real_campaign"),
                        f"{module} reaches into the NVIDIA frontier",
                    )


if __name__ == "__main__":
    unittest.main()
