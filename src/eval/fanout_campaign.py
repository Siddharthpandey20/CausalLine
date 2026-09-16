"""The localized-contamination campaign, on the local GPU.

FOUR DESIGN POINTS, CHOSEN TO ANSWER FOUR DIFFERENT QUESTIONS
--------------------------------------------------------------
    fan04-inf   K=4    does selective recovery get DELIVERED at all, or does
                       it escalate the way it did on every chain run?
    fan08-inf   K=8    the middle of the range, where f is already small
    fan16-inf   K=16   a genuinely larger agentic system (50 events) with a
                       small contaminated region: the regime the method was
                       designed for and has never been run in
    fan08-exp   K=8    the control. Present, flagged, instructing nothing. If
                       this lands, the canary is not measuring what we think.

TWO ARMS, DIFFERING IN ONE FLAG
-------------------------------
    eager   self-report inline on every model event, as every campaign so far
    lazy    self-report deferred to the investigation (D-082/D-089)

`docs/local_llm_frontier/03` projected that the lazy lever alone takes
`A/N + f` from 1.58 to 0.92. That was arithmetic on runs collected with the
lever off. Running both arms over the same design points, same seeds, same
model, measures it instead of projecting it -- and pairs the two so the
comparison is the same run with and without the lever.

The SPRT change (D-087) is in BOTH arms, held constant, so it cannot be
mistaken for the lever under test.

    python -m src.eval.fanout_campaign --repeats 2
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from src.common.local_llama import LocalLlamaClient, LocalLlamaSettings, placement, warn_if_cpu
from src.eval.fanout_scenarios import FanoutScenario
from src.eval.real_llm import run_generated

RUNS_DIR = Path("data/runs/fanout")
OUT_DIR = Path("data/results/fanout")
ARMS = ("eager", "lazy")


def design_points() -> list[FanoutScenario]:
    return [
        FanoutScenario.build(4, "influencing"),
        FanoutScenario.build(8, "influencing"),
        FanoutScenario.build(16, "influencing"),
        FanoutScenario.build(8, "exposed_only"),
    ]


def _one(
    scenario: FanoutScenario,
    settings: LocalLlamaSettings,
    repeat: int,
    arm: str,
    detector: str = "oracle",
) -> tuple[Any, float, int, str]:
    label = f"{scenario.test_id}#r{repeat}[{arm}]"
    workdir = RUNS_DIR / f"{scenario.test_id}-r{repeat}-{arm}"
    client = LocalLlamaClient(settings=settings)
    started = time.time()
    try:
        result = run_generated(
            scenario,
            client,
            workdir=workdir,
            detector_name=detector,
            # A different seed per repeat: the backend is non-deterministic
            # even at temperature 0 with a fixed seed, so repetitions measure
            # something real, but identical seeds would still be the closest
            # thing to the same run twice.
            seed=20260916 + repeat,
            replay_client_factory=lambda: LocalLlamaClient(settings=settings),
            lazy_self_report=(arm == "lazy"),
        )
    except Exception as exc:  # noqa: BLE001 -- one dead run must not end a campaign
        return (None, time.time() - started, client.stats.calls,
                f"{label}: FAILED {type(exc).__name__}: {exc}")
    return result, time.time() - started, client.stats.calls, label


def run(scenarios, settings, repeats, concurrency, out) -> list[dict[str, Any]]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [
        (arm, scenario, repeat)
        for repeat in range(1, repeats + 1)
        for scenario in scenarios
        for arm in ARMS
    ]
    print(f"  {len(jobs)} run(s) = {len(ARMS)} arm(s) x {len(scenarios)} design "
          f"point(s) x {repeats} repetition(s), {concurrency} in flight\n",
          flush=True)

    results: list[dict[str, Any]] = []
    done = 0
    started = time.time()
    _, detail = placement(settings)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(_one, scenario, settings, repeat, arm): (arm, scenario, repeat)
            for arm, scenario, repeat in jobs
        }
        for future in as_completed(futures):
            done += 1
            result, elapsed, calls, label = future.result()
            if result is None:
                print(f"  [{done}/{len(jobs)}] {label}", flush=True)
                continue
            arm, scenario, repeat = futures[future]
            landed = result.truth.payload_landed if result.truth else "n/a"
            print(
                f"  [{done}/{len(jobs)}] {label} K={scenario.design.workers}: "
                f"{'ok' if result.ok else 'VOID'} in {elapsed:.0f}s, "
                f"landed={landed}, task={result.task_success}, "
                f"A={result.analysis_tokens}, sr={result.self_report_calls}, "
                f"{calls} call(s)",
                flush=True,
            )
            if not result.ok and result.failure:
                print(f"      reason: {result.failure[:200]}", flush=True)
            for note in result.notes:
                print(f"      note: {str(note)[:180]}", flush=True)
            for row in result.rows:
                data = dataclasses.asdict(row)
                print(f"        {data['method']:<22} "
                      f"preserved={data['work_preserved']:.2f} "
                      f"esc={data.get('escalations', 0)} "
                      f"blast={data.get('blast_radius_events')} "
                      f"tok={data.get('recovery_tokens')} "
                      f"unsafe={data.get('unsafe_preservations')}", flush=True)

            row = result.to_dict()
            row["arm"] = arm
            row["repeat"] = repeat
            row["workers"] = scenario.design.workers
            results.append(row)
            # Flushed after EVERY run: a campaign that cannot be interrupted
            # safely is one nobody can afford to start.
            out.write_text(json.dumps({
                "frontier": "local-llama",
                "experiment": "fanout-localized-contamination",
                "placement": detail,
                "arms": list(ARMS),
                "results": results,
            }, indent=2), encoding="utf-8")
    print(f"\nwall clock: {time.time() - started:.0f}s, concurrency {concurrency}",
          flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--out", type=Path, default=OUT_DIR / "fanout-campaign.json")
    parser.add_argument("--workers", type=int, nargs="*", default=None,
                        help="override the design points with these sizes")
    args = parser.parse_args(argv)

    settings = LocalLlamaSettings(model=args.model)
    # GPU FIRST, ALWAYS. `warn_if_cpu` loads the model before checking, because
    # `placement` alone cannot tell "on CPU" from "idle-unloaded" and Ollama
    # unloads after a few minutes -- so checking without warming would refuse
    # every campaign started after a pause.
    if not warn_if_cpu(settings):
        print("  REFUSING: not on the GPU. Restart Ollama while idle, re-run.")
        return 2

    scenarios = (
        [FanoutScenario.build(k, "influencing") for k in args.workers]
        if args.workers else design_points()
    )
    for scenario in scenarios:
        problems = scenario.validate()
        if problems:
            print(f"  {scenario.test_id} failed validation: {problems}")
            return 2

    run(scenarios, settings, args.repeats, args.concurrency, args.out)
    print(f"results written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
