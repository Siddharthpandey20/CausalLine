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
from typing import Iterable, Literal

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

    def describe(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"verify {status}"]
        if self.tainted_events:
            lines.append(f"  taint still live: {sorted(self.tainted_events)}")
        for item in self.referential_failures:
            lines.append(f"  referential: {item}")
        if not self.task_success:
            lines.append("  task-level check failed")
        for reason in self.reasons:
            lines.append(f"  {reason}")
        return "\n".join(lines)


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
) -> VerifyResult:
    """Re-run Taint, check live references, check the task.

    `flagged` is the detector verdict, not ground truth. A missed source
    will leave taint the method was never told about; that is the
    detector's failure and is reported as leftover taint, not hidden.
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

    region = contaminate(recovered, seeds) if seeds else None
    tainted = frozenset(region.events) if region else frozenset()

    refs: list[str] = []
    if current_memory is not None:
        refs = live_memory_points_at_invalidated(
            recovered, invalidated, current_memory, original
        )

    ok = (not tainted) and (not refs) and task_success
    if tainted:
        reasons.append("Taint(new_graph) is non-empty")
    if refs:
        reasons.append("referential inconsistency")
    if not task_success:
        reasons.append("task-level check failed")

    return VerifyResult(
        ok=ok,
        tainted_events=tainted,
        referential_failures=refs,
        task_success=task_success,
        reasons=reasons,
    )


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
    """Undo writes that happened after the safe checkpoint. Empty if INIT."""
    if checkpoint is None:
        return {}
    return memory_rollback_plan(checkpoint, current)
