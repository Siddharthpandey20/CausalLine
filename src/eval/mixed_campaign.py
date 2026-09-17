"""Run the 56-agent, three-provider experiment: smoke test, then three regimes.

    python -m src.eval.mixed_campaign --smoke
    python -m src.eval.mixed_campaign --regimes small,medium,large

WHAT THIS FILE IS RESPONSIBLE FOR, AND WHAT IT IS NOT
------------------------------------------------------
It builds the clients, the budget ledgers and the router, and it hands each
scenario to `real_llm.run_generated` -- the SAME entry point the hosted, local
and fan-out frontiers use. Detector, refinement, contamination walk, planner,
selective replay, verification, the three baselines and every metric are
reached through that call and are not re-implemented here. A campaign runner
that re-derived any of them would be measuring a second copy of the system.

THE BUDGET IS SELF-IMPOSED, AND SAYING SO IS THE POINT
--------------------------------------------------------
Verified on 17-09-2026 with three discovery calls: **neither provider returns a
quota or rate-limit header**. Gemini's `models.list` and `generateContent`
responses and NVIDIA's `chat/completions` response carry none, so remaining
quota cannot be read programmatically.

The ceilings below are therefore an experiment budget this file imposes on
itself, not a limit read from an API, and every number reported says which it
is. A call that would exceed one is refused before it is made, and a refused
external call falls back to the local model with the agent recorded in
`degraded` -- never silently substituted, because D-058 exists precisely
because a silent substitution makes a result uninterpretable.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from src.common.local_llama import LocalLlamaClient, LocalLlamaSettings
from src.eval.mixed_scenarios import MixedScenario
from src.eval.provider_budget import (
    BudgetLedger,
    ProviderLimit,
    RoutedClient,
    gemini_client,
    load_env,
    nvidia_client,
)
from src.tracing.mixed import Topology

GEMINI_MODEL = "gemini-3.8-flash"
# PINNED, so the trace records the model that actually answered rather than
# whatever `local_llama.DEFAULT_MODEL` resolves to (D-057).
#
# WHY THE 8B AND NOT THE 3B THIS PROJECT USUALLY MEASURES ON
# -----------------------------------------------------------
# `llama3.2:3b` is what `docs/local_llm_frontier` used and it is 100% on GPU
# here, so it was the obvious choice and it was tried first. It cannot do this
# workflow. Measured on a full 56-agent run: two of twelve specialists mangled
# their code while searching the roster (`YS657` -> `GY657`, `NR712` -> `NR44`),
# and the end-to-end check needs all twelve exactly.
#
# The consequence is not a slightly worse score, it is a dead experiment.
# `verify()` is the only consumer of task success and only CausalLine verifies,
# so a copy error the baselines are never charged for makes CausalLine escalate
# to `restart_all` at 0% preserved while B1 and B2 keep 92.4% by never
# checking. That is `docs/03` #15 for the fourth time, and D-090 already
# established its cause: the model cannot do the underlying work, so the
# measurement lands on its capability instead of on contamination.
#
# `llama3:latest` (8B, 92% on GPU here) completed the same task. It is still a
# local, self-hosted LLaMA; it is simply one that can hold twelve records
# across seven hops.
LOCAL_MODEL = "llama3:latest"
# The registry HANDLE, not the model id: `src/common/nvidia.py` owns the
# mapping, the availability check and the per-model `extra_body`.
NVIDIA_HANDLE = "nemotron"

# Self-imposed, NOT provider-reported. The RPM figures are set well under the
# most restrictive documented free-tier numbers so the experiment never depends
# on having guessed a real limit correctly; the request ceilings are what this
# experiment is willing to spend.
LIMIT_SOURCE = (
    "self-imposed experiment budget; verified 17-09-2026 that neither "
    "provider returns a quota or rate-limit header, so no ceiling here was "
    "read from an API"
)


def build_ledgers(gemini_cap: int = 120,
                  nvidia_cap: int = 800) -> dict[str, BudgetLedger]:
    """One set of ledgers for the WHOLE campaign, not one per regime.

    Built once and shared, because the thing being budgeted is a day's quota
    at a provider, not a case. A fresh ledger per regime would let three cases
    each spend the full ceiling and report that none of them exceeded it.
    """
    return {
        # NO `rpm` HERE ON PURPOSE. `GeminiClient` and `NVIDIAClient` each
        # carry their own `RateLimiter`, tuned in `.env`
        # (GEMINI_REQUESTS_PER_MINUTE=15 -> 4.0s spacing; NVIDIA 40 -> 1.5s).
        # A second limiter stacked on top does not make the experiment safer,
        # it just makes every external call wait twice and turns a measured
        # wall-clock into an artefact of this file. The ceiling below is the
        # thing this module adds that neither client has.
        "gemini": BudgetLedger(ProviderLimit(
            "gemini", rpd=gemini_cap, source=LIMIT_SOURCE)),
        "nvidia": BudgetLedger(ProviderLimit(
            "nvidia", rpd=nvidia_cap, source=LIMIT_SOURCE)),
        "local": BudgetLedger(ProviderLimit(
            "local", source="no external quota; local Ollama on this machine")),
    }


def build_router(topology: Topology,
                 ledgers: dict[str, BudgetLedger] | None = None) -> RoutedClient:
    """One router over local LLaMA, Gemini and NVIDIA."""
    load_env()
    ledgers = ledgers if ledgers is not None else build_ledgers()
    local = LocalLlamaClient(LocalLlamaSettings(model=LOCAL_MODEL,
                                               max_output_tokens=300))
    return RoutedClient(
        clients={
            "local": local,
            "gemini": gemini_client(GEMINI_MODEL, ledgers["gemini"]),
            "nvidia": nvidia_client(NVIDIA_HANDLE, ledgers["nvidia"]),
        },
        routing=topology.routing(),
        default="local",
    )


def smoke(workdir: Path) -> dict[str, Any]:
    """A 9-agent version of the real thing: every provider, every channel.

    Run before the full experiment so a wiring fault costs one minute of local
    inference and two external calls instead of an hour and a full budget. It
    exercises exactly the paths the big run does -- routing, provenance,
    attribution, the exact check -- at a size where a failure is readable.
    """
    topo = Topology(acquisition=3, normalisers=2, specialists=3, verifiers=2,
                    reviewers=2, gemini_agents=("hub",),
                    nvidia_agents=("spec1",))
    ledgers = build_ledgers()
    router = build_router(topo, ledgers)
    scenario = MixedScenario.build("medium", topology=topo,
                                   test_id="smoke-mixed")
    # `medium` poisons documents 1, 4 and 8; at three documents only index 1
    # is in range, so the placement is narrowed -- and the annotation is
    # recomputed rather than patched, so the scenario cannot describe a
    # placement it does not have.
    scenario.poisoned_docs = (1,)
    exposure, influence = MixedScenario._expected(
        topo, scenario.poisoned_docs, (), (), influencing=True)
    scenario.annotation.expected_exposure = exposure
    scenario.annotation.expected_influence = influence
    problems = scenario.validate()
    if problems:
        return {"ok": False, "stage": "validate", "problems": problems}

    from src.eval.real_llm import run_generated

    started = time.time()
    result = run_generated(scenario, router, workdir / "smoke",
                           detector_name="oracle", seed=20260917)
    return {
        "ok": bool(result.ok),
        "failure": result.failure,
        "agents": topo.agent_count,
        "task_success": result.task_success,
        "events": getattr(result, "events", None),
        "payload_landed": getattr(result, "payload_landed", None),
        "routing": dict(router.per_provider_calls),
        "degraded": list(router.degraded),
        "ledgers": {k: v.to_dict() for k, v in ledgers.items()},
        "wall_clock_s": round(time.time() - started, 1),
    }


def run_regime(regime: str, workdir: Path, seed: int,
               ledgers: dict[str, BudgetLedger],
               vary_placement: bool = False) -> dict[str, Any]:
    """One contamination regime, end to end, through the shared harness.

    `vary_placement=False` reproduces the FROZEN placement that the completed
    15-run validation was measured on, so that experiment stays runnable from
    this code. `True` draws the placement from `seed` instead, which is what
    the workload-varied campaign needs -- see `docs/13-mixed56-validation.md`
    Sec 9 Limitation 1 for why repeating a fixed workload was not enough.

    Opt-in rather than default, deliberately: the unflagged command must keep
    reproducing the published experiment.
    """
    from src.eval.real_llm import run_generated

    topo = Topology()
    router = build_router(topo, ledgers)
    scenario = MixedScenario.build(
        regime, topology=topo, seed=seed if vary_placement else None)
    problems = scenario.validate()
    if problems:
        return {"regime": regime, "ok": False, "stage": "validate",
                "problems": problems}

    started = time.time()
    result = run_generated(scenario, router, workdir / regime,
                           detector_name="oracle", seed=seed)
    payload = result.to_dict() if hasattr(result, "to_dict") else {}
    payload.update({
        "regime": regime,
        "placement": scenario.placement(),
        "band": list(scenario.band()),
        "vary_placement": bool(vary_placement),
        "scenario": scenario.to_dict(),
        "agents": topo.agent_count,
        "routing": dict(router.per_provider_calls),
        "degraded": list(router.degraded),
        "ledgers": {k: v.to_dict() for k, v in ledgers.items()},
        "wall_clock_s": round(time.time() - started, 1),
    })
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--regimes", default="")
    parser.add_argument("--workdir", default="data/runs/mixed56")
    parser.add_argument("--out", default="data/results/mixed56.json")
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--vary-placement", action="store_true",
                        help="draw the attack placement from --seed "
                             "instead of using the frozen placement")
    parser.add_argument("--gemini-cap", type=int, default=120)
    parser.add_argument("--nvidia-cap", type=int, default=800)
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if args.smoke:
        report = smoke(workdir)
        print(json.dumps(report, indent=2, default=str))
        raise SystemExit(0 if report.get("ok") else 1)

    regimes = [r.strip() for r in args.regimes.split(",") if r.strip()]
    if not regimes:
        raise SystemExit("nothing to do: pass --smoke or --regimes")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ledgers = build_ledgers(args.gemini_cap, args.nvidia_cap)
    results: list[dict[str, Any]] = []
    for regime in regimes:
        print(f"=== {regime} ===", flush=True)
        row = run_regime(regime, workdir, args.seed, ledgers,
                         vary_placement=args.vary_placement)
        results.append(row)
        print(f"  ok={row.get('ok')} task_success={row.get('task_success')} "
              f"{row.get('wall_clock_s')}s routing={row.get('routing')}",
              flush=True)
        # Written after every regime, not at the end: a campaign that dies on
        # the third case must not take the first two down with it. Same rule
        # the trace logger follows (D-008).
        out.write_text(json.dumps({"results": results}, indent=2, default=str),
                       encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
