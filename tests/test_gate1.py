"""Gate 1's contract, and the properties the experiments actually established.

Each assertion here corresponds to a measured result in
`docs/gate1/02-experiments.md`. Where a property was NOT established, there is
no test claiming it.

    python -m unittest tests.test_gate1 -v
"""

import tempfile
import unittest
from pathlib import Path

from src.recovery.gate1 import (
    A_SCALE,
    DECISION_MARGIN,
    calibrate,
    decide,
    estimate_analysis_tokens,
)
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools, fanout_corpus


def _trace(workers=6, poisoned=1):
    """A real fan-out trace, built offline."""
    from src.eval.fanout_scenarios import FanoutScenario
    from src.eval.gate1_experiment import FanoutScriptedClient

    scenario = FanoutScenario.build(
        workers, "influencing", poisoned_indices=tuple(range(poisoned))
    )
    docs = [dict(d) for d in fanout_corpus(workers)]
    for i in scenario.poisoned:
        docs[i]["injected"] = scenario.payload
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "g.jsonl"
    tools = Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    tools = scenario.apply(tools)
    client = FanoutScriptedClient(docs=docs, marker=scenario.marker,
                                  token=scenario.token)
    run_pipeline(path, client=client, tools=tools, **scenario.workflow_kwargs)
    from src.eval.attacks import label_malicious

    label_malicious(path, scenario.marker)
    return read_trace(path), tmp


class TestTheDecisionItself(unittest.TestCase):
    def test_nothing_flagged_means_investigate_not_restart(self) -> None:
        """A gate with no evidence must not throw work away. Restarting is the
        expensive default, not the safe one -- safety comes from the walk
        treating unchecked pairs as contaminated, not from restarting."""
        trace, tmp = _trace()
        try:
            self.assertTrue(decide(trace, []).investigate)
        finally:
            tmp.cleanup()

    def test_a_widespread_region_is_told_to_restart(self) -> None:
        trace, tmp = _trace(workers=4, poisoned=4)
        try:
            flagged = [s.id for s in trace.sources if s.malicious]
            self.assertFalse(decide(trace, flagged).investigate)
        finally:
            tmp.cleanup()

    def test_a_localized_region_is_told_to_investigate(self) -> None:
        trace, tmp = _trace(workers=16, poisoned=1)
        try:
            flagged = [s.id for s in trace.sources if s.malicious]
            self.assertTrue(decide(trace, flagged).investigate)
        finally:
            tmp.cleanup()

    def test_the_margin_only_ever_moves_the_decision_toward_investigating(self) -> None:
        """DECISION_MARGIN corrects a measured one-sided bias: both estimators
        are upper bounds, and on 65 of 65 development cases the compound
        estimate over-stated the truth. A larger margin must therefore never
        turn an INVESTIGATE into a RESTART."""
        trace, tmp = _trace(workers=8, poisoned=3)
        try:
            flagged = [s.id for s in trace.sources if s.malicious]
            strict = decide(trace, flagged, margin=0.0)
            loose = decide(trace, flagged, margin=1.0)
            self.assertFalse(strict.investigate and not loose.investigate)
        finally:
            tmp.cleanup()


class TestTheCostEstimate(unittest.TestCase):
    def test_a_scale_is_applied_and_overridable(self) -> None:
        """A_SCALE is a property of the CLIENT, not of the algorithm: measured
        at 0.431 scripted, 0.547 real-lazy and 1.781 real-eager. Using the
        frozen scripted value on the real frontier scored 25%; per-deployment
        calibration took the same gate to 75%. So it has to be overridable, and
        an override has to actually change the estimate."""
        trace, tmp = _trace()
        try:
            flagged = [s.id for s in trace.sources if s.malicious]
            low, _ = estimate_analysis_tokens(trace, flagged, a_scale=0.1)
            high, _ = estimate_analysis_tokens(trace, flagged, a_scale=2.0)
            self.assertLess(low, high)
            default, _ = estimate_analysis_tokens(trace, flagged)
            scaled, _ = estimate_analysis_tokens(trace, flagged, a_scale=A_SCALE)
            self.assertAlmostEqual(default, scaled)
        finally:
            tmp.cleanup()

    def test_calibrate_recovers_a_known_ratio(self) -> None:
        trace, tmp = _trace()
        try:
            from src.recovery.gate1 import _raw_cost_estimate

            raw = _raw_cost_estimate(trace)
            self.assertGreater(raw, 0)
            self.assertAlmostEqual(calibrate([(trace, raw * 1.5)]), 1.5, places=5)
        finally:
            tmp.cleanup()

    def test_calibrate_falls_back_rather_than_dividing_by_nothing(self) -> None:
        self.assertEqual(calibrate([]), A_SCALE)


class TestGateOneAndGateTwoStaySeparate(unittest.TestCase):
    """The two gates ask different questions and A belongs in exactly one."""

    def test_gate_two_still_refuses_the_sunk_cost(self) -> None:
        from src.eval.economics import SunkCostError, ex_post_decision

        with self.assertRaises(SunkCostError):
            ex_post_decision(f=0.3, restart_tokens=1000, analysis_tokens=500)

    def test_gate_one_does_put_analysis_in_the_comparison(self) -> None:
        """Gate 1 is the one place A legitimately appears: it has not been
        spent yet, so it is avoidable."""
        trace, tmp = _trace()
        try:
            flagged = [s.id for s in trace.sources if s.malicious]
            decision = decide(trace, flagged)
            self.assertGreater(decision.a_estimate, 0)
            self.assertAlmostEqual(
                decision.total,
                decision.a_estimate / decision.restart_tokens
                + decision.f_structural,
                places=6,
            )
        finally:
            tmp.cleanup()

    def test_the_run_records_what_the_gate_decided(self) -> None:
        """A run that skipped its investigation must never be mistaken for one
        that investigated and found nothing."""
        from src.eval.real_llm import RealRunResult

        result = RealRunResult(
            test_id="t", design={}, execution_model="m", execution_model_id="m",
            generator_model="g", detector="oracle",
        )
        result.gate1 = {"investigate": False, "total": 1.9}
        self.assertEqual(result.to_dict()["gate1"]["investigate"], False)


if __name__ == "__main__":
    unittest.main()
