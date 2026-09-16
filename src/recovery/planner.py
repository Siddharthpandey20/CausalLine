"""
CausalLine Steps 0–2: contamination closure, Safe Frontier, greedy actions.

Step 0 is the existing walk in `src/provenance/contamination.py`. It already
propagates along influence edges only (never Event.parents) and already
keeps precautionary (unchecked) events distinguishable from confirmed ones.
This module calls it; it does not reimplement it.

Step 1 replaces `safe_checkpoint_for()`, which picks a checkpoint by
position in the event order and never asks whether the causal past is
clean. The verified version, plus the cross-agent domino pass that the
repository was missing entirely, lives here.

Step 2 is greedy set cover over the action vocabulary in policy.py. We do
not solve the cut exactly: minimum-cost cut on a contamination DAG is
NP-hard (weighted feedback-vertex / hitting-set). The greedy is the
standard approximation for that reason.
"""

from dataclasses import dataclass, field
from typing import Iterable

from src.provenance.contamination import ContaminatedRegion, Policy, contaminate
from src.recovery.policy import (
    INIT,
    Action,
    isolate_action,
    invalidate_action,
    replay_action,
    restart_action,
    restart_all_action,
    restart_all_cost,
)
from src.tracing.checkpoints import Checkpoint
from src.tracing.graphs import EventGraph
from src.tracing.logger import Trace


# --- event-to-event influence ----------------------------------------------


# DEAD CODE -- no caller, kept for reference.
# The inverse of `source_producer()` below, which is the direction every
# caller actually needs. Kept because the pairing is what makes the D-012
# event/source correspondence readable.
def derived_source_of(trace: Trace, event_id: str) -> list[str]:
    """Sources that *are* this event's output (D-012)."""
    return [s.id for s in trace.sources if s.derived_from == event_id]


def source_producer(trace: Trace, source_id: str) -> str | None:
    """The event a source is the output of, if any.

    Prefers `derived_from` (the producer) over `origin_event` (the event that
    brought the source into the log). For a web page those can differ: the
    page is not derived from the tool_response in the D-012 sense -- it
    arrived from outside -- so origin_event is the only handle. For an agent
    finding they are the same work.
    """
    source = trace.source(source_id)
    return source.derived_from or source.origin_event


def event_influence_edges(trace: Trace) -> set[tuple[str, str]]:
    """(src_event, dst_event) induced by source->event influence.

    InfluenceEdge is source -> event. Contamination already crosses an agent
    boundary via derived_from. The planner needs the composed event->event
    relation so the domino pass can ask "was this kept event influenced by an
    event we are about to throw away?"
    """
    edges: set[tuple[str, str]] = set()
    for edge in trace.influence:
        producer = source_producer(trace, edge.source_id)
        if producer and producer != edge.target_event:
            edges.add((producer, edge.target_event))
    return edges


def influence_children(trace: Trace) -> dict[str, set[str]]:
    children: dict[str, set[str]] = {e.id: set() for e in trace.events}
    for src, dst in event_influence_edges(trace):
        children.setdefault(src, set()).add(dst)
    return children


def influence_parents(trace: Trace) -> dict[str, set[str]]:
    parents: dict[str, set[str]] = {e.id: set() for e in trace.events}
    for src, dst in event_influence_edges(trace):
        parents.setdefault(dst, set()).add(src)
    return parents


def causal_past(trace: Trace, event_id: str) -> set[str]:
    """Influence-ancestors of `event_id`, plus itself.

    Temporal parents are deliberately excluded. A later clean event of the
    same agent is allowed to have a tainted sibling in its chronological
    past; that is the orphan the domino pass exists to catch, and folding
    chronology in here would make every post-taint checkpoint look unsafe
    and collapse Step 1 to B1.
    """
    parents = influence_parents(trace)
    seen: set[str] = {event_id}
    stack = [event_id]
    while stack:
        current = stack.pop()
        for pred in parents.get(current, ()):
            if pred not in seen:
                seen.add(pred)
                stack.append(pred)
    return seen


def influence_descendants(trace: Trace, event_id: str) -> set[str]:
    children = influence_children(trace)
    seen: set[str] = set()
    stack = [event_id]
    while stack:
        current = stack.pop()
        for nxt in children.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# --- Step 0 ----------------------------------------------------------------


def contamination_closure(
    trace: Trace,
    malicious: Iterable[str],
    policy: Policy | None = None,
) -> ContaminatedRegion:
    """Step 0. Thin wrapper so the algorithm's name appears in this folder."""
    return contaminate(trace, malicious, policy=policy)


# --- Step 1: Safe Frontier -------------------------------------------------


def _position(order: list[str]) -> dict[str, int]:
    return {eid: i for i, eid in enumerate(order)}


def _agent_checkpoints(
    checkpoints: list[Checkpoint], agent_id: str, position: dict[str, int]
) -> list[Checkpoint]:
    found = [c for c in checkpoints if c.agent_id == agent_id and c.event_id in position]
    found.sort(key=lambda c: position[c.event_id])
    return found


def initial_frontier(
    trace: Trace,
    checkpoints: list[Checkpoint],
    taint: set[str],
    order: list[str],
) -> dict[str, Checkpoint | None]:
    """Per agent: latest checkpoint whose causal past is disjoint from Taint.

    None means INIT -- no such checkpoint, so this agent restarts from the
    beginning.
    """
    position = _position(order)
    frontiers: dict[str, Checkpoint | None] = {}
    for agent in trace.agents():
        chosen: Checkpoint | None = INIT
        for checkpoint in _agent_checkpoints(checkpoints, agent, position):
            if causal_past(trace, checkpoint.event_id) & taint:
                continue
            chosen = checkpoint
        frontiers[agent] = chosen
    return frontiers


def _on_clean_side(
    event_id: str, frontier: Checkpoint | None, position: dict[str, int]
) -> bool:
    """An event is on the clean side of c(a) if it is at or before the
    checkpoint event. INIT has an empty clean side: nothing was verified."""
    if frontier is INIT:
        return False
    return position.get(event_id, -1) <= position.get(frontier.event_id, -1)


def _on_tainted_side(
    event_id: str, frontier: Checkpoint | None, position: dict[str, int]
) -> bool:
    if frontier is INIT:
        return True
    return position.get(event_id, -1) > position.get(frontier.event_id, -1)


def _pull_back(
    agent: str,
    past_event: str,
    checkpoints: list[Checkpoint],
    taint: set[str],
    order: list[str],
    trace: Trace,
) -> Checkpoint | None:
    """Latest checkpoint of `agent` strictly before `past_event` that is
    still causally clean. INIT if none exists."""
    position = _position(order)
    cutoff = position.get(past_event, -1)
    chosen: Checkpoint | None = INIT
    for checkpoint in _agent_checkpoints(checkpoints, agent, position):
        if position[checkpoint.event_id] >= cutoff:
            break
        if causal_past(trace, checkpoint.event_id) & taint:
            continue
        chosen = checkpoint
    return chosen


def propagate_domino(
    trace: Trace,
    frontiers: dict[str, Checkpoint | None],
    checkpoints: list[Checkpoint],
    taint: set[str],
    order: list[str],
) -> dict[str, Checkpoint | None]:
    """Cross-agent consistency: an event we kept must not have been
    influenced by an event we are going to throw away.

    This is the pass `safe_checkpoint_for()` never did. Without it, a
    checkpoint whose own event is clean can still carry, in its payload,
    an earlier event of the same agent that was influenced by another
    agent's tainted side -- an orphan. Resuming from that checkpoint
    would restore tainted state.

    Bounded by |V| iterations: each pass pulls at least one frontier
    strictly backward (or does nothing), and a frontier has at most
    |V| positions to retreat through. The bound is an assertion, not a
    comment -- a cycle here would mean the pull is not monotone.
    """
    position = _position(order)
    edges = event_influence_edges(trace)
    current = dict(frontiers)
    n = len(trace.events)

    for iteration in range(n + 1):
        if iteration == n:
            raise AssertionError(
                f"domino propagation exceeded |V|={n} iterations; "
                "a pull is not moving strictly backward"
            )
        changed = False
        for agent_b in list(current):
            for event in trace.by_agent(agent_b):
                if not _on_clean_side(event.id, current[agent_b], position):
                    continue
                for src, dst in edges:
                    if dst != event.id:
                        continue
                    if src not in {e.id for e in trace.events}:
                        continue
                    agent_a = trace.event(src).agent_id
                    if agent_a == agent_b:
                        continue
                    other = current.get(agent_a, INIT)
                    if not _on_tainted_side(src, other, position):
                        continue
                    pulled = _pull_back(
                        agent_b, event.id, checkpoints, taint, order, trace
                    )
                    if _frontier_id(pulled) != _frontier_id(current[agent_b]):
                        current[agent_b] = pulled
                        changed = True
        if not changed:
            return current
    return current


def _frontier_id(checkpoint: Checkpoint | None) -> str:
    return "INIT" if checkpoint is INIT else checkpoint.id


def safe_frontier(
    trace: Trace,
    checkpoints: list[Checkpoint],
    taint: set[str],
    order: list[str] | None = None,
) -> dict[str, Checkpoint | None]:
    """Step 1. Verified per-agent recovery line, then the domino pass."""
    order = order or EventGraph.from_trace(trace).topological_order()
    first = initial_frontier(trace, checkpoints, taint, order)
    return propagate_domino(trace, first, checkpoints, taint, order)


# --- Step 2: greedy action selection ---------------------------------------


@dataclass
class InfluencePath:
    """One MaliciousSource -> FinalOutput walk through the tainted subgraph."""

    source_id: str
    events: tuple[str, ...]
    # event->event edges along the walk, for isolate()
    edges: tuple[tuple[str, str], ...] = ()

    def nodes(self) -> tuple[str, ...]:
        return self.events


def _tainted_successors(
    trace: Trace, taint: set[str]
) -> dict[str, set[str]]:
    """Influence children, restricted to tainted events.

    Paths are "through the tainted subgraph only" -- walking a clean event
    would mean the path had already left the region we are cutting.
    """
    children = influence_children(trace)
    return {
        eid: {c for c in kids if c in taint}
        for eid, kids in children.items()
    }


def malicious_to_output_paths(
    trace: Trace,
    malicious: Iterable[str],
    taint: set[str],
    finals: list[str] | None = None,
) -> list[InfluencePath]:
    """All source -> final-output paths that stay inside Taint."""
    if finals is None:
        finals = [
            e.id
            for e in trace.events
            if e.agent_id == "executor" and e.kind == "agent_output"
        ]
        # Do not fall back to "every leaf". On a hand-constructed graph, or
        # a partial trace, that would treat a tainted intermediate as a
        # FinalOutput and force a cut we do not need. No executor output
        # means no path to the task result, which is the exposed-only win.
    live_finals = [f for f in finals if f in taint]
    if not live_finals:
        return []

    wraps: dict[str, list[str]] = {}
    for source in trace.sources:
        producer = source.derived_from or source.origin_event
        if producer:
            wraps.setdefault(source.id, []).append(producer)

    # Seed events: those the malicious sources influenced, if tainted,
    # plus the origin event of a malicious source when that origin is tainted.
    seeds: set[str] = set()
    for sid in malicious:
        for edge in trace.influence:
            if edge.source_id == sid and edge.target_event in taint:
                seeds.add(edge.target_event)
        source = trace.source(sid)
        if source.origin_event and source.origin_event in taint:
            seeds.add(source.origin_event)

    children = _tainted_successors(trace, taint)
    paths: list[InfluencePath] = []

    def walk(source_id: str, node: str, chain: list[str], edges: list[tuple[str, str]]) -> None:
        if node in chain[:-1]:
            return
        if node in live_finals:
            paths.append(InfluencePath(source_id, tuple(chain), tuple(edges)))
            # Keep walking: a final may have tainted descendants (unusual)
            # but we already recorded a complete path.
        for nxt in sorted(children.get(node, ())):
            walk(source_id, nxt, chain + [nxt], edges + [(node, nxt)])

    for sid in malicious:
        for start in sorted(seeds):
            # Only start a path at an event this source actually reached,
            # otherwise every seed is attributed to every source.
            reached = any(
                e.source_id == sid and e.target_event == start
                for e in trace.influence
            ) or (trace.source(sid).origin_event == start)
            if not reached:
                # Also allow a start that is a tainted descendant of an
                # event this source influenced -- the walk from the
                # influenced event covers that. Skip here.
                continue
            walk(sid, start, [start], [])

    # Deduplicate identical event sequences.
    seen: set[tuple[str, tuple[str, ...]]] = set()
    unique: list[InfluencePath] = []
    for path in paths:
        key = (path.source_id, path.events)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _outgoing_edges(trace: Trace, agent_id: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for src, dst in event_influence_edges(trace):
        if trace.event(src).agent_id == agent_id and trace.event(dst).agent_id != agent_id:
            out.append((src, dst))
    return out


def candidate_actions(
    trace: Trace,
    taint: set[str],
    frontiers: dict[str, Checkpoint | None],
    order: list[str],
) -> list[Action]:
    """The action vocabulary, instantiated for this incident.

    WHY EVERY TAINTED EVENT IS OFFERED, INCLUDING THE ONES A REPLAY CANNOT
    CHANGE
    ----------------------------------------------------------------------
    An action breaks a MaliciousSource -> FinalOutput path by recomputing a
    node on it without the malicious source, which works because
    `SplicingClient` redacts flagged sources from a prompt before re-issuing it
    (`redact_flagged` in src/recovery/replay.py). That mechanism exists only
    for events that made a model call: tool calls, tool responses, memory
    operations and the Executor's comparison are recomputed by pipeline code as
    a pure function of unchanged upstream, so re-running one reproduces it
    exactly.

    So the vocabulary contains actions that cannot really cut anything, and
    once the Coder->Executor edge existed and real paths appeared, the greedy's
    cheapest option on every influencing scenario became `invalidate(e0019)` --
    the Executor's final comparison, cost 1 because it spends no tokens,
    sitting at the end of every path. Verification rejects those plans and the
    run escalates.

    Filtering the vocabulary down to model-written events was tried and is NOT
    what this does, because it made results worse rather than better: with the
    cheap cut removed the greedy exceeds its `restart_all` cost cap on traces
    where taint is wide, and falls back to a full restart. Two configurations
    regressed to restart_all that had previously produced a selective plan.

    The real disagreement is one level up and is recorded in
    docs/06-limitations.md: Step 2 optimises "cut every source -> output path"
    while Step 4 verifies "Taint(new_graph) is empty", and those are not the
    same condition. A tainted event that sits on no source -> output path
    satisfies the first and fails the second. Reconciling them is a design
    decision about what Step 2 should optimise, not a filter, and it is not
    made here.
    """
    actions: list[Action] = []
    full = restart_all_cost(trace)
    actions.append(restart_all_action(trace))

    for eid in sorted(taint):
        actions.append(replay_action(trace, eid))
        actions.append(invalidate_action(trace, eid, influence_descendants(trace, eid)))

    for agent in trace.agents():
        if agent == "user":
            continue
        frontier = frontiers.get(agent, INIT)
        after = None if frontier is INIT else frontier.event_id
        actions.append(restart_action(trace, agent, after, order))
        outgoing = _outgoing_edges(trace, agent)
        if outgoing:
            actions.append(isolate_action(trace, agent, outgoing, full))
    return actions


def greedy_cover(
    paths: list[InfluencePath],
    actions: list[Action],
    cap: int,
    committed_analysis: int = 0,
    count_sunk_analysis: bool = False,
) -> list[Action]:
    """Greedy set cover: min cost(a) / paths_broken(a), cap at restart_all.

    Actions that break nothing are ignored. Ties break by lower cost, then
    by label, so the choice is deterministic across runs.

    ON COUNTING THE ANALYSIS IN THIS CAP (D-088)
    ---------------------------------------------
    The cap compares accumulated *replay* cost against `restart_all_cost`, and
    the investigation's cost `A` -- the dominant term, measured at 1.18 x N on
    the local frontier -- is not in it. That looks like an omission, and it was
    raised as one. It is not, and the arithmetic says why.

    By the time this function runs, `A` is spent. It is in both arms of the
    choice and cancels:

        finish selectively : A + spent + best.cost
        restart now        : A + restart_all_cost

    So the comparison that decides correctly is the one already here, without
    `A`. Adding `A` to the left side only makes the gate fire earlier, and
    firing earlier converts a cheap selective replay into a full restart. On
    the campaign's own numbers (A=6489, N=5520, selective=2153) that turns
    8642 tokens into 12009: **3367 tokens worse per run, and never better.**
    A sunk cost cannot be saved by spending more.

    `count_sunk_analysis` exists so that claim is measurable rather than
    asserted, and is **off by default** because the measurement says it should
    be. The place where `A` can still be avoided is *before* it is spent --
    `economics.ex_ante_decision` and the SPRT hypotheses D-087 now derives from
    the cost model -- not here.

    `committed_analysis` is recorded either way, so the total cost of a plan is
    visible without changing what the plan is.
    """
    remaining = set(range(len(paths)))
    selected: list[Action] = []
    spent = 0

    def broken_by(action: Action, pool: set[int]) -> set[int]:
        hits: set[int] = set()
        for i in pool:
            if action.breaks_path(paths[i].events, paths[i].edges):
                hits.add(i)
        return hits

    while remaining:
        best: Action | None = None
        best_hits: set[int] = set()
        best_score = float("inf")
        for action in actions:
            hits = broken_by(action, remaining)
            if not hits:
                continue
            score = action.cost / len(hits)
            if score < best_score or (
                score == best_score
                and best is not None
                and (action.cost, action.label()) < (best.cost, best.label())
            ):
                best = action
                best_hits = hits
                best_score = score
        if best is None:
            # No remaining action breaks a leftover path -- should not
            # happen while restart_all is in the set. Fall back to it.
            full = next(a for a in actions if a.kind == "restart_all")
            return [full]
        sunk = committed_analysis if count_sunk_analysis else 0
        if sunk + spent + best.cost > cap and best.kind != "restart_all":
            full = next(a for a in actions if a.kind == "restart_all")
            return [full]
        selected.append(best)
        spent += best.cost
        remaining -= best_hits
        if best.kind == "restart_all":
            break
    return selected


# --- the plan --------------------------------------------------------------


@dataclass
class RecoveryPlan:
    """What Steps 0–2 decided, ready for replay."""

    taint: ContaminatedRegion
    frontiers: dict[str, Checkpoint | None]
    paths: list[InfluencePath]
    selected: list[Action]
    invalidation_set: frozenset[str]
    restart_all: bool
    analysis_note: str = ""
    reasons: dict[str, str] = field(default_factory=dict)

    def frontier_summary(self) -> dict[str, str]:
        return {
            agent: ("INIT" if cp is INIT else f"{cp.id} after {cp.event_id}")
            for agent, cp in self.frontiers.items()
        }

    def describe(self) -> str:
        lines = [
            f"taint      {sorted(self.taint.events)}",
            f"  precautionary {sorted(self.taint.precautionary)}",
            f"frontiers  {self.frontier_summary()}",
            f"paths      {len(self.paths)} malicious-source -> final-output",
            f"selected   {[a.label() for a in self.selected]}",
            f"invalidate {sorted(self.invalidation_set)}",
            f"restart_all {self.restart_all}",
        ]
        return "\n".join(lines)


def plan_recovery(
    trace: Trace,
    malicious: Iterable[str],
    checkpoints: list[Checkpoint] | None = None,
    policy: Policy | None = None,
    count_sunk_analysis: bool = False,
) -> RecoveryPlan:
    """Steps 0, 1 and 2, in order. Does not replay anything."""
    checkpoints = checkpoints or []
    taint = contamination_closure(trace, malicious, policy=policy)
    order = EventGraph.from_trace(trace).topological_order()
    frontiers = safe_frontier(trace, checkpoints, set(taint.events), order)
    paths = malicious_to_output_paths(trace, malicious, set(taint.events))
    actions = candidate_actions(trace, set(taint.events), frontiers, order)
    cap = restart_all_cost(trace)
    # Already spent investigating this trace, read off its own usage records.
    # Recorded on the plan either way; only counted in the cap when the caller
    # asks for it, for the reason in `greedy_cover`'s docstring (D-088).
    committed_analysis = trace.analysis_tokens()

    # STEP 2'S COVERING TARGET IS THE CONTAMINATION CLOSURE (D-047)
    # ------------------------------------------------------------
    # Step 2 used to minimise cost subject to "cut every MaliciousSource ->
    # FinalOutput path", while Step 4 accepts a recovery only when
    # `Taint(new_graph)` is empty. Those are different conditions: a tainted
    # event lying on no source -> output path satisfies the first and fails
    # the second, so Step 2 would leave it alone to save cost and Step 4 would
    # correctly reject the plan. The run then escalated.
    #
    # This was invisible until the Coder->Executor bridge existed, because
    # `malicious_to_output_paths()` returned nothing on every trace, a
    # "treat each tainted event as a sink" fallback fired unconditionally, and
    # the cover satisfied Step 4 by accident. With real paths the two came
    # apart at once: A-influencing/oracle went from no escalation at 67.8%
    # work preserved to agent_restart at 15.8%.
    #
    # The fix is to make Step 2 optimise what Step 4 checks. Step 4 does not
    # move -- it is the safety floor. The alternative, narrowing Step 4 to
    # Step 1's old target, would mean accepting that genuinely tainted state
    # can survive a "successful" recovery whenever it does not happen to feed
    # the current output. That is exactly the silent residual risk this
    # project refuses everywhere else.
    #
    # Mechanically: one singleton path per tainted event. `breaks_path()` on a
    # singleton is true iff the event is in the action's `invalidates`, so
    # "cover every singleton" is literally "invalidation_set covers Taint",
    # which is Step 4's condition. `greedy_cover` and its cost function are
    # untouched -- only the thing being covered changed.
    #
    # `paths` is still computed and still reported on the plan: it is a real
    # provenance result and the summary line quotes it. It is no longer what
    # Step 2 optimises against.
    cover_targets = [
        InfluencePath(source_id="taint", events=(eid,), edges=())
        for eid in sorted(taint.events)
    ]
    if not cover_targets:
        # Truly exposed-only: Taint is empty. Nothing to cut, nothing to redo.
        selected: list[Action] = []
        invalidation: set[str] = set()
    else:
        selected = greedy_cover(
            cover_targets, actions, cap,
            committed_analysis=committed_analysis,
            count_sunk_analysis=count_sunk_analysis,
        )
        invalidation = set()
        for action in selected:
            invalidation |= action.invalidates

    return RecoveryPlan(
        taint=taint,
        frontiers=frontiers,
        paths=paths,
        selected=selected,
        invalidation_set=frozenset(invalidation),
        restart_all=any(a.kind == "restart_all" for a in selected),
        reasons=dict(taint.reasons),
    )


if __name__ == "__main__":
    import sys

    from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for
    from src.tracing.logger import read_trace

    path = sys.argv[1] if len(sys.argv) > 1 else "data/runs/fake.jsonl"
    seeds = sys.argv[2:] or [
        s.id for s in read_trace(path).sources if getattr(s, "malicious", False)
    ]
    # Recovery must not read Source.malicious. If the caller did not pass
    # seeds, we still refuse to invent them from the label -- fail loud.
    if not seeds:
        raise SystemExit(
            "pass the detector's flagged source ids; this module will not "
            "read Source.malicious"
        )

    # Re-read without using the label we peeked at above for the default.
    # The default is a CLI convenience and is the oracle; say so.
    trace = read_trace(path)
    if not sys.argv[2:] and any(s.malicious for s in trace.sources):
        seeds = [s.id for s in trace.sources if s.malicious]
        print("CLI defaulted seeds to planted labels (oracle). Not the method.")
    checkpoints = CheckpointStore.load(checkpoint_path_for(path))
    plan = plan_recovery(trace, seeds, checkpoints)
    print(f"trace {path}")
    print(plan.describe())
