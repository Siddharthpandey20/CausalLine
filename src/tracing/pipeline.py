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
    record_carrier,
    record_structural,
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
    ) -> None:
        self.log = log
        self.client = client
        self.tools = tools
        self.task = task
        self.checkpoints = checkpoints
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
            ),
            self.log,
        )
        return event, response

    def _checkpoint(self, event_id: str, agent_id: str) -> None:
        """Checkpoint after an agent boundary (docs/02-architecture.md, v1
        policy). Cost is measured rather than assumed: see overhead()."""
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
        plan_event, plan_response = self._call(
            "planner",
            "plan",
            prompt=(
                f"Task:\n{self.task}\n\n"
                "Reply with JSON: {\"brief\": str, \"questions\": [str, str, str]}. "
                "Exactly three research questions, each answerable from "
                "documentation about parsing dates in Python."
            ),
            system=PLANNER_SYSTEM,
            parents=[user_event.id],
            json_output=True,
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

        to_coder = self.log.log_event(
            "researcher",
            "message",
            parents=[e.id for e, _ in findings],
            exposures=self.context.get("researcher", []),
            output_ref=self.log.put_content(
                json.dumps([text for _, text in findings], sort_keys=True),
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
                f"Inputs:\n{coder_block}\n\n"
                "Decide the approach in at most three sentences. State which "
                "library you will use and why."
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
                f"Inputs:\n{coder_block}\n\n"
                "Reply with the complete Python script and nothing else. No "
                "markdown fences, no commentary. The script must hardcode the "
                "samples and print one ISO date per line."
            ),
            system=CODER_SYSTEM,
            parents=[decision_event.id],
            source_block=coder_block,
        )
        code = _strip_fences(code_response.text)

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
        for event in (exec_call, exec_response, final):
            record_structural(
                self.log,
                event.id,
                list(event.exposures),
                used=[],
                why="executor runs the generated script; it consults no source directly",
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
        **settings.fingerprint(),
    }
    if client is not None:
        # Marked in the header for the same reason a cassette is (D-019): a
        # run whose responses did not come from the model must never be
        # mistaken for one that did.
        meta["client"] = type(client).__name__
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
