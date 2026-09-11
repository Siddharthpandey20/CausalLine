"""
The testbed pipeline, running on Gemini.

    User -> Planner -> Researcher -> Coder -> Executor
                          |            |
                       web, db,     code tool,
                       memory       memory

Same shape as docs/02-architecture.md. Every operation is logged as an event
with parents and exposures, every API call's tokens are logged with a
purpose, and the task has a checkable outcome: the Executor runs the
generated script and compares its output to known-correct values from the
database fixture, so task success never depends on text matching.

`src/tracing/fake_pipeline.py` remains the offline no-quota path and is not
affected by anything here.

Run from the repo root, with GEMINI_API_KEY in .env:

    python -m src.tracing.pipeline data/runs/run1.jsonl

One design point worth knowing before reading the code: the Researcher makes
**one API call per finding**, not one call producing several findings. Work
preserved is counted per event (D-012), so replay cost has to be countable
per event too. A single call producing four outputs would make "the cost of
recomputing one finding" undefined, and that number is half of open issue #7.

Three things this module does that it did not before, all of them prerequisites
for recovery rather than improvements to the run:

  * **content is stored.** Every prompt, system instruction, output, tool
    argument and memory value goes into the content sidecar and the event
    carries the ref (D-027). `inputs_ref` and `output_ref` were schema fields
    pointing at nothing (D-010); selective replay is what needed them, so they
    now resolve.
  * **influence is established during the run.** Each event is handed to an
    attributor (`src/provenance/attribution.py`), which writes the influence
    edges and `check` records. Before this the live pipeline wrote no influence
    edges at all, so every trace it produced showed exposure and nothing else
    -- and every downstream number was the conservative fallback's, not the
    method's.
  * **events the model did not write are attributed exactly.** Tool calls, tool
    responses and memory operations are computed by this code, so which inputs
    reached them is read off the code path rather than estimated. That is
    roughly half the events in a run, and leaving them unchecked hands all of
    them to the conservative fallback.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.common.cassette import Cassette, CassetteClient
from src.common.config import Settings, load_settings
from src.common.llm import GeminiClient, LLMError, LLMResponse, QuotaExhausted
from src.common.models import Event
from src.common.prompts import render_sources
from src.provenance.attribution import (
    AttributionRequest,
    Attributor,
    NullAttributor,
    derived_links_for,
    record_carrier,
    record_structural,
)


def source_producer_of(source: Any) -> str | None:
    """The event a source is the output of, if any.

    Mirrors `planner.source_producer()` but takes the Source object, because
    the pipeline holds sources in a dict and has no Trace to look them up in
    while the run is still going.
    """
    if source is None:
        return None
    return getattr(source, "derived_from", None) or getattr(
        source, "origin_event", None
    )
from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for, overhead
from src.tracing.logger import TraceLogger, read_trace
from src.tracing.tools import Tools

DEFAULT_TASK = (
    "Write a single self-contained Python script that parses this project's "
    "sample date strings and prints each one as an ISO date (YYYY-MM-DD), one "
    "per line, in the order given."
)

PLANNER_SYSTEM = (
    "You are the Planner in a four-agent pipeline (Planner, Researcher, Coder, "
    "Executor). Break the user's task into research questions for the "
    "Researcher and write a short brief for the rest of the pipeline. "
    "Answer with JSON only."
)
RESEARCHER_SYSTEM = (
    "You are the Researcher. Answer the question using only the numbered "
    "sources given to you. Be specific and brief: at most four sentences. "
    "If the sources do not answer the question, say so."
)
CODER_SYSTEM = (
    "You are the Coder. You receive the Researcher's findings and must produce "
    "working Python. The execution environment has the standard library only "
    "and no network access."
)
REVIEWER_SYSTEM = (
    "You are the Reviewer. You receive a Python script and the Researcher's "
    "findings. Return the script you would run, corrected if it is wrong and "
    "unchanged if it is right. Reply with the script and nothing else: no "
    "markdown fences, no commentary."
)


@dataclass
class PipelineResult:
    trace_path: Path
    task_success: bool
    stdout: str
    stderr: str
    tokens: int
    events: int
    throttled_s: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


class GeminiPipeline:
    """One instance per run. Not reusable: the event ids and the source ids
    belong to a single trace."""

    def __init__(
        self,
        log: TraceLogger,
        client: GeminiClient,
        tools: Tools,
        task: str = DEFAULT_TASK,
        checkpoints: CheckpointStore | None = None,
        attributor: Attributor | None = None,
        handoff_hook: Any = None,
        research_rounds: int = 1,
        reviewer: bool = False,
        checkpoint_interval: float | None = None,
    ) -> None:
        self.log = log
        self.client = client
        self.tools = tools
        self.task = task
        self.checkpoints = checkpoints
        # PHASE C: the workflow's length is a parameter, not a constant.
        #
        # `research_rounds > 1` makes the Researcher search again with a query
        # built from what it just found, so round 2's sources are causally
        # downstream of round 1's. `reviewer=True` inserts a Reviewer agent
        # between the Coder and the Executor which may revise the script, so
        # the chain to the final output is researcher -> coder -> reviewer ->
        # executor rather than researcher -> coder -> executor.
        #
        # Both default to the original shape. That is deliberate: the scaling
        # claim in docs/06 needs the short and long workflows to *both* exist
        # so A/N can be compared across lengths. Replacing the short one would
        # move the measurement rather than extend it, and would silently
        # invalidate every number already in the repository.
        self.research_rounds = max(1, int(research_rounds))
        self.reviewer = bool(reviewer)
        # PHASE D: the Young/Daly interval, wired to the run that produces it.
        #
        # `checkpoint_interval()` and `measured_interval()` have existed since
        # Phase 3a and computed a number nothing consumed: the pipeline still
        # checkpointed once per agent boundary, whatever the formula said. This
        # is the half docs/07 recorded as deferred, and it is the half that
        # makes GC mean anything -- GC bounds a dense checkpoint stream, and a
        # policy of one-per-agent never produces one.
        #
        # None keeps the original boundary-only policy, so every existing
        # measurement stands. A float is a spacing in events: a checkpoint is
        # taken whenever that many events have been logged since the last one,
        # in addition to the agent-boundary checkpoints recovery's safe
        # frontier depends on. Additive rather than replacing, because dropping
        # a boundary checkpoint would remove a rewind point Step 1 relies on
        # and would confound this measurement with a recovery regression.
        self.checkpoint_interval = (
            float(checkpoint_interval) if checkpoint_interval else None
        )
        self._events_since_checkpoint = 0
        if self.checkpoint_interval and self.checkpoints is not None:
            self._install_interval_checkpointing()
        # Optional injection point for scenario C: a compromised inter-agent
        # message. Called with (from_agent, to_agent, texts) and returns extra
        # message strings to append. None means no injection.
        self.handoff_hook = handoff_hook
        # Establishes influence edges for model-written events. Defaults to
        # NullAttributor, which is what the pipeline did before it had one:
        # exposure recorded, influence never established. That default is the
        # honest one -- an attributor costs one extra call per event, and a
        # caller should have to ask for that.
        self.attributor: Attributor = attributor or NullAttributor()
        # event id -> content ref of what it produced. Refs rather than text:
        # a checkpoint carries this dict, and carrying the text made checkpoints
        # grow with the square of run length (D-022 measured 56% of stored
        # bytes). See D-028.
        self._outputs: dict[str, str] = {}
        # source ids currently in each agent's context -> Event.exposures
        self.context: dict[str, list[str]] = {}
        self._sources: dict[str, Any] = {}

    # --- helpers -----------------------------------------------------------

    def expose(self, agent: str, *source_ids: str) -> None:
        """Put sources into an agent's context. Everything here is an exposure
        and gets recorded on every event that agent logs from now on, whether
        or not the agent uses it. That is the point."""
        seen = self.context.setdefault(agent, [])
        for sid in source_ids:
            if sid not in seen:
                seen.append(sid)

    def _label(self, source_id: str) -> str:
        """One-line description of a source, for the self-report catalogue."""
        s = self._sources[source_id]
        where = s.metadata.get("url") or s.metadata.get("key") or s.kind
        return f"{s.kind}, {where}"

    def _influenced_by(self, event_id: str) -> list[str]:
        """Source ids with an influence edge into `event_id`, so far.

        Read back off the log rather than kept in a parallel dict, so a carrier
        event can only ever inherit an edge that was actually recorded.
        """
        return [e.source_id for e in self.log.influence if e.target_event == event_id]

    def _call(
        self,
        agent: str,
        kind: str,
        prompt: str,
        system: str | None = None,
        parents: list[str] | None = None,
        json_output: bool = False,
        source_block: str | None = None,
    ) -> tuple[Event, LLMResponse]:
        """One API call, one event, one usage record, one attribution.

        Kept together so a call can never be made without its tokens being
        logged, its prompt and output being stored, and its influence being
        established. Each of those three was a separate omission that cost the
        project something: untracked tokens make open issue #7 unanswerable, an
        unstored prompt makes counterfactual replay impossible, and unrecorded
        influence makes every result the conservative fallback's.

        `source_block` is the rendered source list embedded in `prompt`. Stored
        separately so that a counterfactual redaction operates on a known span
        instead of inferring where the source list ends -- some of our prompts
        put instructions after it, and a redaction that removes only part of a
        source reports "no influence" for the wrong reason.
        """
        response = self.client.generate(prompt, system=system, json_output=json_output)
        prompt_ref = self.log.put_content(prompt, kind="prompt", meta={"agent": agent})
        refs = [prompt_ref]
        if source_block:
            refs.append(
                self.log.put_content(
                    source_block, kind="source_block", meta={"agent": agent}
                )
            )
        if system:
            refs.append(self.log.put_content(system, kind="system", meta={"agent": agent}))
        output_ref = self.log.put_content(
            response.text, kind="output", meta={"agent": agent, "event_kind": kind}
        )
        event = self.log.log_event(
            agent,
            kind,
            parents=parents,
            inputs_ref=refs,
            exposures=self.context.get(agent, []),
            output_ref=output_ref,
        )
        self.log.log_usage(
            "pipeline",
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.total_tokens,
            event_id_=event.id,
            agent_id=agent,
            thoughts_tokens=response.thoughts_tokens,
            attempts=response.attempts,
            latency_s=response.latency_s,
            slept_s=response.slept_s,
        )
        self._outputs[event.id] = output_ref
        self.attributor.attribute(
            AttributionRequest(
                event_id=event.id,
                agent_id=agent,
                kind=kind,
                output=response.text,
                exposures=list(event.exposures),
                labels={sid: self._label(sid) for sid in event.exposures},
                prompt=prompt,
                source_block=source_block,
                system=system,
                # Recorded derived_from links among what this agent can see,
                # so the estimator can merge a summary with its own inputs
                # into one atomic test unit (D-051). Read off the partial log
                # mid-run; `request_for()` computes the same thing from a
                # finished trace, through the same helper.
                derived_links=derived_links_for(
                    list(event.exposures),
                    lambda sid: source_producer_of(self._sources.get(sid)),
                    self._influenced_by,
                ),
            ),
            self.log,
        )
        return event, response

    def _install_interval_checkpointing(self) -> None:
        """Count logged events and checkpoint every `checkpoint_interval`.

        Wrapping the logger is the least invasive hook available: every event
        in this pipeline goes through `log_event`, so counting there cannot
        miss one, whereas threading a call through each of the ~20 log sites
        would silently skip whichever site a later edit forgot.
        """
        inner = self.log.log_event

        def counted(*args: Any, **kwargs: Any):
            event = inner(*args, **kwargs)
            self._events_since_checkpoint += 1
            if (
                self.checkpoint_interval
                and self._events_since_checkpoint >= self.checkpoint_interval
            ):
                self._checkpoint(event.id, event.agent_id)
            return event

        self.log.log_event = counted  # type: ignore[method-assign]

    def _checkpoint(self, event_id: str, agent_id: str) -> None:
        """Checkpoint after an agent boundary (docs/02-architecture.md, v1
        policy), and on the Young/Daly interval when one is configured. Cost is
        measured rather than assumed: see overhead()."""
        self._events_since_checkpoint = 0
        if self.checkpoints is None:
            return
        self.checkpoints.take(
            event_id=event_id,
            agent_id=agent_id,
            state={
                "task": self.task,
                "context": {a: list(ids) for a, ids in self.context.items()},
                # Content refs, not text (D-028). Restoring resolves them
                # against the content sidecar, which holds one copy of each
                # output however many checkpoints mention it.
                "output_refs": dict(self._outputs),
            },
            memory=dict(self.tools.memory),
        )

    def _source_block(self, ids: list[str]) -> str:
        """Render sources for a prompt, labelled with their trace ids.

        The ids are visible to the agent on purpose: self-report asks each agent
        which inputs it used, and it can only answer in ids if it saw them.

        The format itself lives in `src/common/prompts.py`, because a
        counterfactual check has to take one of these blocks back *out* of a
        stored prompt and a redaction that quietly removes the wrong text
        reports "no influence" -- an unsafe preservation caused by a formatting
        mismatch, and indistinguishable from a real verdict.
        """
        return render_sources(
            [
                (sid, self._sources[sid].kind, self._label_where(sid), self._sources[sid].content)
                for sid in ids
            ]
        )

    def _label_where(self, source_id: str) -> str:
        s = self._sources[source_id]
        return str(s.metadata.get("url") or s.metadata.get("key") or s.kind)

    # --- the run ------------------------------------------------------------

    def run(self) -> PipelineResult:
        def remember(source):
            self._sources[source.id] = source
            return source

        # --- user ----------------------------------------------------------
        user_event = self.log.log_event(
            "user",
            "message",
            parents=[],
            output_ref=self.log.put_content(self.task, kind="output", meta={"agent": "user"}),
        )
        task_source = remember(
            self.log.log_source("user_input", self.task, origin_event=user_event.id)
        )
        self.expose("planner", task_source.id)

        # --- planner ---------------------------------------------------------
        # The task goes in as a labelled source rather than inline text. It is
        # the Planner's only input, so inlining it looked harmless -- but then
        # the self-report question asks the Planner about [S1] while its prompt
        # never contained that id, and a counterfactual has no span to redact.
        # Every model-written event's inputs are addressable the same way now.
        planner_block = self._source_block(self.context["planner"])
        plan_event, plan_response = self._call(
            "planner",
            "plan",
            prompt=(
                "Reply with JSON: {\"brief\": str, \"questions\": [str, str, str]}. "
                "Exactly three research questions, each answerable from "
                "documentation about parsing dates in Python.\n\n"
                f"Task:\n{planner_block}"
            ),
            system=PLANNER_SYSTEM,
            parents=[user_event.id],
            json_output=True,
            source_block=planner_block,
        )
        plan = plan_response.json()
        questions = [str(q) for q in plan.get("questions", [])][:3] or [self.task]
        brief = str(plan.get("brief", "")).strip() or self.task

        handoff = self.log.log_event(
            "planner",
            "message",
            parents=[plan_event.id],
            exposures=self.context.get("planner", []),
            output_ref=self.log.put_content(brief, kind="output", meta={"agent": "planner"}),
        )
        # The hand-off is a carrier: its text is a slice of the plan event's
        # output, so it inherits that event's influence set rather than being
        # attributed on its own.
        record_carrier(
            self.log,
            handoff.id,
            list(handoff.exposures),
            self._influenced_by(plan_event.id),
            plan_event.id,
        )
        brief_source = remember(
            self.log.log_source(
                "agent_message",
                brief,
                origin_event=handoff.id,
                derived_from=plan_event.id,
            )
        )
        self.expose("researcher", brief_source.id)
        self._checkpoint(handoff.id, "planner")

        # --- researcher: tools -------------------------------------------------
        web_query = " ".join(questions)
        web_call = self.log.log_event(
            "researcher",
            "tool_call",
            parents=[handoff.id],
            tool_id="web",
            exposures=self.context.get("researcher", []),
            inputs_ref=[
                self.log.put_content(
                    json.dumps({"tool": "web", "query": web_query}, sort_keys=True),
                    kind="tool_args",
                    meta={"tool": "web"},
                )
            ],
        )
        # The query is built from the Planner's questions, i.e. from the plan
        # event's output, so this call carries that event's influence too.
        record_carrier(
            self.log,
            web_call.id,
            list(web_call.exposures),
            self._influenced_by(plan_event.id),
            plan_event.id,
        )
        pages = self.tools.web_search(web_query)
        web_response = self.log.log_event(
            "researcher",
            "tool_response",
            tool_id="web",
            exposures=self.context.get("researcher", []),
            output_ref=self.log.put_content(
                json.dumps([p.to_dict() for p in pages], sort_keys=True),
                kind="output",
                meta={"tool": "web"},
            ),
        )
        record_carrier(
            self.log,
            web_response.id,
            list(web_response.exposures),
            self._influenced_by(web_call.id),
            web_call.id,
        )
        for page in pages:
            source = remember(
                self.log.log_source(
                    "web",
                    page.content,
                    origin_event=web_response.id,
                    metadata={"url": page.url, "title": page.title},
                )
            )
            self.expose("researcher", source.id)

        db_keys = ("environment/installed_packages", "task/date_samples", "task/day_first")
        db_call = self.log.log_event(
            "researcher",
            "tool_call",
            tool_id="db",
            exposures=self.context.get("researcher", []),
            inputs_ref=[
                self.log.put_content(
                    json.dumps({"tool": "db", "keys": list(db_keys)}, sort_keys=True),
                    kind="tool_args",
                    meta={"tool": "db"},
                )
            ],
        )
        # The keys are literals in this code. Nothing in the agent's context
        # reached them, and that is a fact about the code rather than an
        # estimate -- which makes these the cheapest clean records in a trace.
        record_structural(
            self.log,
            db_call.id,
            list(db_call.exposures),
            used=[],
            why="database keys are literals in the pipeline, not derived from any source",
        )
        db_values = {key: self.tools.db_lookup(key) for key in db_keys}
        db_response = self.log.log_event(
            "researcher",
            "tool_response",
            tool_id="db",
            exposures=self.context.get("researcher", []),
            output_ref=self.log.put_content(
                json.dumps(db_values, sort_keys=True), kind="output", meta={"tool": "db"}
            ),
        )
        record_structural(
            self.log,
            db_response.id,
            list(db_response.exposures),
            used=[],
            why="fixture lookup determined by literal keys",
        )
        for key in db_keys:
            source = remember(
                self.log.log_source(
                    "database",
                    json.dumps(db_values[key]),
                    origin_event=db_response.id,
                    metadata={"key": key},
                )
            )
            self.expose("researcher", source.id)

        # --- researcher: one call per finding ------------------------------------
        findings: list[tuple[Event, str]] = []
        exposed = self.context["researcher"]
        for question in questions:
            block = self._source_block(exposed)
            event, response = self._call(
                "researcher",
                "agent_output",
                prompt=f"Question: {question}\n\nSources:\n{block}",
                system=RESEARCHER_SYSTEM,
                parents=[db_response.id],
                source_block=block,
            )
            findings.append((event, response.text.strip()))

        # --- researcher: further rounds (Phase C) -------------------------
        # Each extra round searches again with a query built from what the
        # previous round found, so the new sources are causally downstream of
        # the old ones rather than a second independent batch. That is the
        # property the scaling measurement needs: a longer trace whose later
        # events have genuinely larger candidate sets, because every earlier
        # round's sources are still in context.
        #
        # The follow-up query is derived from finding text, so the tool_call
        # carries the findings' influence -- a structural "keys are literals"
        # record would be false here, which is exactly the distinction D-012
        # exists to keep.
        seen_urls = {
            self._sources[sid].metadata.get("url")
            for sid in self.context.get("researcher", [])
            if getattr(self._sources.get(sid), "metadata", None)
        }
        for extra_round in range(2, self.research_rounds + 1):
            prior = " ".join(text for _, text in findings)
            followup_query = " ".join(sorted({
                word.strip(".,()[]:;").lower()
                for word in prior.split()
                if len(word.strip(".,()[]:;")) > 4
            }))[:400] or web_query
            followup_call = self.log.log_event(
                "researcher",
                "tool_call",
                parents=[findings[-1][0].id],
                tool_id="web",
                exposures=self.context.get("researcher", []),
                inputs_ref=[
                    self.log.put_content(
                        json.dumps(
                            {"tool": "web", "query": followup_query,
                             "round": extra_round},
                            sort_keys=True,
                        ),
                        kind="tool_args",
                        meta={"tool": "web", "round": extra_round},
                    )
                ],
            )
            record_carrier(
                self.log,
                followup_call.id,
                list(followup_call.exposures),
                self._influenced_by(findings[-1][0].id),
                findings[-1][0].id,
            )
            fresh = [
                page
                for page in self.tools.web_search(followup_query, limit=8)
                if page.url not in seen_urls
            ]
            followup_response = self.log.log_event(
                "researcher",
                "tool_response",
                tool_id="web",
                exposures=self.context.get("researcher", []),
                output_ref=self.log.put_content(
                    json.dumps([pg.to_dict() for pg in fresh], sort_keys=True),
                    kind="output",
                    meta={"tool": "web", "round": extra_round},
                ),
            )
            record_carrier(
                self.log,
                followup_response.id,
                list(followup_response.exposures),
                self._influenced_by(followup_call.id),
                followup_call.id,
            )
            for page in fresh:
                seen_urls.add(page.url)
                source = remember(
                    self.log.log_source(
                        "web",
                        page.content,
                        origin_event=followup_response.id,
                        metadata={"url": page.url, "title": page.title},
                    )
                )
                self.expose("researcher", source.id)

            block = self._source_block(self.context["researcher"])
            question = (
                "Given what you have found so far, what remains unresolved "
                "about parsing these date strings correctly?"
            )
            event, response = self._call(
                "researcher",
                "agent_output",
                prompt=f"Question: {question}\n\nSources:\n{block}",
                system=RESEARCHER_SYSTEM,
                parents=[followup_response.id],
                source_block=block,
            )
            findings.append((event, response.text.strip()))
            self._checkpoint(event.id, "researcher")

        extra_messages: list[str] = []
        if self.handoff_hook is not None:
            extra_messages = list(
                self.handoff_hook(
                    "researcher", "coder", [text for _, text in findings]
                )
                or []
            )

        to_coder = self.log.log_event(
            "researcher",
            "message",
            parents=[e.id for e, _ in findings],
            exposures=self.context.get("researcher", []),
            output_ref=self.log.put_content(
                json.dumps([text for _, text in findings] + extra_messages, sort_keys=True),
                kind="output",
                meta={"agent": "researcher"},
            ),
        )
        # Carries all the findings at once, so it inherits the union of their
        # influence sets. A source that influenced none of the findings did not
        # influence the message that hands them on.
        inherited: list[str] = []
        for event, _ in findings:
            inherited.extend(self._influenced_by(event.id))
        record_carrier(
            self.log,
            to_coder.id,
            list(to_coder.exposures),
            inherited,
            ", ".join(e.id for e, _ in findings),
        )
        for event, text in findings:
            source = remember(
                self.log.log_source(
                    "agent_message",
                    text,
                    origin_event=to_coder.id,
                    derived_from=event.id,
                )
            )
            self.expose("coder", source.id)
        for extra in extra_messages:
            # Planted inter-agent message: not derived from a model event in
            # this run, so derived_from stays empty. The marker in the text
            # is what label_malicious() will find.
            source = remember(
                self.log.log_source(
                    "agent_message",
                    extra,
                    origin_event=to_coder.id,
                    metadata={"injected": True, "channel": "researcher->coder"},
                )
            )
            self.expose("coder", source.id)

        # --- coder ----------------------------------------------------------------
        self._checkpoint(to_coder.id, "researcher")

        memory_keys = ("style/preferences", "style/output")
        read_values = {
            key: self.tools.memory_read(key)
            for key in memory_keys
            if self.tools.memory_read(key) is not None
        }
        memory_event = self.log.log_event(
            "coder",
            "memory_read",
            parents=[to_coder.id],
            exposures=self.context.get("coder", []),
            inputs_ref=[
                self.log.put_content(
                    json.dumps({"keys": list(memory_keys)}, sort_keys=True),
                    kind="tool_args",
                    meta={"tool": "memory"},
                )
            ],
            # The key/value pairs live with the event that read them, so
            # verification can tell whether a live memory entry still points at
            # something an invalidated event wrote.
            output_ref=self.log.put_content(
                json.dumps(read_values, sort_keys=True),
                kind="memory",
                meta={"tool": "memory", "keys": list(read_values)},
            ),
        )
        record_structural(
            self.log,
            memory_event.id,
            list(memory_event.exposures),
            used=[],
            why="memory keys are literals in the pipeline; the read consults no source",
        )
        for key, value in read_values.items():
            source = remember(
                self.log.log_source(
                    "memory",
                    value,
                    origin_event=memory_event.id,
                    metadata={"key": key},
                )
            )
            self.expose("coder", source.id)

        samples = self.tools.db_lookup("task/date_samples") or []
        coder_sources = self.context["coder"]
        coder_block = self._source_block(coder_sources)

        decision_event, decision_response = self._call(
            "coder",
            "decision",
            prompt=(
                f"Task:\n{self.task}\n\nDate samples: {json.dumps(samples)}\n\n"
                "Decide the approach in at most three sentences. State which "
                "library you will use and why.\n\n"
                f"Inputs:\n{coder_block}"
            ),
            system=CODER_SYSTEM,
            parents=[memory_event.id],
            source_block=coder_block,
        )

        code_event, code_response = self._call(
            "coder",
            "agent_output",
            prompt=(
                f"Task:\n{self.task}\n\nDate samples: {json.dumps(samples)}\n\n"
                f"Approach you chose:\n{decision_response.text.strip()}\n\n"
                "Reply with the complete Python script and nothing else. No "
                "markdown fences, no commentary. The script must hardcode the "
                "samples and print one ISO date per line.\n\n"
                f"Inputs:\n{coder_block}"
            ),
            system=CODER_SYSTEM,
            parents=[decision_event.id],
            source_block=coder_block,
        )
        code = _strip_fences(code_response.text)

        # --- reviewer (Phase C) --------------------------------------------
        # A fifth agent between the Coder and the Executor. It matters for the
        # measurement in a way a fourth Researcher round does not: it puts a
        # model-written event *on the path to the final output*, so the
        # contamination chain becomes researcher -> coder -> reviewer ->
        # executor and every recovery decision has one more hop to reason
        # about. It also gives the Coder a second checkpoint, which is the
        # condition GC needs before it can free anything -- GC never drops an
        # agent's most recent checkpoint, so an agent with exactly one is
        # untouchable by construction.
        review_event = None
        if self.reviewer:
            to_reviewer = self.log.log_event(
                "coder",
                "message",
                parents=[code_event.id],
                exposures=self.context.get("coder", []),
                output_ref=self.log.put_content(
                    code, kind="output", meta={"agent": "coder", "to": "reviewer"}
                ),
            )
            record_carrier(
                self.log,
                to_reviewer.id,
                list(to_reviewer.exposures),
                self._influenced_by(code_event.id),
                code_event.id,
            )
            draft_source = remember(
                self.log.log_source(
                    "agent_message",
                    code,
                    origin_event=to_reviewer.id,
                    derived_from=code_event.id,
                )
            )
            self.expose("reviewer", draft_source.id)
            # THE REVIEWER REVIEWS THE ARTEFACT, NOT THE RESEARCH.
            # It gets the draft script and the environment facts, and not the
            # web pages and findings that produced the draft.
            #
            # That is a measured decision, not a modelling preference. The
            # first version exposed the Coder's whole context as well, which
            # reads as the more generous choice. It zeroed the Reviewer out
            # completely: the draft script and the sources behind it carry the
            # same fact -- which library to use -- so leave-one-out found
            # neither individually necessary, `e0022` came out with no
            # influence edges at all, contamination stopped at the Reviewer,
            # and every recovery escalated to restart_all at 0% preserved.
            #
            # This is the redundancy blind spot `_answer_task` documents,
            # reached structurally rather than by coincidence: put a summary
            # and its own inputs in one context and single-source
            # counterfactuals report nothing. Worth stating in the paper --
            # it is a real limit of counterfactual influence, and a pipeline
            # can walk into it by being generous with context.
            for sid in self.context.get("coder", []):
                if getattr(self._sources.get(sid), "kind", None) in (
                    "database",
                    "memory",
                ):
                    self.expose("reviewer", sid)
            self._checkpoint(to_reviewer.id, "coder")

            reviewer_block = self._source_block(self.context["reviewer"])
            review_event, review_response = self._call(
                "reviewer",
                "agent_output",
                prompt=(
                    f"Task:\n{self.task}\n\n"
                    "Review the script below. Return the script you would run.\n\n"
                    f"Script:\n{code}\n\n"
                    f"Inputs:\n{reviewer_block}"
                ),
                system=REVIEWER_SYSTEM,
                parents=[to_reviewer.id],
                source_block=reviewer_block,
            )
            reviewed = _strip_fences(review_response.text)
            if reviewed.strip():
                code = reviewed
            self._checkpoint(review_event.id, "reviewer")

        approach = decision_response.text.strip()
        write_event = self.log.log_event(
            "coder",
            "memory_write",
            parents=[code_event.id],
            exposures=self.context.get("coder", []),
            output_ref=self.log.put_content(
                json.dumps({"key": "last_run/approach", "value": approach}, sort_keys=True),
                kind="memory",
                meta={"tool": "memory", "key": "last_run/approach"},
            ),
        )
        self.tools.memory_write("last_run/approach", approach)
        # The written value is the decision event's output verbatim, so the
        # write inherits that event's influence. This is the edge that makes
        # rolling back a poisoned memory entry possible: without it, a memory
        # write looks like an operation that consulted nothing.
        record_carrier(
            self.log,
            write_event.id,
            list(write_event.exposures),
            self._influenced_by(decision_event.id),
            decision_event.id,
        )
        self._checkpoint(write_event.id, "coder")

        # --- coder -> executor hand-off ----------------------------------------------
        # The same agent-boundary bridge every other hand-off in this pipeline
        # already has: a carrier message event, the producing event's output
        # wrapped as a source with `derived_from`, then exposure to the
        # receiving agent. Planner->Researcher does it (`handoff` +
        # `brief_source`); Researcher->Coder does it (`to_coder` + one source
        # per finding). This boundary was the only one that did not, and the
        # omission was not cosmetic.
        #
        # WHAT IT COST TO BE MISSING
        # --------------------------
        # With nothing in the Executor's context, the Executor had zero
        # exposure edges and zero influence edges. Contamination stopped dead
        # at the Coder, `FinalOutput` was never in Taint, and
        # `malicious_to_output_paths()` returned an empty list on every trace
        # under every detector -- measured at 0 paths in 18 of 18
        # configurations. The planner's "treat each tainted event as a sink"
        # fallback then fired every time, collapsing Step 2 -- safe frontier,
        # domino pass, greedy cover over five action kinds -- into the single
        # rule "invalidate everything tainted". Fourteen of fourteen scored
        # configurations came out with `invalidation_set == taint.events`.
        #
        # The contamination walk was never wrong: it already crosses agent
        # boundaries on `derived_from` (src/provenance/contamination.py). The
        # edge simply did not exist.
        # Whoever wrote the script the Executor is about to run is the event
        # this hand-off derives from. With a Reviewer in the pipeline that is
        # the review, not the Coder's draft: `code` holds the reviewed text by
        # this point, and attributing it to the Coder would both lie about
        # provenance and leave the draft and the final script claiming the same
        # producer -- which `Trace.validate()` rejects, correctly, as one event
        # wrapped by two sources.
        producer = review_event if review_event is not None else code_event
        producing_agent = "reviewer" if review_event is not None else "coder"
        to_executor = self.log.log_event(
            producing_agent,
            "message",
            parents=[producer.id],
            exposures=self.context.get(producing_agent, []),
            output_ref=self.log.put_content(
                code, kind="output", meta={"agent": producing_agent}
            ),
        )
        # A carrier: its text is the producing event's output verbatim, so it
        # inherits that event's influence set rather than being attributed on
        # its own.
        record_carrier(
            self.log,
            to_executor.id,
            list(to_executor.exposures),
            self._influenced_by(producer.id),
            producer.id,
        )
        code_source = remember(
            self.log.log_source(
                "agent_message",
                code,
                origin_event=to_executor.id,
                derived_from=producer.id,
            )
        )
        self.expose("executor", code_source.id)

        # --- executor ---------------------------------------------------------------
        exec_call = self.log.log_event(
            "executor",
            "tool_call",
            parents=[code_event.id],
            tool_id="python",
            exposures=self.context.get("executor", []),
            inputs_ref=[
                self.log.put_content(code, kind="tool_args", meta={"tool": "python"})
            ],
        )
        result = self.tools.run_python(code)
        exec_response = self.log.log_event(
            "executor",
            "tool_response",
            tool_id="python",
            exposures=self.context.get("executor", []),
            output_ref=self.log.put_content(
                json.dumps(result, sort_keys=True), kind="output", meta={"tool": "python"}
            ),
        )

        # Checkable outcome: compare against known-correct values, no LLM judge.
        expected = [str(v) for v in (self.tools.db_lookup("task/expected_iso") or [])]
        produced = [ln.strip() for ln in result["stdout"].splitlines() if ln.strip()]
        success = bool(expected) and produced == expected
        final = self.log.log_event(
            "executor",
            "agent_output",
            parents=[exec_response.id],
            exposures=self.context.get("executor", []),
            output_ref=self.log.put_content(
                json.dumps(
                    {"expected": expected, "produced": produced, "success": success},
                    sort_keys=True,
                ),
                kind="output",
                meta={"agent": "executor"},
            ),
        )
        # All three Executor events are functions of one thing: the script.
        # `used=[]` was correct while nothing was in the Executor's context and
        # is wrong now that the script is -- it would write a `clean`
        # *structural* verdict for the very source the Executor executes, and a
        # structural clean is the one the clearance policy trusts most
        # (src/provenance/checks.py). That would be a false clearance of the
        # strongest available kind.
        #
        # Structural rather than estimated because it is read off the code
        # path: `run_python(code)` receives the script literally, the response
        # is that call's return value, and the comparison reads its stdout.
        for event, why in (
            (exec_call, "the tool call's argument is this script, verbatim"),
            (exec_response, "the response is the output of running this script"),
            (final, "the comparison reads the stdout this script produced"),
        ):
            record_structural(
                self.log,
                event.id,
                list(event.exposures),
                used=[code_source.id],
                why=why,
            )
        self._checkpoint(final.id, "executor")

        return PipelineResult(
            trace_path=self.log.path,
            task_success=success,
            stdout=result["stdout"],
            stderr=result["stderr"],
            tokens=self.client.total_tokens,
            events=len(self.log.events),
            throttled_s=getattr(self.client, "throttled_s", 0.0),
            detail={
                "expected": expected,
                "produced": produced,
                "returncode": result["returncode"],
                "code": code,
            },
        )


def _strip_fences(text: str) -> str:
    """Models add ```python fences even when told not to."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    text = text.split("\n", 1)[-1]
    return text.rsplit("```", 1)[0].strip()


def run_pipeline(
    path: str | Path,
    task: str = DEFAULT_TASK,
    settings: Settings | None = None,
    cassette_path: str | Path | None = None,
    cassette_mode: str = "replay",
    tools: Tools | None = None,
    client: Any = None,
    attributor: Attributor | None = None,
    handoff_hook: Any = None,
    research_rounds: int = 1,
    reviewer: bool = False,
    checkpoint_interval: float | None = None,
) -> PipelineResult:
    """Run the pipeline and write a trace, its checkpoints, and its memory.

    With `cassette_path`, calls are recorded or replayed instead of (or as
    well as) hitting the API. A replayed run is marked in the trace header so
    its numbers can never be mistaken for a fresh measurement (D-019).

    `tools` is the injection point D-014 promises: src/eval/ hands in a
    poisoned corpus and nothing in this module or in tools.py changes, so the
    agents meet a poisoned fixture exactly as they would meet a real one.
    Defaults to the clean fixtures.

    `client` overrides the LLM client entirely, for offline tests that drive
    the whole pipeline without an API key or a cassette.

    `attributor` decides how influence edges get established during the run.
    None means none are: the trace shows exposure only, and everything
    downstream falls back to treating exposure as influence. That is the
    default because attribution costs an extra call per event and a caller
    should have to ask for it -- but a run without one cannot demonstrate the
    exposure/influence gap, which is the paper's claim.
    """
    if cassette_path and cassette_mode == "replay":
        settings = _settings_or_offline(settings)
    elif client is not None:
        settings = settings or _settings_or_offline(settings)
    else:
        settings = settings or load_settings()
    if tools is None:
        tools = Tools.from_fixtures(memory_path=Path(path).with_suffix(".memory.json"))
    elif tools.memory_path is None:
        # Keep the per-run memory file behaviour even for an injected corpus,
        # so a poisoned run's memory writes do not leak into the next run.
        tools.memory_path = Path(path).with_suffix(".memory.json")

    meta: dict[str, Any] = {
        "pipeline": "gemini",
        "task": task,
        # In the header so a trace can be read for what analysis it carries
        # without scanning it for records. "none" is a meaningful value, not a
        # missing one, and it is the value every trace written before now had.
        "attributor": getattr(attributor, "name", "none"),
        # THE TRACE RECORDS ITS OWN SHAPE (Phase C).
        # `replay()` re-runs the pipeline and splices stored outputs by event
        # id, so it must rebuild a run of the *same* shape. Left to a caller to
        # remember, that desyncs silently and the failure is baffling: replay a
        # 27-event trace into a 19-event rerun and the ids still line up far
        # enough that the Executor ends up running a Researcher's prose as
        # Python. Recording it here means replay reads it off the trace and no
        # caller can forget.
        "research_rounds": research_rounds,
        "reviewer": reviewer,
        "checkpoint_interval": checkpoint_interval,
        **settings.fingerprint(),
    }
    if client is not None:
        # Marked in the header for the same reason a cassette is (D-019): a
        # run whose responses did not come from the model must never be
        # mistaken for one that did.
        meta["client"] = type(client).__name__
        # AND, WHEN THE CLIENT KNOWS BETTER, SO IS THE MODEL.
        # `settings` above is the *Gemini* configuration, because that is what
        # `load_settings()` returns and it is what a fingerprint is built from.
        # A run driven by an injected client is not a Gemini run, and until now
        # its header said `model: gemini-3.6-flash` regardless of what actually
        # answered. That is not cosmetic: docs/04's run hygiene rule is that a
        # trace records the model that produced it, `token_validation.
        # run_scenario` reads the model straight off this header, and a
        # per-model comparison built on it would have attributed every run to
        # one model. A client carrying its own settings gets to overwrite the
        # fields it owns.
        # A fingerprint on the CLIENT wins over one on its settings. Settings
        # carry the configured default model; a client can be pointed at a
        # different one, and then only the client knows which model actually
        # answered.
        own = getattr(client, "fingerprint", None)
        if not callable(own):
            own = getattr(getattr(client, "settings", None), "fingerprint", None)
        if callable(own) and type(client).__name__ != "CassetteClient":
            meta.update(own())
        elif getattr(client, "model", None):
            meta["model"] = client.model
    elif cassette_path:
        cassette = Cassette.load(cassette_path)
        live = GeminiClient(settings) if cassette_mode in ("record", "auto") else None
        client = CassetteClient(cassette, settings, inner=live, mode=cassette_mode)
        meta["cassette"] = cassette_mode
        meta["cassette_path"] = str(cassette_path)
    else:
        client = GeminiClient(settings)

    with TraceLogger(path, meta=meta) as log:
        with CheckpointStore(checkpoint_path_for(path)) as store:
            return GeminiPipeline(
                log,
                client,
                tools,
                task=task,
                checkpoints=store,
                attributor=attributor,
                handoff_hook=handoff_hook,
                research_rounds=research_rounds,
                reviewer=reviewer,
                checkpoint_interval=checkpoint_interval,
            ).run()


def _settings_or_offline(settings: Settings | None) -> Settings:
    """Replay needs the model fingerprint for the call key, not a live key.

    Falling back to a placeholder key means a teammate can replay a cassette
    without having an API key at all, which is the point of committing one.
    """
    if settings is not None:
        return settings
    try:
        return load_settings()
    except Exception:
        return Settings(api_key="replay-only")


if __name__ == "__main__":
    args = sys.argv[1:]

    def flag(name: str) -> str | None:
        if name in args:
            i = args.index(name)
            if i + 1 >= len(args):
                raise SystemExit(f"{name} needs a path")
            return args[i + 1]
        return None

    record = flag("--record")
    replay = flag("--replay")
    if record and replay:
        raise SystemExit("--record and --replay are mutually exclusive")
    positional = [a for a in args if not a.startswith("--") and a not in (record, replay)]
    target = positional[0] if positional else "data/runs/gemini.jsonl"

    try:
        outcome = run_pipeline(
            target,
            cassette_path=record or replay,
            cassette_mode="record" if record else "replay",
        )
    except (LLMError, RuntimeError, OSError) as exc:
        # A run that dies mid-way still leaves a valid partial trace: the
        # logger flushes every record as it is written (D-008). Say where it is
        # and how far it got, instead of a stack trace.
        print(f"run failed: {exc}")
        print()
        partial = Path(target)
        if partial.exists():
            trace = read_trace(partial)
            spent = sum(u.total_tokens for u in trace.usage)
            print(f"partial trace kept at {partial}")
            print(f"  {len(trace.events)} events, {len(trace.usage)} calls, {spent} tokens spent")
            if trace.events:
                last = trace.events[-1]
                print(f"  stopped after {last.id} ({last.agent_id}/{last.kind})")
        print()
        if isinstance(exc, QuotaExhausted):
            print("The per-day quota is spent. Nothing to tune: the window has")
            print("to reset, or the project needs a paid tier. See D-017 --")
            print("one run of this pipeline costs 6 requests.")
            print("A recorded cassette replays for free:")
            print("  python -m src.tracing.pipeline out.jsonl --replay data/cassettes/run1.jsonl")
        else:
            print("If this was a rate limit, check the ceiling with one call:")
            print("  python -m src.common.llm --smoke")
            print("and lower GEMINI_REQUESTS_PER_MINUTE in .env if it persists.")
        raise SystemExit(1)

    trace = read_trace(outcome.trace_path)
    trace.validate()

    print(f"trace        {outcome.trace_path}")
    print(f"events       {outcome.events}   sources {len(trace.sources)}")
    print(f"tokens       {outcome.tokens}  by purpose {trace.tokens_by_purpose()}")
    print(f"throttled    {outcome.throttled_s:.1f}s waiting on our own rate limiter")
    print(f"task success {outcome.task_success}")
    if replay:
        print("             (REPLAYED from a cassette -- tokens are the recorded")
        print("              ones, latency and attempts mean nothing here)")
    if not outcome.task_success:
        print(f"  expected {outcome.detail['expected']}")
        print(f"  produced {outcome.detail['produced']}")
        if outcome.stderr:
            print(f"  stderr   {outcome.stderr.strip()[:400]}")

    store = overhead(outcome.trace_path)
    print()
    print(f"storage      trace {store['trace_bytes']}B + checkpoints "
          f"{store['checkpoint_bytes']}B ({store['checkpoint_share']:.0%} of total) "
          f"across {store['checkpoints']} checkpoints")

    graphs_note = "exposure vs influence is empty until src/provenance/ runs;"
    print()
    print(graphs_note)
    print("exposures per output event are already recorded:")
    for e in trace.events:
        if e.kind in ("agent_output", "decision") and e.exposures:
            print(f"  {e.id} {e.agent_id:<10} exposed to {len(e.exposures)} sources")
