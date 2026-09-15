"""Phases 4, 5 and 7: upstream attribution, investigation order, memory rollback.

Three boundaries that were implicit and are now decided, implemented and
pinned:

  Phase 4  the system treated every flagged source as the origin and never
           asked what produced it. `src/provenance/upstream.py` walks back and
           surfaces candidates -- and deliberately stops there.
  Phase 5  the detector's per-source confidence was computed and thrown away;
           only a flat list of ids crossed into recovery. It now orders the
           investigation.
  Phase 7  memory was reset wholesale on every recovery attempt. The per-write
           plan a live store would actually need is now computed, and the
           reason the replay still resets wholesale is a property of replay,
           not a shortcut.

    python -m unittest tests.test_scope_boundaries -v
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.eval.attacks import build, label_malicious
from src.eval.detectors import Oracle
from src.eval.scripted import ScriptedClient
from src.provenance.upstream import investigation_candidates
from src.recovery.verify import memory_writes, selective_memory_rollback
from src.tracing.logger import TraceLogger, read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools


def _scenario_a(tmp: Path, refine: bool = False):
    """The scripted A-influencing run. S3 is the planted page; the Coder's
    script is two hops downstream of it.

    `refine` runs the counterfactual pass, which is what puts influence edges in
    the trace. It matters here: with edges, the walk follows established
    influence and the route is script -> finding -> page. Without them it falls
    back to the producing event's exposures, which is wider -- more candidates,
    reached sooner -- and deliberately so, since a candidate list erring wide
    costs a second look and erring narrow costs the origin.
    """
    attack = build("A", True)
    path = tmp / "a.jsonl"
    tools = attack.apply(
        Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    )
    client = ScriptedClient(seed=20260906)
    run_pipeline(
        path, client=client, tools=tools, handoff_hook=attack.handoff_hook
    )
    planted = label_malicious(path, attack.marker)
    if not planted:
        raise RuntimeError("attack did not land")
    if refine:
        from src.provenance.estimator import refine_for_verdict

        verdict = Oracle().flag(read_trace(path))
        refine_for_verdict(
            path, verdict.sources(), client, model="scripted",
            detector_confidence=dict(verdict.flagged),
        )
    return read_trace(path), planted


class TestUpstreamAttribution(unittest.TestCase):
    def test_a_true_origin_two_hops_up_is_surfaced(self) -> None:
        """The case the boundary exists for.

        A detector that flags the Coder's script rather than the page that
        poisoned it has named a symptom. The walk goes script -> findings ->
        page, and the page is the planted source -- reached without anything
        reading `Source.malicious`.
        """
        with tempfile.TemporaryDirectory() as raw:
            trace, planted = _scenario_a(Path(raw), refine=True)
            script = next(
                s.id for s in trace.sources
                if s.derived_from and "samples = [" in s.content
            )
            report = investigation_candidates(trace, [script])

        self.assertGreaterEqual(report.max_hops, 2)
        for origin in planted:
            self.assertIn(
                origin,
                report.ids(),
                f"the true origin {origin} was not surfaced from {script}; "
                f"candidates were {report.ids()}",
            )
        surfaced = next(c for c in report.candidates if c.source_id == planted[0])
        self.assertEqual(surfaced.hops, 2, surfaced.describe())

    def test_a_source_from_outside_the_run_terminates_the_walk(self) -> None:
        """A web page is not *derived from* the response that fetched it, so
        flagging it implicates nothing earlier. Walking `origin_event` instead
        would drag in everything else that tool call happened to see."""
        with tempfile.TemporaryDirectory() as raw:
            trace, planted = _scenario_a(Path(raw))
            report = investigation_candidates(trace, planted)
        self.assertEqual(report.candidates, [])
        self.assertEqual(sorted(report.terminal), sorted(planted))

    def test_candidates_are_not_promoted_to_seeds(self) -> None:
        """The boundary itself. Surfacing is not flagging: the relation walked
        is "was an input to", which is exposure, and contaminating on exposure
        is the conflation the whole project exists to remove."""
        with tempfile.TemporaryDirectory() as raw:
            trace, planted = _scenario_a(Path(raw))
            script = next(
                s.id for s in trace.sources
                if s.derived_from and "samples = [" in s.content
            )
            report = investigation_candidates(trace, [script])
        self.assertEqual(report.flagged, [script])
        self.assertNotIn(script, report.ids())


class TestInvestigationOrder(unittest.TestCase):
    def test_confidence_orders_the_group_least_certain_first(self) -> None:
        """The ordering rule, on the sort itself.

        Ascending, because a low-confidence flag is the one that might be a
        false positive and clearing it removes a whole downstream region, while
        a high-confidence flag mostly confirms taint that was going to be
        recomputed anyway.
        """
        group = ["S1", "S2", "S3", "S4"]
        confidence = {"S1": 0.9, "S2": 0.3, "S3": 0.6}
        position = {sid: i for i, sid in enumerate(group)}
        ordered = sorted(
            group, key=lambda s: (confidence.get(s, 1.0), position[s])
        )
        self.assertEqual(ordered, ["S2", "S3", "S1", "S4"])

    def test_an_unflagged_source_never_displaces_a_doubtful_flag(self) -> None:
        confidence = {"S1": 0.2}
        group = ["S9", "S1"]
        position = {sid: i for i, sid in enumerate(group)}
        ordered = sorted(
            group, key=lambda s: (confidence.get(s, 1.0), position[s])
        )
        self.assertEqual(ordered, ["S1", "S9"])

    def test_the_verdict_carries_confidence_the_recovery_can_read(self) -> None:
        """Pins the interface, not the value. Phase 5's finding was that this
        field existed and nothing downstream ever looked at it."""
        with tempfile.TemporaryDirectory() as raw:
            trace, planted = _scenario_a(Path(raw))
        verdict = Oracle().flag(trace)
        self.assertTrue(verdict.flagged)
        for sid in planted:
            self.assertGreater(verdict.confidence(sid), 0.0)


def _trace_with_two_writes(path: Path) -> None:
    """Two memory writes by two different agents, one of which will be
    invalidated. Built by hand because the four-agent testbed writes memory
    exactly once, so it cannot express the case this rollback exists for."""
    with TraceLogger(path, meta={"task": "two writes"}) as log:
        for agent, key, value in (
            ("coder", "last_run/approach", "poisoned"),
            ("planner", "last_run/brief", "clean"),
        ):
            log.log_event(
                agent,
                "memory_write",
                output_ref=log.put_content(
                    json.dumps({"key": key, "value": value}, sort_keys=True),
                    kind="output",
                ),
            )


class TestSelectiveMemoryRollback(unittest.TestCase):
    def test_a_clean_write_survives_a_rollback_of_a_different_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "writes.jsonl"
            _trace_with_two_writes(path)
            trace = read_trace(path)
            writes = memory_writes(trace)
            self.assertEqual(len(writes), 2)
            poisoned_event = writes[0][0]

            plan = selective_memory_rollback(
                trace,
                invalidated={poisoned_event},
                current_memory={
                    "last_run/approach": "poisoned",
                    "last_run/brief": "clean",
                },
            )

        self.assertEqual(
            plan,
            {"last_run/approach": None},
            "the clean write must not appear in the plan at all; a key that is "
            "absent is a key that survives",
        )

    def test_a_key_reverts_to_the_last_surviving_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "writes.jsonl"
            with TraceLogger(path, meta={"task": "same key twice"}) as log:
                for value in ("first", "second"):
                    log.log_event(
                        "coder",
                        "memory_write",
                        output_ref=log.put_content(
                            json.dumps({"key": "k", "value": value}, sort_keys=True),
                            kind="output",
                        ),
                    )
            trace = read_trace(path)
            later = memory_writes(trace)[1][0]
            plan = selective_memory_rollback(
                trace, invalidated={later}, current_memory={"k": "second"}
            )
        self.assertEqual(plan, {"k": "first"})

    def test_the_fixture_value_is_the_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "writes.jsonl"
            _trace_with_two_writes(path)
            trace = read_trace(path)
            poisoned_event = memory_writes(trace)[0][0]
            plan = selective_memory_rollback(
                trace,
                invalidated={poisoned_event},
                current_memory={"last_run/approach": "poisoned"},
                initial_memory={"last_run/approach": "from the fixture"},
            )
        self.assertEqual(plan, {"last_run/approach": "from the fixture"})

    def test_nothing_invalidated_means_nothing_to_undo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "writes.jsonl"
            _trace_with_two_writes(path)
            trace = read_trace(path)
            plan = selective_memory_rollback(
                trace, invalidated=set(), current_memory={"last_run/approach": "x"}
            )
        self.assertEqual(plan, {})


if __name__ == "__main__":
    unittest.main()
