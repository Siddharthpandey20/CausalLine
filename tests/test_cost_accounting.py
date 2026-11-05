"""The comparison must charge every method for the work it actually did.

Two accounting defects were found while reproducing the cost results, and both
are the kind that come back the moment someone refactors the call site. Neither
is about recovery semantics; both are about what the harness records or hands
over, and both made CausalLine look cheaper or the baselines look worse.

    python -m unittest tests.test_cost_accounting -v

See `docs/research/IREC_COST_ACCOUNTING.md` for the measured size of each.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from src.eval.attacks import build
from src.eval.baselines import b2_topology_closure
from src.eval.detectors import build as build_detector
from src.eval.experiment import (
    _fresh_tools,
    _original_run,
    run_baseline_recovery,
    run_cell,
)
from src.eval.scripted import ScriptedClient
from src.tracing.logger import read_trace


class TestBaselinesReplayOnTheSameFixtures(unittest.TestCase):
    """Defect A. `run_cell` called `run_baseline_recovery` without `tools`, so
    the baselines replayed against the SHORT fixture corpus while CausalLine
    replayed against the extended one. A long cell therefore scored the methods
    on different tool responses, and B2 "failed the task" for that reason
    alone: 6 of 18 baseline rows passed, against 18 of 18 once the fixtures are
    shared.
    """

    def test_run_cell_passes_tools_to_the_baselines(self) -> None:
        """Read off the source, because the failure is a missing keyword and a
        passing functional test on a short workflow would not notice."""
        source = inspect.getsource(run_cell)
        head = source.split("run_baseline_recovery(", 1)[1][:400]
        self.assertIn("tools=", head,
                      "run_cell must hand the baselines the same fixtures "
                      "CausalLine replays on")
        self.assertIn("extended=", head,
                      "the fixtures must follow the workflow, as CausalLine's do")

    def test_a_long_cell_scores_every_method_on_the_same_world(self) -> None:
        """The functional half: on a long workflow B2 passes the task check.

        Before the fix this failed for every influencing long cell, and the
        failure was read as a property of the baseline rather than of the
        harness.
        """
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            orig = tmp / "acct-long.jsonl"
            _original_run("A", True, orig, 20260906, "none", workflow="long")
            original = read_trace(orig)
            attack = build("A", True)
            flagged = build_detector("oracle").flag(original).sources()
            discard = b2_topology_closure(original, flagged)
            out = tmp / "acct-long-B2.jsonl"
            result, _report = run_baseline_recovery(
                "B2 topology closure", discard, original,
                ScriptedClient(seed=20260907), sorted(flagged), attack, out,
                tools=_fresh_tools(attack, out, extended=True),
            )
            self.assertTrue(
                result.task_success,
                "B2 fails a long cell only when it is replayed on the short "
                "fixtures; see docs/research/IREC_COST_ACCOUNTING.md section 2",
            )


class TestEscalatedRunsAreChargedForEveryAttempt(unittest.TestCase):
    """Defect B. `causalline.recover` reported
    `replay_tokens = last_report.replay_tokens` -- the final attempt alone. A
    run that replayed at `selective`, failed verification, replayed at
    `agent_restart` and failed again before `restart_all` paid for three
    replays and was reported as paying for one.

    Measured understatement: up to 2.8x on the scripted matrix, 1.63x pooled
    over the five escalated real-model runs.
    """

    def _recover(self, scenario: str, detector: str):
        from src.eval.baselines import discarded_events  # noqa: F401
        from src.recovery.causalline import recover
        from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            orig = tmp / f"esc-{scenario}-{detector}.jsonl"
            _original_run(scenario, True, orig, 20260906, "hybrid")
            original = read_trace(orig)
            attack = build(scenario, True)
            flagged = build_detector(detector).flag(original).sources()
            out = tmp / f"esc-{scenario}-{detector}-CL.jsonl"
            return recover(
                original, flagged, ScriptedClient(seed=20260907), out,
                tools=_fresh_tools(attack, out),
                checkpoints=CheckpointStore.load(checkpoint_path_for(orig)),
                handoff_hook=attack.handoff_hook,
            )

    def test_an_escalated_run_reports_more_than_its_last_replay(self) -> None:
        """A/influencing under the pessimistic detector escalates twice and
        does real work at each rung: 500 + 500 + 600."""
        rec = self._recover("A", "pessimistic")
        if rec.escalations == 0:
            self.skipTest("this cell no longer escalates; pick another")
        note = next((n for n in rec.notes if "replay cost by attempt" in n), "")
        self.assertTrue(note, "an escalated run must record its per-attempt cost")
        final_only = int(note.rsplit("was ", 1)[1].rstrip(").") or 0)
        self.assertGreater(
            rec.replay_tokens, final_only,
            "replay_tokens is still the final attempt only; the earlier "
            "replays before an escalation are not being charged",
        )

    def test_the_per_attempt_breakdown_sums_to_the_total(self) -> None:
        rec = self._recover("C", "pessimistic")
        note = next((n for n in rec.notes if "replay cost by attempt" in n), "")
        if not note:
            self.skipTest("this cell did not escalate")
        parts = note.split(":", 1)[1].split("(total", 1)[0]
        total = sum(int(chunk.split("=")[1]) for chunk in parts.split(",")
                    if "=" in chunk)
        self.assertEqual(
            total, rec.replay_tokens,
            "the breakdown in the notes must account for the reported total",
        )

    def test_a_run_that_never_escalates_is_unchanged(self) -> None:
        """The fix must not inflate the ordinary case: with one attempt, the
        accumulated total is that attempt."""
        rec = self._recover("A", "oracle")
        self.assertEqual(rec.escalations, 0)
        self.assertFalse([n for n in rec.notes if "replay cost by attempt" in n],
                         "a single-attempt run needs no breakdown")
        self.assertGreater(rec.replay_tokens, 0)

    def test_replay_tokens_still_drives_no_decision(self) -> None:
        """The fix is accounting only. If a scope, plan or verdict ever starts
        reading `replay_tokens`, changing it stops being free."""
        import src.recovery.causalline as cl

        source = inspect.getsource(cl.recover)
        body = source.split("replay_tokens = replay_tokens_spent", 1)[0]
        for forbidden in ("if replay_tokens", "replay_tokens >", "replay_tokens <"):
            self.assertNotIn(
                forbidden, body,
                "a recovery decision now depends on the replay-token count, "
                "so the accounting fix is no longer semantics-free",
            )


if __name__ == "__main__":
    unittest.main()
