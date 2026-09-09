"""
Phase 7: run B0, B1, B2, and CausalLine on scenarios A, B, and C.

Offline by default (scripted agent). Produces the docs/04 table without
hand-editing.

    python -m src.eval.experiment
    python -m src.eval.experiment --ablation
    python -m src.eval.experiment --detector pessimistic

Each cell is one original run plus one recovery replay per method. The
original run uses the hybrid estimator. Recovery uses the same scripted
client, so the comparison is free of quota and of model noise -- and is
therefore a measurement of the *method*, not of gemini-3.6-flash. Live
numbers need a separate hosted pass (D-019).
"""

import time
from pathlib import Path
from typing import Any, Iterable

from src.eval.attacks import build, label_malicious
from src.eval.baselines import (
    b0_full_restart,
    b1_agent_taint,
    b2_topology_closure,
    discarded_events,
)
from src.eval.detectors import Oracle, build as build_detector
from src.eval.influence_eval import score_estimator
from src.eval.metrics import RecoveryScore, ground_truth_events, recovery_table
from src.eval.scripted import ScriptedClient, ground_truth_influence
from src.provenance.estimator import CheckBudget, HybridAttributor, refine_for_verdict
from src.provenance.signatures import Calibration
from src.recovery.causalline import recover
from src.recovery.replay import replay
from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for, overhead
from src.tracing.logger import read_trace
from src.tracing.pipeline import run_pipeline
from src.tracing.tools import Tools

SCENARIOS = ("A", "B", "C")
VARIANTS = (("influencing", True), ("exposed_only", False))
METHODS = ("B0 full restart", "B1 agent taint", "B2 topology closure", "CausalLine")


def _fresh_tools(attack, path: Path) -> Tools:
    clean = Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
    return attack.apply(clean)


def _original_run(
    scenario: str,
    influencing: bool,
    path: Path,
    seed: int,
    estimator_mode: str = "hybrid",
) -> tuple[Any, ScriptedClient, set[tuple[str, str]]]:
    attack = build(scenario, influencing)
    tools = _fresh_tools(attack, path)
    client = ScriptedClient(seed=seed)
    # D-032: self-report inline, counterfactual only on the detector's
    # region. The `hybrid` mode here means that targeted pass, not the
    # inline-everything ablation.
    inline_mode = "self_report" if estimator_mode == "hybrid" else estimator_mode
    attributor = None
    if estimator_mode != "none":
        attributor = HybridAttributor(
            client=client,
            mode=inline_mode,
            calibration=Calibration.load(),
            model="scripted",
            seed=seed,
            audit_rate=0.0,
            trust_self_report_negatives=(estimator_mode == "self_report"),
        )
    result = run_pipeline(
        path,
        client=client,
        tools=tools,
        attributor=attributor,
        handoff_hook=attack.handoff_hook,
    )
    marked = label_malicious(path, attack.marker)
    if not marked:
        raise RuntimeError(
            f"{attack.name}: marker never reached the trace; run is void"
        )
    if estimator_mode == "hybrid":
        flagged = Oracle().flag(read_trace(path)).sources()
        refine_for_verdict(
            path,
            flagged,
            client,
            calibration=Calibration.load(),
            budget=CheckBudget(),
            model="scripted",
        )
    trace = read_trace(path)
    truth = ground_truth_influence(trace, client)
    return result, client, truth


def _score_row(
    *,
    scenario: str,
    variant: str,
    method: str,
    detector: str,
    trace,
    discarded: Iterable[str],
    truth_events: set[str],
    recovery_tokens: int,
    analysis_tokens: int,
    replay_tokens: int,
    task_success: bool,
    wall_clock_s: float,
    storage_bytes: int,
    blast_events: int,
    blast_agents: int,
    escalations: int = 0,
    notes: str = "",
    pair_unsafe_rate: float = 0.0,
    pair_false_negatives: int = 0,
    pair_scored: int = 0,
) -> RecoveryScore:
    discarded_set = set(discarded)
    unsafe = sorted(truth_events - discarded_set)
    total = len(trace.events)
    return RecoveryScore(
        scenario=scenario,
        variant=variant,
        method=method,
        detector=detector,
        total_events=total,
        discarded=len(discarded_set),
        work_preserved=(total - len(discarded_set)) / total if total else 0.0,
        recovery_tokens=recovery_tokens,
        analysis_tokens=analysis_tokens,
        replay_tokens=replay_tokens,
        pipeline_tokens=trace.pipeline_tokens(),
        unsafe_preservations=len(unsafe),
        unsafe_ids=tuple(unsafe),
        task_success=task_success,
        wall_clock_s=wall_clock_s,
        storage_bytes=storage_bytes,
        blast_radius_events=blast_events,
        blast_radius_agents=blast_agents,
        escalations=escalations,
        notes=notes,
        pair_unsafe_rate=pair_unsafe_rate,
        pair_false_negatives=pair_false_negatives,
        pair_scored=pair_scored,
    )


def _run_baseline_recovery(
    method: str,
    discard: set[str],
    original,
    client,
    flagged: list[str],
    attack,
    out: Path,
) -> tuple[Any, Any]:
    tools = _fresh_tools(attack, out)
    started = time.time()
    result, report = replay(
        original,
        discard,
        client,
        out,
        tools=tools,
        flagged=flagged,
        handoff_hook=attack.handoff_hook,
    )
    report.wall_clock_s = time.time() - started
    return result, report


def run_cell(
    scenario: str,
    influencing: bool,
    detector_name: str = "oracle",
    estimator_mode: str = "hybrid",
    workdir: Path | None = None,
    seed: int = 20260906,
    **detector_kwargs: Any,
) -> list[RecoveryScore]:
    """One (scenario, variant) against every method."""
    variant = "influencing" if influencing else "exposed_only"
    workdir = workdir or Path("data/runs")
    workdir.mkdir(parents=True, exist_ok=True)
    stem = f"exp-{scenario}-{variant}-{estimator_mode}-{detector_name}"
    orig_path = workdir / f"{stem}.jsonl"

    _outcome, client, true_inf = _original_run(
        scenario, influencing, orig_path, seed, estimator_mode
    )
    original = read_trace(orig_path)
    original.validate()
    attack = build(scenario, influencing)
    verdict = build_detector(detector_name, **detector_kwargs).flag(original)
    flagged = verdict.sources()
    truth_events = ground_truth_events(original, true_influence=true_inf)
    # Pair-level estimator accuracy, scored against the scripted client's own
    # leave-one-out record (which the estimator never sees). Computed once and
    # attached to every row so the event-level and pair-level unsafe numbers
    # are never reported apart -- see the note on RecoveryScore.
    estimator_score = score_estimator(original, client)
    pair_rate = estimator_score.unsafe_preservation_rate
    pair_fn = estimator_score.false_negative
    pair_n = estimator_score.true_positive + estimator_score.false_negative
    store = overhead(orig_path)
    analysis = original.analysis_tokens()
    checkpoints = CheckpointStore.load(checkpoint_path_for(orig_path))

    discard_of = {
        "B0 full restart": b0_full_restart(original, flagged),
        "B1 agent taint": b1_agent_taint(original, flagged),
        "B2 topology closure": b2_topology_closure(original, flagged),
    }

    rows: list[RecoveryScore] = []
    for method, discard in discard_of.items():
        out = workdir / f"{stem}-{method.split()[0]}.jsonl"
        # A new scripted client so replay calls do not share the original
        # usage log (ground truth is already captured).
        replay_client = ScriptedClient(seed=seed + 1)
        result, report = _run_baseline_recovery(
            method, discard, original, replay_client, flagged, attack, out
        )
        # Read off the replay report rather than from `discard` directly, so
        # the baselines and CausalLine are scored through the same function on
        # the same field. Identical here by construction -- which is the point:
        # it stays identical when someone changes one of them.
        redone = discarded_events(report, discard)
        rows.append(
            _score_row(
                scenario=scenario,
                variant=variant,
                method=method,
                detector=verdict.detector,
                trace=original,
                discarded=redone,
                truth_events=truth_events,
                # Baselines do not run the estimator. Charging them the
                # original run's analysis tokens would hide the cost that
                # is ours alone (docs/04, open issue #7).
                recovery_tokens=report.replay_tokens,
                analysis_tokens=0,
                replay_tokens=report.replay_tokens,
                task_success=result.task_success,
                wall_clock_s=report.wall_clock_s,
                storage_bytes=store["total_bytes"],
                blast_events=len(redone),
                blast_agents=len({original.event(e).agent_id for e in redone} - {"user"}),
                pair_unsafe_rate=pair_rate,
                pair_false_negatives=pair_fn,
                pair_scored=pair_n,
            )
        )

    out = workdir / f"{stem}-CausalLine.jsonl"
    replay_client = ScriptedClient(seed=seed + 1)
    tools = _fresh_tools(attack, out)
    recovered = recover(
        original,
        flagged,
        replay_client,
        out,
        tools=tools,
        checkpoints=checkpoints,
        handoff_hook=attack.handoff_hook,
    )
    # ONE definition of discarded, shared with the baselines above: the set
    # handed to replay(). See `discarded_events()` in src/eval/baselines.py.
    discarded = discarded_events(recovered.report, recovered.invalidated)
    rows.append(
        _score_row(
            scenario=scenario,
            variant=variant,
            method="CausalLine",
            detector=verdict.detector,
            trace=original,
            discarded=discarded,
            truth_events=truth_events,
            recovery_tokens=recovered.recovery_tokens,
            analysis_tokens=recovered.analysis_tokens,
            replay_tokens=recovered.replay_tokens,
            task_success=recovered.task_success,
            wall_clock_s=recovered.wall_clock_s,
            storage_bytes=store["total_bytes"],
            blast_events=recovered.blast_radius_events,
            blast_agents=recovered.blast_radius_agents,
            escalations=recovered.escalations,
            notes=f"scope={recovered.scope}",
            pair_unsafe_rate=pair_rate,
            pair_false_negatives=pair_fn,
            pair_scored=pair_n,
        )
    )
    return rows


def run_matrix(
    detector: str = "oracle",
    estimator_mode: str = "hybrid",
    workdir: Path | None = None,
    **detector_kwargs: Any,
) -> list[RecoveryScore]:
    rows: list[RecoveryScore] = []
    for scenario in SCENARIOS:
        for variant, influencing in VARIANTS:
            print(f"-- {scenario} {variant} / {estimator_mode} / {detector}")
            rows.extend(
                run_cell(
                    scenario,
                    influencing,
                    detector_name=detector,
                    estimator_mode=estimator_mode,
                    workdir=workdir,
                    **detector_kwargs,
                )
            )
    return rows


def run_ablations(workdir: Path | None = None) -> list[RecoveryScore]:
    """docs/04: self-report only, counterfactual only, hybrid."""
    rows: list[RecoveryScore] = []
    for mode in ("self_report", "counterfactual", "hybrid"):
        rows.extend(run_matrix(estimator_mode=mode, workdir=workdir))
    return rows


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    detector = "oracle"
    if "--detector" in args:
        detector = args[args.index("--detector") + 1]
    workdir = Path("data/runs")
    if "--ablation" in args:
        rows = run_ablations(workdir)
    else:
        rows = run_matrix(detector=detector, workdir=workdir)
    print()
    print(recovery_table(rows))
    print()
    print(f"detector: {detector}")
    print("CausalLine vs B1 (the baseline we claim to beat), work preserved:")
    for scenario in SCENARIOS:
        for variant, _ in VARIANTS:
            subset = [r for r in rows if r.scenario == scenario and r.variant == variant]
            ours = next((r for r in subset if r.method == "CausalLine"), None)
            b1 = next((r for r in subset if r.method == "B1 agent taint"), None)
            if ours and b1:
                print(
                    f"  {scenario} {variant:<14}  CausalLine {ours.work_preserved:.0%}  "
                    f"B1 {b1.work_preserved:.0%}  "
                    f"unsafe {ours.unsafe_preservations}  "
                    f"success {ours.recovery_success}"
                )
