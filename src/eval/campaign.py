"""
The scaled campaign: the full matrix, thirty times, with confidence intervals.

Phase 13.4. docs/04 asks for "roughly 30 runs each" and everything up to now has
run the matrix once. A single run of a deterministic pipeline is not a
measurement with an error bar; it is one draw, and quoting it as a result
invites exactly the question a reviewer should ask.

WHAT VARIES BETWEEN REPETITIONS
--------------------------------
The seed, and only the seed. It feeds `ScriptedClient`, whose task answers are
a deterministic function of the sources -- so the *work* is identical every
time -- and whose self-report is deliberately imperfect at a known rate
(`self_report_false_negative_rate`, `self_report_false_positive_rate`). The
variation between repetitions is therefore variation in **which claims the
cheap stage got wrong**, which is precisely the thing the expensive stage
exists to catch and the thing an error bar on this method should describe.

What does not vary: the attack, the corpus, the pipeline, or the model. This is
not a study of model non-determinism -- D-026 measured that separately and
found a 100% text-level floor. Reporting these intervals as though they covered
live-model variance would be wrong, and the interval is labelled with what it
does cover.

THE CONTROL THAT MUST BE IN THE TABLE
--------------------------------------
`blind` flags nothing, so every method preserves everything and every truly
contaminated event is an unsafe preservation. It is in the detector list on
purpose: a metric that never comes out badly is not measuring anything, and a
campaign with no failing row has not demonstrated that it can report one.
"""

import json
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from src.eval.experiment import METHODS, SCENARIOS, VARIANTS, run_cell
from src.eval.metrics import RecoveryScore

# Registers "heuristic" and "classifier" in the detector registry. Imported for
# that side effect: the campaign's whole point in Phase 13.3's terms is that a
# non-simulated detector goes through the same socket as the simulated ones.
import src.eval.classifier_detector  # noqa: F401

DEFAULT_REPETITIONS = 30
BASE_SEED = 20260906

# oracle    the perfect detector docs/01-scope.md assumes: an upper bound
# heuristic a real classifier that has never seen a ground-truth label
# pessim.   localises to a channel, not an item: where exposure/influence pays
# blind     flags nothing: the control that proves the metric can fail
DEFAULT_DETECTORS: tuple[str, ...] = ("oracle", "heuristic", "pessimistic", "blind")

# Metrics averaged across repetitions. Every one of them is already on
# RecoveryScore; nothing new is computed here, so a number in the campaign
# table and the same number in a single run mean the same thing.
METRIC_FIELDS: tuple[str, ...] = (
    "work_preserved",
    "recovery_tokens",
    "analysis_tokens",
    "replay_tokens",
    "unsafe_preservations",
    "pair_unsafe_rate",
    "blast_radius_events",
    "blast_radius_agents",
    "escalations",
)

# Two-sided 95% t multipliers. A small table rather than a scipy dependency
# (CLAUDE.md: ask before adding one). Anything larger falls back to the normal
# approximation, which is what t converges to.
_T95: dict[int, float] = {
    2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447, 8: 2.365,
    9: 2.306, 10: 2.262, 12: 2.201, 15: 2.145, 20: 2.093, 25: 2.064,
    30: 2.045, 40: 2.023, 60: 2.001, 120: 1.980,
}


def _t95(n: int) -> float:
    if n < 2:
        return 0.0
    for size in sorted(_T95):
        if n <= size:
            return _T95[size]
    return 1.96


@dataclass
class Interval:
    """Mean and 95% CI of one metric over the repetitions.

    `half_width` is zero when every repetition agreed, which happens often here
    and is information rather than a missing error bar: it says the metric did
    not depend on which self-report errors occurred.
    """

    metric: str
    n: int
    mean: float
    stdev: float
    half_width: float
    minimum: float
    maximum: float

    @property
    def low(self) -> float:
        return self.mean - self.half_width

    @property
    def high(self) -> float:
        return self.mean + self.half_width

    def render(self, percent: bool = False) -> str:
        if percent:
            return f"{self.mean:.1%} +/- {self.half_width:.1%}"
        if self.half_width == 0:
            return f"{self.mean:.4g}"
        return f"{self.mean:.4g} +/- {self.half_width:.3g}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "n": self.n,
            "mean": self.mean,
            "stdev": self.stdev,
            "ci95_half_width": self.half_width,
            "ci95_low": self.low,
            "ci95_high": self.high,
            "min": self.minimum,
            "max": self.maximum,
        }


def summarise(metric: str, values: Sequence[float]) -> Interval:
    values = [float(v) for v in values]
    n = len(values)
    if not n:
        return Interval(metric, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if n > 1 else 0.0
    half = _t95(n) * stdev / math.sqrt(n) if n > 1 else 0.0
    return Interval(metric, n, mean, stdev, half, min(values), max(values))


@dataclass
class CampaignCell:
    """One (scenario, variant, detector, method) cell, over all repetitions."""

    scenario: str
    variant: str
    detector: str
    method: str
    repetitions: int
    intervals: dict[str, Interval] = field(default_factory=dict)
    # Every repetition's unsafe-preservation count, kept unaggregated. A mean
    # of 0.03 unsafe preservations hides whether that was one run failing badly
    # or thirty failing slightly, and those are different findings.
    unsafe_per_run: list[int] = field(default_factory=list)
    task_successes: int = 0

    @property
    def any_unsafe(self) -> bool:
        return any(self.unsafe_per_run)

    @property
    def unsafe_run_rate(self) -> float:
        """Fraction of runs with at least one unsafe preservation.

        docs/04 defines the unsafe preservation rate as a fraction of *runs*,
        not a mean count, so that is what this reports.
        """
        if not self.unsafe_per_run:
            return 0.0
        return sum(1 for u in self.unsafe_per_run if u) / len(self.unsafe_per_run)

    @property
    def recovery_success_rate(self) -> float:
        return self.task_successes / self.repetitions if self.repetitions else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "variant": self.variant,
            "detector": self.detector,
            "method": self.method,
            "repetitions": self.repetitions,
            "unsafe_run_rate": self.unsafe_run_rate,
            "recovery_success_rate": self.recovery_success_rate,
            "unsafe_per_run": self.unsafe_per_run,
            "intervals": {k: v.to_dict() for k, v in self.intervals.items()},
        }


def _aggregate(rows: list[RecoveryScore], repetitions: int) -> CampaignCell:
    first = rows[0]
    cell = CampaignCell(
        scenario=first.scenario,
        variant=first.variant,
        detector=first.detector,
        method=first.method,
        repetitions=repetitions,
    )
    for metric in METRIC_FIELDS:
        cell.intervals[metric] = summarise(
            metric, [getattr(row, metric) for row in rows]
        )
    cell.unsafe_per_run = [row.unsafe_preservations for row in rows]
    cell.task_successes = sum(1 for row in rows if row.recovery_success)
    return cell


def run_campaign(
    repetitions: int = DEFAULT_REPETITIONS,
    detectors: Iterable[str] = DEFAULT_DETECTORS,
    scenarios: Iterable[str] = SCENARIOS,
    estimator_mode: str = "hybrid",
    workdir: str | Path = "data/runs/campaign",
    base_seed: int = BASE_SEED,
    progress: bool = True,
) -> list[CampaignCell]:
    """The full matrix, `repetitions` times each.

    Traces go to one directory and are overwritten between repetitions on
    purpose: thirty repetitions of six cells across four detectors is 720 runs
    and roughly 2GB of traces if all are kept, which is not a thing to leave in
    a student repository. The *scores* are what the campaign produces, and they
    are written to `data/results/`.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cells: dict[tuple[str, str, str, str], list[RecoveryScore]] = {}
    started = time.time()
    total = len(list(scenarios)) * len(VARIANTS) * len(list(detectors)) * repetitions
    done = 0

    for detector in detectors:
        for scenario in scenarios:
            for variant_name, influencing in VARIANTS:
                for rep in range(repetitions):
                    rows = run_cell(
                        scenario,
                        influencing,
                        detector_name=detector,
                        estimator_mode=estimator_mode,
                        workdir=workdir,
                        seed=base_seed + rep,
                    )
                    for row in rows:
                        key = (row.scenario, row.variant, row.detector, row.method)
                        cells.setdefault(key, []).append(row)
                    done += 1
                    if progress and done % 10 == 0:
                        elapsed = time.time() - started
                        rate = done / elapsed if elapsed else 0
                        print(
                            f"  {done}/{total} cells "
                            f"({elapsed:.0f}s elapsed, "
                            f"~{(total - done) / rate:.0f}s left)",
                            flush=True,
                        )
    return [_aggregate(rows, repetitions) for rows in cells.values()]


# --- reporting ----------------------------------------------------------------


def render(cells: list[CampaignCell]) -> str:
    """The docs/04 main result table, with intervals."""
    head = (
        f"{'det':<12}{'scen':<6}{'variant':<14}{'method':<22}"
        f"{'work preserved':>20}{'recovery tokens':>20}"
        f"{'unsafe%':>9}{'pairUNSF%':>11}{'ok%':>7}"
    )
    lines = [head, "-" * len(head)]
    order = {name: i for i, name in enumerate(METHODS)}
    for cell in sorted(
        cells,
        key=lambda c: (c.detector, c.scenario, c.variant, order.get(c.method, 99)),
    ):
        lines.append(
            f"{cell.detector:<12}{cell.scenario:<6}{cell.variant:<14}"
            f"{cell.method:<22}"
            f"{cell.intervals['work_preserved'].render(percent=True):>20}"
            f"{cell.intervals['recovery_tokens'].render():>20}"
            f"{cell.unsafe_run_rate:>8.0%} "
            f"{cell.intervals['pair_unsafe_rate'].mean:>10.0%} "
            f"{cell.recovery_success_rate:>6.0%}"
        )
    return "\n".join(lines)


def render_headline(cells: list[CampaignCell]) -> str:
    """CausalLine against B1, which docs/04 names as the baseline to beat."""
    lines = [
        "CausalLine vs B1 (agent taint), work preserved, mean +/- 95% CI",
        f"{'det':<12}{'scen':<6}{'variant':<14}"
        f"{'CausalLine':>20}{'B1':>20}{'gain':>10}{'unsafe%':>9}{'pairUNSF%':>11}",
        "-" * 102,
    ]
    keyed = {
        (c.detector, c.scenario, c.variant, c.method): c for c in cells
    }
    for detector in sorted({c.detector for c in cells}):
        for scenario in sorted({c.scenario for c in cells}):
            for variant, _ in VARIANTS:
                ours = keyed.get((detector, scenario, variant, "CausalLine"))
                b1 = keyed.get((detector, scenario, variant, "B1 agent taint"))
                if not (ours and b1):
                    continue
                gain = (
                    ours.intervals["work_preserved"].mean
                    - b1.intervals["work_preserved"].mean
                )
                lines.append(
                    f"{detector:<12}{scenario:<6}{variant:<14}"
                    f"{ours.intervals['work_preserved'].render(percent=True):>20}"
                    f"{b1.intervals['work_preserved'].render(percent=True):>20}"
                    f"{gain:>9.1%} {ours.unsafe_run_rate:>8.0%}"
                    f"{ours.intervals['pair_unsafe_rate'].mean:>10.0%}"
                )
    return "\n".join(lines)


def save(cells: list[CampaignCell], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([c.to_dict() for c in cells], indent=2), encoding="utf-8"
    )
    return out


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    repetitions = DEFAULT_REPETITIONS
    if "--reps" in args:
        repetitions = int(args[args.index("--reps") + 1])
    detectors = DEFAULT_DETECTORS
    if "--detectors" in args:
        detectors = tuple(args[args.index("--detectors") + 1].split(","))

    cells_total = len(SCENARIOS) * len(VARIANTS) * len(detectors) * repetitions
    print(
        f"campaign: {len(SCENARIOS)} scenarios x {len(VARIANTS)} variants x "
        f"{len(detectors)} detectors x {repetitions} repetitions "
        f"= {cells_total} cells, {cells_total * 5} pipeline runs"
    )
    print(f"detectors: {list(detectors)}")
    print("offline: scripted agent, no API calls, no quota.")
    print()

    started = time.time()
    cells = run_campaign(repetitions=repetitions, detectors=detectors)
    elapsed = time.time() - started

    print()
    print(render(cells))
    print()
    print(render_headline(cells))
    print()
    out = save(cells, "data/results/campaign.json")
    print(f"{len(cells)} cells over {repetitions} repetitions in {elapsed:.0f}s")
    print(f"written to {out}")
    print()
    print("Intervals cover variation in which self-report claims were wrong --")
    print("the scripted agent's error rates are knobs and the seed moves them.")
    print("They do NOT cover live-model variance; D-026 measured that apart.")
    failing = [c for c in cells if c.any_unsafe]
    if failing:
        print()
        print(f"{len(failing)} cell(s) recorded an unsafe preservation:")
        for cell in sorted(failing, key=lambda c: -c.unsafe_run_rate)[:12]:
            print(
                f"  {cell.detector:<12}{cell.scenario}-{cell.variant:<14}"
                f"{cell.method:<22}{cell.unsafe_run_rate:.0%} of runs"
            )
    else:
        print()
        print("No cell recorded an unsafe preservation, INCLUDING the blind")
        print("control -- which would be suspicious. Check the control fired.")


# --- detector sensitivity sweep (Phase 7.3) -----------------------------------
#
# The experiment the detector socket was built to support, and which had never
# been run. `Simulated` degrades a perfect verdict at a KNOWN miss rate and
# false-positive rate, which is the only way to ask "how does CausalLine
# degrade as detection degrades" and get an answer rather than an anecdote.
#
# A missed source is the dangerous failure and it is not recoverable by any
# method: everything it influenced is preserved, and unsafely. So the expected
# shape is unsafe preservations rising roughly with the miss rate, for every
# method including ours. Reporting that CausalLine is unaffected would mean the
# sweep was not wired to anything.
#
# Needs no API quota: `Simulated` is a function of the ground-truth labels.

DEFAULT_MISS_RATES: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)


def detector_sweep(
    miss_rates: Iterable[float] = DEFAULT_MISS_RATES,
    false_positive_rate: float = 0.0,
    repetitions: int = 5,
    scenarios: Iterable[str] = SCENARIOS,
    workdir: str | Path = "data/runs/sweep",
    base_seed: int = BASE_SEED,
) -> list[CampaignCell]:
    """Run the matrix against `Simulated` at each miss rate."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cells: dict[tuple[str, str, str, str], list[RecoveryScore]] = {}

    for miss in miss_rates:
        for scenario in scenarios:
            for _variant_name, influencing in VARIANTS:
                for rep in range(repetitions):
                    rows = run_cell(
                        scenario,
                        influencing,
                        detector_name="simulated",
                        estimator_mode="hybrid",
                        workdir=workdir,
                        seed=base_seed + rep,
                        miss_rate=miss,
                        false_positive_rate=false_positive_rate,
                    )
                    for row in rows:
                        key = (str(miss), row.variant, row.method, row.scenario)
                        cells.setdefault(key, []).append(row)

    out: list[CampaignCell] = []
    for (miss, _variant, _method, _scenario), rows in cells.items():
        cell = _aggregate(rows, len(rows))
        cell.detector = f"miss={miss}"
        out.append(cell)
    return out


def render_sweep(cells: list[CampaignCell]) -> str:
    """Work preserved and unsafe preservation against detector miss rate."""
    by_key: dict[tuple[str, str], list[CampaignCell]] = {}
    for cell in cells:
        by_key.setdefault((cell.detector, cell.method), []).append(cell)

    misses = sorted(
        {c.detector for c in cells}, key=lambda d: float(d.split("=")[1])
    )
    methods = [m for m in METHODS]
    head = f"{'method':<22}" + "".join(f"{m:>22}" for m in misses)
    lines = [
        "work preserved / unsafe-run-rate, by detector miss rate",
        head,
        "-" * len(head),
    ]
    for method in methods:
        row = f"{method:<22}"
        for miss in misses:
            group = by_key.get((miss, method), [])
            if not group:
                row += f"{'-':>22}"
                continue
            wp = statistics.fmean(
                [c.intervals["work_preserved"].mean for c in group]
            )
            un = statistics.fmean([c.unsafe_run_rate for c in group])
            row += f"{wp:>13.0%} /{un:>7.0%}"
        lines.append(row)
    return "\n".join(lines)
