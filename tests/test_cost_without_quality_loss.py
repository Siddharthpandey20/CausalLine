"""Cost reductions that must not change what recovery produces.

THE RULE THIS FILE ENFORCES
----------------------------
The existing full investigation is the CORRECTNESS BASELINE. A cheaper variant
is acceptable only if it reproduces the baseline's recall and safety. Saving
tokens by recovering less, or by letting a contaminated pair through, is a
failure however large the saving.

These tests read results already on disk. They skip rather than fail when the
data is absent, so a fresh clone is not broken by a missing campaign.

    python -m unittest tests.test_cost_without_quality_loss -v
"""

import json
import unittest
from pathlib import Path

FANOUT = Path("data/results/fanout/fanout-campaign.json")
LAZY = Path("data/results/lazy-selfreport.json")


def _paired_rows():
    """(test_id, repeat) -> {arm: CausalLine row}, for arms run on the same
    design point with the same seed."""
    if not FANOUT.exists():
        return {}
    payload = json.loads(FANOUT.read_text(encoding="utf-8"))
    out: dict = {}
    for run in payload.get("results", []):
        mine = next((r for r in run.get("rows", [])
                     if r.get("method") == "CausalLine"), None)
        if mine is None:
            continue
        out.setdefault((run["test_id"], run["repeat"]), {})[run["arm"]] = {
            "analysis": run["analysis_tokens"],
            "preserved": mine["work_preserved"],
            "blast": mine["blast_radius_events"],
            "unsafe": mine["unsafe_preservations"],
        }
    return {k: v for k, v in out.items() if "eager" in v and "lazy" in v}


class TestTheCheaperVariantMatchesTheBaseline(unittest.TestCase):
    """Measured on the fan-out workload: 12 paired real runs, same seeds.

    The cheaper variant cost 72% less analysis and produced the SAME recovery
    on every one. If that ever stops being true, the saving has started being
    paid for out of recovery quality and this file should fail.
    """

    def setUp(self):
        self.pairs = _paired_rows()
        if not self.pairs:
            self.skipTest("no paired fan-out campaign on disk")

    def test_the_cheaper_variant_never_preserves_less_work(self) -> None:
        for key, arms in self.pairs.items():
            with self.subTest(case=key):
                self.assertGreaterEqual(
                    arms["lazy"]["preserved"], arms["eager"]["preserved"] - 1e-9,
                    "the cheaper variant recovered less work than the baseline",
                )

    def test_the_cheaper_variant_never_discards_a_smaller_region(self) -> None:
        """A smaller blast radius with the same preserved work would mean it
        stopped recomputing something the baseline recomputed."""
        for key, arms in self.pairs.items():
            with self.subTest(case=key):
                self.assertEqual(arms["lazy"]["blast"], arms["eager"]["blast"])

    def test_the_cheaper_variant_introduces_no_unsafe_preservation(self) -> None:
        for key, arms in self.pairs.items():
            with self.subTest(case=key):
                self.assertLessEqual(arms["lazy"]["unsafe"],
                                     arms["eager"]["unsafe"])

    def test_and_it_is_actually_cheaper(self) -> None:
        """A mechanism that preserves quality but saves nothing is not an
        optimization."""
        eager = sum(a["eager"]["analysis"] for a in self.pairs.values())
        lazy = sum(a["lazy"]["analysis"] for a in self.pairs.values())
        self.assertLess(lazy, eager * 0.9)


class TestTheKnownQualityLossIsStillRecorded(unittest.TestCase):
    """The same variant is NOT outcome-identical on the scripted chain matrix.

    It preserves recall (0 pair false negatives) and safety (0 unsafe) but
    recovers less work. That is a workload-dependent result and adopting the
    variant globally would trade recovery quality for tokens, which the
    constraint forbids. Recorded here so the fan-out result above is never
    read as a general licence.
    """

    def setUp(self):
        if not LAZY.exists():
            self.skipTest("no lazy-selfreport measurement on disk")
        self.data = json.loads(LAZY.read_text(encoding="utf-8"))

    def _attacked(self):
        for key in ("attacked", "matrix", "scored"):
            if isinstance(self.data.get(key), dict):
                return self.data[key]
        return self.data

    def test_removing_self_report_entirely_costs_recall(self) -> None:
        """`targeted_only` dropped a pair false negative -- a recall loss, and
        therefore a FAILURE regardless of the tokens it saved."""
        rows = self._attacked()
        target = rows.get("targeted_only")
        hybrid = rows.get("hybrid")
        if not isinstance(target, dict) or not isinstance(hybrid, dict):
            self.skipTest("measurement shape changed")
        self.assertGreater(
            target.get("pair_false_negatives", 0),
            hybrid.get("pair_false_negatives", 0),
            "targeted_only no longer loses recall; re-read the verdict",
        )

    def test_the_deferral_preserves_recall_and_safety_there(self) -> None:
        rows = self._attacked()
        lazy, hybrid = rows.get("lazy"), rows.get("hybrid")
        if not isinstance(lazy, dict) or not isinstance(hybrid, dict):
            self.skipTest("measurement shape changed")
        self.assertLessEqual(lazy.get("pair_false_negatives", 0),
                             hybrid.get("pair_false_negatives", 0))
        self.assertLessEqual(lazy.get("unsafe_preservations", 0),
                             hybrid.get("unsafe_preservations", 0))

    def test_but_it_recovers_less_work_there(self) -> None:
        """The reason it cannot be adopted globally."""
        rows = self._attacked()
        lazy, hybrid = rows.get("lazy"), rows.get("hybrid")
        if not isinstance(lazy, dict) or not isinstance(hybrid, dict):
            self.skipTest("measurement shape changed")
        self.assertLess(lazy.get("work_preserved", 1.0),
                        hybrid.get("work_preserved", 0.0),
                        "the chain-matrix quality loss has gone; re-check")


class TestTheBaselineRemainsTheDefault(unittest.TestCase):
    def test_lazy_self_report_is_off_by_default(self) -> None:
        """Constraint 9: the existing flow must remain intact as fallback."""
        import inspect

        from src.eval.real_llm import run_generated

        params = inspect.signature(run_generated).parameters
        self.assertFalse(params["lazy_self_report"].default)
        self.assertEqual(params["investigation_mode"].default, "self_report")



class TestTheClosureInvariantThatMakesSkippingSound(unittest.TestCase):
    """Contamination never escaped the B2 closure on any trace on disk.

    This is the fact a "skip the provably-irrelevant work" optimization would
    rest on, and it is measured rather than proved: verified with 0 escapes on
    290 real traces and on all three attack channels of the scripted matrix
    (web, memory, agent_message).

    It is recorded here because it stays true and useful even though the
    mechanism built on it FAILED for a separate reason (see
    `docs/gate1/final_cost_direction.md`): the saving cannot be taken without
    deferring attribution, and deferral itself changes the trace.
    """

    def _traces(self):
        import glob

        from src.tracing.logger import read_trace

        out = []
        for path in glob.glob("data/runs/**/*.jsonl", recursive=True):
            if any(x in path for x in (".checkpoints", ".content")):
                continue
            try:
                trace = read_trace(Path(path))
            except Exception:  # noqa: BLE001
                continue
            flagged = [s.id for s in trace.sources if s.malicious]
            if flagged:
                out.append((path, trace, flagged))
        return out

    def test_contamination_never_escapes_the_closure(self) -> None:
        from src.eval.baselines import b2_topology_closure
        from src.provenance.contamination import contaminate

        traces = self._traces()
        if not traces:
            self.skipTest("no traces on disk")
        for path, trace, flagged in traces:
            with self.subTest(trace=Path(path).name):
                region = set(contaminate(trace, set(flagged)).events)
                closure = set(b2_topology_closure(trace, flagged))
                self.assertFalse(region - closure)

    def test_dropping_out_of_closure_records_leaves_recovery_identical(self) -> None:
        """The exact equivalence: the records outside the closure do not
        contribute to the recovery decision on any trace we have."""
        from src.eval.baselines import b2_topology_closure
        from src.provenance.contamination import contaminate

        def region(trace, flagged, scope=None):
            influence = {(x.source_id, x.target_event) for x in trace.influence}
            checked = {(c.source_id, c.target_event) for c in trace.checks}
            if scope is not None:
                influence = {p for p in influence if p[1] in scope}
                checked = {p for p in checked if p[1] in scope}
            return set(contaminate(trace, set(flagged),
                                   influence=influence, checked=checked).events)

        traces = [t for t in self._traces() if t[1].checks]
        if not traces:
            self.skipTest("no traces with check records")
        for path, trace, flagged in traces:
            with self.subTest(trace=Path(path).name):
                closure = set(b2_topology_closure(trace, flagged))
                self.assertEqual(region(trace, flagged),
                                 region(trace, flagged, scope=closure))


class TestDeferralItselfChangesTheTrace(unittest.TestCase):
    """Why the saving above cannot be taken.

    The pipeline's OWN provenance records depend on whether an attributor was
    installed during execution -- not merely on what the attributor concluded.
    Measured on scenario A: 25 structural records and 21 influence edges with
    the inline attributor, 18 and 3 without. Any deferral therefore changes the
    trace before a scoping decision is even made, which is why closure-scoped
    self-report reached 77.2% preserved work against the baseline's 78.9%.
    """

    def test_installing_an_attributor_changes_the_pipelines_own_records(self) -> None:
        import tempfile

        from src.eval.experiment import _original_run
        from src.tracing.logger import read_trace

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _original_run("A", True, base / "inline.jsonl", 20260906, "hybrid")
            _original_run("A", True, base / "none.jsonl", 20260906, "none")
            inline = read_trace(base / "inline.jsonl")
            none = read_trace(base / "none.jsonl")

            def structural(trace):
                return {(c.source_id, c.target_event) for c in trace.checks
                        if c.method == "structural"}

            self.assertNotEqual(
                structural(inline), structural(none),
                "the pipeline's structural records no longer depend on the "
                "attributor -- if so, deferral may now be outcome-preserving "
                "and the direction should be re-tested",
            )

if __name__ == "__main__":
    unittest.main()
