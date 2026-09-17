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


if __name__ == "__main__":
    unittest.main()
