"""Building the case family Gate 1 is judged on.

Every case is produced by running the COMPLETE process -- pipeline, detection,
full investigation, planning, selective replay -- and reading `N`, `A` and `R`
off the trace. The oracle is that measurement. Nothing here labels a case with
what it "should" have done.

THE FAMILY IS DESIGNED TO CONTAIN CASES THAT BREAK GATES
---------------------------------------------------------
Sweeping only the easy regimes would produce a gate that works where the answer
is obvious. Deliberately included:

  * **localized**   fan-out, 1 of K poisoned. Recovery obviously pays.
  * **widespread**  fan-out, K of K poisoned, and the chain scenarios where a
                    poisoned source reaches everything downstream.
  * **borderline**  the intermediate poison counts, which walk `f` through the
                    economic break-even point in small steps. This is where a
                    gate is actually tested.
  * **exposed-only** controls: many exposure edges, zero influence. These are
                    adversarial for every signal derived from exposure --
                    `structural_prior` (B2 closure) and `P` both -- because
                    both will read them as heavily contaminated when nothing
                    is.
  * **noisy**       the planted instruction is followed only some of the time,
                    so the early verdicts on the tape are a bad sample of the
                    later ones. This is the case SPRT's i.i.d. assumption is
                    most exposed to.

Development and held-out sets are split by **workflow size**, not at random:
splitting at random would put near-identical runs of the same design point on
both sides and make the held-out set a re-test of the development set.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from src.eval.gate1_experiment import Case, FanoutScriptedClient, read_tape
from src.tracing.logger import read_trace

# --- free structural signals, computed before any analysis token is spent -----


def structural_signals(trace: Any, flagged: Iterable[str]) -> dict[str, Any]:
    """Everything a gate may look at for free. No model calls."""
    from src.eval.baselines import b1_agent_taint, b2_topology_closure
    from src.recovery.sprt_investigate import structural_prior
    from src.risk.attack_model import run_probability
    from src.tracing.graphs import EventGraph

    flagged = list(flagged)
    b2 = b2_topology_closure(trace, flagged)
    b1 = b1_agent_taint(trace, flagged)

    graph = EventGraph.from_trace(trace)
    order = graph.topological_order()
    depth = {}
    children: dict[str, list[str]] = {e.id: [] for e in trace.events}
    for event in trace.events:
        for parent in event.parents:
            children.setdefault(parent, []).append(event.id)
    for eid in order:
        event = trace.event(eid)
        depth[eid] = 1 + max((depth.get(p, 0) for p in event.parents), default=0)

    # Pre-investigation cost predictors. A gate needs an estimate of A, and it
    # must be computable from the trace ALONE -- the first version of this
    # experiment estimated A from the mean cost per check on the tape, which is
    # only known AFTER the investigation it is supposed to gate. That is a
    # leak, and it flattered every economic gate.
    region_pairs = sum(
        1 for e in trace.events for sid in e.exposures
        if e.id in set(b2)
    )
    model_events = {u.event_id for u in trace.usage
                    if u.purpose == "pipeline" and u.event_id}
    mean_event_cost = (
        trace.pipeline_tokens() / len(model_events) if model_events else 0.0
    )
    return {
        "region_pairs": region_pairs,
        "mean_event_cost": mean_event_cost,
        "structural_prior": structural_prior(trace, flagged),
        "compromise_p": run_probability(trace),
        "b2_events": len(b2),
        "b1_events": len(b1),
        "flagged_sources": len(flagged),
        "exposure_edges": sum(len(e.exposures) for e in trace.events),
        "graph_depth": max(depth.values(), default=0),
        "max_fanout": max((len(v) for v in children.values()), default=0),
        "downstream_events": len(b2),
    }


# --- fan-out cases ------------------------------------------------------------


def _fanout_case(
    workers: int,
    poisoned: int,
    intent: str,
    miss_rate: float,
    workdir: Path,
    seed: int,
) -> Case | None:
    from src.eval.fanout_scenarios import FanoutScenario
    from src.eval.real_llm import run_generated
    from src.tracing.tools import Tools, fanout_corpus

    scenario = FanoutScenario.build(
        workers, intent, poisoned_indices=tuple(range(poisoned))
    )
    docs = [dict(d) for d in fanout_corpus(workers)]
    for index in scenario.poisoned:
        docs[index]["injected"] = scenario.payload

    def client() -> FanoutScriptedClient:
        return FanoutScriptedClient(
            docs=docs, marker=scenario.marker, token=scenario.token,
            miss_rate=miss_rate, seed=seed,
        )

    case_dir = workdir / f"fan{workers:02d}p{poisoned:02d}-{intent[:3]}-m{int(miss_rate*100)}"
    try:
        result = run_generated(
            scenario, client(), workdir=case_dir, detector_name="oracle",
            seed=seed, replay_client_factory=client, lazy_self_report=True,
            # THE ORACLE MUST NOT BE DEFINED BY THE GATE BEING TESTED.
            gate1_enabled=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    skip fan{workers}p{poisoned}/{intent}: "
              f"{type(exc).__name__}: {exc}")
        return None
    if not result.ok:
        print(f"    skip fan{workers}p{poisoned}/{intent}: {result.failure}")
        return None

    trace_path = Path(result.trace_path)
    trace = read_trace(trace_path)
    from src.eval.real_llm import label_malicious

    flagged = [s.id for s in trace.sources if s.malicious]
    signals = structural_signals(trace, flagged)

    rows = {r.method: r for r in result.rows}
    mine = rows.get("CausalLine")
    if mine is None:
        return None

    return Case(
        case_id=case_dir.name,
        family="fanout",
        shape=f"K={workers} poisoned={poisoned} {intent} miss={miss_rate}",
        workers=workers,
        poisoned=poisoned,
        intent=intent,
        n_restart=trace.pipeline_tokens(),
        a_full=trace.analysis_tokens(),
        r_replay=mine.replay_tokens,
        events=len(trace.events),
        escalated=bool(mine.escalations),
        unsafe=mine.unsafe_preservations,
        task_success=result.task_success,
        tape=read_tape(trace_path),
        **signals,
    )


# --- chain cases --------------------------------------------------------------


def _chain_case(
    scenario: str, influencing: bool, workflow: str, workdir: Path, seed: int
) -> Case | None:
    from src.eval.experiment import run_cell

    variant = "influencing" if influencing else "exposed_only"
    try:
        rows = run_cell(
            scenario, influencing, detector_name="oracle",
            estimator_mode="hybrid", workdir=workdir, seed=seed,
            workflow=workflow,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    skip chain {scenario}/{variant}/{workflow}: "
              f"{type(exc).__name__}: {exc}")
        return None

    stem = f"exp-{scenario}-{variant}-hybrid-oracle"
    if workflow != "short":
        stem = f"{stem}-{workflow}"
    trace_path = workdir / f"{stem}.jsonl"
    if not trace_path.exists():
        return None
    trace = read_trace(trace_path)
    flagged = [s.id for s in trace.sources if s.malicious]
    if not flagged:
        return None
    signals = structural_signals(trace, flagged)

    mine = next((r for r in rows if r.method == "CausalLine"), None)
    if mine is None:
        return None

    return Case(
        case_id=f"chain-{scenario}-{variant}-{workflow}",
        family="chain",
        shape=f"{scenario} {variant} {workflow}",
        workers=0,
        poisoned=1 if influencing else 0,
        intent=variant,
        n_restart=trace.pipeline_tokens(),
        a_full=trace.analysis_tokens(),
        r_replay=mine.replay_tokens,
        events=len(trace.events),
        escalated=bool(mine.escalations),
        unsafe=mine.unsafe_preservations,
        task_success=mine.task_success,
        tape=read_tape(trace_path),
        **signals,
    )


# --- the family ---------------------------------------------------------------

# Split by SIZE, not at random: near-identical repetitions of one design point
# on both sides would make the held-out set a re-test of the development set.
DEV_WORKERS = (6, 10, 14, 20, 28)
HELDOUT_WORKERS = (8, 12, 18, 24, 32)


def build_family(
    workdir: Path,
    workers_set: Iterable[int],
    include_chain: bool = True,
    include_noise: bool = True,
    seed: int = 20260917,
) -> list[Case]:
    cases: list[Case] = []
    for workers in workers_set:
        # Poison counts that walk f from localized to total, including the
        # intermediate values where the economics is genuinely close.
        # A FINE ladder at the low end, not a coarse quartile sweep. The first
        # attempt used {1, K/4, K/2, 3K/4, K} and produced 42 RESTART cases
        # against 6 RECOVER with exactly one borderline case -- on which a gate
        # that always restarts scores 87.5% and learns nothing. The economics
        # turns over between one and a few poisoned analysts, so that is where
        # the resolution has to be.
        counts = sorted({c for c in (1, 2, 3, 4, 6, 8, 12, 16, workers)
                         if 1 <= c <= workers})
        for poisoned in counts:
            case = _fanout_case(workers, poisoned, "influencing", 0.0,
                                workdir, seed)
            if case:
                cases.append(case)
        # The exposure-without-influence control: adversarial for every
        # exposure-derived signal.
        case = _fanout_case(workers, max(1, workers // 2), "exposed_only", 0.0,
                            workdir, seed)
        if case:
            cases.append(case)
        if include_noise:
            # Early verdicts unrepresentative of later ones.
            for miss in (0.3, 0.6):
                case = _fanout_case(workers, max(1, workers // 2),
                                    "influencing", miss, workdir, seed)
                if case:
                    cases.append(case)

    if include_chain:
        for scenario in ("A", "B", "C"):
            for influencing in (True, False):
                for workflow in ("short", "long"):
                    case = _chain_case(scenario, influencing, workflow,
                                       workdir, seed)
                    if case:
                        cases.append(case)
    return cases


def save(cases: list[Case], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([c.to_dict() for c in cases], indent=2), encoding="utf-8"
    )


def load(path: Path) -> list[Case]:
    return [Case.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]
