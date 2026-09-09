"""
Phase 5 exit test: spliced outputs match the log; replay is cheaper than restart.

    python -m unittest tests.test_replay -v
"""

import tempfile
import unittest
from pathlib import Path

from src.common.content import ref_for
from src.eval.attacks import build, label_malicious
from src.eval.scripted import ScriptedClient
from src.provenance.estimator import HybridAttributor
from src.provenance.signatures import Calibration
from src.recovery.planner import plan_recovery
from src.recovery.policy import restart_all_cost
from src.recovery.replay import SpliceError, SplicingClient, pipeline_model_events, replay
from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools


def _run_attack(tmp: Path, scenario: str = "A", influencing: bool = True):
    attack = build(scenario, influencing)
    path = tmp / f"{scenario}-{influencing}.jsonl"
    tools = attack.apply(Tools.from_fixtures(memory_path=path.with_suffix(".memory.json")))
    client = ScriptedClient(seed=20260906)
    attributor = HybridAttributor(
        client=client,
        mode="hybrid",
        calibration=Calibration.load(),
        model="scripted",
        seed=20260906,
        audit_rate=0.25,
    )
    run_pipeline(
        path,
        client=client,
        tools=tools,
        attributor=attributor,
        handoff_hook=attack.handoff_hook,
    )
    planted = label_malicious(path, attack.marker)
    if not planted:
        raise RuntimeError("attack did not land")
    return path, planted, attack


class TestSplicingClientInvariants(unittest.TestCase):
    def test_refuses_to_generate_past_the_original_call_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path, planted, _attack = _run_attack(tmp, "A", False)
            original = read_trace(path)
            wrapper = SplicingClient(
                original=original,
                invalidation=set(),
                inner=ScriptedClient(),
                flagged=set(planted),
            )
            n = len(pipeline_model_events(original))
            for _ in range(n):
                wrapper.generate("Decide the approach\n\nInputs:\n")
            with self.assertRaises(SpliceError):
                wrapper.generate("one more call than the original")


class TestSelectiveReplay(unittest.TestCase):
    def test_spliced_bytes_match_and_cost_beats_restart_all(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path, planted, attack = _run_attack(tmp, "A", True)
            original = read_trace(path)
            checkpoints = CheckpointStore.load(checkpoint_path_for(path))
            plan = plan_recovery(original, planted, checkpoints)
            full = restart_all_cost(original)
            self.assertGreater(full, 0)

            out = tmp / "recovered.jsonl"
            tools = attack.apply(Tools.from_fixtures(memory_path=out.with_suffix(".memory.json")))
            _result, report = replay(
                original,
                plan.invalidation_set,
                ScriptedClient(seed=20260907),
                out,
                tools=tools,
                flagged=planted,
                handoff_hook=attack.handoff_hook,
            )
            report.assert_invariants(set(plan.invalidation_set))

            for event_id in report.spliced:
                logged = original.output_text(event_id)
                self.assertIsNotNone(logged)
                self.assertEqual(
                    ref_for(logged),
                    original.event(event_id).output_ref,
                    f"{event_id} splice is not byte-identical to the log",
                )

            self.assertTrue(report.spliced, "expected at least one spliced event")
            self.assertLess(
                report.replay_tokens,
                full,
                "recovery token cost must be below restart_all() on this scenario",
            )
            self.assertEqual(set(report.replayed) - set(plan.invalidation_set), set())


class TestScenarioC(unittest.TestCase):
    def test_lands_and_no_longer_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path, planted, attack = _run_attack(tmp, "C", True)
            self.assertTrue(planted)
            self.assertEqual(attack.scenario, "C")
            trace = read_trace(path)
            marked = [s for s in trace.sources if s.malicious]
            self.assertTrue(marked)
            self.assertTrue(any(s.kind == "agent_message" for s in marked))


if __name__ == "__main__":
    unittest.main()
