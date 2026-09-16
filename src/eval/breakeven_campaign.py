"""Prove -- or refute -- the break-even projection, by measuring it.

WHAT THIS ANSWERS
-----------------
`docs/local_llm_frontier/03` §3 projected that deferring the self-report
(D-082) drops `A/N + f` from 1.58 to **0.92**, the first time this project has
had a number under 1.00. That projection was arithmetic on runs collected with
self-report still switched on: take the measured self-report cost out of `A` and
see where the ratio lands. Nothing about it had been run.

This runs it. Two arms over the same design points, same seeds, same model:

    eager   self-report inline on every model event, as every campaign so far
    lazy    self-report deferred to the investigation, asked once per event the
            frontier actually reaches (D-082 / D-089)

The arms differ in one flag. The SPRT change (D-087) is in *both*, so it is
held constant and cannot be mistaken for the lever under test.

WHY TWO ARMS AND NOT ONE
------------------------
A lazy-only campaign would give a number with nothing to compare it to except
the old campaign, which ran on different traces, a different SPRT config and a
different day. Pairing the arms on (scenario, repeat) makes the comparison the
one the question actually asks -- the same run with and without the lever --
and lets the sign test do its work.

    python -m src.eval.breakeven_campaign --repeats 5
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from src.common.local_llama import LocalLlamaSettings, placement, warn_if_cpu
from src.eval.local_campaign import (
    DEFAULT_SUITE,
    RUNS_DIR,
    _one,
    c_safe_from_sweep,
    load_suite,
)

OUT_DIR = Path("data/results/local_llama")
ARMS = ("eager", "lazy")


def run(
    scenarios: list,
    model: str,
    detector: str,
    repeats: int,
    concurrency: int,
    out: Path,
    placement_note: str,
) -> list[dict[str, Any]]:
    """Every (arm, scenario, repeat). Results flush after every single run."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = LocalLlamaSettings(model=model)

    jobs = [
        (arm, scenario, repeat)
        for repeat in range(1, repeats + 1)
        for scenario in scenarios
        for arm in ARMS
    ]
    print(f"  {len(jobs)} run(s) = {len(ARMS)} arm(s) x {len(scenarios)} design "
          f"point(s) x {repeats} repetition(s), {concurrency} in flight",
          flush=True)
    print(flush=True)

    results: list[dict[str, Any]] = []
    done = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(_one, scenario, settings, detector, repeat,
                        arm == "lazy", arm): (arm, scenario, repeat)
            for arm, scenario, repeat in jobs
        }
        for future in as_completed(futures):
            done += 1
            result, elapsed, calls, label = future.result()
            if result is None:
                print(f"  [{done}/{len(jobs)}] {label}", flush=True)
                continue
            arm, scenario, repeat = futures[future]
            print(
                f"  [{done}/{len(jobs)}] {label} "
                f"{scenario.design.channel}/{scenario.design.intent}/"
                f"{scenario.design.workflow}: "
                f"{'ok' if result.ok else 'VOID'} in {elapsed:.0f}s, "
                f"landed={result.truth.payload_landed if result.truth else 'n/a'}, "
                f"A={result.analysis_tokens}, sr={result.self_report_calls}, "
                f"sprt={result.sprt_decision or '-'}, {calls} call(s)",
                flush=True,
            )
            if not result.ok and result.failure:
                print(f"      reason: {result.failure[:200]}", flush=True)
            # A swallowed refinement error once produced analysis_tokens=0 and
            # a run that looked fine. Every note is printed, always.
            for note in result.notes:
                print(f"      note: {note[:200]}", flush=True)

            row = result.to_dict()
            row["arm"] = arm
            row["repeat"] = repeat
            results.append(row)
            out.write_text(
                json.dumps(
                    {
                        "frontier": "local-llama",
                        "experiment": "breakeven",
                        "placement": placement_note,
                        "arms": list(ARMS),
                        "results": results,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    print(f"{chr(10)}wall clock: {time.time() - started:.0f}s, "
          f"concurrency {concurrency}", flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--detector", default="oracle")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path,
                        default=OUT_DIR / "breakeven-campaign.json")
    args = parser.parse_args(argv)

    # GPU FIRST, ALWAYS. A campaign that silently runs on CPU is one nobody
    # notices until the throughput is explained afterwards, and this one costs
    # hours. Refuse before spending a token, not after.
    # `warn_if_cpu` loads the model first. `placement` alone cannot tell "on
    # CPU" from "idle-unloaded", and Ollama unloads after a few minutes idle --
    # so checking without warming refuses every campaign started after a pause.
    settings = LocalLlamaSettings(model=args.model)
    on_gpu = warn_if_cpu(settings)
    _, detail = placement(settings)
    if not on_gpu:
        print("  REFUSING: the model is not on the GPU. Restart Ollama while "
              "idle and re-run.", flush=True)
        return 2

    scenarios = load_suite(args.suite, limit=args.limit)
    concurrency = args.concurrency or c_safe_from_sweep(args.model, on_gpu)
    run(scenarios, args.model, args.detector, args.repeats, concurrency,
        args.out, detail)
    print(f"results written to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
