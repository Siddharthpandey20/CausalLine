"""The frozen 56-agent validation: 5 seeds x 3 regimes, one process per run.

    python -m src.eval.mixed_validation --seeds 5
    python -m src.eval.mixed_validation --status

WHY ONE PROCESS PER RUN
-----------------------
Two reasons, both learned the hard way.

**Memory.** The single-process campaign was killed twice by the OS partway
through the large regime, losing everything after the last completed case. A
fresh process per (seed, regime) caps the loss at one run and returns all of
its memory afterwards.

**Accounting.** The budget ledger is cumulative within a process, so a
three-regime process reports a regime's usage plus everything before it. One
run per process makes each ledger exactly that run's external spend, which is
what `TASK 5` asks to record per run.

RESUMABLE, AND THAT IS NOT A CONVENIENCE
-----------------------------------------
Each run writes its own file and an existing file is skipped. A campaign that
dies at run 11 of 15 is restarted, not repeated -- so a partial validation is
reported as "11 of 15 completed", with the eleven unchanged, rather than
silently becoming a different experiment.

WHAT THE PER-PROCESS DESIGN COSTS, STATED RATHER THAN HIDDEN
--------------------------------------------------------------
A fresh process means a fresh budget ledger, so `mixed_campaign`'s self-imposed
ceiling (120 Gemini / 800 NVIDIA) now applies *per run* instead of per
campaign. It never binds -- a run spends 12-28 external calls -- but it is no
longer the thing protecting the day's quota. The real ceiling is the
provider's, which neither API exposes, and it will announce itself as 429s.
Those are retried once and then answered locally with the agent recorded in
`degraded`, so a run that hit an unknown limit is visible in the results rather
than silently different.

NOTHING HERE IS TUNED
---------------------
The topology, provider assignment, payloads, regime definitions, baselines,
CausalLine configuration, metrics and selection criteria are exactly those of
`mixed_campaign`. This module only decides *how many times* and *in what
order*, and records what happened.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REGIMES = ("small", "medium", "large")
# Arbitrary but fixed, and written down so the set is reproducible rather than
# whatever the clock happened to say.
SEEDS = (20260917, 20260918, 20260919, 20260920, 20260921)


def run_path(out_dir: Path, seed: int, regime: str) -> Path:
    return out_dir / f"seed{seed}-{regime}.json"


def completed(out_dir: Path, seeds, regimes) -> list[tuple[int, str]]:
    return [(s, r) for s in seeds for r in regimes
            if run_path(out_dir, s, r).exists()]


def one_run(seed: int, regime: str, out_dir: Path, workdir: Path,
            timeout_s: float, vary_placement: bool = False) -> dict[str, Any]:
    """One (seed, regime) in its own interpreter."""
    target = run_path(out_dir, seed, regime)
    command = [
        sys.executable, "-m", "src.eval.mixed_campaign",
        "--regimes", regime,
        "--seed", str(seed),
        "--workdir", str(workdir / f"seed{seed}"),
        "--out", str(target),
    ] + (["--vary-placement"] if vary_placement else [])
    started = time.time()
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout_s)
        status = "ok" if proc.returncode == 0 else f"exit{proc.returncode}"
        tail = (proc.stdout or "")[-400:] + (proc.stderr or "")[-400:]
    except subprocess.TimeoutExpired:
        status, tail = "timeout", ""
    except Exception as exc:  # noqa: BLE001
        status, tail = type(exc).__name__, str(exc)[:400]
    return {"seed": seed, "regime": regime, "status": status,
            "wall_clock_s": round(time.time() - started, 1),
            "produced": target.exists(), "tail": tail.strip()[-400:]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=5,
                        help="how many of the fixed seed list to use")
    parser.add_argument("--regimes", default=",".join(REGIMES))
    parser.add_argument("--vary-placement", action="store_true",
                        help="draw each run's attack placement from its seed "
                             "(the workload-varied experiment). Without it the "
                             "frozen placement of the completed 15-run "
                             "validation is reproduced.")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--timeout", type=float, default=5400.0)
    parser.add_argument("--status", action="store_true",
                        help="report what is done and exit; runs nothing")
    args = parser.parse_args()

    # SEPARATE DIRECTORIES, NOT A FLAG IN A SHARED ONE.
    # The frozen validation and the workload-varied experiment are different
    # experiments and must never be aggregated into one mean. Different
    # default destinations make merging them an act rather than an accident.
    default = ("mixed56-workload-varied" if args.vary_placement
               else "mixed56-validation")
    out_dir = Path(args.out_dir or f"data/results/{default}")
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(args.workdir or f"data/runs/{default}")
    seeds = SEEDS[:args.seeds]
    regimes = [r.strip() for r in args.regimes.split(",") if r.strip()]
    planned = [(s, r) for s in seeds for r in regimes]

    done = completed(out_dir, seeds, regimes)
    if args.status:
        print(f"{len(done)} of {len(planned)} runs complete")
        for seed, regime in planned:
            mark = "done" if (seed, regime) in done else "PENDING"
            print(f"  seed {seed}  {regime:<7} {mark}")
        return

    print(f"planned {len(planned)} runs ({len(seeds)} seeds x "
          f"{len(regimes)} regimes); {len(done)} already on disk")
    log: list[dict[str, Any]] = []
    for seed, regime in planned:
        if (seed, regime) in done:
            print(f"  seed {seed} {regime:<7} SKIP (already on disk)",
                  flush=True)
            continue
        print(f"  seed {seed} {regime:<7} running...", end="", flush=True)
        outcome = one_run(seed, regime, out_dir, workdir, args.timeout,
                          vary_placement=args.vary_placement)
        log.append(outcome)
        print(f" {outcome['status']} in {outcome['wall_clock_s']}s",
              flush=True)
        (out_dir / "driver-log.json").write_text(
            json.dumps(log, indent=2), encoding="utf-8")

    done = completed(out_dir, seeds, regimes)
    print(f"\n{len(done)} of {len(planned)} runs complete")
    if len(done) < len(planned):
        missing = [f"seed{s}-{r}" for s, r in planned if (s, r) not in done]
        print("NOT COMPLETED:", ", ".join(missing))


if __name__ == "__main__":
    main()
