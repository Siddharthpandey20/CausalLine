"""
CausalLine Step 4: verification and progressive escalation.

    if Taint(new_graph) != ∅
       or referential inconsistency
       or task-level check fails:
        escalate: replay(e) -> restart(agent) -> restart_all()
        go to Step 3
    else:
        return SUCCESS with metrics

Taint is re-run on the post-recovery graph. Trusting that recovery worked
because we planned it is how a silent unsafe preservation happens.

Escalation is a state machine, not a novel design: retry at increasing
scope only after a verification failure. The pattern is standard
distributed rollback-recovery.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from src.provenance.contamination import contaminate
from src.tracing.checkpoints import Checkpoint, memory_rollback_plan
from src.tracing.logger import Trace

Escalation = Literal["selective", "agent_restart", "restart_all", "exhausted"]

ESCALATION_ORDER: tuple[Escalation, ...] = (
    "selective",
    "agent_restart",
    "restart_all",
    "exhausted",
)


@dataclass
class VerifyResult:
    """One verification pass over a recovered trace."""

    ok: bool
    tainted_events: frozenset[str] = frozenset()
    referential_failures: list[str] = field(default_factory=list)
    task_success: bool = False
    reasons: list[str] = field(default_factory=list)
    # Places the independent re-check found flagged material still present in
    # the recovered trace (D-068). Distinct from `tainted_events`: that is what
    # the walk inferred, this is what the bytes say.
    recheck_failures: list[str] = field(default_factory=list)
    # Did the run being recovered already pass the task check? Recorded so a
    # method comparison can exclude or annotate runs that were broken before
    # recovery began -- the three baselines never verify, so they are never
    # charged for a pre-existing failure and CausalLine is (docs/03 #15, D-069).
    original_task_success: bool = True

    @property
    def pre_existing_task_failure(self) -> bool:
        return (not self.task_success) and (not self.original_task_success)

    def describe(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"verify {status}"]
        if self.tainted_events:
            lines.append(f"  taint still live: {sorted(self.tainted_events)}")
        for item in self.referential_failures:
            lines.append(f"  referential: {item}")
        for item in self.recheck_failures:
            lines.append(f"  re-check: {item}")
        if not self.task_success:
            lines.append("  task-level check failed")
        for reason in self.reasons:
            lines.append(f"  {reason}")
        return "\n".join(lines)


def surviving_payload(
    recovered: Trace,
    event_id: str,
    source_id: str,
    issued_prompt: str | None = None,
) -> list[str]:
    """Is the flagged source's material still present at this recovered event?

    THE ASSUMPTION THIS REPLACES (D-068)
    ------------------------------------
    Post-recovery verification used to clear a replayed event of a flagged
    source on one ground: the source was redacted from the prompt we re-issued,
    therefore it cannot have influenced the new output. That is an argument, not
    a check, and it is the same argument the two measured false cleans both
    defeated -- one because the redaction did not remove everything, the other
    because the payload came back out of the model anyway.

    A verification that reasons from what recovery *intended* cannot catch a
    recovery that did something else. So this looks at the bytes instead:

      * the re-issued **prompt** -- does the source's material still reach the
        model by some route the redaction did not cut? (the e0014 class)
      * the recovered **output** and tool arguments -- did the material come out
        the other side regardless? (the e0013 class)

    Uses the same span extraction as the `carryover` facet and the removability
    check, so all three agree on what "this material is present" means.

    `issued_prompt` is the text the replay engine actually sent
    (`ReplayReport.issued_prompts`). It has to be passed in rather than read
    off the trace: redaction happens inside the splicing client, after the
    pipeline has already stored the prompt, so the stored copy is the
    un-redacted one and checking it would fail every successful recovery.
    Passing None means "no prompt to check" -- which is the right answer for a
    spliced event, whose bytes are the original's and whose verdict is
    inherited from the original's own examination.

    Returns the reasons it is not clear. Empty means the re-check passed, which
    is the only thing that licenses a clearance.
    """
    from src.provenance.signatures import distinctive_spans, spans_present_in

    if source_id not in {s.id for s in recovered.sources}:
        return []
    content = recovered.source(source_id).content
    spans = distinctive_spans(content)
    if not spans:
        return []

    reasons: list[str] = []
    prompt = issued_prompt or ""
    if prompt and spans_present_in(prompt, spans):
        reasons.append(
            f"{event_id}: the re-issued prompt still carries material from "
            f"{source_id}, so the redaction did not cut every route"
        )
    for label, text in (
        ("output", recovered.output_text(event_id) or ""),
        ("tool arguments", recovered.tool_args_text(event_id) or ""),
    ):
        if text and spans_present_in(text, spans):
            reasons.append(
                f"{event_id}: the recovered {label} still carries material from "
                f"{source_id}"
            )
    return reasons


def live_memory_points_at_invalidated(
    recovered: Trace,
    invalidated: Iterable[str],
    current_memory: dict[str, str],
    original: Trace | None = None,
) -> list[str]:
    """Live memory still holding a value an invalidated write produced.

    docs/02: "Memory writes made by contaminated events must be rolled
    back too." A recovered run that *re-executed* the write with new
    content is fine -- the store is pointing at the new object. Failure
    is when the live value is still the original invalidated bytes.
    Event ids in the recovered trace reuse the original numbering, so
    comparing writer ids would flag every re-done write as stale.
    """
    import json

    dead = set(invalidated)
    if original is None:
        return []
    failures: list[str] = []
    for event in original.events:
        if event.id not in dead or event.kind != "memory_write":
            continue
        raw = original.output_text(event.id)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        key = payload.get("key")
        value = payload.get("value")
        if key and key in current_memory and current_memory[key] == value:
            failures.append(
                f"memory[{key!r}] still holds the value invalidated write "
                f"{event.id} produced"
            )
    return failures


def verify(
    recovered: Trace,
    flagged: Iterable[str],
    task_success: bool,
    invalidated: Iterable[str] = (),
    current_memory: dict[str, str] | None = None,
    original: Trace | None = None,
    region: Any = None,
    recheck_failures: Iterable[str] = (),
    original_task_success: bool = True,
) -> VerifyResult:
    """Re-run Taint, check live references, check the task.

    `flagged` is the detector verdict, not ground truth. A missed source
    will leave taint the method was never told about; that is the
    detector's failure and is reported as leftover taint, not hidden.

    `region` lets a caller supply the post-recovery contamination walk it has
    already done. `recover()` does: a plain `contaminate()` over a recovered
    trace re-taints every spliced event that merely saw the flagged source,
    because the recovered run re-logs the exposure without re-attributing it --
    the D-024 defect, reintroduced by recovery. `_post_recovery_region()` in
    src/recovery/causalline.py does that walk properly and passes the result
    here.

    This parameter exists because `verify()` and `recover()` had grown two
    copies of this logic and only one of them had the missing-flagged-source
    warning below. Sharing the function keeps the warning on the live path;
    sharing the *walk* would have silently downgraded it.
    """
    reasons: list[str] = []
    known = {s.id for s in recovered.sources}
    seeds = [sid for sid in flagged if sid in known]
    missing = [sid for sid in flagged if sid not in known]
    if missing:
        reasons.append(
            f"flagged source(s) {missing} are absent from the recovered "
            "trace; they cannot seed a walk"
        )

    if region is None:
        region = contaminate(recovered, seeds) if seeds else None
    tainted = frozenset(region.events) if region else frozenset()

    refs: list[str] = []
    if current_memory is not None:
        refs = live_memory_points_at_invalidated(
            recovered, invalidated, current_memory, original
        )

    survived = list(recheck_failures)

    # THE TASK CHECK STAYS STRICT, AND THE REASON IS MEASURED (docs/03 #15, D-069)
    # -----------------------------------------------------------------------
    # docs/03 #15 recommended relaxing this to `task_success or not
    # original_task_success` -- recovery should be judged against where it
    # started, not against perfection -- and asserted the change would be "a
    # no-op on every existing measurement" because the scripted matrix always
    # succeeds at the task.
    #
    # **That assertion is false, and relaxing it costs safety.** In the scripted
    # matrix the A-influencing attack breaks the task *by working*, so the
    # original run's `task_success` is already False there. Under the relaxed
    # predicate, `tests/test_validation.py`'s lying-self-reporter condition went
    # from zero unsafe preservations to one: e0010 stayed preserved while truly
    # contaminated.
    #
    # The mechanism is worth stating, because it means the strictness is not
    # incidental. **The task check is an end-to-end detector of surviving
    # contamination.** When the estimator misses an influence edge, the taint
    # walk under-covers, the selective plan leaves the contaminated event in
    # place -- and the contamination then shows up in the output the task check
    # reads. Verification fails, the ladder climbs, and the unsafe preservation
    # is eliminated by a route that never had to identify it. Relaxing the
    # predicate removes that last line of defence exactly when the first one has
    # already failed.
    #
    # So the predicate does not move. docs/03 #15's real complaint -- that
    # CausalLine is charged for a pre-existing failure while B0, B1 and B2 are
    # exempt because they never verify -- is answered by its second option
    # instead: the condition is *recorded*, so a comparison can exclude or
    # annotate those runs rather than the safety rule being loosened for them.
    ok = (not tainted) and (not refs) and (not survived) and task_success
    if tainted:
        reasons.append("Taint(new_graph) is non-empty")
    if refs:
        reasons.append("referential inconsistency")
    if survived:
        # D-068. Named separately from leftover taint: "the walk reached this
        # event" and "we read the recovered bytes and the payload is still in
        # them" are different findings, and the second is the one that says the
        # recovery did not do what it reported doing.
        reasons.append(
            f"independent re-check found flagged material surviving in "
            f"{len(survived)} place(s): {survived[0]}"
        )
    if not task_success:
        reasons.append("task-level check failed")
    if not task_success and not original_task_success:
        # Recorded, not excused. A comparison against methods that never verify
        # can exclude or annotate this run; verification itself does not soften.
        reasons.append(
            "NOTE: the original run failed the task check too, so this failure "
            "is not evidence that recovery made anything worse (docs/03 #15)"
        )

    return VerifyResult(
        ok=ok,
        tainted_events=tainted,
        referential_failures=refs,
        task_success=task_success,
        reasons=reasons,
        recheck_failures=survived,
        original_task_success=original_task_success,
    )


def original_task_success(trace: Trace) -> bool:
    """Did the run being recovered already do the task?

    Read off the Executor's own comparison event, which stores
    `{"expected": ..., "produced": ..., "success": ...}` -- our own code's
    verdict, not an LLM's. Absent or unreadable means we cannot show the
    original was broken, so the strict rule applies: a missing record must not
    become a free pass.
    """
    import json as _json

    for event in reversed(trace.events):
        if event.agent_id != "executor" or event.kind != "agent_output":
            continue
        raw = trace.output_text(event.id)
        if not raw:
            return True
        try:
            return bool(_json.loads(raw).get("success", True))
        except (ValueError, AttributeError):
            return True
    return True


def next_scope(current: Escalation) -> Escalation:
    """The next wider retry. `exhausted` is terminal."""
    index = ESCALATION_ORDER.index(current)
    return ESCALATION_ORDER[min(index + 1, len(ESCALATION_ORDER) - 1)]


def invalidation_for_scope(
    scope: Escalation,
    selective: Iterable[str],
    agent_events: Iterable[str],
    all_events: Iterable[str],
) -> frozenset[str]:
    """What to recompute at this escalation level."""
    if scope == "selective":
        return frozenset(selective)
    if scope == "agent_restart":
        return frozenset(agent_events)
    if scope == "restart_all":
        return frozenset(all_events)
    return frozenset(all_events)


def rollback_memory(checkpoint: Checkpoint | None, current: dict[str, str]) -> dict[str, str | None]:
    """Undo writes that happened after the safe checkpoint. Empty if INIT.

    Coarse by construction: everything after the checkpoint goes, whether it was
    contaminated or not. `selective_memory_rollback` below is the per-write
    version, and the two are kept side by side because they answer different
    questions -- this one "what does resuming from here require", that one "what
    does this incident actually require".
    """
    if checkpoint is None:
        return {}
    return memory_rollback_plan(checkpoint, current)


def memory_writes(trace: Trace) -> list[tuple[str, str, str]]:
    """(event id, key, value) for every memory write in the trace, in order."""
    import json as _json

    out: list[tuple[str, str, str]] = []
    for event in trace.events:
        if event.kind != "memory_write":
            continue
        raw = trace.output_text(event.id)
        if not raw:
            continue
        try:
            payload = _json.loads(raw)
        except ValueError:
            continue
        key, value = payload.get("key"), payload.get("value")
        if key is not None:
            out.append((event.id, str(key), value))
    return out


def selective_memory_rollback(
    original: Trace,
    invalidated: Iterable[str],
    current_memory: dict[str, str],
    initial_memory: dict[str, str] | None = None,
) -> dict[str, str | None]:
    """Undo only the writes this incident actually contaminated.

    {key -> value to restore, or None to delete}. A key written only by events
    outside the invalidation set does not appear: the write was clean and stays.
    A key written by an invalidated event is rolled back to the value the last
    *surviving* write left, or to the fixture value, or removed if neither
    exists.

    WHERE THIS IS AND IS NOT THE RIGHT MODEL (Phase 7, D-071)
    ---------------------------------------------------------
    This is what a deployment does: the workflow has already run, the store
    holds what it holds, and the incident says which of those writes are
    suspect. Rolling the whole store back to its initial state would discard
    every clean write for the sake of one contaminated one, which is precisely
    the waste this project exists to avoid.

    It is **not** what `recover()` applies before a replay, and the reason is
    not cost. This testbed's replay re-executes the entire workflow from its
    starting state -- every `memory_read` runs again, in order, before the
    writes that follow it. Seeding that replay with writes the original run
    made would show an early read a value the original never saw, so the
    replay would no longer be a replay. Wholesale reset is not a shortcut
    there; it is what "re-run from the beginning with the same fixtures" means.

    So `recover()` computes this plan and reports it -- it is the answer to
    "what must the live store be told" -- and still resets the replay's own
    fixture wholesale. The two are different stores doing different jobs.
    """
    dead = set(invalidated)
    initial = dict(initial_memory or {})
    writes = memory_writes(original)
    if not writes:
        return {}

    contaminated_keys = {key for eid, key, _v in writes if eid in dead}
    plan: dict[str, str | None] = {}
    for key in sorted(contaminated_keys):
        surviving = [v for eid, k, v in writes if k == key and eid not in dead]
        if surviving:
            restore: str | None = surviving[-1]
        elif key in initial:
            restore = initial[key]
        else:
            restore = None
        if key in current_memory and current_memory[key] == restore:
            continue  # already correct; an entry here would be a no-op
        plan[key] = restore
    return plan
