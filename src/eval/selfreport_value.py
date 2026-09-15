"""
Phase 6: is the cheap stage still earning its keep?

Self-report has never established influence. docs/02 calls it a claim rather
than evidence, `SelfReportAttributor` deliberately leaves negatives unrecorded,
and the estimator spends a counterfactual on every negative it makes. What it
does is **triage**: its positives are accepted without verification (a wrong
positive costs replay tokens, never safety), so the targeted pass has fewer
pairs left to examine.

That was a defensible trade when the expensive stage was the only thing standing
between a claim and a clearance. Phases 1-3 have since made the expensive stage
stricter -- a removal-aware facet, a nested removability check, carrier
clearances that no longer outrank what they inherit -- so the question is worth
re-asking with a measurement instead of an intuition.

THE COMPARISON, AND WHY IT IS NOT THE OLD ABLATION
---------------------------------------------------
`--ablation` runs `self_report`, `counterfactual` and `hybrid`, and none of
those isolates the triage: `self_report` and `counterfactual` also skip the
targeted pass entirely, so they differ from `hybrid` in two things at once.

This compares `hybrid` against `targeted_only`, which is `hybrid` with the
inline self-report removed and nothing else changed. The difference between them
is exactly what self-report buys.

Three outcomes are possible and all three are reportable:

    keep as a cost-ordering heuristic  it measurably reduces the targeted pass
    remove entirely                    it does not, and it costs a call an event
    keep unchanged                     it reduces cost AND changes no verdict

    python -m src.eval.selfreport_value
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.eval.experiment import SCENARIOS, VARIANTS, run_cell
from src.eval.metrics import RecoveryScore

MODES = ("hybrid", "targeted_only")


@dataclass
class ModeResult:
    mode: str
    rows: list[RecoveryScore] = field(default_factory=list)
    wall_clock_s: float = 0.0

    def causalline(self) -> list[RecoveryScore]:
        return [r for r in self.rows if r.method == "CausalLine"]

    def totals(self) -> dict[str, float]:
        ours = self.causalline()
        n = len(ours) or 1
        return {
            "cells": len(ours),
            "analysis_tokens": sum(r.analysis_tokens for r in ours),
            "recovery_tokens": sum(r.recovery_tokens for r in ours),
            "work_preserved": sum(r.work_preserved for r in ours) / n,
            "unsafe_preservations": sum(r.unsafe_preservations for r in ours),
            "pair_false_negatives": sum(r.pair_false_negatives for r in ours),
            "escalations": sum(r.escalations for r in ours),
            "task_successes": sum(1 for r in ours if r.recovery_success),
        }


def run_mode(mode: str, workdir: Path, detector: str = "oracle") -> ModeResult:
    out = ModeResult(mode=mode)
    started = time.time()
    for scenario in SCENARIOS:
        for _variant, influencing in VARIANTS:
            out.rows.extend(
                run_cell(
                    scenario,
                    influencing,
                    detector_name=detector,
                    estimator_mode=mode,
                    workdir=workdir / mode,
                )
            )
    out.wall_clock_s = time.time() - started
    return out


def recommendation(with_sr: ModeResult, without: ModeResult) -> str:
    """The decision, reasoned on cost AND safety rather than on cost alone.

    Both axes are needed, because self-report can move them in opposite
    directions and the design docs only ever claimed the cost one. Its positives
    are accepted without verification and recorded as `tainted`, so an
    over-claiming reporter ADDS contamination -- which costs replay tokens and
    can only improve safety. A measurement that looked at tokens alone would
    read that as pure waste.
    """
    a, b = with_sr.totals(), without.totals()
    cheaper = a["recovery_tokens"] < b["recovery_tokens"]
    safer = (
        a["unsafe_preservations"] < b["unsafe_preservations"]
        or a["pair_false_negatives"] < b["pair_false_negatives"]
    )
    worse_safety = (
        a["unsafe_preservations"] > b["unsafe_preservations"]
        or a["pair_false_negatives"] > b["pair_false_negatives"]
    )
    cost = b["recovery_tokens"] - a["recovery_tokens"]

    if cheaper and not worse_safety:
        return (
            "KEEP AS A COST-ORDERING HEURISTIC, which is what it was always "
            f"described as. It saves {abs(cost):.0f} recovery tokens across "
            "these cells and costs nothing in safety."
        )
    if safer and not cheaper:
        return (
            "KEEP -- BUT ITS JOB IS NOT THE ONE THE DOCS CLAIM. It is "
            f"{cost * -1:+.0f} recovery tokens, i.e. MORE expensive, and it is "
            "safer: its over-claimed positives are recorded as `tainted` "
            "without verification, so they taint pairs the targeted pass alone "
            "misses. That is a conservative bias bought with tokens, not a "
            "cost-ordering heuristic, and it should be described that way."
        )
    if worse_safety:
        return (
            "REMOVE, and treat this as a finding rather than a tidy-up: the "
            "cheap stage is making the result LESS safe. A triage stage whose "
            "claims are acted on is not triage."
        )
    return (
        "REMOVE. It costs one call per event, it is "
        f"{cost * -1:+.0f} recovery tokens, and it buys nothing measurable "
        "here -- same verdicts, same safety."
    )


def render(with_sr: ModeResult, without: ModeResult) -> str:
    a, b = with_sr.totals(), without.totals()

    def row(label: str, key: str, percent: bool = False) -> str:
        if percent:
            return (
                f"{label:<28}{a[key]:>12.1%}{b[key]:>16.1%}"
                f"{b[key] - a[key]:>+12.1%}"
            )
        return f"{label:<28}{a[key]:>12.0f}{b[key]:>16.0f}{b[key] - a[key]:>+12.0f}"

    lines = [
        f"Phase 6: self-report's value, {int(a['cells'])} CausalLine cells each",
        "",
        f"{'metric':<28}{'hybrid':>12}{'targeted_only':>16}{'delta':>12}",
        "-" * 68,
        row("analysis tokens (total)", "analysis_tokens"),
        row("recovery tokens (total)", "recovery_tokens"),
        row("work preserved (mean)", "work_preserved", percent=True),
        row("unsafe preservations", "unsafe_preservations"),
        row("pair false negatives", "pair_false_negatives"),
        row("escalations", "escalations"),
        row("recoveries that verified", "task_successes"),
        "",
        "`targeted_only` is `hybrid` minus the inline self-report. Nothing else",
        "differs, so the delta column is what the cheap stage buys.",
        "",
        "DECISION: " + recommendation(with_sr, without),
    ]
    return "\n".join(lines)


def main(detector: str = "oracle") -> dict[str, Any]:
    workdir = Path("data/runs/selfreport-value")
    workdir.mkdir(parents=True, exist_ok=True)
    results = {mode: run_mode(mode, workdir, detector) for mode in MODES}
    print(render(results["hybrid"], results["targeted_only"]))
    return {
        "detector": detector,
        "modes": {
            mode: {
                "wall_clock_s": r.wall_clock_s,
                "totals": r.totals(),
                "rows": [asdict(x) for x in r.causalline()],
            }
            for mode, r in results.items()
        },
        "decision": recommendation(results["hybrid"], results["targeted_only"]),
    }


if __name__ == "__main__":
    payload = main()
    out = Path("data/results/selfreport-value.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print()
    print(f"written to {out}")
