"""The environmental simulator and the candidate gates, offline.

The simulator's job is to be *hard*. A simulator that quietly fails to express
the adversarial condition produces a reassuring result for no reason, so the
properties that make each case adversarial are asserted here rather than
assumed.

    python -m unittest tests.test_gate1_sim -v
"""

import tempfile
import unittest
from pathlib import Path

from src.eval.gate1_probes import (
    a_hat,
    evaluate,
    gate_always_investigate,
    gate_structural,
    make_bottleneck_gate,
    make_sound_bottleneck_gate,
)
from src.eval.gate1_sim import ProbeOracle, build, full_investigation


class TestGroundTruthStaysOutsideTheTrace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_planted_relation_is_not_written_to_the_trace(self) -> None:
        """A gate reading influence off the trace would be reading the answer."""
        w = build("t", "hub", "one", 6, Path(self.tmp.name))
        self.assertTrue(w.truth.influences)
        self.assertEqual(w.trace.influence, [],
                         "influence edges leaked into the trace")
        self.assertEqual(w.trace.checks, [],
                         "check records leaked into the trace")

    def test_a_gate_learns_only_through_the_charged_oracle(self) -> None:
        w = build("t", "hub", "one", 6, Path(self.tmp.name))
        oracle = ProbeOracle(w)
        self.assertEqual(oracle.tokens, 0)
        make_bottleneck_gate(3)(w, oracle)
        self.assertGreater(oracle.tokens, 0, "the gate decided without paying")


class TestTheTopologiesDifferStructurally(unittest.TestCase):
    """If `f_structural` were 1.00 everywhere the experiment would be vacuous;
    that is what a single shared ingest event produced in the first draft."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_hub_saturates_the_structural_bound(self) -> None:
        w = build("h", "hub", "none", 8, Path(self.tmp.name))
        self.assertGreaterEqual(w.f_structural, 0.99)

    def test_a_flat_fanout_does_not(self) -> None:
        w = build("f", "fanout", "none", 8, Path(self.tmp.name))
        self.assertLess(w.f_structural, 0.4)

    def test_more_hubs_means_a_smaller_share_each(self) -> None:
        one = build("m1", "multihub", "none", 12, Path(self.tmp.name), hubs=1)
        three = build("m3", "multihub", "none", 12, Path(self.tmp.name), hubs=3)
        self.assertLess(three.f_structural, one.f_structural)


class TestTheAdversarialPatternsAreAdversarial(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_redundancy_makes_real_influence_invisible_to_every_probe(self) -> None:
        """docs/03 #16: the counterfactual comes back clean because a second
        source carries the same material, not because nothing was used."""
        w = build("r", "hub", "redundant", 8, Path(self.tmp.name))
        self.assertTrue(w.truth.influences)
        self.assertEqual(w.truth.detectable, set())
        oracle = ProbeOracle(w)
        for source, event in w.truth.influences:
            with self.subTest(pair=(source, event)):
                self.assertFalse(oracle.probe(source, event))

    def test_semantic_influence_leaves_no_span(self) -> None:
        """The free text-level probe is blind to it by construction."""
        w = build("s", "hub", "semantic_one", 8, Path(self.tmp.name))
        self.assertTrue(w.truth.influences)
        self.assertEqual(w.truth.carries_span, set())

    def test_the_blocked_pattern_leaves_nothing_soundly_probeable(self) -> None:
        """H2's predicted failure: no removable candidate exists."""
        w = build("b", "hub", "none_blocked", 8, Path(self.tmp.name))
        probeable = [
            (s, e.id) for e in w.trace.events for s in e.exposures
            if w.is_removable(s, e.id)
        ]
        self.assertEqual(probeable, [])

    def test_deep_hidden_keeps_the_bottleneck_honest(self) -> None:
        """The head is removable and genuinely clean; the influence is further
        down behind a pair no probe can see."""
        w = build("d", "hub", "deep_hidden", 8, Path(self.tmp.name))
        self.assertTrue(w.truth.influences)
        self.assertTrue(any(not w.is_removable(s, e)
                            for s, e in w.truth.influences))


class TestTheGatesBehaveAsClaimed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _one(self, gate, name, topology, pattern, branches, hubs=1):
        w = build(name, topology, pattern, branches, Path(self.tmp.name),
                  hubs=hubs)
        full = {w.name: full_investigation(w)}
        return evaluate(name, gate, [w], full)[0], w

    def test_the_structural_gate_still_fails_the_hub_case(self) -> None:
        """The falsification this whole phase starts from, reproduced offline."""
        out, w = self._one(gate_structural, "hub", "hub", "none", 8)
        self.assertEqual(out.verdict, "FALSE RESTART")
        self.assertGreaterEqual(w.f_structural, 0.99)
        self.assertEqual(w.f_true, 0.0)

    def test_one_probe_fixes_it(self) -> None:
        out, _w = self._one(make_bottleneck_gate(3), "hub", "hub", "none", 8)
        self.assertIn("correct", out.verdict)
        self.assertLessEqual(out.probe_calls, 2)

    def test_the_probing_gate_still_restarts_when_contamination_is_total(self) -> None:
        """It must not degenerate into always-investigate."""
        out, _w = self._one(make_bottleneck_gate(3), "all", "hub", "all", 8)
        self.assertFalse(out.decision)
        self.assertIn("correct", out.verdict)

    def test_the_soundness_guard_refuses_an_unsound_clean_verdict(self) -> None:
        """H1 believes it and preserves contaminated work; H2 does not."""
        unguarded, _ = self._one(make_bottleneck_gate(3), "r1", "hub",
                                 "redundant", 16)
        guarded, _ = self._one(make_sound_bottleneck_gate(3), "r2", "hub",
                               "redundant", 16)
        self.assertTrue(unguarded.decision)
        self.assertGreater(unguarded.unsafe, 0)
        self.assertFalse(guarded.decision)
        self.assertEqual(guarded.unsafe, 0)

    def test_the_soundness_guard_costs_a_false_restart_when_nothing_is_probeable(self) -> None:
        """The price of the guard, and the case H2 was NOT designed against."""
        out, _w = self._one(make_sound_bottleneck_gate(3), "b", "hub",
                            "none_blocked", 8)
        self.assertEqual(out.verdict, "FALSE RESTART")

    def test_no_gate_can_exceed_the_investigations_own_unsafe_count(self) -> None:
        """A gate only chooses WHETHER to investigate; the blind spots belong
        to the investigation. This is the safety argument, asserted."""
        for pattern in ("redundant", "deep_hidden", "deep"):
            with self.subTest(pattern=pattern):
                w = build(f"u-{pattern}", "hub", pattern, 8,
                          Path(self.tmp.name))
                full = {w.name: full_investigation(w)}
                ceiling = evaluate("b1", gate_always_investigate, [w], full)[0]
                for name, gate in (("H1", make_bottleneck_gate(3)),
                                   ("H2", make_sound_bottleneck_gate(3))):
                    got = evaluate(name, gate, [w], full)[0]
                    self.assertLessEqual(got.unsafe, ceiling.unsafe)


class TestTheCostEstimateIsAnUpperBound(unittest.TestCase):
    def test_a_hat_never_understates_the_full_investigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for topology, pattern in (("hub", "all"), ("chain", "all"),
                                      ("fanout", "one"), ("fanin", "one")):
                with self.subTest(topology=topology):
                    w = build(f"{topology}-{pattern}", topology, pattern, 8,
                              Path(tmp))
                    spent, _ = full_investigation(w)
                    self.assertGreaterEqual(a_hat(w), spent)



class TestH2IsFalsifiedByComparatorBlindness(unittest.TestCase):
    """The smallest counterexample that breaks H2, preserved.

    H2's safety argument is that `removability.check()` guards against
    believing an unsound clean verdict. It does -- for the failure mode it
    checks. `removability` answers "does redacting this source remove its
    information from the prompt?" It does NOT answer "would the comparator
    notice if the output changed?", and those are different questions: D-026
    removed text comparison deliberately, so a decision signature can miss a
    real semantic change.

    Five events. The probed pair is removable, so the guard passes; it genuinely
    influences, so the verdict is wrong; the comparator cannot see it, so the
    probe returns clean. H2 investigates a workflow that is 3/5 contaminated and
    for which restart was the cheaper action.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = build("cb", "hub", "comparator_blind", 2, Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_counterexample_is_five_events(self) -> None:
        self.assertEqual(len(self.w.trace.events), 5)

    def test_the_removability_guard_passes_on_the_probed_pair(self) -> None:
        """If this ever starts failing, the counterexample has stopped being
        one -- the guard would be catching it."""
        probed = [(s, e) for s, e in self.w.truth.influences]
        self.assertTrue(probed)
        for source, event in probed:
            with self.subTest(pair=(source, event)):
                self.assertTrue(self.w.is_removable(source, event))

    def test_the_probe_returns_clean_while_influence_is_real(self) -> None:
        oracle = ProbeOracle(self.w)
        for source, event in self.w.truth.influences:
            with self.subTest(pair=(source, event)):
                self.assertTrue(self.w.truth.influenced(source, event))
                self.assertFalse(oracle.probe(source, event))

    def test_h2_makes_a_false_recovery_here(self) -> None:
        full = {self.w.name: full_investigation(self.w)}
        out = evaluate("h2", make_sound_bottleneck_gate(3), [self.w], full)[0]
        self.assertEqual(out.verdict, "FALSE RECOVERY")
        self.assertGreater(out.unsafe, 0)

    def test_the_workflow_is_majority_contaminated(self) -> None:
        self.assertEqual(self.w.f_true, 1.0)
        self.assertEqual(len(self.w.true_region()), 3)

if __name__ == "__main__":
    unittest.main()
