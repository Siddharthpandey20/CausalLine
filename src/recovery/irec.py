"""IREC: Interventional Replay with Early Cutoff.

An independent recovery strategy in which **the replay is the counterfactual**.
There is no separate attribution stage: no self-report, no leave-one-out pass,
no comparator and no calibrated noise floor.

    for each pipeline model event e, in the original order:
        p' = the prompt the rerun composes for e, with flagged sources redacted
        if p' == the prompt the original run ISSUED  -> splice, no model call
        else                                         -> re-execute on p'
             if the reply is BYTE-IDENTICAL to y_e   -> keep y_e, do not
                                                        propagate (early cutoff)
             else                                    -> keep the new reply and
                                                        let the change propagate

OFF BY DEFAULT. `src/recovery/causalline.py` remains the production strategy and
the comparison baseline; nothing here is reached unless a caller asks for it.
See `docs/research/IREC_FINAL_DECISION.md` for the conditions attached to that
default, and `docs/research/IREC_SAFETY_AND_NOVELTY.md` for the safety argument
and its assumptions.

WHY THE EQUALITY IS EXACT, AND MUST STAY EXACT
-----------------------------------------------
A kept output is one the model actually produced on the clean-world prompt:
either the prompt was byte-identical (so the recorded output is a valid output
on it), or the clean prompt was issued just now and the reply came back
identical. That argument needs no comparator, so it cannot inherit a
comparator's false negatives.

Relaxing the rule has been measured, and it is unsafe. Substituting the
production signature comparator and carryover facet produced **1 and 4 unsafe
preservations** on scenario A/influencing. Do not replace `==` with cosine
similarity, semantic similarity, a fuzzy threshold or a noise floor. The only
sound generalisation is exact equality of a *wider tuple* -- reply plus tool
arguments plus memory writes plus the typed fields a consumer parses -- never a
similarity judgment on any one of them.

NONDETERMINISM COSTS MONEY, NEVER SAFETY
-----------------------------------------
A reply that differs because the provider sampled differently is treated as
CHANGED: the original is discarded and the change propagates. That is the
conservative direction. Measured reproduction rate on an identical prompt is
96.7% (87/90) across three providers, so the cutoff fires often enough to be
worth having, and the 3.3% residual buys unnecessary replay rather than an
unsafe preservation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.common.llm import LLMResponse
from src.recovery.replay import (
    SELF_REPORT_MARKER,
    ReplayReport,
    pipeline_model_events,
    redact_flagged,
)
from src.recovery.verify import VerifyResult, verify
from src.tracing.logger import Trace, read_trace
from src.tracing.pipeline import run_pipeline

__all__ = ["CutoffClient", "IRECResult", "recover_irec"]


@dataclass
class CutoffClient:
    """Wraps the replay client and applies the two IREC rules.

    Reaches the model only through `inner`, so it works with the scripted
    client, a single provider or the 56-agent `RoutedClient` without change.
    """

    original: Trace
    inner: Any
    flagged: set[str]

    spliced: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    issued_prompts: dict[str, str] = field(default_factory=dict)

    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    replay_tokens: int = 0
    total_tokens: int = 0
    throttled_s: float = 0.0
    divergence: str | None = None

    def __post_init__(self) -> None:
        self._queue = pipeline_model_events(self.original)
        self._i = 0
        self._pending: tuple[str, str] | None = None
        self._last_issued: str | None = None

    # --- the two hooks the pipeline offers a replay client ------------------

    def announce(self, agent_id: str, kind: str) -> None:
        self._pending = (agent_id, kind)

    def last_issued_prompt(self) -> str | None:
        return self._last_issued

    # --- the mechanism ------------------------------------------------------

    def _spliced_response(self, text: str) -> LLMResponse:
        return LLMResponse(text=text, model="spliced", prompt_tokens=0,
                           output_tokens=0, thoughts_tokens=0, total_tokens=0,
                           attempts=1, latency_s=0.0, slept_s=0.0,
                           finish_reason="SPLICE")

    def generate(self, prompt: str, system: str | None = None,
                 json_output: bool = False,
                 temperature: float | None = None) -> LLMResponse:
        self._last_issued = None
        if SELF_REPORT_MARKER in prompt:
            # Structural, not defensive: IREC's whole claim is that the replay
            # subsumes attribution. A self-report reaching here means an
            # attributor was installed and the cost measurement is void.
            raise RuntimeError("IREC never asks a self-report")

        if self._i >= len(self._queue):
            # The rerun made more model calls than the original. Record it and
            # pass the call through: a control-flow divergence is a finding and
            # verification will see the shape mismatch.
            self.divergence = (f"rerun made call {self._i + 1} where the "
                               f"original made {len(self._queue)}")
            self._i += 1
            self._last_issued = prompt
            response = self.inner.generate(prompt, system=system,
                                           json_output=json_output,
                                           temperature=temperature)
            self._bill(response)
            return response

        event_id = self._queue[self._i]
        self._i += 1
        announced, self._pending = self._pending, None
        event = self.original.event(event_id)
        if announced is not None and announced != (event.agent_id, event.kind):
            self.divergence = (f"rerun diverged at {event_id}: expected "
                               f"{(event.agent_id, event.kind)}, got {announced}")

        original_prompt = self.original.prompt_text(event_id) or ""
        original_output = self.original.output_text(event_id) or ""
        cleaned = redact_flagged(prompt, self.flagged)

        # RULE 1 -- identical input, so the recorded output is a valid output
        # on it (assumption A1). No model call.
        if cleaned == original_prompt:
            self.spliced.append(event_id)
            return self._spliced_response(original_output)

        # RULE 2 -- the input changed, so the counterfactual has to be run.
        self._last_issued = cleaned
        self.issued_prompts[event_id] = cleaned
        response = self.inner.generate(cleaned, system=system,
                                       json_output=json_output,
                                       temperature=temperature)
        self._bill(response)

        if response.text == original_output:
            # EARLY CUTOFF. The agent was exposed and demonstrably ignored it:
            # same output on the clean prompt. Children see identical input, so
            # the intervention stops here.
            self.unchanged.append(event_id)
            return self._spliced_response(original_output)

        self.changed.append(event_id)
        return response

    def _bill(self, response: Any) -> None:
        self.calls += 1
        self.prompt_tokens += getattr(response, "prompt_tokens", 0) or 0
        self.output_tokens += getattr(response, "output_tokens", 0) or 0
        self.replay_tokens += getattr(response, "total_tokens", 0) or 0
        self.total_tokens = self.replay_tokens
        self.throttled_s += getattr(response, "slept_s", 0.0) or 0.0

    # --- what the recovery result is scored on ------------------------------

    @property
    def reexecuted(self) -> list[str]:
        return list(self.unchanged) + list(self.changed)

    def report(self, task_success: bool) -> ReplayReport:
        """A `ReplayReport` over IREC's own bookkeeping.

        `replayed` is every re-executed event, INCLUDING the ones whose output
        came back identical: their prompt was the redacted one, so the clean
        clearance applies to them and the output they kept is the one the model
        returned on that prompt. `spliced` is the prompt-identical set, which
        carries the original evidence forward unchanged.
        """
        return ReplayReport(
            invalidation=frozenset(self.reexecuted),
            spliced=list(self.spliced),
            replayed=self.reexecuted,
            issued_prompts=dict(self.issued_prompts),
            replay_tokens=self.replay_tokens,
            inner_calls=self.calls,
            task_success=task_success,
        )


@dataclass
class IRECResult:
    """What one IREC recovery produced, and what it cost.

    `replay_tokens` is the sum over every attempt, including a fallback. A
    fallback is never free (`docs/research/IREC_COST_ACCOUNTING.md` section 5).
    """

    recovered_path: Path | None
    report: ReplayReport | None
    verification: VerifyResult
    invalidated: frozenset[str]
    spliced: int
    unchanged: int
    changed: int
    calls: int
    analysis_tokens: int          # always 0: IREC has no attribution stage
    replay_tokens: int
    recovery_tokens: int
    task_success: bool
    wall_clock_s: float
    blast_radius_events: int
    blast_radius_agents: int
    divergence: str | None
    notes: list[str] = field(default_factory=list)
    fallback: str | None = None
    fallback_tokens: int = 0

    @property
    def scope(self) -> str:
        return self.fallback or "irec"


def recover_irec(
    original: Trace,
    flagged: Iterable[str],
    client: Any,
    out: Path,
    *,
    task: str | None = None,
    tools: Any = None,
    handoff_hook: Any = None,
    workflow_kwargs: dict[str, Any] | None = None,
    verify_result: bool = True,
) -> IRECResult:
    """One IREC recovery, through the production pipeline.

    Mirrors `causalline.recover`'s shape so a caller can swap strategies, but
    shares none of its machinery: no plan, no estimator, no attribution.

    Verification is the production `verify()`, driven by IREC's own
    splice/replay bookkeeping through `_post_recovery_region` -- a plain
    `contaminate()` over a recovered trace would re-taint every spliced event
    that merely saw the flagged source (the D-024 defect, reintroduced by
    recovery).
    """
    from src.recovery.causalline import _post_recovery_region
    from src.recovery.verify import original_task_success

    started = time.time()
    flagged_set = {s for s in flagged}
    notes: list[str] = []

    cutoff = CutoffClient(original=original, inner=client, flagged=flagged_set)
    outcome = run_pipeline(
        out,
        task=task if task is not None else original.meta.get("task"),
        client=cutoff,
        tools=tools,
        attributor=None,
        handoff_hook=handoff_hook,
        **(workflow_kwargs or {}),
    )
    task_success = bool(getattr(outcome, "task_success", False))
    report = cutoff.report(task_success)

    if cutoff.divergence:
        notes.append(f"control-flow divergence: {cutoff.divergence}")
    notes.append(
        f"IREC: spliced {len(cutoff.spliced)}, re-executed {cutoff.calls} "
        f"(cutoff kept {len(cutoff.unchanged)}, replaced {len(cutoff.changed)})"
    )

    verification = VerifyResult(ok=True, reasons=["verification not requested"])
    if verify_result:
        recovered = read_trace(out)
        region, recheck = _post_recovery_region(
            original, recovered, report, sorted(flagged_set))
        if recheck:
            notes.append(
                f"independent re-check withheld {len(recheck)} clearance(s): "
                f"{recheck[0]}"
            )
        verification = verify(
            recovered,
            sorted(flagged_set),
            task_success=task_success,
            invalidated=report.invalidation,
            current_memory=dict(tools.memory) if tools is not None else None,
            original=original,
            region=region,
            recheck_failures=recheck,
            original_task_success=original_task_success(original),
        )
        notes.append(
            f"verify {'PASS' if verification.ok else 'FAIL'}"
            + ("" if verification.ok else f": {verification.reasons}")
        )

    agents = {original.event(e).agent_id for e in report.invalidation
              if original.has_event(e)}
    return IRECResult(
        recovered_path=Path(out),
        report=report,
        verification=verification,
        invalidated=frozenset(report.invalidation),
        spliced=len(cutoff.spliced),
        unchanged=len(cutoff.unchanged),
        changed=len(cutoff.changed),
        calls=cutoff.calls,
        analysis_tokens=0,
        replay_tokens=cutoff.replay_tokens,
        recovery_tokens=cutoff.replay_tokens,
        task_success=task_success,
        wall_clock_s=time.time() - started,
        blast_radius_events=len(report.invalidation),
        blast_radius_agents=len(agents - {"user"}),
        divergence=cutoff.divergence,
        notes=notes,
    )
