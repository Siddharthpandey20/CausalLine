"""Phase D: the Young/Daly interval is wired, and why GC still frees nothing.

`checkpoint_interval()` and `measured_interval()` existed since Phase 3a and
computed a number nothing consumed -- the pipeline checkpointed once per agent
boundary whatever the formula said. `Pipeline(checkpoint_interval=...)` now
consumes it.

docs/07 section 3a predicted this would make GC matter: "GC and the interval
are complementary and neither does anything alone -- the interval would create
the denser stream GC exists to bound."

**That prediction is wrong, and these tests pin why.** The interval does create
a denser stream (4 checkpoints -> 9 on the short workflow, 8 -> 15 on the long
one). GC still frees zero bytes, at every density measured.

The cause is `confirmed_clean()`, which requires that *every* (event, source)
exposure pair in a checkpoint's prefix be **cleared**. Clearing a pair means
showing it did **not** influence the event. A pair that genuinely did influence
is therefore never cleared -- correctly -- and one such pair anywhere in the
prefix blocks that checkpoint permanently. Real influence is not an anomaly; it
is what a working agent run is made of.

So GC is not broken and is not waiting on checkpoint density. Its precondition
is "a prefix provably free of influence", which a run that did useful work
essentially never has. That is a design tension worth stating in the paper
rather than a bug worth fixing quietly.

    python -m unittest tests.test_checkpoint_interval -v
"""

import tempfile
import unittest
from collections import Counter
from pathlib import Path

from src.eval.scripted import ScriptedClient
from src.provenance.checks import CheckLedger, ClearancePolicy
from src.tracing.checkpoints import (
    CheckpointStore,
    checkpoint_interval,
    checkpoint_path_for,
    gc_checkpoints,
)
from src.tracing.graphs import EventGraph
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools

SEED = 20260906


def _run(tmp: Path, name: str, interval: float | None, long: bool = False):
    path = tmp / f"{name}.jsonl"
    extra = {"research_rounds": 3, "reviewer": True} if long else {}
    run_pipeline(
        path,
        client=ScriptedClient(seed=SEED),
        tools=Tools.from_fixtures(
            memory_path=path.with_suffix(".memory.json"), extended=long
        ),
        checkpoint_interval=interval,
        **extra,
    )
    return (
        read_trace(path),
        list(CheckpointStore.load(checkpoint_path_for(path))),
        path,
    )


class TestYoungDalyFormula(unittest.TestCase):
    def test_interval_grows_with_the_square_root(self) -> None:
        """T_opt ~ sqrt(2 * delta * M). Quadrupling either doubles it."""
        base = checkpoint_interval(0.4, 8.0)
        self.assertAlmostEqual(checkpoint_interval(1.6, 8.0), 2 * base, places=9)
        self.assertAlmostEqual(checkpoint_interval(0.4, 32.0), 2 * base, places=9)

    def test_zero_inputs_give_no_basis_for_an_interval(self) -> None:
        self.assertEqual(checkpoint_interval(0.0, 8.0), 0.0)
        self.assertEqual(checkpoint_interval(0.4, 0.0), 0.0)

    def test_negative_inputs_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            checkpoint_interval(-1.0, 8.0)


class TestIntervalIsActuallyWired(unittest.TestCase):
    def test_default_is_the_original_boundary_policy(self) -> None:
        """No interval means one checkpoint per agent boundary, as before."""
        with tempfile.TemporaryDirectory() as raw:
            _trace, checkpoints, _p = _run(Path(raw), "boundary", None)
            per_agent = Counter(c.agent_id for c in checkpoints)
            self.assertEqual(len(checkpoints), 4)
            self.assertTrue(
                all(n == 1 for n in per_agent.values()),
                f"expected one checkpoint per agent, got {dict(per_agent)}",
            )

    def test_an_interval_produces_a_denser_stream(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _t1, boundary, _p1 = _run(tmp, "b", None)
            _t2, dense, _p2 = _run(tmp, "d", 2.66)
            self.assertGreater(
                len(dense),
                len(boundary),
                "the measured interval is denser than agent-boundary spacing, "
                "so it must produce more checkpoints",
            )

    def test_a_huge_interval_adds_nothing(self) -> None:
        """Interval checkpoints are additive to boundary ones, never fewer.

        Guards the direction: wiring the interval must not be able to *remove*
        a rewind point Step 1's safe frontier depends on.
        """
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _t1, boundary, _p1 = _run(tmp, "b", None)
            _t2, sparse, _p2 = _run(tmp, "s", 10_000.0)
            self.assertEqual(len(sparse), len(boundary))


class TestGCIsBlockedByItsPreconditionNotByDensity(unittest.TestCase):
    def test_gc_frees_nothing_at_any_density(self) -> None:
        """The docs/07 prediction, falsified at three densities."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for name, interval, long in (
                ("short-boundary", None, False),
                ("short-dense", 2.66, False),
                ("long-dense", 2.41, True),
            ):
                trace, checkpoints, _p = _run(tmp, name, interval, long=long)
                order = EventGraph.from_trace(trace).topological_order()
                result = gc_checkpoints(checkpoints, trace, order)
                with self.subTest(config=name, checkpoints=len(checkpoints)):
                    self.assertEqual(
                        result.bytes_freed,
                        0,
                        "if this ever becomes non-zero the docs/07 prediction "
                        "was right after all and D-050 needs rewriting",
                    )

    def test_an_influencing_pair_is_never_cleared(self) -> None:
        """The mechanism, stated as an assertion.

        Clearing means "shown not to influence". So a pair that did influence
        is never cleared, however generous the policy -- and `confirmed_clean`
        needs the whole prefix cleared.
        """
        with tempfile.TemporaryDirectory() as raw:
            trace, _cps, _p = _run(Path(raw), "infl", 2.66)
            permissive = CheckLedger.from_trace(
                trace, policy=ClearancePolicy(accept_self_report=True)
            )
            influencing = {(e.source_id, e.target_event) for e in trace.influence}
            self.assertTrue(influencing, "run established no influence at all")
            for source_id, event_id in sorted(influencing):
                with self.subTest(pair=f"{source_id}->{event_id}"):
                    self.assertFalse(
                        permissive.is_cleared(event_id, source_id),
                        "an influencing pair was reported clean",
                    )


if __name__ == "__main__":
    unittest.main()
