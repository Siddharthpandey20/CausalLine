"""
When is CausalLine worth deploying, and when is an investigation worth
finishing? Two different questions with two different answers.

Phase 9. This module exists because the comparison the project had been using
was wrong in a specific, correctable way, and the wrongness always pointed the
same direction -- towards investigating.

THE SUNK-COST ERROR
-------------------
The old rule was: investigate if

    A + f*N  <  N            (A = analysis cost, N = full restart, f = the
                              fraction of the run selective recovery replays)

which rearranges to `f < 1 - A/N`. That comparison is correct exactly once: at
the moment you are deciding whether to *start*. Both sides then still have A in
front of them, and including it on the left only is the whole content of the
decision.

The moment A has been spent, it is spent on both branches. Restarting from
scratch does not refund it. So a post-investigation decision that still writes
`A + f*N < N` is comparing a number that includes a sunk cost against one that
does not, and it will keep recommending "finish the selective plan" in cases
where finishing costs more than restarting. The correct ex-post comparison is

    f*N  <  N               i.e. simply f < 1

which is a much weaker bar, and the difference between the two bars is exactly
the mistake. `ex_post_decision()` below cannot be handed A at all; passing it
raises. There is a regression test for that, because this was a live error in
this project's own reasoning and not a hypothetical.

THE OTHER MISSING TERM: STORAGE IS PAID ON EVERY RUN
-----------------------------------------------------
Comparing total costs on an attacked run flatters CausalLine, because the
tracing overhead is paid on every run and the recovery saving is collected only
on attacked ones. Amortised per run:

    causalline    = storage_per_run + attack_rate * (A + f*N)
    restart_only  =                   attack_rate * N

so deployment is worth it exactly when

    storage_per_run / attack_rate  +  A + f*N  <  N

The first term is the one that decides it. At a low attack rate the storage tax
is divided by a small number and dominates; at a high enough attack rate it
vanishes and the token comparison takes over. That crossing point -- reported as
a function of attack_rate by `attack_rate_table()` and `plot_attack_rate()` --
is the deliverable, not the single number.

UNITS
-----
Everything is in tokens. Storage is converted at `storage_weight` tokens per
byte, which is a reporting parameter and not a measurement (see
`metrics.CostWeights`); the sweep is over it as well.
"""

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.eval.metrics import CostWeights
from src.risk.attack_model import run_probability

# Default sweep for the attack-rate axis. Log-spaced because the interesting
# region is small rates: at 1-in-10 runs attacked nobody needs this analysis.
DEFAULT_ATTACK_RATES: tuple[float, ...] = (
    0.0001, 0.0003, 0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0
)


class SunkCostError(RuntimeError):
    """A post-investigation decision was handed the analysis cost.

    Raised rather than ignored. Silently dropping A would make the caller
    believe it was accounted for; the caller has a bug in its reasoning and
    needs to see it.
    """


# --- ex ante: is the investigation worth starting? ----------------------------


def f_star_ex_ante(analysis_tokens: float, restart_tokens: float) -> float:
    """The contamination fraction below which investigating is worth starting.

        f* = 1 - A/N

    Compared against a *prior* estimate of f -- from the structural pre-check in
    src/recovery/sprt_investigate.py, or from the fraction of the trace
    downstream of the detector's alarm. Not against a measured f: measuring f is
    what A buys, so a rule that needs the measurement first has no ex-ante
    content.

    Negative when A > N, which is the honest answer for an investigation that
    costs more than the thing it might avoid: no value of f makes it worth
    starting.
    """
    if restart_tokens <= 0:
        raise ValueError("restart_tokens (N) must be positive")
    return 1.0 - (analysis_tokens / restart_tokens)


@dataclass
class ExAnteDecision:
    investigate: bool
    f_star: float
    f_prior: float
    analysis_tokens: float
    restart_tokens: float
    reason: str = ""

    def line(self) -> str:
        verdict = "INVESTIGATE" if self.investigate else "RESTART NOW"
        return (
            f"{verdict}: prior f={self.f_prior:.3f} vs f*={self.f_star:.3f} "
            f"(A={self.analysis_tokens:.0f}, N={self.restart_tokens:.0f}) "
            f"-- {self.reason}"
        )


def ex_ante_decision(
    analysis_tokens: float, restart_tokens: float, f_prior: float
) -> ExAnteDecision:
    """Before spending anything: investigate, or restart immediately?

    This is the only decision in which A legitimately appears on one side.
    """
    f_star = f_star_ex_ante(analysis_tokens, restart_tokens)
    investigate = f_prior < f_star
    if f_star <= 0:
        reason = "analysis alone costs at least as much as a full restart"
    elif investigate:
        reason = "expected selective replay plus analysis beats restarting"
    else:
        reason = "contamination is expected to be too widespread to pay for analysis"
    return ExAnteDecision(
        investigate=investigate,
        f_star=f_star,
        f_prior=f_prior,
        analysis_tokens=analysis_tokens,
        restart_tokens=restart_tokens,
        reason=reason,
    )


# --- ex post: A is spent. It is spent on both branches. -----------------------


@dataclass
class ExPostDecision:
    finish_selective: bool
    f: float
    restart_tokens: float
    remaining_selective_tokens: float
    reason: str = ""

    def line(self) -> str:
        verdict = "FINISH SELECTIVE" if self.finish_selective else "RESTART FROM HERE"
        return (
            f"{verdict}: f={self.f:.3f}, remaining "
            f"{self.remaining_selective_tokens:.0f} vs restart "
            f"{self.restart_tokens:.0f} -- {self.reason}"
        )


def ex_post_decision(
    f: float,
    restart_tokens: float,
    **forbidden: Any,
) -> ExPostDecision:
    """After the investigation: finish the selective plan, or restart?

    Compares `f*N` against `N` and nothing else. A is **not** an argument and
    cannot be made one: it has already been spent, it is not refunded by
    restarting, and adding it to either side is the error this module is named
    after.

    Any keyword argument at all raises `SunkCostError`. That is deliberately
    blunt -- the plausible mistakes are all spelled differently
    (`analysis_tokens=`, `A=`, `analysis=`, `investigation_cost=`) and a
    whitelist would miss the next one.
    """
    if forbidden:
        raise SunkCostError(
            f"ex_post_decision() was given {sorted(forbidden)}. The analysis "
            "cost is sunk once the investigation has run: it is not refunded "
            "by restarting, so it belongs on neither side of this comparison. "
            "Compare f*N against N only. Use ex_ante_decision() for the "
            "before-you-start question, which is the one A belongs in."
        )
    if restart_tokens <= 0:
        raise ValueError("restart_tokens (N) must be positive")
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"contamination fraction f must be in 0..1, got {f}")

    remaining = f * restart_tokens
    finish = remaining < restart_tokens
    return ExPostDecision(
        finish_selective=finish,
        f=f,
        restart_tokens=restart_tokens,
        remaining_selective_tokens=remaining,
        reason=(
            "selective replay recomputes less than the whole run"
            if finish
            else "selective replay would recompute the whole run anyway"
        ),
    )


# --- per-run amortised comparison ---------------------------------------------


@dataclass(frozen=True)
class Deployment:
    """One deployment's numbers. All in tokens except storage, in bytes.

    `storage_bytes_per_run` is the tracing overhead CausalLine adds -- the
    checkpoint and content sidecars -- measured by
    `src/tracing/checkpoints.overhead()`. `restart_tokens` is N, the cost of
    re-running the workflow. `analysis_tokens` is A. `f` is the fraction of N
    that selective replay actually recomputes.
    """

    restart_tokens: float
    analysis_tokens: float
    f: float
    storage_bytes_per_run: float = 0.0
    weights: CostWeights = field(default_factory=CostWeights)
    label: str = ""

    @property
    def storage_per_run(self) -> float:
        """Storage overhead in tokens, at the stated exchange rate."""
        return self.storage_bytes_per_run * self.weights.storage_weight

    @property
    def selective_incident_cost(self) -> float:
        """A + f*N: what one attacked run costs under CausalLine."""
        return self.analysis_tokens + self.f * self.restart_tokens


def per_run_amortized_cost_causalline(
    deployment: Deployment, attack_rate: float
) -> float:
    """storage_per_run + attack_rate * (A + f*N)"""
    return deployment.storage_per_run + attack_rate * deployment.selective_incident_cost


def per_run_amortized_cost_restart_only(
    deployment: Deployment, attack_rate: float
) -> float:
    """attack_rate * N. No storage: the baseline keeps no checkpoints."""
    return attack_rate * deployment.restart_tokens


def worth_deploying(deployment: Deployment, attack_rate: float) -> bool:
    """storage_per_run / attack_rate + A + f*N < N

    Written in the divided form because that is the form that shows what is
    going on: the storage tax is amortised over however many runs it takes to
    see one attack.
    """
    if attack_rate <= 0:
        # No attacks ever. CausalLine costs its storage tax, restart-only costs
        # nothing, and there is no saving to set against it -- so the answer is
        # no, including in the free-storage case where the two merely tie.
        # Written out rather than folded into the division below, which would
        # be a ZeroDivisionError, and stated to match the direct comparison
        # exactly: a helper that disagreed with the costs it summarises would
        # be worse than no helper.
        return False
    return (
        deployment.storage_per_run / attack_rate + deployment.selective_incident_cost
        < deployment.restart_tokens
    )


def attack_rate_threshold(deployment: Deployment) -> float:
    """The attack rate above which the storage tax pays for itself.

    Solving  S/r + A + f*N < N  for r:

        r  >  S / (N*(1-f) - A)

    Returns `inf` when the denominator is non-positive -- when A + f*N already
    exceeds N, no attack rate saves it, because CausalLine loses on the
    incident itself and the storage tax only makes it worse. Returns 0.0 when
    there is no storage overhead to amortise.
    """
    headroom = deployment.restart_tokens * (1.0 - deployment.f) - deployment.analysis_tokens
    if deployment.storage_per_run <= 0:
        return 0.0 if headroom > 0 else math.inf
    if headroom <= 0:
        return math.inf
    return deployment.storage_per_run / headroom


@dataclass
class AttackRateRow:
    attack_rate: float
    causalline: float
    restart_only: float

    @property
    def saving(self) -> float:
        return self.restart_only - self.causalline

    @property
    def worth_it(self) -> bool:
        return self.causalline < self.restart_only


def attack_rate_table(
    deployment: Deployment,
    rates: Iterable[float] = DEFAULT_ATTACK_RATES,
) -> list[AttackRateRow]:
    """Per-run amortised cost of both options, across the attack-rate axis."""
    return [
        AttackRateRow(
            attack_rate=r,
            causalline=per_run_amortized_cost_causalline(deployment, r),
            restart_only=per_run_amortized_cost_restart_only(deployment, r),
        )
        for r in rates
    ]


def render_attack_rate_table(
    deployment: Deployment, rows: list[AttackRateRow]
) -> str:
    threshold = attack_rate_threshold(deployment)
    head = (
        f"{'attack rate':>12}{'CausalLine':>13}{'restart-only':>14}"
        f"{'saving':>11}  verdict"
    )
    lines = [
        f"{deployment.label or 'deployment'}: N={deployment.restart_tokens:.0f} "
        f"A={deployment.analysis_tokens:.0f} f={deployment.f:.3f} "
        f"storage={deployment.storage_bytes_per_run:.0f}B "
        f"@ {deployment.weights.storage_weight} tok/B "
        f"= {deployment.storage_per_run:.1f} tok/run",
        head,
        "-" * len(head),
    ]
    for row in rows:
        lines.append(
            f"{row.attack_rate:>12.4f}{row.causalline:>13.1f}"
            f"{row.restart_only:>14.1f}{row.saving:>11.1f}"
            f"  {'pays off' if row.worth_it else 'not worth it'}"
        )
    lines.append("")
    if threshold == math.inf:
        lines.append(
            "threshold: NONE. A + f*N already exceeds N, so no attack rate "
            "makes the storage tax worth paying."
        )
    else:
        lines.append(
            f"threshold: attack_rate > {threshold:.5f} "
            f"(1 run in {1/threshold:.0f}) for the storage tax to pay for itself"
        )
    return "\n".join(lines)


def plot_attack_rate(
    deployment: Deployment,
    path: str | Path,
    rates: Iterable[float] = DEFAULT_ATTACK_RATES,
) -> Path:
    """The required deliverable: amortised cost against attack rate.

    Written to a file rather than shown, so the experiment runner can produce it
    unattended.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = attack_rate_table(deployment, rates)
    threshold = attack_rate_threshold(deployment)

    fig, ax = plt.subplots(figsize=(6.0, 4.0))
    xs = [r.attack_rate for r in rows]
    ax.plot(xs, [r.causalline for r in rows], marker="o", label="CausalLine")
    ax.plot(
        xs, [r.restart_only for r in rows], marker="s", label="restart only"
    )
    if threshold != math.inf and threshold > 0:
        ax.axvline(
            threshold,
            linestyle="--",
            color="grey",
            label=f"break-even r={threshold:.4g}",
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("attack rate (fraction of runs attacked)")
    ax.set_ylabel("amortised cost per run (tokens)")
    ax.set_title(
        f"{deployment.label or 'deployment'}: storage tax vs recovery saving"
    )
    ax.legend()
    ax.grid(True, which="both", alpha=0.25)
    fig.tight_layout()
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


# --- the ex-ante gate: is this run even likely to have been attacked? ---------

# Below this compromise probability, investigation is not launched at all.
#
# NOT a tuned value and not a safety knob: it is the point below which the
# *expected* saving from investigating cannot cover A. Set deliberately low,
# because skipping investigation does not preserve anything -- an uninvestigated
# pair stays `unchecked`, which the contamination walk treats as contaminated.
# So a wrong skip costs preserved work and never costs safety, exactly like
# running out of budget.
SKIP_INVESTIGATION_BELOW = 0.05


def should_investigate(compromise_probability: float) -> bool:
    """Whether a trace is worth spending analysis tokens on.

    `src/risk/attack_model.py` computes P from the trace's own exposure-edge
    counts per channel. This is the only consumer of it that changes behaviour
    -- the planner and metrics still ignore it, which is the honest state.

    READ THE SATURATION WARNING FIRST. `run_probability()` combines every
    exposure edge as an independent attempt, and at ~64 edges per run of this
    pipeline it returns 1.000. So on our own traces this gate never fires, and
    that is a property of the testbed rather than of the gate: a short workflow
    with heavy re-retrieval saturates the formula. The gate is written for the
    regime where P is genuinely small -- a run with two or three exposures --
    and the measured value is reported alongside so a reader can see which
    regime a number came from.
    """
    return compromise_probability >= SKIP_INVESTIGATION_BELOW


# --- the contamination sweep, run on real scripted traces ---------------------


@dataclass
class SweepPoint:
    """One (scenario, seed-set-size) point of the contamination sweep."""

    scenario: str
    variant: str
    seeds: int
    contaminated_events: int
    total_events: int
    f: float
    analysis_tokens: int
    restart_tokens: int
    # A split by where it went. The inline half is charged on every event of
    # every run; the targeted half only on the detector's region. They are
    # different deployment choices with very different totals, and a single A
    # hides which one a result is about (D-032).
    self_report_tokens: int = 0
    counterfactual_tokens: int = 0
    # P = 1 - PROD_i (1 - pa_i)^{m_i} over this trace's actual exposure-edge
    # counts per channel (src/risk/attack_model.py). The ex-ante question:
    # how likely is it that this run was compromised at all, before any
    # analysis token is spent finding out.
    compromise_probability: float = 0.0
    investigation_skipped: bool = False

    @property
    def targeted_analysis_tokens(self) -> int:
        """A under the deployment path docs/02 actually describes: no inline
        self-report on every event, counterfactual only on the flagged region."""
        return self.counterfactual_tokens

    @property
    def selective_cost(self) -> float:
        return self.analysis_tokens + self.f * self.restart_tokens

    @property
    def targeted_selective_cost(self) -> float:
        return self.targeted_analysis_tokens + self.f * self.restart_tokens

    @property
    def selective_wins(self) -> bool:
        return self.selective_cost < self.restart_tokens

    @property
    def targeted_selective_wins(self) -> bool:
        return self.targeted_selective_cost < self.restart_tokens

    def line(self) -> str:
        return (
            f"P={self.compromise_probability:.3f} "
            + ("SKIP " if self.investigation_skipped else "     ")
            + f"{self.scenario}-{self.variant:<12} seeds={self.seeds:<3} "
            f"taint={self.contaminated_events:>2}/{self.total_events:<3} "
            f"f={self.f:.3f}  A+fN={self.selective_cost:>7.0f}  "
            f"N={self.restart_tokens:>6}  "
            f"{'selective' if self.selective_wins else 'RESTART':<9}"
            f"| targeted A={self.targeted_analysis_tokens:>4} "
            f"-> {self.targeted_selective_cost:>6.0f} "
            f"{'selective' if self.targeted_selective_wins else 'RESTART'}"
        )


def _replay_cost(trace: Any, events: Iterable[str]) -> int:
    from src.recovery.policy import event_cost

    return sum(event_cost(trace, eid) for eid in events)


def contamination_sweep(
    scenarios: Iterable[str] = ("A", "B", "C"),
    variants: Iterable[bool] = (True, False),
    workdir: str | Path = "data/runs/economics",
    seed: int = 20260906,
) -> list[SweepPoint]:
    """Dial contamination from one agent up to most of the trace, measured.

    The dial is the **detector's verdict**, not a synthetic parameter: seeding
    the contamination walk with one flagged source reaches one agent, seeding it
    with several reaches several, and seeding it with every source of the
    poisoned kind (which is what `Pessimistic` does) reaches most of the run.
    Each seed set produces a real invalidation set through the real walk, so f
    is the fraction the real planner would replay rather than a number we chose.

    A and N come off the trace: A is `analysis_tokens()`, N is
    `restart_all_cost()`. Nothing here is estimated by hand, which is the point.
    """
    from src.eval.attacks import build, label_malicious
    from src.eval.scripted import ScriptedClient
    from src.provenance.contamination import contaminate
    from src.provenance.estimator import CheckBudget, HybridAttributor, refine_for_verdict
    from src.provenance.signatures import Calibration
    from src.recovery.policy import restart_all_cost
    from src.tracing.logger import read_trace
    from src.tracing.pipeline import run_pipeline
    from src.tracing.tools import Tools

    out_dir = Path(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    points: list[SweepPoint] = []

    for scenario in scenarios:
        for influencing in variants:
            variant = "influencing" if influencing else "exposed_only"
            attack = build(scenario, influencing)
            path = out_dir / f"sweep-{scenario}-{variant}.jsonl"
            tools = attack.apply(
                Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
            )
            client = ScriptedClient(seed=seed)
            attributor = HybridAttributor(
                client=client,
                mode="self_report",
                calibration=Calibration.load(),
                model="scripted",
                seed=seed,
            )
            run_pipeline(
                path,
                client=client,
                tools=tools,
                attributor=attributor,
                handoff_hook=attack.handoff_hook,
            )
            planted = label_malicious(path, attack.marker)
            if not planted:
                raise RuntimeError(f"{attack.name}: marker never reached the trace")
            refine_for_verdict(
                path, planted, client,
                calibration=Calibration.load(),
                budget=CheckBudget(),
                model="scripted",
            )
            trace = read_trace(path)
            trace.validate()

            N = restart_all_cost(trace)
            A = trace.analysis_tokens()
            compromise_p = run_probability(trace)
            by_purpose = trace.tokens_by_purpose()
            A_self = by_purpose.get("self_report", 0)
            A_cf = by_purpose.get("counterfactual", 0)
            planted_kinds = {trace.source(s).kind for s in planted}
            same_kind = [
                s.id for s in trace.sources if s.kind in planted_kinds
            ]
            # Growing seed sets: the planted source alone, then progressively
            # more of the same channel, up to all of it. Each is a verdict a
            # real detector could plausibly emit (Oracle at one end,
            # Pessimistic at the other).
            ladder: list[list[str]] = []
            widened = list(planted)
            for extra in same_kind:
                if extra not in widened:
                    widened.append(extra)
                ladder.append(list(widened))
            ladder = [list(planted)] + ladder[:: max(1, len(ladder) // 5)] + [same_kind]

            seen: set[tuple[str, ...]] = set()
            for seeds_list in ladder:
                key = tuple(sorted(seeds_list))
                if key in seen:
                    continue
                seen.add(key)
                region = contaminate(trace, seeds_list)
                replay_tokens = _replay_cost(trace, region.events)
                points.append(
                    SweepPoint(
                        scenario=scenario,
                        variant=variant,
                        seeds=len(seeds_list),
                        contaminated_events=len(region.events),
                        total_events=len(trace.events),
                        f=(replay_tokens / N) if N else 0.0,
                        analysis_tokens=A,
                        restart_tokens=N,
                        self_report_tokens=A_self,
                        counterfactual_tokens=A_cf,
                        compromise_probability=compromise_p,
                        investigation_skipped=not should_investigate(compromise_p),
                    )
                )
    return points


def empirical_crossing(points: list[SweepPoint], targeted: bool = False) -> dict[str, Any]:
    """Where A + f*N stops being cheaper than N, read off the sweep.

    Reported as an interval between the last winning point and the first losing
    one, rather than as a single interpolated number: f moves in event-sized
    steps here, so a decimal place we did not measure would be invented.

    `targeted` scores under the deployment path (counterfactual on the flagged
    region only) instead of the inline-everything condition. Both are reported
    by `run_report()`, because they answer different questions and the inline
    one is the more expensive of the two by a wide margin.

    When A >= N there is no crossing at all: analysis alone already costs more
    than the restart it is trying to avoid, so *every* f loses and the "lowest
    losing f" is 0.0 for a reason that has nothing to do with contamination
    spreading. That case is flagged explicitly rather than reported as a
    crossing at zero, which would read as "selective recovery never helps"
    when the real statement is "the instrument costs more than the repair".
    """
    def cost(p: SweepPoint) -> float:
        return p.targeted_selective_cost if targeted else p.selective_cost

    def a_of(p: SweepPoint) -> int:
        return p.targeted_analysis_tokens if targeted else p.analysis_tokens

    winners = [p for p in points if cost(p) < p.restart_tokens]
    losers = [p for p in points if cost(p) >= p.restart_tokens]
    starved = [p for p in points if a_of(p) >= p.restart_tokens]
    return {
        "condition": "targeted" if targeted else "inline",
        "points": len(points),
        "selective_wins": len(winners),
        "restart_wins": len(losers),
        "highest_winning_f": max((p.f for p in winners), default=None),
        "lowest_losing_f": min((p.f for p in losers), default=None),
        # Points where A alone already exceeds N. No f can rescue these, and
        # counting them as "contamination too wide" would be a misreading.
        "analysis_exceeds_restart": len(starved),
        "mean_f_star_ex_ante": (
            statistics.fmean(
                [f_star_ex_ante(a_of(p), p.restart_tokens) for p in points]
            )
            if points
            else None
        ),
    }


# --- how much cheaper would analysis have to get? -----------------------------


@dataclass
class FrontierCell:
    """One (A/N, f) cell of the break-even frontier."""

    analysis_ratio: float  # A/N
    f: float
    threshold: float  # attack rate above which deployment pays off

    @property
    def ever_pays(self) -> bool:
        return self.threshold != math.inf


def breakeven_frontier(
    restart_tokens: float,
    storage_per_run: float,
    analysis_ratios: Iterable[float] = (0.05, 0.1, 0.25, 0.5, 1.0, 1.5),
    fractions: Iterable[float] = (0.1, 0.25, 0.5, 0.67, 0.85),
) -> list[FrontierCell]:
    """The attack-rate threshold over a grid of (A/N, f).

    Why this and not one number: the measured point on our own testbed is
    A/N = 1.5, at which nothing pays off at any attack rate. That is a real
    finding about a 6-call pipeline analysed by 9 analysis calls, and it is
    also entirely a fact about the *ratio*, not about the method. The frontier
    says what the ratio would have to become, which is the actionable half --
    and it is computed from the real N and the real measured storage overhead,
    not invented.
    """
    cells: list[FrontierCell] = []
    for ratio in analysis_ratios:
        for f in fractions:
            deployment = Deployment(
                restart_tokens=restart_tokens,
                analysis_tokens=ratio * restart_tokens,
                f=f,
                storage_bytes_per_run=storage_per_run,
                weights=CostWeights(storage_weight=1.0),
            )
            cells.append(
                FrontierCell(
                    analysis_ratio=ratio,
                    f=f,
                    threshold=attack_rate_threshold(deployment),
                )
            )
    return cells


def render_frontier(cells: list[FrontierCell]) -> str:
    ratios = sorted({c.analysis_ratio for c in cells})
    fractions = sorted({c.f for c in cells})
    lookup = {(c.analysis_ratio, c.f): c for c in cells}
    head = "    A/N \\ f " + "".join(f"{f:>11.2f}" for f in fractions)
    lines = [head, "-" * len(head)]
    for ratio in ratios:
        cells_row = []
        for f in fractions:
            cell = lookup[(ratio, f)]
            cells_row.append("never" if not cell.ever_pays else f"{cell.threshold:.4f}")
        lines.append(f"{ratio:>11.2f} " + "".join(f"{v:>11}" for v in cells_row))
    lines.append("")
    lines.append(
        "cell = attack rate above which CausalLine's storage tax pays for "
        "itself; 'never' = A + f*N already exceeds N."
    )
    return "\n".join(lines)


def plot_contamination_sweep(points: list[SweepPoint], path: str | Path) -> Path:
    """A + f*N against N, with the empirical crossing marked."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    by_scenario: dict[str, list[SweepPoint]] = {}
    for p in points:
        by_scenario.setdefault(f"{p.scenario}-{p.variant}", []).append(p)
    for label, group in sorted(by_scenario.items()):
        group = sorted(group, key=lambda p: p.f)
        ax.plot(
            [p.f for p in group],
            [p.selective_cost for p in group],
            marker="o",
            label=f"A+fN  {label}",
        )
    if points:
        N = statistics.fmean([p.restart_tokens for p in points])
        ax.axhline(N, color="black", linestyle="--", label="N (full restart, mean)")
    crossing = empirical_crossing(points)
    if crossing["lowest_losing_f"] is not None:
        ax.axvline(
            crossing["lowest_losing_f"],
            color="grey",
            linestyle=":",
            label=f"first losing f={crossing['lowest_losing_f']:.2f}",
        )
    ax.set_xlabel("contamination fraction f (replay tokens / N)")
    ax.set_ylabel("cost (tokens)")
    ax.set_title("Selective recovery vs full restart, by contamination fraction")
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


# --- detection latency, which Phase 12 needs ----------------------------------


@dataclass
class LatencyObservation:
    scenario: str
    variant: str
    detector: str
    injected_at_index: int
    detected_at_index: int
    total_events: int

    @property
    def latency_events(self) -> int:
        return max(0, self.detected_at_index - self.injected_at_index)


def detection_latency(
    trace: Any, verdict: Any, scenario: str = "", variant: str = ""
) -> LatencyObservation | None:
    """Events between the poisoning event and the detector's alarm.

    The poisoning event is the *origin event* of the earliest flagged source --
    the moment the payload entered the run. The alarm is `verdict.detected_at`,
    or the last event of the trace when the detector gives no point, which is
    what "the alarm fires at the end of the run" means and is the case for
    `Oracle`. Returns None when the detector flagged nothing: there is no
    latency to measure for an alarm that never went off, and folding those in as
    zero would drag the mean towards a detector that is doing nothing.
    """
    flagged = set(verdict.sources())
    if not flagged:
        return None
    order = {e.id: i for i, e in enumerate(trace.events)}
    origins = [
        order[s.origin_event]
        for s in trace.sources
        if s.id in flagged and s.origin_event and s.origin_event in order
    ]
    if not origins:
        return None
    detected_index = (
        order[verdict.detected_at]
        if verdict.detected_at and verdict.detected_at in order
        else len(trace.events) - 1
    )
    return LatencyObservation(
        scenario=scenario,
        variant=variant,
        detector=verdict.detector,
        injected_at_index=min(origins),
        detected_at_index=detected_index,
        total_events=len(trace.events),
    )


def latency_distribution(
    observations: list[LatencyObservation],
) -> dict[str, Any]:
    """Mean, median and spread of detection latency, in events.

    `mean_turns_between_detections` is the figure Phase 12's checkpoint interval
    formula consumes. It is the mean of this distribution and nothing else --
    computed from the runs, never hardcoded.
    """
    values = [o.latency_events for o in observations]
    if not values:
        return {"n": 0, "mean_turns_between_detections": 0.0}
    return {
        "n": len(values),
        "mean_turns_between_detections": statistics.fmean(values),
        "median": statistics.median(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
        "by_detector": {
            name: statistics.fmean(
                [o.latency_events for o in observations if o.detector == name]
            )
            for name in sorted({o.detector for o in observations})
        },
    }


def measure_detection_latency(
    workdir: str | Path = "data/runs/economics",
    detectors: Iterable[str] = ("oracle", "pessimistic", "simulated"),
    scenarios: Iterable[str] = ("A", "B", "C"),
    variants: Iterable[bool] = (True, False),
    seed: int = 20260906,
) -> tuple[list[LatencyObservation], dict[str, Any]]:
    """Run the scripted scenarios and measure how late each detector fires.

    Reuses the traces `contamination_sweep()` writes when they already exist,
    so calling both does not re-run the pipeline twice.
    """
    from src.eval.attacks import build, label_malicious
    from src.eval.detectors import build as build_detector
    from src.eval.scripted import ScriptedClient
    from src.tracing.logger import read_trace
    from src.tracing.pipeline import run_pipeline
    from src.tracing.tools import Tools

    out_dir = Path(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    observations: list[LatencyObservation] = []

    for scenario in scenarios:
        for influencing in variants:
            variant = "influencing" if influencing else "exposed_only"
            path = out_dir / f"sweep-{scenario}-{variant}.jsonl"
            attack = build(scenario, influencing)
            if not path.exists():
                tools = attack.apply(
                    Tools.from_fixtures(memory_path=path.with_suffix(".memory.json"))
                )
                run_pipeline(
                    path,
                    client=ScriptedClient(seed=seed),
                    tools=tools,
                    handoff_hook=attack.handoff_hook,
                )
                if not label_malicious(path, attack.marker):
                    raise RuntimeError(f"{attack.name}: marker never reached the trace")
            trace = read_trace(path, content=False)
            for name in detectors:
                kwargs: dict[str, Any] = {}
                if name == "simulated":
                    # A detector that notices some events after the fact. The
                    # latency is a parameter here rather than a measurement --
                    # we do not have a real detector's response time -- and it
                    # is reported per detector for exactly that reason.
                    kwargs = {"latency_events": 3}
                verdict = build_detector(name, **kwargs).flag(trace)
                observation = detection_latency(trace, verdict, scenario, variant)
                if observation is not None:
                    observations.append(observation)
    return observations, latency_distribution(observations)


# --- the report ---------------------------------------------------------------


def _deployment_dict(deployment: Deployment, storage_weight: float) -> dict[str, Any]:
    return {
        "label": deployment.label,
        "restart_tokens": deployment.restart_tokens,
        "analysis_tokens": deployment.analysis_tokens,
        "f": deployment.f,
        "storage_bytes_per_run": deployment.storage_bytes_per_run,
        "storage_weight": storage_weight,
        "storage_per_run_tokens": deployment.storage_per_run,
        "attack_rate_threshold": attack_rate_threshold(deployment),
    }


def _deployment_from_dict(data: dict[str, Any]) -> Deployment:
    return Deployment(
        restart_tokens=data["restart_tokens"],
        analysis_tokens=data["analysis_tokens"],
        f=data["f"],
        storage_bytes_per_run=data["storage_bytes_per_run"],
        weights=CostWeights(storage_weight=data["storage_weight"]),
        label=data["label"],
    )


def deployment_from_trace(
    trace: Any,
    f: float,
    storage_bytes: float,
    weights: CostWeights | None = None,
    label: str = "",
) -> Deployment:
    from src.recovery.policy import restart_all_cost

    return Deployment(
        restart_tokens=restart_all_cost(trace),
        analysis_tokens=trace.analysis_tokens(),
        f=f,
        storage_bytes_per_run=storage_bytes,
        weights=weights or CostWeights(storage_weight=1e-3),
        label=label,
    )


def run_report(
    workdir: str | Path = "data/runs/economics",
    out_dir: str | Path = "data/results",
    storage_weight: float = 1e-3,
) -> dict[str, Any]:
    """Phase 9's required outputs, generated from real runs.

    1. the contamination sweep, with the empirical crossing point
    2. the attack-rate threshold table and plot
    3. the detection-latency distribution Phase 12 consumes
    """
    from src.tracing.checkpoints import overhead
    from src.tracing.logger import read_trace

    results = Path(out_dir)
    results.mkdir(parents=True, exist_ok=True)

    points = contamination_sweep(workdir=workdir)
    crossing = empirical_crossing(points, targeted=False)
    crossing_targeted = empirical_crossing(points, targeted=True)
    sweep_plot = plot_contamination_sweep(points, results / "contamination-sweep.png")

    observations, latency = measure_detection_latency(workdir=workdir)

    # The deployment used for the attack-rate axis: the influencing scenario A
    # run, at the f CausalLine actually achieved on it.
    reference_path = Path(workdir) / "sweep-A-influencing.jsonl"
    trace = read_trace(reference_path)
    store = overhead(reference_path)
    reference_points = [
        p for p in points if p.scenario == "A" and p.variant == "influencing"
    ]
    # The oracle-seeded point: one flagged source, which is CausalLine's actual
    # operating point rather than the widest verdict in the ladder.
    reference = min(reference_points, key=lambda p: p.seeds)
    f_achieved = reference.f
    storage_bytes = store["checkpoint_bytes"] + store["content_bytes"]
    weights = CostWeights(storage_weight=storage_weight)

    deployment = deployment_from_trace(
        trace,
        f=f_achieved,
        storage_bytes=storage_bytes,
        weights=weights,
        label="A-influencing (inline attribution)",
    )
    # The same run costed under the deployment path docs/02 describes:
    # counterfactual on the flagged region only, no inline self-report on every
    # event. Both are plotted, because the inline condition has no threshold at
    # all and a reader needs to see which of the two that statement is about.
    targeted_deployment = Deployment(
        restart_tokens=deployment.restart_tokens,
        analysis_tokens=reference.targeted_analysis_tokens,
        f=f_achieved,
        storage_bytes_per_run=storage_bytes,
        weights=weights,
        label="A-influencing (targeted attribution)",
    )
    rows = attack_rate_table(deployment)
    targeted_rows = attack_rate_table(targeted_deployment)
    rate_plot = plot_attack_rate(deployment, results / "attack-rate-threshold.png")
    targeted_plot = plot_attack_rate(
        targeted_deployment, results / "attack-rate-threshold-targeted.png"
    )
    frontier = breakeven_frontier(
        restart_tokens=deployment.restart_tokens,
        storage_per_run=deployment.storage_per_run,
    )

    report = {
        "sweep": [p.__dict__ for p in points],
        "crossing": crossing,
        "crossing_targeted": crossing_targeted,
        "frontier": [c.__dict__ for c in frontier],
        "latency": latency,
        "deployment": _deployment_dict(deployment, storage_weight),
        "deployment_targeted": _deployment_dict(targeted_deployment, storage_weight),
        "attack_rate_table": [r.__dict__ for r in rows],
        "attack_rate_table_targeted": [r.__dict__ for r in targeted_rows],
        "figures": {
            "contamination_sweep": str(sweep_plot),
            "attack_rate_threshold": str(rate_plot),
            "attack_rate_threshold_targeted": str(targeted_plot),
        },
    }
    (results / "economics.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    return report


if __name__ == "__main__":
    report = run_report()

    print("CONTAMINATION SWEEP (A + f*N vs N, on real scripted traces)")
    print()
    for row in report["sweep"]:
        point = SweepPoint(**row)
        print("  " + point.line())
    print()
    for key in ("crossing", "crossing_targeted"):
        crossing = report[key]
        print(
            f"[{crossing['condition']}] selective wins {crossing['selective_wins']}"
            f"/{crossing['points']} points; highest winning f="
            f"{crossing['highest_winning_f']}, lowest losing f="
            f"{crossing['lowest_losing_f']}; "
            f"mean ex-ante f* = 1 - A/N = {crossing['mean_f_star_ex_ante']:.3f}"
        )
        if crossing["analysis_exceeds_restart"]:
            print(
                f"    {crossing['analysis_exceeds_restart']} of "
                f"{crossing['points']} points have A >= N: analysis alone "
                "costs more than the restart it avoids, so no f wins and the "
                "crossing is not about contamination width at all."
            )
    print()

    print("DETECTION LATENCY (events between injection and alarm)")
    latency = report["latency"]
    print(f"  n={latency['n']}  mean={latency['mean_turns_between_detections']:.2f} "
          f"median={latency['median']} sd={latency['stdev']:.2f} "
          f"range={latency['min']}..{latency['max']}")
    for name, mean in latency["by_detector"].items():
        print(f"    {name:<14} mean {mean:.2f} events")
    print()
    print("  This mean is what Phase 12's checkpoint interval consumes. It is")
    print("  computed here, never hardcoded there.")
    print()

    print("ATTACK-RATE THRESHOLD")
    print()
    deployment = _deployment_from_dict(report["deployment"])
    for key in ("deployment", "deployment_targeted"):
        one = _deployment_from_dict(report[key])
        print(render_attack_rate_table(one, attack_rate_table(one)))
        print()

    print("BREAK-EVEN FRONTIER: attack rate needed, over (A/N, f)")
    print(
        f"  at the real N={deployment.restart_tokens:.0f} and the real measured "
        f"storage {deployment.storage_per_run:.1f} tok/run. Our own measured "
        f"point is A/N={deployment.analysis_tokens / deployment.restart_tokens:.2f}, "
        f"f={deployment.f:.2f}."
    )
    print()
    print(render_frontier([FrontierCell(**c) for c in report["frontier"]]))
    print()
    print(f"figures: {report['figures']}")
    print("report:  data/results/economics.json")
