"""
Phase 4 exit test: Safe Frontier on hand-constructed graphs.

Three graphs, known-correct answers, including one that needs the
domino/orphan pull and one that needs two pulls. These are not traces
from the pipeline -- they exist so a wrong frontier cannot hide behind
a real run whose right answer we do not already know.

    python -m unittest tests.test_safe_frontier -v
"""

import unittest

from src.common.models import CheckRecord, Event, InfluenceEdge, Source
from src.recovery.planner import (
    causal_past,
    initial_frontier,
    plan_recovery,
    propagate_domino,
    safe_frontier,
)
from src.recovery.policy import INIT
from src.tracing.checkpoints import Checkpoint
from src.tracing.logger import Trace


def _event(eid: str, agent: str, exposures: list[str], kind: str = "agent_output") -> Event:
    return Event(id=eid, agent_id=agent, kind=kind, exposures=list(exposures))


def _source(
    sid: str,
    derived_from: str | None = None,
    origin_event: str | None = None,
    kind: str = "agent_message",
) -> Source:
    return Source(
        id=sid,
        kind=kind,
        content=f"content of {sid}",
        derived_from=derived_from,
        origin_event=origin_event or derived_from,
    )


def _edge(sid: str, eid: str) -> InfluenceEdge:
    return InfluenceEdge(sid, eid, method="structural", confident=True)


def _check(sid: str, eid: str, verdict: str) -> CheckRecord:
    return CheckRecord(
        source_id=sid,
        target_event=eid,
        verdict=verdict,
        method="structural",
        confidence=1.0,
    )


def _checkpoint(kid: str, event_id: str, agent: str) -> Checkpoint:
    return Checkpoint(id=kid, event_id=event_id, agent_id=agent)


def _trace(events, sources, edges, checks) -> Trace:
    return Trace(
        events=list(events),
        sources=list(sources),
        influence=list(edges),
        checks=list(checks),
    )


def _order(trace: Trace) -> list[str]:
    return [e.id for e in trace.events]


def _frontier_ids(frontiers: dict) -> dict[str, str]:
    return {
        agent: ("INIT" if cp is INIT else cp.id) for agent, cp in frontiers.items()
    }


# ---------------------------------------------------------------------------
# Graph 1 — no domino.
#
#   alice: e1 (clean) --kA1-- e2 (tainted by S1)
#   bob:   e3 (influenced by e1 only) --kB1
#
# Taint = {e2}. c(alice)=kA1, c(bob)=kB1. e3's influencer is on alice's
# *clean* side, so the consistency pass is a no-op.
# ---------------------------------------------------------------------------


def graph_no_domino() -> tuple[Trace, list[Checkpoint], set[str]]:
    events = [
        _event("e0001", "alice", ["S1"]),
        _event("e0002", "alice", ["S1"]),
        _event("e0003", "bob", ["S2"]),
    ]
    sources = [
        _source("S1", kind="web"),
        _source("S2", derived_from="e0001"),
        _source("S3", derived_from="e0002"),
    ]
    edges = [_edge("S1", "e0002"), _edge("S2", "e0003")]
    checks = [
        _check("S1", "e0001", "clean"),
        _check("S1", "e0002", "tainted"),
        _check("S2", "e0003", "tainted"),
    ]
    checkpoints = [
        _checkpoint("kA1", "e0001", "alice"),
        _checkpoint("kA2", "e0002", "alice"),
        _checkpoint("kB1", "e0003", "bob"),
    ]
    return _trace(events, sources, edges, checks), checkpoints, {"e0002"}


# ---------------------------------------------------------------------------
# Graph 2 — one orphan, one pull.
#
#   alice: e1 --kA1-- e2 (tainted)
#   bob:   e3 (influenced by e2) --kB1-- e4 (clean) --kB2
#
# CausalPast(e4) does not include e3, so the *initial* frontier puts bob
# at kB2. kB2's payload still holds e3, and e3 was influenced by e2, which
# sits on alice's tainted side. The domino pass must pull bob to INIT.
# ---------------------------------------------------------------------------


def graph_orphan() -> tuple[Trace, list[Checkpoint], set[str]]:
    events = [
        _event("e0001", "alice", ["S1"]),
        _event("e0002", "alice", ["S1"]),
        _event("e0003", "bob", ["S2"]),
        _event("e0004", "bob", ["S4"]),
    ]
    sources = [
        _source("S1", kind="web"),
        _source("S2", derived_from="e0002"),
        _source("S3", derived_from="e0003"),
        _source("S4", kind="database"),
    ]
    edges = [_edge("S1", "e0002"), _edge("S2", "e0003")]
    checks = [
        _check("S1", "e0001", "clean"),
        _check("S1", "e0002", "tainted"),
        _check("S2", "e0003", "tainted"),
        _check("S4", "e0004", "clean"),
    ]
    checkpoints = [
        _checkpoint("kA1", "e0001", "alice"),
        _checkpoint("kA2", "e0002", "alice"),
        _checkpoint("kB1", "e0003", "bob"),
        _checkpoint("kB2", "e0004", "bob"),
    ]
    return _trace(events, sources, edges, checks), checkpoints, {"e0002", "e0003"}


# ---------------------------------------------------------------------------
# Graph 3 — two orphans, two pulls (the second depends on the first).
#
#   alice: e1 --kA1-- e2 (tainted)
#   bob:   e3 (inf. e2) --kB1-- e4 (clean) --kB2
#   carol: e5 (inf. e3) --kC1-- e6 (clean) --kC2
#
# Initial: c(alice)=kA1, c(bob)=kB2, c(carol)=kC2.
# First pull: bob -> INIT (e3 orphaned by alice's tainted e2).
# Second pull: carol -> INIT (e5 orphaned once bob's e3 is on a tainted side).
# ---------------------------------------------------------------------------


def graph_double_domino() -> tuple[Trace, list[Checkpoint], set[str]]:
    events = [
        _event("e0001", "alice", ["S1"]),
        _event("e0002", "alice", ["S1"]),
        _event("e0003", "bob", ["S2"]),
        _event("e0004", "bob", ["S4"]),
        _event("e0005", "carol", ["S3"]),
        _event("e0006", "carol", ["S6"]),
    ]
    sources = [
        _source("S1", kind="web"),
        _source("S2", derived_from="e0002"),
        _source("S3", derived_from="e0003"),
        _source("S4", kind="database"),
        _source("S6", kind="database"),
    ]
    edges = [
        _edge("S1", "e0002"),
        _edge("S2", "e0003"),
        _edge("S3", "e0005"),
    ]
    checks = [
        _check("S1", "e0001", "clean"),
        _check("S1", "e0002", "tainted"),
        _check("S2", "e0003", "tainted"),
        _check("S4", "e0004", "clean"),
        _check("S3", "e0005", "tainted"),
        _check("S6", "e0006", "clean"),
    ]
    checkpoints = [
        _checkpoint("kA1", "e0001", "alice"),
        _checkpoint("kA2", "e0002", "alice"),
        _checkpoint("kB1", "e0003", "bob"),
        _checkpoint("kB2", "e0004", "bob"),
        _checkpoint("kC1", "e0005", "carol"),
        _checkpoint("kC2", "e0006", "carol"),
    ]
    return (
        _trace(events, sources, edges, checks),
        checkpoints,
        {"e0002", "e0003", "e0005"},
    )


class TestCausalPast(unittest.TestCase):
    def test_includes_self_and_influence_ancestors_only(self) -> None:
        trace, _, _ = graph_orphan()
        self.assertEqual(causal_past(trace, "e0004"), {"e0004"})
        self.assertEqual(causal_past(trace, "e0003"), {"e0003", "e0002"})
        self.assertEqual(causal_past(trace, "e0002"), {"e0002"})


class TestGraph1NoDomino(unittest.TestCase):
    def test_frontier_matches_known_answer(self) -> None:
        trace, checkpoints, taint = graph_no_domino()
        order = _order(trace)
        frontiers = safe_frontier(trace, checkpoints, taint, order)
        self.assertEqual(_frontier_ids(frontiers), {"alice": "kA1", "bob": "kB1"})

    def test_domino_is_a_noop(self) -> None:
        trace, checkpoints, taint = graph_no_domino()
        order = _order(trace)
        first = initial_frontier(trace, checkpoints, taint, order)
        second = propagate_domino(trace, first, checkpoints, taint, order)
        self.assertEqual(_frontier_ids(first), _frontier_ids(second))


class TestGraph2OrphanDomino(unittest.TestCase):
    def test_initial_frontier_keeps_the_orphan(self) -> None:
        # If this fails, the initial rule is already pulling by chronology
        # and the domino pass has nothing to do -- which is B1 in disguise.
        trace, checkpoints, taint = graph_orphan()
        first = initial_frontier(trace, checkpoints, taint, _order(trace))
        self.assertEqual(_frontier_ids(first), {"alice": "kA1", "bob": "kB2"})

    def test_domino_pulls_bob_to_init(self) -> None:
        trace, checkpoints, taint = graph_orphan()
        frontiers = safe_frontier(trace, checkpoints, taint, _order(trace))
        self.assertEqual(_frontier_ids(frontiers), {"alice": "kA1", "bob": "INIT"})


class TestGraph3DoubleDomino(unittest.TestCase):
    def test_initial_frontiers_look_safe(self) -> None:
        trace, checkpoints, taint = graph_double_domino()
        first = initial_frontier(trace, checkpoints, taint, _order(trace))
        self.assertEqual(
            _frontier_ids(first),
            {"alice": "kA1", "bob": "kB2", "carol": "kC2"},
        )

    def test_both_orphans_pulled(self) -> None:
        trace, checkpoints, taint = graph_double_domino()
        frontiers = safe_frontier(trace, checkpoints, taint, _order(trace))
        self.assertEqual(
            _frontier_ids(frontiers),
            {"alice": "kA1", "bob": "INIT", "carol": "INIT"},
        )


class TestDominoBound(unittest.TestCase):
    def test_terminates_within_event_count(self) -> None:
        trace, checkpoints, taint = graph_double_domino()
        # The assertion lives inside propagate_domino: exceeding |V|
        # raises. Completing is the test.
        frontiers = safe_frontier(trace, checkpoints, taint, _order(trace))
        self.assertEqual(len(frontiers), 3)


class TestGreedyDoesNotDefaultToRestartAll(unittest.TestCase):
    def test_tainted_sinks_are_covered_without_restart_all(self) -> None:
        # Graph 1 has no executor output in Taint, so there is no
        # source->FinalOutput path. The planner still has to cover the
        # tainted events themselves; it must not jump to restart_all.
        trace, checkpoints, _ = graph_no_domino()
        plan = plan_recovery(trace, ["S1"], checkpoints)
        self.assertIn("e0002", plan.invalidation_set)
        self.assertFalse(plan.restart_all)
        self.assertLess(len(plan.invalidation_set), len(trace.events))

    def test_empty_taint_means_empty_invalidation(self) -> None:
        trace, checkpoints, _ = graph_no_domino()
        # S4 does not exist; use a source that never influenced anyone.
        # S4 is not in this graph. S2 only influences the clean bob event
        # and is not flagged. Flagging nothing is Blind.
        plan = plan_recovery(trace, [], checkpoints)
        self.assertEqual(set(plan.taint.events), set())
        self.assertEqual(plan.invalidation_set, frozenset())
        self.assertFalse(plan.restart_all)


if __name__ == "__main__":
    unittest.main()
