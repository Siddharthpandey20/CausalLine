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


def redact_flagged(prompt: str, flagged: Iterable[str]) -> str:
    """Remove detector-flagged sources from a prompt about to be re-issued.

    This is "spliced_clean_upstream_context_only": the recomputed event must
    not see the malicious source. Sources that are not in the prompt are
    skipped -- they were never rendered, so there is nothing to redact.
    """
    remaining = list(flagged)
    for sid in remaining:
        block = _block_from_prompt(prompt)
        if not block or sid not in sources_in(block):
            continue
        try:
            prompt = redact_in_prompt(prompt, block, sid)
        except SourceNotInPrompt:
            continue
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
    replay_tokens: int = 0
    inner_calls: int = 0
    splice_assertions: int = 0
    recovered_path: Path | None = None
    task_success: bool = False
    stdout: str = ""
    stderr: str = ""
    wall_clock_s: float = 0.0

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
        self.total_tokens = 0
        self.throttled_s = getattr(self.inner, "throttled_s", 0.0)

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        json_output: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
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
    result = run_pipeline(
        path,
        task=task or original.meta.get("task") or DEFAULT_TASK,
        tools=tools,
        client=wrapper,
        attributor=attributor,
        handoff_hook=handoff_hook,
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
