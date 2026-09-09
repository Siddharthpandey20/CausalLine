"""
Recovery actions and their costs.

Step 2 of CausalLine picks a set of these that breaks every MaliciousSource
-> FinalOutput path. The underlying problem is a weighted hitting-set /
feedback-vertex instance on a contamination DAG, which is NP-hard in the
general case -- the same complexity class as minimum-cost deadlock-victim
selection. We do not attempt an exact solution. The planner runs the
standard greedy set-cover heuristic: pick the action that minimises
cost(alpha) / (paths alpha breaks), and cap the total at restart_all().

That guarantee is the usual H(n) approximation for greedy set cover. State
that in the paper; do not claim optimality.
"""

from dataclasses import dataclass, field
from typing import Iterable, Literal

from src.tracing.logger import Trace

# Sentinel: no safe checkpoint exists for this agent. Forces a full restart
# of that agent's work. Distinct from a missing list entry, which would mean
# we never considered the agent.
INIT = None

ActionKind = Literal[
    "invalidate",
    "replay",
    "isolate",
    "restart",
    "restart_all",
]


def event_cost(trace: Trace, event_id: str) -> int:
    """Tokens (or a 1-call stand-in) to re-derive one event.

    Pipeline usage is the real number when we have it. Events with no call
    (tool responses, memory ops, the Executor's comparison) cost 1: they are
    one tool invocation, not free, and treating them as 0 would make the
    greedy prefer them over everything, including the events that actually
    spend tokens.
    """
    tokens = sum(
        u.total_tokens
        for u in trace.usage
        if u.event_id == event_id and u.purpose == "pipeline"
    )
    return tokens if tokens else 1


def restart_all_cost(trace: Trace) -> int:
    """What B0 pays: re-execute the original run."""
    spent = trace.pipeline_tokens()
    if spent:
        return spent
    return sum(event_cost(trace, e.id) for e in trace.events)


@dataclass(frozen=True)
class Action:
    """One recovery action. Frozen so it can be a set element."""

    kind: ActionKind
    target: str  # event id, agent id, or "*" for restart_all
    cost: int
    # Events this action puts in the invalidation set (recompute these).
    invalidates: frozenset[str] = field(default_factory=frozenset)
    # Event-to-event edges this action cuts without recomputing the source
    # (isolate). Empty for every kind except isolate.
    cuts: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    def label(self) -> str:
        if self.kind == "restart_all":
            return "restart_all()"
        return f"{self.kind}({self.target})"

    def breaks_path(self, path_events: tuple[str, ...], path_edges: tuple[tuple[str, str], ...] = ()) -> bool:
        """Does this action disconnect this source->output path?"""
        if self.kind == "restart_all":
            return True
        if self.invalidates and set(path_events) & self.invalidates:
            return True
        if self.cuts and set(path_edges) & self.cuts:
            return True
        return False


def replay_action(trace: Trace, event_id: str) -> Action:
    """Re-invoke one event. Cost = one LLM/tool call."""
    return Action(
        kind="replay",
        target=event_id,
        cost=event_cost(trace, event_id),
        invalidates=frozenset({event_id}),
    )


def invalidate_action(trace: Trace, event_id: str, dependents: Iterable[str]) -> Action:
    """Throw away x and re-derive its influence-dependents.

    Cost is the tokens of that set. Breaks every path through x or anything
    that inherited from it. More expensive than replay(x) for the same vertex,
    so the greedy will only pick it when the extra dependents cover enough
    additional paths to pay for themselves -- which is the point of listing
    both.
    """
    group = frozenset({event_id, *dependents})
    return Action(
        kind="invalidate",
        target=event_id,
        cost=sum(event_cost(trace, eid) for eid in group),
        invalidates=group,
    )


def restart_action(
    trace: Trace, agent_id: str, after_event: str | None, order: list[str]
) -> Action:
    """Recompute every event of `agent_id` after its safe frontier.

    `after_event` is the frontier checkpoint's event, or None for INIT
    (restart the agent from the beginning).
    """
    position = {eid: i for i, eid in enumerate(order)}
    cutoff = -1 if after_event is None else position.get(after_event, -1)
    victims = frozenset(
        e.id
        for e in trace.events
        if e.agent_id == agent_id and position.get(e.id, -1) > cutoff
    )
    return Action(
        kind="restart",
        target=agent_id,
        cost=sum(event_cost(trace, eid) for eid in victims) or 1,
        invalidates=victims,
    )


def isolate_action(
    trace: Trace,
    agent_id: str,
    outgoing: Iterable[tuple[str, str]],
    full_cost: int,
) -> Action:
    """Cut this agent's outgoing influence. No rollback.

    Future capability lost, which we price above a full restart so the
    greedy will not "save money" by disabling an agent the task still needs.
    Isolate is in the vocabulary because the spec lists it and because B2
    is the same idea at topology granularity; it should almost never win
    on this four-agent testbed.
    """
    return Action(
        kind="isolate",
        target=agent_id,
        cost=max(2 * full_cost, 1),
        cuts=frozenset(outgoing),
    )


def restart_all_action(trace: Trace) -> Action:
    victims = frozenset(e.id for e in trace.events)
    return Action(
        kind="restart_all",
        target="*",
        cost=restart_all_cost(trace),
        invalidates=victims,
    )
