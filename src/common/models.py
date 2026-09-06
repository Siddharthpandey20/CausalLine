"""
Shared data models for agent-recovery.

A trace is made of three record types:

    Source          an incoming unit of information ("S1", "S2", ...)
    Event           one recorded operation ("e0001", "e0002", ...)
    InfluenceEdge   a source demonstrably changed the output of an event

Field names come from docs/02-architecture.md. Every subsystem (tracing,
provenance, recovery, eval) depends on these, so they are frozen after
week 1 (docs/05-decisions.md, D-006) and change only by agreement.

Every model round-trips through plain JSON-compatible dicts:

    d = event.to_dict()
    event == Event.from_dict(d)

Vocabulary note (CLAUDE.md): *exposure* is a source being present in an
agent's context; *influence* is a source demonstrably changing the output.
An InfluenceEdge records influence only. Exposure is recorded separately.

Counting rule (D-012): **work is measured in events, never in sources.**
An agent output exists twice in a trace -- once as the event that produced
it (e0006) and once as the source a later agent consumed (S6, carrying
`derived_from="e0006"`). They are one piece of work. Every metric in
docs/04-experiments.md counts the event and ignores the source.
"""

import json
import time
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, Literal

SCHEMA_VERSION = 1

# --- controlled vocabularies -------------------------------------------------

EventKind = Literal[
    "message",
    "tool_call",
    "tool_response",
    "memory_read",
    "memory_write",
    "agent_output",
    "plan",
    "decision",
]
EVENT_KINDS: frozenset[str] = frozenset(
    {
        "message",
        "tool_call",
        "tool_response",
        "memory_read",
        "memory_write",
        "agent_output",
        "plan",
        "decision",
    }
)

SourceKind = Literal[
    "user_input",
    "web",
    "database",
    "memory",
    "agent_message",
    "tool_output",
]
SOURCE_KINDS: frozenset[str] = frozenset(
    {"user_input", "web", "database", "memory", "agent_message", "tool_output"}
)

# How an influence edge was established (docs/02-architecture.md):
# self_report is a claim, counterfactual is evidence, assumed is the
# conservative fallback -- unknown provenance is treated as influenced.
InfluenceMethod = Literal["self_report", "counterfactual", "assumed"]
INFLUENCE_METHODS: frozenset[str] = frozenset(
    {"self_report", "counterfactual", "assumed"}
)

# What an API call was spent on. The cost metric in docs/04 splits recovery
# cost into analysis and replay, so every call has to declare which it is at
# the moment it is made.
UsagePurpose = Literal[
    "pipeline",       # the original run
    "self_report",    # asking an agent which inputs it used
    "counterfactual", # re-running an event with a source removed
    "replay",         # recomputing an invalidated event
    "verification",   # checking a recovered output
]
USAGE_PURPOSES: frozenset[str] = frozenset(
    {"pipeline", "self_report", "counterfactual", "replay", "verification"}
)


# --- id helpers --------------------------------------------------------------
# One place, so tracing / provenance / eval all format ids identically.


def event_id(n: int) -> str:
    """1 -> 'e0001'."""
    return f"e{n:04d}"


def source_id(n: int) -> str:
    """1 -> 'S1'."""
    return f"S{n}"


def sort_source_ids(ids: list[str]) -> list[str]:
    """Sort S-ids numerically. Plain sorted() puts S10 before S7, which looks
    like a mistake in a figure. Falls back to string order for odd ids."""
    def key(sid: str) -> tuple[int, str]:
        digits = sid[1:]
        return (int(digits), "") if digits.isdigit() else (10**9, sid)

    return sorted(ids, key=key)


# --- serialisation helpers ---------------------------------------------------


def _check_keys(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown or missing keys. A frozen schema should fail loudly."""
    known = {f.name for f in fields(cls)}
    required = {
        f.name
        for f in fields(cls)
        if f.default is MISSING and f.default_factory is MISSING
    }
    data = {k: v for k, v in data.items() if k != "record"}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown field(s) {sorted(unknown)}")
    missing = required - set(data)
    if missing:
        raise ValueError(f"{cls.__name__}: missing field(s) {sorted(missing)}")
    return data


def _require_str_list(value: Any, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{label} must be a list of strings, got {value!r}")


# --- models ------------------------------------------------------------------


@dataclass
class Event:
    """One recorded operation in the workflow.

    parents      event ids this event was derived from (execution history)
    inputs_ref   references into the content store, not source ids
    exposures    source ids present in the agent's context for this event
    output_ref   reference into the content store, None if the event has no output

    `exposures` is recorded at logging time, by whoever runs the agent -- it is
    a fact about what was in the prompt, not an analysis result. Influence is
    the separate, smaller set (see InfluenceEdge). Comparing the two is the
    paper's core claim, so exposure cannot be reconstructed after the fact.
    """

    id: str
    agent_id: str
    kind: EventKind
    parents: list[str] = field(default_factory=list)
    inputs_ref: list[str] = field(default_factory=list)
    exposures: list[str] = field(default_factory=list)
    output_ref: str | None = None
    tool_id: str | None = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Event.id must be non-empty")
        if not self.agent_id:
            raise ValueError("Event.agent_id must be non-empty")
        if self.kind not in EVENT_KINDS:
            raise ValueError(
                f"Event.kind {self.kind!r} not one of {sorted(EVENT_KINDS)}"
            )
        _require_str_list(self.parents, "Event.parents")
        _require_str_list(self.inputs_ref, "Event.inputs_ref")
        _require_str_list(self.exposures, "Event.exposures")
        if self.id in self.parents:
            raise ValueError(f"Event {self.id} lists itself as a parent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "kind": self.kind,
            "parents": list(self.parents),
            "inputs_ref": list(self.inputs_ref),
            "exposures": list(self.exposures),
            "output_ref": self.output_ref,
            "tool_id": self.tool_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(**_check_keys(cls, data))


@dataclass
class Source:
    """An incoming unit of information, given an id (S1, S2, ...).

    Source content is always stored: replay and counterfactual replay both
    need it (docs/02-architecture.md, "Checkpoints and storage").

    origin_event  the event that brought this source in (tool_response,
                  memory_read, message, ...). None for the user instruction.
    derived_from  set when this source IS the output of an earlier event in
                  this run (an agent output consumed downstream). The source
                  and that event are the same work, counted once, as the event
                  (D-012). Also the link contamination crosses when it moves
                  from a producing agent to a consuming one.
    malicious     GROUND TRUTH, FOR EVALUATION ONLY. Set by attack injection
                  in src/eval/ and read only by src/eval/ when scoring a run.
                  Nothing in src/provenance/ or src/recovery/ may ever read
                  this field. Those subsystems must reach their conclusions
                  from the detector's verdict and the influence edges alone;
                  reading the label would leak the answer into the method and
                  make every number in the paper meaningless.
    metadata      free-form provenance detail (url, memory key, ...). An
                  escape hatch so the frozen schema does not need a new field
                  for every source kind.
    """

    id: str
    kind: SourceKind
    content: str
    origin_event: str | None = None
    derived_from: str | None = None
    malicious: bool = False
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Source.id must be non-empty")
        if self.kind not in SOURCE_KINDS:
            raise ValueError(
                f"Source.kind {self.kind!r} not one of {sorted(SOURCE_KINDS)}"
            )
        if not isinstance(self.content, str):
            raise ValueError("Source.content must be a string")
        if not isinstance(self.metadata, dict):
            raise ValueError("Source.metadata must be a dict")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "origin_event": self.origin_event,
            "derived_from": self.derived_from,
            "malicious": self.malicious,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        return cls(**_check_keys(cls, data))


@dataclass
class InfluenceEdge:
    """A source demonstrably changed the output of an event.

    Only influence lives here. Exposure (a source merely present in an
    agent's context) is a different relation, recorded elsewhere. The gap
    between the two sets is the contribution of this project.

    method     self_report | counterfactual | assumed
    confident  whether we are willing to act on this edge. Anything not
               established confidently is treated as contaminated.
    """

    source_id: str
    target_event: str
    method: InfluenceMethod
    confident: bool

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("InfluenceEdge.source_id must be non-empty")
        if not self.target_event:
            raise ValueError("InfluenceEdge.target_event must be non-empty")
        if self.method not in INFLUENCE_METHODS:
            raise ValueError(
                f"InfluenceEdge.method {self.method!r} not one of "
                f"{sorted(INFLUENCE_METHODS)}"
            )
        if not isinstance(self.confident, bool):
            raise ValueError("InfluenceEdge.confident must be a bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_event": self.target_event,
            "method": self.method,
            "confident": self.confident,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InfluenceEdge":
        return cls(**_check_keys(cls, data))


@dataclass
class UsageRecord:
    """Tokens spent on one API call.

    Added after the week-1 freeze. D-006 protects the three trace models
    above; this is a fourth, additive record and changes none of them.

    `purpose` is the field the cost metric turns on. docs/04 requires
    recovery cost split into analysis and replay, and open issue #7 (the
    check may cost more than the rerun) is decided by that split. Recording
    it per call is the only way to get it -- it cannot be reconstructed from
    a token total afterwards.

    `event_id` is the event the call produced, or the event being analysed
    for analysis-purpose calls. None only for calls outside the event stream.
    """

    call_id: str
    purpose: UsagePurpose
    model: str
    prompt_tokens: int
    output_tokens: int
    total_tokens: int
    event_id: str | None = None
    agent_id: str | None = None
    thoughts_tokens: int = 0
    attempts: int = 1
    latency_s: float = 0.0
    slept_s: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.purpose not in USAGE_PURPOSES:
            raise ValueError(
                f"UsageRecord.purpose {self.purpose!r} not one of "
                f"{sorted(USAGE_PURPOSES)}"
            )
        for name in ("prompt_tokens", "output_tokens", "total_tokens"):
            if getattr(self, name) < 0:
                raise ValueError(f"UsageRecord.{name} must not be negative")

    @property
    def is_analysis(self) -> bool:
        """Analysis cost, as opposed to replay cost. The ratio between the two
        is the break-even number open issue #7 asks for."""
        return self.purpose in ("self_report", "counterfactual", "verification")

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "purpose": self.purpose,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "event_id": self.event_id,
            "agent_id": self.agent_id,
            "thoughts_tokens": self.thoughts_tokens,
            "attempts": self.attempts,
            "latency_s": self.latency_s,
            "slept_s": self.slept_s,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UsageRecord":
        return cls(**_check_keys(cls, data))


# --- JSON convenience --------------------------------------------------------


def to_json(obj: Event | Source | InfluenceEdge, **kwargs: Any) -> str:
    """Serialise one model to a JSON string."""
    return json.dumps(obj.to_dict(), **kwargs)


def from_json(cls: type, text: str) -> Any:
    """from_json(Event, line) -> Event"""
    return cls.from_dict(json.loads(text))
