"""
Detection, as a black box we can degrade on purpose.

CausalLine starts *after* detection. The specification is explicit that we do
not build a detector -- it "consumes detector output as an input; treat detection
as a pluggable, replaceable black box throughout" -- so what this module provides
is not a detector but a **socket** for one, plus simulated occupants for that
socket whose failure modes we control.

WHY GROUND TRUTH IS ALLOWED IN HERE, AND ONLY HERE
--------------------------------------------------
`metrics.py` says it is the only module permitted to read `Source.malicious`.
This is the second, and the exception is narrow and load-bearing: a *simulated*
detector is by definition a function of the truth, because there is nothing else
to simulate it from. The rule that keeps this honest is the boundary, not the
data:

    a detector reads ground truth and emits a Verdict
    everything downstream reads the Verdict and never the truth

So `src/provenance/` and `src/recovery/` still never see a label, and the
previous arrangement -- where `compare()` passed the planted source ids straight
through as "the detector's verdict" -- becomes one option among several rather
than the only behaviour. That passthrough is `Oracle` below. Keeping it named
and explicit matters: docs/01-scope.md assumes a perfect detector and does not
build one, and an assumption with a name in the results table can be argued
with, while an assumption buried in a default cannot.

WHAT A VERDICT CARRIES BEYOND "WHICH SOURCES"
--------------------------------------------
Three things, because CausalLine's sensitivity to each is a separate question:

  * **confidence** per source, so `MaliciousSources` can be a threshold rather
    than a set, and so the threshold itself becomes an axis.
  * **misses**, because a missed source is the only detector failure that can
    cause an unsafe preservation. Recovery cannot recover from an incident it is
    never told about, and a run where the detector missed should show that
    plainly rather than being excluded.
  * **latency**, expressed as the event at which the alarm fires. A slow
    detector means more work exists downstream of the injection by the time
    recovery starts -- which is precisely the condition under which selective
    recovery should beat restarting, so a method evaluated only at zero latency
    is evaluated where it has least to prove.
"""

import random
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Verdict:
    """A detector's output. The only thing recovery is permitted to read.

    `flagged` maps source id to confidence. Sources absent from it were not
    flagged, which is different from being cleared -- a detector says nothing
    about the sources it did not name, exactly as a `checked` record
    distinguishes clean from unchecked.
    """

    detector: str
    flagged: dict[str, float] = field(default_factory=dict)
    threshold: float = 0.5
    # Event id at which the alarm fires. None means "at the end of the run",
    # i.e. the whole trace exists before recovery begins.
    detected_at: str | None = None
    latency_events: int = 0
    notes: tuple[str, ...] = ()

    def sources(self, threshold: float | None = None) -> list[str]:
        """Flagged sources at or above the threshold. This is `MaliciousSources`."""
        cut = self.threshold if threshold is None else threshold
        return sorted(sid for sid, c in self.flagged.items() if c >= cut)

    def confidence(self, source_id: str) -> float:
        return self.flagged.get(source_id, 0.0)

    def describe(self) -> str:
        listed = ", ".join(
            f"{sid}@{self.flagged[sid]:.2f}" for sid in sorted(self.flagged)
        ) or "nothing"
        at = self.detected_at or "end of run"
        return f"{self.detector}: flagged {listed} (threshold {self.threshold:.2f}) at {at}"


@runtime_checkable
class Detector(Protocol):
    name: str

    def flag(self, trace: Any) -> Verdict: ...


def _planted(trace: Any) -> list[str]:
    """Ground truth, read here and nowhere downstream."""
    return sorted(s.id for s in trace.sources if getattr(s, "malicious", False))


def _detection_point(trace: Any, flagged: list[str], latency_events: int) -> str | None:
    """The event at which the alarm fires, `latency_events` after first exposure.

    Measured from the first event that had a flagged source in its context,
    because that is the earliest moment a detector could possibly have noticed.
    Returning None means the alarm came after the last recorded event, so the
    whole run is available to recovery -- which is the zero-information case and
    also the most favourable one for the baselines.
    """
    if not flagged:
        return None
    marked = set(flagged)
    for index, event in enumerate(trace.events):
        if marked & set(event.exposures):
            target = index + latency_events
            if target >= len(trace.events):
                return None
            return trace.events[target].id
    return None


@dataclass
class Oracle:
    """Flags exactly the planted sources, with full confidence.

    The perfect detector docs/01-scope.md assumes. Every number produced under
    this detector is an upper bound on what CausalLine can do, and should be
    labelled as one: it is the condition in which the method is handed a correct
    and complete incident report, which no real deployment gets.
    """

    name: str = "oracle"
    threshold: float = 0.5

    def flag(self, trace: Any) -> Verdict:
        planted = _planted(trace)
        return Verdict(
            detector=self.name,
            flagged={sid: 1.0 for sid in planted},
            threshold=self.threshold,
            notes=("perfect detector; an upper bound, not a measurement",),
        )


@dataclass
class Simulated:
    """A detector with a miss rate, a false-positive rate, and a latency.

    Derived from ground truth and then degraded, which is the only way to get a
    detector whose error rates are *known* -- and knowing them is the whole point,
    because it turns "how good is CausalLine" into "how does CausalLine degrade as
    detection degrades", which is a question with an answer.

    `miss_rate` is the dangerous knob. A missed source is never contaminated, so
    everything it influenced is preserved, and every one of those preservations is
    unsafe. That is not a flaw in recovery and must not be reported as one: it is
    the cost of a detector that did not fire, and the reason the metric is
    reported per-detector rather than pooled.

    `false_positive_rate` flags a clean source. Costs recomputation, never safety
    -- the mirror of the estimator's own asymmetry.
    """

    miss_rate: float = 0.0
    false_positive_rate: float = 0.0
    latency_events: int = 0
    # Confidence is what the threshold axis acts on. True positives are drawn
    # high and false positives low, but the ranges overlap on purpose: a
    # detector whose confidence perfectly separated its own errors would make
    # the threshold sweep meaningless.
    true_confidence: tuple[float, float] = (0.6, 1.0)
    false_confidence: tuple[float, float] = (0.5, 0.8)
    threshold: float = 0.5
    seed: int = 20260906
    name: str = "simulated"

    def flag(self, trace: Any) -> Verdict:
        rng = random.Random(self.seed)
        planted = set(_planted(trace))
        flagged: dict[str, float] = {}
        notes: list[str] = []

        for source in trace.sources:
            truly = source.id in planted
            if truly:
                if rng.random() < self.miss_rate:
                    notes.append(f"MISSED {source.id}: any work it influenced will be preserved unsafely")
                    continue
                flagged[source.id] = round(rng.uniform(*self.true_confidence), 2)
            elif rng.random() < self.false_positive_rate:
                flagged[source.id] = round(rng.uniform(*self.false_confidence), 2)
                notes.append(f"false positive {source.id}: costs recomputation, not safety")

        above = sorted(sid for sid, c in flagged.items() if c >= self.threshold)
        return Verdict(
            detector=self.name,
            flagged=flagged,
            threshold=self.threshold,
            detected_at=_detection_point(trace, above, self.latency_events),
            latency_events=self.latency_events,
            notes=tuple(notes),
        )


@dataclass
class Blind:
    """Flags nothing. The control that shows what the metrics look like when no
    incident is reported at all: every method preserves everything, and every
    truly contaminated event is an unsafe preservation.

    Worth running because it is the only condition that proves the scoring is
    capable of reporting failure. A metric that never comes out badly is not
    measuring anything.
    """

    name: str = "blind"
    threshold: float = 0.5

    def flag(self, trace: Any) -> Verdict:
        return Verdict(
            detector=self.name,
            threshold=self.threshold,
            notes=("flags nothing; every contaminated event survives",),
        )


@dataclass
class Pessimistic:
    """Flags every source of the same kind as any planted one.

    Stands in for a detector that localises an incident to a channel but not to
    an item -- "something in the web results was poisoned". Realistic, and it is
    the condition where the exposure/influence distinction should pay most,
    because the seed set is large and mostly clean.
    """

    name: str = "pessimistic"
    threshold: float = 0.5
    confidence: float = 0.7

    def flag(self, trace: Any) -> Verdict:
        planted = set(_planted(trace))
        kinds = {s.kind for s in trace.sources if s.id in planted}
        flagged = {
            s.id: self.confidence for s in trace.sources if s.kind in kinds
        }
        return Verdict(
            detector=self.name,
            flagged=flagged,
            threshold=self.threshold,
            notes=(f"flagged every source of kind(s) {sorted(kinds)}",),
        )


DETECTORS: dict[str, Any] = {
    "oracle": Oracle,
    "blind": Blind,
    "pessimistic": Pessimistic,
    "simulated": Simulated,
}


def build(name: str, **kwargs: Any) -> Detector:
    """Construct a detector by name, for the experiment runner's config."""
    if name not in DETECTORS:
        raise ValueError(f"unknown detector {name!r}; have {sorted(DETECTORS)}")
    return DETECTORS[name](**kwargs)


if __name__ == "__main__":
    import sys

    from src.tracing.logger import read_trace

    path = sys.argv[1] if len(sys.argv) > 1 else "data/runs/fake.jsonl"
    trace = read_trace(path)

    print(f"trace {path}  ({len(trace.events)} events, {len(trace.sources)} sources)")
    print(f"planted (ground truth, eval only): {_planted(trace)}")
    print()
    for name, factory in DETECTORS.items():
        verdict = factory().flag(trace)
        print(f"{name:14} {verdict.describe()}")
        for note in verdict.notes:
            print(f"{'':14}   {note}")
    print()
    print("with latency and a 50% miss rate:")
    for miss in (0.0, 0.5, 1.0):
        verdict = Simulated(miss_rate=miss, latency_events=3).flag(trace)
        print(f"  miss={miss:<4} {verdict.describe()}")
