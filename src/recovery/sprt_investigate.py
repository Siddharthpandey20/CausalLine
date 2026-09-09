"""
Anytime investigation: decide as soon as there is enough evidence, not after
checking everything.

Phase 10. This is the structural fix for the sunk-cost problem
`src/eval/economics.py` describes. That module shows the *arithmetic* is
different before and after A is spent; this module makes A stop being a single
lump you must commit to in advance.

THE PROBLEM
-----------
Investigation was all-or-nothing. You either ran the whole counterfactual pass
and then decided, or you skipped it. If the pass ran to completion and revealed
that contamination had spread across most of the trace, the entire analysis
budget was wasted -- restarting from the beginning would have been cheaper, and
that was knowable long before the last check.

THE FIX
-------
Each candidate contaminated node is checked one at a time, and each check is a
Bernoulli observation (tainted / clean). After every observation, test two
hypotheses about the true contamination fraction f:

    H0: f <  f_star        selective recovery is worth finishing
    H1: f >= f_star_high   contamination is spreading; abort and restart now

and stop as soon as the accumulated evidence favours one decisively. Wald's
sequential probability ratio test is the rule, and it is used *as specified*:
the SPRT minimises expected sample size for given error constraints among all
tests with those error rates (Wald & Wolfowitz 1948). There is no better
stopping rule to invent here, so none is invented.

    Lambda_n = PROD_i  p1(x_i) / p0(x_i)
    accept H1 (ABORT)   when Lambda_n >= A = (1 - beta) / alpha
    accept H0 (PROCEED) when Lambda_n <= B = beta / (1 - alpha)
    otherwise            keep checking

    alpha = P(abort   | H0 true)  -- wrongly abandoning a cheap recovery
    beta  = P(proceed | H1 true)  -- wrongly continuing a hopeless one

A NOTE ON WHICH THRESHOLD MEANS WHICH DECISION
-----------------------------------------------
Under Wald's convention the likelihood ratio has the *alternative* on top, so
crossing the **upper** threshold A accepts H1 and crossing the **lower**
threshold B accepts H0. With H1 = "abort and restart", that makes the upper
threshold the abort boundary and the lower one the proceed boundary, and that
is how it is implemented here. The constants themselves are exactly as
specified -- A = (1-beta)/alpha, B = beta/(1-alpha). If you flip the ratio to
put H0 on top, both the thresholds and the decisions swap; the test is the
same. The boundaries are named `abort_threshold` and `proceed_threshold` below
rather than A and B so that a reader never has to reconstruct which is which.

The whole thing runs in log space. Lambda for a 30-node investigation
underflows a float otherwise, and an underflow would read as "proceed".
"""

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

Decision = Literal["abort_restart", "proceed_selective", "continue"]

# Sensible starting point. Both errors are costly and neither is catastrophic:
# a wrong abort pays for a restart that was avoidable, a wrong continue pays
# for an investigation that ends in a restart anyway. Symmetric until the cost
# model says otherwise.
DEFAULT_ALPHA = 0.1
DEFAULT_BETA = 0.1


@dataclass(frozen=True)
class SPRTConfig:
    """The two hypotheses and the two error rates.

    `f_star` is the ex-ante break-even fraction from
    `economics.f_star_ex_ante(A, N)` -- below it, finishing is worth it.
    `f_star_high` is the fraction at which we would rather restart. They must
    be separated: an SPRT between two hypotheses that touch never terminates,
    because no observation distinguishes them.
    """

    f_star: float = 0.3
    f_star_high: float = 0.7
    alpha: float = DEFAULT_ALPHA
    beta: float = DEFAULT_BETA

    def __post_init__(self) -> None:
        for name in ("f_star", "f_star_high"):
            value = getattr(self, name)
            if not 0.0 < value < 1.0:
                raise ValueError(
                    f"{name} must be strictly between 0 and 1 (a Bernoulli "
                    f"parameter of 0 or 1 makes the log-likelihood infinite); "
                    f"got {value}"
                )
        if self.f_star >= self.f_star_high:
            raise ValueError(
                f"f_star ({self.f_star}) must be below f_star_high "
                f"({self.f_star_high}). Hypotheses that touch cannot be "
                "distinguished by any number of observations, so the test "
                "would never stop."
            )
        for name in ("alpha", "beta"):
            value = getattr(self, name)
            if not 0.0 < value < 0.5:
                raise ValueError(
                    f"{name} must be in (0, 0.5); got {value}. At 0.5 the "
                    "thresholds collapse and the test decides on one sample."
                )

    @property
    def abort_threshold(self) -> float:
        """Wald's A = (1 - beta) / alpha. Crossing it accepts H1: restart."""
        return (1.0 - self.beta) / self.alpha

    @property
    def proceed_threshold(self) -> float:
        """Wald's B = beta / (1 - alpha). Crossing it accepts H0: finish."""
        return self.beta / (1.0 - self.alpha)

    @property
    def log_abort(self) -> float:
        return math.log(self.abort_threshold)

    @property
    def log_proceed(self) -> float:
        return math.log(self.proceed_threshold)

    def log_likelihood_step(self, tainted: bool) -> float:
        """log p1(x) - log p0(x) for one Bernoulli observation."""
        if tainted:
            return math.log(self.f_star_high) - math.log(self.f_star)
        return math.log(1.0 - self.f_star_high) - math.log(1.0 - self.f_star)

    def describe(self) -> str:
        return (
            f"H0: f < {self.f_star:.2f} (finish selective)   "
            f"H1: f >= {self.f_star_high:.2f} (abort, restart now)\n"
            f"alpha={self.alpha} beta={self.beta}  "
            f"abort at LR >= {self.abort_threshold:.2f}, "
            f"proceed at LR <= {self.proceed_threshold:.3f}"
        )


@dataclass
class SPRTState:
    """The running log-likelihood ratio and what produced it."""

    config: SPRTConfig
    log_lr: float = 0.0
    checks: int = 0
    tainted: int = 0
    observations: list[bool] = field(default_factory=list)

    @property
    def clean(self) -> int:
        return self.checks - self.tainted

    @property
    def observed_fraction(self) -> float:
        return self.tainted / self.checks if self.checks else 0.0

    def observe(self, tainted: bool) -> "SPRTState":
        """One more checked node. Mutates and returns self, so a caller can
        chain; the state is a running total, not a value object."""
        self.log_lr += self.config.log_likelihood_step(tainted)
        self.checks += 1
        self.tainted += int(tainted)
        self.observations.append(tainted)
        return self

    def decision(self) -> Decision:
        if self.log_lr >= self.config.log_abort:
            return "abort_restart"
        if self.log_lr <= self.config.log_proceed:
            return "proceed_selective"
        return "continue"


@dataclass
class SPRTResult:
    """What the investigation concluded, and what it cost to conclude it."""

    decision: Decision
    checks_used: int
    candidates: int
    tainted: int
    log_lr: float
    observed_fraction: float
    exhausted: bool = False
    checked_ids: list[Any] = field(default_factory=list)
    config: SPRTConfig = field(default_factory=SPRTConfig)

    @property
    def checks_saved(self) -> int:
        return self.candidates - self.checks_used

    @property
    def saving(self) -> float:
        return self.checks_saved / self.candidates if self.candidates else 0.0

    def line(self) -> str:
        how = "exhausted candidates" if self.exhausted else "threshold crossed"
        return (
            f"{self.decision:<18} after {self.checks_used}/{self.candidates} "
            f"checks ({self.saving:.0%} saved, {how}); "
            f"observed f={self.observed_fraction:.2f}, logLR={self.log_lr:+.2f}"
        )


def investigate(
    candidates: Iterable[Any],
    check: Callable[[Any], bool],
    config: SPRTConfig | None = None,
    max_checks: int | None = None,
) -> SPRTResult:
    """Check candidates one at a time, stopping as soon as the SPRT decides.

    `check(candidate) -> bool` is one counterfactual (or one group test): True
    when the node turns out contaminated. It is called at most once per
    candidate and lazily, which is the entire point -- a candidate never
    reached is a call never paid for.

    Running out of candidates without crossing either boundary is not a
    failure. It means the true fraction sits between the two hypotheses, where
    the test is *designed* to be slow because the answer genuinely does not
    matter much. The fallback then compares the observed fraction against
    f_star, which is the ordinary non-sequential decision and is exactly what
    the old check-everything code did -- so the SPRT never does worse than the
    thing it replaces.
    """
    config = config or SPRTConfig()
    state = SPRTState(config=config)
    checked: list[Any] = []

    for candidate in candidates:
        if max_checks is not None and state.checks >= max_checks:
            break
        state.observe(bool(check(candidate)))
        checked.append(candidate)
        decision = state.decision()
        if decision != "continue":
            return SPRTResult(
                decision=decision,
                checks_used=state.checks,
                candidates=_count(candidates, state.checks),
                tainted=state.tainted,
                log_lr=state.log_lr,
                observed_fraction=state.observed_fraction,
                exhausted=False,
                checked_ids=checked,
                config=config,
            )

    fallback: Decision = (
        "abort_restart"
        if state.observed_fraction >= config.f_star
        else "proceed_selective"
    )
    return SPRTResult(
        decision=fallback,
        checks_used=state.checks,
        candidates=_count(candidates, state.checks),
        tainted=state.tainted,
        log_lr=state.log_lr,
        observed_fraction=state.observed_fraction,
        exhausted=True,
        checked_ids=checked,
        config=config,
    )


def _count(candidates: Iterable[Any], at_least: int) -> int:
    """How many candidates there were, when the iterable can say.

    A generator cannot, and asking would consume it. Falling back to the number
    actually checked makes `checks_saved` read 0 rather than negative, which is
    the honest answer for an input whose length was never known.
    """
    try:
        return len(candidates)  # type: ignore[arg-type]
    except TypeError:
        return at_least


# --- the cheap prior the ex-ante rule needs -----------------------------------


def structural_prior(trace: Any, flagged: Iterable[str]) -> float:
    """A free estimate of f, for `economics.ex_ante_decision`.

    Counts the fraction of the run that is *downstream in the call graph* of
    where the flagged sources entered -- baseline B2's discard set, in other
    words. That is deliberately the pessimistic reading: it is an upper bound
    on what contamination could reach, it costs no model calls, and using an
    optimistic prior here would bias the ex-ante rule towards investigating,
    which is the failure mode the whole phase exists to remove.

    Weighted by replay cost rather than by event count, so it is comparable to
    the f in `A + f*N` -- which is a token fraction, not an event fraction.
    """
    from src.eval.baselines import b2_topology_closure
    from src.recovery.policy import event_cost, restart_all_cost

    total = restart_all_cost(trace)
    if not total:
        return 0.0
    closure = b2_topology_closure(trace, flagged)
    return min(1.0, sum(event_cost(trace, eid) for eid in closure) / total)


def config_for(
    analysis_tokens: float,
    restart_tokens: float,
    margin: float = 0.15,
    alpha: float = DEFAULT_ALPHA,
    beta: float = DEFAULT_BETA,
) -> SPRTConfig:
    """Build the hypotheses from the cost model rather than from taste.

    f_star is the ex-ante break-even fraction; f_star_high sits `margin` above
    it. The margin is the indifference band: between the two we do not care
    much which way it goes, and a wider band is a cheaper test. It is a
    parameter and not a measurement, so it is named and defaulted here rather
    than buried at a call site.

    Both are clamped into (0, 1) because the SPRT's log-likelihoods are
    undefined at the endpoints, and a break-even fraction can legitimately come
    out negative (analysis costs more than the restart) or above 1.
    """
    from src.eval.economics import f_star_ex_ante

    raw = f_star_ex_ante(analysis_tokens, restart_tokens)
    low = min(max(raw, 0.02), 0.90)
    high = min(low + margin, 0.98)
    return SPRTConfig(f_star=low, f_star_high=high, alpha=alpha, beta=beta)


if __name__ == "__main__":
    import random

    config = SPRTConfig(f_star=0.3, f_star_high=0.7)
    print(config.describe())
    print()
    print("Expected sample size against the true contamination fraction.")
    print("SPRT is fastest where the truth is far from both hypotheses and")
    print("slowest between them -- that is the documented behaviour, not a")
    print("defect, and it is where the answer matters least.")
    print()
    print(f"{'true f':>8}{'mean checks':>13}{'abort rate':>12}{'candidates':>12}")
    print("-" * 45)
    for true_f in (0.0, 0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95, 1.0):
        totals = []
        aborts = 0
        for trial in range(400):
            rng = random.Random(1000 + trial)
            candidates = list(range(200))
            result = investigate(
                candidates,
                lambda _c, rng=rng, f=true_f: rng.random() < f,
                config,
            )
            totals.append(result.checks_used)
            aborts += result.decision == "abort_restart"
        print(
            f"{true_f:>8.2f}{sum(totals)/len(totals):>13.1f}"
            f"{aborts/400:>12.2f}{200:>12}"
        )
