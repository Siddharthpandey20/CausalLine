"""
Fixed-budget attribution, for when sparsity fails.

Phase 11.3, and the lowest-priority piece of Phase 11 by design. It exists
because group testing (11.2) rests on an assumption that its own diagnostics
can watch failing: influential sources are *sparse*. When most groups come back
"mattered", recursive halving degenerates -- it pays for the splits and then
tests nearly every candidate anyway, ending up worse than leave-one-out. Our
own measurement shows this happening on the Coder's decision event, where three
of five sources genuinely matter.

THE APPROACH, AND WHOSE IT IS
-----------------------------
Cohen-Wang et al., "ContextCite: Attributing Model Generation to Context"
(USENIX Security 2025). Rather than removing sources one at a time or by
halves, spend a **fixed** budget of calls -- 16 to 32 -- each removing a random
subset of the candidates, then fit a linear surrogate from "which sources were
present" to "did the decision hold", and read each source's contribution off
the fitted weights. It is LIME's construction with the context as the feature
space.

What it buys: cost that does not depend on n. Thirty-two calls answers a
thirty-candidate event and a three-hundred-candidate one. What it costs:
the answer is a *fitted estimate* rather than a per-source observation, so it
comes with no guarantee that a source with a small weight had no effect.

WHY IT IS A FALLBACK AND NOT THE DEFAULT
-----------------------------------------
On our own events n is 5 to 11. A fixed budget of 16-32 calls is *more*
expensive than leave-one-out at that size, and the estimate is weaker than the
direct observation it replaces. The budget only wins where n is large and
dense, which is a regime this testbed does not reach. Invoking it by default
would pay 32 calls to answer questions 8 calls already answer exactly.

So the entry point is `attribute_if_sparsity_failed()`, which runs group
testing first and only falls back when *group testing's own diagnostics* say
its assumption is not holding.

THE SIGNAL BEING FITTED, AND WHY IT TRANSFERS BADLY
---------------------------------------------------
Binary: did the decision signature survive this subset removal. Context-Cite
fits *log-probabilities*, which it can because it has the model's logits. This
project decided in D-026 that text-level comparison is unusable and that a
canonicalised decision signature is what we have, and a signature either
matches or does not.

That substitution is not free, and the measured cost of it is larger than the
docstring of a faithful port would suggest. Two compounding problems, both
visible in `python -m src.provenance.budget_attribution`:

  * **one bit per call.** A log-probability tells you how much a removal moved
    the answer; a signature match tells you only whether it moved. So a budget
    that is ample for Context-Cite is thin here.
  * **class imbalance from the OR structure.** The decision holds only when
    *no* influential source was removed. At a keep fraction of 1/2 and k truly
    influential sources, that happens with probability 2^-k -- one row in
    sixteen at k=4. Most of a 32-call budget then lands on rows that all say
    the same thing, and the fit has almost no contrast to work with.

The consequence, measured rather than feared: this fallback recovers sparse
sets at small n and fails at k/n = 0.5 whatever the budget. `r_squared` drops
when it fails, which is what it is reported for. Fixing it properly needs a
graded signal -- how *far* the signature moved, facet by facet -- which is a
different piece of work and is named in docs/06-limitations.md rather than
half-done here.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

# Context-Cite's own experiments use 32 calls as the default budget and show
# most of the accuracy arriving by 16. Both are offered; neither is tuned.
DEFAULT_BUDGET = 32
MIN_BUDGET = 16

# Fraction of candidates kept in each random subset. Half is the standard LIME
# choice and it maximises the variance of the design matrix, which is what a
# least-squares fit needs to separate the columns.
KEEP_FRACTION = 0.5


@dataclass
class SourceWeight:
    """One source's fitted contribution."""

    source_id: str
    weight: float
    times_present: int
    times_absent: int

    def line(self) -> str:
        return (
            f"{self.source_id:<6} weight={self.weight:+.3f} "
            f"(present {self.times_present}, absent {self.times_absent})"
        )


@dataclass
class BudgetAttribution:
    """What a fixed-budget pass concluded, and how well it fitted.

    `r_squared` is reported because a fit nobody looked at is a number with no
    error bar. A low value means the linear surrogate did not explain the
    observations, and the weights below it should not be acted on -- which is a
    thing the caller can only know if it is told.
    """

    influential: list[str] = field(default_factory=list)
    weights: list[SourceWeight] = field(default_factory=list)
    calls: int = 0
    tokens: int = 0
    threshold: float = 0.0
    r_squared: float = 0.0
    iterations: int = 0
    degenerate: bool = False
    notes: list[str] = field(default_factory=list)

    def weight_of(self, source_id: str) -> float:
        for entry in self.weights:
            if entry.source_id == source_id:
                return entry.weight
        return 0.0

    def line(self) -> str:
        return (
            f"budget attribution: {len(self.influential)} influential of "
            f"{len(self.weights)} in {self.calls} calls, "
            f"threshold={self.threshold:.3f}, R^2={self.r_squared:.2f}"
            + ("  DEGENERATE" if self.degenerate else "")
        )


def _design(
    candidates: Sequence[str], budget: int, seed: int
) -> list[list[int]]:
    """Random present/absent masks, one row per call.

    Two rows are added deliberately and are not random: all-present (the
    original request, which must hold the decision) and all-absent (everything
    removed, which must break it if anything does). They anchor the fit at both
    ends, and without them a budget that happened to draw similar subsets can
    produce a design matrix with no contrast at all.
    """
    import random

    rng = random.Random(seed)
    n = len(candidates)
    rows: list[list[int]] = [[1] * n, [0] * n]
    while len(rows) < budget:
        row = [1 if rng.random() < KEEP_FRACTION else 0 for _ in range(n)]
        rows.append(row)
    return rows[:budget]


def _lasso(
    X: Any,
    y: Any,
    lambda_fraction: float = 0.05,
    max_iterations: int = 500,
    tolerance: float = 1e-7,
) -> tuple[Any, float, int]:
    """L1-penalised least squares by coordinate descent. (weights, intercept, iters)

    Minimises  ||y - Xw - b||^2 / (2m)  +  lambda * ||w||_1.

    `lambda_fraction` is a fraction of lambda_max, the smallest penalty at which
    every weight is zero. Expressing it that way makes it scale-free -- the same
    number means the same thing for a 5-source event and a 300-source one -- and
    it is a conventional default, not a value tuned until a scenario came out
    right. Nothing in this file adjusts it per event.

    Written out rather than imported so the project does not take a dependency
    on scikit-learn for thirty lines of arithmetic (CLAUDE.md: ask before adding
    a dependency).
    """
    import numpy as np

    m, n = X.shape
    intercept = float(y.mean())
    centred_y = y - intercept
    # Column norms; a constant column (a source kept in every draw) carries no
    # information and its weight stays at zero rather than dividing by zero.
    norms = (X ** 2).sum(axis=0)
    lambda_max = float(np.abs(X.T @ centred_y).max()) / m if m else 0.0
    penalty = lambda_fraction * lambda_max

    weights = np.zeros(n)
    residual = centred_y.copy()
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        largest_step = 0.0
        for j in range(n):
            if norms[j] == 0:
                continue
            old = weights[j]
            rho = float(X[:, j] @ (residual + X[:, j] * old))
            # Soft threshold.
            shrunk = max(abs(rho) / m - penalty, 0.0) * (1.0 if rho >= 0 else -1.0)
            new = shrunk * m / norms[j]
            if new != old:
                residual = residual - X[:, j] * (new - old)
                weights[j] = new
                largest_step = max(largest_step, abs(new - old))
        if largest_step < tolerance:
            break
    return weights, intercept, iterations


def attribute(
    candidates: Sequence[str],
    decision_fn: Callable[[Sequence[str]], bool],
    budget: int = DEFAULT_BUDGET,
    seed: int = 20260906,
    threshold: float | None = None,
) -> BudgetAttribution:
    """Fit a linear surrogate over `budget` random-subset removals.

    `decision_fn(removed_group) -> True when removing that group changes the
    decision` -- the same signature group testing uses, so both stages are
    driven by one `CounterfactualDecision` and a call-count comparison between
    them means something.

    The target is `1.0` when the decision **held** (the removal changed
    nothing) and `0.0` when it moved. A source whose presence predicts the
    decision holding gets a positive weight; that is the source that mattered.

    `threshold` defaults to half the largest fitted weight, which is a
    relative rule rather than an absolute one because the weights are in units
    of "fraction of decision variance", not tokens, and no absolute cut
    transfers between events.
    """
    import numpy as np

    result = BudgetAttribution()
    candidates = list(candidates)
    if not candidates:
        return result
    if budget < MIN_BUDGET:
        result.notes.append(
            f"budget {budget} is below the {MIN_BUDGET} Context-Cite reports as "
            "the point where accuracy stabilises; weights are noisier than the "
            "method's published behaviour"
        )

    rows = _design(candidates, budget, seed)
    targets: list[float] = []
    for row in rows:
        removed = [sid for sid, keep in zip(candidates, row) if not keep]
        if not removed:
            # The all-present anchor is the original request. Its answer is
            # known without asking, so it is not charged: paying a call to
            # confirm that removing nothing changes nothing would be a call
            # spent on arithmetic.
            targets.append(1.0)
            continue
        result.calls += 1
        targets.append(0.0 if bool(decision_fn(removed)) else 1.0)

    X = np.array(rows, dtype=float)
    y = np.array(targets, dtype=float)

    if float(y.std()) == 0.0:
        # Every subset gave the same answer, so no weight is identifiable and
        # fitting one would produce confident zeros. The all-present anchor is
        # always 1.0, so a constant target means *nothing ever moved the
        # decision* -- every candidate is clean under this comparator.
        #
        # That verdict is a clearance, and this module is not entitled to
        # issue one on a fit that failed. It reports the finding and clears
        # nobody; the caller's conservative fallback then keeps the pairs
        # contaminated, which costs work and never costs safety.
        result.degenerate = True
        result.notes.append(
            "every subset produced the same decision, so no weight is "
            "identifiable. No source is cleared on this evidence -- the "
            "caller's fallback decides, and unchecked stays contaminated."
        )
        result.influential = []
        result.weights = [
            SourceWeight(sid, 0.0, int(X[:, i].sum()), int(len(rows) - X[:, i].sum()))
            for i, sid in enumerate(candidates)
        ]
        return result

    # Lasso, by coordinate descent, which is the estimator Context-Cite uses
    # and it is used here for its reason rather than out of habit. With a
    # budget of 32 calls over 32 candidates the system is under-determined --
    # there are more sources than observations -- so least squares (and ridge,
    # which only shrinks) has no unique answer and returns a spread of small
    # positive weights that rank mostly by noise. That failure was measured
    # here before this line was written, not anticipated.
    #
    # The L1 penalty supplies the missing constraint, and it supplies exactly
    # the right one: it prefers solutions where few sources carry weight, which
    # is the sparsity assumption this whole stage is built on. Where the
    # assumption is false the fit degrades, which is what `r_squared` and the
    # non-zero count are reported for.
    weights, intercept, iterations = _lasso(X, y)
    fitted = X @ weights + intercept
    residual = float(((y - fitted) ** 2).sum())
    total = float(((y - y.mean()) ** 2).sum())
    result.r_squared = 1.0 - residual / total if total else 0.0
    result.iterations = iterations
    if len(candidates) >= budget:
        result.notes.append(
            f"{len(candidates)} candidates on a budget of {budget} calls: the "
            "system is under-determined and the fit leans on the sparsity "
            "penalty rather than on the observations. Budget should exceed n."
        )
    result.weights = [
        SourceWeight(
            source_id=sid,
            weight=float(weights[i]),
            times_present=int(X[:, i].sum()),
            times_absent=int(len(rows) - X[:, i].sum()),
        )
        for i, sid in enumerate(candidates)
    ]
    # The Lasso has already zeroed the sources it could not justify, so the
    # threshold only has to separate "carries weight" from "does not". Any
    # strictly positive weight qualifies; the relative cut is kept available
    # for a caller that wants to be stricter than the penalty was.
    largest = max((abs(w.weight) for w in result.weights), default=0.0)
    result.threshold = threshold if threshold is not None else 0.0
    result.influential = [
        w.source_id for w in result.weights if w.weight > result.threshold
    ]
    result.notes.append(
        f"lasso kept {len(result.influential)} of {len(candidates)} sources "
        f"(largest weight {largest:.3f}, {result.iterations} iterations)"
    )
    if result.r_squared < 0.5:
        result.notes.append(
            f"R^2={result.r_squared:.2f}: the linear surrogate explains little "
            "of the observed variation, so these weights are weak evidence. "
            "Interaction effects or an unstable signature both look like this."
        )
    return result


# --- the entry point: group testing first, this only if it fails --------------


@dataclass
class StagedAttribution:
    """The result of Phase 11.2 with Phase 11.3 behind it."""

    influential: list[str]
    stage: str  # "group_test" | "budget"
    group_calls: int = 0
    budget_calls: int = 0
    group_diagnostics: Any = None
    budget_result: BudgetAttribution | None = None

    @property
    def total_calls(self) -> int:
        return self.group_calls + self.budget_calls

    def line(self) -> str:
        return (
            f"decided by {self.stage} in {self.total_calls} calls "
            f"(group {self.group_calls}, budget {self.budget_calls}): "
            f"{self.influential or 'nothing influential'}"
        )


def attribute_if_sparsity_failed(
    candidates: Sequence[str],
    decision_fn: Callable[[Sequence[str]], bool],
    budget: int = DEFAULT_BUDGET,
    seed: int = 20260906,
    sparsity_threshold: float = 0.75,
) -> StagedAttribution:
    """Group-test first; fall back to a fixed budget only if sparsity failed.

    The trigger is group testing's own diagnostic -- the fraction of tested
    groups that came back "mattered" -- not a guess made in advance about how
    dense the event is. That is the condition Phase 11.3 specifies, and it is
    the only condition under which the fallback is cheaper than the thing it
    replaces.

    The group-testing calls already spent are **not** refunded and are reported
    in `total_calls`. Pretending the fallback cost only its own budget would be
    the same sunk-cost error `src/eval/economics.py` exists to prevent, one
    level down.
    """
    from src.provenance.group_test import GroupTestDiagnostics, group_test

    diagnostics = GroupTestDiagnostics()
    found = group_test(candidates, decision_fn, diagnostics)

    if not diagnostics.sparsity_failing(sparsity_threshold):
        return StagedAttribution(
            influential=found,
            stage="group_test",
            group_calls=diagnostics.calls,
            group_diagnostics=diagnostics,
        )

    fitted = attribute(candidates, decision_fn, budget=budget, seed=seed)
    return StagedAttribution(
        influential=fitted.influential,
        stage="budget",
        group_calls=diagnostics.calls,
        budget_calls=fitted.calls,
        group_diagnostics=diagnostics,
        budget_result=fitted,
    )


if __name__ == "__main__":
    import random

    print("Fixed-budget attribution against group testing, on synthetic sets.")
    print("The budget's cost is flat in n; group testing's is not. Where the")
    print("crossover sits is the whole argument for keeping this as a fallback.")
    print()
    print(
        f"{'n':>5}{'k':>4}{'density':>9}{'GT':>6}"
        f"{'b=32':>7}{'ok':>5}{'R^2':>7}"
        f"{'b=2n':>7}{'ok':>5}{'R^2':>7}"
    )
    print("-" * 60)

    from src.provenance.group_test import GroupTestDiagnostics, group_test

    for n, k in ((8, 1), (8, 4), (16, 2), (16, 8), (32, 4), (32, 16)):
        rng = random.Random(4242)
        influential_ids = {f"S{i}" for i in rng.sample(range(n), k)}
        candidates = [f"S{i}" for i in range(n)]

        def decide(group, ids=influential_ids):
            return bool(set(group) & ids)

        diag = GroupTestDiagnostics()
        group_test(candidates, decide, diag)
        fixed = attribute(candidates, decide, budget=DEFAULT_BUDGET)
        ample = attribute(candidates, decide, budget=max(DEFAULT_BUDGET, 2 * n))
        print(
            f"{n:>5}{k:>4}{k/n:>9.2f}{diag.calls:>6}"
            f"{fixed.calls:>7}"
            f"{('Y' if set(fixed.influential) == influential_ids else 'n'):>5}"
            f"{fixed.r_squared:>7.2f}"
            f"{ample.calls:>7}"
            f"{('Y' if set(ample.influential) == influential_ids else 'n'):>5}"
            f"{ample.r_squared:>7.2f}"
        )
    print()
    print("Two things this table says, and both belong in the writeup.")
    print()
    print("1. The budget is flat in n and group testing is not, but at OUR n")
    print("   (5-11 exposures per event) 32 calls is the more expensive of the")
    print("   two AND the weaker: a fitted weight is not an observation. That")
    print("   is why this is invoked on group testing's diagnostics, never by")
    print("   default.")
    print("2. Density beats budget. Raising the budget to 2n does not rescue")
    print("   k/n = 0.5, and barely moves n=32,k=4. The reason is structural,")
    print("   not a shortage of calls: the decision holds only when NO")
    print("   influential source was removed, which at keep=1/2 happens with")
    print("   probability 2^-k, so most rows of the design carry the same")
    print("   answer and the fit has nothing to separate. Context-Cite avoids")
    print("   this with continuous log-probabilities; a binary signature")
    print("   cannot. R^2 falls when this bites, which is what it is for.")
