"""
Checkpoints: agent state serialised at event boundaries.

Recovery replays only invalidated events, and it has to start from somewhere
trustworthy. That is what a checkpoint is for: "the earliest safe checkpoint
covering that set" in docs/02-architecture.md.

Storage split (D-018). The trace file keeps what is always cheap -- event
metadata, source content, influence edges, token counts. Checkpoint payloads
are the expensive part, so they live in a sidecar next to the trace:

    data/runs/run1.jsonl              the trace
    data/runs/run1.checkpoints.jsonl  the payloads

Two files per run, not one, and the split is deliberate: it is the same split
the storage-overhead measurement needs (open issue #8), and `overhead()`
below reports it directly.

Policy for v1 is a checkpoint after every agent boundary, per the
architecture doc. Whether that is affordable is a measurement, not an
assumption -- if the overhead is bad, that is itself a paper finding.
"""

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.common.content import ContentStore, content_path_for

CHECKPOINT_SUFFIX = ".checkpoints.jsonl"


def checkpoint_path_for(trace_path: str | Path) -> Path:
    """data/runs/run1.jsonl -> data/runs/run1.checkpoints.jsonl"""
    path = Path(trace_path)
    return path.with_suffix("").with_name(path.stem + CHECKPOINT_SUFFIX)


@dataclass
class Checkpoint:
    """Enough state to resume the workflow from just after `event_id`.

    `state` is whatever the pipeline needs to continue: for our testbed, each
    agent's context (source ids) and the outputs it has produced so far.
    `memory` is a full snapshot of the shared memory store, because recovery
    has to roll back memory writes made by contaminated events and a diff
    against the snapshot is the simplest way to know what to undo.
    """

    id: str
    event_id: str
    agent_id: str
    state: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, str] = field(default_factory=dict)
    bytes_stored: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "agent_id": self.agent_id,
            "state": self.state,
            "memory": dict(self.memory),
            "bytes_stored": self.bytes_stored,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        return cls(**{k: v for k, v in data.items() if k != "record"})


class CheckpointStore:
    """Append-only sidecar file of checkpoints for one run."""

    def __init__(self, path: str | Path, append: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoints: list[Checkpoint] = []
        self._count = 0
        self._fh = self.path.open("a" if append else "w", encoding="utf-8")

    # --- writing -----------------------------------------------------------

    def take(
        self,
        event_id: str,
        agent_id: str,
        state: dict[str, Any] | None = None,
        memory: dict[str, str] | None = None,
    ) -> Checkpoint:
        """Record a checkpoint taken just after `event_id`."""
        self._count += 1
        checkpoint = Checkpoint(
            id=f"k{self._count:04d}",
            event_id=event_id,
            agent_id=agent_id,
            state=dict(state or {}),
            memory=dict(memory or {}),
        )
        # Size of the payload only -- state plus memory. That is "the expensive
        # part" the architecture doc means, and counting the id and timestamp
        # alongside it would inflate the overhead figure with metadata we keep
        # for every event anyway.
        checkpoint.bytes_stored = len(
            json.dumps({"state": checkpoint.state, "memory": checkpoint.memory}).encode(
                "utf-8"
            )
        )
        self._fh.write(json.dumps({"record": "checkpoint", **checkpoint.to_dict()}) + "\n")
        self._fh.flush()
        self.checkpoints.append(checkpoint)
        return checkpoint

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "CheckpointStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- reading -------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> list[Checkpoint]:
        path = Path(path)
        if not path.exists():
            return []
        out: list[Checkpoint] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Checkpoint.from_dict(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: bad JSON ({exc})") from exc
        return out

    def total_bytes(self) -> int:
        return sum(c.bytes_stored for c in self.checkpoints)


# --- queries recovery needs ---------------------------------------------------


def latest_before(
    checkpoints: list[Checkpoint], event_id: str, order: list[str]
) -> Checkpoint | None:
    """The last checkpoint taken strictly before `event_id` in dependency order.

    `order` comes from EventGraph.topological_order(). Position in that order,
    not timestamp, is what makes a checkpoint safe to resume from: wall-clock
    time says nothing about whether one event depends on another.
    """
    position = {eid: i for i, eid in enumerate(order)}
    if event_id not in position:
        raise KeyError(f"{event_id} is not in the given order")
    target = position[event_id]
    candidates = [
        c for c in checkpoints if c.event_id in position and position[c.event_id] < target
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: position[c.event_id])


def safe_checkpoint_for(
    checkpoints: list[Checkpoint], invalidated: set[str], order: list[str]
) -> Checkpoint | None:
    """The latest checkpoint that precedes *every* invalidated event.

    Recovery resumes from one point, so the checkpoint has to be earlier than
    the earliest thing being recomputed. Returns None when even the first
    invalidated event precedes every checkpoint -- which means a full restart,
    and that is a legitimate answer worth reporting rather than hiding.
    """
    if not invalidated:
        return None
    position = {eid: i for i, eid in enumerate(order)}
    missing = [eid for eid in invalidated if eid not in position]
    if missing:
        raise KeyError(f"invalidated events not in order: {sorted(missing)[:5]}")
    earliest = min(invalidated, key=lambda eid: position[eid])
    return latest_before(checkpoints, earliest, order)


def memory_rollback_plan(
    checkpoint: Checkpoint, current_memory: dict[str, str]
) -> dict[str, str | None]:
    """{key -> value to restore, or None to delete}.

    Memory writes made by contaminated events have to be undone
    (docs/02-architecture.md). Comparing the live store against the snapshot
    gives the undo directly, including keys that did not exist at checkpoint
    time and must be removed rather than reverted.
    """
    plan: dict[str, str | None] = {}
    for key, value in current_memory.items():
        if key not in checkpoint.memory:
            plan[key] = None
        elif checkpoint.memory[key] != value:
            plan[key] = checkpoint.memory[key]
    for key, value in checkpoint.memory.items():
        if key not in current_memory:
            plan[key] = value
    return plan


# --- the measurement open issue #8 asks for ------------------------------------


def overhead(trace_path: str | Path) -> dict[str, Any]:
    """Storage cost of tracing, split by what we keep and where.

    Three files now, not two (D-027): the trace, the checkpoint payloads, and
    the content store. The split is the point -- the trace grows with event
    count, the content store grows with model verbosity, and checkpoints under
    the v1 policy used to grow with the *square* of run length because each one
    re-serialised every output before it (D-022 measured 56% of stored bytes).
    """
    trace_path = Path(trace_path)
    sidecar = checkpoint_path_for(trace_path)
    content_file = content_path_for(trace_path)
    trace_bytes = trace_path.stat().st_size if trace_path.exists() else 0
    checkpoint_bytes = sidecar.stat().st_size if sidecar.exists() else 0
    content_bytes = content_file.stat().st_size if content_file.exists() else 0
    total = trace_bytes + checkpoint_bytes + content_bytes
    return {
        "trace_bytes": trace_bytes,
        "checkpoint_bytes": checkpoint_bytes,
        "content_bytes": content_bytes,
        "total_bytes": total,
        "checkpoint_share": (checkpoint_bytes / total) if total else 0.0,
        "content_share": (content_bytes / total) if total else 0.0,
        "checkpoints": len(CheckpointStore.load(sidecar)),
    }


def payload_comparison(trace_path: str | Path) -> dict[str, Any]:
    """What checkpoints would cost if they still carried output text inline.

    The measurement behind D-028. A checkpoint's `state` holds every output
    produced so far, so under the v1 policy of one checkpoint per agent
    boundary the *k*th checkpoint re-serialises all *k-1* previous outputs --
    quadratic in run length, and D-022 measured the result at 56% of stored
    bytes on an 18-event run. Storing content refs instead makes each output
    appear once, in the content store, however many checkpoints mention it.

    Returns both numbers so the ratio can be reported rather than asserted. On
    a trace written before D-028 (`state["outputs"]` rather than
    `state["output_refs"]`) `with_refs` and `with_text` come out equal, which
    is the correct answer for that trace.
    """
    trace_path = Path(trace_path)
    checkpoints = CheckpointStore.load(checkpoint_path_for(trace_path))
    content_file = content_path_for(trace_path)
    store = ContentStore.load(content_file) if content_file.exists() else None

    with_refs = sum(c.bytes_stored for c in checkpoints)
    with_text = 0
    resolvable = True
    for checkpoint in checkpoints:
        state = dict(checkpoint.state)
        refs = state.pop("output_refs", None)
        if refs and store is not None:
            try:
                state["outputs"] = {eid: store.get(ref) for eid, ref in refs.items()}
            except KeyError:
                resolvable = False
                state["output_refs"] = refs
        elif refs:
            resolvable = False
            state["output_refs"] = refs
        with_text += len(
            json.dumps({"state": state, "memory": checkpoint.memory}).encode("utf-8")
        )
    return {
        "checkpoints": len(checkpoints),
        "with_refs_bytes": with_refs,
        "with_text_bytes": with_text,
        "ratio": (with_text / with_refs) if with_refs else 0.0,
        "content_bytes": content_file.stat().st_size if content_file.exists() else 0,
        "resolvable": resolvable,
    }


# =============================================================================
# Phase 12: lifecycle. Storage that does not grow without bound.
# =============================================================================
#
# Everything above writes. Nothing above ever stopped writing, which is fine
# for an 18-event testbed run and is not a policy: a long-running agent with
# many subcalls accumulates checkpoints for as long as it runs, and the
# overhead measurement (open issue #8) then describes a number that only goes
# up.
#
# Three mechanisms, in increasing order of how much they give away:
#
#   garbage collection   drops checkpoints that can no longer be a rewind
#                        point. Loses nothing: the retained one is strictly
#                        better than the ones it replaces.
#   recovery horizon     drops the *content* behind old events, keeping the
#                        hash. Loses the ability to replay them selectively,
#                        and the fallback to coarse recovery is explicit.
#   checkpoint interval  writes fewer of them in the first place, at the
#                        frequency Young/Daly says is optimal.


# --- 12.1 garbage collection --------------------------------------------------


@dataclass
class GCResult:
    """What garbage collection kept and dropped."""

    retained: list[Checkpoint] = field(default_factory=list)
    deleted: list[Checkpoint] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def bytes_freed(self) -> int:
        return sum(c.bytes_stored for c in self.deleted)

    @property
    def bytes_retained(self) -> int:
        return sum(c.bytes_stored for c in self.retained)

    def line(self) -> str:
        return (
            f"kept {len(self.retained)} checkpoints ({self.bytes_retained}B), "
            f"dropped {len(self.deleted)} ({self.bytes_freed}B freed)"
        )


def confirmed_clean(
    trace: Any,
    checkpoint: Checkpoint,
    order: list[str] | None = None,
    ledger: Any = None,
) -> bool:
    """Has every exposure this checkpoint's state depends on been *examined*
    and cleared?

    Read off `check` records (D-024), never off elapsed time. "This checkpoint
    is old, so it is probably fine" is exactly the reasoning that makes a
    deleted rewind point unrecoverable when it turns out not to be.

    WHICH EXPOSURES COUNT, AND WHY IT IS ALL OF THEM
    ------------------------------------------------
    Not just the checkpointing agent's own. `GeminiPipeline._checkpoint` stores
    `output_refs` for *every* output produced so far, across all agents, plus a
    full memory snapshot -- the payload is a global snapshot, not an agent-local
    one. So a checkpoint is only trustworthy if the whole prefix behind it is,
    and asking only about one agent's exposures would clear a checkpoint whose
    payload carries another agent's unexamined work.

    An unchecked pair is not clean. That is the conservative fallback doing its
    job here as everywhere else: an uncleared checkpoint is retained, which
    costs bytes and never costs a rewind point.
    """
    from src.provenance.checks import CheckLedger
    from src.tracing.graphs import EventGraph

    ledger = ledger or CheckLedger.from_trace(trace)
    order = order or EventGraph.from_trace(trace).topological_order()
    position = {eid: i for i, eid in enumerate(order)}
    cutoff = position.get(checkpoint.event_id)
    if cutoff is None:
        return False

    for event in trace.events:
        if position.get(event.id, 10**9) > cutoff:
            continue
        for sid in event.exposures:
            if not ledger.is_cleared(event.id, sid):
                return False
    return True


def gc_checkpoints(
    checkpoints: list[Checkpoint],
    trace: Any,
    order: list[str] | None = None,
    ledger: Any = None,
) -> GCResult:
    """Drop every checkpoint an agent can no longer usefully rewind to.

    The rule, per agent:

        find the latest checkpoint whose prefix is confirmed clean
        delete every checkpoint of that agent strictly before it

    A checkpoint earlier than a confirmed-clean one is dominated: anything the
    earlier one could be rewound to, the later one can be rewound to as well,
    with less to recompute. Deleting it loses no recovery option.

    **The most recent checkpoint of an agent is never deleted**, even when it
    looks finished. It may be the only safe rewind point left, and "this agent
    is done" is a statement about the past, not about what a detector will say
    in ten minutes.
    """
    from src.tracing.graphs import EventGraph

    order = order or EventGraph.from_trace(trace).topological_order()
    position = {eid: i for i, eid in enumerate(order)}
    result = GCResult()

    by_agent: dict[str, list[Checkpoint]] = {}
    for checkpoint in checkpoints:
        by_agent.setdefault(checkpoint.agent_id, []).append(checkpoint)

    for agent, group in by_agent.items():
        group = sorted(group, key=lambda c: position.get(c.event_id, -1))
        newest = group[-1]
        cleared = [
            c for c in group if confirmed_clean(trace, c, order=order, ledger=ledger)
        ]
        if not cleared:
            for c in group:
                result.retained.append(c)
                result.reasons[c.id] = (
                    f"{agent}: no checkpoint of this agent has a fully examined "
                    "prefix, so none can be shown to dominate another"
                )
            continue

        keep_from = position.get(cleared[-1].event_id, -1)
        for c in group:
            at = position.get(c.event_id, -1)
            if c is newest:
                result.retained.append(c)
                result.reasons[c.id] = f"{agent}: most recent checkpoint, never dropped"
            elif at < keep_from:
                result.deleted.append(c)
                result.reasons[c.id] = (
                    f"{agent}: dominated by {cleared[-1].id}, whose prefix is "
                    "confirmed clean and which rewinds to a later point"
                )
            else:
                result.retained.append(c)
                result.reasons[c.id] = (
                    f"{agent}: at or after the latest confirmed-clean "
                    f"checkpoint {cleared[-1].id}"
                )
    result.retained.sort(key=lambda c: position.get(c.event_id, -1))
    result.deleted.sort(key=lambda c: position.get(c.event_id, -1))
    return result


def rewrite_checkpoints(path: str | Path, retained: list[Checkpoint]) -> int:
    """Replace a checkpoint sidecar with just the retained set. Returns bytes freed.

    The one place in this project that is not append-only, and the exception is
    narrow on purpose: D-008 protects the *trace*, because history is the
    evidence. A checkpoint payload is a cache of state that can be recomputed
    by replay; dropping one loses a shortcut, not a record. The trace still says
    the checkpoint was taken -- only the payload goes.
    """
    file = Path(path)
    before = file.stat().st_size if file.exists() else 0
    lines = [
        json.dumps({"record": "checkpoint", **c.to_dict()}) for c in retained
    ]
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return max(0, before - file.stat().st_size)


# --- 12.2 recovery horizon ----------------------------------------------------


@dataclass(frozen=True)
class RetentionPolicy:
    """How far back full replayable detail is kept.

    Within `horizon_turns` events of the head of the trace, the content store
    keeps prompts and outputs, and selective replay works. Beyond it, content is
    downgraded to its hash -- which is what the ref already is, so the trace
    still proves *what* an event produced (the ref is a commitment to the bytes)
    while no longer being able to *re-issue* it.

    That is a real loss and the fallback for it is explicit: an incident
    reaching back beyond the horizon gets coarse recovery -- restart the
    affected agents from the beginning -- rather than a selective plan that
    would silently under-recover because half its prompts do not resolve.

    `horizon_turns=None` means keep everything, which is what the project did
    before Phase 12 and remains the default. Turning retention on is a choice a
    deployment makes about its own storage budget.
    """

    horizon_turns: int | None = None

    def within(self, index: int, head_index: int) -> bool:
        if self.horizon_turns is None:
            return True
        return (head_index - index) <= self.horizon_turns


@dataclass
class HorizonResult:
    """What applying a horizon dropped."""

    refs_kept: int = 0
    refs_downgraded: int = 0
    bytes_freed: int = 0
    events_beyond: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"kept {self.refs_kept} content refs, downgraded "
            f"{self.refs_downgraded} to hash only ({self.bytes_freed}B freed) "
            f"across {len(self.events_beyond)} events beyond the horizon"
        )


def apply_horizon(
    trace_path: str | Path,
    policy: RetentionPolicy,
    head_index: int | None = None,
) -> HorizonResult:
    """Downgrade content behind the horizon to a hash, in place.

    A ref referenced by **any** in-horizon event is kept in full. Content is
    addressed by hash, so one blob can be shared by an old event and a recent
    one -- dropping it because the old event aged out would break the recent
    one's replay, and the symptom would appear at recovery time as a missing
    prompt rather than here as a policy decision.
    """
    from src.common.content import ContentRecord, ContentStore
    from src.tracing.logger import read_trace

    trace_path = Path(trace_path)
    result = HorizonResult()
    if policy.horizon_turns is None:
        return result

    trace = read_trace(trace_path, content=False)
    content_file = content_path_for(trace_path)
    if not content_file.exists():
        return result
    store = ContentStore.load(content_file)

    head = len(trace.events) - 1 if head_index is None else head_index
    keep: set[str] = set()
    for index, event in enumerate(trace.events):
        refs = list(event.inputs_ref) + ([event.output_ref] if event.output_ref else [])
        if policy.within(index, head):
            keep.update(refs)
        else:
            result.events_beyond.append(event.id)

    lines: list[str] = []
    for record in store:
        if record.ref in keep or not record.text:
            result.refs_kept += 1
            lines.append(json.dumps({"record": "content", **record.to_dict()}))
            continue
        result.refs_downgraded += 1
        result.bytes_freed += len(record.text.encode("utf-8"))
        downgraded = ContentRecord(
            ref=record.ref,
            kind=record.kind,
            text="",
            meta={**record.meta, "downgraded": True, "bytes": len(record.text)},
        )
        lines.append(json.dumps({"record": "content", **downgraded.to_dict()}))
    content_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def replayable(trace: Any, event_ids: Iterable[str]) -> tuple[bool, list[str]]:
    """Can these events be re-issued? (yes/no, the ones that cannot).

    An event is replayable when its prompt still resolves to text. Beyond the
    horizon it does not: the ref is there, the bytes are gone. Events that never
    had a prompt -- tool calls, memory operations, the Executor's comparison --
    are recomputed by our own code rather than re-issued, so their content being
    absent is not a blocker and they are not counted.
    """
    blocked: list[str] = []
    model_events = {
        u.event_id for u in trace.usage if u.purpose == "pipeline" and u.event_id
    }
    for eid in event_ids:
        if eid not in model_events:
            continue
        try:
            prompt = trace.prompt_text(eid)
        except Exception:
            prompt = None
        if not prompt:
            blocked.append(eid)
    return (not blocked), sorted(blocked)


@dataclass
class RecoveryMode:
    """Selective replay, or the coarse fallback when the horizon forbids it."""

    mode: str  # "selective" | "coarse"
    invalidation: frozenset[str]
    blocked_events: list[str] = field(default_factory=list)
    reason: str = ""

    def line(self) -> str:
        return (
            f"{self.mode}: {len(self.invalidation)} events to recompute"
            + (f" -- {self.reason}" if self.reason else "")
        )


def recovery_mode_for(trace: Any, invalidation: Iterable[str]) -> RecoveryMode:
    """Decide between selective replay and coarse restart, given what is stored.

    This is the fallback path Phase 12.2 requires to be explicit. The failure it
    prevents is the quiet one: a selective plan whose prompts have been
    downgraded cannot re-issue those events, and a replay engine that skipped
    them would emit a trace that looks recovered and is not. So when any
    invalidated model event is unreplayable, the scope widens to every event of
    every affected agent -- which needs no stored prompts, because those agents
    are re-run from the beginning.
    """
    invalidation = set(invalidation)
    ok, blocked = replayable(trace, invalidation)
    if ok:
        return RecoveryMode(
            mode="selective",
            invalidation=frozenset(invalidation),
            reason="every invalidated event still has its prompt stored",
        )
    agents = {
        trace.event(eid).agent_id for eid in invalidation if trace.has_event(eid)
    }
    coarse = {e.id for e in trace.events if e.agent_id in agents}
    return RecoveryMode(
        mode="coarse",
        invalidation=frozenset(coarse),
        blocked_events=blocked,
        reason=(
            f"{len(blocked)} invalidated event(s) are beyond the recovery "
            f"horizon and cannot be re-issued; restarting agent(s) "
            f"{sorted(agents)} instead of under-recovering"
        ),
    )


# --- 12.3 how often to checkpoint ---------------------------------------------


def checkpoint_interval(
    checkpoint_write_cost: float, mean_turns_between_detections: float
) -> float:
    """Young (1974) / Daly (2006): T_opt ~ sqrt(2 * delta * M).

    `delta` is the cost of writing one checkpoint and `M` the mean time between
    the failures it protects against. In the original setting those are seconds
    of wall clock and hours of MTBF; here they are **events**, because that is
    the unit this project counts work in (D-012) and mixing units would produce
    an interval in no unit at all.

    So:
      delta = the cost of one checkpoint write, expressed in event-equivalents
              (`write_cost_in_events()` below measures it)
      M     = the mean number of events between a poisoning and its detection,
              which is the distribution Phase 9 measures across the scripted
              runs and is why that measurement was required before this one.

    The shape is what matters and it is the standard result: the optimal
    interval grows with the square root of both, so a cheap checkpoint or a
    fast detector both justify checkpointing more often, and neither justifies
    it linearly.
    """
    if checkpoint_write_cost < 0 or mean_turns_between_detections < 0:
        raise ValueError("checkpoint cost and detection interval must be >= 0")
    return math.sqrt(2.0 * checkpoint_write_cost * mean_turns_between_detections)


def write_cost_in_events(trace_path: str | Path) -> float:
    """delta: one checkpoint's payload, measured against one event's record.

    Both in bytes off the actual files, so the ratio is dimensionless and the
    interval that comes out is in events. Returns 0.0 when there is nothing to
    measure, which yields an interval of 0 -- correctly meaning "no basis to
    choose one" rather than a made-up default.
    """
    trace_path = Path(trace_path)
    sidecar = checkpoint_path_for(trace_path)
    checkpoints = CheckpointStore.load(sidecar)
    if not checkpoints or not trace_path.exists():
        return 0.0

    from src.tracing.logger import read_trace

    trace = read_trace(trace_path, content=False)
    if not trace.events:
        return 0.0
    mean_checkpoint = sum(c.bytes_stored for c in checkpoints) / len(checkpoints)
    mean_event = trace_path.stat().st_size / len(trace.events)
    return mean_checkpoint / mean_event if mean_event else 0.0


def measured_interval(
    trace_path: str | Path,
    economics_report: str | Path = "data/results/economics.json",
) -> dict[str, Any]:
    """The checkpoint interval, computed from measurements rather than chosen.

    `mean_turns_between_detections` is read from the Phase 9 report. It is
    **not** defaulted: a missing report raises, because a hardcoded fallback
    here would be indistinguishable from a measured value in the output and
    Phase 12.3's whole requirement is that it not be hardcoded.
    """
    report_path = Path(economics_report)
    if not report_path.exists():
        raise FileNotFoundError(
            f"{report_path} does not exist. The checkpoint interval is derived "
            "from Phase 9's measured detection latency and is not allowed a "
            "default. Run `python -m src.eval.economics` first."
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    latency = report.get("latency") or {}
    mean_turns = latency.get("mean_turns_between_detections")
    if not mean_turns:
        raise ValueError(
            f"{report_path} carries no mean_turns_between_detections; the "
            "latency measurement did not run."
        )
    delta = write_cost_in_events(trace_path)
    interval = checkpoint_interval(delta, mean_turns)
    return {
        "checkpoint_write_cost_events": delta,
        "mean_turns_between_detections": mean_turns,
        "interval_events": interval,
        "current_policy": "one checkpoint per agent boundary",
        "source": str(report_path),
    }


if __name__ == "__main__":
    import sys

    from src.tracing.graphs import EventGraph
    from src.tracing.logger import read_trace

    path = sys.argv[1] if len(sys.argv) > 1 else "data/runs/fake.jsonl"
    trace = read_trace(path)
    checkpoints = CheckpointStore.load(checkpoint_path_for(path))
    if not checkpoints:
        print(f"no checkpoints beside {path}")
        print(f"expected {checkpoint_path_for(path)}")
        raise SystemExit(0)

    order = EventGraph.from_trace(trace).topological_order()
    print(f"{len(checkpoints)} checkpoints for {path}")
    for c in checkpoints:
        print(f"  {c.id} after {c.event_id} ({c.agent_id}) {c.bytes_stored} bytes")
    print()
    print("storage overhead:", json.dumps(overhead(path), indent=2))
    print()
    comparison = payload_comparison(path)
    print(f"checkpoint payloads, refs vs inline text (D-028):")
    print(f"  with content refs  {comparison['with_refs_bytes']:>8} B")
    print(f"  with text inline   {comparison['with_text_bytes']:>8} B"
          f"   ({comparison['ratio']:.1f}x)")
    print(f"  content store      {comparison['content_bytes']:>8} B"
          f"   (one copy of each output, however many checkpoints name it)")
    if not comparison["resolvable"]:
        print("  (some refs did not resolve, so the inline figure is a floor)")

    # --- Phase 12 lifecycle ---------------------------------------------------
    print()
    print("LIFECYCLE (Phase 12)")
    print()
    gc = gc_checkpoints(checkpoints, trace, order=order)
    print(f"  garbage collection: {gc.line()}")
    for c in gc.deleted:
        print(f"    drop {c.id}  {gc.reasons[c.id]}")
    for c in gc.retained:
        print(f"    keep {c.id}  {gc.reasons[c.id]}")
    if not gc.deleted:
        print("    nothing dropped -- an unchecked exposure is not a clean one,")
        print("    so no checkpoint here can be shown to dominate another.")

    print()
    try:
        interval = measured_interval(path)
        print(
            f"  checkpoint interval (Young/Daly): "
            f"sqrt(2 * {interval['checkpoint_write_cost_events']:.3f} * "
            f"{interval['mean_turns_between_detections']:.2f}) = "
            f"{interval['interval_events']:.2f} events"
        )
        print(f"    delta measured off this run's own files; M from "
              f"{interval['source']}")
        print(f"    current policy: {interval['current_policy']} "
              f"({len(checkpoints)} taken over {len(trace.events)} events "
              f"= one per {len(trace.events)/max(1,len(checkpoints)):.1f})")
    except (FileNotFoundError, ValueError) as exc:
        print(f"  checkpoint interval: unavailable -- {exc}")
