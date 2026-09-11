"""
The real-LLM campaign: generate a suite, run it, score it, write it down.

    python -m src.eval.real_campaign --plan --n 6
    python -m src.eval.real_campaign --n 6 --detector oracle
    python -m src.eval.real_campaign --suite data/generated/suite.jsonl

Two modes, and the first one spends nothing. `--plan` prints the design points
that would be generated, the model that would write each, and the request
budget, so a campaign's size is a decision made before the requests are spent
rather than discovered afterwards (the same courtesy `token_validation.
request_budget()` offers).

WHAT THIS IS NOT
----------------
It is not `src/eval/campaign.py`. That one repeats a deterministic matrix
thirty times to put an error bar on the *estimator's* variation, holding the
model fixed by construction. This one runs each generated scenario once
against a live model, and its variation is the model's. Pooling the two would
average an interval over self-report noise together with an interval over model
non-determinism and label the result as one thing.

CONCURRENCY
-----------
Bounded and small. The reason to keep it small is not politeness to the
provider: a test writes traces, checkpoints and a memory file keyed by its own
path, and the recovery replays inside one test are sequential by nature. The
parallel unit is therefore a whole test, and two at a time is enough to hide
latency without a burst. `KeyPool` and `RateLimiter` are both lock-guarded and
the pool hands every client the *same* limiter, so two tests in flight pace
against one budget rather than one each.

WALL CLOCK, BECAUSE IT DECIDES HOW BIG A CAMPAIGN CAN BE
---------------------------------------------------------
Measured on Nemotron: individual calls range from 0.5s to 160s for the same
prompt shape, and a short-workflow test is ~45 calls. A six-test campaign is
therefore an hour or two, not minutes, and that -- not a quota -- is what
bounds the size here. `--plan` prints the request estimate before anything is
spent; the time estimate is roughly 20 seconds a call.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.common.nvidia import (
    MODELS,
    ModelPool,
    available_models,
    load_nvidia_settings,
    retired_models,
)
from src.eval.llm_scenarios import (
    DEFAULT_SUITE_DIR,
    GeneratedScenario,
    describe_suite,
    generate_suite,
    load_suite,
    sample_design,
    save_suite,
)
from src.eval.real_llm import (
    RealRunResult,
    annotation_summary,
    landing_summary,
    method_summary,
    pair_summary,
    render,
    run_generated,
    save_results,
)

DEFAULT_WORKDIR = Path("data/runs/real")
DEFAULT_RESULTS = Path("data/results/real-llm.json")

# One test costs: the pipeline's model calls, one self-report per model-written
# event, the counterfactual refinement, and then a replay for each of the four
# methods. Measured on the short workflow rather than derived, and printed
# before a campaign so the size is a choice.
REQUESTS_PER_TEST_SHORT = 45
REQUESTS_PER_TEST_LONG = 80


@dataclass
class CampaignReport:
    results: list[RealRunResult] = field(default_factory=list)
    scenarios: list[GeneratedScenario] = field(default_factory=list)
    generation_notes: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    wall_clock_s: float = 0.0

    @property
    def scored(self) -> list[RealRunResult]:
        return [r for r in self.results if r.ok]

    @property
    def void(self) -> list[RealRunResult]:
        return [r for r in self.results if not r.ok]


def plan(n: int, seed: int) -> str:
    """What a campaign of size `n` would do, and what it would cost."""
    points = sample_design(n, seed=seed)
    live = available_models()
    gone = retired_models()
    lines = [f"planned campaign: {len(points)} test(s), seed {seed}", ""]
    lines.append("models available for generation and execution:")
    for spec in live:
        lines.append(f"  {spec.display:<34} {spec.model_id}")
    for spec in gone:
        lines.append(f"  {spec.display:<34} UNAVAILABLE -- {spec.status_detail}")
    if not live:
        lines.append("  NONE. This campaign cannot run.")
    lines.append("")
    lines.append("design points, and the model that would generate each:")
    budget = 0
    for index, point in enumerate(points, start=1):
        handle = live[(index - 1) % len(live)].handle if live else "-"
        lines.append(f"  gen{index:03d}  {point.key():<62} <- {handle}")
        budget += (
            REQUESTS_PER_TEST_LONG
            if point.workflow == "long"
            else REQUESTS_PER_TEST_SHORT
        )
    lines.append("")
    lines.append(
        f"estimated requests: {len(points)} generation + ~{budget} execution "
        f"= ~{len(points) + budget} total"
    )
    return "\n".join(lines)


def run_campaign(
    n: int = 6,
    seed: int = 20260910,
    detector: str = "oracle",
    workdir: Path = DEFAULT_WORKDIR,
    suite_path: Path | None = None,
    existing: list[GeneratedScenario] | None = None,
    max_concurrency: int | None = None,
    refine: bool = True,
    execution_models: list[str] | None = None,
    verbose: bool = True,
    results_path: Path | None = None,
) -> CampaignReport:
    """Generate (or load) a suite and run every scenario through CausalLine.

    A test that dies takes itself out of the table and nothing else with it
    (§13). The failure is recorded on the result and printed; it is not
    retried, because a scenario that killed the pipeline once will very likely
    do it again and the campaign's remaining budget is better spent on the
    tests that work.
    """
    started = time.time()
    settings = load_nvidia_settings()
    pool = ModelPool(settings)
    report = CampaignReport()

    # A campaign is tens of minutes of API calls. Printing what it is doing is
    # not decoration: without it, a stalled endpoint and a slow one look
    # identical, and the only way to tell is to kill the run. Nothing printed
    # here is or contains a credential.
    def progress(line: str) -> None:
        if verbose:
            print(line, flush=True)

    for spec in retired_models():
        report.unavailable.append(f"{spec.display}: {spec.status_detail}")

    # Two short requests, before the campaign commits to anything. A model that
    # cannot answer "ok" in 45 seconds is cooled here rather than discovered
    # three scenarios in, at five stalled requests apiece.
    progress("preflight:")

    def note(handle: str, ok: bool, why: str) -> None:
        progress(f"  {MODELS[handle].display:<34} {'ok' if ok else 'UNREACHABLE: ' + why}")
        if not ok:
            report.unavailable.append(f"{MODELS[handle].display}: unreachable -- {why}")

    reachable = pool.preflight(on_result=note)
    if not any(reachable.values()):
        report.generation_notes.append(
            "no NVIDIA model answered the preflight; the campaign cannot run"
        )
        report.wall_clock_s = time.time() - started
        return report

    if existing is not None:
        report.scenarios = list(existing)
    elif suite_path and Path(suite_path).exists():
        report.scenarios = load_suite(suite_path)
        report.generation_notes.append(f"loaded {len(report.scenarios)} from {suite_path}")
    else:
        suite = generate_suite(n, pool, seed=seed, on_progress=progress)
        report.scenarios = suite.scenarios
        report.generation_notes.append(suite.summary())
        report.unavailable.extend(suite.unavailable)
        if suite.scenarios:
            target = Path(suite_path or DEFAULT_SUITE_DIR / f"suite-{seed}.jsonl")
            save_suite(suite.scenarios, target)
            report.generation_notes.append(f"suite written to {target}")

    if not report.scenarios:
        report.wall_clock_s = time.time() - started
        return report

    # WHICH MODEL EXECUTES WHICH TEST.
    # Round-robin over the *available* models, offset by the scenario index, so
    # a test is not usually executed by the model that wrote it. That
    # separation matters: a model evaluating a scenario it authored is the one
    # arrangement where a generated test could be tuned to the executor, and
    # keeping them apart costs nothing.
    # `pool.candidates()`, not `available_models()`. The registry says which
    # models exist; the pool says which ones answered the preflight and are not
    # cooling. Using the registry here made the preflight pointless for
    # execution -- a model that had just failed to say "ok" would still be
    # handed a third of the tests, at five stalled requests per model call.
    live = [
        h
        for h in (execution_models or pool.candidates())
        if MODELS[h].available
    ]
    if not live:
        report.generation_notes.append("no model available to execute with")
        report.wall_clock_s = time.time() - started
        return report

    limit = max(1, int(max_concurrency or settings.max_concurrency))

    def one(item: tuple[int, GeneratedScenario]) -> RealRunResult:
        index, scenario = item
        # Chosen when the test starts, not when the campaign was planned: a
        # model cooled by an earlier test must not be handed a later one.
        # Falls back to the planned rotation only if everything is cooling,
        # which is better than refusing to run at all.
        usable = [h for h in pool.candidates() if h in live] or live
        handle = usable[index % len(usable)]
        progress(
            f"  [{index + 1}/{len(report.scenarios)}] {scenario.test_id} "
            f"{scenario.design.key()} -> {handle}"
        )
        began = time.time()
        # A fresh client per test, built through the pool so it shares the one
        # key rotation and the one rate limiter. Fresh because token counts,
        # retry counts and key rotations are recorded per test and a shared
        # client would pool them into whichever test ran first.
        outcome = run_generated(
            scenario,
            pool.client_for(handle),
            workdir=workdir / scenario.test_id,
            detector_name=detector,
            seed=seed,
            refine=refine,
            replay_client_factory=lambda: pool.client_for(handle),
        )
        if outcome.model_failure:
            # THE ENDPOINT WOULD NOT SERVE THIS TEST, SO STOP SENDING IT TESTS.
            # Execution has no per-call model fallback and must not have one --
            # a trace is a trace of one model, and splicing two into a workflow
            # would make its header a lie. But nothing was telling the pool that
            # a model had just failed a whole test, so the next test went to it
            # anyway. On a flaky endpoint that is half a campaign lost to one
            # outage. Cooling doubles per failure, so a model having a bad
            # minute comes back and a model that is down does not.
            pool.cool(handle)
            progress(f"      {handle} failed this test; cooling it")
        landed = outcome.truth.payload_landed if outcome.truth else False
        progress(
            f"      {scenario.test_id} done in {time.time() - began:.0f}s: "
            f"{'ok' if outcome.ok else 'VOID'}, landed={landed}, "
            f"{len(outcome.rows)} row(s), {outcome.api_calls} call(s)"
        )
        return outcome

    # WRITTEN AFTER EVERY TEST, NOT ONLY AT THE END.
    # A test costs 45-80 requests and several minutes; a six-test campaign is
    # an hour or two. Saving once at the end means any failure after the last
    # replay -- a rendering bug, a full disk, an interrupt -- throws away every
    # one of those requests. The traces themselves are already written
    # incrementally (D-008), but the expensive part of a test is its four
    # recovery replays, and those live only in the scored result.
    partial = Path(results_path) if results_path else DEFAULT_RESULTS

    def keep(result: RealRunResult) -> RealRunResult:
        report.results.append(result)
        try:
            save_results(report.results, partial)
        except OSError as exc:  # noqa: BLE001 -- never lose a test to its own bookkeeping
            progress(f"      (could not write partial results: {exc})")
        return result

    items = list(enumerate(report.scenarios))
    if limit == 1:
        for item in items:
            keep(_guarded(one, item))
    else:
        with ThreadPoolExecutor(max_workers=limit) as ex:
            for result in ex.map(lambda i: _guarded(one, i), items):
                keep(result)

    report.wall_clock_s = time.time() - started
    return report


def _guarded(fn, item) -> RealRunResult:
    """Never let one test's exception end the campaign.

    `run_generated` already catches the failures it can name. This is the net
    under it, and it produces a result object rather than a hole in the list
    so the failure appears in the table (§19).
    """
    index, scenario = item
    try:
        return fn(item)
    except Exception as exc:  # noqa: BLE001
        return RealRunResult(
            test_id=scenario.test_id,
            design=scenario.design.to_dict(),
            execution_model="unknown",
            execution_model_id="unknown",
            generator_model=scenario.generation.model_handle,
            detector="unknown",
            ok=False,
            failure=f"{type(exc).__name__}: {exc}",
        )


def render_campaign(report: CampaignReport) -> str:
    lines: list[str] = []
    for note in report.generation_notes:
        lines.append(note)
    if report.unavailable:
        lines.append("")
        lines.append("MODELS NOT USED (reported, not substituted):")
        for note in report.unavailable:
            lines.append(f"  {note}")
    lines.append("")
    lines.append(describe_suite(report.scenarios))
    lines.append("")
    lines.append(render(report.results))
    lines.append("")
    lines.append(landing_summary(report.results))
    lines.append("")
    lines.append(method_summary(report.results))
    lines.append("")
    lines.append(pair_summary(report.results))
    lines.append("")
    lines.append(annotation_summary(report.results))
    if report.void:
        lines.append("")
        lines.append(f"{len(report.void)} test(s) produced no scoreable row:")
        for result in report.void:
            lines.append(f"  {result.test_id}: {result.failure[:160]}")
    api = sum(r.api_calls for r in report.results)
    retries = sum(r.api_retries for r in report.results)
    limited = sum(r.api_rate_limited for r in report.results)
    lines.append("")
    lines.append(
        f"api: {api} call(s), {retries} retry(ies), {limited} rate limit(s), "
        f"{report.wall_clock_s:.0f}s wall clock"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    argv = sys.argv[1:]

    def opt(name: str, default: str) -> str:
        return argv[argv.index(name) + 1] if name in argv else default

    count = int(opt("--n", "6"))
    seed = int(opt("--seed", "20260910"))
    detector = opt("--detector", "oracle")
    workdir = Path(opt("--workdir", str(DEFAULT_WORKDIR)))
    results_path = Path(opt("--out", str(DEFAULT_RESULTS)))
    suite_arg = opt("--suite", "")
    concurrency = int(opt("--concurrency", "0")) or None

    if "--plan" in argv:
        print(plan(count, seed))
        raise SystemExit(0)

    report = run_campaign(
        n=count,
        seed=seed,
        detector=detector,
        workdir=workdir,
        suite_path=Path(suite_arg) if suite_arg else None,
        max_concurrency=concurrency,
        refine="--no-refine" not in argv,
        results_path=results_path,
    )
    print(render_campaign(report))
    if report.results:
        written = save_results(report.results, results_path)
        print()
        print(f"results written to {written}")
    raise SystemExit(0 if report.scored else 1)
