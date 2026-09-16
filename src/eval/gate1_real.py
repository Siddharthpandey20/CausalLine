"""Falsification-first validation of Gate 1 on the real local LLaMA.

FROZEN BEFORE THE RUN
---------------------
`src/recovery/gate1.py` is not modified by this module. `DECISION_MARGIN`
(0.174) and the calibration *procedure* are exactly as committed in D-092.
`A_SCALE` is measured from the 24 pre-existing GPU runs in
`data/results/fanout/fanout-campaign.json`, which were produced before this
suite existed and share no case with it.

THE HYPOTHESIS UNDER TEST
-------------------------
    H_G1: a cheap structural upper bound on the recovery footprint, plus a
          calibrated estimate of A/N, is a useful zero-cost pre-investigation
          recovery-vs-restart decision.

FALSIFICATION CRITERIA, WRITTEN DOWN BEFORE ANY CASE WAS RUN
-------------------------------------------------------------
H_G1 is NOT SUPPORTED on real LLaMA if any of these is observed:

    F1  repeated false RESTARTS in the localized family (A) -- the gate
        throwing away recoverable work in the regime it was built for.
    F2  repeated false RECOVERIES in the widespread family (B).
    F3  the wide-exposure/no-influence family (D) produces false restarts.
        This is the pre-registered prediction: f_structural is the call-graph
        closure of where a flagged source ENTERED, and in family D every
        analyst retrieves the bulletin, so the closure is the whole trace
        (measured offline at f_struct = 1.000) while true influence is nil.
        If A/N < 1 there, the oracle says INVESTIGATE and the gate cannot.
    F4  total Gate-1 policy cost >= always-restart cost.
    F5  the gate collapses to a trivial always-restart policy (no INVESTIGATE
        decisions at all).
    F6  calibration does not transfer between eager and lazy -- i.e. a scale
        measured on one is systematically wrong on the other.

"Repeated" means: occurring on more than one distinct design point within the
family, not merely on one repetition.

F3 is expected to fire. It is written here, before the run, so that a failure
cannot afterwards be described as an anticipated limitation rather than a
falsification.

GROUND TRUTH
------------
Every case runs with `gate1_enabled=False`, so the full investigation happens
and `N`, `A`, `R` are measured. The oracle is
`INVESTIGATE iff A + min(R, N) < N`, computed only afterwards. The gate's
decision is replayed offline against the resulting trace and never influences
what was measured.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.common.local_llama import LocalLlamaClient, LocalLlamaSettings, warn_if_cpu
from src.eval.fanout_scenarios import FanoutScenario
from src.eval.real_llm import run_generated
from src.tracing.logger import read_trace
from src.tracing.tools import fanout_corpus

RUNS_DIR = Path("data/runs/gate1-real")
OUT = Path("data/results/gate1/real-falsification.json")
CALIBRATION_SOURCE = Path("data/results/fanout/fanout-campaign.json")


@dataclass
class RealCase:
    """One adversarial design point."""

    case_id: str
    family: str          # A..F
    family_name: str
    scenario: Any
    arm: str             # "eager" | "lazy"
    repeats: int


def case_matrix() -> list[RealCase]:
    """12-20 cases spanning qualitatively different economic regimes.

    Repeats are spent where the brief asks -- localized, wide-exposure,
    near-break-even and the eager/lazy comparison -- and not on the widespread
    family, where the answer is not close.
    """
    m: list[RealCase] = []

    # A. LOCALIZED: small region, large untouched remainder.
    m.append(RealCase("A-fan08p01-lazy", "A", "localized (moderate fan-out)",
                      FanoutScenario.build(8, poisoned_indices=(0,)), "lazy", 2))
    m.append(RealCase("A-fan16p01-lazy", "A", "localized (larger fan-out)",
                      FanoutScenario.build(16, poisoned_indices=(0,)), "lazy", 2))

    # B. WIDESPREAD: everything poisoned, restart should win.
    m.append(RealCase("B-fan08p08-lazy", "B", "widespread (all poisoned)",
                      FanoutScenario.build(8, poisoned_indices=tuple(range(8))),
                      "lazy", 2))

    # C. NEAR BREAK-EVEN: intermediate poison counts, chosen from the scripted
    #    sweep as the region where A/N + f sat closest to 1.
    m.append(RealCase("C-fan08p03-lazy", "C", "near break-even",
                      FanoutScenario.build(8, poisoned_indices=(0, 1, 2)),
                      "lazy", 2))
    m.append(RealCase("C-fan12p04-lazy", "C", "near break-even",
                      FanoutScenario.build(12, poisoned_indices=(0, 1, 2, 3)),
                      "lazy", 2))

    # D. WIDE EXPOSURE, NO INFLUENCE -- the pre-registered failure prediction.
    m.append(RealCase("D-share08ben-lazy", "D", "wide exposure, no influence",
                      FanoutScenario.build_shared(8, "benign"), "lazy", 2))
    m.append(RealCase("D-share16ben-lazy", "D", "wide exposure, no influence",
                      FanoutScenario.build_shared(16, "benign"), "lazy", 2))

    # E. REDUNDANCY: every analyst exposed, one materially affected.
    m.append(RealCase("E-share08tar-lazy", "E", "wide exposure, narrow influence",
                      FanoutScenario.build_shared(8, "targeted"), "lazy", 2))

    # F. EAGER vs LAZY, on designs that also appear in the lazy arm above.
    m.append(RealCase("F-fan08p01-eager", "F", "eager/lazy comparison",
                      FanoutScenario.build(8, poisoned_indices=(0,)), "eager", 1))
    m.append(RealCase("F-share08ben-eager", "F", "eager/lazy comparison",
                      FanoutScenario.build_shared(8, "benign"), "eager", 1))
    return m


def calibrate_from_history(arm: str) -> tuple[float, int, str]:
    """A_SCALE from the PRE-EXISTING campaign, never from this suite.

    Returns (scale, n_runs, note). Reports cold-start rather than silently
    borrowing a value when the arm has no history.
    """
    from src.recovery.gate1 import A_SCALE, calibrate

    if not CALIBRATION_SOURCE.exists():
        return A_SCALE, 0, "cold-start: no calibration history on disk"
    payload = json.loads(CALIBRATION_SOURCE.read_text(encoding="utf-8"))
    pairs = []
    for row in payload.get("results", []):
        if row.get("arm") != arm:
            continue
        path = Path(row.get("trace_path", ""))
        if not path.exists():
            continue
        trace = read_trace(path)
        if trace.analysis_tokens() > 0:
            pairs.append((trace, trace.analysis_tokens()))
    if not pairs:
        return A_SCALE, 0, f"cold-start: no {arm} history; frozen default used"
    return calibrate(pairs), len(pairs), f"calibrated on {len(pairs)} prior {arm} runs"


def _client(settings: LocalLlamaSettings) -> LocalLlamaClient:
    return LocalLlamaClient(settings=settings)


def run_case(case: RealCase, repeat: int, settings: LocalLlamaSettings,
             seed: int) -> dict[str, Any] | None:
    """One real run, with Gate 1 OFF so the oracle is measurable."""
    scenario = case.scenario
    workdir = RUNS_DIR / f"{case.case_id}-r{repeat}"
    started = time.time()
    try:
        result = run_generated(
            scenario, _client(settings), workdir=workdir,
            detector_name="oracle", seed=seed,
            replay_client_factory=lambda: _client(settings),
            lazy_self_report=(case.arm == "lazy"),
            gate1_enabled=False,          # THE ORACLE MUST NOT SEE THE GATE
        )
    except Exception as exc:  # noqa: BLE001
        return {"case_id": case.case_id, "repeat": repeat, "ok": False,
                "failure": f"{type(exc).__name__}: {exc}"}
    if not result.ok:
        return {"case_id": case.case_id, "repeat": repeat, "ok": False,
                "failure": result.failure}

    trace = read_trace(Path(result.trace_path))
    flagged = [s.id for s in trace.sources if s.malicious]
    mine = next((r for r in result.rows if r.method == "CausalLine"), None)
    if mine is None:
        return {"case_id": case.case_id, "repeat": repeat, "ok": False,
                "failure": "no CausalLine row"}

    n = trace.pipeline_tokens()
    a = trace.analysis_tokens()
    r = mine.replay_tokens
    return {
        "case_id": case.case_id,
        "family": case.family,
        "family_name": case.family_name,
        "arm": case.arm,
        "repeat": repeat,
        "workers": scenario.design.workers,
        "intent": scenario.design.intent,
        "ok": True,
        "trace_path": str(result.trace_path),
        "flagged": len(flagged),
        "events": len(trace.events),
        "N": n,
        "A": a,
        "R": r,
        "landed": bool(result.truth.payload_landed) if result.truth else False,
        "task_success": result.task_success,
        "escalated": bool(mine.escalations),
        "unsafe": mine.unsafe_preservations,
        "elapsed_s": round(time.time() - started, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--only", default=None, help="restrict to one family")
    args = parser.parse_args(argv)

    settings = LocalLlamaSettings(model=args.model)
    if not warn_if_cpu(settings):
        print("  REFUSING: not on the GPU. Restart Ollama while idle.")
        return 2

    matrix = case_matrix()
    if args.only:
        matrix = [c for c in matrix if c.family == args.only]
    total = sum(c.repeats for c in matrix)
    print(f"  {len(matrix)} design point(s), {total} real run(s), Gate 1 OFF\n",
          flush=True)

    rows: list[dict[str, Any]] = []
    done = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for case in matrix:
        for repeat in range(1, case.repeats + 1):
            done += 1
            row = run_case(case, repeat, settings, seed=20270201 + repeat)
            rows.append(row)
            if row.get("ok"):
                print(f"  [{done}/{total}] {row['case_id']}#r{repeat} "
                      f"N={row['N']} A={row['A']} R={row['R']} "
                      f"landed={row['landed']} in {row['elapsed_s']}s", flush=True)
            else:
                print(f"  [{done}/{total}] {row['case_id']}#r{repeat} FAILED: "
                      f"{row.get('failure')}", flush=True)
            args.out.write_text(json.dumps({
                "experiment": "gate1-real-falsification",
                "model": args.model,
                "rows": rows,
            }, indent=2), encoding="utf-8")
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
