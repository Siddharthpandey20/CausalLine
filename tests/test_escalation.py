"""Phase 0: the escalation ladder stops where it says it stops.

`recover()` widens the invalidation set on verification failure:

    selective -> agent_restart -> restart_all -> (exhausted)

`max_escalations` bounds how many of those widenings may happen. It did not:
`escalations` was incremented on the transition into `"exhausted"` as well,
and `"exhausted"` is a terminal marker, not a scope anything is replayed at.
So a run that exhausted the ladder reported `escalations=3` against
`max_escalations=2` -- three counted, two performed -- and every
blind-detector cell in the campaign printed 3.

The tests below pin both halves of the correct behaviour, because the
obvious-looking fix breaks the second one:

  * the counter never exceeds `max_escalations`
  * `restart_all`, the strongest rung, is still reachable at the default
    max_escalations=2. Changing the loop guard from `<=` to `<` fixes the
    count and makes this unreachable.

    python -m unittest tests.test_escalation -v
"""

import tempfile
import unittest
from pathlib import Path

from src.eval.attacks import build, label_malicious
from src.eval.scripted import ScriptedClient
from src.recovery.causalline import recover
from src.recovery.verify import ESCALATION_ORDER, next_scope
from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools


def _poisoned_run(tmp: Path, scenario: str = "A"):
    """A run whose task genuinely fails, so verification cannot pass and the
    ladder is forced all the way up. Scenario A influencing poisons the Coder,
    so the generated script does not produce the expected ISO dates."""
    attack = build(scenario, True)
    path = tmp / f"{scenario}.jsonl"
    tools = attack.apply(
        Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    )
    run_pipeline(
        path,
        client=ScriptedClient(seed=20260906),
        tools=tools,
        handoff_hook=attack.handoff_hook,
    )
    planted = label_malicious(path, attack.marker)
    if not planted:
        raise RuntimeError("attack did not land")
    return path, planted, attack


def _recover_with(tmp: Path, max_escalations: int, flagged=None):
    """Force failure by flagging nothing: with no seeds the plan invalidates
    nothing, the poisoned code is replayed verbatim, the task check fails, and
    the ladder has to climb."""
    path, planted, attack = _poisoned_run(tmp)
    original = read_trace(path)
    out = tmp / f"rec-{max_escalations}.jsonl"
    return recover(
        original,
        planted if flagged is None else flagged,
        ScriptedClient(seed=20260907),
        out,
        tools=attack.apply(
            Tools.from_fixtures(memory_path=out.with_suffix(".memory.json"))
        ),
        checkpoints=CheckpointStore.load(checkpoint_path_for(path)),
        handoff_hook=attack.handoff_hook,
        max_escalations=max_escalations,
    )


class TestEscalationLadder(unittest.TestCase):
    def test_the_ladder_has_three_rungs_and_a_terminal_marker(self) -> None:
        self.assertEqual(
            ESCALATION_ORDER,
            ("selective", "agent_restart", "restart_all", "exhausted"),
        )
        self.assertEqual(next_scope("restart_all"), "exhausted")
        self.assertEqual(next_scope("exhausted"), "exhausted")

    def test_counter_never_exceeds_the_budget(self) -> None:
        """The off-by-one. Before the fix a forced failure reported 3."""
        with tempfile.TemporaryDirectory() as raw:
            result = _recover_with(Path(raw), max_escalations=2, flagged=[])
        self.assertFalse(result.ok, "this fixture must fail verification")
        self.assertLessEqual(
            result.escalations, 2,
            f"escalations={result.escalations} exceeds max_escalations=2; the "
            "transition into the terminal 'exhausted' marker is being counted "
            "as a widening",
        )

    def test_restart_all_is_still_reachable_at_the_default_budget(self) -> None:
        """The half the obvious fix breaks.

        Changing the loop guard from `escalations <= max` to `escalations <
        max` bounds the counter correctly and stops the ladder one rung early,
        so `restart_all` -- the strongest recovery available -- never runs.
        """
        with tempfile.TemporaryDirectory() as raw:
            result = _recover_with(Path(raw), max_escalations=2, flagged=[])
        widened = [n for n in result.notes if "verify failed at scope=" in n]
        scopes = [n.split("scope=")[1].split(":")[0] for n in widened]
        self.assertIn(
            "restart_all", scopes,
            f"the ladder stopped before restart_all; scopes attempted: {scopes}",
        )

    def test_a_zero_budget_performs_no_widening(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = _recover_with(Path(raw), max_escalations=0, flagged=[])
        self.assertEqual(result.escalations, 0)
        scopes = [
            n.split("scope=")[1].split(":")[0]
            for n in result.notes
            if "verify failed at scope=" in n
        ]
        self.assertEqual(scopes, ["selective"])

    def test_a_budget_of_one_reaches_agent_restart_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = _recover_with(Path(raw), max_escalations=1, flagged=[])
        self.assertEqual(result.escalations, 1)
        scopes = [
            n.split("scope=")[1].split(":")[0]
            for n in result.notes
            if "verify failed at scope=" in n
        ]
        self.assertEqual(scopes, ["selective", "agent_restart"])

    def test_a_successful_recovery_does_not_escalate_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = _recover_with(Path(raw), max_escalations=2)
        if result.ok:
            self.assertEqual(result.escalations, 0)
            self.assertTrue(
                any("succeeded at scope=selective" in n for n in result.notes)
            )


if __name__ == "__main__":
    unittest.main()
