"""
Phase 12 exit test: storage stays bounded, and the horizon's fallback works.

Three requirements, each with its own test:

  * after GC, exactly one checkpoint remains per agent (the most recent
    confirmed-clean one), and recovery from the retained one still works
  * a detection within the horizon still replays selectively; one beyond it
    falls back to coarse recovery rather than failing or under-recovering
  * checkpoint storage does not grow with trace length -- asserted directly on
    a long synthetic run, not argued

    python -m unittest tests.test_checkpoint_lifecycle -v
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.common.models import CheckRecord, Event, Source, UsageRecord
from src.eval.attacks import build, label_malicious
from src.eval.scripted import ScriptedClient
from src.provenance.estimator import HybridAttributor
from src.provenance.signatures import Calibration
from src.recovery.replay import replay
from src.tracing.checkpoints import (
    Checkpoint,
    CheckpointStore,
    RetentionPolicy,
    apply_horizon,
    checkpoint_interval,
    checkpoint_path_for,
    confirmed_clean,
    gc_checkpoints,
    measured_interval,
    recovery_mode_for,
    replayable,
    rewrite_checkpoints,
    write_cost_in_events,
)
from src.tracing.logger import Trace, read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools


def _clean_check(sid: str, eid: str) -> CheckRecord:
    return CheckRecord(
        source_id=sid, target_event=eid, verdict="clean",
        method="structural", confidence=1.0,
    )


def _synthetic(
    events: int, agents: tuple[str, ...] = ("a", "b"), all_clean: bool = True
) -> tuple[Trace, list[Checkpoint]]:
    """A long run: one source, one exposure per event, one checkpoint per
    event. Deliberately not a pipeline trace -- the point is to grow the
    length far past anything the testbed produces."""
    source = Source(id="S1", kind="web", content="page")
    event_list = []
    checks = []
    checkpoints = []
    for i in range(1, events + 1):
        agent = agents[i % len(agents)]
        eid = f"e{i:04d}"
        event_list.append(
            Event(id=eid, agent_id=agent, kind="agent_output", exposures=["S1"])
        )
        if all_clean:
            checks.append(_clean_check("S1", eid))
        checkpoints.append(
            Checkpoint(
                id=f"k{i:04d}",
                event_id=eid,
                agent_id=agent,
                state={"outputs": {f"e{j:04d}": "x" * 40 for j in range(1, i + 1)}},
                memory={"k": "v" * 20},
                bytes_stored=200 + 45 * i,
            )
        )
    trace = Trace(events=event_list, sources=[source], checks=checks)
    return trace, checkpoints


# --- 12.1 garbage collection --------------------------------------------------


class TestGarbageCollection(unittest.TestCase):
    def test_one_checkpoint_per_agent_survives_a_fully_examined_trace(self) -> None:
        trace, checkpoints = _synthetic(events=40, agents=("a", "b"))
        result = gc_checkpoints(checkpoints, trace)

        by_agent: dict[str, list[Checkpoint]] = {}
        for c in result.retained:
            by_agent.setdefault(c.agent_id, []).append(c)
        self.assertEqual(sorted(by_agent), ["a", "b"])
        for agent, kept in by_agent.items():
            self.assertEqual(
                len(kept), 1, f"{agent} kept {[c.id for c in kept]}, expected one"
            )
        self.assertGreater(len(result.deleted), 0)
        self.assertGreater(result.bytes_freed, result.bytes_retained)

    def test_the_survivor_is_the_most_recent(self) -> None:
        trace, checkpoints = _synthetic(events=20, agents=("a",))
        result = gc_checkpoints(checkpoints, trace)
        self.assertEqual(len(result.retained), 1)
        self.assertEqual(result.retained[0].id, checkpoints[-1].id)

    def test_nothing_is_dropped_when_nothing_was_examined(self) -> None:
        """Unchecked is not clean. An uncleared checkpoint is retained, which
        costs bytes and never costs a rewind point."""
        trace, checkpoints = _synthetic(events=20, all_clean=False)
        result = gc_checkpoints(checkpoints, trace)
        self.assertEqual(len(result.deleted), 0)
        self.assertEqual(len(result.retained), len(checkpoints))

    def test_the_most_recent_checkpoint_is_never_deleted(self) -> None:
        trace, checkpoints = _synthetic(events=30, agents=("a",))
        result = gc_checkpoints(checkpoints, trace)
        self.assertIn(checkpoints[-1].id, {c.id for c in result.retained})

    def test_a_single_unexamined_pair_blocks_clearance_of_later_checkpoints(self) -> None:
        """The prefix rule: a checkpoint payload is a global snapshot, so one
        unexamined exposure anywhere behind it makes it unclearable."""
        trace, checkpoints = _synthetic(events=20, agents=("a",))
        # Drop the check on an early event.
        trace = Trace(
            events=trace.events,
            sources=trace.sources,
            checks=[c for c in trace.checks if c.target_event != "e0003"],
        )
        # Checkpoints before the unexamined event are still clearable; every
        # one at or after it carries the unexamined pair in its snapshot.
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint.event_id):
                if checkpoint.event_id in ("e0001", "e0002"):
                    self.assertTrue(confirmed_clean(trace, checkpoint))
                else:
                    self.assertFalse(confirmed_clean(trace, checkpoint))

        # And GC then keeps more than one, because no later checkpoint
        # dominates: the most recent (never dropped) plus everything at or
        # after the last confirmed-clean one.
        result = gc_checkpoints(checkpoints, trace)
        self.assertGreater(len(result.retained), 1)
        self.assertIn("k0002", {c.id for c in result.retained})

    def test_rewriting_the_sidecar_frees_bytes_and_reloads(self) -> None:
        trace, checkpoints = _synthetic(events=40)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "run.checkpoints.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps({"record": "checkpoint", **c.to_dict()})
                    for c in checkpoints
                )
                + "\n",
                encoding="utf-8",
            )
            result = gc_checkpoints(checkpoints, trace)
            freed = rewrite_checkpoints(path, result.retained)
            self.assertGreater(freed, 0)
            reloaded = CheckpointStore.load(path)
        self.assertEqual(
            {c.id for c in reloaded}, {c.id for c in result.retained}
        )


class TestStorageStaysBounded(unittest.TestCase):
    """The Phase 12 definition-of-done assertion, made directly."""

    def test_retained_bytes_do_not_grow_with_trace_length(self) -> None:
        retained_at = {}
        raw_at = {}
        for length in (50, 200, 800):
            trace, checkpoints = _synthetic(events=length, agents=("a", "b", "c", "d"))
            result = gc_checkpoints(checkpoints, trace)
            retained_at[length] = result.bytes_retained
            raw_at[length] = sum(c.bytes_stored for c in checkpoints)

        # Without GC, storage is superlinear: each checkpoint re-serialises the
        # prefix. Confirm the thing we are protecting against is real first,
        # otherwise the bound below proves nothing.
        self.assertGreater(raw_at[800] / raw_at[50], 16)

        # With GC, only the newest checkpoint per agent survives. Its own
        # payload still grows with the prefix it snapshots, but the *count*
        # stops growing -- which is the unbounded term. Retained bytes must
        # therefore grow far slower than raw, and the checkpoint count must not
        # grow at all.
        self.assertLess(
            retained_at[800] / retained_at[50],
            raw_at[800] / raw_at[50] / 4,
            "GC must remove the superlinear term, not merely shave it",
        )

    def test_the_retained_checkpoint_count_is_flat_in_trace_length(self) -> None:
        counts = []
        for length in (50, 200, 800, 2000):
            trace, checkpoints = _synthetic(events=length, agents=("a", "b", "c", "d"))
            counts.append(len(gc_checkpoints(checkpoints, trace).retained))
        self.assertEqual(
            counts, [4, 4, 4, 4],
            "one checkpoint per agent, whatever the trace length",
        )


# --- 12.2 recovery horizon ----------------------------------------------------


def _real_run(tmp: Path, scenario: str = "A"):
    attack = build(scenario, True)
    path = tmp / f"{scenario}.jsonl"
    tools = attack.apply(
        Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    )
    client = ScriptedClient(seed=20260906)
    run_pipeline(
        path,
        client=client,
        tools=tools,
        attributor=HybridAttributor(
            client=client, mode="self_report",
            calibration=Calibration.load(), model="scripted", seed=20260906,
        ),
        handoff_hook=attack.handoff_hook,
    )
    planted = label_malicious(path, attack.marker)
    if not planted:
        raise RuntimeError("attack did not land")
    return path, planted, attack


class TestRecoveryHorizon(unittest.TestCase):
    def test_no_horizon_keeps_everything(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path, _planted, _attack = _real_run(Path(raw))
            result = apply_horizon(path, RetentionPolicy(horizon_turns=None))
            self.assertEqual(result.refs_downgraded, 0)
            self.assertEqual(result.bytes_freed, 0)

    def test_within_the_horizon_selective_replay_is_still_possible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path, _planted, _attack = _real_run(Path(raw))
            trace = read_trace(path)
            recent = [e.id for e in trace.events[-4:]]

            apply_horizon(path, RetentionPolicy(horizon_turns=6))
            trace = read_trace(path)
            ok, blocked = replayable(trace, recent)
            self.assertTrue(ok, f"recent events unexpectedly blocked: {blocked}")
            mode = recovery_mode_for(trace, recent)
            self.assertEqual(mode.mode, "selective")

    def test_beyond_the_horizon_falls_back_to_coarse_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path, _planted, _attack = _real_run(Path(raw))
            trace = read_trace(path)
            model_events = [
                u.event_id for u in trace.usage
                if u.purpose == "pipeline" and u.event_id
            ]
            oldest = model_events[0]

            freed = apply_horizon(path, RetentionPolicy(horizon_turns=4))
            self.assertGreater(freed.refs_downgraded, 0)
            self.assertGreater(freed.bytes_freed, 0)

            trace = read_trace(path)
            ok, blocked = replayable(trace, [oldest])
            self.assertFalse(ok)
            self.assertIn(oldest, blocked)

            mode = recovery_mode_for(trace, [oldest])
            self.assertEqual(
                mode.mode, "coarse",
                "a detection beyond the horizon must widen scope, not "
                "silently under-recover",
            )
            # Coarse means the whole affected agent, which needs no stored
            # prompts. It must be a strict superset of what was asked for.
            self.assertIn(oldest, mode.invalidation)
            self.assertGreater(len(mode.invalidation), 1)
            self.assertTrue(mode.reason)

    def test_a_ref_shared_with_an_in_horizon_event_is_kept(self) -> None:
        """Content is addressed by hash, so one blob can be shared. Ageing out
        the old event must not break the recent one's replay."""
        with tempfile.TemporaryDirectory() as raw:
            path, _planted, _attack = _real_run(Path(raw))
            before = read_trace(path)
            head_refs = set(before.events[-1].inputs_ref)
            apply_horizon(path, RetentionPolicy(horizon_turns=2))
            after = read_trace(path)
            for ref in head_refs:
                if after.content is not None and after.content.has(ref):
                    self.assertNotEqual(after.content.get(ref), "")


class TestGCRetainedCheckpointStillRecovers(unittest.TestCase):
    def test_recovery_from_the_surviving_checkpoint_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path, planted, attack = _real_run(tmp)
            original = read_trace(path)
            checkpoints = CheckpointStore.load(checkpoint_path_for(path))
            self.assertTrue(checkpoints)

            result = gc_checkpoints(checkpoints, original)
            retained = result.retained
            self.assertTrue(retained)
            rewrite_checkpoints(checkpoint_path_for(path), retained)

            from src.recovery.planner import plan_recovery

            plan = plan_recovery(original, planted, retained)
            out = tmp / "recovered.jsonl"
            tools = attack.apply(
                Tools.from_fixtures(memory_path=out.with_suffix(".memory.json"))
            )
            _outcome, report = replay(
                original,
                plan.invalidation_set,
                ScriptedClient(seed=20260907),
                out,
                tools=tools,
                flagged=planted,
                handoff_hook=attack.handoff_hook,
            )
            report.assert_invariants(set(plan.invalidation_set))
            self.assertTrue(report.spliced)


# --- 12.3 checkpoint interval -------------------------------------------------


class TestCheckpointInterval(unittest.TestCase):
    def test_matches_the_young_daly_form(self) -> None:
        # sqrt(2 * delta * M)
        self.assertAlmostEqual(checkpoint_interval(2.0, 8.0), 32.0 ** 0.5)
        self.assertAlmostEqual(checkpoint_interval(0.5, 4.0), 2.0)
        self.assertAlmostEqual(checkpoint_interval(8.0, 16.0), 16.0)

    def test_grows_with_the_square_root_of_both_terms(self) -> None:
        base = checkpoint_interval(1.0, 10.0)
        self.assertAlmostEqual(checkpoint_interval(4.0, 10.0) / base, 2.0)
        self.assertAlmostEqual(checkpoint_interval(1.0, 40.0) / base, 2.0)

    def test_zero_cost_or_zero_latency_gives_zero(self) -> None:
        self.assertEqual(checkpoint_interval(0.0, 10.0), 0.0)
        self.assertEqual(checkpoint_interval(10.0, 0.0), 0.0)

    def test_negatives_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            checkpoint_interval(-1.0, 10.0)

    def test_write_cost_is_measured_off_the_real_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path, _planted, _attack = _real_run(Path(raw))
            delta = write_cost_in_events(path)
        self.assertGreater(delta, 0.0)

    def test_the_latency_input_is_never_defaulted(self) -> None:
        """Phase 12.3 requires the mean to come from Phase 9's measurement. A
        missing report must raise, not silently substitute a constant."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path, _planted, _attack = _real_run(tmp)
            with self.assertRaises(FileNotFoundError):
                measured_interval(path, economics_report=tmp / "absent.json")

            (tmp / "report.json").write_text(
                json.dumps({"latency": {"mean_turns_between_detections": 8.0}}),
                encoding="utf-8",
            )
            out = measured_interval(path, economics_report=tmp / "report.json")
            self.assertEqual(out["mean_turns_between_detections"], 8.0)
            self.assertAlmostEqual(
                out["interval_events"],
                checkpoint_interval(out["checkpoint_write_cost_events"], 8.0),
            )

    def test_an_empty_latency_measurement_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path, _planted, _attack = _real_run(tmp)
            (tmp / "report.json").write_text(
                json.dumps({"latency": {"n": 0}}), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                measured_interval(path, economics_report=tmp / "report.json")


if __name__ == "__main__":
    unittest.main()
