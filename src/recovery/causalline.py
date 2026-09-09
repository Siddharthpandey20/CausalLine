"""
CausalLine: plan, selective replay, verify, escalate.

This is the method. It consumes a detector verdict and a trace; it never
reads `Source.malicious`. Ground truth stays in src/eval/.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.provenance.contamination import Policy, contaminate
from src.recovery.planner import RecoveryPlan, plan_recovery
from src.recovery.replay import ReplayReport, replay
from src.recovery.verify import (
    Escalation,
    VerifyResult,
    invalidation_for_scope,
    live_memory_points_at_invalidated,
    next_scope,
)
from src.tracing.checkpoints import Checkpoint, CheckpointStore, checkpoint_path_for
from src.tracing.graphs import CallGraph
from src.tracing.logger import Trace, read_trace
from src.tracing.tools import Tools


def _compromised_and_downstream(trace: Trace, flagged: Iterable[str]) -> set[str]:
    """Events B1 would discard: the entry agents and everyone downstream.

    Used as the agent_restart escalation scope, not as the selective plan.
    """
    flagged_set = set(flagged)
    entries = {
        s.origin_event
        for s in trace.sources
        if s.id in flagged_set and s.origin_event and trace.has_event(s.origin_event)
    }
    agents = {trace.event(eid).agent_id for eid in entries}
    call = CallGraph.from_trace(trace)
    downstream: set[str] = set()
    for agent in agents:
        downstream |= call.reachable_from(agent)
    affected = agents | downstream
    return {e.id for e in trace.events if e.agent_id in affected}


def _post_recovery_region(
    original: Trace,
    recovered: Trace,
    report: ReplayReport,
    flagged: Iterable[str],
):
    """Taint the recovered graph using original evidence, updated for replay.

    The recovered pipeline re-logs exposures, including the poisoned source
    (the tool still returns it). It does not, by default, re-attribute.
    Walking that trace with the conservative fallback would re-taint every
    spliced event that merely saw the source -- the D-024 defect, reintroduced
    by recovery. So the walk is given:

      * original checks and edges for every event we spliced (same bytes,
        same influence)
      * a `clean` clearance for every flagged source on a replayed event
        (the source was redacted from the prompt we actually issued)

    Events the splicing client never saw (tools, memory) are treated as
    spliced: they are deterministic and were not re-invoked at the model.
    """
    flagged_set = set(flagged) & {s.id for s in recovered.sources}
    replayed = set(report.replayed)
    spliced = set(report.spliced)
    checked: set[tuple[str, str]] = set()
    influence: set[tuple[str, str]] = set()

    orig_ids = [e.id for e in original.events]
    rec_ids = [e.id for e in recovered.events]
    # Same pipeline, same order. A length mismatch means replay diverged
    # and verification should not invent pairings.
    if len(orig_ids) != len(rec_ids):
        return contaminate(recovered, flagged_set)

    remap = dict(zip(orig_ids, rec_ids))
    orig_cleared = {
        c.pair for c in original.checks if c.verdict == "clean"
    }
    orig_edges = {(e.source_id, e.target_event) for e in original.influence}

    for old_id, new_id in remap.items():
        if old_id in replayed:
            for sid in recovered.event(new_id).exposures:
                if sid in flagged_set:
                    checked.add((sid, new_id))
            for sid, target in orig_edges:
                if target == old_id and sid not in flagged_set:
                    influence.add((sid, new_id))
            continue

        for sid, target in orig_cleared:
            if target == old_id:
                checked.add((sid, new_id))
        for sid, target in orig_edges:
            if target != old_id:
                continue
            # Spliced model events are byte-identical, so their edges stand.
            # Pipeline-computed events (handoffs, tool calls) were rebuilt
            # from whatever upstream we just spliced or replayed: an edge
            # from a flagged source is the old run's, and keeping it would
            # re-taint a message that now carries recomputed findings.
            if sid in flagged_set and old_id not in spliced:
                continue
            influence.add((sid, new_id))
        if old_id not in spliced:
            for sid in recovered.event(new_id).exposures:
                if sid in flagged_set:
                    checked.add((sid, new_id))

    return contaminate(
        recovered, flagged_set, influence=influence, checked=checked
    )


@dataclass
class RecoveryResult:
    """One CausalLine run, including any escalation."""

    plan: RecoveryPlan
    report: ReplayReport | None
    verification: VerifyResult
    scope: Escalation
    escalations: int
    recovered_path: Path | None
    task_success: bool
    analysis_tokens: int
    replay_tokens: int
    recovery_tokens: int
    wall_clock_s: float
    blast_radius_events: int
    blast_radius_agents: int
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verification.ok


def recover(
    original: Trace,
    flagged: Iterable[str],
    client: Any,
    out_path: str | Path,
    tools: Tools | None = None,
    checkpoints: list[Checkpoint] | None = None,
    policy: Policy | None = None,
    attributor: Any = None,
    handoff_hook: Any = None,
    max_escalations: int = 2,
) -> RecoveryResult:
    """Plan, replay, verify; widen the invalidation set on failure."""
    started = time.time()
    flagged_list = list(flagged)
    if checkpoints is None and original.path is not None:
        checkpoints = CheckpointStore.load(checkpoint_path_for(original.path))
    checkpoints = checkpoints or []

    plan = plan_recovery(original, flagged_list, checkpoints, policy=policy)
    agent_scope = _compromised_and_downstream(original, flagged_list)
    all_events = {e.id for e in original.events}

    analysis_tokens = original.analysis_tokens()
    initial_memory = dict(tools.memory) if tools is not None else {}
    scope: Escalation = "selective"
    escalations = 0
    last_verify = VerifyResult(ok=False, reasons=["recovery did not run"])
    last_report: ReplayReport | None = None
    recovered_path: Path | None = None
    task_success = False
    notes: list[str] = []

    # `escalations` counts WIDENINGS ACTUALLY REPLAYED, and is bounded by
    # max_escalations. The budget check is at the bottom of the loop, not in
    # this guard, and that placement is the fix rather than an accident:
    #
    #   was:  while scope != "exhausted" and escalations <= max_escalations:
    #             ... replay ...
    #             scope = next_scope(scope); escalations += 1
    #
    # `next_scope("restart_all")` is `"exhausted"`, which is a terminal marker
    # and not a scope anything is replayed at. The old loop incremented on that
    # transition too, so a run that exhausted the ladder reported
    # `escalations=3` against `max_escalations=2` -- three counted, two
    # performed. Every blind-detector cell in the campaign printed 3.
    #
    # Moving the guard to `<` in the header, which is the obvious-looking fix,
    # is worse: with max=2 it stops after agent_restart and `restart_all` --
    # the strongest recovery the ladder has -- becomes unreachable. So the
    # count is fixed where the count was wrong, and the ladder still reaches
    # its top rung.
    while scope != "exhausted":
        invalidation = invalidation_for_scope(
            scope, plan.invalidation_set, agent_scope, all_events
        )
        target = Path(out_path)
        if escalations:
            target = target.with_name(f"{target.stem}-esc{escalations}{target.suffix}")

        if tools is not None:
            tools.memory.clear()
            tools.memory.update(initial_memory)
        result, report = replay(
            original,
            invalidation,
            client,
            target,
            tools=tools,
            flagged=flagged_list,
            attributor=attributor,
            handoff_hook=handoff_hook,
        )
        recovered = read_trace(result.trace_path)
        region = _post_recovery_region(original, recovered, report, flagged_list)
        refs = live_memory_points_at_invalidated(
            recovered,
            invalidation,
            dict(tools.memory) if tools is not None else {},
            original=original,
        )
        reasons: list[str] = []
        if region.events:
            reasons.append("Taint(new_graph) is non-empty")
        if refs:
            reasons.append("referential inconsistency")
        if not result.task_success:
            reasons.append("task-level check failed")
        last_verify = VerifyResult(
            ok=not reasons,
            tainted_events=frozenset(region.events),
            referential_failures=refs,
            task_success=result.task_success,
            reasons=reasons,
        )

        last_report = report
        recovered_path = result.trace_path
        task_success = result.task_success

        if last_verify.ok:
            notes.append(f"succeeded at scope={scope}")
            break
        notes.append(f"verify failed at scope={scope}: {last_verify.reasons}")
        if escalations >= max_escalations:
            notes.append(
                f"escalation budget spent ({escalations}/{max_escalations}); "
                f"stopping at scope={scope}"
            )
            scope = "exhausted"
            break
        scope = next_scope(scope)
        escalations += 1

    replay_tokens = last_report.replay_tokens if last_report else 0
    invalidation_final = invalidation_for_scope(
        "selective" if last_verify.ok and escalations == 0 else scope,
        plan.invalidation_set,
        agent_scope,
        all_events,
    )
    if last_verify.ok and escalations == 0:
        invalidation_final = set(plan.invalidation_set)
    elif last_report is not None:
        invalidation_final = set(last_report.replayed)
    agents_hit = {original.event(eid).agent_id for eid in invalidation_final if original.has_event(eid)}

    return RecoveryResult(
        plan=plan,
        report=last_report,
        verification=last_verify,
        scope=scope if last_verify.ok else (scope if scope == "exhausted" else scope),
        escalations=escalations if not last_verify.ok else max(0, escalations),
        recovered_path=recovered_path,
        task_success=task_success,
        analysis_tokens=analysis_tokens,
        replay_tokens=replay_tokens,
        recovery_tokens=analysis_tokens + replay_tokens,
        wall_clock_s=time.time() - started,
        blast_radius_events=len(invalidation_final),
        blast_radius_agents=len(agents_hit - {"user"}),
        notes=notes,
    )
