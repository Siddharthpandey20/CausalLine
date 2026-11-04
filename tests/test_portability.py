"""The repository has to run on a machine that is not the one it was written on.

Three properties, each of which was false before and is cheap to break again:

1. The local backend is configurable from the environment. It used to hardcode
   `http://localhost:11434` and `llama3`, so pointing at another machine's
   Ollama meant editing `src/common/local_llama.py`.
2. A missing API key does not prevent local-only execution. `gemini_client` and
   `nvidia_client` construct their settings eagerly and raise without a key, so
   `build_router` could not build *any* router on a machine with no keys --
   not even one that used nothing but the local model.
3. The startup diagnostic never prints a key. It reports presence and length.
   A dataclass repr leaked a key into a terminal once; this is the guard.
"""

from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

from src.common.local_llama import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    LocalLlamaSettings,
)
from src.eval.college_runner import shape_for, topology_from_args
from src.tracing.mixed import Topology


@contextmanager
def environment(**values: str | None):
    """Set/unset environment variables and put them back afterwards."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


OLLAMA_VARS = ("OLLAMA_HOST", "OLLAMA_ENDPOINT", "OLLAMA_MODEL",
               "OLLAMA_NUM_CTX", "OLLAMA_TIMEOUT_S",
               "OLLAMA_MAX_OUTPUT_TOKENS", "OLLAMA_MAX_ATTEMPTS")


class TestLocalBackendIsConfigurableFromTheEnvironment(unittest.TestCase):
    def test_defaults_when_nothing_is_set(self) -> None:
        with environment(**{v: None for v in OLLAMA_VARS}):
            settings = LocalLlamaSettings.from_env()
        self.assertEqual(settings.endpoint, DEFAULT_ENDPOINT)
        self.assertEqual(settings.model, DEFAULT_MODEL)

    def test_a_bare_host_and_port_gets_a_scheme(self) -> None:
        """`OLLAMA_HOST` is Ollama's own variable and Ollama documents it
        without a scheme, so a bare value must not produce an unusable URL."""
        with environment(OLLAMA_HOST="gpu-box:11434", OLLAMA_ENDPOINT=None):
            self.assertEqual(LocalLlamaSettings.from_env().endpoint,
                             "http://gpu-box:11434")

    def test_an_explicit_endpoint_wins_over_host(self) -> None:
        with environment(OLLAMA_HOST="wrong:1",
                         OLLAMA_ENDPOINT="https://right:443"):
            self.assertEqual(LocalLlamaSettings.from_env().endpoint,
                             "https://right:443")

    def test_the_numeric_settings_are_read(self) -> None:
        with environment(OLLAMA_MODEL="llama3.2", OLLAMA_NUM_CTX="8192",
                         OLLAMA_TIMEOUT_S="123.5",
                         OLLAMA_MAX_OUTPUT_TOKENS="77",
                         OLLAMA_MAX_ATTEMPTS="2"):
            settings = LocalLlamaSettings.from_env()
        self.assertEqual(settings.model, "llama3.2")
        self.assertEqual(settings.num_ctx, 8192)
        self.assertEqual(settings.timeout_s, 123.5)
        self.assertEqual(settings.max_output_tokens, 77)
        self.assertEqual(settings.max_attempts, 2)

    def test_a_keyword_override_beats_the_environment(self) -> None:
        """`mixed_campaign` passes the model this way so the published tag
        stays the default while the environment can still redirect the host."""
        with environment(OLLAMA_MODEL="from-env"):
            self.assertEqual(
                LocalLlamaSettings.from_env(model="explicit").model,
                "explicit")

    def test_an_unparseable_value_falls_back_instead_of_crashing(self) -> None:
        """A typo in a shell profile must not be a stack trace on a machine
        whose owner cannot read one."""
        with environment(OLLAMA_NUM_CTX="not-a-number",
                         OLLAMA_TIMEOUT_S=""):
            settings = LocalLlamaSettings.from_env()
        self.assertEqual(settings.num_ctx, 4096)
        self.assertGreater(settings.timeout_s, 0)


class TestLocalOnlyNeedsNoKey(unittest.TestCase):
    def test_build_router_local_only_constructs_no_external_client(self) -> None:
        from src.eval.mixed_campaign import build_ledgers, build_router

        # No key is removed from the environment here: the point is that the
        # local-only path never reaches the code that would read one.
        router = build_router(Topology(), build_ledgers(), local_only=True)
        self.assertEqual(sorted(router.clients), ["local"])
        self.assertEqual(router.routing, {})

    def test_every_agent_routes_locally(self) -> None:
        from src.eval.mixed_campaign import build_ledgers, build_router

        router = build_router(Topology(), build_ledgers(), local_only=True)
        for agent in ("hub", "synth", "spec1", "spec3", "ver1", "rev8",
                      "acq1", "executor"):
            self.assertEqual(router.provider_for(agent), "local")

    def test_local_only_clears_the_provider_assignment(self) -> None:
        """Otherwise "gemini" appears in the fingerprint of a local run and the
        trace misrepresents which model produced it."""
        import argparse

        args = argparse.Namespace(
            mode="baseline56", num_agents=0, local_only=True,
            acquisition=None, normalisers=None, specialists=None,
            verifiers=None, reviewers=None)
        topo = topology_from_args(args)
        self.assertEqual(topo.gemini_agents, ())
        self.assertEqual(topo.nvidia_agents, ())
        self.assertEqual(topo.routing(), {})


class TestTopologyScaling(unittest.TestCase):
    def test_the_published_size_reproduces_the_published_shape(self) -> None:
        """56 must land on 12/10/1/12/10/8/3 exactly, or `--num-agents 56`
        would silently be a different experiment from the default."""
        topo = shape_for(56)
        self.assertEqual(topo.agent_count, 56)
        self.assertEqual(
            (topo.acquisition, topo.normalisers, topo.specialists,
             topo.verifiers, topo.reviewers),
            (12, 10, 12, 10, 8))

    def test_every_scaled_shape_is_runnable(self) -> None:
        for n in (9, 12, 16, 20, 30, 40, 56, 60, 80):
            topo = shape_for(n)
            self.assertEqual(topo.problems(), [],
                             f"shape_for({n}) produced an unrunnable topology")

    def test_the_count_is_close_to_what_was_asked(self) -> None:
        for n in (20, 30, 40, 56):
            self.assertLessEqual(abs(shape_for(n).agent_count - n), 3)


class TestTheStartupCheckNeverPrintsAKey(unittest.TestCase):
    """Presence and length only. Enforced by reading the source, because the
    failure mode is a value reaching stdout and a passing functional test
    would not notice."""

    def setUp(self) -> None:
        from pathlib import Path

        self.source = Path("startup_check.py").read_text(encoding="utf-8")

    def test_no_key_value_is_formatted_into_output(self) -> None:
        # The only permitted uses of a key variable are `len(...)` and a
        # truthiness test. A bare interpolation of the value is not.
        for forbidden in ('{os.environ[name]}', '{value}"', 'print(value',
                          'f"{api_key}', 'print(os.environ'):
            self.assertNotIn(forbidden, self.source,
                             f"startup_check.py may be printing a key value: "
                             f"{forbidden}")

    def test_it_reports_length_rather_than_content(self) -> None:
        self.assertIn("chars", self.source)
        self.assertIn("len(os.environ[n].strip())", self.source)

    def test_placeholders_from_env_example_do_not_count_as_set(self) -> None:
        """`.env.example` copied without editing must report "not set", or the
        diagnostic sends someone looking for a network fault."""
        self.assertIn("your-key-here", self.source)
        self.assertIn("placeholders", self.source)


if __name__ == "__main__":
    unittest.main()
