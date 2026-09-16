"""Candidate ex-ante gates, tested against the deterministic environment.

Every gate has the same signature: given a `Workflow` and a `ProbeOracle`,
decide INVESTIGATE or RESTART, having spent whatever it spent on probes.

    gate(workflow, oracle) -> bool

The oracle charges every probe, so a gate's cost is measured, not declared.
`src/recovery/gate1.py` is NOT imported for the decision logic -- the falsified
structural gate is re-implemented here as a reference line so it can be compared
on identical cases without touching production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.eval.gate1_sim import ProbeOracle, Workflow

Gate = Callable[[Workflow, ProbeOracle], bool]


# --- shared cost estimation ---------------------------------------------------


def a_hat(workflow: Workflow, influence: set | None = None,
          checked: set | None = None) -> float:
    """Estimated remaining investigation cost, from the trace alone.

    Exposure pairs still unsettled inside the contaminated region, priced at the
    event's own cost -- a counterfactual re-issues that event's prompt. Upper
    bound: group testing and the frontier expansion both settle more than one
    pair per call.
    """
    from src.provenance.contamination import contaminate
    from src.recovery.policy import event_cost

    region = contaminate(workflow.trace, set(workflow.truth.planted),
                         influence=influence or set(), checked=checked or set())
    total = 0.0
    for event in workflow.trace.events:
        for sid in event.exposures:
            if sid in region.sources and (sid, event.id) not in (checked or set()):
                total += event_cost(workflow.trace, event.id)
    return total


def _f_structural(workflow: Workflow, influence: set | None = None,
                  checked: set | None = None) -> float:
    """Cost-weighted contaminated fraction under the CURRENT verdicts.

    With no verdicts this is the plain structural closure. As probes come back
    clean it shrinks, which is the whole point of the probing gates.
    """
    from src.provenance.contamination import contaminate
    from src.recovery.policy import event_cost, restart_all_cost

    total = restart_all_cost(workflow.trace)
    if not total:
        return 0.0
    region = contaminate(workflow.trace, set(workflow.truth.planted),
                         influence=influence or set(), checked=checked or set())
    return min(1.0, sum(event_cost(workflow.trace, e)
                        for e in region.events) / total)


# --- reference lines ----------------------------------------------------------


def gate_always_restart(workflow: Workflow, oracle: ProbeOracle) -> bool:
    return False


def gate_always_investigate(workflow: Workflow, oracle: ProbeOracle) -> bool:
    return True


def make_oracle_gate(decisions: dict[str, bool]) -> Gate:
    def gate(workflow: Workflow, oracle: ProbeOracle) -> bool:
        return decisions[workflow.name]
    return gate


def gate_structural(workflow: Workflow, oracle: ProbeOracle) -> bool:
    """G_STRUCT -- the falsified gate, kept as the line to beat.

    `A_hat/N + f_structural <= 1`. No probes. Saturates whenever a flagged
    source enters at an agent everything else is downstream of, which is the
    hub counterexample.
    """
    n = max(1, workflow.n_restart)
    return (a_hat(workflow) / n + _f_structural(workflow)) <= 1.0


# --- H1: bottleneck probing ---------------------------------------------------


def _dominated_mass(workflow: Workflow, pair: tuple[str, str],
                    influence: set, checked: set) -> float:
    """How much of the contaminated region disappears if `pair` is CLEAN.

    Not a heuristic score: it recomputes the contamination walk with that pair
    marked checked-and-clean and measures the cost-weighted difference. The pair
    whose clean verdict would remove the most is the bottleneck worth probing.
    """
    before = _f_structural(workflow, influence, checked)
    after = _f_structural(workflow, influence, checked | {pair})
    return before - after


def make_bottleneck_gate(max_probes: int = 3) -> Gate:
    """H1 -- probe the structural bottleneck, but only when it could matter.

    THE DECISION RULE, AND WHY IT HAS NO TUNED THRESHOLD
    -----------------------------------------------------
    The economics are unchanged: investigate iff `A_hat/N + f <= 1`. What
    changes is that `f` is no longer the raw closure -- it is the closure
    *after* a small number of targeted clean verdicts have been folded in.

    A probe is bought only when a **clean answer would flip the decision**. That
    is value-of-information, not a threshold: if the gate would still say
    RESTART even with the most favourable possible answer, the probe cannot
    change what happens and is not worth its cost. If the gate already says
    INVESTIGATE, no probe is needed either.

    WHY THIS IS NOT THE FALSIFIED "PEEK" GATE
    ------------------------------------------
    The peek gate sampled the first `k` candidates in frontier order and used
    the tainted *rate* as an estimate of `f`. It failed because the rate is not
    `f` (measured correlation 0.234) -- the candidate pool is already the
    contaminated region, so a high rate is guaranteed by construction.

    This does not estimate anything. A clean verdict at a dominating pair
    removes the dominated part of the closure *exactly*, by the contamination
    walk's own semantics. One verdict, one deterministic consequence -- not a
    sample of a population.
    """

    def gate(workflow: Workflow, oracle: ProbeOracle) -> bool:
        n = max(1, workflow.n_restart)
        influence: set[tuple[str, str]] = set()
        checked: set[tuple[str, str]] = set()

        for _ in range(max_probes):
            total = a_hat(workflow, influence, checked) / n + \
                _f_structural(workflow, influence, checked)
            if total <= 1.0:
                return True                      # already pays; no probe needed

            # Candidate pairs: unsettled pairs whose source is contaminated.
            from src.provenance.contamination import contaminate

            region = contaminate(workflow.trace, set(workflow.truth.planted),
                                 influence=influence, checked=checked)
            candidates = [
                (sid, e.id)
                for e in workflow.trace.events
                for sid in e.exposures
                if sid in region.sources and (sid, e.id) not in checked
            ]
            if not candidates:
                return total <= 1.0

            best = max(candidates,
                       key=lambda p: _dominated_mass(workflow, p, influence,
                                                     checked))
            # VALUE OF INFORMATION: would a clean answer change the decision?
            optimistic = a_hat(workflow, influence, checked | {best}) / n + \
                _f_structural(workflow, influence, checked | {best})
            if optimistic > 1.0:
                return False        # even the best case still says restart

            hit = oracle.probe(*best)
            checked.add(best)
            if hit:
                influence.add(best)

        return (a_hat(workflow, influence, checked) / n
                + _f_structural(workflow, influence, checked)) <= 1.0

    return gate


def make_sound_bottleneck_gate(max_probes: int = 3) -> Gate:
    """H2 -- H1, but a clean verdict is only believed when it is SOUND.

    `removability.check()` (D-066) asks whether redacting a source actually
    removes its information from the prompt. It is pure string work: free, no
    model call. When it fails, a "clean" counterfactual verdict means nothing --
    a second source carried the same material, so the output was always going to
    be unchanged.

    H1 trusts every clean verdict and therefore inherits that blind spot. Here a
    clean verdict on a NON-removable pair is discarded and the pair is treated as
    contaminated, which is the conservative direction and costs preserved work
    rather than safety.

    This is a soundness precondition, not a tuned rule: it refuses to use
    evidence the repository already knows to be invalid.
    """

    def gate(workflow: Workflow, oracle: ProbeOracle) -> bool:
        from src.provenance.contamination import contaminate

        n = max(1, workflow.n_restart)
        influence: set[tuple[str, str]] = set()
        checked: set[tuple[str, str]] = set()

        for _ in range(max_probes):
            total = a_hat(workflow, influence, checked) / n +                 _f_structural(workflow, influence, checked)
            if total <= 1.0:
                return True

            region = contaminate(workflow.trace, set(workflow.truth.planted),
                                 influence=influence, checked=checked)
            candidates = [
                (sid, e.id)
                for e in workflow.trace.events
                for sid in e.exposures
                if sid in region.sources and (sid, e.id) not in checked
                # A pair whose verdict could not be believed is not worth buying.
                and workflow.is_removable(sid, e.id)
            ]
            if not candidates:
                return total <= 1.0

            best = max(candidates,
                       key=lambda p: _dominated_mass(workflow, p, influence,
                                                     checked))
            optimistic = a_hat(workflow, influence, checked | {best}) / n +                 _f_structural(workflow, influence, checked | {best})
            if optimistic > 1.0:
                return False

            hit = oracle.probe(*best)
            checked.add(best)
            if hit:
                influence.add(best)

        return (a_hat(workflow, influence, checked) / n
                + _f_structural(workflow, influence, checked)) <= 1.0

    return gate


# --- H3: span propagation, free -----------------------------------------------


def gate_span(workflow: Workflow, oracle: ProbeOracle) -> bool:
    """H3 -- zero-cost: does any flagged span actually appear downstream?

    Uses `carries_span`, the text-level relation the repository can already
    compute for free from stored outputs (`signatures.carried_spans`). The
    contaminated fraction is recomputed treating every pair that carries NO span
    as clean.

    It costs nothing and it is blind to semantic influence by construction --
    a source that changed a decision without leaving its wording behind is
    invisible here. The `semantic_one` pattern exists to exercise exactly that.
    """
    n = max(1, workflow.n_restart)
    all_pairs = {(s, e.id) for e in workflow.trace.events for s in e.exposures}
    # Everything with no observable span is treated as clean.
    checked = {p for p in all_pairs if p not in workflow.truth.carries_span}
    influence = set(workflow.truth.carries_span)
    return (a_hat(workflow, influence, checked) / n
            + _f_structural(workflow, influence, checked)) <= 1.0


# --- scoring ------------------------------------------------------------------


@dataclass
class Outcome:
    case: str
    topology: str
    pattern: str
    decision: bool
    oracle: bool
    probe_tokens: int
    probe_calls: int
    n: int
    a_full: int
    r: int
    f_true: float
    f_struct: float
    realized: int
    best: int
    unsafe: int

    @property
    def verdict(self) -> str:
        if self.decision == self.oracle:
            return "correct INVESTIGATE" if self.oracle else "correct RESTART"
        return "FALSE RECOVERY" if self.decision else "FALSE RESTART"

    @property
    def regret(self) -> int:
        return self.realized - self.best


def evaluate(name: str, gate: Gate, workflows: list[Workflow],
             full: dict[str, tuple[int, set[str]]]) -> list[Outcome]:
    out: list[Outcome] = []
    for workflow in workflows:
        oracle_probe = ProbeOracle(workflow)
        decision = gate(workflow, oracle_probe)

        a_full, concluded = full[workflow.name]
        n, r = workflow.n_restart, workflow.r_replay
        recover_cost = a_full + min(r, n)
        oracle_decision = recover_cost < n

        # Cost of following this gate. A gate's probes are part of the
        # investigation it then completes, so they are not charged twice when it
        # decides to investigate.
        if decision:
            realized = max(recover_cost, oracle_probe.tokens)
            # Unsafe preservations are a property of what the INVESTIGATION
            # concluded, not of the gate -- a gate that restarts preserves
            # nothing and can never be unsafe.
            unsafe = len(workflow.true_region() - concluded)
        else:
            realized = oracle_probe.tokens + n
            unsafe = 0

        out.append(Outcome(
            case=workflow.name, topology=workflow.topology,
            pattern=workflow.pattern, decision=decision,
            oracle=oracle_decision, probe_tokens=oracle_probe.tokens,
            probe_calls=oracle_probe.calls, n=n, a_full=a_full, r=r,
            f_true=workflow.f_true, f_struct=workflow.f_structural,
            realized=realized, best=min(recover_cost, n), unsafe=unsafe,
        ))
    return out
