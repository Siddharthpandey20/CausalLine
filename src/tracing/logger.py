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
    {"record": "check",     "source_id": "S1", "target_event": "e0004", ...}
    {"record": "usage",     "call_id": "c0001", "purpose": "pipeline", ...}

The `check` record is the one that makes the trace able to say "we examined
this pair and it came back clean", as opposed to "nobody looked" (D-024).
Everything downstream -- the contamination walk, the recovery planner, the
work-preserved metric -- reads it.

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

from src.common.content import ContentStore, content_path_for
from src.common.models import (
    SCHEMA_VERSION,
    CheckRecord,
    CheckVerdict,
    Event,
    EventKind,
    InfluenceEdge,
    InfluenceMethod,
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
    "check": CheckRecord,
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
        self.checks: list[CheckRecord] = []
        self.usage: list[UsageRecord] = []
        # last event id per agent, for automatic parent links
        self._last_by_agent: dict[str, str] = {}
        # Prompts and outputs, in the sidecar D-010 deferred until replay
        # needed it. Opened alongside the trace so a caller cannot end up with
        # a trace whose refs point at a file that was never created.
        self.content = ContentStore(content_path_for(self.path), append=append)

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

    def log_check(
        self,
        source_id_: str,
        target_event: str,
        verdict: CheckVerdict,
        method: InfluenceMethod,
        confidence: float = 1.0,
        signature_before: str | None = None,
        signature_after: str | None = None,
        comparator: str | None = None,
        repeats: int = 0,
        notes: str = "",
    ) -> CheckRecord:
        """Record that a (source, event) pair was examined (D-024).

        Write one of these for **both** outcomes. The negative is the one that
        matters and the one the old code could not express: an examined pair
        with no influence edge is cleared, an unexamined pair is unknown, and
        without this record those two are the same absence.

        A `clean` verdict here is what lets the contamination walk stop. That
        is also what makes it the dangerous direction of error, so `method` and
        `confidence` are not decoration -- the planner is entitled to weigh a
        counterfactual `clean` differently from a self-reported one.
        """
        record = CheckRecord(
            source_id=source_id_,
            target_event=target_event,
            verdict=verdict,
            method=method,
            confidence=confidence,
            signature_before=signature_before,
            signature_after=signature_after,
            comparator=comparator,
            repeats=repeats,
            notes=notes,
        )
        self.checks.append(record)
        self._write({"record": "check", **record.to_dict()})
        return record

    # --- content ------------------------------------------------------------

    def put_content(
        self, text: str, kind: str = "output", meta: dict[str, Any] | None = None
    ) -> str:
        """Store event content and return the ref to put in inputs_ref /
        output_ref. See src/common/content.py for why refs are hashes."""
        return self.content.put(text, kind=kind, meta=meta)

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
        self.content.close()

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
    checks: list[CheckRecord] = field(default_factory=list)
    usage: list[UsageRecord] = field(default_factory=list)
    # Populated by read_trace() when the content sidecar exists beside the
    # trace. None means the refs in this trace cannot be resolved, which is a
    # legitimate state (the graphs never need content) and one that replay has
    # to refuse to guess around.
    content: ContentStore | None = None
    path: Path | None = None

    def __post_init__(self) -> None:
        self._events_by_id = {e.id: e for e in self.events}
        self._sources_by_id = {s.id: s for s in self.sources}
        self._checks_by_pair = {c.pair: c for c in self.checks}

    def event(self, eid: str) -> Event:
        return self._events_by_id[eid]

    def source(self, sid: str) -> Source:
        return self._sources_by_id[sid]

    def has_event(self, eid: str) -> bool:
        return eid in self._events_by_id

    # --- checked(event, source), the D-024 lookup --------------------------

    def checked(self, event_id_: str, source_id_: str) -> CheckVerdict:
        """clean | tainted | unchecked, for one (event, source) pair.

        `unchecked` is returned for the absence of a record, which is the
        distinction the trace could not previously make. Step 0 of the recovery
        algorithm defaults `unchecked` into the taint set but keeps it
        distinguishable from a confirmed `tainted`, because the two cost
        different amounts to resolve.
        """
        record = self._checks_by_pair.get((source_id_, event_id_))
        return record.verdict if record else "unchecked"

    def check_record(self, event_id_: str, source_id_: str) -> CheckRecord | None:
        return self._checks_by_pair.get((source_id_, event_id_))

    def checked_pairs(self) -> set[tuple[str, str]]:
        """Every (source, event) pair that was actually examined.

        This is the honest version of `metrics.all_exposure_pairs()`, which
        asserted that everything had been examined and could turn "no analysis
        has run" into "nothing was influenced" (D-025 point 3). Here the set is
        read off records that exist.
        """
        return set(self._checks_by_pair)

    def unchecked_pairs(self) -> set[tuple[str, str]]:
        """Exposures nobody examined. What the conservative fallback taints."""
        return {
            (sid, e.id)
            for e in self.events
            for sid in e.exposures
            if (sid, e.id) not in self._checks_by_pair
        }

    def content_of(self, ref: str | None) -> str | None:
        """Resolve a content ref, or None if there is nothing to resolve."""
        if ref is None:
            return None
        if self.content is None:
            raise ValueError(
                f"trace has no content sidecar, so {ref} cannot be resolved. "
                "Expected a .content.jsonl beside the trace file."
            )
        return self.content.get(ref)

    def output_text(self, eid: str) -> str | None:
        """What an event produced, or None for events with no output."""
        return self.content_of(self.event(eid).output_ref)

    def prompt_text(self, eid: str) -> str | None:
        """The prompt that produced an event, or None if it was not an LLM call."""
        return self._input_of_kind(eid, "prompt")

    def system_text(self, eid: str) -> str | None:
        return self._input_of_kind(eid, "system")

    def source_block_text(self, eid: str) -> str | None:
        """The rendered source list inside this event's prompt.

        Stored separately from the prompt so that redacting one source never
        requires inferring where the source list ends -- see the docstring of
        src/common/prompts.py. Absent for events with no sources in context.
        """
        return self._input_of_kind(eid, "source_block")

    def tool_args_text(self, eid: str) -> str | None:
        """The arguments our own code passed to a tool, or None.

        Distinct from `prompt_text` in the way that matters most here: a prompt
        is what an agent was *shown*, and tool arguments are what our code
        actually *handed over*. The Executor's `tool_call` is the case that
        needs it -- it has no output at all, and the script it is about to run
        is only visible as its argument.
        """
        return self._input_of_kind(eid, "tool_args")

    def _input_of_kind(self, eid: str, kind: str) -> str | None:
        for ref in self.event(eid).inputs_ref:
            if self.content is not None and self.content.has(ref):
                if self.content.record(ref).kind == kind:
                    return self.content.get(ref)
        return None

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
        if len(self._checks_by_pair) != len(self.checks):
            # Two verdicts on one pair means one of them is stale, and which
            # one wins would be decided by dict order. A pair is examined once
            # per run; an escalated re-examination belongs to the new trace
            # that replay appends, not to this one.
            counts: dict[tuple[str, str], int] = {}
            for c in self.checks:
                counts[c.pair] = counts.get(c.pair, 0) + 1
            duplicated = sorted(p for p, n in counts.items() if n > 1)
            raise ValueError(f"trace has two check records for pair(s) {duplicated[:5]}")
        for check in self.checks:
            if check.target_event not in self._events_by_id:
                raise ValueError(
                    f"check record targets unknown event {check.target_event}"
                )
            if check.source_id not in self._sources_by_id:
                raise ValueError(f"check record for unknown source {check.source_id}")
            # A verdict about a pair that was never an exposure is either a
            # typo or an analysis run against the wrong trace. Both produce
            # confident-looking nonsense downstream.
            if check.source_id not in self.event(check.target_event).exposures:
                raise ValueError(
                    f"check record says {check.source_id} was examined against "
                    f"{check.target_event}, but that source was never in that "
                    "event's context"
                )
        # An influence edge is a positive claim, so the pair it names must not
        # also carry a `clean` verdict. Contradicting records would let the
        # contamination walk and the metric disagree about the same pair.
        for edge in self.influence:
            record = self._checks_by_pair.get((edge.source_id, edge.target_event))
            if record is not None and record.verdict == "clean":
                raise ValueError(
                    f"({edge.source_id}, {edge.target_event}) has an influence "
                    f"edge and a 'clean' check record. One of them is wrong."
                )


def read_trace(path: str | Path, content: bool = True) -> Trace:
    """Read a JSONL trace file written by TraceLogger.

    `content=True` attaches the content sidecar if one exists beside the trace,
    so `trace.output_text(eid)` works. Set it False to read metadata only,
    which is all the graphs need and is cheaper on a verbose run.
    """
    meta: dict[str, Any] = {}
    events: list[Event] = []
    sources: list[Source] = []
    influence: list[InfluenceEdge] = []
    checks: list[CheckRecord] = []
    usage: list[UsageRecord] = []
    for line_no, record in enumerate(_read_records(Path(path)), start=1):
        kind = record.get("record")
        if kind == "meta":
            # MERGE, do not replace. A trace can carry more than one meta
            # record: `refine_for_verdict()` reopens the log with
            # `append=True, meta={"record_kind": "refinement"}`, which writes a
            # second one. Replacing here dropped the entire original header --
            # model, task, attributor, settings fingerprint -- for every trace
            # that went through refinement, which is the whole hybrid campaign
            # path. It surfaced as `score_run()` labelling a live
            # gemini-3.6-flash result "unknown", and again when replay could
            # not read the workflow shape it needed off the header.
            meta.update({k: v for k, v in record.items() if k != "record"})
        elif kind == "event":
            events.append(Event.from_dict(record))
        elif kind == "source":
            sources.append(Source.from_dict(record))
        elif kind == "influence":
            influence.append(InfluenceEdge.from_dict(record))
        elif kind == "check":
            checks.append(CheckRecord.from_dict(record))
        elif kind == "usage":
            usage.append(UsageRecord.from_dict(record))
        else:
            raise ValueError(f"{path}:{line_no}: unknown record type {kind!r}")

    store: ContentStore | None = None
    if content:
        sidecar = content_path_for(path)
        if sidecar.exists():
            store = ContentStore.load(sidecar)

    return Trace(
        meta=meta,
        events=events,
        sources=sources,
        influence=influence,
        checks=checks,
        usage=usage,
        content=store,
        path=Path(path),
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
