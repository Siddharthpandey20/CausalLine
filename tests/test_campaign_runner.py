"""The campaign must survive interruption, outage and a budget cap.

A run that loses hours of work to one failed request is not usable on a
borrowed machine, so every failure mode the acceptance criteria name is tested
here. All of it runs in stub mode: no key, no GPU, no network.

    python -m unittest tests.test_campaign_runner -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from experiments.industry_validation import (
    DEFAULTS,
    METHODS,
    Progress,
    RunDir,
    aggregate,
    load_config,
    plan,
)
from src.eval.routing import CallLedger


def stub_config(**over) -> dict:
    config = load_config(None)
    config.update({"stub": True, "trials": 1, "architectures": ["A"],
                   "levels": ["small"]})
    config.update(over)
    return config


class TestThePlan(unittest.TestCase):
    def test_the_matrix_is_architectures_times_levels_times_trials(self) -> None:
        config = stub_config(architectures=["A", "B"],
                             levels=["small", "large"], trials=2)
        self.assertEqual(len(plan(config)), 2 * 2 * 2)

    def test_the_full_matrix_is_seven_by_three(self) -> None:
        config = load_config(None)
        config["trials"] = 3
        self.assertEqual(len(plan(config)), 7 * 3 * 3)
        self.assertEqual(len(plan(config)) * len(METHODS), 315)

    def test_asking_for_more_trials_than_seeds_is_refused(self) -> None:
        config = stub_config(trials=99)
        with self.assertRaises(SystemExit):
            plan(config)

    def test_all_five_methods_are_configured_by_default(self) -> None:
        self.assertEqual(tuple(DEFAULTS["methods"]), METHODS)
        self.assertEqual(len(DEFAULTS["architectures"]), 7)
        self.assertEqual(sorted(DEFAULTS["levels"]),
                         ["large", "medium", "small"])


class TestTheRunDirectory(unittest.TestCase):
    def test_every_artifact_directory_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            for sub in ("config", "traces", "recovered", "results",
                        "checkpoints", "logs", "providers", "reports"):
                self.assertTrue((run.root / sub).is_dir(), sub)

    def test_an_atomic_write_leaves_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            target = run.root / "results" / "x.json"
            run.write_atomic(target, {"a": 1})
            self.assertEqual(json.loads(target.read_text())["a"], 1)
            self.assertFalse(list(target.parent.glob("*.part")),
                             "a .part file survived the write")


class TestCheckpointAndResume(unittest.TestCase):
    def _ledger(self, run: RunDir, pairs, write_results=True) -> None:
        for placement, method in pairs:
            if write_results:
                run.write_atomic(run.result(placement, method), {"ok": True})
            run.append(run.units, {"placement": placement, "method": method,
                                   "status": "ok", "at": "now"})

    def test_completed_units_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            self._ledger(run, [("p1", "B0"), ("p1", "B1")])
            self.assertEqual(run.completed(), {("p1", "B0"), ("p1", "B1")})

    def test_a_unit_whose_result_file_is_gone_is_not_counted(self) -> None:
        """Otherwise a deleted artifact would be assumed done and the campaign
        would report a complete run with a missing cell."""
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            self._ledger(run, [("p1", "B0")], write_results=False)
            self.assertEqual(run.completed(), set())

    def test_a_truncated_trailing_line_is_discarded_not_fatal(self) -> None:
        """A process killed mid-append is the normal case this must survive."""
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            self._ledger(run, [("p1", "B0")])
            with run.units.open("a", encoding="utf-8") as handle:
                handle.write('{"placement": "p1", "method": "B1", "stat')
            self.assertEqual(run.completed(), {("p1", "B0")})

    def test_a_failed_or_deferred_unit_is_not_counted_as_done(self) -> None:
        """Never mark an incomplete invocation successful."""
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            run.write_atomic(run.result("p1", "IREC"), {"partial": True})
            for status in ("failed", "deferred"):
                run.append(run.units, {"placement": "p1", "method": "IREC",
                                       "status": status, "at": "now"})
            self.assertEqual(run.completed(), set())

    def test_resume_skips_completed_and_keeps_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            self._ledger(run, [("p1", "B0"), ("p1", "B2")])
            done = run.completed()
            remaining = [m for m in METHODS if ("p1", m) not in done]
            self.assertEqual(remaining, ["B1", "CausalLine", "IREC"])


class TestCompletenessIsNeverAssumed(unittest.TestCase):
    def test_a_short_run_is_labelled_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            config = stub_config()
            run.write_atomic(run.result("p1", "B0"), {
                "method": "B0", "unsafe_num": 0, "unsafe_den": 2,
                "recall_num": 2, "escapes_num": 0, "task_success": True,
                "replay_tokens": 10, "analysis_tokens": 0, "usd": 0.0,
                "work_preserved_num": 1, "work_preserved_den": 2})
            summary = aggregate(run, config, CallLedger(), None)
            self.assertEqual(summary["status"], "INCOMPLETE")
            self.assertFalse(summary["complete"])
            self.assertEqual(summary["units_expected"], len(METHODS))
            self.assertEqual(summary["units_present"], 1)

    def test_a_paused_run_is_incomplete_even_with_every_unit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = RunDir(Path(raw) / "RUN")
            config = stub_config(methods=["B0"])
            run.write_atomic(run.result("p", "B0"), {"method": "B0"})
            summary = aggregate(run, config, CallLedger(),
                                "all providers failed")
            self.assertEqual(summary["status"], "INCOMPLETE")
            self.assertEqual(summary["paused"], "all providers failed")


class TestProgressIsComputedFromRecords(unittest.TestCase):
    def test_it_reports_counts_not_guesses(self) -> None:
        progress = Progress(total_placements=21, total_units=105,
                            done_units=7)
        progress.architecture = "A-parallel-research"
        progress.level = "medium"
        progress.agents = "61/61"
        progress.method_state = {"B0": "Complete", "IREC": "Pending"}
        text = progress.render(None, stub_config(methods=["B0", "IREC"]))
        for expected in ("A-parallel-research", "medium", "61/61",
                         "7/105", "B0", "Complete", "IREC", "Pending"):
            self.assertIn(expected, text)

    def test_provider_usage_and_spend_appear_when_a_router_is_given(self) -> None:
        from src.eval.routing import FallbackRouter, ProviderLimits, ProviderState

        router = FallbackRouter(clients={"gemini": object()}, route={})
        router.states["gemini"] = ProviderState(
            limits=ProviderLimits(name="gemini", max_usd=5.0))
        router.states["gemini"].record(100, 10, 0.25, "ok", ok=True)
        text = Progress(total_placements=1, total_units=1).render(
            router, stub_config(methods=["B0"]))
        self.assertIn("gemini", text)
        self.assertIn("spend", text)


class TestBudgetPauseAndOutage(unittest.TestCase):
    """Campaign-level behaviour of the failures routing already unit-tests."""

    def test_the_campaign_spend_cap_defers_rather_than_spending(self) -> None:
        from src.eval.routing import AllProvidersFailed, FallbackRouter

        class Fake:
            model = "fake"

            def generate(self, *a, **k):
                raise AssertionError("a call was made past the cap")

        router = FallbackRouter(clients={"gemini": Fake()}, route={"x": "gemini"})
        router.total_usd_cap = 0.0
        router.active_agent = "x"
        with self.assertRaises(AllProvidersFailed) as caught:
            router.generate("p")
        self.assertEqual(caught.exception.record.error_type,
                         "campaign_spend_cap")

    def test_a_total_outage_raises_the_signal_the_runner_pauses_on(self) -> None:
        from src.common.retry import Retryable
        from src.eval.routing import AllProvidersFailed, FallbackRouter

        class Dead:
            model = "dead"

            def generate(self, *a, **k):
                raise Retryable("HTTP 503")

        router = FallbackRouter(
            clients={"gemini": Dead(), "local": Dead(), "nvidia": Dead()},
            route={"x": "gemini"}, sleep=lambda s: None)
        router.active_agent = "x"
        with self.assertRaises(AllProvidersFailed):
            router.generate("p")
        # The record is kept so the resume knows which invocation to redo.
        self.assertEqual(router.ledger.records[-1].outcome, "deferred")

    def test_the_runner_treats_that_signal_as_a_pause_not_a_crash(self) -> None:
        """`cmd_run` returns 2 on a pause, so a wrapper can tell a pause from
        a failure, and prints resume instructions."""
        import inspect

        from experiments.industry_validation import cmd_run

        source = inspect.getsource(cmd_run)
        self.assertIn("AllProvidersFailed", source)
        self.assertIn("CAMPAIGN PAUSED", source)
        self.assertIn("--run-id", source)
        self.assertIn("return 2", source)


class TestPairedInputs(unittest.TestCase):
    def test_one_original_trace_serves_every_method(self) -> None:
        """`_recover_one` receives the trace; there is no code path that could
        generate a second original for a second method."""
        import inspect

        from experiments.industry_validation import _recover_one, run_placement

        signature = inspect.signature(_recover_one)
        self.assertIn("original", signature.parameters)
        self.assertNotIn("run_pipeline", inspect.getsource(_recover_one),
                         "_recover_one must not run a pipeline of its own")
        body = inspect.getsource(run_placement)
        self.assertEqual(body.count("run_pipeline("), 1,
                         "the original is produced exactly once per placement")

    def test_the_detector_verdict_is_computed_once_per_placement(self) -> None:
        import inspect

        from experiments.industry_validation import run_placement

        body = inspect.getsource(run_placement)
        self.assertEqual(body.count("ground_truth("), 1)
        self.assertIn("flagged", body)


class TestNoSecretsInArtifacts(unittest.TestCase):
    def test_the_runner_never_writes_a_key(self) -> None:
        import inspect

        from experiments import industry_validation as mod

        source = inspect.getsource(mod)
        for forbidden in ("GEMINI_API_KEY", "NVIDIA_API_KEY",
                          "os.environ[", "api_key"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden!r} appears in the runner")

    def test_the_provider_state_dump_has_no_key_material(self) -> None:
        from src.eval.routing import FallbackRouter

        router = FallbackRouter(clients={"gemini": object()}, route={})
        text = repr(router.budget_state()).lower()
        for forbidden in ("key", "secret", "nvapi", "aiza", "token="):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
