"""
The trace contract, checked.

Everything downstream of the trace -- influence estimation, the recovery
planner, selective replay, verification -- assumes four things about a trace
that nothing was previously checking:

    1. every event's `output_ref` and `inputs_ref` resolve to real content
    2. every (source, event) exposure pair carries a `check` record, so the
       conservative fallback is a decision rather than a default
    3. every LLM event has a stored prompt, because a counterfactual check
       cannot be run without one
    4. memory writes carry their key and value, so verification can tell
       whether a live memory entry still points at something invalidated

This is the exit test for the trace layer, and it is a program rather than a
paragraph because all four failed silently before. A missing prompt does not
raise; it makes the counterfactual stage skip the event, which shows up as a
smaller exposure/influence gap and looks like a finding about the model.

    python -m src.eval.contract data/runs/run1.jsonl
"""

from dataclasses import dataclass, field
from pathlib import Path

from src.common.models import EVENT_KINDS
from src.provenance.checks import CheckLedger
from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for
from src.tracing.logger import Trace, read_trace

def model_written(trace: Trace) -> set[str]:
    """Events an API call produced.

    Read off the usage records rather than from the event kind. Kind is the
    wrong signal and gets this wrong in the direction that matters: the
    Executor's final `agent_output` is computed by comparing the script's
    output against the database fixture, with no model in the loop, and
    demanding a prompt for it reports a contract failure that is not one.
    A usage record, by contrast, exists exactly when a call was made.
    """
    return {u.event_id for u in trace.usage if u.purpose == "pipeline" and u.event_id}


@dataclass
class Finding:
    rule: str
    detail: str
    fatal: bool = True


def _check_block_boundary(trace: Trace, event_id: str, prompt: str) -> list[Finding]:
    """Does reading the sources out of the prompt give the same answer as
    reading them out of the stored block?

    It should, and when it does not the difference is silent and dangerous. The
    source block has no terminator, so a source's content runs to the next
    header -- and for the *last* source, to the end of whatever it was embedded
    in. Put instructions after the block and the last source absorbs them.

    D-029 removed this hazard from the redaction path by doing surgery inside
    the stored block. It did not remove it from every *reader* of a prompt, and
    the scripted agent read prompts directly: its view of the Coder's
    `style/output` memory ended with "...print one result per line.\\n\\nDecide the
    approach in at most three sentences", so that source appeared to supply
    whatever vocabulary the trailing instructions contained. Harmless with the
    current wording and not harmless in general -- an instruction mentioning ISO
    or a format code would have made the last source look like the thing that
    supplied it, and the leave-one-out ground truth would have agreed.

    Every prompt now ends with its source block, which makes the boundary
    unambiguous. This check is here so that stops being a convention someone has
    to remember and becomes a property the trace is tested for.
    """
    from src.common.prompts import parse_sources

    block = trace.source_block_text(event_id)
    if not block:
        return []
    if parse_sources(prompt) == parse_sources(block):
        return []
    return [
        Finding(
            "block-boundary",
            f"{event_id}: parsing sources from the prompt disagrees with parsing "
            "them from the stored block, so text outside the block is being read "
            "as part of a source. Put the source block last in the prompt.",
        )
    ]


@dataclass
class ContractReport:
    trace_path: Path
    events: int = 0
    exposure_pairs: int = 0
    checked_pairs: int = 0
    resolved_outputs: int = 0
    events_with_output: int = 0
    model_events: int = 0
    model_events_with_prompt: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.fatal for f in self.findings)

    @property
    def check_coverage(self) -> float:
        return self.checked_pairs / self.exposure_pairs if self.exposure_pairs else 1.0

    def render(self) -> str:
        lines = [
            f"trace              {self.trace_path}",
            f"events             {self.events}",
            f"content refs       {self.resolved_outputs}/{self.events_with_output} "
            f"output refs resolve",
            f"prompts stored     {self.model_events_with_prompt}/{self.model_events} "
            f"model-written events",
            f"check coverage     {self.checked_pairs}/{self.exposure_pairs} "
            f"exposure pairs ({self.check_coverage:.0%})",
            "",
        ]
        if not self.findings:
            lines.append("PASS: the trace satisfies the contract recovery depends on.")
            return "\n".join(lines)
        fatal = [f for f in self.findings if f.fatal]
        warnings = [f for f in self.findings if not f.fatal]
        for finding in fatal:
            lines.append(f"FAIL  {finding.rule}: {finding.detail}")
        for finding in warnings:
            lines.append(f"warn  {finding.rule}: {finding.detail}")
        lines.append("")
        lines.append("PASS" if self.ok else f"FAIL ({len(fatal)} fatal)")
        return "\n".join(lines)


def check_contract(trace: Trace, path: str | Path | None = None) -> ContractReport:
    report = ContractReport(trace_path=Path(path or trace.path or "<memory>"))
    report.events = len(trace.events)
    from_model = model_written(trace)

    if trace.content is None:
        report.findings.append(
            Finding(
                "content-store",
                "no content sidecar beside the trace, so no ref resolves. "
                "Replay is impossible on this trace; the graphs still work.",
            )
        )

    for event in trace.events:
        if event.kind not in EVENT_KINDS:
            report.findings.append(
                Finding("event-kind", f"{event.id} has unknown kind {event.kind!r}")
            )

        if event.output_ref is not None:
            report.events_with_output += 1
            if trace.content is not None and trace.content.has(event.output_ref):
                report.resolved_outputs += 1
            else:
                report.findings.append(
                    Finding(
                        "output-ref",
                        f"{event.id} has output_ref {event.output_ref} that resolves "
                        "to nothing",
                    )
                )
        elif event.id in from_model:
            report.findings.append(
                Finding(
                    "output-ref",
                    f"{event.id} is a model-written {event.kind} with no output_ref. "
                    "Selective replay cannot splice an event whose output was "
                    "never stored, so this event can only be recomputed.",
                )
            )

        if event.id in from_model:
            report.model_events += 1
            prompt = trace.prompt_text(event.id)
            if prompt:
                report.model_events_with_prompt += 1
                report.findings.extend(_check_block_boundary(trace, event.id, prompt))
            else:
                report.findings.append(
                    Finding(
                        "prompt-ref",
                        f"{event.id} ({event.agent_id}/{event.kind}) has no stored "
                        "prompt. A counterfactual check on this event is not "
                        "possible, and its exposures can therefore only ever be "
                        "resolved by the conservative fallback.",
                    )
                )

        for ref in event.inputs_ref:
            if trace.content is not None and not trace.content.has(ref):
                report.findings.append(
                    Finding("inputs-ref", f"{event.id} references missing content {ref}")
                )

        if event.kind == "memory_write":
            payload = trace.content_of(event.output_ref) if trace.content else None
            if not payload or "key" not in payload:
                report.findings.append(
                    Finding(
                        "memory-write",
                        f"{event.id} is a memory_write whose key and value were not "
                        "stored. Rolling this write back is guesswork, and "
                        "docs/02 requires memory writes by contaminated events to "
                        "be undone.",
                    )
                )

        for sid in event.exposures:
            report.exposure_pairs += 1
            if trace.checked(event.id, sid) != "unchecked":
                report.checked_pairs += 1

    unchecked = trace.unchecked_pairs()
    if unchecked:
        by_event: dict[str, int] = {}
        for _sid, eid in unchecked:
            by_event[eid] = by_event.get(eid, 0) + 1
        worst = sorted(by_event.items(), key=lambda kv: -kv[1])[:4]
        report.findings.append(
            Finding(
                "check-coverage",
                f"{len(unchecked)} exposure pairs carry no check record, so the "
                f"conservative fallback decides them. Worst events: "
                f"{', '.join(f'{eid} ({n})' for eid, n in worst)}. "
                "Run the pipeline with an attributor.",
                fatal=False,
            )
        )

    # Checkpoints are not part of the contract for the graphs, but they are for
    # recovery: Step 1 of the algorithm has nothing to pick from without them.
    if path is not None:
        checkpoints = CheckpointStore.load(checkpoint_path_for(path))
        if not checkpoints:
            report.findings.append(
                Finding(
                    "checkpoints",
                    "no checkpoint sidecar. The recovery planner would have to "
                    "restart every agent from INIT.",
                    fatal=False,
                )
            )
        else:
            for checkpoint in checkpoints:
                refs = checkpoint.state.get("output_refs") or {}
                missing = [
                    r for r in refs.values()
                    if trace.content is not None and not trace.content.has(r)
                ]
                if missing:
                    report.findings.append(
                        Finding(
                            "checkpoint-refs",
                            f"{checkpoint.id} names {len(missing)} content refs that "
                            "do not resolve, so restoring it would lose state",
                        )
                    )
    return report


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "data/runs/fake.jsonl"
    trace = read_trace(path)
    trace.validate()
    report = check_contract(trace, path)
    print(report.render())
    print()
    ledger = CheckLedger.from_trace(trace)
    print(f"clearance policy   {ledger.policy.describe()}")
    print(f"                   {ledger.coverage(trace).summary()}")
    raise SystemExit(0 if report.ok else 1)
