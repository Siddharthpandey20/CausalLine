"""
Event logging for agent-recovery.

TraceLogger records every operation in a workflow run as an Event with
auto-generated ids and parent links, and appends it to a JSONL trace file.
read_trace() reads that file back. The call graph and the event graph are
both built from this file, so nothing else needs to be persisted for the
week-1 exit test.

Trace file format: one JSON object per line, tagged by a "record" key.

    {"record": "meta",      "run_id": ..., "schema_version": 1, ...}
    {"record": "source",    "id": "S1", ...}
    {"record": "event",     "id": "e0001", ...}
    {"record": "influence", "source_id": "S1", "target_event": "e0004", ...}
    {"record": "usage",     "call_id": "c0001", "purpose": "pipeline", ...}

Lines are written and flushed as they happen, so a run that crashes still
leaves a readable trace up to the crash.

No LLM calls live here. The logger is passed into the pipeline; agents call
it. See src/tracing/fake_pipeline.py for a runnable example.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from src.common.models import (
    SCHEMA_VERSION,
    Event,
    EventKind,
    InfluenceEdge,
    Source,
    SourceKind,
    UsagePurpose,
    UsageRecord,
    event_id,
    source_id,
)

RECORD_CLASSES: dict[str, type] = {
    "event": Event,
    "source": Source,
    "influence": InfluenceEdge,
    "usage": UsageRecord,
}


class TraceLogger:
    """Records events and sources for one workflow run.

    Parent links
    ------------
    `log_event(..., parents=None)` links the new event to the previous event
    logged by the *same* agent. That is the common case: an agent's own chain
    of operations. Links that cross agents (Planner -> Researcher message) are
    never guessed and must be passed explicitly. Pass `parents=[]` for a
    genuine root event. See docs/05-decisions.md, D-007.

    Usage:

        with TraceLogger("data/runs/run1.jsonl", run_id="run1") as log:
            e = log.log_event("planner", "plan")
            log.log_event("researcher", "message", parents=[e.id])
    """

    def __init__(
        self,
        path: str | Path,
        run_id: str | None = None,
        meta: dict[str, Any] | None = None,
        append: bool = False,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id or self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a" if append else "w", encoding="utf-8")

        self._event_count = 0
        self._source_count = 0
        self._call_count = 0
        self.events: list[Event] = []
        self.sources: list[Source] = []
        self.influence: list[InfluenceEdge] = []
        self.usage: list[UsageRecord] = []
        # last event id per agent, for automatic parent links
        self._last_by_agent: dict[str, str] = {}

        header = {
            "record": "meta",
            "run_id": self.run_id,
            "schema_version": SCHEMA_VERSION,
            "created_at": time.time(),
        }
        header.update(meta or {})
        self._write(header)

    # --- logging ----------------------------------------------------------

    def log_event(
        self,
        agent_id: str,
        kind: EventKind,
        parents: list[str] | None = None,
        inputs_ref: list[str] | None = None,
        exposures: list[str] | None = None,
        output_ref: str | None = None,
        tool_id: str | None = None,
    ) -> Event:
        """Record one operation. Returns the Event, whose .id the caller
        passes as a parent to whatever it causes next.

        `exposures` is every source id that was in the agent's context for this
        operation, whether or not the agent used it. Record it here, at call
        time: it cannot be recovered later, and the exposure/influence gap is
        the result the paper reports.
        """
        self._event_count += 1
        event = Event(
            id=event_id(self._event_count),
            agent_id=agent_id,
            kind=kind,
            parents=self._resolve_parents(agent_id, parents),
            inputs_ref=list(inputs_ref or []),
            exposures=list(exposures or []),
            output_ref=output_ref,
            tool_id=tool_id,
        )
        self.events.append(event)
        self._last_by_agent[agent_id] = event.id
        self._write({"record": "event", **event.to_dict()})
        return event

    def log_source(
        self,
        kind: SourceKind,
        content: str,
        origin_event: str | None = None,
        derived_from: str | None = None,
        malicious: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> Source:
        """Register an incoming unit of information and give it an id.

        Pass `derived_from` when the source is an earlier event's output being
        handed to another agent. It is the same work as that event and is not
        counted again (D-012).
        """
        self._source_count += 1
        source = Source(
            id=source_id(self._source_count),
            kind=kind,
            content=content,
            origin_event=origin_event,
            derived_from=derived_from,
            malicious=malicious,
            metadata=dict(metadata or {}),
        )
        self.sources.append(source)
        self._write({"record": "source", **source.to_dict()})
        return source

    def log_usage(
        self,
        purpose: UsagePurpose,
        model: str,
        prompt_tokens: int,
        output_tokens: int,
        total_tokens: int,
        event_id_: str | None = None,
        agent_id: str | None = None,
        thoughts_tokens: int = 0,
        attempts: int = 1,
        latency_s: float = 0.0,
        slept_s: float = 0.0,
    ) -> UsageRecord:
        """Record the token cost of one API call.

        Called for every call, including the ones that are not part of the
        original run. docs/04 is explicit that hiding the cost of the
        counterfactual checks is the easiest way to look good dishonestly,
        so the trace records analysis calls next to pipeline calls and the
        metric splits them by `purpose`.
        """
        self._call_count += 1
        record = UsageRecord(
            call_id=f"c{self._call_count:04d}",
            purpose=purpose,
            model=model,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            event_id=event_id_,
            agent_id=agent_id,
            thoughts_tokens=thoughts_tokens,
            attempts=attempts,
            latency_s=latency_s,
            slept_s=slept_s,
        )
        self.usage.append(record)
        self._write({"record": "usage", **record.to_dict()})
        return record

    def log_influence(self, edge: InfluenceEdge) -> InfluenceEdge:
        """Record an influence edge. Written by src/provenance/, which
        decides *whether* an edge exists; the logger only stores it."""
        self.influence.append(edge)
        self._write({"record": "influence", **edge.to_dict()})
        return edge

    # --- helpers ----------------------------------------------------------

    def _resolve_parents(self, agent_id: str, parents: list[str] | None) -> list[str]:
        if parents is not None:
            return list(parents)
        previous = self._last_by_agent.get(agent_id)
        return [previous] if previous else []

    def last_event(self, agent_id: str) -> str | None:
        """Id of the last event this agent logged, or None."""
        return self._last_by_agent.get(agent_id)

    def _write(self, record: dict[str, Any]) -> None:
        if self._fh.closed:
            raise ValueError(f"TraceLogger for {self.path} is closed")
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "TraceLogger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


@dataclass
class Trace:
    """A trace file read back into memory."""

    meta: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    influence: list[InfluenceEdge] = field(default_factory=list)
    usage: list[UsageRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._events_by_id = {e.id: e for e in self.events}
        self._sources_by_id = {s.id: s for s in self.sources}

    def event(self, eid: str) -> Event:
        return self._events_by_id[eid]

    def source(self, sid: str) -> Source:
        return self._sources_by_id[sid]

    def children(self, eid: str) -> list[Event]:
        """Events that list `eid` as a parent."""
        return [e for e in self.events if eid in e.parents]

    def by_agent(self, agent_id: str) -> list[Event]:
        return [e for e in self.events if e.agent_id == agent_id]

    def work_units(self) -> list[Event]:
        """The events that count as work (D-012).

        Work is counted in events. Sources are information, not work: a source
        carrying `derived_from` is the same work as that event and must never
        be added to it. Every metric in docs/04-experiments.md starts here, so
        the unit is defined once and not re-decided per metric.
        """
        return list(self.events)

    def derived_sources(self) -> dict[str, str]:
        """{source id -> the event whose output it is}. These sources are the
        events' work seen from the consumer's side, not extra work."""
        return {s.id: s.derived_from for s in self.sources if s.derived_from}

    def source_event(self, sid: str) -> str | None:
        """The event a source is the output of, or None for sources that came
        from outside the run (web, user input, memory written earlier)."""
        return self.source(sid).derived_from

    def influences(self, eid: str) -> list[str]:
        """Source ids with an influence edge into this event."""
        return [e.source_id for e in self.influence if e.target_event == eid]

    def exposed_not_influenced(self, eid: str) -> list[str]:
        """Sources that were in context but did not influence the output.

        This is the set the baselines throw away and we keep. Note that an
        empty influence set means "no edges recorded yet", not "clean" --
        deciding that is src/provenance/'s job, and unknown stays contaminated.
        """
        influenced = set(self.influences(eid))
        return [s for s in self.event(eid).exposures if s not in influenced]

    def tokens_by_purpose(self) -> dict[str, int]:
        """Total tokens per UsagePurpose. The cost table in docs/04 is built
        from this; analysis and replay must stay separable."""
        totals: dict[str, int] = {}
        for u in self.usage:
            totals[u.purpose] = totals.get(u.purpose, 0) + u.total_tokens
        return totals

    def analysis_tokens(self) -> int:
        """Tokens spent deciding what is contaminated, rather than redoing it.
        Open issue #7 is this number against replay tokens."""
        return sum(u.total_tokens for u in self.usage if u.is_analysis)

    def replay_tokens(self) -> int:
        return sum(u.total_tokens for u in self.usage if u.purpose == "replay")

    def pipeline_tokens(self) -> int:
        """Cost of the original run: the denominator B0 (full restart) pays."""
        return sum(u.total_tokens for u in self.usage if u.purpose == "pipeline")

    def agents(self) -> list[str]:
        """Agent ids in first-seen order."""
        seen: list[str] = []
        for e in self.events:
            if e.agent_id not in seen:
                seen.append(e.agent_id)
        return seen

    def validate(self) -> None:
        """Raise if the trace is not internally consistent. Cheap, and it
        catches the mistakes that would silently corrupt a graph later."""
        if len(self._events_by_id) != len(self.events):
            raise ValueError("trace contains duplicate event ids")
        if len(self._sources_by_id) != len(self.sources):
            raise ValueError("trace contains duplicate source ids")
        for e in self.events:
            for p in e.parents:
                if p not in self._events_by_id:
                    raise ValueError(f"event {e.id} has unknown parent {p}")
            for s in e.exposures:
                if s not in self._sources_by_id:
                    raise ValueError(f"event {e.id} is exposed to unknown source {s}")
        for s in self.sources:
            if s.origin_event and s.origin_event not in self._events_by_id:
                raise ValueError(
                    f"source {s.id} has unknown origin_event {s.origin_event}"
                )
            if s.derived_from and s.derived_from not in self._events_by_id:
                raise ValueError(
                    f"source {s.id} has unknown derived_from {s.derived_from}"
                )
        # One event's output must not be wrapped as two sources: that is the
        # double count D-012 exists to prevent, and it would inflate every
        # work-preserved figure.
        wrapped: dict[str, str] = {}
        for s in self.sources:
            if not s.derived_from:
                continue
            if s.derived_from in wrapped:
                raise ValueError(
                    f"event {s.derived_from} is wrapped by two sources: "
                    f"{wrapped[s.derived_from]} and {s.id}"
                )
            wrapped[s.derived_from] = s.id
        for u in self.usage:
            if u.event_id and u.event_id not in self._events_by_id:
                raise ValueError(
                    f"usage {u.call_id} refers to unknown event {u.event_id}"
                )
        for edge in self.influence:
            if edge.target_event not in self._events_by_id:
                raise ValueError(
                    f"influence edge targets unknown event {edge.target_event}"
                )
            if edge.source_id not in self._sources_by_id:
                raise ValueError(f"influence edge from unknown source {edge.source_id}")


def read_trace(path: str | Path) -> Trace:
    """Read a JSONL trace file written by TraceLogger."""
    meta: dict[str, Any] = {}
    events: list[Event] = []
    sources: list[Source] = []
    influence: list[InfluenceEdge] = []
    usage: list[UsageRecord] = []
    for line_no, record in enumerate(_read_records(Path(path)), start=1):
        kind = record.get("record")
        if kind == "meta":
            meta = {k: v for k, v in record.items() if k != "record"}
        elif kind == "event":
            events.append(Event.from_dict(record))
        elif kind == "source":
            sources.append(Source.from_dict(record))
        elif kind == "influence":
            influence.append(InfluenceEdge.from_dict(record))
        elif kind == "usage":
            usage.append(UsageRecord.from_dict(record))
        else:
            raise ValueError(f"{path}:{line_no}: unknown record type {kind!r}")
    return Trace(
        meta=meta,
        events=events,
        sources=sources,
        influence=influence,
        usage=usage,
    )


def _read_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: bad JSON ({exc})") from exc
