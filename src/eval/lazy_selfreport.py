"""
Phase 3: what deferring self-report to detection time actually costs and saves.

    python -m src.eval.lazy_selfreport

THE PROPOSAL, AND THE PART OF IT THAT IS SOUND
-----------------------------------------------
Self-report fires on every model event of every run, before anyone knows whether
an attack happened. On a clean run — which is every run, in any deployment where
attacks are rare — every one of those calls is spent on a question nobody asked.

Deferring them is not supposed to weaken anything, because the contamination
walk already treats an unexamined pair as contaminated (D-024): nothing is
*clean* before it is checked, whether the check happens eagerly or lazily. That
is the claim, and this module measures it rather than repeating it.

WHAT IS COMPARED
----------------
Three conditions, identical in every other respect:

    hybrid          inline self-report on every event, then the targeted pass.
                    The condition every reported number was produced under.
    lazy            no inline attribution at all; self-report asked inside
                    `refine_for_verdict`, once per event, only for events the
                    detector's region reaches (D-082).
    targeted_only   no self-report anywhere. Phase 6's condition, kept as the
                    floor: it is what "defer it and then never need it" reduces
                    to on a clean run.

Two questions, and they need different runs:

    clean    no attack, so no detector flags anything and the deferred pass
             never fires. The saving is the whole inline cost. Measured on the
             pipeline directly, because there is no recovery to score.
    attacked the scored matrix. Here `lazy` must reproduce `hybrid` -- same
             work preserved, same unsafe count, same escalations -- or the
             deferral has changed an answer rather than its timing.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.eval.experiment import SCENARIOS, VARIANTS, run_cell, scripted_calibration
from src.eval.metrics import RecoveryScore
from src.eval.scripted import ScriptedClient
from src.provenance.attribution import NullAttributor
from src.provenance.estimator import HybridAttributor
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools

MODES = ("hybrid", "lazy", "targeted_only")
SEED = 20260906


@dataclass
class CleanRun:
    """One unattacked run, and what attribution cost it."""

    mode: str
    self_report_calls: int = 0
    self_report_tokens: int = 0
    pipeline_tokens: int = 0
    events: int = 0


def clean_run(mode: str, workdir: Path) -> CleanRun:
    """The pipeline on the clean fixture corpus. No attack, no detector, no
    recovery -- so every self-report call made here is one a deployment would
    have paid for and never used."""
    path = workdir / f"clean-{mode}.jsonl"
    client = ScriptedClient(seed=SEED)
    attributor: Any
    if mode == "hybrid":
        attributor = HybridAttributor(
            client=client, mode="self_report",
            calibration=scripted_calibration(), model="scripted", seed=SEED,
        )
    else:
        # `lazy` and `targeted_only` are identical on a clean run: neither asks
        # anything inline. They diverge only once a detector speaks.
        attributor = NullAttributor()
    run_pipeline(
        path,
        client=client,
        tools=Tools.from_fixtures(memory_path=path.with_suffix(".memory.json")),
        attributor=attributor,
    )
    trace = read_trace(path)
    out = CleanRun(mode=mode, events=len(trace.events))
    for usage in trace.usage:
        if usage.purpose == "self_report":
            out.self_report_calls += 1
            out.self_report_tokens += usage.total_tokens
        elif usage.purpose == "pipeline":
            out.pipeline_tokens += usage.total_tokens
    return out


@dataclass
class AttackedRun:
    mode: str
    rows: list[RecoveryScore] = field(default_factory=list)
    wall_clock_s: float = 0.0

    def ours(self) -> list[RecoveryScore]:
        return [r for r in self.rows if r.method == "CausalLine"]

    def totals(self) -> dict[str, float]:
        ours = self.ours()
        n = len(ours) or 1
        return {
            "cells": len(ours),
            "analysis_tokens": sum(r.analysis_tokens for r in ours),
            "recovery_tokens": sum(r.recovery_tokens for r in ours),
            "work_preserved": round(sum(r.work_preserved for r in ours) / n, 4),
            "unsafe_preservations": sum(r.unsafe_preservations for r in ours),
            "pair_false_negatives": sum(r.pair_false_negatives for r in ours),
            "escalations": sum(r.escalations for r in ours),
        }


def attacked_run(mode: str, workdir: Path, detector: str = "oracle") -> AttackedRun:
    out = AttackedRun(mode=mode)
    started = time.time()
    for scenario in SCENARIOS:
        for _variant, influencing in VARIANTS:
            out.rows.extend(
                run_cell(
                    scenario, influencing,
                    detector_name=detector,
                    estimator_mode=mode,
                    workdir=workdir / mode,
                )
            )
    out.wall_clock_s = time.time() - started
    return out


def render(clean: dict[str, CleanRun], attacked: dict[str, AttackedRun]) -> str:
    lines = [
        "Phase 3: deferring self-report to detection time (D-082)",
        "",
        "CLEAN RUN -- no attack, so nothing is ever flagged and the deferred",
        "pass never fires. This is what a deployment pays per run when nothing",
        "has happened, which is the common case.",
        "",
        f"  {'mode':<16}{'self-report calls':>19}{'self-report tokens':>20}",
        "  " + "-" * 55,
    ]
    for mode in MODES:
        c = clean[mode]
        lines.append(
            f"  {mode:<16}{c.self_report_calls:>19}{c.self_report_tokens:>20}"
        )
    base = clean["hybrid"]
    saved = base.self_report_tokens - clean["lazy"].self_report_tokens
    share = saved / base.pipeline_tokens if base.pipeline_tokens else 0.0
    lines += [
        "",
        f"  saved per clean run: {saved} tokens, "
        f"{base.self_report_calls - clean['lazy'].self_report_calls} calls "
        f"({share:.0%} of the run's own pipeline cost)",
        "",
        "ATTACKED RUNS -- the scored matrix, oracle detector. `lazy` has to",
        "reproduce `hybrid` here, or the deferral changed an answer and not",
        "just its timing.",
        "",
        f"  {'metric':<24}" + "".join(f"{m:>16}" for m in MODES),
        "  " + "-" * (24 + 16 * len(MODES)),
    ]
    keys = [
        "analysis_tokens", "recovery_tokens", "work_preserved",
        "unsafe_preservations", "pair_false_negatives", "escalations",
    ]
    totals = {m: attacked[m].totals() for m in MODES}
    for key in keys:
        row = f"  {key:<24}"
        for mode in MODES:
            value = totals[mode][key]
            row += f"{value:>16.1%}" if key == "work_preserved" else f"{value:>16}"
        lines.append(row)

    same = all(
        totals["lazy"][k] == totals["hybrid"][k]
        for k in ("work_preserved", "unsafe_preservations", "escalations")
    )
    lines += [
        "",
        "VERDICT",
        "  lazy reproduces hybrid on the attacked matrix: "
        + ("YES" if same else "NO -- the deferral changed an answer"),
        f"  self-report's safety value, hybrid vs targeted_only: "
        f"{totals['hybrid']['recovery_tokens'] - totals['targeted_only']['recovery_tokens']:+d}"
        " recovery tokens, "
        f"{totals['targeted_only']['pair_false_negatives'] - totals['hybrid']['pair_false_negatives']:+d}"
        " pair false negatives avoided",
    ]
    if not same:
        lines.append(
            "  -> that is a finding, not a bug to hide: report which metric "
            "moved and why before adopting the deferral."
        )
    return "\n".join(lines)


def main(detector: str = "oracle") -> dict[str, Any]:
    workdir = Path("data/runs/lazy")
    workdir.mkdir(parents=True, exist_ok=True)
    clean = {mode: clean_run(mode, workdir) for mode in MODES}
    attacked = {mode: attacked_run(mode, workdir, detector) for mode in MODES}
    print(render(clean, attacked))
    report = {
        "detector": detector,
        "clean": {m: asdict(c) for m, c in clean.items()},
        "attacked": {m: attacked[m].totals() for m in MODES},
    }
    out = Path("data/results/lazy-selfreport.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwritten to {out}")
    return report


if __name__ == "__main__":
    main()
