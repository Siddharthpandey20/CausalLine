"""
Phase 9 exit test: the cost model, and the sunk-cost regression.

The important one is `TestSunkCostRegression`. `A + f*N < N` was being used as
the rule for a decision made *after* A had been spent, which double-counts a
sunk cost and biases every such decision towards finishing the investigation.
It was a real error in this project's reasoning, not a hypothetical, so it gets
a regression test rather than only a fix.

    python -m unittest tests.test_economics -v
"""

import math
import unittest

from src.eval.economics import (
    Deployment,
    SunkCostError,
    attack_rate_threshold,
    breakeven_frontier,
    detection_latency,
    empirical_crossing,
    ex_ante_decision,
    ex_post_decision,
    f_star_ex_ante,
    latency_distribution,
    per_run_amortized_cost_causalline,
    per_run_amortized_cost_restart_only,
    worth_deploying,
    SweepPoint,
    LatencyObservation,
)
from src.eval.metrics import CostWeights, total_cost


class TestTotalCost(unittest.TestCase):
    def test_default_weights_reproduce_the_plain_token_total(self) -> None:
        cost = total_cost(analysis_tokens=100, replay_tokens=250, storage_bytes=9999)
        self.assertEqual(cost.total, 350)
        self.assertEqual(cost.storage_cost, 0.0)

    def test_storage_and_risk_terms_are_added_at_their_weights(self) -> None:
        cost = total_cost(
            analysis_tokens=100,
            replay_tokens=250,
            storage_bytes=1000,
            lost_legitimate_work=40,
            residual_risk=0.25,
            weights=CostWeights(storage_weight=0.5, risk_lambda=1000.0),
        )
        # 100 + 250 + 40 + 1000*0.5 + 1000*0.25
        self.assertEqual(cost.total, 100 + 250 + 40 + 500 + 250)
        self.assertEqual(cost.token_cost, 390)


class TestExAnte(unittest.TestCase):
    def test_f_star_is_one_minus_a_over_n(self) -> None:
        self.assertAlmostEqual(f_star_ex_ante(200, 1000), 0.8)
        self.assertAlmostEqual(f_star_ex_ante(1000, 1000), 0.0)

    def test_f_star_goes_negative_when_analysis_costs_more_than_restart(self) -> None:
        """No contamination fraction makes an over-priced investigation worth
        starting, and the formula should say so rather than clamp."""
        self.assertLess(f_star_ex_ante(1500, 1000), 0.0)

    def test_decision_uses_a_prior_not_a_measurement(self) -> None:
        cheap = ex_ante_decision(analysis_tokens=100, restart_tokens=1000, f_prior=0.2)
        self.assertTrue(cheap.investigate)
        wide = ex_ante_decision(analysis_tokens=100, restart_tokens=1000, f_prior=0.95)
        self.assertFalse(wide.investigate)


class TestSunkCostRegression(unittest.TestCase):
    """A is spent on both branches once the investigation has run."""

    def test_ex_post_refuses_the_analysis_cost_by_any_name(self) -> None:
        for name in (
            "analysis_tokens",
            "A",
            "analysis",
            "investigation_cost",
            "analysis_cost",
        ):
            with self.subTest(kwarg=name):
                with self.assertRaises(SunkCostError):
                    ex_post_decision(0.5, 1000, **{name: 300})

    def test_ex_post_compares_fn_against_n_only(self) -> None:
        decision = ex_post_decision(0.5, 1000)
        self.assertTrue(decision.finish_selective)
        self.assertEqual(decision.remaining_selective_tokens, 500)

    def test_the_two_rules_disagree_and_ex_post_is_the_permissive_one(self) -> None:
        """The whole point. With A=400, N=1000, f=0.8:

            ex ante   400 + 800 = 1200 > 1000  -> would not have started
            ex post           800      < 1000  -> finish, A is gone either way

        A rule that re-added A here would restart and pay 1000 more instead of
        800, purely because of a number that is already spent.
        """
        A, N, f = 400, 1000, 0.8
        self.assertFalse(ex_ante_decision(A, N, f_prior=f).investigate)
        self.assertTrue(ex_post_decision(f, N).finish_selective)
        # And the wrong-but-tempting arithmetic is strictly worse here.
        wrong = A + f * N
        right = f * N
        self.assertGreater(wrong, N)
        self.assertLess(right, N)

    def test_ex_post_restarts_only_when_selective_replays_everything(self) -> None:
        self.assertFalse(ex_post_decision(1.0, 1000).finish_selective)
        self.assertTrue(ex_post_decision(0.999, 1000).finish_selective)

    def test_ex_post_rejects_an_impossible_fraction(self) -> None:
        with self.assertRaises(ValueError):
            ex_post_decision(1.4, 1000)


class TestAmortizedComparison(unittest.TestCase):
    def setUp(self) -> None:
        self.deployment = Deployment(
            restart_tokens=1000,
            analysis_tokens=100,
            f=0.3,
            storage_bytes_per_run=2000,
            weights=CostWeights(storage_weight=0.01),  # 20 tokens/run
            label="test",
        )

    def test_storage_is_charged_on_every_run_and_recovery_only_on_attacked_ones(self) -> None:
        at_zero = per_run_amortized_cost_causalline(self.deployment, 0.0)
        self.assertEqual(at_zero, 20.0)
        self.assertEqual(per_run_amortized_cost_restart_only(self.deployment, 0.0), 0.0)

    def test_the_comparison_matches_the_divided_form(self) -> None:
        for rate in (0.0, 0.001, 0.01, 0.1, 0.5, 1.0):
            with self.subTest(rate=rate):
                direct = per_run_amortized_cost_causalline(
                    self.deployment, rate
                ) < per_run_amortized_cost_restart_only(self.deployment, rate)
                self.assertEqual(direct, worth_deploying(self.deployment, rate))

    def test_threshold_is_where_the_verdict_flips(self) -> None:
        threshold = attack_rate_threshold(self.deployment)
        # S / (N(1-f) - A) = 20 / (700 - 100) = 0.0333...
        self.assertAlmostEqual(threshold, 20 / 600)
        self.assertFalse(worth_deploying(self.deployment, threshold * 0.99))
        self.assertTrue(worth_deploying(self.deployment, threshold * 1.01))

    def test_no_threshold_exists_when_the_incident_itself_loses(self) -> None:
        losing = Deployment(
            restart_tokens=1000,
            analysis_tokens=900,
            f=0.5,  # A + fN = 1400 > 1000
            storage_bytes_per_run=2000,
            weights=CostWeights(storage_weight=0.01),
        )
        self.assertEqual(attack_rate_threshold(losing), math.inf)
        for rate in (0.001, 0.5, 1.0):
            self.assertFalse(worth_deploying(losing, rate))

    def test_free_storage_needs_no_attack_rate_to_pay_off(self) -> None:
        free = Deployment(restart_tokens=1000, analysis_tokens=100, f=0.3)
        self.assertEqual(attack_rate_threshold(free), 0.0)
        self.assertTrue(worth_deploying(free, 0.0001))

    def test_with_no_attacks_at_all_nothing_is_worth_deploying(self) -> None:
        """Not even free storage: with no incidents there is no saving, and
        the two options merely tie. The helper must agree with the costs it
        summarises."""
        free = Deployment(restart_tokens=1000, analysis_tokens=100, f=0.3)
        self.assertFalse(worth_deploying(free, 0.0))
        self.assertFalse(worth_deploying(self.deployment, 0.0))


class TestFrontier(unittest.TestCase):
    def test_thresholds_rise_with_both_analysis_cost_and_contamination(self) -> None:
        cells = breakeven_frontier(
            restart_tokens=1000,
            storage_per_run=20,
            analysis_ratios=(0.1, 0.3),
            fractions=(0.2, 0.6),
        )
        lookup = {(c.analysis_ratio, c.f): c.threshold for c in cells}
        self.assertLess(lookup[(0.1, 0.2)], lookup[(0.3, 0.2)])
        self.assertLess(lookup[(0.1, 0.2)], lookup[(0.1, 0.6)])


class TestCrossingReadsTheRightStory(unittest.TestCase):
    def _point(self, f: float, a: int) -> SweepPoint:
        return SweepPoint(
            scenario="A", variant="influencing", seeds=1,
            contaminated_events=1, total_events=10, f=f,
            analysis_tokens=a, restart_tokens=1000, counterfactual_tokens=a,
        )

    def test_a_over_n_is_reported_apart_from_contamination_width(self) -> None:
        points = [self._point(0.1, 1200), self._point(0.9, 1200)]
        crossing = empirical_crossing(points)
        self.assertEqual(crossing["selective_wins"], 0)
        self.assertEqual(crossing["analysis_exceeds_restart"], 2)

    def test_a_real_crossing_is_found_when_analysis_is_affordable(self) -> None:
        points = [self._point(f, 100) for f in (0.1, 0.5, 0.95)]
        crossing = empirical_crossing(points)
        self.assertEqual(crossing["analysis_exceeds_restart"], 0)
        self.assertEqual(crossing["highest_winning_f"], 0.5)
        self.assertEqual(crossing["lowest_losing_f"], 0.95)


class TestDetectionLatency(unittest.TestCase):
    def test_distribution_reports_the_mean_phase_12_consumes(self) -> None:
        observations = [
            LatencyObservation("A", "influencing", "oracle", 2, 10, 18),
            LatencyObservation("B", "influencing", "oracle", 4, 10, 18),
        ]
        stats = latency_distribution(observations)
        self.assertEqual(stats["n"], 2)
        self.assertAlmostEqual(stats["mean_turns_between_detections"], 7.0)
        self.assertEqual(stats["min"], 6)
        self.assertEqual(stats["max"], 8)

    def test_empty_distribution_is_not_a_zero_latency_claim(self) -> None:
        stats = latency_distribution([])
        self.assertEqual(stats["n"], 0)

    def test_a_detector_that_flagged_nothing_has_no_latency(self) -> None:
        class _Verdict:
            detector = "blind"
            detected_at = None

            def sources(self):
                return []

        class _Trace:
            events: list = []
            sources: list = []

        self.assertIsNone(detection_latency(_Trace(), _Verdict()))


if __name__ == "__main__":
    unittest.main()
