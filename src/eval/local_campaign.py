"""
The local-LLaMA frontier: the same experiment, a model on this machine.

    python -m src.eval.local_campaign --plan            # free, prints the budget
    python -m src.eval.local_campaign --suite <file>    # run it
    python -m src.eval.local_campaign --suite <file> --limit 2

WHAT IS REUSED, WHICH IS ALMOST EVERYTHING
-------------------------------------------
This module is a runner, not a pipeline. `real_llm.run_generated()` does the
work -- the same tracing, provenance, attribution, contamination walk, planner,
replay, verification, baselines and `RecoveryScore` the NVIDIA frontier uses.
The only difference is the client it is handed.

    NVIDIA frontier   src/eval/real_campaign.py  -> NVIDIAClient   (untouched)
    local frontier    this module                -> LocalLlamaClient

`src/common/nvidia.py` and `src/eval/real_campaign.py` are not imported here.
Results go to `data/results/local_llama/`, never to `data/results/real-llm*`.

WHY IT RUNS THE **SAME SUITE** RATHER THAN A NEW ONE
-----------------------------------------------------
The brief's Phase 7 asks for a comparison against the existing frontier. A
comparison needs the scenarios held constant, so this replays
`data/generated/suite-20260910.jsonl` -- the exact design points the NVIDIA
campaign ran, stratified across all three channels (`web` = scenario A,
`memory` = B, `agent_message` = C) and both intents. Generating a fresh suite
locally would change the model *and* the tests at once and answer nothing.

It also means no generation calls are spent: every local token goes into
execution, which matters at ~4 tok/s.

CONCURRENCY IS 1, AND THAT IS A MEASUREMENT
--------------------------------------------
`python -m src.eval.local_bench` found this backend fully serialised: identical
throughput at 1, 2 and 4 in flight, with latency scaling linearly. So there is
no thread pool here. Running two tests at once would double every latency and
finish no sooner.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.common.local_llama import (
    LocalLlamaClient,
    LocalLlamaSettings,
    available,
    placement,
    warn_if_cpu,
)
from src.eval.llm_scenarios import GeneratedScenario
from src.eval.real_llm import (
    annotation_summary,
    landing_summary,
    method_summary,
    pair_summary,
    render,
    run_generated,
)

RESULTS_DIR = Path("data/results/local_llama")
RUNS_DIR = Path("data/runs/local_llama")
DEFAULT_SUITE = Path("data/generated/suite-20260910.jsonl")



def c_safe_from_sweep(model: str, on_gpu: bool) -> int:
    """`C_safe` for this model and placement, from the sweep that measured it.

    Read rather than guessed, and keyed on placement as well as model because
    the two answers differ: 1 on CPU, 2 on GPU for the same `llama3.2:3b`.
    Falls back to 1 when no sweep exists, which is the conservative reading --
    a campaign at an unmeasured concurrency is the thing Phase 1 exists to
    prevent.
    """
    safe = model.replace(":", "-").replace("/", "-")
    where = "GPU" if on_gpu else "CPU"
    path = RESULTS_DIR / f"concurrency-sweep-{safe}-{where}.json"
    if not path.exists():
        print(f"  no sweep at {path}; falling back to concurrency 1")
        return 1
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["verdict"]["C_safe"])
    except Exception:  # noqa: BLE001
        return 1


def load_suite(path: Path, limit: int | None = None) -> list[GeneratedScenario]:
    scenarios = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            scenarios.append(GeneratedScenario.from_dict(json.loads(line)))
    return scenarios[:limit] if limit else scenarios


def plan(scenarios: list[GeneratedScenario], tok_per_s: float = 4.0) -> str:
    """What this would cost, before spending it.

    The estimate is in **wall clock**, not requests, because on a CPU-bound
    local model wall clock is the binding constraint and a request count does
    not convert to one. Derived from `local_bench`'s measured throughput and
    the NVIDIA frontier's measured call counts per workflow shape.
    """
    calls = {"short": 45, "long": 80}
    out_tokens = 250  # measured mean answer length on this pipeline's prompts
    lines = [
        f"planned local campaign: {len(scenarios)} test(s), concurrency 1",
        f"assuming {tok_per_s:.1f} tok/s (local_bench) and ~{out_tokens} output "
        "tokens per call",
        "",
        f"  {'test':<9}{'channel':<15}{'intent':<14}{'workflow':<10}{'est. calls':>11}{'est. min':>10}",
    ]
    total_min = 0.0
    for scenario in scenarios:
        shape = scenario.design.workflow
        n = calls.get(shape, 45)
        minutes = n * out_tokens / tok_per_s / 60
        total_min += minutes
        lines.append(
            f"  {scenario.test_id:<9}{scenario.design.channel:<15}"
            f"{scenario.design.intent:<14}{shape:<10}{n:>11}{minutes:>10.0f}"
        )
    lines += [
        "",
        f"  estimated total: {total_min:.0f} min ({total_min / 60:.1f} h)",
        "",
        "  The estimate is deliberately pessimistic on call count and optimistic",
        "  on answer length; treat it as an order of magnitude, not a promise.",
    ]
    return "\n".join(lines)


def _one(
    scenario: GeneratedScenario,
    settings: LocalLlamaSettings,
    detector: str,
    repeat: int,
) -> tuple[Any, float, int, str]:
    """One (scenario, repeat). Returns (result | None, elapsed, calls, label)."""
    label = f"{scenario.test_id}#r{repeat}"
    # Each repeat gets its own directory, or repeat 2 would overwrite repeat 1's
    # trace and the repetitions would all be the last one.
    workdir = RUNS_DIR / f"{scenario.test_id}-r{repeat}"
    client = LocalLlamaClient(settings=settings)
    started = time.time()
    try:
        result = run_generated(
            scenario,
            client,
            workdir=workdir,
            detector_name=detector,
            # A different seed per repeat, so repetitions are repetitions and
            # not the same run five times. The model is greedy at temperature
            # 0, so without this every repeat would be identical and the
            # "variance" would be zero by construction.
            seed=20260910 + repeat,
            replay_client_factory=lambda: LocalLlamaClient(settings=settings),
        )
    except Exception as exc:  # noqa: BLE001 -- one dead test must not end a campaign
        return None, time.time() - started, client.stats.calls, f"{label}: FAILED {type(exc).__name__}: {exc}"
    return result, time.time() - started, client.stats.calls, label


def run(
    scenarios: list[GeneratedScenario],
    detector: str = "oracle",
    model: str = "llama3",
    repeats: int = 1,
    concurrency: int = 1,
    out: Path | None = None,
    placement_note: str = "",
) -> list[Any]:
    """Every (scenario, repeat), `concurrency` tests in flight.

    The unit of parallelism is a whole test, never a call inside one: a test
    writes traces, checkpoints and a memory file keyed by its own path, and the
    replays inside it are sequential by nature. That is the same rule the
    hosted frontier uses, and `C_safe` from `local_bench` is what sets the
    number.
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    settings = LocalLlamaSettings(model=model)
    jobs = [
        (scenario, repeat)
        for repeat in range(1, repeats + 1)
        for scenario in scenarios
    ]
    print(
        f"  {len(jobs)} run(s) = {len(scenarios)} design point(s) x "
        f"{repeats} repetition(s), {concurrency} in flight",
        flush=True,
    )
    print(flush=True)

    results: list[Any] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(_one, scenario, settings, detector, repeat): (scenario, repeat)
            for scenario, repeat in jobs
        }
        for future in as_completed(futures):
            done += 1
            result, elapsed, calls, label = future.result()
            if result is None:
                print(f"  [{done}/{len(jobs)}] {label}", flush=True)
                continue
            scenario, repeat = futures[future]
            print(
                f"  [{done}/{len(jobs)}] {label} "
                f"{scenario.design.channel}/{scenario.design.intent}/"
                f"{scenario.design.workflow}: "
                f"{'ok' if result.ok else 'VOID'} in {elapsed:.0f}s, "
                f"landed={result.truth.payload_landed if result.truth else 'n/a'}, "
                f"{len(result.rows)} row(s), {calls} call(s)",
                flush=True,
            )
            # A void row without its reason is a row nobody can act on, and
            # the first local run produced exactly that: five and a half
            # minutes of inference reported as "VOID" with no cause attached.
            if not result.ok and result.failure:
                print(f"      reason: {result.failure[:200]}", flush=True)
            results.append(result)
            # Flushed after EVERY run, not at the end. The CPU campaign was
            # stopped after four of six tests and its results existed only
            # inside a process that was about to be killed -- they had to be
            # rebuilt from the traces afterwards. A long campaign that cannot
            # be interrupted safely is a campaign nobody can afford to start.
            if out is not None:
                save(results, out, placement_note=placement_note)
    return results


def save(results: list[Any], path: Path, placement_note: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "frontier": "local-llama",
                "placement": placement_note,
                "results": [r.to_dict() for r in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local LLaMA campaign")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--detector", default="oracle")
    parser.add_argument("--model", default="llama3")
    parser.add_argument("--repeats", type=int, default=1,
                        help="repetitions per design point")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="tests in flight; defaults to C_safe from the "
                             "most recent sweep for this model and placement")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--out", type=Path,
                        default=RESULTS_DIR / "local-llm-campaign.json")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.suite.exists():
        print(f"no suite at {args.suite}")
        return 1
    scenarios = load_suite(args.suite, args.limit)

    if args.plan:
        print(plan(scenarios))
        return 0

    settings = LocalLlamaSettings(model=args.model)
    ok, detail = available(settings)
    print(f"local backend: {detail}")
    if not ok:
        return 1
    # Before anything is spent: is this actually on the GPU? The first local
    # campaign ran entirely on CPU at a fifth of the achievable speed and
    # nobody knew until afterwards.
    on_gpu = warn_if_cpu(settings)
    _placement_ok, placement_detail = placement(settings)
    print()
    print(f"suite: {args.suite} ({len(scenarios)} test(s)), detector={args.detector}")
    print()

    started = time.time()
    concurrency = args.concurrency
    if concurrency is None:
        concurrency = c_safe_from_sweep(args.model, on_gpu)
        print(f"concurrency: {concurrency} (C_safe from the measured sweep)")
    results = run(
        scenarios, detector=args.detector, model=args.model,
        repeats=args.repeats, concurrency=concurrency,
        out=args.out, placement_note=placement_detail,
    )
    elapsed = time.time() - started

    print()
    print(render(results))
    print()
    print(landing_summary(results))
    print()
    print(method_summary(results))
    print()
    print(pair_summary(results))
    print()
    print(annotation_summary(results))
    print()
    print(f"wall clock: {elapsed:.0f}s ({elapsed / 60:.0f} min), concurrency 1")
    out = save(results, args.out, placement_note=placement_detail)
    print(f"results written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
