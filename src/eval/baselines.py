"""
The three baselines, and our method, as discard sets.

Every method here answers one question in the same units: **given the
detector's verdict, which events do we throw away and recompute?** Work is
counted in events (D-012), so a discard set of event ids is the only output
any of them needs, and the metrics in metrics.py take it from there.

    B0  full restart      discard everything
    B1  agent-level taint discard the compromised agent from the compromise
                          onward, plus every downstream agent
    B2  topology closure  discard the compromised agent entirely, plus every
                          agent reachable from it in the call graph
    ours                  discard only what influence edges connect

BE FAIR TO B1. IT IS THE ONE WE CLAIM TO BEAT.
----------------------------------------------
B1 is "the honest representation of current practice" (docs/04), so it gets
the temporal cutoff: a real team knows *when* the compromise happened and
would not discard the compromised agent's work from before the poison
arrived. Denying it that would be free margin for us and a reviewer would be
right to call it rigged. B2 keeps no cutoff, which is what makes it the
strict upper bound rather than a second version of B1.
"""

from typing import Iterable

from src.provenance.contamination import Policy, contaminate
from src.tracing.graphs import CallGraph
from src.tracing.logger import Trace


def entry_events(trace: Trace, malicious: Iterable[str]) -> set[str]:
    """The events that brought the flagged sources into the run."""
    flagged = set(malicious)
    return {
        s.origin_event
        for s in trace.sources
        if s.id in flagged and s.origin_event
    }


def compromised_agents(trace: Trace, malicious: Iterable[str]) -> set[str]:
    """The agent(s) holding the events where the bad sources arrived."""
    entries = entry_events(trace, malicious)
    return {e.agent_id for e in trace.events if e.id in entries}


def _downstream_agents(trace: Trace, agents: set[str]) -> set[str]:
    call = CallGraph.from_trace(trace)
    reachable: set[str] = set()
    for agent in agents:
        reachable |= call.reachable_from(agent)
    return reachable


def b0_full_restart(trace: Trace, malicious: Iterable[str]) -> set[str]:
    """Throw the run away and start again. Preserves nothing by definition."""
    return {e.id for e in trace.events}


def b1_agent_taint(trace: Trace, malicious: Iterable[str]) -> set[str]:
    """The compromised agent from the moment of compromise, plus everything
    the downstream agents did.

    The cutoff is by position in the trace rather than by timestamp: the log
    is append-only and written in order (D-008), and two events inside the
    same millisecond would otherwise sort arbitrarily.
    """
    entries = entry_events(trace, malicious)
    if not entries:
        return set()
    order = {e.id: i for i, e in enumerate(trace.events)}
    first = min(order[eid] for eid in entries if eid in order)
    bad_agents = compromised_agents(trace, malicious)
    downstream = _downstream_agents(trace, bad_agents)

    discard: set[str] = set()
    for index, event in enumerate(trace.events):
        if event.agent_id in downstream:
            discard.add(event.id)
        elif event.agent_id in bad_agents and index >= first:
            discard.add(event.id)
    return discard


def b2_topology_closure(trace: Trace, malicious: Iterable[str]) -> set[str]:
    """Everything reachable in the call graph, with no temporal mercy.

    The strict upper bound we report against. If our method ever exceeds
    this, something is walking edges it should not be.
    """
    bad_agents = compromised_agents(trace, malicious)
    if not bad_agents:
        return set()
    affected = bad_agents | _downstream_agents(trace, bad_agents)
    return {e.id for e in trace.events if e.agent_id in affected}


def ours(
    trace: Trace,
    malicious: Iterable[str],
    policy: Policy | None = None,
    checked: set[tuple[str, str]] | None = None,
) -> set[str]:
    """The contaminated region: only what influence edges actually connect.

    Week 2 stops here. The recovery *invalidation* set -- contaminated events
    plus whatever depends on them -- is week 3, and it is a superset of this.
    Reporting this set as though it were the invalidation set would overstate
    what we preserve, so metrics.py names it "contaminated", not "discarded
    by recovery".
    """
    return set(contaminate(trace, malicious, policy=policy, checked=checked).events)


METHODS = {
    "B0 full restart": b0_full_restart,
    "B1 agent taint": b1_agent_taint,
    "B2 topology closure": b2_topology_closure,
    "ours": ours,
}
