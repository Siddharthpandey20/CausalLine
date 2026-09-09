"""Phase A: Step 2 optimises exactly what Step 4 checks (D-047).

Step 2 (greedy set cover) used to minimise cost subject to "cut every
MaliciousSource -> FinalOutput path". Step 4 (verification) accepts a recovery
only when `Taint(new_graph)` is empty. Those are different conditions. A
tainted event lying on no source -> output path satisfied Step 2 and failed
Step 4, so the planner left it alone to save cost and verification then
rejected the plan -- and the run escalated.

Nothing in the suite caught that. When the covering target was changed from
paths to the contamination closure, all 201 existing tests passed unchanged:
the relationship between the two steps was never pinned by anything. That is
why this file exists.

The invariant, stated once:

    plan.invalidation_set covers plan.taint.events

`greedy_cover` gets one singleton path per tainted event, and `breaks_path()`
on a singleton is true iff the event is in the action's `invalidates`. So
"cover every singleton" is literally the condition Step 4 verifies, and a
selective plan is now correct by construction rather than by accident.

The `restart_all` carve-out below is not an exception to the invariant. When
the cover would cost more than restarting everything, `greedy_cover` returns
`restart_all`, which recomputes the whole trace -- it satisfies Step 4 by
doing strictly more, not less.

    python -m unittest tests.test_step2_covers_step4 -v
"""

import tempfile
import unittest
from pathlib import Path

from src.eval.attacks import build, label_malicious
from src.eval.detectors import Oracle
from src.eval.detectors import build as build_detector
from src.eval.scripted import ScriptedClient
from src.provenance.estimator import (
    CheckBudget,
    HybridAttributor,
    refine_for_verdict,
)
from src.provenance.signatures import Calibration
from src.recovery.planner import plan_recovery
from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools

SCENARIOS = ("A", "B", "C")
DETECTORS = ("oracle", "pessimistic", "blind")
SEED = 20260906


def _run(tmp: Path, scenario: str, influencing: bool):
    """One scripted pipeline run, plus the checkpoints the planner needs.

    This mirrors `experiment._original_run`'s hybrid mode deliberately.
    Influence edges are produced by the attributor *during* the run, so a
    pipeline run without one yields a trace with no influence edges at all --
    no paths, a different closure, and a planner exercised under conditions
    the campaign never uses. The first version of this file made exactly that
    mistake and the paths assertion below is what caught it.
    """
    attack = build(scenario, influencing)
    path = tmp / f"{scenario}-{influencing}.jsonl"
    tools = attack.apply(
        Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    )
    client = ScriptedClient(seed=SEED)
    run_pipeline(
        path,
        client=client,
        tools=tools,
        attributor=HybridAttributor(
            client=client,
            mode="self_report",
            calibration=Calibration.load(),
            model="scripted",
            seed=SEED,
            audit_rate=0.0,
            trust_self_report_negatives=False,
        ),
        handoff_hook=attack.handoff_hook,
    )
    if not label_malicious(path, attack.marker):
        raise RuntimeError(f"{attack.name}: marker never reached the trace")
    refine_for_verdict(
        path,
        Oracle().flag(read_trace(path)).sources(),
        client,
        calibration=Calibration.load(),
        budget=CheckBudget(),
        model="scripted",
    )
    return (
        read_trace(path),
        CheckpointStore.load(checkpoint_path_for(path)),
    )


class TestStep2CoversStep4(unittest.TestCase):
    def test_invalidation_covers_taint_on_every_configuration(self) -> None:
        """The whole point. Every scenario x variant x detector."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for scenario in SCENARIOS:
                for influencing in (True, False):
                    trace, checkpoints = _run(tmp, scenario, influencing)
                    for detector in DETECTORS:
                        flagged = build_detector(detector).flag(trace).sources()
                        plan = plan_recovery(trace, flagged, checkpoints)
                        with self.subTest(
                            scenario=scenario,
                            influencing=influencing,
                            detector=detector,
                        ):
                            if plan.restart_all:
                                continue  # satisfies Step 4 by doing more
                            uncovered = set(plan.taint.events) - set(
                                plan.invalidation_set
                            )
                            self.assertEqual(
                                uncovered,
                                set(),
                                f"{len(uncovered)} tainted event(s) left "
                                f"uncovered by Step 2; Step 4 would reject "
                                f"this plan and the run would escalate",
                            )

    def test_empty_taint_selects_nothing(self) -> None:
        """Exposed-only with a detector that flags nothing: no taint, no work.

        Guards the other direction -- covering the closure must not invent
        actions when the closure is empty, which is the exposed-only win.
        """
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            trace, checkpoints = _run(tmp, "A", False)
            plan = plan_recovery(trace, [], checkpoints)
            self.assertEqual(set(plan.taint.events), set())
            self.assertEqual(plan.selected, [])
            self.assertEqual(set(plan.invalidation_set), set())

    def test_paths_are_still_reported_even_though_step2_ignores_them(
        self,
    ) -> None:
        """`plan.paths` stays a real provenance result.

        The covering target moved off it; the measurement did not. The
        completion report quotes a path count (9/18 configurations) and the
        plan summary prints one, so an empty list here would be a silent
        regression of a reported number rather than a design change.
        """
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            trace, checkpoints = _run(tmp, "A", True)
            flagged = build_detector("oracle").flag(trace).sources()
            plan = plan_recovery(trace, flagged, checkpoints)
            self.assertTrue(
                plan.paths,
                "A-influencing under oracle had real source->output paths "
                "before Step 2's target changed; it must still report them",
            )
            for path in plan.paths:
                self.assertNotEqual(
                    path.source_id,
                    "taint",
                    "'taint' is the synthetic id used for cover targets; it "
                    "must not leak into the reported provenance paths",
                )


if __name__ == "__main__":
    unittest.main()
