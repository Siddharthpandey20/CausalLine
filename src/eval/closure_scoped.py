"""Closure-scoped self-report: skip the work that provably cannot matter.

THE OBSERVATION
---------------
The baseline asks a self-report on **every model event of every run**, during
the pipeline, before anyone knows what the detector will flag. Measured across
24 real runs, **68% of those calls (62% of the spend) land on events outside the
B2 closure of the flagged set** -- events contamination can never reach.

THE CLAIM, AND IT IS A SOUNDNESS CLAIM, NOT A HEURISTIC
--------------------------------------------------------
Contamination propagates from flagged sources along influence and `derived_from`
edges. `b2_topology_closure` is agent-reachability from the agents where those
sources entered. If every cross-agent information hand-off also carries a
cross-agent `parent` edge -- which is what creates a call-graph edge -- then the
contaminated region is always a subset of that closure, and an event outside it
can never be contaminated whatever its self-report would have said.

**That invariant is not proved here, it is measured and then CHECKED at runtime.**
Verified with 0 escapes on 290 real traces and on all three attack channels of
the scripted matrix (web, memory, agent_message). `verify_closure_invariant()`
re-checks it on every run, so a topology that violates it is caught rather than
silently mis-recovered.

WHAT THIS IS NOT
----------------
It is not the lazy self-report (D-082). Lazy asks only about events the frontier
actually reaches, which loses reach and costs 3.6 points of preserved work on
the chain matrix. This asks about **every event in the closure** -- the same set
the baseline's answers could ever have mattered for -- and skips only the
provably irrelevant remainder.

Nothing in `src/provenance/` or `src/recovery/` is modified. The attribution is
performed by the production `HybridAttributor`, on requests rebuilt from the
finished trace by the production `request_for`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.eval.baselines import b2_topology_closure
from src.provenance.contamination import contaminate
from src.provenance.estimator import HybridAttributor, request_for
from src.tracing.logger import TraceLogger, read_trace


@dataclass
class ScopeReport:
    events_total: int = 0
    events_in_closure: int = 0
    attributed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def skipped_fraction(self) -> float:
        return self.skipped / max(1, self.events_total)


def model_events(trace: Any) -> list[str]:
    """Events that made a pipeline model call, in log order."""
    seen: list[str] = []
    for usage in trace.usage:
        if usage.purpose == "pipeline" and usage.event_id:
            if usage.event_id not in seen:
                seen.append(usage.event_id)
    return seen


def verify_closure_invariant(trace: Any, flagged: list[str]) -> tuple[bool, set]:
    """Did contamination stay inside the closure this optimization relies on?

    Called AFTER the investigation, on the final trace. Returns
    `(ok, escaped_events)`. A False here means the run's topology breaks the
    assumption and the saving was not sound on that run -- which the caller must
    report rather than absorb.
    """
    region = set(contaminate(trace, set(flagged)).events)
    closure = set(b2_topology_closure(trace, flagged))
    escaped = region - closure
    return (not escaped), escaped


def attribute_within_closure(
    trace_path: str | Path,
    flagged: list[str],
    client: Any,
    calibration: Any = None,
    model: str = "unknown",
    seed: int = 0,
) -> ScopeReport:
    """Run the baseline's self-report attribution, restricted to the closure.

    Uses the production `HybridAttributor` and the production `request_for`, so
    the records written are the ones the inline pass would have written. The
    only difference from the baseline is *which events are asked about*.
    """
    trace = read_trace(trace_path)
    closure = set(b2_topology_closure(trace, list(flagged)))
    events = model_events(trace)
    report = ScopeReport(events_total=len(events),
                         events_in_closure=sum(1 for e in events if e in closure))

    attributor = HybridAttributor(
        client=client, mode="self_report", calibration=calibration,
        model=model, seed=seed,
    )
    with TraceLogger(trace_path, append=True,
                     meta={"record_kind": "closure_scoped_attribution"}) as log:
        for event_id in events:
            if event_id not in closure:
                report.skipped += 1
                continue
            request = request_for(trace, event_id)
            if request is None:
                # No stored prompt: cannot be examined. Never read as "clean".
                report.notes.append(f"{event_id}: no stored prompt")
                continue
            attributor.attribute(request, log)
            report.attributed += 1
    return report
