"""
Phase 2: turning on the robustness machinery, and reporting what it costs.

`repeats` and `control_run` were built to guard against two things:

    repeats      a verdict decided by one noisy sample
    control_run  a verdict from an event whose signature does not hold still
                 even when nothing is removed

Neither has ever been on in a reported result. Every number in docs/07, docs/08
and docs/09 was produced at `repeats=1, control_run=False`, so the mechanisms
that exist to catch exactly the class of confound Phase 1 is about had never
been exercised.

WHAT THIS RUNS
--------------
A representative subset of the matrix twice -- once as reported, once with the
machinery on -- and prints the three things that decide whether it should stay
on:

  1. **did any verdict change?** That is the open question. The control run is
     supposed to catch events whose signature is unstable; on `ScriptedClient`
     the expectation is that it catches none, because the scripted noise floor
     is 0% on every facet (`python -m src.provenance.scripted_noise`). A
     measured zero is a result. A non-zero would be a bigger one.
  2. **what it costs.** `control_run` is one extra call per event and `repeats`
     is up to k per removal, so this raises `A` in `A + f*N` and makes open
     issue #7 worse. That is an expected, reportable trade-off and it is
     reported rather than minimised.
  3. **whether the answers moved.** Work preserved and unsafe preservations,
     side by side, so the safety half is visible next to the cost half.

    python -m src.eval.robustness
    python -m src.eval.robustness --repeats 5
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.eval.experiment import SCENARIOS, VARIANTS, run_cell
from src.eval.metrics import RecoveryScore

# The representative subset: every scenario, both variants, at the oracle
# detector. The detector axis is deliberately not swept -- this measures the
# estimator's robustness machinery, and the detector decides where to look
# rather than how carefully to look.
DEFAULT_DETECTOR = "oracle"


@dataclass
class Condition:
    name: str
    repeats: int
    control_run: bool
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
            "replay_tokens": sum(r.replay_tokens for r in ours),
            "recovery_tokens": sum(r.recovery_tokens for r in ours),
            "pipeline_tokens": sum(r.pipeline_tokens for r in ours),
            "work_preserved": sum(r.work_preserved for r in ours) / n,
            "unsafe_preservations": sum(r.unsafe_preservations for r in ours),
            "pair_false_negatives": sum(r.pair_false_negatives for r in ours),
            "escalations": sum(r.escalations for r in ours),
        }


def run_condition(
    name: str,
    repeats: int,
    control_run: bool,
    workdir: Path,
    detector: str = DEFAULT_DETECTOR,
) -> Condition:
    condition = Condition(name=name, repeats=repeats, control_run=control_run)
    started = time.time()
    for scenario in SCENARIOS:
        for _variant, influencing in VARIANTS:
            condition.rows.extend(
                run_cell(
                    scenario,
                    influencing,
                    detector_name=detector,
                    estimator_mode="hybrid",
                    workdir=workdir / name,
                    repeats=repeats,
                    control_run=control_run,
                )
            )
    condition.wall_clock_s = time.time() - started
    return condition


def compare(base: Condition, hardened: Condition) -> str:
    a, b = base.totals(), hardened.totals()

    def delta(key: str, percent: bool = False) -> str:
        diff = b[key] - a[key]
        if percent:
            return f"{a[key]:>10.1%}{b[key]:>12.1%}{diff:>+11.1%}"
        return f"{a[key]:>10.0f}{b[key]:>12.0f}{diff:>+11.0f}"

    n_pipeline = a["pipeline_tokens"] / (a["cells"] or 1)
    lines = [
        f"Phase 2: {base.name} vs {hardened.name}, "
        f"{int(a['cells'])} CausalLine cells each",
        "",
        f"{'metric':<26}{'as reported':>10}{'hardened':>12}{'delta':>11}",
        "-" * 59,
        f"{'analysis tokens (A, total)':<26}" + delta("analysis_tokens"),
        f"{'replay tokens (total)':<26}" + delta("replay_tokens"),
        f"{'recovery tokens (total)':<26}" + delta("recovery_tokens"),
        f"{'work preserved (mean)':<26}" + delta("work_preserved", percent=True),
        f"{'unsafe preservations':<26}" + delta("unsafe_preservations"),
        f"{'pair false negatives':<26}" + delta("pair_false_negatives"),
        f"{'escalations':<26}" + delta("escalations"),
        "",
        f"wall clock: {base.wall_clock_s:.1f}s -> {hardened.wall_clock_s:.1f}s",
    ]

    # open issue #7's ratio, which is the number this trade-off moves.
    for label, totals in (("as reported", a), ("hardened", b)):
        cells = totals["cells"] or 1
        analysis = totals["analysis_tokens"] / cells
        lines.append(
            f"  {label:<12} A={analysis:.0f} tokens/run against "
            f"N={n_pipeline:.0f}  ->  A/N = {analysis / n_pipeline if n_pipeline else 0:.2f}"
        )

    changed = _verdict_deltas(base, hardened)
    lines += ["", "did any cell's answer move?"]
    if changed:
        for line in changed:
            lines.append(f"  {line}")
    else:
        lines.append(
            "  no. Every cell preserved the same work, committed the same "
            "number of unsafe preservations and escalated the same number of "
            "times."
        )
    return "\n".join(lines)


def _verdict_deltas(base: Condition, hardened: Condition) -> list[str]:
    keyed = {
        (r.scenario, r.variant): r for r in base.causalline()
    }
    out: list[str] = []
    for row in hardened.causalline():
        old = keyed.get((row.scenario, row.variant))
        if old is None:
            continue
        if (
            abs(old.work_preserved - row.work_preserved) > 1e-9
            or old.unsafe_preservations != row.unsafe_preservations
            or old.escalations != row.escalations
        ):
            out.append(
                f"{row.scenario}-{row.variant}: work preserved "
                f"{old.work_preserved:.0%} -> {row.work_preserved:.0%}, "
                f"unsafe {old.unsafe_preservations} -> "
                f"{row.unsafe_preservations}, escalations "
                f"{old.escalations} -> {row.escalations}"
            )
    return out


def main(repeats: int = 3, detector: str = DEFAULT_DETECTOR) -> dict[str, Any]:
    workdir = Path("data/runs/robustness")
    workdir.mkdir(parents=True, exist_ok=True)
    base = run_condition("as-reported", 1, False, workdir, detector)
    hardened = run_condition(f"repeats{repeats}+control", repeats, True, workdir, detector)
    print(compare(base, hardened))
    return {
        "detector": detector,
        "conditions": [
            {
                "name": c.name,
                "repeats": c.repeats,
                "control_run": c.control_run,
                "wall_clock_s": c.wall_clock_s,
                "totals": c.totals(),
                "rows": [asdict(r) for r in c.causalline()],
            }
            for c in (base, hardened)
        ],
        "verdict_deltas": _verdict_deltas(base, hardened),
    }


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    repeats = int(args[args.index("--repeats") + 1]) if "--repeats" in args else 3
    detector = args[args.index("--detector") + 1] if "--detector" in args else DEFAULT_DETECTOR
    payload = main(repeats=repeats, detector=detector)
    out = Path("data/results/robustness.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print()
    print(f"written to {out}")
