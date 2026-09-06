"""
The three representations, built from a trace file.

    CallGraph        who called whom, who used which tool
    EventGraph       every operation, with parent links
    ProvenanceGraph  sources -> events, as exposure or as influence

docs/02-architecture.md describes all three. This module builds them from a
`Trace` and nothing else, which is the week-1 exit test: run the pipeline,
get a trace file, rebuild both graphs from it.

READ THIS BEFORE USING EventGraph.descendants()
-----------------------------------------------
Parent links are *execution history*. They are not influence. Walking them
transitively gives you everything downstream of an event, which is exactly
what baseline B2 does and exactly what our method refuses to do. Propagating
contamination along parent edges would reproduce the baseline and quietly
delete our contribution.

Contamination walks `ProvenanceGraph` influence edges, plus `derived_from`
to cross an agent boundary. `descendants()` is here to *build the baselines
we compare against*, and to answer "what depends on this" for the
invalidation set once contamination is already established.
"""

from dataclasses import dataclass, field
from typing import Iterable

from src.common.models import Event, sort_source_ids
from src.tracing.logger import Trace


# --- call / topology graph ---------------------------------------------------


@dataclass
class CallGraph:
    """Static shape of the pipeline: agents, tools, and who talks to whom.

    Derived from the trace rather than declared separately, so it always
    matches the run it describes. Useful for context and for baseline B2
    (topology closure). Cannot by itself determine contamination.
    """

    agents: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    calls: set[tuple[str, str]] = field(default_factory=set)  # agent -> agent
    uses: set[tuple[str, str]] = field(default_factory=set)  # agent -> tool

    @classmethod
    def from_trace(cls, trace: Trace) -> "CallGraph":
        graph = cls(agents=trace.agents())
        tools: list[str] = []
        for event in trace.events:
            if event.tool_id:
                if event.tool_id not in tools:
                    tools.append(event.tool_id)
                graph.uses.add((event.agent_id, event.tool_id))
            # A parent in another agent is a hand-off between agents.
            for parent_id in event.parents:
                parent = trace.event(parent_id)
                if parent.agent_id != event.agent_id:
                    graph.calls.add((parent.agent_id, event.agent_id))
        graph.tools = tools
        return graph

    def successors(self, agent: str) -> set[str]:
        return {b for a, b in self.calls if a == agent}

    def reachable_from(self, agent: str) -> set[str]:
        """Every agent downstream in the topology, transitively.

        This is baseline B2's discard set at agent granularity: everything
        reachable in the call graph, regardless of whether information
        actually flowed. It is the strict upper bound we report against.
        """
        seen: set[str] = set()
        stack = [agent]
        while stack:
            current = stack.pop()
            for nxt in self.successors(current):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def to_dot(self) -> str:
        lines = ["digraph call_graph {", "  rankdir=LR;"]
        for agent in self.agents:
            lines.append(f'  "{agent}" [shape=box];')
        for tool in self.tools:
            lines.append(f'  "{tool}" [shape=ellipse,style=dashed];')
        for a, b in sorted(self.calls):
            lines.append(f'  "{a}" -> "{b}";')
        for a, t in sorted(self.uses):
            lines.append(f'  "{a}" -> "{t}" [style=dashed];')
        lines.append("}")
        return "\n".join(lines)


# --- event graph -------------------------------------------------------------


@dataclass
class EventGraph:
    """Execution history: one node per operation, edges from parent to child."""

    events: dict[str, Event] = field(default_factory=dict)
    children: dict[str, set[str]] = field(default_factory=dict)
    parents: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_trace(cls, trace: Trace) -> "EventGraph":
        graph = cls(events={e.id: e for e in trace.events})
        graph.children = {eid: set() for eid in graph.events}
        graph.parents = {eid: set() for eid in graph.events}
        for event in trace.events:
            for parent_id in event.parents:
                graph.children[parent_id].add(event.id)
                graph.parents[event.id].add(parent_id)
        return graph

    def roots(self) -> list[str]:
        return [eid for eid, ps in self.parents.items() if not ps]

    def leaves(self) -> list[str]:
        return [eid for eid, cs in self.children.items() if not cs]

    def descendants(self, *event_ids: str) -> set[str]:
        """Everything downstream, transitively. NOT contamination.

        See the module docstring. This answers "what depends on these events",
        which is what the invalidation set needs *after* contamination has
        been established by influence edges -- and what B2 uses to define its
        discard set. It must never be used to decide contamination itself.
        """
        return self._walk(event_ids, self.children)

    def ancestors(self, *event_ids: str) -> set[str]:
        """Everything upstream. Used to find how far back a replay must start."""
        return self._walk(event_ids, self.parents)

    def _walk(self, start: Iterable[str], links: dict[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        stack = list(start)
        while stack:
            current = stack.pop()
            for nxt in links.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def topological_order(self) -> list[str]:
        """Dependency order, for replaying events after invalidation.

        Kahn's algorithm. Ties are broken by event id so the order is stable
        across runs -- a replay that reorders itself between runs would make
        recovery results impossible to compare.
        """
        remaining = {eid: set(ps) for eid, ps in self.parents.items()}
        ready = sorted(eid for eid, ps in remaining.items() if not ps)
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for child in sorted(self.children.get(current, ())):
                remaining[child].discard(current)
                if not remaining[child]:
                    ready.append(child)
                    ready.sort()
        if len(order) != len(self.events):
            stuck = sorted(set(self.events) - set(order))
            raise ValueError(f"event graph has a cycle involving {stuck[:5]}")
        return order

    def to_dot(self, highlight: Iterable[str] = ()) -> str:
        marked = set(highlight)
        lines = ["digraph event_graph {", "  rankdir=LR;", "  node [shape=box];"]
        for eid, event in sorted(self.events.items()):
            style = ',style=filled,fillcolor="#f8b4b4"' if eid in marked else ""
            lines.append(
                f'  "{eid}" [label="{eid}\\n{event.agent_id}\\n{event.kind}"{style}];'
            )
        for eid, kids in sorted(self.children.items()):
            for child in sorted(kids):
                lines.append(f'  "{eid}" -> "{child}";')
        lines.append("}")
        return "\n".join(lines)


# --- provenance graphs -------------------------------------------------------


@dataclass
class ProvenanceGraph:
    """Sources on one side, events on the other.

    Built twice from the same trace: once for exposure, once for influence.
    Figure 2 in docs/04 is these two side by side, and the difference between
    them is the contribution of the project, so they are deliberately the same
    type with the same rendering -- a reader should see one picture with fewer
    edges, not two different kinds of diagram.
    """

    kind: str  # "exposure" | "influence"
    edges: set[tuple[str, str]] = field(default_factory=set)  # source -> event

    def sources_for(self, event_id: str) -> set[str]:
        return {s for s, e in self.edges if e == event_id}

    def events_for(self, source_id: str) -> set[str]:
        return {e for s, e in self.edges if s == source_id}

    def to_dot(self) -> str:
        sources = sorted({s for s, _ in self.edges})
        events = sorted({e for _, e in self.edges})
        lines = [
            f"digraph {self.kind}_graph {{",
            "  rankdir=LR;",
            "  { rank=same; " + " ".join(f'"{s}"' for s in sources) + " }",
        ]
        for s in sources:
            lines.append(f'  "{s}" [shape=note];')
        for e in events:
            lines.append(f'  "{e}" [shape=box];')
        for s, e in sorted(self.edges):
            lines.append(f'  "{s}" -> "{e}";')
        lines.append("}")
        return "\n".join(lines)


def exposure_graph(trace: Trace) -> ProvenanceGraph:
    """Every source that was in context for an event. The larger set."""
    return ProvenanceGraph(
        kind="exposure",
        edges={(sid, e.id) for e in trace.events for sid in e.exposures},
    )


def influence_graph(trace: Trace) -> ProvenanceGraph:
    """Every source that demonstrably changed an event's output.

    Only confident edges count. Anything unconfident is treated as
    contaminated elsewhere (security first), but it is not evidence of
    influence and does not belong in the figure that claims to show it.
    """
    return ProvenanceGraph(
        kind="influence",
        edges={(e.source_id, e.target_event) for e in trace.influence if e.confident},
    )


def exposure_influence_gap(trace: Trace) -> dict[str, list[str]]:
    """{event id -> sources exposed to it that did not influence it}.

    The headline claim, as a number you can put in a table. An empty list
    means either "everything mattered" or "no influence edges recorded yet";
    those are different, and only src/provenance/ can tell them apart.
    """
    exposure = exposure_graph(trace)
    influence = influence_graph(trace)
    gap: dict[str, list[str]] = {}
    for event in trace.events:
        if not event.exposures:
            continue
        influenced = influence.sources_for(event.id)
        gap[event.id] = sort_source_ids(
            [s for s in exposure.sources_for(event.id) if s not in influenced]
        )
    return gap


# --- convenience -------------------------------------------------------------


@dataclass
class Graphs:
    """All three, built once."""

    call: CallGraph
    events: EventGraph
    exposure: ProvenanceGraph
    influence: ProvenanceGraph


def build(trace: Trace) -> Graphs:
    return Graphs(
        call=CallGraph.from_trace(trace),
        events=EventGraph.from_trace(trace),
        exposure=exposure_graph(trace),
        influence=influence_graph(trace),
    )


if __name__ == "__main__":
    import sys

    from src.tracing.logger import read_trace

    path = sys.argv[1] if len(sys.argv) > 1 else "data/runs/fake.jsonl"
    trace = read_trace(path)
    trace.validate()
    graphs = build(trace)

    print(f"trace {path}")
    print(f"  agents {graphs.call.agents}")
    print(f"  tools  {graphs.call.tools}")
    print(f"  calls  {sorted(graphs.call.calls)}")
    print(f"  events {len(graphs.events.events)}, roots {graphs.events.roots()}")
    print(f"  topological order ok: {len(graphs.events.topological_order())} events")
    print()
    print(f"  exposure edges  {len(graphs.exposure.edges)}")
    print(f"  influence edges {len(graphs.influence.edges)}")
    gap = exposure_influence_gap(trace)
    total_gap = sum(len(v) for v in gap.values())
    print(f"  exposed-not-influenced: {total_gap} edges across {len(gap)} events")
    for eid, sources in gap.items():
        if sources:
            print(f"    {eid}: {sources}")
