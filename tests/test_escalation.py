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


# The source to flag in order to force the ladder to climb. S11 is the
# Researcher's finding about format codes; removing it leaves the Coder with no
# format literals, so the replayed script reaches for a third-party parser the
# executor does not have and the task fails.
#
# WHY THE FIXTURE IS A CLEAN RUN AND NOT A POISONED ONE (D-069)
# --------------------------------------------------------------
# It used to be scenario A influencing with nothing flagged: the attack broke
# the task, recovery was told there was nothing to fix, the task check failed
# and the ladder climbed. That fixture stopped forcing anything when
# verification stopped charging recovery for a task failure the original run
# already had (docs/03 #15). Under the new rule that run verifies clean, and
# correctly so -- the workflow is exactly as recovery found it.
#
# So the fixture now does what the rule actually forbids: it starts from a run
# that **passed** the task and makes recovery break it. The original's success
# is asserted below rather than assumed, because the whole point of the fixture
# is the gap between where the run started and where recovery left it.
FORCING_SOURCE = "S11"


def _clean_run(tmp: Path):
    """A run that does the task, so a later failure is recovery's doing."""
    path = tmp / "clean.jsonl"
    tools = Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    result = run_pipeline(path, client=ScriptedClient(seed=20260906), tools=tools)
    if not result.task_success:
        raise RuntimeError(
            "the fixture's original run must succeed at the task; this test "
            "measures what recovery does to a workflow that was working"
        )
    return path


def _recover_with(tmp: Path, max_escalations: int, flagged=None):
    """Force failure by flagging a source the workflow actually needs."""
    path = _clean_run(tmp)
    original = read_trace(path)
    out = tmp / f"rec-{max_escalations}.jsonl"
    return recover(
        original,
        [FORCING_SOURCE] if flagged is None else flagged,
        ScriptedClient(seed=20260907),
        out,
        tools=Tools.from_fixtures(memory_path=out.with_suffix(".memory.json")),
        checkpoints=CheckpointStore.load(checkpoint_path_for(path)),
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
            result = _recover_with(Path(raw), max_escalations=2)
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
            result = _recover_with(Path(raw), max_escalations=2)
        widened = [n for n in result.notes if "verify failed at scope=" in n]
        scopes = [n.split("scope=")[1].split(":")[0] for n in widened]
        self.assertIn(
            "restart_all", scopes,
            f"the ladder stopped before restart_all; scopes attempted: {scopes}",
        )

    def test_a_zero_budget_performs_no_widening(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = _recover_with(Path(raw), max_escalations=0)
        self.assertEqual(result.escalations, 0)
        scopes = [
            n.split("scope=")[1].split(":")[0]
            for n in result.notes
            if "verify failed at scope=" in n
        ]
        self.assertEqual(scopes, ["selective"])

    def test_a_budget_of_one_reaches_agent_restart_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = _recover_with(Path(raw), max_escalations=1)
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
