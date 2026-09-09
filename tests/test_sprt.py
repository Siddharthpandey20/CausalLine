"""
Phase 10 exit test: the SPRT stops early, and stops earlier the clearer the
answer is.

The test that matters counts **calls**, not correctness. An SPRT that reached
the right verdict after checking every candidate would pass a correctness test
and would have bought nothing: the whole point is that a decisive answer costs
fewer counterfactuals than an indecisive one. So the assertions are on
`checks_used`, and the check function counts its own invocations.

Also asserted: the empirical error rates stay inside the alpha/beta the
thresholds were built from. That is Wald's guarantee, and it is the reason this
module does not invent a stopping rule of its own.

    python -m unittest tests.test_sprt -v
"""

import random
import unittest

from src.recovery.sprt_investigate import (
    SPRTConfig,
    SPRTState,
    config_for,
    investigate,
)


class _CountingCheck:
    """A check function that records how many times it was actually called."""

    def __init__(self, true_f: float, seed: int) -> None:
        self.rng = random.Random(seed)
        self.calls = 0
        self.true_f = true_f

    def __call__(self, _candidate) -> bool:
        self.calls += 1
        return self.rng.random() < self.true_f


def _mean_checks(true_f: float, config: SPRTConfig, trials: int = 300) -> float:
    total = 0
    for trial in range(trials):
        check = _CountingCheck(true_f, seed=7000 + trial)
        result = investigate(list(range(500)), check, config)
        assert check.calls == result.checks_used, "lazy evaluation broke"
        total += result.checks_used
    return total / trials


def _decision_rate(
    true_f: float, config: SPRTConfig, decision: str, trials: int = 400
) -> float:
    hits = 0
    for trial in range(trials):
        result = investigate(
            list(range(500)), _CountingCheck(true_f, seed=9000 + trial), config
        )
        hits += result.decision == decision
    return hits / trials


class TestConfigValidation(unittest.TestCase):
    def test_hypotheses_must_be_separated(self) -> None:
        with self.assertRaises(ValueError):
            SPRTConfig(f_star=0.5, f_star_high=0.5)
        with self.assertRaises(ValueError):
            SPRTConfig(f_star=0.6, f_star_high=0.4)

    def test_endpoints_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SPRTConfig(f_star=0.0, f_star_high=0.5)
        with self.assertRaises(ValueError):
            SPRTConfig(f_star=0.5, f_star_high=1.0)

    def test_walds_thresholds(self) -> None:
        config = SPRTConfig(alpha=0.1, beta=0.1)
        self.assertAlmostEqual(config.abort_threshold, 0.9 / 0.1)
        self.assertAlmostEqual(config.proceed_threshold, 0.1 / 0.9)

    def test_config_for_derives_hypotheses_from_the_cost_model(self) -> None:
        # f* = 1 - A/N = 1 - 200/1000 = 0.8, clamped to 0.8, high = 0.95.
        config = config_for(analysis_tokens=200, restart_tokens=1000, margin=0.15)
        self.assertAlmostEqual(config.f_star, 0.8)
        self.assertAlmostEqual(config.f_star_high, 0.95)

    def test_config_for_clamps_a_negative_break_even(self) -> None:
        """A > N gives f* < 0, which has no log-likelihood. Clamped, not crashed."""
        config = config_for(analysis_tokens=1500, restart_tokens=1000)
        self.assertGreater(config.f_star, 0.0)
        self.assertLess(config.f_star, config.f_star_high)


class TestStopsEarlyWhenTheAnswerIsClear(unittest.TestCase):
    """The headline claim of Phase 10, measured in calls."""

    def setUp(self) -> None:
        self.config = SPRTConfig(f_star=0.3, f_star_high=0.7)

    def test_call_counts_form_a_peak_at_the_ambiguous_middle(self) -> None:
        clearly_clean = _mean_checks(0.02, self.config)
        clearly_dirty = _mean_checks(0.98, self.config)
        ambiguous = _mean_checks(0.50, self.config)

        self.assertLess(
            clearly_clean, ambiguous,
            "an obviously-clean region must cost fewer checks than an "
            "ambiguous one",
        )
        self.assertLess(
            clearly_dirty, ambiguous,
            "an obviously-contaminated region must cost fewer checks than an "
            "ambiguous one",
        )
        # And the saving is large, not marginal: this is the number quoted.
        self.assertLess(clearly_clean, ambiguous * 0.6)
        self.assertLess(clearly_dirty, ambiguous * 0.6)

    def test_it_never_checks_every_candidate_when_the_answer_is_clear(self) -> None:
        for true_f in (0.0, 1.0):
            with self.subTest(true_f=true_f):
                check = _CountingCheck(true_f, seed=11)
                result = investigate(list(range(200)), check, self.config)
                self.assertLess(result.checks_used, 10)
                self.assertEqual(check.calls, result.checks_used)
                self.assertGreater(result.saving, 0.9)
                self.assertFalse(result.exhausted)

    def test_monotone_in_distance_from_the_threshold(self) -> None:
        """Further from the indifference band means fewer checks."""
        near = _mean_checks(0.45, self.config)
        far = _mean_checks(0.05, self.config)
        further = _mean_checks(0.0, self.config)
        self.assertLess(far, near)
        self.assertLessEqual(further, far)


class TestDecisionsAndErrorRates(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SPRTConfig(f_star=0.3, f_star_high=0.7, alpha=0.1, beta=0.1)

    def test_clean_regions_proceed_and_dirty_ones_abort(self) -> None:
        self.assertEqual(
            investigate(range(200), _CountingCheck(0.0, 1), self.config).decision,
            "proceed_selective",
        )
        self.assertEqual(
            investigate(range(200), _CountingCheck(1.0, 1), self.config).decision,
            "abort_restart",
        )

    def test_alpha_holds_under_h0(self) -> None:
        """Wrongly aborting a cheap recovery, at a true f well inside H0."""
        wrong_aborts = _decision_rate(0.15, self.config, "abort_restart")
        self.assertLessEqual(wrong_aborts, self.config.alpha)

    def test_beta_holds_under_h1(self) -> None:
        """Wrongly continuing a hopeless investigation, at a true f in H1."""
        wrong_continues = _decision_rate(0.85, self.config, "proceed_selective")
        self.assertLessEqual(wrong_continues, self.config.beta)


class TestExhaustionFallback(unittest.TestCase):
    def test_running_out_of_candidates_falls_back_to_the_plain_comparison(self) -> None:
        """Between the hypotheses the test may not decide. The fallback is the
        ordinary non-sequential rule, so SPRT is never worse than checking
        everything."""
        config = SPRTConfig(f_star=0.4, f_star_high=0.6, alpha=0.01, beta=0.01)
        result = investigate([0, 1], lambda _c: True, config)
        self.assertTrue(result.exhausted)
        self.assertEqual(result.checks_used, 2)
        self.assertEqual(result.decision, "abort_restart")  # observed f=1.0 >= 0.4

        result = investigate([0, 1], lambda _c: False, config)
        self.assertTrue(result.exhausted)
        self.assertEqual(result.decision, "proceed_selective")

    def test_max_checks_caps_the_budget(self) -> None:
        check = _CountingCheck(0.5, seed=3)
        result = investigate(
            list(range(500)),
            check,
            SPRTConfig(f_star=0.45, f_star_high=0.55),
            max_checks=4,
        )
        self.assertLessEqual(check.calls, 4)
        self.assertLessEqual(result.checks_used, 4)

    def test_no_candidates_at_all_proceeds(self) -> None:
        result = investigate([], lambda _c: True)
        self.assertEqual(result.checks_used, 0)
        self.assertEqual(result.decision, "proceed_selective")


class TestLogSpaceArithmetic(unittest.TestCase):
    def test_long_runs_do_not_underflow(self) -> None:
        """Lambda for 400 clean observations underflows a float; log(Lambda)
        does not. An underflow would silently read as 'proceed'."""
        config = SPRTConfig(f_star=0.3, f_star_high=0.7)
        state = SPRTState(config=config)
        for _ in range(400):
            state.observe(False)
        self.assertTrue(state.log_lr < 0)
        self.assertTrue(state.log_lr > float("-inf"))

    def test_the_ratio_moves_the_right_way(self) -> None:
        config = SPRTConfig(f_star=0.3, f_star_high=0.7)
        self.assertGreater(config.log_likelihood_step(True), 0)
        self.assertLess(config.log_likelihood_step(False), 0)


if __name__ == "__main__":
    unittest.main()
