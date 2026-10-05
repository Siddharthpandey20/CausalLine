"""Derive the agent graph from a REAL executed trace, not from the source text.

Runs the 56-agent pipeline with the deterministic echo client (no inference,
no API cost), then reads the resulting trace to recover:
  - every agent, in execution order
  - every caller->callee edge, from the recorded `parents` of each event
  - the provider each agent was routed to
  - per-stage event counts

This is the evidence the report's architecture figure is built from.
"""
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "E:/Desktop/Agent Recovery Algo")

from src.eval.attacks import label_malicious
from src.eval.mixed_scenarios import MixedScenario
from src.tracing.logger import read_trace
from src.tracing.mixed import Topology
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools
from tests.test_mixed import _Echo


def stage_of(agent: str) -> str:
    for prefix, stage in (("acq", "A acquisition"), ("norm", "B normalise"),
                          ("hub", "C hub"), ("spec", "C specialists"),
                          ("ver", "D verifiers"), ("rev", "E reviewers")):
        if agent.startswith(prefix):
            return stage
    return {"synth": "F synthesis", "audit": "F audit",
            "executor": "F executor"}.get(agent, "?")


def main() -> None:
    topo = Topology()
    tmp = Path(tempfile.mkdtemp())
    scenario = MixedScenario.build("large", seed=20260917)
    path = tmp / "introspect.jsonl"
    tools = scenario.apply(Tools.from_fixtures(
        memory_path=path.with_suffix(".memory.json")))
    run_pipeline(path, task=scenario.task, client=_Echo(), tools=tools,
                 **scenario.workflow_kwargs)
    trace = read_trace(path)
    trace.validate()

    by_id = {e.id: e for e in trace.events}
    agents_in_order, seen = [], set()
    for e in trace.events:
        if e.agent_id not in seen:
            seen.add(e.agent_id)
            agents_in_order.append(e.agent_id)

    # caller -> callee, from recorded parent edges that cross an agent boundary
    edges = defaultdict(int)
    for e in trace.events:
        for p in e.parents:
            parent = by_id.get(p)
            if parent and parent.agent_id != e.agent_id:
                edges[(parent.agent_id, e.agent_id)] += 1

    # source-mediated edges: an agent consumes a source another agent produced
    src_by_id = {s.id: s for s in trace.sources}
    data_edges = defaultdict(int)
    for e in trace.events:
        for sid in e.exposures:
            s = src_by_id.get(sid)
            if s and s.derived_from:
                producer = by_id.get(s.derived_from)
                if producer and producer.agent_id != e.agent_id:
                    data_edges[(producer.agent_id, e.agent_id)] += 1

    stage_counts = defaultdict(lambda: [0, 0])  # agents, events
    for a in agents_in_order:
        stage_counts[stage_of(a)][0] += 1
    for e in trace.events:
        stage_counts[stage_of(e.agent_id)][1] += 1

    out = {
        "agents_total": len(agents_in_order),
        "events_total": len(trace.events),
        "sources_total": len(trace.sources),
        "agents_in_order": agents_in_order,
        "routing": topo.routing(),
        "control_edges": {f"{a}->{b}": n for (a, b), n in sorted(edges.items())},
        "data_edges": {f"{a}->{b}": n for (a, b), n in sorted(data_edges.items())},
        "stages": {k: {"agents": v[0], "events": v[1]}
                   for k, v in sorted(stage_counts.items())},
        "event_kinds": {k: sum(1 for e in trace.events if e.kind == k)
                        for k in sorted({e.kind for e in trace.events})},
        "checks": len(trace.checks),
        "influence_edges": len(trace.influence),
        "flagged_sources": len(label_malicious(path, scenario.marker)),
    }
    print(json.dumps(out, indent=2))
    Path(sys.argv[1]).write_text(json.dumps(out, indent=2), encoding="utf-8") \
        if len(sys.argv) > 1 else None


if __name__ == "__main__":
    main()
