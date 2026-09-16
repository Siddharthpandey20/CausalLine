"""
CausalLine Step 3: piecewise-deterministic selective replay.

    for each event e:
        if e not in the invalidation set:
            splice(e, logged_output(e))     # exact reuse -- never call the LLM
        else:
            new_output = invoke(e, context = spliced_clean_upstream_only)
            log(new_output)                 # a new trace; history is not overwritten

LLM output is non-deterministic even at temperature 0 (D-026: 8/8 textually
unique). Recovery therefore never depends on the model reproducing a prior
output. It only re-invokes events in the invalidation set, and reuses the
logged bytes everywhere else.

Two assertions, and they are not optional (Phase 5 exit test):

  * every spliced event's output matches its original log entry byte for byte
    (content-addressed refs make this a 16-character comparison, D-028)
  * every call that reaches the inner client is for an event in the
    invalidation set -- the client refuses to generate otherwise
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from src.common.content import ref_for
from src.common.llm import LLMResponse
from src.common.prompts import HEADER, SourceNotInPrompt, redact_in_prompt, sources_in
from src.tracing.logger import Trace
from src.tracing.pipeline import DEFAULT_TASK, PipelineResult, run_pipeline
from src.tracing.tools import Tools

SELF_REPORT_MARKER = "Which of them actually changed what you wrote?"


class SpliceError(RuntimeError):
    """A spliced event's output was not the logged one, or a generate()
    reached the model for an event that was supposed to be reused.

    Either is a correctness bug in the replay engine, not a finding about
    the attack. Fail the run rather than emit a recovered trace that looks
    fine and is not.
    """


def _block_from_prompt(prompt: str) -> str | None:
    """The source block, which D-036 puts last in every pipeline prompt.

    Taking everything from the first header to the end is safe only because
    of that decision. Inferring the end any other way is the D-029 hazard.
    """
    match = HEADER.search(prompt)
    if not match:
        return None
    return prompt[match.start():]


class RedactionError(RuntimeError):
    """A flagged source was in the prompt and is still in it afterwards.

    THE FAILURE MODE THIS EXISTS TO MAKE LOUD
    -----------------------------------------
    A redaction that does not happen is indistinguishable, downstream, from one
    that did: the event is replayed, the output comes back, verification is told
    the source was removed, and the recovery reports success while the payload
    was in front of the model the whole time. That is the e0014 class, and the
    only reason it was ever silent is that this function swallowed
    `SourceNotInPrompt` and moved on.

    Failing here aborts the replay, which `real_llm.run_generated()` already
    catches per method and records as a note. A recovery that produces no row is
    a worse result and an honest one; a recovery that produces a wrong row is
    neither.
    """


def redact_flagged(prompt: str, flagged: Iterable[str], strict: bool = True) -> str:
    """Remove detector-flagged sources from a prompt about to be re-issued.

    This is "spliced_clean_upstream_context_only": the recomputed event must
    not see the malicious source. Sources that are not in the prompt are
    skipped -- they were never rendered, so there is nothing to redact, and that
    is not a failure.

    What *is* a failure is a source that is rendered into the prompt and still
    rendered into it when this returns. `strict` exists so the old permissive
    behaviour stays reachable for a caller that deliberately wants it; nothing
    in this repository asks for it.
    """
    for sid in list(flagged):
        block = _block_from_prompt(prompt)
        if not block or sid not in sources_in(block):
            continue
        try:
            prompt = redact_in_prompt(prompt, block, sid)
        except SourceNotInPrompt as exc:
            if strict:
                raise RedactionError(
                    f"{sid} is rendered into this prompt and could not be "
                    f"removed from it: {exc}. Refusing to re-issue a prompt "
                    "that still contains a source the recovery reports as "
                    "redacted."
                ) from exc
            continue

    if strict:
        block = _block_from_prompt(prompt)
        still_there = sorted(set(flagged) & set(sources_in(block or "")))
        if still_there:
            raise RedactionError(
                f"flagged source(s) {still_there} are still rendered in the "
                "prompt after redaction; a partial redaction must not look "
                "like a complete one"
            )
    return prompt


def pipeline_model_events(trace: Trace) -> list[str]:
    """Events that made a pipeline LLM call, in log order.

    Read off usage records, not kind: the Executor's agent_output is not a
    model call (see src/eval/contract.py). Self-report and counterfactual
    calls are analysis and are not in this list.
    """
    seen: list[str] = []
    for usage in trace.usage:
        if usage.purpose != "pipeline" or not usage.event_id:
            continue
        if usage.event_id not in seen:
            seen.append(usage.event_id)
    return seen


@dataclass
class ReplayReport:
    """What the splicing client did. The Phase 5 assertions live here.

    THE ONE DEFINITION OF "DISCARDED"
    ---------------------------------
    `invalidation` is the set handed to `replay()`, and it is the **only**
    field a metric may use to answer "how much work did this method throw
    away". Every method -- B0, B1, B2 and CausalLine -- reaches the replay
    engine through the same call with the same argument, so scoring on this
    field is the one place all four are measured the same way.

    `replayed` is NOT that number. It lists the events that reached the inner
    client, which is only the events that made a *model call* -- 6 of 19 in
    this pipeline. Tool calls, tool responses, memory operations and the
    Executor's comparison are recomputed by pipeline code and never pass
    through `SplicingClient.generate()`, so they are absent from it however
    thoroughly they were redone.

    Scoring CausalLine on `replayed` while scoring the baselines on their full
    discard sets is not a rounding difference: it made an escalated
    `restart_all` -- which redoes every event, exactly as B0 does -- score
    66.7% work preserved against B0's 0%. Two definitions of one word, printed
    in adjacent columns.
    """

    # The set handed to replay(). Score on this.
    invalidation: frozenset[str] = frozenset()
    spliced: list[str] = field(default_factory=list)
    replayed: list[str] = field(default_factory=list)
    # event id -> the prompt actually sent to the inner client.
    #
    # WHY THIS IS NOT READ BACK OFF THE TRACE (D-068)
    # The pipeline writes a prompt into the content store and *then* calls the
    # client, and redaction happens inside the client -- so for a replayed event
    # the stored prompt is the one that would have been sent, not the one that
    # was. Nothing depended on the difference until verification started reading
    # prompts back to check that the flagged material was really gone, at which
    # point reading the stored copy reports a failure on every successful
    # recovery. The issued text is recorded here, where it is known.
    issued_prompts: dict[str, str] = field(default_factory=dict)
    replay_tokens: int = 0
    inner_calls: int = 0
    splice_assertions: int = 0
    recovered_path: Path | None = None
    task_success: bool = False
    stdout: str = ""
    stderr: str = ""
    wall_clock_s: float = 0.0

    # (agent_id, kind) of every pipeline call the rerun made, in order, as the
    # rerun itself declared it -- against which the original's sequence is
    # checked. See `SplicingClient.announce`.
    rerun_shape: list[tuple[str, str]] = field(default_factory=list)
    original_shape: list[tuple[str, str]] = field(default_factory=list)

    def assert_invariants(self, invalidation: set[str]) -> None:
        extra = set(self.replayed) - invalidation
        if extra:
            raise SpliceError(
                f"replayed events not in the invalidation set: {sorted(extra)}"
            )
        leaked = set(self.spliced) & invalidation
        if leaked:
            raise SpliceError(
                f"spliced events that were supposed to be replayed: {sorted(leaked)}"
            )
        if len(self.rerun_shape) != len(self.original_shape):
            raise SpliceError(
                f"the rerun made {len(self.rerun_shape)} pipeline model calls "
                f"where the original made {len(self.original_shape)}; the "
                "splice queue and the rerun are not the same workflow"
            )


@dataclass
class SplicingClient:
    """Drop-in for GeminiClient that reuses logged outputs where allowed.

    Matching is by call order of pipeline events, not by prompt text. A
    replayed upstream event changes the downstream prompt (new source
    contents), so matching on the original prompt would miss and fall
    through to a live call -- exactly the failure the splice assertion
    exists to prevent.

    Self-report questions are passed through: they are analysis, not
    pipeline events, and they are not in `pipeline_model_events`.

    ORDER MATCHING NEEDS AN IDENTITY CHECK BEHIND IT (docs/03 #13)
    ---------------------------------------------------------------
    Matching by position assumes the rerun makes the same calls in the same
    order, which `ScriptedClient` guarantees and a real model does not. The
    Planner is asked for three research questions and the pipeline takes
    `questions[:3] or [task]`; a rerun that returns two makes the Researcher
    loop twice, and from there every splice is matched to the wrong event. Two
    outcomes, and the second is the dangerous one:

      * the queue runs short, `SpliceError` fires -- loud, and fine
      * the queue stays long enough that ids still line up, and a stored output
        is spliced onto an event it did not come from -- **silent**, and
        nothing downstream can detect it

    So the assumption is now checked instead of relied on. The pipeline
    announces `(agent_id, kind)` before each model call; if it disagrees with
    the event the queue is about to hand back, the replay aborts. That turns the
    silent failure into the loud one, which is the whole of #13's answer.

    A pipeline that does not announce is not penalised: `announce` is optional
    and an un-announced call is spliced on position as before. That keeps the
    check from becoming a hard dependency of the replay engine on one pipeline.
    """

    original: Trace
    invalidation: set[str]
    inner: Any
    flagged: set[str] = field(default_factory=set)
    report: ReplayReport = field(default_factory=ReplayReport)
    # Optional hook: mutate a prompt about to be re-issued (tests, redaction).
    clean_prompt: Callable[[str, str], str] | None = None

    def __post_init__(self) -> None:
        self._queue = pipeline_model_events(self.original)
        self._index = 0
        self._pending: tuple[str, str] | None = None
        # The text the most recent real call actually went out with, for the
        # pipeline to store instead of the one it composed (docs/03 #18).
        # None after a splice or a self-report, because neither issued a
        # pipeline prompt and inheriting the previous one would be a new lie.
        self._last_issued: str | None = None
        self.total_tokens = 0
        self.throttled_s = getattr(self.inner, "throttled_s", 0.0)
        self.report.original_shape = [
            (self.original.event(eid).agent_id, self.original.event(eid).kind)
            for eid in self._queue
        ]

    def last_issued_prompt(self) -> str | None:
        """The prompt the last `generate()` really sent, or None.

        docs/03 #18: the pipeline composes a prompt, calls the client, and then
        writes the composed text into the content store -- but redaction happens
        *inside* this client, so for a replayed event the stored prompt was the
        un-redacted one and the trace recorded a request that was never made.
        The pipeline now asks for this after the call and stores the answer.

        None means no pipeline prompt was issued: a spliced event made no call
        at all, and a self-report is analysis rather than a pipeline event. The
        caller keeps what it composed in that case, which is what it has always
        done.
        """
        return self._last_issued

    def announce(self, agent_id: str, kind: str) -> None:
        """The pipeline says what the next model call is for.

        Optional by design -- see the class docstring. Consumed by the next
        `generate()` and cleared, so an analysis call made in between (a
        self-report) cannot inherit it.
        """
        self._pending = (agent_id, kind)

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        json_output: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        self._last_issued = None
        if SELF_REPORT_MARKER in prompt:
            return self.inner.generate(
                prompt, system=system, json_output=json_output, temperature=temperature
            )
        if self._index >= len(self._queue):
            raise SpliceError(
                "pipeline issued more model calls than the original trace; "
                "refusing to generate without an event to justify it"
            )
        event_id = self._queue[self._index]
        self._index += 1

        announced, self._pending = self._pending, None
        if announced is not None:
            event = self.original.event(event_id)
            expected = (event.agent_id, event.kind)
            self.report.rerun_shape.append(announced)
            if announced != expected:
                raise SpliceError(
                    f"call {self._index} of the rerun is {announced} but the "
                    f"original's call {self._index} was {expected} ({event_id}). "
                    "The rerun's call sequence has diverged from the trace being "
                    "spliced into it, so every splice from here on would be "
                    "matched to the wrong event."
                )

        if event_id not in self.invalidation:
            text = self.original.output_text(event_id)
            if text is None:
                raise SpliceError(
                    f"{event_id} has no stored output, so it cannot be spliced"
                )
            logged_ref = self.original.event(event_id).output_ref
            if ref_for(text) != logged_ref:
                raise SpliceError(
                    f"{event_id}: spliced bytes do not match the logged "
                    f"output_ref {logged_ref}"
                )
            self.report.spliced.append(event_id)
            self.report.splice_assertions += 1
            return LLMResponse(
                text=text,
                model="spliced",
                prompt_tokens=0,
                output_tokens=0,
                thoughts_tokens=0,
                total_tokens=0,
                attempts=1,
                latency_s=0.0,
                slept_s=0.0,
                finish_reason="SPLICE",
            )

        # Re-invoke. The assertion is the `if` above: we only reach here
        # for an event in the invalidation set.
        if event_id not in self.invalidation:
            raise SpliceError(
                f"{event_id} is not in the invalidation set; the inner "
                "client must not be called"
            )
        cleaned = redact_flagged(prompt, self.flagged)
        if self.clean_prompt is not None:
            cleaned = self.clean_prompt(event_id, cleaned)
        self.report.issued_prompts[event_id] = cleaned
        self._last_issued = cleaned
        response = self.inner.generate(
            cleaned, system=system, json_output=json_output, temperature=temperature
        )
        self.report.replayed.append(event_id)
        self.report.inner_calls += 1
        self.report.replay_tokens += getattr(response, "total_tokens", 0)
        self.total_tokens += getattr(response, "total_tokens", 0)
        return response


def replay(
    original: Trace,
    invalidation: Iterable[str],
    client: Any,
    path: str | Path,
    tools: Tools | None = None,
    flagged: Iterable[str] = (),
    attributor: Any = None,
    handoff_hook: Any = None,
    task: str | None = None,
) -> tuple[PipelineResult, ReplayReport]:
    """Re-run the pipeline, splicing every event not in `invalidation`.

    Writes a *new* trace. The original is not overwritten (D-008: history
    is append-only; a recovered run is a new run that points at the old
    one in its header).
    """
    import time

    invalidation_set = set(invalidation)
    wrapper = SplicingClient(
        original=original,
        invalidation=invalidation_set,
        inner=client,
        flagged=set(flagged),
    )
    started = time.time()
    # The rerun must have the same shape as the trace being spliced into it;
    # the trace carries that shape in its header. Defaults reproduce the short
    # pipeline, which is what every trace written before Phase C implies.
    result = run_pipeline(
        path,
        task=task or original.meta.get("task") or DEFAULT_TASK,
        tools=tools,
        client=wrapper,
        attributor=attributor,
        handoff_hook=handoff_hook,
        research_rounds=int(original.meta.get("research_rounds") or 1),
        reviewer=bool(original.meta.get("reviewer") or False),
        # The agent graph itself, read off the trace for exactly the reason the
        # length is: replaying a fan-out trace into a chain rerun would line up
        # event ids far enough to splice, and then splice the wrong outputs
        # into the wrong agents. "chain" is the pre-existing default, so every
        # trace written before the header carried this key replays unchanged.
        workflow=str(original.meta.get("workflow") or "chain"),
        workers=int(original.meta.get("workers") or 6),
    )
    # Recorded at the one point every method passes through, so no caller has
    # to reconstruct "what was discarded" from something narrower.
    wrapper.report.invalidation = frozenset(invalidation_set)
    wrapper.report.recovered_path = result.trace_path
    wrapper.report.task_success = result.task_success
    wrapper.report.stdout = result.stdout
    wrapper.report.stderr = result.stderr
    wrapper.report.wall_clock_s = time.time() - started
    wrapper.report.assert_invariants(invalidation_set)
    return result, wrapper.report
