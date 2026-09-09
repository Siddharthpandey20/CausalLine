"""
Scoring one run: work preserved, and whether we preserved something we
should not have.

This module is the **only** place in the codebase permitted to read
`Source.malicious`. That field is ground truth, authored by attack injection,
and src/provenance/ and src/recovery/ reaching for it would let the method
see the answer it is supposed to derive (see the warning on the model).
Everything here runs after the method has already committed to a set.

READ THIS BEFORE QUOTING AN UNSAFE-PRESERVATION NUMBER
------------------------------------------------------
Ground truth is the contamination walk seeded from the truly-malicious
sources. Our method is the contamination walk seeded from the detector's
verdict. On a trace where those walks use **the same influence edges**, the
two sets are identical by construction and the unsafe-preservation rate is
0.0 for reasons that have nothing to do with our method being good.

`data/runs/fake.jsonl` is exactly such a trace: it was authored with its
influence edges already correct, so it is a test of the plumbing, not of the
method. A real number needs a run where the influence edges were *estimated*
(self-report, counterfactual) and can therefore be wrong, while the true
edges are known separately because we wrote the attack.

`Score.ground_truth_is_circular` flags this so a number cannot be quoted by
accident. Do not remove it because it is inconvenient.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable

from src.eval.baselines import METHODS
from src.provenance.checks import CheckLedger
from src.provenance.contamination import contaminate
from src.tracing.logger import Trace


def malicious_sources(trace: Trace) -> set[str]:
    """Ground truth: the sources attack injection planted. Eval only."""
    return {s.id for s in trace.sources if s.malicious}


def ground_truth_events(
    trace: Trace,
    checked: set[tuple[str, str]] | None = None,
    true_influence: set[tuple[str, str]] | None = None,
) -> set[str]:
    """The events that truly became contaminated.

    Known by construction because we author the attacks (open issue #3). This
    is the yardstick every method is scored against.

    `true_influence` is what makes the yardstick independent. Given the real
    (source, event) influence relation -- which `src/eval/scripted.py` computes
    exactly, by leave-one-out -- the walk uses those edges and treats every other
    exposure pair as known-clean, because with the truth in hand there is nothing
    unexamined. Ground truth then owes nothing to the estimator, and where the
    estimator was wrong the two sets genuinely differ.

    Without it, ground truth walks the same estimated edges the method walks.
    Both sides then inflate and deflate together: a source the estimator wrongly
    called clean is absent from both walks, so the event it contaminated is
    missing from truth as well, and the unsafe preservation is not counted
    because ground truth agreed with the mistake. `checked` has the same
    property and is kept for the same reason -- it must be given to both sides or
    to neither (D-024).
    """
    seeds = malicious_sources(trace)
    if not seeds:
        return set()
    if true_influence is not None:
        exposure_pairs = {
            (sid, e.id) for e in trace.events for sid in e.exposures
        }
        return set(
            contaminate(
                trace,
                seeds,
                influence=true_influence,
                checked=exposure_pairs - set(true_influence),
            ).events
        )
    return set(contaminate(trace, seeds, checked=checked).events)


@dataclass
class Score:
    """One method, on one run."""

    method: str
    total_events: int
    discarded: frozenset[str]
    truly_contaminated: frozenset[str]
    ground_truth_is_circular: bool = False
    # Which detector's verdict this method was given. A work-preserved figure
    # means something different under each one, so a score that does not carry
    # its detector cannot be read -- and pooling scores across detectors would
    # average an upper bound together with a measurement.
    detector: str = "unknown"
    detector_missed: frozenset[str] = frozenset()
    notes: list[str] = field(default_factory=list)

    @property
    def work_preserved(self) -> float:
        """Fraction of events kept rather than recomputed. The headline.

        Counted in events, never sources (D-012).
        """
        if not self.total_events:
            return 0.0
        return (self.total_events - len(self.discarded)) / self.total_events

    @property
    def unsafe_preservations(self) -> list[str]:
        """Truly-contaminated events this method kept.

        The dangerous error. Always reported, even when it is unflattering --
        especially then (docs/04).
        """
        return sorted(self.truly_contaminated - self.discarded)

    @property
    def over_discards(self) -> list[str]:
        """Clean events this method threw away. Waste, not danger."""
        return sorted(self.discarded - self.truly_contaminated)

    @property
    def is_safe(self) -> bool:
        return not self.unsafe_preservations


def score(
    trace: Trace,
    method: str,
    discarded: Iterable[str],
    truth: set[str] | None = None,
    detector: str = "unknown",
    missed: Iterable[str] = (),
    independent_truth: bool = False,
) -> Score:
    truth = ground_truth_events(trace) if truth is None else truth
    return Score(
        method=method,
        total_events=len(trace.events),
        discarded=frozenset(discarded),
        truly_contaminated=frozenset(truth),
        # Circular exactly when ground truth was derived from the same influence
        # edges the method walked. Only "ours" walks them; the baselines use
        # parent/topology edges and are unaffected either way.
        #
        # The old test was `bool(trace.influence) and method == "ours"`, which
        # asserted a permanent condition -- true of every trace the project could
        # produce -- and would have gone on printing CIRCULAR over real results
        # forever. Estimated edges are not sufficient to fix it either: if ground
        # truth reads the estimator's edges then it inherits the estimator's
        # mistakes and agrees with them. What breaks the circle is a separately
        # known influence relation, so that is what the flag asks about.
        ground_truth_is_circular=(method == "ours" and not independent_truth),
        detector=detector,
        detector_missed=frozenset(missed),
    )


def compare(
    trace: Trace,
    verdict: Any = None,
    checked: set[tuple[str, str]] | None = None,
    true_influence: set[tuple[str, str]] | None = None,
) -> list[Score]:
    """Run every method on one trace and score them against ground truth.

    `verdict` is the detector's output (`src/eval/detectors.py`), and it is the
    only channel by which anything about the attack reaches a method. It used to
    be a bare list of source ids defaulting to the true labels, which made the
    perfect-detector assumption docs/01-scope.md declares out of scope into the
    silent default -- present in every number the project produced, named in
    none of them. It now defaults to `Oracle`, which is the same behaviour
    wearing its own name, and it is recorded on every Score.

    `checked` is passed to both the method and ground truth, so the two are
    always computed under the same assumption. Giving one and not the other
    would compare two different definitions of contaminated and call the
    difference a result.
    """
    from src.eval.detectors import Oracle

    verdict = verdict if verdict is not None else Oracle().flag(trace)
    seeds = set(verdict.sources())
    missed = malicious_sources(trace) - seeds
    truth = ground_truth_events(trace, checked=checked, true_influence=true_influence)
    scores = []
    for name, fn in METHODS.items():
        discarded = (
            fn(trace, seeds, checked=checked) if name == "ours" else fn(trace, seeds)
        )
        scores.append(
            score(
                trace, name, discarded, truth=truth,
                detector=verdict.detector, missed=missed,
                independent_truth=true_influence is not None,
            )
        )
    return scores


def table(scores: list[Score]) -> str:
    """The docs/04 main result table, for one run."""
    head = f"{'method':<22}{'preserved':>11}{'discarded':>11}{'unsafe':>8}  notes"
    rows = [head, "-" * len(head)]
    for s in scores:
        unsafe = len(s.unsafe_preservations)
        note = "CIRCULAR" if s.ground_truth_is_circular else ""
        if unsafe:
            note = (note + " " if note else "") + f"kept {s.unsafe_preservations}"
        rows.append(
            f"{s.method:<22}"
            f"{s.work_preserved:>10.0%}"
            f"{len(s.discarded):>11}"
            f"{unsafe:>8}"
            f"  {note}"
        )
    if scores:
        first = scores[0]
        rows.append("")
        rows.append(f"detector: {first.detector}")
        if first.detector_missed:
            # Stated separately from the unsafe count because it is a different
            # failure: recovery cannot act on an incident it was not told about,
            # and reading these as a fault in the method would be wrong.
            rows.append(
                f"  MISSED {sorted(first.detector_missed)} -- everything these "
                f"influenced is preserved, and unsafely"
            )
    return "\n".join(rows)


@dataclass
class RecoveryScore:
    """One method, on one recovered run. The Phase 7 table row."""

    scenario: str
    variant: str
    method: str
    detector: str
    total_events: int
    discarded: int
    work_preserved: float
    recovery_tokens: int
    analysis_tokens: int
    replay_tokens: int
    pipeline_tokens: int
    unsafe_preservations: int
    unsafe_ids: tuple[str, ...] = ()
    # PAIR-LEVEL unsafe preservation: of the (source, event) influences that
    # really existed, the fraction the estimator called clean
    # (src/eval/influence_eval.py). Reported ALONGSIDE the event-level count
    # above, never instead of it, because they answer different questions and
    # routinely disagree:
    #
    #   event-level  did recovery keep an event that was truly contaminated?
    #                This is what a deployment experiences, and it is 0 across
    #                the campaign.
    #   pair-level   did the estimator clear a (source, event) pair that was
    #                really an influence? This runs 0.14-0.60 depending on
    #                ablation.
    #
    # A pair-level false clean does not have to become an event-level unsafe
    # preservation -- the event is often contaminated by another route -- so
    # quoting only the event-level 0% overstates how well the estimator works.
    # Quoting only the pair-level rate overstates the risk a deployment runs.
    # Both, always.
    pair_unsafe_rate: float = 0.0
    pair_false_negatives: int = 0
    pair_scored: int = 0
    task_success: bool = False
    wall_clock_s: float = 0.0
    storage_bytes: int = 0
    blast_radius_events: int = 0
    blast_radius_agents: int = 0
    escalations: int = 0
    notes: str = ""

    @property
    def recovery_success(self) -> bool:
        return self.task_success and self.unsafe_preservations == 0


def recovery_table(rows: list[RecoveryScore]) -> str:
    """The docs/04 main result table, filled from actual recovery runs."""
    head = (
        f"{'scenario':<10}{'variant':<14}{'method':<22}"
        f"{'preserved':>10}{'rec.tok':>9}{'anal':>7}{'replay':>8}"
        f"{'unsafe':>8}{'pairUNSF':>9}{'ok':>5}{'blast':>7}  notes"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        note = r.notes
        if r.unsafe_ids:
            note = (note + " " if note else "") + f"kept {list(r.unsafe_ids)}"
        lines.append(
            f"{r.scenario:<10}{r.variant:<14}{r.method:<22}"
            f"{r.work_preserved:>9.0%}"
            f"{r.recovery_tokens:>9}"
            f"{r.analysis_tokens:>7}"
            f"{r.replay_tokens:>8}"
            f"{r.unsafe_preservations:>8}"
            f"{r.pair_unsafe_rate:>8.0%} "
            f"{'Y' if r.recovery_success else 'n':>5}"
            f"{r.blast_radius_events:>7}"
            f"  {note}"
        )
    return "\n".join(lines)


# --- the complete cost of one recovery (Phase 9) ------------------------------
#
# docs/04 asks for recovery cost "split into analysis cost + replay cost", and
# that is what `RecoveryScore` above already carries. It is not the whole cost,
# and the two missing terms are missing in opposite directions:
#
#   storage       CausalLine's standing tax. It is paid on every run, attacked
#                 or not, and leaving it out is how a method that only ever
#                 costs tokens during an incident looks free. Open issue #8.
#   residual risk what we are still exposed to after choosing to preserve work
#                 rather than recompute it. Leaving it out makes "preserve
#                 everything" look optimal, since preserving is what costs
#                 nothing in tokens.
#
# `lost_legitimate_work` is the third: clean events a method discarded anyway.
# Priced in the same units as replay, because that is what re-doing them costs.


@dataclass(frozen=True)
class CostWeights:
    """Exchange rates between the four things a recovery spends.

    Not measured -- there is no principled conversion from a byte to a token,
    and pretending otherwise would put a fabricated constant at the centre of
    every comparison. They are *reporting* parameters: a result is quoted at a
    stated weight, and `src/eval/economics.py` sweeps them rather than picking
    one. The defaults below are the neutral choice (storage free, risk unpriced)
    so that `total_cost()` with default weights is exactly the token cost
    docs/04 already defines, and any difference from that number is visibly
    something a caller asked for.
    """

    # Tokens per stored byte. 0.0 means "report storage separately, do not fold
    # it into the token total".
    storage_weight: float = 0.0
    # Tokens per unit of residual risk (lambda). The price of being wrong,
    # expressed in the currency of being slow.
    risk_lambda: float = 0.0


@dataclass
class TotalCost:
    """One recovery, fully priced. Terms kept separate so the total can be
    re-weighted without re-running anything."""

    analysis_tokens: int = 0
    replay_tokens: int = 0
    storage_bytes: int = 0
    lost_legitimate_work: int = 0
    residual_risk: float = 0.0
    weights: CostWeights = field(default_factory=CostWeights)

    @property
    def storage_cost(self) -> float:
        return self.storage_bytes * self.weights.storage_weight

    @property
    def risk_cost(self) -> float:
        return self.weights.risk_lambda * self.residual_risk

    @property
    def token_cost(self) -> int:
        return self.analysis_tokens + self.replay_tokens + self.lost_legitimate_work

    @property
    def total(self) -> float:
        return self.token_cost + self.storage_cost + self.risk_cost

    def line(self) -> str:
        return (
            f"analysis={self.analysis_tokens} replay={self.replay_tokens} "
            f"lost_work={self.lost_legitimate_work} "
            f"storage={self.storage_bytes}B(->{self.storage_cost:.1f}) "
            f"risk={self.residual_risk:.3f}(->{self.risk_cost:.1f}) "
            f"TOTAL={self.total:.1f}"
        )


def total_cost(
    analysis_tokens: int,
    replay_tokens: int,
    storage_bytes: int = 0,
    lost_legitimate_work: int = 0,
    residual_risk: float = 0.0,
    weights: CostWeights | None = None,
) -> TotalCost:
    """The cost formula Phase 9 completes:

        total = analysis + replay
              + storage_bytes * storage_weight
              + lost_legitimate_work
              + lambda * residual_risk

    Every term is reported as well as summed. A single number here would hide
    which of them moved, and the storage and risk terms are precisely the ones
    whose weight is a choice rather than a measurement.
    """
    return TotalCost(
        analysis_tokens=analysis_tokens,
        replay_tokens=replay_tokens,
        storage_bytes=storage_bytes,
        lost_legitimate_work=lost_legitimate_work,
        residual_risk=residual_risk,
        weights=weights or CostWeights(),
    )


# DEAD CODE -- no caller, kept for reference.
# Convenience wrapper over `total_cost()` for a scored row. The economics
# report builds its Deployment objects directly from traces instead, so this
# has never been on a path that runs.
def cost_of(
    row: RecoveryScore,
    residual_risk: float = 0.0,
    lost_legitimate_work: int | None = None,
    weights: CostWeights | None = None,
) -> TotalCost:
    """`total_cost` for a scored recovery row.

    `lost_legitimate_work` defaults to 0 rather than being inferred from the
    row: a RecoveryScore knows how many events were discarded but not how many
    of those were clean, and guessing would make the term a function of the
    method's own claim instead of ground truth.
    """
    return total_cost(
        analysis_tokens=row.analysis_tokens,
        replay_tokens=row.replay_tokens,
        storage_bytes=row.storage_bytes,
        lost_legitimate_work=lost_legitimate_work or 0,
        residual_risk=residual_risk,
        weights=weights,
    )


if __name__ == "__main__":
    import sys

    from src.tracing.logger import read_trace

    args = sys.argv[1:]
    positional = [a for a in args if not a.startswith("--")]
    path = positional[0] if positional else "data/runs/fake.jsonl"

    trace = read_trace(path)
    trace.validate()

    truth_seeds = malicious_sources(trace)
    truth = ground_truth_events(trace)
    ledger = CheckLedger.from_trace(trace)

    print(f"trace          {path}  ({len(trace.events)} events)")
    print(f"planted        {sorted(truth_seeds)}")
    print(f"truly hit      {sorted(truth)}  ({len(truth)} events)")
    print(f"coverage       {ledger.coverage(trace).summary()}")
    if not trace.checks:
        print("               no `check` records: every exposure is unexamined,")
        print("               so every method below is really the conservative")
        print("               fallback wearing a different name")
    print()
    scores = compare(trace)
    print(table(scores))
    print()

    if any(s.ground_truth_is_circular for s in scores):
        print("CIRCULAR: ground truth and our method walked the same influence")
        print("edges, so 'unsafe 0' here tests the plumbing, not the method. A")
        print("real number needs estimated edges that are allowed to be wrong.")
