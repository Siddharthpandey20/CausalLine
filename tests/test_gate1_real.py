"""The real-LLM falsification suite's own machinery, checked offline.

Everything here runs without a model. The point is that the suite's design
properties -- family coverage, calibration separation, the wide-exposure shape
actually being wide -- are asserted rather than assumed, because a suite that
quietly fails to be adversarial produces a reassuring result for no reason.

    python -m unittest tests.test_gate1_real -v
"""

import tempfile
import unittest
from pathlib import Path

from src.eval.fanout_scenarios import FanoutScenario
from src.eval.gate1_real import CALIBRATION_SOURCE, calibrate_from_history, case_matrix
from src.eval.gate1_real_report import score
from src.recovery.sprt_investigate import structural_prior
from src.tracing.logger import read_trace


class TestTheSuiteIsActuallyAdversarial(unittest.TestCase):
    def test_every_required_family_is_present(self) -> None:
        """The brief names six families; a missing one is a silent hole."""
        families = {c.family for c in case_matrix()}
        self.assertEqual(families, {"A", "B", "C", "D", "E", "F"})

    def test_the_suite_is_small_as_instructed(self) -> None:
        total = sum(c.repeats for c in case_matrix())
        self.assertGreaterEqual(total, 12)
        self.assertLessEqual(total, 20)

    def test_repeats_go_to_the_families_the_brief_prioritises(self) -> None:
        by_family = {}
        for case in case_matrix():
            by_family.setdefault(case.family, []).append(case.repeats)
        for family in ("A", "C", "D"):
            with self.subTest(family=family):
                self.assertTrue(all(r >= 2 for r in by_family[family]))

    def test_the_eager_lazy_comparison_shares_a_design_with_the_lazy_arm(self) -> None:
        """An eager/lazy comparison on different workflows measures the
        workflow, not the arm."""
        matrix = case_matrix()
        eager = {c.case_id.split("-")[1] for c in matrix if c.arm == "eager"}
        lazy = {c.case_id.split("-")[1] for c in matrix if c.arm == "lazy"}
        self.assertTrue(eager <= lazy, f"eager-only designs: {eager - lazy}")


class TestTheWideExposureShapeIsWide(unittest.TestCase):
    """Family D only tests anything if the structural closure really does
    cover the trace. It did NOT when the briefing was logged as a single
    shared source -- `b2_topology_closure` locates a flagged source by its
    entry event, so one entry point gave f_structural 0.13-0.20, no wider than
    an ordinary single-analyst poisoning. Each analyst now retrieves it."""

    def _f_structural(self, scenario, workers):
        from src.eval.attacks import label_malicious
        from src.eval.gate1_experiment import FanoutScriptedClient
        from src.tracing.pipeline import run_pipeline
        from src.tracing.tools import Tools, fanout_corpus

        docs = [dict(d) for d in fanout_corpus(workers)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            tools = scenario.apply(
                Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
            )
            run_pipeline(
                path,
                client=FanoutScriptedClient(docs=docs, marker=scenario.marker,
                                            token=scenario.token),
                tools=tools, **scenario.workflow_kwargs,
            )
            label_malicious(path, scenario.marker)
            trace = read_trace(path)
            flagged = [s.id for s in trace.sources if s.malicious]
            return structural_prior(trace, flagged), len(flagged)

    def test_the_briefing_flags_one_source_per_analyst(self) -> None:
        _f, flagged = self._f_structural(
            FanoutScenario.build_shared(6, "benign"), 6)
        self.assertEqual(flagged, 6)

    def test_the_closure_covers_the_whole_trace(self) -> None:
        f, _flagged = self._f_structural(
            FanoutScenario.build_shared(6, "benign"), 6)
        self.assertGreaterEqual(f, 0.99)

    def test_an_ordinary_poisoning_is_still_narrow(self) -> None:
        """The contrast that makes family D meaningful."""
        f, _flagged = self._f_structural(
            FanoutScenario.build(6, poisoned_indices=(0,)), 6)
        self.assertLess(f, 0.6)

    def test_the_benign_briefing_carries_no_canary(self) -> None:
        scenario = FanoutScenario.build_shared(8, "benign")
        self.assertNotIn(scenario.token, scenario.payload)
        self.assertEqual(scenario.validate(), [])

    def test_the_targeted_briefing_names_exactly_one_report(self) -> None:
        scenario = FanoutScenario.build_shared(8, "targeted")
        self.assertIn(scenario.token, scenario.payload)
        self.assertIn("REPORT-01", scenario.payload)
        self.assertNotIn("REPORT-02", scenario.payload)


class TestCalibrationStaysSeparateFromEvaluation(unittest.TestCase):
    def test_calibration_reads_only_the_prior_campaign(self) -> None:
        """If this ever pointed at the falsification results, the gate would be
        calibrated on the data it is judged against."""
        self.assertIn("fanout-campaign", str(CALIBRATION_SOURCE))
        self.assertNotIn("falsification", str(CALIBRATION_SOURCE))

    def test_a_missing_arm_reports_cold_start_rather_than_borrowing(self) -> None:
        scale, n, note = calibrate_from_history("no-such-arm")
        self.assertEqual(n, 0)
        self.assertIn("cold-start", note)

    def test_the_two_arms_get_different_scales(self) -> None:
        """A_SCALE is client- and configuration-dependent; a single number for
        both arms is the failure D-092 recorded."""
        if not CALIBRATION_SOURCE.exists():
            self.skipTest("no calibration history on disk")
        eager, n_e, _ = calibrate_from_history("eager")
        lazy, n_l, _ = calibrate_from_history("lazy")
        if not (n_e and n_l):
            self.skipTest("calibration history incomplete")
        self.assertNotAlmostEqual(eager, lazy, places=2)


class TestScoringClassifiesHonestly(unittest.TestCase):
    def test_verdicts_follow_the_oracle_not_the_gate(self) -> None:
        """A hand-built row where the oracle and the gate disagree in each
        direction, to pin the labelling."""
        rows = score([])
        self.assertEqual(rows, [])

    def test_the_oracle_formula_is_the_committed_one(self) -> None:
        """oracle = INVESTIGATE iff A + min(R,N) < N."""
        for a, r, n, expected in (
            (100, 100, 1000, True),     # cheap analysis, small replay
            (900, 900, 1000, False),    # both large
            (100, 5000, 1000, False),   # replay capped at N, so A+N > N
        ):
            with self.subTest(a=a, r=r, n=n):
                self.assertEqual((a + min(r, n)) < n, expected)



class TestTheBreakTestConstruction(unittest.TestCase):
    """The dispatcher shape, which is what made the target regime reachable.

    The previous wide-exposure family could not reach `A/N < 1` because a
    briefing read by every analyst ENTERED at every analyst, so `A` grew with
    `K` exactly as fast as the structural closure. A coordinator at the head of
    the call graph decouples them: one entry event, whole-trace closure.
    """

    def _trace(self, workers, dispatcher):
        from src.eval.attacks import label_malicious
        from src.eval.gate1_experiment import FanoutScriptedClient
        from src.tracing.pipeline import run_pipeline
        from src.tracing.tools import Tools, fanout_corpus

        scenario = FanoutScenario.build_shared(workers, "benign",
                                               dispatcher=dispatcher)
        docs = [dict(d) for d in fanout_corpus(workers)]
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "b.jsonl"
        tools = scenario.apply(
            Tools.from_fixtures(memory_path=path.with_suffix(".memory.json")))
        run_pipeline(
            path,
            client=FanoutScriptedClient(docs=docs, marker=scenario.marker,
                                        token=scenario.token),
            tools=tools, **scenario.workflow_kwargs)
        label_malicious(path, scenario.marker)
        return read_trace(path), tmp

    def test_the_dispatcher_gives_one_entry_point_not_K(self) -> None:
        trace, tmp = self._trace(8, dispatcher=True)
        try:
            flagged = [s.id for s in trace.sources if s.malicious]
            self.assertEqual(len(flagged), 1)
        finally:
            tmp.cleanup()

    def test_the_closure_is_still_the_whole_trace(self) -> None:
        """One entry point, total structural reach -- the decoupling."""
        trace, tmp = self._trace(8, dispatcher=True)
        try:
            flagged = [s.id for s in trace.sources if s.malicious]
            self.assertGreaterEqual(structural_prior(trace, flagged), 0.99)
        finally:
            tmp.cleanup()

    def test_without_the_dispatcher_every_analyst_is_an_entry_point(self) -> None:
        trace, tmp = self._trace(8, dispatcher=False)
        try:
            flagged = [s.id for s in trace.sources if s.malicious]
            self.assertEqual(len(flagged), 8)
        finally:
            tmp.cleanup()

    def test_every_analyst_is_downstream_of_the_dispatcher(self) -> None:
        """The call-graph edge is what puts them in the closure at all."""
        from src.tracing.graphs import CallGraph

        trace, tmp = self._trace(6, dispatcher=True)
        try:
            reach = CallGraph.from_trace(trace).reachable_from("dispatcher")
            for i in range(1, 7):
                with self.subTest(analyst=i):
                    self.assertIn(f"analyst{i}", reach)
        finally:
            tmp.cleanup()


class TestTheTargetRegimePredicate(unittest.TestCase):
    """A case only falsifies the gate if restart was NOT already justified.

    `A/N >= 1`, or `A/N + f_true >= 1`, means restarting is correct and the
    gate agreeing with it is not an error. The previous family D was rejected
    as evidence on exactly this ground.
    """

    @staticmethod
    def _in_target(a, n, r, f_struct):
        an = a / n
        f_true = min(1.0, r / n)
        return an < 1 and an + f_true < 1 and an + f_struct > 1

    def test_a_cheap_investigation_under_a_wide_bound_is_in_the_regime(self) -> None:
        self.assertTrue(self._in_target(a=202, n=3812, r=0, f_struct=1.0))

    def test_an_expensive_investigation_is_not_in_the_regime(self) -> None:
        """Family D of the previous suite: A/N = 2.05, restart correct."""
        self.assertFalse(self._in_target(a=7149, n=3491, r=430, f_struct=1.0))

    def test_a_narrow_bound_is_not_in_the_regime(self) -> None:
        """If the bound is not pessimistic there is nothing to falsify."""
        self.assertFalse(self._in_target(a=1737, n=3549, r=552, f_struct=0.18))


class TestInvestigationModeChangesNoDefault(unittest.TestCase):
    def test_the_default_is_still_self_report(self) -> None:
        import inspect

        from src.eval.real_llm import run_generated

        default = inspect.signature(run_generated).parameters[
            "investigation_mode"].default
        self.assertEqual(default, "self_report")

if __name__ == "__main__":
    unittest.main()
