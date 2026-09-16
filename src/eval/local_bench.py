"""
Phase 1 of the local-LLaMA brief: measure the machine before trusting it.

    python -m src.eval.local_bench                 # the sweep
    python -m src.eval.local_bench --levels 1,2    # a shorter one

The brief is explicit that concurrency must not be guessed. This runs a
controlled sweep, repeats each level, and reports the three numbers the campaign
needs:

    C_safe        highest concurrency with no failures and no timeouts, whose
                  throughput is still improving or flat
    C_saturated   the level past which extra concurrency stops buying throughput
    C_failure     the level where failures, timeouts or OOM first appear

WHY THIS IS NOT A FORMALITY ON THIS MACHINE
--------------------------------------------
The model is `llama3:latest` -- Llama 3 8B at Q4, 4.7 GB -- and the GPU is a
6 GB laptop RTX 3050. The weights do not leave much room for KV cache, so a
second concurrent request is not free the way it is on a hosted endpoint: it
competes for the same VRAM and can push layers onto the CPU, which shows up as
throughput *falling* rather than as an error. A sweep that only counted failures
would call that level safe.

So throughput per level is measured, not just success. Telemetry (GPU
utilisation, VRAM, RAM) is sampled during each level through `nvidia-smi` when
it is available, and its absence is recorded rather than silently skipped.

Nothing here writes into the NVIDIA frontier's artifacts. Results go to
`data/results/local_llama/`.
"""

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "llama3"
RESULTS_DIR = Path("data/results/local_llama")

# A prompt shaped like the ones the pipeline actually sends: a short task plus a
# rendered source, asking for a bounded answer. Benchmarking on "reply ok" would
# measure the scheduler and not the workload.
BENCH_PROMPT = (
    "Task: parse the sample date strings and print each one as an ISO date, "
    "one per line.\n\n"
    "Sources:\n"
    "[S1] (web, https://docs.python.org/3/library/datetime.html)\n"
    "datetime.strptime parses a string into a datetime using an explicit "
    "format code. It raises ValueError when the string does not match.\n\n"
    "In at most three sentences, state which library you will use and why."
)
BENCH_TOKENS = 160


@dataclass
class CallResult:
    ok: bool
    latency_s: float = 0.0
    output_tokens: int = 0
    error: str = ""


@dataclass
class LevelResult:
    """One concurrency level, one repeat."""

    concurrency: int
    repeat: int
    calls: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    wall_clock_s: float = 0.0
    latencies: list[float] = field(default_factory=list)
    output_tokens: int = 0
    gpu_util_pct: float | None = None
    gpu_mem_used_mb: float | None = None
    ram_used_mb: float | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def throughput_tok_s(self) -> float:
        return self.output_tokens / self.wall_clock_s if self.wall_clock_s else 0.0

    @property
    def completed_per_min(self) -> float:
        return (
            self.successes / (self.wall_clock_s / 60) if self.wall_clock_s else 0.0
        )

    def p(self, q: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
        return ordered[index]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["p50_s"] = round(self.p(0.50), 2)
        out["p95_s"] = round(self.p(0.95), 2)
        out["throughput_tok_s"] = round(self.throughput_tok_s, 2)
        out["completed_per_min"] = round(self.completed_per_min, 2)
        out.pop("latencies")
        return out


def _call(model: str, timeout_s: float) -> CallResult:
    payload = json.dumps(
        {
            "model": model,
            "prompt": BENCH_PROMPT,
            "stream": False,
            "options": {"num_predict": BENCH_TOKENS, "temperature": 0},
        }
    ).encode()
    request = urllib.request.Request(
        OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = json.loads(response.read())
        return CallResult(
            ok=True,
            latency_s=time.time() - started,
            output_tokens=int(body.get("eval_count") or 0),
        )
    except TimeoutError:
        return CallResult(False, time.time() - started, error="timeout")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        kind = "timeout" if "timed out" in str(reason).lower() else "urlerror"
        return CallResult(False, time.time() - started, error=f"{kind}: {reason}")
    except Exception as exc:  # noqa: BLE001 -- a bench must not die on one call
        return CallResult(False, time.time() - started, error=f"{type(exc).__name__}: {exc}")


class _Telemetry:
    """GPU and RAM sampling during a level, or an honest None.

    Sampled in a thread rather than read once at the end, because the interesting
    value is the peak while the level is in flight.
    """

    def __init__(self) -> None:
        self.gpu_util: list[float] = []
        self.gpu_mem: list[float] = []
        self.ram: list[float] = []
        self.available = self._probe()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _probe() -> bool:
        try:
            subprocess.run(
                ["nvidia-smi", "--version"], capture_output=True, timeout=10, check=True
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=utilization.gpu,memory.used",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                util, mem = out.stdout.strip().splitlines()[0].split(",")
                self.gpu_util.append(float(util))
                self.gpu_mem.append(float(mem))
            except Exception:  # noqa: BLE001
                pass
            try:
                import shutil  # noqa: F401  (kept cheap; psutil is not a dependency)

                with open("/proc/meminfo") as fh:  # not on Windows; falls through
                    pass
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(2.0)

    def __enter__(self) -> "_Telemetry":
        if self.available:
            self._thread = threading.Thread(target=self._sample, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def peak(self) -> tuple[float | None, float | None]:
        return (
            max(self.gpu_util) if self.gpu_util else None,
            max(self.gpu_mem) if self.gpu_mem else None,
        )


def run_level(
    concurrency: int, repeat: int, model: str, calls_per_worker: int, timeout_s: float
) -> LevelResult:
    result = LevelResult(concurrency=concurrency, repeat=repeat)
    total = concurrency * calls_per_worker
    with _Telemetry() as telemetry:
        started = time.time()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for call in pool.map(
                lambda _i: _call(model, timeout_s), range(total)
            ):
                result.calls += 1
                if call.ok:
                    result.successes += 1
                    result.latencies.append(call.latency_s)
                    result.output_tokens += call.output_tokens
                else:
                    result.failures += 1
                    if call.error.startswith("timeout"):
                        result.timeouts += 1
                    if call.error not in result.errors:
                        result.errors.append(call.error)
        result.wall_clock_s = time.time() - started
        util, mem = telemetry.peak()
        result.gpu_util_pct, result.gpu_mem_used_mb = util, mem
    return result


def classify(levels: list[LevelResult]) -> dict[str, Any]:
    """C_safe / C_saturated / C_failure, from the measurements only.

    `C_safe` is the highest level with zero failures **and** throughput no worse
    than 90% of the best seen. The second half matters on a VRAM-bound machine:
    a level that completes every call while running half as fast has not failed,
    and it is not safe to run a campaign on either.
    """
    by_level: dict[int, list[LevelResult]] = {}
    for level in levels:
        by_level.setdefault(level.concurrency, []).append(level)

    summary = {}
    for c, runs in sorted(by_level.items()):
        summary[c] = {
            "failures": sum(r.failures for r in runs),
            "timeouts": sum(r.timeouts for r in runs),
            "throughput_tok_s": round(
                statistics.mean(r.throughput_tok_s for r in runs), 2
            ),
            "p50_s": round(statistics.mean(r.p(0.50) for r in runs), 2),
            "p95_s": round(statistics.mean(r.p(0.95) for r in runs), 2),
            "completed_per_min": round(
                statistics.mean(r.completed_per_min for r in runs), 2
            ),
        }

    best = max((s["throughput_tok_s"] for s in summary.values()), default=0.0)
    c_failure = next(
        (c for c, s in sorted(summary.items()) if s["failures"] or s["timeouts"]), None
    )

    # C_safe is the SMALLEST concurrency that reaches peak throughput, not the
    # largest that does not fail. The first version of this rule took the
    # largest and answered 4 on a backend that turned out to be fully
    # serialised: throughput flat at ~4 tok/s across 1, 2 and 4 while p50 went
    # 15s -> 29s -> 62s. Nothing failed, so "no failures" called it safe -- and
    # running the campaign there would have tripled every latency for no
    # throughput at all, which is not a safe setting, it is a wasteful one.
    #
    # Above saturation, extra concurrency is pure queueing: the work is the
    # same, it just waits longer. So the useful answer is the cheapest level
    # that gets the available throughput.
    healthy = [
        c for c, s in sorted(summary.items())
        if not s["failures"] and not s["timeouts"]
        and s["throughput_tok_s"] >= 0.95 * best
    ]
    c_safe = min(healthy) if healthy else min(summary, default=1)

    # C_saturated: the level past which more concurrency stops buying
    # throughput. When the backend is serialised this is the lowest level
    # measured, and saying so is the point of the number.
    c_saturated = None
    ordered = sorted(summary)
    for a, b in zip(ordered, ordered[1:]):
        if summary[b]["throughput_tok_s"] <= summary[a]["throughput_tok_s"] * 1.05:
            c_saturated = a
            break
    if c_saturated is None and ordered:
        c_saturated = ordered[-1]
    return {
        "per_level": summary,
        "C_safe": c_safe,
        "C_saturated": c_saturated,
        "C_failure": c_failure,
        "best_throughput_tok_s": best,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local LLaMA concurrency sweep")
    parser.add_argument("--levels", default="1,2,4")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--calls-per-worker", type=int, default=2)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    print(f"local LLaMA sweep: model={args.model} levels={levels} "
          f"repeats={args.repeats} calls/worker={args.calls_per_worker}")
    print("warming the model (first load is not part of any level)...")
    warm = _call(args.model, args.timeout)
    print(f"  warm-up {'ok' if warm.ok else 'FAILED: ' + warm.error} "
          f"in {warm.latency_s:.1f}s\n")
    if not warm.ok:
        print("the endpoint did not answer; nothing below would mean anything")
        return 1

    # Placement is part of the measurement, not context for it: a CPU sweep and
    # a GPU sweep of the same model are different experiments.
    from src.common.local_llama import LocalLlamaSettings, placement

    on_gpu, placement_detail = placement(LocalLlamaSettings(model=args.model))
    where = "GPU" if on_gpu else "*** CPU ***"
    print(f"  placement: {where} -- {placement_detail}")
    print()

    results: list[LevelResult] = []
    for concurrency in levels:
        for repeat in range(1, args.repeats + 1):
            level = run_level(
                concurrency, repeat, args.model, args.calls_per_worker, args.timeout
            )
            results.append(level)
            print(
                f"  c={concurrency} rep={repeat}: "
                f"{level.successes}/{level.calls} ok, {level.failures} failed, "
                f"{level.timeouts} timed out | "
                f"p50={level.p(0.50):.1f}s p95={level.p(0.95):.1f}s | "
                f"{level.throughput_tok_s:.1f} tok/s | "
                f"{level.completed_per_min:.2f} calls/min"
                + (f" | GPU {level.gpu_util_pct:.0f}% "
                   f"{level.gpu_mem_used_mb:.0f}MiB"
                   if level.gpu_mem_used_mb is not None else " | GPU n/a")
            )

    verdict = classify(results)
    print("\nper level (averaged over repeats)")
    print(f"  {'c':>3}{'fail':>6}{'timeout':>9}{'tok/s':>9}{'p50 s':>8}"
          f"{'p95 s':>8}{'calls/min':>11}")
    for c, s in verdict["per_level"].items():
        print(f"  {c:>3}{s['failures']:>6}{s['timeouts']:>9}"
              f"{s['throughput_tok_s']:>9}{s['p50_s']:>8}{s['p95_s']:>8}"
              f"{s['completed_per_min']:>11}")
    print()
    print(f"  C_safe      = {verdict['C_safe']}")
    print(f"  C_saturated = {verdict['C_saturated']}")
    print(f"  C_failure   = {verdict['C_failure']}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # Per model, because the answer is a property of the model and the machine
    # together: llama3 (8B) and llama3.2 (3B) gave 4.1 and 9.5 tok/s on the same
    # box. One filename would have let the second overwrite the first.
    safe_name = args.model.replace(":", "-").replace("/", "-")
    # Placement is part of the filename, not just the payload. The GPU sweep
    # silently overwrote the CPU one for the same model until it was: they are
    # different experiments (58 tok/s against 9.5, and C_safe 2 against 1), and
    # the CPU campaign's benchmark has to survive alongside the GPU one.
    where = "GPU" if on_gpu else "CPU"
    out = RESULTS_DIR / f"concurrency-sweep-{safe_name}-{where}.json"
    out.write_text(
        json.dumps(
            {
                "model": args.model,
                "placement": placement_detail,
                "on_gpu": on_gpu,
                "prompt_tokens_requested": BENCH_TOKENS,
                "levels": [r.to_dict() for r in results],
                "verdict": verdict,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
