"""One entry point for running CausalLine's real-model evaluation elsewhere.

    # the established 56-agent experiment, entirely on local Ollama
    python -m src.eval.college_runner --mode baseline56 --local-only --seeds 3

    # the same pipeline at a size the machine can afford
    python -m src.eval.college_runner --mode production_network \\
        --num-agents 40 --local-only --seeds 3

    python -m src.eval.college_runner --mode baseline56 --smoke   # ~2 min
    python -m src.eval.college_runner --status                    # what is done

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
It is a front end. The pipeline, the attack injection, the provenance, the
baselines, the recovery and the metrics are `src/eval/mixed_campaign.py` and
`src/eval/real_llm.py` unchanged -- this module chooses a topology, a provider
set and a seed list, then calls `run_regime`. There is no second recovery
implementation here and there must never be one: a number produced through this
runner has to be comparable with `docs/14-workload-varied-validation.md`, and
it only is if the code underneath is the same code.

TWO MODES
---------
`baseline56`          `Topology()` -- the published 56-agent shape (acquisition
                      12, normalisers 10, hub 1, specialists 12, verifiers 10,
                      reviewers 8, synth/audit/executor 3). With
                      `--vary-placement` this reproduces the primary result.

`production_network`  the same hierarchical manager-worker / fan-out-fan-in
                      shape scaled to `--num-agents`, or set stage by stage.
                      Shared memory and inter-agent messaging are properties of
                      the pipeline, so all three attack channels work at every
                      size.

ONE PROCESS PER RUN
-------------------
Each (seed, regime) runs in a fresh interpreter, for the reason
`mixed_validation` gives: the single-process campaign was OOM-killed twice on
the development machine, and a college machine is likelier to be tighter, not
roomier. An existing result file is skipped, so an interrupted campaign is
continued rather than repeated.

LOCAL-ONLY IS THE DEFAULT ASSUMPTION
------------------------------------
Missing API keys are not an error. `--local-only` builds the local client alone;
without it, Gemini and NVIDIA are used for the six agents the published
routing assigns to them, and a missing key is reported before anything is spent
rather than as a crash halfway through.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from src.tracing.mixed import Topology

REGIMES = ("small", "medium", "large")
# The same fixed seed list `mixed_validation` uses, extended on demand so a
# 3-seed college run is a prefix of the published 5-seed one rather than a
# different sample.
BASE_SEEDS = (20260917, 20260918, 20260919, 20260920, 20260921)
# The published shape, used as the proportions to scale from.
REFERENCE = Topology()


def seeds_for(n: int) -> list[int]:
    out = list(BASE_SEEDS[:n])
    while len(out) < n:
        out.append(BASE_SEEDS[0] + len(out))
    return out


def shape_for(n_agents: int) -> Topology:
    """A valid topology of about `n_agents`, in the published proportions.

    `Topology.problems()` is the authority on validity -- specialists must
    equal acquisition, and each downstream stage must not exceed the one above
    it -- so this scales and then repairs, and the caller is told the agent
    count it actually got rather than the one it asked for.
    """
    fixed = 1 + 3  # hub + synth/audit/executor
    ratio_norm = REFERENCE.normalisers / REFERENCE.acquisition
    ratio_ver = REFERENCE.verifiers / REFERENCE.specialists
    ratio_rev = REFERENCE.reviewers / REFERENCE.verifiers

    best: Topology | None = None
    for acquisition in range(1, max(2, n_agents)):
        normalisers = max(1, min(acquisition, round(acquisition * ratio_norm)))
        specialists = acquisition
        verifiers = max(1, min(specialists, round(specialists * ratio_ver)))
        reviewers = max(1, min(verifiers, round(verifiers * ratio_rev)))
        topo = Topology(acquisition=acquisition, normalisers=normalisers,
                        specialists=specialists, verifiers=verifiers,
                        reviewers=reviewers)
        if topo.problems():
            continue
        if best is None or abs(topo.agent_count - n_agents) < abs(
                best.agent_count - n_agents):
            best = topo
        if topo.agent_count >= n_agents + fixed:
            break
    return best or Topology(acquisition=1, normalisers=1, specialists=1,
                            verifiers=1, reviewers=1)


def topology_from_args(args: argparse.Namespace) -> Topology:
    if args.mode == "baseline56":
        topo = Topology()
    else:
        stages = ("acquisition", "normalisers", "specialists", "verifiers",
                  "reviewers")
        explicit = {s: getattr(args, s) for s in stages
                    if getattr(args, s) is not None}
        topo = shape_for(args.num_agents) if args.num_agents else Topology()
        if explicit:
            spec = topo.to_dict()
            spec.update(explicit)
            topo = Topology.from_dict(spec)
    if args.local_only:
        # Provider assignment is meaningless with one provider, and leaving it
        # populated would put "gemini" in the fingerprint of a local run.
        topo = Topology(acquisition=topo.acquisition,
                        normalisers=topo.normalisers,
                        specialists=topo.specialists,
                        verifiers=topo.verifiers,
                        reviewers=topo.reviewers,
                        gemini_agents=(), nvidia_agents=())
    return topo


def result_path(out_dir: Path, seed: int, regime: str) -> Path:
    return out_dir / f"seed{seed}-{regime}.json"


# --- the worker: one (seed, regime) in its own process -----------------------

def worker(args: argparse.Namespace) -> int:
    from src.eval.mixed_campaign import build_ledgers, run_regime

    topo = topology_from_args(args)
    problems = topo.problems()
    if problems:
        print(json.dumps({"ok": False, "stage": "topology",
                          "problems": problems}))
        return 2
    ledgers = build_ledgers(gemini_cap=args.gemini_cap,
                            nvidia_cap=args.nvidia_cap)
    payload = run_regime(args.regime, Path(args.workdir), args.seed, ledgers,
                         vary_placement=args.vary_placement,
                         topology=topo, local_only=args.local_only)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    ok = bool(payload.get("ok", True))
    print(f"{'ok' if ok else 'FAILED'}: {args.regime} seed {args.seed} "
          f"-> {target}")
    return 0 if ok else 1


def spawn(args: argparse.Namespace, seed: int, regime: str,
          out_dir: Path, workdir: Path) -> dict[str, Any]:
    target = result_path(out_dir, seed, regime)
    command = [
        sys.executable, "-m", "src.eval.college_runner", "--_worker",
        "--mode", args.mode, "--regime", regime, "--seed", str(seed),
        "--workdir", str(workdir / f"seed{seed}"), "--out", str(target),
        "--gemini-cap", str(args.gemini_cap),
        "--nvidia-cap", str(args.nvidia_cap),
    ]
    if args.local_only:
        command.append("--local-only")
    if args.vary_placement:
        command.append("--vary-placement")
    if args.num_agents:
        command += ["--num-agents", str(args.num_agents)]
    for stage in ("acquisition", "normalisers", "specialists", "verifiers",
                  "reviewers"):
        value = getattr(args, stage)
        if value is not None:
            command += [f"--{stage}", str(value)]

    started = time.time()
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=args.timeout)
        status = "ok" if proc.returncode == 0 else f"exit{proc.returncode}"
        tail = ((proc.stdout or "")[-400:] + (proc.stderr or "")[-400:]).strip()
    except subprocess.TimeoutExpired:
        status, tail = "timeout", f"exceeded --timeout {args.timeout}s"
    except Exception as exc:  # noqa: BLE001
        status, tail = type(exc).__name__, str(exc)[:400]
    return {"seed": seed, "regime": regime, "status": status,
            "produced": target.exists(),
            "wall_clock_s": round(time.time() - started, 1),
            "tail": tail[-400:]}


# --- the smoke test ---------------------------------------------------------

def smoke(args: argparse.Namespace) -> int:
    """Nine agents, every channel, one regime. Run this first, always."""
    from src.eval.mixed_campaign import build_ledgers, run_regime

    topo = Topology(acquisition=3, normalisers=2, specialists=3, verifiers=2,
                    reviewers=2,
                    gemini_agents=() if args.local_only else ("hub",),
                    nvidia_agents=() if args.local_only else ("spec1",))
    print(f"smoke: {topo.agent_count} agents, "
          f"{'local only' if args.local_only else 'mixed providers'}")
    started = time.time()
    # `vary_placement=True` is not optional here. The frozen placement is the
    # published 56-agent one -- it names `ver7`, which does not exist in a
    # 2-verifier topology -- and `_draw_placement` is the only path that clamps
    # the placement to the shape it is given.
    payload = run_regime("small", Path(args.workdir) / "smoke", seeds_for(1)[0],
                         build_ledgers(gemini_cap=args.gemini_cap,
                                       nvidia_cap=args.nvidia_cap),
                         vary_placement=True,
                         topology=topo, local_only=args.local_only)
    ok = bool(payload.get("ok", False))
    # `payload_landed` is nested under `truth`, not top level -- reading it
    # from the top prints None on a run where the attack plainly landed.
    truth = payload.get("truth") or {}
    landed = truth.get("payload_landed")
    task_success = payload.get("task_success")
    print(f"  ok={ok}  events={payload.get('events')}  "
          f"sources={payload.get('sources')}  "
          f"payload_landed={landed}  task_success={task_success}  "
          f"{time.time() - started:.0f}s")
    if payload.get("degraded"):
        print(f"  degraded agents (answered locally after a provider "
              f"failure): {payload['degraded']}")
    if not ok:
        print(f"  failure: {payload.get('failure') or payload.get('problems')}")
        print("  nothing was wasted: fix this before the full run.")
        return 1
    # The smoke test runs the ATTACKED original. A landed payload means the
    # injection worked, so a false task check is the attack succeeding -- which
    # is the thing recovery then has to repair. Said out loud because
    # "task_success=False" reads like a broken setup and is the opposite.
    if landed and not task_success:
        print("  the attack landed and corrupted the final answer. That is the "
              "intended\n  starting condition, not a fault: it is what "
              "recovery is measured against.")
    elif landed is False and task_success:
        print("  the model ignored the injection on this run. Nothing was "
              "contaminated,\n  so there is nothing to preserve unsafely and "
              "the run says little.")
    print("  wiring is good. Now the campaign (expect hours on CPU):")
    print(f"    python -m src.eval.college_runner --mode {args.mode} "
          f"{'--local-only ' if args.local_only else ''}--seeds 3 "
          f"--vary-placement")
    return 0


# --- driver -----------------------------------------------------------------

def drive(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir or f"data/results/college-{args.mode}")
    workdir = Path(args.workdir or f"data/runs/college-{args.mode}")
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)

    topo = topology_from_args(args)
    problems = topo.problems()
    if problems:
        print("topology is not runnable, nothing spent:")
        for p in problems:
            print(f"  - {p}")
        return 2

    # The frozen placement is the published 56-agent one and names agents by
    # index (`ver7`, normaliser 4, record 8). Only `_draw_placement`, reached
    # with --vary-placement, clamps a placement to the topology it is given, so
    # any other shape needs it. Refused here rather than per-run, so a 9-run
    # campaign does not fail nine times for one reason.
    if topo.agent_count != Topology().agent_count and not args.vary_placement:
        print(f"this topology has {topo.agent_count} agents, not "
              f"{Topology().agent_count}, so the frozen attack placement does "
              f"not fit it.\nAdd --vary-placement (which draws the placement "
              f"from the seed, clamped to the shape).\nNothing was spent.")
        return 2

    regimes = [r.strip() for r in args.regimes.split(",") if r.strip()]
    bad = [r for r in regimes if r not in REGIMES]
    if bad:
        print(f"unknown regime(s) {bad}; choose from {', '.join(REGIMES)}")
        return 2
    seeds = seeds_for(args.seeds)
    planned = [(s, r) for s in seeds for r in regimes]
    done = [(s, r) for s, r in planned if result_path(out_dir, s, r).exists()]
    todo = [(s, r) for s, r in planned if (s, r) not in done]

    print(f"mode {args.mode}: {topo.agent_count} agents "
          f"({topo.acquisition}/{topo.normalisers}/1/{topo.specialists}/"
          f"{topo.verifiers}/{topo.reviewers}/3)")
    print(f"providers: {'local only' if args.local_only else 'local + gemini + nvidia'}")
    print(f"regimes {regimes}  seeds {seeds}")
    print(f"{len(planned)} runs planned, {len(done)} already on disk, "
          f"{len(todo)} to run")
    print(f"results -> {out_dir}")
    if args.status:
        return 0
    if not todo:
        print("nothing to do.")
        return 0
    if args.dry_run:
        print("--dry-run: stopping before the first run.")
        return 0

    log: list[dict[str, Any]] = []
    for i, (seed, regime) in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] seed {seed} / {regime}", flush=True)
        row = spawn(args, seed, regime, out_dir, workdir)
        log.append(row)
        print(f"    {row['status']}  {row['wall_clock_s']}s"
              f"  produced={row['produced']}")
        if row["status"] != "ok" and row["tail"]:
            print(f"    {row['tail'][:300]}")

    (out_dir / "driver-log.json").write_text(
        json.dumps({"mode": args.mode, "topology": topo.to_dict(),
                    "local_only": args.local_only, "runs": log}, indent=2),
        encoding="utf-8")
    produced = sum(1 for r in log if r["produced"])
    print(f"\n{produced} of {len(todo)} runs produced a result file "
          f"({len(done)} were already there).")
    if produced < len(todo):
        print("Incomplete. Re-run the same command to continue; finished runs "
              "are skipped.")
    print(f"\nReport:  python -m src.eval.mixed_validation_report "
          f"--dir {out_dir}")
    return 0 if produced == len(todo) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", default="baseline56",
                        choices=("baseline56", "production_network"))
    parser.add_argument("--seeds", type=int, default=3,
                        help="how many seeds, taken from the fixed list")
    parser.add_argument("--regimes", default=",".join(REGIMES))
    parser.add_argument("--local-only", action="store_true",
                        help="use only the local Ollama model; needs no key")
    parser.add_argument("--vary-placement", action="store_true",
                        help="draw the attack placement from the seed "
                             "(the primary result's setting)")
    parser.add_argument("--num-agents", type=int, default=0,
                        help="production_network: target agent count, scaled "
                             "in the published proportions")
    for stage in ("acquisition", "normalisers", "specialists", "verifiers",
                  "reviewers"):
        parser.add_argument(f"--{stage}", type=int, default=None,
                            help=f"production_network: {stage} count")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--timeout", type=float, default=7200.0,
                        help="per-run ceiling in seconds; CPU inference is slow")
    parser.add_argument("--gemini-cap", type=int, default=120)
    parser.add_argument("--nvidia-cap", type=int, default=800)
    parser.add_argument("--smoke", action="store_true",
                        help="nine agents, one regime; run this first")
    parser.add_argument("--status", action="store_true",
                        help="report what is on disk and stop")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and stop")
    # Internal: one (seed, regime) in this process. Not for direct use.
    parser.add_argument("--_worker", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--regime", default="small", help=argparse.SUPPRESS)
    parser.add_argument("--seed", type=int, default=BASE_SEEDS[0],
                        help=argparse.SUPPRESS)
    parser.add_argument("--out", default="", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args._worker:
        return worker(args)
    if args.smoke:
        args.workdir = args.workdir or f"data/runs/college-{args.mode}"
        return smoke(args)
    return drive(args)


if __name__ == "__main__":
    sys.exit(main())
