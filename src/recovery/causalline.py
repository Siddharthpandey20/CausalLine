"""
CausalLine: plan, selective replay, verify, escalate.

This is the method. It consumes a detector verdict and a trace; it never
reads `Source.malicious`. Ground truth stays in src/eval/.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.provenance.checks import CheckLedger
from src.provenance.contamination import Policy, contaminate
from src.provenance.upstream import investigation_candidates
from src.recovery.planner import RecoveryPlan, plan_recovery
from src.recovery.replay import ReplayReport, replay
from src.recovery.verify import (
    Escalation,
    VerifyResult,
    invalidation_for_scope,
    next_scope,
    original_task_success,
    memory_writes,
    selective_memory_rollback,
    surviving_payload,
    verify,
)
from src.tracing.checkpoints import (
    Checkpoint,
    CheckpointStore,
    checkpoint_path_for,
    gc_checkpoints,
    recovery_mode_for,
)
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

    THE CLEARANCE THIS NO LONGER GRANTS ON TRUST (D-068)
    ----------------------------------------------------
    The clearance for a flagged source on a replayed event used to rest on the
    fact that `redact_flagged()` had been called. That is verification believing
    its own plan. It is now conditional on `surviving_payload()`: the recovered
    prompt, output and tool arguments are read back and the clearance is
    withheld if the flagged source's material is still in any of them. A
    withheld clearance leaves the pair contaminated, verification fails, and the
    run escalates -- which is what should happen when a recovery did not
    actually remove the thing it was recovering from.

    Returns the region and the re-check failures, so the failures can be named
    in the verification result rather than showing up only as leftover taint.
    """
    flagged_set = set(flagged) & {s.id for s in recovered.sources}
    replayed = set(report.replayed)
    spliced = set(report.spliced)
    checked: set[tuple[str, str]] = set()
    influence: set[tuple[str, str]] = set()
    recheck_failures: list[str] = []

    orig_ids = [e.id for e in original.events]
    rec_ids = [e.id for e in recovered.events]
    # Same pipeline, same order. A length mismatch means replay diverged
    # and verification should not invent pairings.
    if len(orig_ids) != len(rec_ids):
        return contaminate(recovered, flagged_set), recheck_failures

    remap = dict(zip(orig_ids, rec_ids))
    # The original's clearances, read under the SAME policy the walk uses
    # rather than off the raw verdict. A `clean` the policy refuses -- a
    # self-report, or an inherited verdict resting on nothing (D-067) -- must
    # not become a clearance here just because verification reads the field
    # directly.
    orig_cleared = CheckLedger.from_trace(original).cleared_pairs()
    orig_edges = {(e.source_id, e.target_event) for e in original.influence}

    for old_id, new_id in remap.items():
        if old_id in replayed:
            for sid in recovered.event(new_id).exposures:
                if sid not in flagged_set:
                    continue
                failures = surviving_payload(
                    recovered, new_id, sid, report.issued_prompts.get(old_id)
                )
                if failures:
                    recheck_failures.extend(failures)
                    continue
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
                if sid not in flagged_set:
                    continue
                failures = surviving_payload(recovered, new_id, sid)
                if failures:
                    recheck_failures.extend(failures)
                    continue
                checked.add((sid, new_id))

    return (
        contaminate(recovered, flagged_set, influence=influence, checked=checked),
        recheck_failures,
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
    # Every event this recovery actually recomputed -- the set handed to the
    # last replay attempt. THE field to score work preserved on, and the same
    # definition the baselines are scored under. See ReplayReport.invalidation.
    invalidated: frozenset[str]
    task_success: bool
    analysis_tokens: int
    replay_tokens: int
    recovery_tokens: int
    wall_clock_s: float
    blast_radius_events: int
    blast_radius_agents: int
    notes: list[str] = field(default_factory=list)
    # {key -> value to restore, or None to delete} for a live memory store.
    # Only the writes this incident contaminated; clean writes are absent
    # because they survive (D-071).
    memory_rollback: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verification.ok

    @property
    def pre_existing_task_failure(self) -> bool:
        """Verification failed the task check on a run that was already failing it.

        Not an excuse -- the verdict stands (D-069) -- but the one fact a method
        comparison needs in order to be fair: B0, B1 and B2 do not verify, so
        they are never charged for a failure that predates recovery, and a table
        that does not say which rows are in this state is comparing two
        different things.
        """
        return self.verification.pre_existing_task_failure


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

    # --- Phase 3a: checkpoint lifecycle, on the path that actually runs ------
    # `gc_checkpoints()` was built and tested (20 tests) and called by no
    # experiment. Run it here, before planning, so the frontier is chosen over
    # the checkpoints a real deployment would still be holding rather than over
    # every checkpoint ever written.
    #
    # Dropping a checkpoint cannot make recovery less safe: GC only removes a
    # checkpoint that a *later* confirmed-clean one dominates, and it never
    # removes an agent's most recent one. If nothing has been examined, nothing
    # is dropped -- which is what happens on a lightly-analysed trace, and is
    # the conservative direction.
    gc = gc_checkpoints(checkpoints, original)
    checkpoints = gc.retained
    notes: list[str] = []
    if gc.deleted:
        notes.append(
            f"checkpoint GC dropped {len(gc.deleted)} of "
            f"{len(gc.deleted) + len(gc.retained)} checkpoints "
            f"({gc.bytes_freed}B freed, {gc.bytes_retained}B retained)"
        )

    # --- Phase 4: where did the flagged source itself come from? -----------
    # Surfaced, never acted on. The detector decides what is malicious; a
    # backward walk follows "was an input to", which is exposure rather than
    # influence, so promoting its output to a seed would re-import exactly the
    # conflation this method exists to remove. See src/provenance/upstream.py.
    upstream = investigation_candidates(original, flagged_list)
    if upstream.candidates:
        notes.append(f"upstream attribution: {upstream.summary()}")

    plan = plan_recovery(original, flagged_list, checkpoints, policy=policy)

    # --- Phase 3a: the recovery horizon -------------------------------------
    # An incident reaching back past the horizon cannot be replayed selectively
    # -- the prompts behind it have been downgraded to their hashes -- so the
    # scope widens to a coarse agent restart rather than a selective plan that
    # would silently skip the events it cannot re-issue.
    mode = recovery_mode_for(original, plan.invalidation_set)
    horizon_scope: set[str] | None = None
    if mode.mode == "coarse":
        horizon_scope = set(mode.invalidation)
        notes.append(f"recovery horizon: {mode.reason}")
    agent_scope = _compromised_and_downstream(original, flagged_list)
    all_events = {e.id for e in original.events}

    analysis_tokens = original.analysis_tokens()
    # Whether the run being recovered already did the task. Verification is
    # judged against this rather than against absolute success -- see D-069 and
    # docs/03 #15. True on every scripted run, so nothing in the scripted matrix
    # moves.
    started_successful = original_task_success(original)
    initial_memory = dict(tools.memory) if tools is not None else {}
    scope: Escalation = "selective"
    escalations = 0
    last_verify = VerifyResult(ok=False, reasons=["recovery did not run"])
    last_report: ReplayReport | None = None
    recovered_path: Path | None = None
    task_success = False

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
        if scope == "selective" and horizon_scope is not None:
            # The selective plan is not replayable at this horizon; use the
            # coarse fallback instead of a plan that cannot be carried out.
            invalidation = frozenset(horizon_scope)
        target = Path(out_path)
        if escalations:
            target = target.with_name(f"{target.stem}-esc{escalations}{target.suffix}")

        if tools is not None:
            # Wholesale, and deliberately so: the replay re-executes the whole
            # workflow from its starting state, so a surviving write would show
            # an early `memory_read` a value the original run never saw. The
            # per-write plan a *deployment* would apply to the live store is
            # computed after the replay instead -- see D-071 and
            # `selective_memory_rollback`.
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
        region, recheck_failures = _post_recovery_region(
            original, recovered, report, flagged_list
        )
        if recheck_failures:
            notes.append(
                f"independent re-check withheld {len(recheck_failures)} "
                f"clearance(s) at scope={scope}: {recheck_failures[0]}"
            )
        # One implementation, shared. This block used to reimplement
        # `verify()` inline and, in doing so, dropped its missing-flagged-source
        # warning: a flagged source absent from the recovered trace cannot seed
        # a walk, the walk comes back empty, and an empty walk is
        # indistinguishable from a clean recovery. The region is computed here
        # rather than inside verify() because the post-recovery walk needs the
        # splice/replay bookkeeping -- see `_post_recovery_region`.
        last_verify = verify(
            recovered,
            flagged_list,
            task_success=result.task_success,
            invalidated=invalidation,
            current_memory=dict(tools.memory) if tools is not None else None,
            original=original,
            region=region,
            recheck_failures=recheck_failures,
            original_task_success=started_successful,
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

    # --- Phase 7: what a live memory store would actually have to undo -----
    # Reported rather than applied. The replay's own fixture is reset wholesale
    # above for a reason that is about replay semantics, not about cost; this is
    # the answer to the different question a deployment asks, and it is the one
    # that distinguishes "roll back the contaminated write" from "roll back
    # everything since the checkpoint".
    memory_plan = selective_memory_rollback(
        original,
        last_report.invalidation if last_report else plan.invalidation_set,
        dict(tools.memory) if tools is not None else {},
        initial_memory,
    )
    if memory_plan:
        kept = {
            key for _e, key, _v in memory_writes(original)
        } - set(memory_plan)
        notes.append(
            f"selective memory rollback: {len(memory_plan)} key(s) to undo "
            f"{sorted(memory_plan)}, {len(kept)} clean write(s) kept {sorted(kept)}"
        )

    replay_tokens = last_report.replay_tokens if last_report else 0
    # What was actually recomputed: the set handed to the last replay attempt,
    # which after an escalation is the widened scope rather than the original
    # selective plan.
    #
    # This used to fall back to `last_report.replayed` on any escalation, which
    # holds only the events that made a model call -- 6 of 19 here. A
    # `restart_all` escalation redoes every event and was reported as having
    # redone six, so the blast radius of the most expensive recovery available
    # came out smaller than the blast radius of the cheapest.
    if last_report is not None:
        invalidation_final = set(last_report.invalidation)
    else:
        invalidation_final = set(plan.invalidation_set)
    agents_hit = {original.event(eid).agent_id for eid in invalidation_final if original.has_event(eid)}

    return RecoveryResult(
        plan=plan,
        report=last_report,
        verification=last_verify,
        scope=scope if last_verify.ok else (scope if scope == "exhausted" else scope),
        escalations=escalations if not last_verify.ok else max(0, escalations),
        recovered_path=recovered_path,
        invalidated=frozenset(invalidation_final),
        task_success=task_success,
        analysis_tokens=analysis_tokens,
        replay_tokens=replay_tokens,
        recovery_tokens=analysis_tokens + replay_tokens,
        wall_clock_s=time.time() - started,
        blast_radius_events=len(invalidation_final),
        blast_radius_agents=len(agents_hit - {"user"}),
        notes=notes,
        memory_rollback=memory_plan,
    )
