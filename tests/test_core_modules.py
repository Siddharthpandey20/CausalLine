"""Phase 4: direct tests for the modules every headline number rests on.

`contamination.py` decides which events are contaminated. Every work-preserved
figure, every unsafe-preservation count and every recovery plan in this project
is downstream of it, and it had **zero** tests -- it was exercised only
indirectly, through pipelines whose own correctness was being judged by its
output.

`baselines.py` decides what "discarded" means. It had none either, and that is
exactly where the accounting bug lived: CausalLine and the baselines were
scored under two different definitions of the same word for the whole campaign.

Each test below is written against a hand-built graph with a known correct
answer, so a wrong result cannot hide behind a real run nobody has the truth
for. Where a test corresponds to a bug this project actually had, the docstring
says which one.

    python -m unittest tests.test_core_modules -v
"""

import unittest

from src.common.models import CheckRecord, Event, InfluenceEdge, Source, UsageRecord
from src.eval.baselines import (
    b0_full_restart,
    b1_agent_taint,
    b2_topology_closure,
    compromised_agents,
    discarded_events,
    entry_events,
    ours,
)
from src.provenance.checks import ClearancePolicy, CheckLedger
from src.provenance.contamination import Policy, contaminate, downstream_closure
from src.recovery.replay import ReplayReport
from src.recovery.verify import (
    ESCALATION_ORDER,
    invalidation_for_scope,
    live_memory_points_at_invalidated,
    next_scope,
    verify,
)
from src.tracing.graphs import (
    CallGraph,
    EventGraph,
    exposure_graph,
    exposure_influence_gap,
    influence_graph,
)
from src.tracing.logger import Trace


# --- fixtures -----------------------------------------------------------------


def _event(eid, agent, exposures=(), parents=(), kind="agent_output", tool=None):
    return Event(
        id=eid, agent_id=agent, kind=kind,
        exposures=list(exposures), parents=list(parents), tool_id=tool,
    )


def _source(sid, kind="web", derived_from=None, origin=None, malicious=False):
    return Source(
        id=sid, kind=kind, content=f"content of {sid}",
        derived_from=derived_from, origin_event=origin or derived_from,
        malicious=malicious,
    )


def _edge(sid, eid):
    return InfluenceEdge(sid, eid, method="counterfactual", confident=True)


def _check(sid, eid, verdict, method="counterfactual"):
    return CheckRecord(
        source_id=sid, target_event=eid, verdict=verdict,
        method=method, confidence=1.0,
    )


def _chain():
    """A -> B -> C, with the poison entering at A and flowing to C.

        S1 (web, malicious) --influences--> e1 (agent a)
        e1's output becomes S2 -----------> e2 (agent b)
        e2's output becomes S3 -----------> e3 (agent c)

    e4 is agent b's *other* event, exposed to S2 but examined and cleared. It
    is the event the whole project exists to preserve.
    """
    events = [
        _event("e1", "a", ["S1"]),
        _event("e2", "b", ["S2"], parents=["e1"]),
        _event("e3", "c", ["S3"], parents=["e2"]),
        _event("e4", "b", ["S2"], parents=["e1"]),
    ]
    sources = [
        _source("S1", malicious=True, origin="e1"),
        _source("S2", kind="agent_message", derived_from="e1"),
        _source("S3", kind="agent_message", derived_from="e2"),
    ]
    edges = [_edge("S1", "e1"), _edge("S2", "e2"), _edge("S3", "e3")]
    checks = [
        _check("S1", "e1", "tainted"),
        _check("S2", "e2", "tainted"),
        _check("S3", "e3", "tainted"),
        _check("S2", "e4", "clean"),
    ]
    return Trace(events=events, sources=sources, influence=edges, checks=checks)


# --- contamination.py ---------------------------------------------------------


class TestContaminationWalk(unittest.TestCase):
    """The algorithm every reported number depends on. Previously untested."""

    def test_contamination_crosses_agent_boundaries_via_derived_from(self) -> None:
        """The edge that is easy to forget and fails in the DANGEROUS direction.

        An agent's output exists twice: as the event that produced it and as
        the source a later agent consumed. Without the `derived_from` hop,
        contamination stops at the first agent boundary and the reported
        contaminated set is too SMALL -- an unsafe preservation.
        """
        region = contaminate(_chain(), ["S1"])
        self.assertEqual(sorted(region.events), ["e1", "e2", "e3"])
        self.assertEqual(sorted(region.sources), ["S1", "S2", "S3"])

    def test_an_examined_and_cleared_pair_stops_the_walk(self) -> None:
        """D-024, as a test. e4 saw S2 and was cleared, so it survives even
        though its agent is contaminated and its exposure is a bad source."""
        region = contaminate(_chain(), ["S1"])
        self.assertNotIn("e4", region.events)

    def test_an_unchecked_exposure_is_contaminated(self) -> None:
        """The conservative fallback. Drop e4's clearance and it must be
        swept in -- absence of a record is not a clearance."""
        trace = _chain()
        trace = Trace(
            events=trace.events, sources=trace.sources, influence=trace.influence,
            checks=[c for c in trace.checks if c.target_event != "e4"],
        )
        region = contaminate(trace, ["S1"])
        self.assertIn("e4", region.events)
        self.assertIn("e4", region.precautionary)
        self.assertNotIn("e4", region.confirmed)

    def test_precautionary_and_confirmed_are_distinguishable(self) -> None:
        """Both are contaminated and both get recovered, but they cost
        different amounts to resolve -- which is what Step 2 chooses between."""
        trace = _chain()
        trace = Trace(
            events=trace.events, sources=trace.sources, influence=trace.influence,
            checks=[c for c in trace.checks if c.target_event != "e4"],
        )
        region = contaminate(trace, ["S1"])
        self.assertEqual(sorted(region.confirmed), ["e1", "e2", "e3"])
        self.assertEqual(sorted(region.precautionary), ["e4"])

    def test_the_walk_never_follows_parent_edges(self) -> None:
        """The rule the module exists to enforce. e4's parent is the
        contaminated e1, but no influence edge connects them, so walking
        parents would sweep it in -- and that is baseline B2, not our method.
        """
        region = contaminate(_chain(), ["S1"])
        self.assertNotIn("e4", region.events)
        # B2 walking the same graph by topology does take it, which is what
        # makes the two methods different at all.
        self.assertIn("e4", downstream_closure(_chain(), ["S1"]))

    def test_a_clean_seed_contaminates_nothing(self) -> None:
        trace = _chain()
        # S3 influences only e3.
        region = contaminate(trace, ["S3"])
        self.assertEqual(sorted(region.events), ["e3"])

    def test_an_unknown_seed_raises_rather_than_reporting_clean(self) -> None:
        """A detector naming a source this trace never saw means the verdict
        and the trace are from different runs. Propagating nothing would look
        exactly like a clean run, which is the worst way to fail."""
        with self.assertRaises(ValueError):
            contaminate(_chain(), ["S99"])

    def test_unconfident_edges_contaminate_by_default(self) -> None:
        trace = _chain()
        weak = Trace(
            events=trace.events, sources=trace.sources,
            influence=[InfluenceEdge("S1", "e1", method="self_report", confident=False)],
            checks=[],
        )
        self.assertIn("e1", contaminate(weak, ["S1"]).events)
        self.assertIn(
            "e1",
            contaminate(
                weak, ["S1"], policy=Policy(unconfident_edges_contaminate=False)
            ).events,
            "still exposed, so the fallback takes it even with the edge ignored",
        )

    def test_reasons_are_recorded_for_every_contaminated_event(self) -> None:
        """A contaminated set is not usable in a paper without 'and this is
        why this one'."""
        region = contaminate(_chain(), ["S1"])
        for eid in region.events:
            self.assertIn(eid, region.reasons)
            self.assertTrue(region.reasons[eid])

    def test_a_self_report_clearance_is_refused_by_the_default_policy(self) -> None:
        """Accepting a self-reported clean is how an unsafe preservation
        happens; the clearance policy must not do it by default."""
        trace = _chain()
        trace = Trace(
            events=trace.events, sources=trace.sources, influence=trace.influence,
            checks=[c for c in trace.checks if c.target_event != "e4"]
            + [_check("S2", "e4", "clean", method="self_report")],
        )
        self.assertIn("e4", contaminate(trace, ["S1"]).events)
        ledger = CheckLedger.from_trace(
            trace, policy=ClearancePolicy(accept_self_report=True)
        )
        self.assertNotIn("e4", contaminate(trace, ["S1"], ledger=ledger).events)


# --- baselines.py -------------------------------------------------------------


class TestBaselines(unittest.TestCase):
    def test_b0_discards_everything(self) -> None:
        self.assertEqual(len(b0_full_restart(_chain(), ["S1"])), 4)

    def test_b1_keeps_the_compromised_agents_work_from_before_the_poison(self) -> None:
        """B1 is the baseline we claim to beat, so it gets the temporal
        cutoff. Denying it that would be free margin and a reviewer would be
        right to call it rigged."""
        trace = _chain()
        # Insert an agent-a event BEFORE the poison arrives.
        events = [_event("e0", "a", [])] + trace.events
        early = Trace(
            events=events, sources=trace.sources,
            influence=trace.influence, checks=trace.checks,
        )
        discard = b1_agent_taint(early, ["S1"])
        self.assertNotIn("e0", discard)
        self.assertIn("e1", discard)

    def test_b2_has_no_temporal_mercy(self) -> None:
        trace = _chain()
        events = [_event("e0", "a", [])] + trace.events
        early = Trace(
            events=events, sources=trace.sources,
            influence=trace.influence, checks=trace.checks,
        )
        self.assertIn("e0", b2_topology_closure(early, ["S1"]))

    def test_ours_is_a_strict_subset_of_b2(self) -> None:
        """The week-2 exit test. If our method ever exceeds B2, something is
        walking edges it should not be."""
        trace = _chain()
        self.assertLess(
            set(ours(trace, ["S1"])), set(b2_topology_closure(trace, ["S1"]))
        )

    def test_entry_events_and_compromised_agents(self) -> None:
        self.assertEqual(entry_events(_chain(), ["S1"]), {"e1"})
        self.assertEqual(compromised_agents(_chain(), ["S1"]), {"a"})

    def test_a_detector_that_flagged_nothing_discards_nothing(self) -> None:
        self.assertEqual(b1_agent_taint(_chain(), []), set())
        self.assertEqual(b2_topology_closure(_chain(), []), set())


class TestDiscardedEventsRegression(unittest.TestCase):
    """The Phase 1b accounting bug, pinned.

    THE BUG: "discarded" had two definitions. Baselines were scored on the
    full event set they chose; an escalated CausalLine run was scored on
    `ReplayReport.replayed`, which holds only the events that made a model
    call -- 6 of 19 in this pipeline. A `restart_all` escalation redoes every
    event, exactly as B0 does, and scored 66.7% work preserved against B0's
    0%.

    WHAT THIS TEST WOULD HAVE CAUGHT: the first assertion below fails outright
    against the old code path, because `replayed` and `invalidation` are
    different sets and the old scorer read the wrong one.
    """

    def test_a_full_restart_is_scored_as_a_full_restart(self) -> None:
        every = {f"e{i}" for i in range(1, 20)}
        model_calls = ["e2", "e5", "e8"]  # what SplicingClient would see
        report = ReplayReport(
            invalidation=frozenset(every), replayed=list(model_calls)
        )
        self.assertEqual(discarded_events(report), every)
        self.assertNotEqual(
            discarded_events(report), set(model_calls),
            "scoring on `replayed` is the bug: it reports 3 events redone "
            "when 19 were",
        )

    def test_it_agrees_with_the_baselines_on_the_same_input(self) -> None:
        """Both paths must produce the same answer for the same recovery."""
        trace = _chain()
        discard = b2_topology_closure(trace, ["S1"])
        report = ReplayReport(invalidation=frozenset(discard))
        self.assertEqual(discarded_events(report, discard), set(discard))

    def test_it_falls_back_when_no_replay_happened(self) -> None:
        self.assertEqual(discarded_events(None, {"e1", "e2"}), {"e1", "e2"})
        self.assertEqual(discarded_events(ReplayReport(), {"e1"}), {"e1"})


# --- graphs.py ----------------------------------------------------------------


class TestGraphs(unittest.TestCase):
    def test_exposure_is_a_superset_of_influence(self) -> None:
        """The paper's core claim, as an invariant."""
        trace = _chain()
        self.assertLessEqual(
            {(s, e) for s, e in influence_graph(trace).edges},
            {(s, e) for s, e in exposure_graph(trace).edges},
        )

    def test_the_gap_is_what_the_project_reports(self) -> None:
        trace = _chain()
        gap = exposure_influence_gap(trace)
        # e4 saw S2 and was not influenced by it: that pair is the gap.
        self.assertEqual(gap["e4"], ["S2"])
        self.assertEqual(gap["e2"], [])

    def test_unconfident_edges_are_excluded_from_the_influence_figure(self) -> None:
        """Not evidence of influence, so it does not belong in the figure that
        claims to show it -- even though it still contaminates."""
        trace = Trace(
            events=[_event("e1", "a", ["S1"])],
            sources=[_source("S1")],
            influence=[InfluenceEdge("S1", "e1", method="self_report", confident=False)],
        )
        self.assertEqual(influence_graph(trace).edges, set())
        self.assertEqual(exposure_graph(trace).edges, {("S1", "e1")})

    def test_topological_order_respects_parents(self) -> None:
        graph = EventGraph.from_trace(_chain())
        order = graph.topological_order()
        self.assertLess(order.index("e1"), order.index("e2"))
        self.assertLess(order.index("e2"), order.index("e3"))

    def test_a_cycle_is_reported_rather_than_silently_truncated(self) -> None:
        cyclic = Trace(
            events=[
                _event("e1", "a", parents=["e2"]),
                _event("e2", "a", parents=["e1"]),
            ],
            sources=[],
        )
        with self.assertRaises(ValueError):
            EventGraph.from_trace(cyclic).topological_order()

    def test_call_graph_reachability_is_what_b2_uses(self) -> None:
        call = CallGraph.from_trace(_chain())
        self.assertEqual(call.reachable_from("a"), {"b", "c"})
        self.assertEqual(call.reachable_from("c"), set())


# --- verify.py ----------------------------------------------------------------


class TestVerify(unittest.TestCase):
    def test_leftover_taint_fails_verification(self) -> None:
        result = verify(_chain(), ["S1"], task_success=True)
        self.assertFalse(result.ok)
        self.assertIn("Taint(new_graph) is non-empty", result.reasons)

    def test_a_failed_task_fails_verification_even_with_no_taint(self) -> None:
        clean = Trace(events=[_event("e1", "a", [])], sources=[])
        result = verify(clean, [], task_success=False)
        self.assertFalse(result.ok)
        self.assertIn("task-level check failed", result.reasons)

    def test_a_missing_flagged_source_is_warned_about_not_ignored(self) -> None:
        """A flagged source absent from the recovered trace cannot seed a
        walk, so the walk comes back empty -- which looks exactly like a clean
        recovery. It must say so instead."""
        clean = Trace(events=[_event("e1", "a", [])], sources=[])
        result = verify(clean, ["S404"], task_success=True)
        self.assertTrue(
            any("absent from the recovered" in r for r in result.reasons),
            f"expected a missing-source warning, got {result.reasons}",
        )

    def test_escalation_order_and_terminal_marker(self) -> None:
        self.assertEqual(next_scope("selective"), "agent_restart")
        self.assertEqual(next_scope("agent_restart"), "restart_all")
        self.assertEqual(next_scope("restart_all"), "exhausted")
        self.assertEqual(next_scope("exhausted"), "exhausted")
        self.assertEqual(ESCALATION_ORDER[-1], "exhausted")

    def test_invalidation_widens_with_scope(self) -> None:
        selective, agent, every = {"e1"}, {"e1", "e2"}, {"e1", "e2", "e3"}
        self.assertEqual(
            invalidation_for_scope("selective", selective, agent, every), selective
        )
        self.assertEqual(
            invalidation_for_scope("agent_restart", selective, agent, every), agent
        )
        self.assertEqual(
            invalidation_for_scope("restart_all", selective, agent, every), every
        )
        self.assertEqual(
            invalidation_for_scope("exhausted", selective, agent, every), every
        )

    def test_stale_memory_is_detected_by_value_not_by_writer_id(self) -> None:
        """A recovered run reuses event ids, so comparing writer ids would
        flag every re-done write as stale. Failure is the live value still
        being the invalidated bytes."""
        import json

        from src.common.content import ContentStore, content_path_for

        original = Trace(
            events=[_event("e1", "a", kind="memory_write")], sources=[]
        )
        original.events[0].output_ref = "cX"

        class _Store:
            def get(self, ref):
                return json.dumps({"key": "k", "value": "poisoned"})

            def has(self, ref):
                return True

        original.content = _Store()
        stale = live_memory_points_at_invalidated(
            original, ["e1"], {"k": "poisoned"}, original=original
        )
        self.assertEqual(len(stale), 1)
        fresh = live_memory_points_at_invalidated(
            original, ["e1"], {"k": "recomputed"}, original=original
        )
        self.assertEqual(fresh, [])


if __name__ == "__main__":
    unittest.main()
