"""Gate 1: spend analysis tokens investigating, or restart now?

TWO GATES, AND THEY ASK DIFFERENT QUESTIONS
--------------------------------------------
    GATE 1 (here)      before any analysis token is spent:
                       "is investigating likely to pay for itself?"
                       A is NOT yet spent, so A belongs in the comparison.

    GATE 2 (planner)   after the investigation, with a plan in hand:
                       "is this replay cheaper than a restart?"
                       A IS spent, is not refunded by restarting, and belongs
                       on neither side (D-088, `economics.SunkCostError`).

Both stay. Gate 2 is the safety net; Gate 1 stops the analysis bill being run
up on a workflow that was always going to be restarted.

THE RULE, AND WHERE IT COMES FROM
----------------------------------
With Gate 2 underneath, continuing costs `A + min(R, N)` and restarting costs
`N`, so investigating is preferable exactly when

    A + min(R, N) < N   <=>   A/N + f < 1        (f = R/N)

Neither A nor f is known at this point, so both are estimated from the trace:

    f_hat = structural_prior(trace, flagged)     cost-weighted B2 closure
    A_hat = A_SCALE * region_pairs * mean_event_cost

WHY THE RULE IS NOT `total < 1`
--------------------------------
Both estimators are **upper bounds by construction** -- the B2 closure is
everything contamination *could* reach, and the pair count ignores that group
testing, the frontier expansion and lazy self-report each settle more than one
pair per call. Measured on the 65-case development family, the compound bias

    (A_hat/N + f_hat) - (A/N + f)

was positive on **65 of 65 cases**, median +0.223. A gate that compares the
biased estimate against 1.0 therefore inherits a one-sided pull toward
restarting: on the development family it produced 10 false restarts and **zero**
false recoveries, every one of them a case where the estimate landed just over
1.0 while the truth sat just under.

So the comparison carries a margin, and the margin is the measured bias, not a
tuned knob: `DECISION_MARGIN` is the first quartile of that distribution.
Choosing it by sweeping accuracy would have been tuning on the metric; q25 is
derived from the error the estimator is independently known to have.

THE ASYMMETRY THAT JUSTIFIES ERRING TOWARD INVESTIGATING
---------------------------------------------------------
Because Gate 2 caps the replay, the worst case of wrongly investigating is
bounded by `A`. The worst case of wrongly restarting is bounded by `N`. When
`A < N` the cheaper mistake is to investigate, and the gate should require
decisive evidence before restarting rather than merely a coin-flip's worth.
Measured on development plus borderline cases (115 runs), the margin-carrying
gate's errors cost a mean of 296 tokens each; an SPRT that aborts mid-way cost
1957 each, because it pays for analysis *and then* restarts anyway.

WHAT THIS GATE DOES NOT DO
---------------------------
It never decides anything is clean. Declining to investigate leaves every pair
`unchecked`, and the contamination walk treats unchecked as contaminated -- so
a wrong RESTART costs preserved work and can never cost safety. That is the
same asymmetry running out of budget already has.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Calibrated on the 65-case development family and frozen before the held-out
# set was built. See `docs/gate1/02-experiments.md`.
#
# A_SCALE  median of A_full / (region_pairs * mean_event_cost) = 0.431.
# DECISION_MARGIN  q25 of the compound estimator bias = 0.174.
A_SCALE = 0.431
DECISION_MARGIN = 0.174


@dataclass
class Gate1Decision:
    investigate: bool
    f_structural: float
    a_estimate: float
    restart_tokens: int
    total: float
    margin: float
    region_pairs: int
    reason: str = ""

    def line(self) -> str:
        verdict = "INVESTIGATE" if self.investigate else "RESTART NOW"
        return (
            f"GATE1 {verdict}: Ahat/N="
            f"{self.a_estimate / max(1, self.restart_tokens):.2f} + "
            f"f_struct={self.f_structural:.2f} = {self.total:.2f} "
            f"vs 1+{self.margin:.3f} -- {self.reason}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigate": self.investigate,
            "f_structural": round(self.f_structural, 4),
            "a_estimate": round(self.a_estimate, 1),
            "restart_tokens": self.restart_tokens,
            "total": round(self.total, 4),
            "margin": self.margin,
            "region_pairs": self.region_pairs,
            "reason": self.reason,
        }


def calibrate(traces_and_costs: Iterable[tuple[Any, int]]) -> float:
    """Measure `A_SCALE` from runs this deployment has already made.

    WHY THIS IS NOT OPTIONAL, AND WHY THE CONSTANT ABOVE IS A FALLBACK
    -------------------------------------------------------------------
    `A_SCALE` converts "exposure pairs in the region, priced at the mean event
    cost" into expected analysis tokens. It is a property of the **client and
    the prompt structure**, not of the algorithm: the scripted client prices a
    call at `len(text)/4` with no system prompt, no JSON scaffolding and no
    source catalogue, while a real self-report prompt carries all three.

    Measured, it comes out at

        0.431   scripted client
        0.547   llama3.2:3b, lazy self-report
        1.781   llama3.2:3b, eager self-report

    -- a factor of four between two *modes of the same model*. A gate using the
    frozen scripted value on the real frontier scored 25% (18 false recoveries
    in 24 runs), and that is the honest verdict on hard-coding it.

    Pass `(trace, analysis_tokens)` for runs already completed under the same
    client and configuration. Returns the median ratio, which is what the
    caller should hand to `decide(..., a_scale=...)`.
    """
    import statistics

    ratios = []
    for trace, analysis_tokens in traces_and_costs:
        raw = _raw_cost_estimate(trace)
        if raw > 0 and analysis_tokens > 0:
            ratios.append(analysis_tokens / raw)
    return statistics.median(ratios) if ratios else A_SCALE


def _raw_cost_estimate(trace: Any) -> float:
    """region_pairs * mean_event_cost, before the scale factor."""
    flagged = [s.id for s in trace.sources if getattr(s, "malicious", False)]
    if not flagged:
        return 0.0
    ahat, _pairs = estimate_analysis_tokens(trace, flagged, a_scale=1.0)
    return ahat


def estimate_analysis_tokens(
    trace: Any, flagged: Iterable[str], a_scale: float | None = None
) -> tuple[float, int]:
    """(A_hat, region_pairs), from the trace alone. No model calls.

    A counterfactual check re-issues one event's prompt, so it costs about what
    that event cost, and the number of checks is bounded by the exposure pairs
    inside the structural region.
    """
    from src.eval.baselines import b2_topology_closure

    region = set(b2_topology_closure(trace, list(flagged)))
    region_pairs = sum(
        len(e.exposures) for e in trace.events if e.id in region
    )
    model_events = {
        u.event_id for u in trace.usage
        if u.purpose == "pipeline" and u.event_id
    }
    if not model_events:
        return 0.0, region_pairs
    mean_event_cost = trace.pipeline_tokens() / len(model_events)
    scale = A_SCALE if a_scale is None else a_scale
    return scale * region_pairs * mean_event_cost, region_pairs


def decide(
    trace: Any,
    flagged: Iterable[str],
    margin: float = DECISION_MARGIN,
    a_scale: float | None = None,
) -> Gate1Decision:
    """Investigate, or restart without spending anything?

    Returns INVESTIGATE when there is nothing to go on -- no flagged sources, a
    trace with no recorded pipeline cost -- because the alternative is throwing
    work away on the strength of a number that was not measured.
    """
    from src.recovery.sprt_investigate import structural_prior

    flagged = list(flagged)
    restart_tokens = int(trace.pipeline_tokens())
    if not flagged:
        return Gate1Decision(
            True, 0.0, 0.0, restart_tokens, 0.0, margin, 0,
            "nothing flagged; no reason to restart",
        )
    if restart_tokens <= 0:
        return Gate1Decision(
            True, 0.0, 0.0, restart_tokens, 0.0, margin, 0,
            "no recorded pipeline cost, so no cost model; investigating",
        )

    f_structural = structural_prior(trace, flagged)
    a_estimate, region_pairs = estimate_analysis_tokens(
        trace, flagged, a_scale=a_scale)
    total = a_estimate / restart_tokens + f_structural
    investigate = total <= 1.0 + margin
    reason = (
        "estimated analysis plus replay is within the restart budget"
        if investigate else
        "even the optimistic reading of the estimate exceeds a full restart"
    )
    return Gate1Decision(
        investigate=investigate,
        f_structural=f_structural,
        a_estimate=a_estimate,
        restart_tokens=restart_tokens,
        total=total,
        margin=margin,
        region_pairs=region_pairs,
        reason=reason,
    )
