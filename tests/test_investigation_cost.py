"""
Phase 11 exit tests: calibration, group testing, budget fallback.

Two requirements from the phase spec are load-bearing and get their own tests:

  * calibration numbers must come from **actual scripted-run data**. The test
    for that changes the scripted agent's honesty and asserts the measured
    precision moves. A hardcoded table would pass every other test here and
    fail this one.
  * group testing must find the same set as exhaustive leave-one-out, in
    measurably fewer calls, with the reduction reported as a number.

    python -m unittest tests.test_investigation_cost -v
"""

import tempfile
import unittest
from pathlib import Path

# The Lasso fit in src/provenance/budget_attribution.py is the one piece of
# this project that needs numpy, and requirements.txt is explicit that the core
# must run on a bare interpreter -- "a research prototype that cannot run its
# own test suite offline is worse than one with an optional component". These
# tests were the exception to that: they failed with an ImportError rather than
# skipping, so the suite did not in fact pass without numpy installed. Skipping
# keeps the claim true and keeps the coverage wherever numpy is present, which
# includes the analysis-extra CI job.
try:  # `import` rather than `find_spec`: a finder may raise, and either way
    import numpy  # noqa: F401
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
NEEDS_NUMPY = "needs numpy (pip install -r requirements.txt); Lasso fallback"

from src.provenance.budget_attribution import (
    attribute,
    attribute_if_sparsity_failed,
)
from src.provenance.calibration import (
    ChannelStats,
    SelfReportCalibration,
    filter_candidates,
    measure,
)
from src.provenance.group_test import (
    GroupTestDiagnostics,
    group_test,
    leave_one_out,
    measure_on_scenario,
    redact_group,
    split_in_half,
)


class _OracleDecision:
    """A decision function with a known influential set. Counts its calls."""

    def __init__(self, influential) -> None:
        self.influential = set(influential)
        self.calls = 0

    def __call__(self, group) -> bool:
        self.calls += 1
        return bool(set(group) & self.influential)


# --- 11.1 calibration ---------------------------------------------------------


class TestCalibrationIsMeasuredNotHardcoded(unittest.TestCase):
    def test_changing_the_agents_honesty_changes_the_numbers(self) -> None:
        """The requirement, stated as a test.

        A more over-claiming self-reporter must produce lower measured
        precision. If the numbers were baked in, this could not pass.
        """
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            honest = measure(
                scenarios=("A",),
                variants=(True,),
                seeds=(20260906,),
                workdir=tmp / "honest",
                client_kwargs={"self_report_false_positive_rate": 0.0},
            )
            liar = measure(
                scenarios=("A",),
                variants=(True,),
                seeds=(20260906,),
                workdir=tmp / "liar",
                client_kwargs={"self_report_false_positive_rate": 0.9},
            )
        channel = "indirect_injection"
        self.assertIn(channel, honest.by_channel)
        self.assertIn(channel, liar.by_channel)
        self.assertGreater(
            honest.by_channel[channel].precision,
            liar.by_channel[channel].precision,
            "an agent that over-claims must measure as less precise; if these "
            "are equal the numbers are not coming from the run",
        )
        self.assertGreater(liar.by_channel[channel].false_positive, 0)

    def test_a_channel_is_reported_per_channel_not_pooled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            calibration = measure(
                scenarios=("A",),
                variants=(True,),
                seeds=(20260906,),
                workdir=Path(raw),
            )
        self.assertGreater(len(calibration.by_channel), 1)


class TestCalibrationAcceptanceRule(unittest.TestCase):
    def test_precision_below_threshold_is_not_accepted(self) -> None:
        calibration = SelfReportCalibration(
            by_channel={
                "web": ChannelStats("web", true_positive=8, false_positive=8),
            },
            precision_threshold=0.85,
            min_support=10,
        )
        self.assertFalse(calibration.accepts_positive("web"))

    def test_high_precision_with_support_is_accepted(self) -> None:
        calibration = SelfReportCalibration(
            by_channel={
                "web": ChannelStats("web", true_positive=19, false_positive=1),
            },
            precision_threshold=0.85,
            min_support=10,
        )
        self.assertTrue(calibration.accepts_positive("web"))

    def test_high_precision_on_thin_evidence_is_not_accepted(self) -> None:
        """Precision on three observations is not a measurement."""
        calibration = SelfReportCalibration(
            by_channel={"web": ChannelStats("web", true_positive=3)},
            precision_threshold=0.85,
            min_support=10,
        )
        self.assertEqual(calibration.by_channel["web"].precision, 1.0)
        self.assertFalse(calibration.accepts_positive("web"))
        self.assertIn("need 10", calibration.explain("web"))

    def test_an_unseen_channel_is_never_accepted(self) -> None:
        self.assertFalse(SelfReportCalibration().accepts_positive("web"))

    def test_an_empty_calibration_accepts_nothing(self) -> None:
        """The conservative default: no measurement means full price."""
        self.assertEqual(SelfReportCalibration().accepted_channels(), [])

    def test_round_trips_through_json(self) -> None:
        original = SelfReportCalibration(
            by_channel={
                "web": ChannelStats("web", true_positive=19, false_positive=1),
            },
            runs=4,
            model="scripted",
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "cal.json"
            original.save(path)
            loaded = SelfReportCalibration.load(path)
        self.assertEqual(loaded.runs, 4)
        self.assertEqual(loaded.by_channel["web"].true_positive, 19)
        self.assertTrue(loaded.accepts_positive("web"))


class TestFilterNeverAcceptsANegative(unittest.TestCase):
    """The safety rule calibration does not get to override."""

    class _Record:
        def __init__(self, method, verdict):
            self.method = method
            self.verdict = verdict

    class _Source:
        kind = "web"

    class _Trace:
        def __init__(self, records):
            self._records = records

        def check_record(self, eid, sid):
            return self._records.get((sid, eid))

        def source(self, sid):
            return TestFilterNeverAcceptsANegative._Source()

    def setUp(self) -> None:
        self.calibration = SelfReportCalibration(
            by_channel={
                "indirect_injection": ChannelStats(
                    "indirect_injection", true_positive=19, false_positive=1
                )
            },
            precision_threshold=0.85,
            min_support=10,
        )

    def test_a_positive_claim_on_a_trusted_channel_is_accepted(self) -> None:
        trace = self._Trace({("S1", "e1"): self._Record("self_report", "tainted")})
        result = filter_candidates(trace, [("S1", "e1")], self.calibration)
        self.assertEqual(result.accepted, [("S1", "e1")])

    def test_a_self_reported_negative_is_never_accepted(self) -> None:
        """A negative claim is absent from the records entirely, and absence
        must route to verification, not to a clearance."""
        trace = self._Trace({})
        result = filter_candidates(trace, [("S1", "e1")], self.calibration)
        self.assertEqual(result.accepted, [])
        self.assertEqual(result.needs_verification, [("S1", "e1")])

    def test_a_structural_verdict_is_not_a_self_report_claim(self) -> None:
        trace = self._Trace({("S1", "e1"): self._Record("structural", "tainted")})
        result = filter_candidates(trace, [("S1", "e1")], self.calibration)
        self.assertEqual(result.accepted, [])


# --- 11.2 group testing -------------------------------------------------------


class TestGroupTestCorrectness(unittest.TestCase):
    def test_finds_the_influential_set(self) -> None:
        candidates = [f"S{i}" for i in range(16)]
        decision = _OracleDecision({"S3", "S11"})
        found = group_test(candidates, decision)
        self.assertEqual(sorted(found), ["S11", "S3"])

    def test_nothing_influential_costs_two_calls(self) -> None:
        candidates = [f"S{i}" for i in range(32)]
        decision = _OracleDecision(set())
        self.assertEqual(group_test(candidates, decision), [])
        self.assertEqual(decision.calls, 2)

    def test_empty_and_singleton(self) -> None:
        self.assertEqual(group_test([], _OracleDecision({"S1"})), [])
        self.assertEqual(group_test(["S1"], _OracleDecision({"S1"})), ["S1"])
        self.assertEqual(group_test(["S1"], _OracleDecision(set())), [])

    def test_split_is_deterministic(self) -> None:
        self.assertEqual(
            split_in_half(["a", "b", "c", "d", "e"]),
            (["a", "b"], ["c", "d", "e"]),
        )

    def test_sibling_inference_agrees_and_costs_less(self) -> None:
        candidates = [f"S{i}" for i in range(32)]
        plain = _OracleDecision({"S7"})
        smart = _OracleDecision({"S7"})
        plain_diag, smart_diag = GroupTestDiagnostics(), GroupTestDiagnostics()
        a = group_test(candidates, plain, plain_diag)
        b = group_test(candidates, smart, smart_diag, infer_sibling=True)
        self.assertEqual(sorted(a), sorted(b))
        self.assertLess(smart.calls, plain.calls)
        self.assertGreater(smart_diag.inferred, 0)


class TestGroupTestBeatsLeaveOneOutWhenSparse(unittest.TestCase):
    def test_same_answer_measurably_fewer_calls(self) -> None:
        candidates = [f"S{i}" for i in range(64)]
        truth = {"S5"}
        loo_decision = _OracleDecision(truth)
        gt_decision = _OracleDecision(truth)
        loo_found = leave_one_out(candidates, loo_decision)
        gt_found = group_test(candidates, gt_decision)

        self.assertEqual(sorted(loo_found), sorted(gt_found))
        self.assertEqual(loo_decision.calls, 64)
        self.assertLess(gt_decision.calls, 20)
        reduction = 1 - gt_decision.calls / loo_decision.calls
        self.assertGreater(reduction, 0.75)

    def test_dense_sets_are_worse_and_the_diagnostics_say_so(self) -> None:
        """The honest half: group testing is not free, and when sparsity fails
        it costs more than the baseline. That must be visible, not hidden."""
        candidates = [f"S{i}" for i in range(16)]
        dense = _OracleDecision({f"S{i}" for i in range(0, 16, 2)})
        diagnostics = GroupTestDiagnostics()
        group_test(candidates, dense, diagnostics)
        self.assertGreater(dense.calls, 16)
        self.assertTrue(diagnostics.sparsity_failing())

    def test_interaction_effects_are_counted_not_swallowed(self) -> None:
        """Two sources that only matter together. Group removal sees the pair
        and neither singleton -- the documented blind spot."""
        calls = {"n": 0}

        def only_together(group):
            calls["n"] += 1
            return {"S0", "S1"} <= set(group)

        diagnostics = GroupTestDiagnostics()
        found = group_test(["S0", "S1", "S2", "S3"], only_together, diagnostics)
        self.assertEqual(found, [])
        self.assertGreater(diagnostics.interaction_suspected, 0)
        self.assertTrue(diagnostics.notes)


class TestGroupTestOnRealScriptedRun(unittest.TestCase):
    """The measurement Phase 11.2 requires, on a real trace."""

    def test_agrees_with_leave_one_out_and_reports_the_reduction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            rows = measure_on_scenario("A", influencing=True, workdir=Path(raw))

        self.assertTrue(rows, "expected at least one multi-source event")
        for row in rows:
            with self.subTest(event=row.event_id):
                self.assertTrue(
                    row.agrees,
                    f"{row.event_id}: group testing found {row.group_found}, "
                    f"leave-one-out found {row.loo_found}",
                )

        total_loo = sum(r.loo_calls for r in rows)
        total_gt = sum(r.group_calls for r in rows)
        reduction = 1 - total_gt / total_loo
        # Reported as a number, not claimed. On this scenario the saving is
        # modest because n is 5-8 and k is 1-3; the sparse events pay for the
        # dense ones.
        self.assertLess(
            total_gt, total_loo,
            f"group testing spent {total_gt} calls against leave-one-out's "
            f"{total_loo} ({reduction:+.0%})",
        )


class TestRedactGroup(unittest.TestCase):
    def test_removes_every_named_source_in_one_pass(self) -> None:
        block = (
            "[S1] (web, https://a)\nfirst\n\n"
            "[S2] (web, https://b)\nsecond\n\n"
            "[S3] (web, https://c)\nthird"
        )
        reduced = redact_group(block, ["S1", "S3"])
        self.assertNotIn("[S1]", reduced)
        self.assertNotIn("[S3]", reduced)
        self.assertIn("[S2]", reduced)
        self.assertIn("second", reduced)

    def test_removing_an_absent_source_raises(self) -> None:
        """A redaction that removes nothing produces an unchanged answer and
        the verdict 'no influence' -- a false clean from plumbing (D-029)."""
        from src.common.prompts import SourceNotInPrompt

        with self.assertRaises(SourceNotInPrompt):
            redact_group("[S1] (web, https://a)\nfirst", ["S9"])


# --- 11.3 budget fallback -----------------------------------------------------


@unittest.skipUnless(HAS_NUMPY, NEEDS_NUMPY)
class TestBudgetAttribution(unittest.TestCase):
    def test_spends_a_fixed_budget_that_does_not_scale_with_n(self) -> None:
        """The whole point of the fallback: cost is flat in n.

        The all-present anchor is free -- removing nothing cannot change
        anything -- so the charged count is budget-1.
        """
        for n in (8, 40):
            with self.subTest(n=n):
                decision = _OracleDecision({"S2"})
                result = attribute([f"S{i}" for i in range(n)], decision, budget=24)
                self.assertEqual(result.calls, 23)
                self.assertEqual(decision.calls, 23)

    def test_recovers_a_sparse_set(self) -> None:
        decision = _OracleDecision({"S2"})
        result = attribute([f"S{i}" for i in range(8)], decision, budget=32)
        self.assertIn("S2", result.influential)

    def test_a_constant_response_is_reported_as_degenerate(self) -> None:
        """Every subset the same means no weight is identifiable. Saying so
        beats emitting confident zeros -- and it must not be turned into a
        clearance, which is the unsafe direction."""
        result = attribute(
            [f"S{i}" for i in range(6)], lambda _g: False, budget=16
        )
        self.assertTrue(result.degenerate)
        self.assertEqual(result.influential, [])
        self.assertTrue(any("cleared" in n for n in result.notes))

    def test_no_candidates(self) -> None:
        self.assertEqual(attribute([], lambda _g: True).calls, 0)


@unittest.skipUnless(HAS_NUMPY, NEEDS_NUMPY)
class TestFallbackOnlyFiresWhenSparsityFails(unittest.TestCase):
    def test_sparse_case_never_reaches_the_budget(self) -> None:
        decision = _OracleDecision({"S3"})
        staged = attribute_if_sparsity_failed(
            [f"S{i}" for i in range(32)], decision
        )
        self.assertEqual(staged.stage, "group_test")
        self.assertEqual(staged.budget_calls, 0)
        self.assertEqual(staged.influential, ["S3"])

    def test_dense_case_falls_back(self) -> None:
        decision = _OracleDecision({f"S{i}" for i in range(0, 16, 2)})
        staged = attribute_if_sparsity_failed(
            [f"S{i}" for i in range(16)], decision
        )
        self.assertEqual(staged.stage, "budget")
        self.assertGreater(staged.budget_calls, 0)

    def test_group_calls_already_spent_are_still_counted(self) -> None:
        """The sunk-cost rule, one level down: falling back does not refund the
        calls group testing already made."""
        decision = _OracleDecision({f"S{i}" for i in range(0, 16, 2)})
        staged = attribute_if_sparsity_failed(
            [f"S{i}" for i in range(16)], decision
        )
        self.assertEqual(
            staged.total_calls, staged.group_calls + staged.budget_calls
        )
        # `total_calls` is the true number of counterfactuals issued across
        # both stages -- the budget stage's free anchor row is already excluded
        # from its own count, so the two agree exactly.
        self.assertEqual(staged.total_calls, decision.calls)
        self.assertGreater(staged.group_calls, 0)


if __name__ == "__main__":
    unittest.main()


class TestSunkAnalysisInThePlannerCap(unittest.TestCase):
    """D-088: why the planner's cap excludes the analysis it has already paid.

    The cap was reported as buggy for comparing replay cost only, never the
    analysis -- the dominant term at 1.18 x N. The switch exists so the claim
    can be measured instead of argued, and these assertions are the
    measurement: including the sunk term cannot save a token and does cost
    real ones.
    """

    def _cover(self, count_sunk: bool):
        from src.recovery.planner import greedy_cover
        from src.recovery.policy import Action

        # One cheap selective action that breaks the only path, and the
        # restart that always exists. Selective is far cheaper than restart.
        selective = Action(kind="replay", target="e1", cost=2153,
                           invalidates=frozenset({"e1"}))
        full = Action(kind="restart_all", target="*", cost=5520)
        paths = [_OnePath()]
        return greedy_cover(
            paths, [selective, full], cap=5520,
            committed_analysis=6489, count_sunk_analysis=count_sunk,
        )

    def test_by_default_the_sunk_analysis_does_not_force_a_restart(self) -> None:
        chosen = self._cover(count_sunk=False)
        self.assertEqual([a.kind for a in chosen], ["replay"])

    def test_counting_it_forces_a_restart_that_costs_strictly_more(self) -> None:
        chosen = self._cover(count_sunk=True)
        self.assertEqual([a.kind for a in chosen], ["restart_all"])

        # The arithmetic that settles it. A is spent either way, so it is in
        # both arms and cancels; the only thing the switch changes is which
        # of the two replay costs is paid on top of it.
        analysis, selective, restart = 6489, 2153, 5520
        self.assertLess(analysis + selective, analysis + restart)
        self.assertEqual((analysis + restart) - (analysis + selective), 3367)


class _OnePath:
    """A path both candidate actions break, so cost alone decides."""

    events = ("e1",)
    edges = ()

    def __init__(self) -> None:
        pass
