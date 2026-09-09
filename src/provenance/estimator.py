"""
The influence estimator: self-report, then a targeted counterfactual.

This is the one unsolved research problem in the design (docs/03 issue #1: we
cannot see inside an LLM call), and the two-stage answer is docs/02's:

    1. **self-report** -- ask the agent which inputs it used. One cheap call.
       A claim, not evidence.
    2. **targeted counterfactual** -- remove the suspect source, re-run that one
       event, and compare *decision signatures* (never text: D-026, and see
       src/provenance/signatures.py). Slow, and it is evidence.

WHICH CLAIMS GET A COUNTERFACTUAL SPENT ON THEM
-----------------------------------------------
Not all of them, because the check has to cost less than the rerun it avoids
(docs/03 issue #7) -- and not a fixed fraction either, because the two kinds of
claim are not equally dangerous:

    "I used S3"          if wrong, we invalidate work that was fine. Wasteful,
                         safe. Accepted at face value.
    "I did not use S3"   if wrong, we preserve work that was contaminated. An
                         unsafe preservation, the error CLAUDE.md names as the
                         dangerous one. Must be verified before it is acted on.

So every negative claim is a candidate for a counterfactual, and a positive
claim is not -- except for a random `audit_rate` sample of positives, which
exists to estimate how often self-report over-claims. Without that sample the
false-positive rate of the cheap stage is unmeasured, and the "self-report
only" ablation has nothing to compare against.

An unverified negative is left **unrecorded**, which leaves the pair
`unchecked`, which the contamination walk treats as contaminated. That is the
conservative fallback doing its job: running out of budget costs work
preserved, never safety.

WHAT THIS MODULE MUST NOT READ
------------------------------
`Source.malicious`, and the detector's verdict. `AttributionRequest` does not
carry either. During the original run there is no verdict yet; in the targeted
pass (`refine_for_verdict`) the flagged ids arrive as an argument and are used
only to decide *where to look*, never to decide what the answer is. An estimator
that could see the label would produce edges shaped by the answer, and every
accuracy number after that would be worthless.
"""

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from src.common.models import InfluenceEdge
from src.common.prompts import SourceNotInPrompt, redact_in_prompt
from src.provenance import selfreport
from src.provenance.attribution import AttributionRequest, Sink
from src.provenance.signatures import (
    Calibration,
    Comparator,
    Signature,
    compare,
    for_event,
)

MODES = ("hybrid", "self_report", "counterfactual")


@dataclass
class CheckBudget:
    """A cap on counterfactual calls, because they are the expensive half.

    One flagged source on one event is one extra call, times the repeat count,
    and the Researcher's events carry eight exposures each. Unbounded, the
    analysis cost exceeds the rerun cost it is meant to avoid -- which is
    exactly the collapse docs/03 issue #7 describes.

    Running out is safe and is recorded: unverified pairs stay `unchecked` and
    the conservative fallback contaminates them.
    """

    per_event: int | None = None
    total: int | None = None
    spent: int = 0
    spent_this_event: int = 0
    denied: int = 0

    def start_event(self) -> None:
        self.spent_this_event = 0

    def allows(self) -> bool:
        if self.total is not None and self.spent >= self.total:
            return False
        if self.per_event is not None and self.spent_this_event >= self.per_event:
            return False
        return True

    def charge(self, calls: int = 1) -> None:
        self.spent += calls
        self.spent_this_event += calls

    def deny(self) -> None:
        self.denied += 1


@dataclass
class CounterfactualResult:
    """One counterfactual check, with the evidence it rests on."""

    source_id: str
    event_id: str
    influenced: bool
    before: Signature
    after: Signature
    moved: list[str] = field(default_factory=list)
    repeats: int = 0
    calls: int = 0
    tokens: int = 0
    excluded: list[str] = field(default_factory=list)
    control_stable: bool | None = None
    error: str | None = None

    @property
    def verdict(self) -> str:
        return "tainted" if self.influenced else "clean"

    def notes(self) -> str:
        parts = [f"counterfactual, {self.repeats} repeat(s)"]
        if self.moved:
            parts.append(f"facets moved: {','.join(self.moved)}")
        else:
            parts.append("signature unchanged")
        if self.excluded:
            parts.append(f"facets excluded as noisy: {','.join(self.excluded)}")
        if self.control_stable is False:
            parts.append("CONTROL UNSTABLE: unchanged re-run also moved")
        if self.error:
            parts.append(f"error: {self.error}")
        return "; ".join(parts)


def counterfactual(
    client: Any,
    request: AttributionRequest,
    source_id: str,
    comparator: Comparator | None = None,
    calibration: Calibration | None = None,
    repeats: int = 1,
    context: dict[str, Any] | None = None,
    control_run: bool = False,
) -> CounterfactualResult:
    """Re-run one event with one source removed, and compare decisions.

    `repeats` re-runs the redacted request more than once. The aggregation is
    **any-differs**: if any repeat's signature moves, the source is called
    influential. That is the safe direction -- a missed flip is an unsafe
    preservation, an extra flip is wasted recomputation -- and it is only
    affordable because the calibrated signature has a measured 0% floor. On an
    uncalibrated comparator the same rule would tend towards "everything is
    influential", which is the conservative fallback with extra steps.

    `control_run` spends one more call re-running the request **unchanged**.
    That turns each verdict into its own noise measurement rather than relying
    on the model-wide calibration: if the control moves, this event's signature
    is unstable and the verdict is not evidence, whatever the comparator's
    average floor says.
    """
    if not request.prompt or not request.source_block:
        return CounterfactualResult(
            source_id, request.event_id, influenced=True,
            before=Signature("none"), after=Signature("none"),
            error="no stored prompt or source block, so the request cannot be "
                  "reissued; defaulting to influenced",
        )

    comparator = comparator or for_event(request.kind, request.output)
    calibration = calibration or Calibration()
    excluded = calibration.exclude_for(comparator.name)

    try:
        redacted = redact_in_prompt(request.prompt, request.source_block, source_id)
    except (SourceNotInPrompt, KeyError) as exc:
        # A redaction that changed nothing would give an unchanged answer and
        # the verdict "no influence" -- a false clean produced by plumbing.
        return CounterfactualResult(
            source_id, request.event_id, influenced=True,
            before=Signature(comparator.name), after=Signature(comparator.name),
            error=f"could not redact {source_id}: {exc}",
        )

    before = comparator.signature(request.output, context)
    result = CounterfactualResult(
        source_id, request.event_id, influenced=False,
        before=before, after=before, repeats=0,
        excluded=sorted(excluded),
    )

    for _ in range(max(1, repeats)):
        response = client.generate(redacted, system=request.system)
        result.calls += 1
        result.tokens += getattr(response, "total_tokens", 0)
        after = comparator.signature(response.text, context)
        result.after = after
        result.repeats += 1
        same, moved = compare(before, after, exclude=excluded)
        if not same:
            result.influenced = True
            result.moved = moved
            break

    if control_run:
        response = client.generate(request.prompt, system=request.system)
        result.calls += 1
        result.tokens += getattr(response, "total_tokens", 0)
        control = comparator.signature(response.text, context)
        same, _moved = compare(before, control, exclude=excluded)
        result.control_stable = same
        if not same:
            # The instrument moved with nothing removed, so this event's flip
            # carries no information. Fall back to contaminated.
            result.influenced = True
            result.error = (
                "control run (nothing removed) also changed the signature, so a "
                "flip on this event is not evidence"
            )
    return result


# --- the attributor the pipeline uses ----------------------------------------


@dataclass
class HybridAttributor:
    """Self-report, then a counterfactual on the claims that could hurt us.

    `mode` selects the three conditions docs/04 asks for as ablations, and they
    are one object with a switch rather than three code paths that have to be
    kept in step:

        hybrid          self-report, counterfactual on negatives + audit sample
        self_report     self-report only. Cheap. Cannot preserve any work unless
                        `trust_self_report_negatives` is on, in which case it
                        can preserve work it has no evidence for -- which is the
                        point of running the ablation.
        counterfactual  no self-report at all; counterfactual every exposure.
                        The most evidence per verdict and the highest cost, and
                        the condition that shows what the cheap stage buys.
    """

    client: Any
    mode: str = "hybrid"
    name: str = "hybrid"
    model: str = "unknown"
    calibration: Calibration = field(default_factory=Calibration)
    budget: CheckBudget = field(default_factory=CheckBudget)
    repeats: int = 1
    audit_rate: float = 0.0
    control_run: bool = False
    trust_self_report_negatives: bool = False
    confident_at: float = 0.7
    # Runs generated code, for the behavioural half of the code comparator.
    # None disables it and the comparator falls back to its AST facets.
    run_code: Callable[[str], dict[str, Any]] | None = None
    seed: int = 20260906
    # Filled in as it goes, for the cost table and the ablation report.
    stats: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        self._rng = random.Random(self.seed)
        if self.name == "hybrid":
            self.name = self.mode

    def _bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def attribute(self, request: AttributionRequest, sink: Sink) -> None:
        if not request.exposures:
            return
        self.budget.start_event()
        comparator = for_event(request.kind, request.output)
        context = {"run": self.run_code} if self.run_code else None
        if not self.calibration.is_calibrated(comparator.name):
            self._bump(f"uncalibrated:{comparator.name}")

        report = None
        if self.mode != "counterfactual":
            report = selfreport.ask(
                self.client,
                request.event_id,
                request.kind,
                request.output,
                request.catalogue(),
            )
            sink.log_usage(
                "self_report",
                model=self.model,
                prompt_tokens=report.prompt_tokens,
                output_tokens=report.output_tokens,
                total_tokens=report.total_tokens,
                event_id_=request.event_id,
                agent_id=request.agent_id,
            )
            self._bump("self_report_calls")
            if report.parse_failed:
                self._bump("self_report_unparseable")

        # Order matters when the budget is tight: the least confident negative
        # claims are the ones most likely to be wrong, so they are checked first.
        # Nothing here can consult the detector or the ground-truth label, so
        # confidence is the only signal available for prioritising.
        order = sorted(
            request.exposures,
            key=lambda sid: report.claim(sid).confidence if report else 0.0,
        )

        for sid in order:
            claim = report.claim(sid) if report else None
            wants_evidence = claim is None or claim.needs_evidence
            audited = (
                claim is not None
                and claim.used
                and claim.reported
                and self.audit_rate > 0
                and self._rng.random() < self.audit_rate
            )

            if claim is not None and claim.used and not audited:
                self._record_self_report_positive(sink, request, sid, claim)
                continue

            if not (wants_evidence or audited):
                continue

            if self.mode == "self_report":
                # No counterfactual available in this condition.
                if self.trust_self_report_negatives and claim is not None and not claim.used:
                    sink.log_check(
                        sid, request.event_id, "clean", "self_report",
                        confidence=claim.confidence,
                        notes="agent reported not using this source; ABLATION: "
                              "accepted without evidence",
                    )
                    self._bump("self_report_negatives_trusted")
                else:
                    self._bump("left_unchecked")
                continue

            if not self.budget.allows():
                self.budget.deny()
                self._bump("budget_denied")
                continue

            result = counterfactual(
                self.client, request, sid,
                comparator=comparator,
                calibration=self.calibration,
                repeats=self.repeats,
                context=context,
                control_run=self.control_run,
            )
            self.budget.charge(result.calls)
            self._bump("counterfactual_calls", result.calls)
            self._bump(f"counterfactual_{result.verdict}")
            if audited:
                self._bump("audited_positives")
                if not result.influenced:
                    # Self-report claimed a source it did not use. Counts the
                    # cheap stage's over-claiming, which is otherwise invisible.
                    self._bump("audit_found_overclaim")
            sink.log_usage(
                "counterfactual",
                model=self.model,
                prompt_tokens=0,
                output_tokens=0,
                total_tokens=result.tokens,
                event_id_=request.event_id,
                agent_id=request.agent_id,
            )
            if result.influenced:
                sink.log_influence(
                    InfluenceEdge(
                        sid, request.event_id, method="counterfactual",
                        confident=result.error is None,
                    )
                )
            sink.log_check(
                sid, request.event_id, result.verdict, "counterfactual",
                confidence=0.5 if result.error else 1.0,
                signature_before=result.before.value,
                signature_after=result.after.value,
                comparator=comparator.name,
                repeats=result.repeats,
                notes=result.notes(),
            )

    def _record_self_report_positive(
        self, sink: Sink, request: AttributionRequest, sid: str, claim: Any
    ) -> None:
        sink.log_influence(
            InfluenceEdge(
                sid, request.event_id, method="self_report",
                confident=claim.confidence >= self.confident_at,
            )
        )
        sink.log_check(
            sid, request.event_id, "tainted", "self_report",
            confidence=claim.confidence,
            notes="agent reported using this source; accepted without evidence "
                  "because a wrong positive costs work, not safety",
        )
        self._bump("self_report_positives")

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.stats.items())]
        return f"{self.name}: " + (", ".join(parts) if parts else "nothing recorded")


# --- the targeted pass, run after a detector has spoken ----------------------


@dataclass
class RefineResult:
    examined: int = 0
    cleared: int = 0
    tainted: int = 0
    calls: int = 0
    tokens: int = 0
    denied: int = 0
    order: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"examined {self.examined} pairs ({self.cleared} cleared, "
            f"{self.tainted} influenced) in {self.calls} calls, "
            f"{self.tokens} tokens; {self.denied} denied by budget"
        )


def refine_for_verdict(
    trace_path: str | Path,
    malicious: Iterable[str],
    client: Any,
    calibration: Calibration | None = None,
    budget: CheckBudget | None = None,
    repeats: int = 1,
    run_code: Callable[[str], dict[str, Any]] | None = None,
    model: str = "unknown",
    control_run: bool = False,
) -> RefineResult:
    """Examine only the pairs the detector's verdict makes relevant.

    docs/02: "Run counterfactual only where it matters: on sources the detector
    flagged." This is that, and it is what makes the analysis cost bearable --
    checking every exposure on every event costs more than the rerun it avoids,
    which is docs/03 issue #7's collapse condition.

    The loop is a frontier expansion, and the shape is the point:

        examine an unchecked pair whose source is currently contaminated
          -> record the verdict
          -> recompute the contaminated region
          -> the frontier moves

    A pair whose source turns out clean never has its downstream pairs
    examined at all, so the cost scales with the contaminated region rather
    than with the trace. Running out of budget leaves the rest `unchecked`,
    which the walk contaminates -- expensive in preserved work, never unsafe.

    Appends `check` and `influence` records to the existing trace, which is what
    the append-only format is for (D-008). Nothing already written is rewritten.
    """
    from src.provenance.contamination import contaminate
    from src.tracing.logger import TraceLogger, read_trace

    calibration = calibration or Calibration()
    budget = budget or CheckBudget()
    result = RefineResult()
    flagged = set(malicious)

    trace = read_trace(trace_path)
    trace.validate()

    with TraceLogger(trace_path, append=True, meta={"record_kind": "refinement"}) as log:
        while True:
            region = contaminate(trace, flagged)
            candidates = [
                (sid, eid)
                for eid, sid in _unchecked_in_region(trace, region.sources)
            ]
            if not candidates:
                break
            if not budget.allows():
                result.denied += len(candidates)
                break

            sid, eid = candidates[0]
            request = request_for(trace, eid, run_code=run_code)
            if request is None:
                # Nothing to re-issue. Left unchecked, so the walk keeps it
                # contaminated; a placeholder `clean` here would be a fabricated
                # clearance.
                _mark_unexaminable(log, sid, eid)
                trace = read_trace(trace_path)
                continue

            outcome = counterfactual(
                client, request, sid,
                calibration=calibration,
                repeats=repeats,
                context={"run": run_code} if run_code else None,
                control_run=control_run,
            )
            budget.charge(outcome.calls)
            result.examined += 1
            result.calls += outcome.calls
            result.tokens += outcome.tokens
            result.order.append((sid, eid))
            comparator = for_event(request.kind, request.output).name

            if outcome.influenced:
                result.tainted += 1
                log.log_influence(
                    InfluenceEdge(sid, eid, method="counterfactual",
                                  confident=outcome.error is None)
                )
            else:
                result.cleared += 1
            log.log_usage(
                "counterfactual",
                model=model,
                prompt_tokens=0,
                output_tokens=0,
                total_tokens=outcome.tokens,
                event_id_=eid,
                agent_id=request.agent_id,
            )
            log.log_check(
                sid, eid, outcome.verdict, "counterfactual",
                confidence=0.5 if outcome.error else 1.0,
                signature_before=outcome.before.value,
                signature_after=outcome.after.value,
                comparator=comparator,
                repeats=outcome.repeats,
                notes=outcome.notes(),
            )
            trace = read_trace(trace_path)

    return result


def _unchecked_in_region(trace: Any, sources: Iterable[str]) -> list[tuple[str, str]]:
    """(event, source) pairs that are unchecked and whose source is contaminated.

    Ordered by position in the trace so the frontier is walked forwards: an
    upstream verdict can remove downstream pairs from the region entirely, and
    checking downstream first would spend calls on pairs that were about to
    become irrelevant.
    """
    contaminated = set(sources)
    out: list[tuple[str, str]] = []
    for event in trace.events:
        for sid in event.exposures:
            if sid in contaminated and trace.checked(event.id, sid) == "unchecked":
                out.append((event.id, sid))
    return out


def _mark_unexaminable(log: Any, source_id: str, event_id: str) -> None:
    """Record that a pair cannot be examined, as `assumed` rather than `clean`.

    Writing a record here is not bookkeeping: without one the frontier
    expansion would pick the same pair again on the next iteration and never
    terminate. `assumed` is the honest verdict -- we looked for a way to check
    it and there was none -- and it is never a clearance, so it keeps the pair
    contaminated.
    """
    log.log_check(
        source_id, event_id, "tainted", "assumed",
        confidence=0.0,
        notes="no stored prompt for this event, so no counterfactual is "
              "possible; conservative fallback applies",
    )


def request_for(
    trace: Any, event_id: str, run_code: Callable[[str], dict[str, Any]] | None = None
) -> AttributionRequest | None:
    """Rebuild the attribution request for a logged event, from the trace.

    Returns None when the event has no stored prompt -- a tool response, or an
    event logged before the content store existed. Callers must treat None as
    "cannot be examined", never as "nothing was influenced".
    """
    event = trace.event(event_id)
    prompt = trace.prompt_text(event_id)
    output = trace.output_text(event_id)
    if not prompt or output is None:
        return None
    labels: dict[str, str] = {}
    for sid in event.exposures:
        source = trace.source(sid)
        where = source.metadata.get("url") or source.metadata.get("key") or source.kind
        labels[sid] = f"{source.kind}, {where}"
    return AttributionRequest(
        event_id=event_id,
        agent_id=event.agent_id,
        kind=event.kind,
        output=output,
        exposures=list(event.exposures),
        labels=labels,
        prompt=prompt,
        source_block=trace.source_block_text(event_id),
        system=trace.system_text(event_id),
    )
