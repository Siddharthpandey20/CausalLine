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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    """Storage cost of tracing, split into what we always keep and what we
    only keep at checkpoints. Reported as bytes and as a percentage."""
    trace_path = Path(trace_path)
    sidecar = checkpoint_path_for(trace_path)
    trace_bytes = trace_path.stat().st_size if trace_path.exists() else 0
    checkpoint_bytes = sidecar.stat().st_size if sidecar.exists() else 0
    total = trace_bytes + checkpoint_bytes
    return {
        "trace_bytes": trace_bytes,
        "checkpoint_bytes": checkpoint_bytes,
        "total_bytes": total,
        "checkpoint_share": (checkpoint_bytes / total) if total else 0.0,
        "checkpoints": len(CheckpointStore.load(sidecar)),
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
