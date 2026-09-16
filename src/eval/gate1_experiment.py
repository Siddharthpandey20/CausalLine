"""Gate 1: should we spend anything investigating, or restart now?

WHAT THIS MODULE IS FOR
-----------------------
CausalLine already has Gate 2 -- once a recovery plan exists, the planner
compares selective replay against a full restart and takes the cheaper. Gate 2
is sound and stays.

Gate 1 is the decision *before* the investigation: `A` may be spent discovering
that contamination is everywhere, and a restart would have been cheaper all
along. There is currently no such gate; the only condition on the production
path is `if refine and flagged:`.

This module does NOT implement a gate. It is the experiment that decides
whether one is worth having and which one.

THE ORACLE IS MEASURED, NOT LABELLED
-------------------------------------
For every case the **complete** process is run -- full investigation, planning,
selective replay -- and the three quantities are read off the trace:

    N        pipeline tokens (what a full restart re-spends)
    A_full   analysis tokens actually spent investigating
    R        replay tokens the executed selective plan actually cost

    ORACLE = RECOVER  iff  A_full + R < N

That is a measurement of what did happen, not a label about what should have.
No gate is allowed to define its own ground truth (Phase 6).

THE TAPE
--------
The trace is append-only and records `check` and `usage` records in the order
they were written, so the investigation can be replayed exactly:

    [(k, source, event, verdict, cumulative_analysis_tokens_after_k), ...]

A gate that stops after `k` checks therefore has an exact cost, and every
candidate gate is scored on the *same* tapes. Nothing is simulated: the
verdicts are the ones the estimator actually produced.

A gate that stops early and restarts costs `tape[k].cumulative + N`. A gate
that continues costs `A_full + min(R, N)` -- the `min` is Gate 2, which is
still there underneath and must not be double-counted.
"""

from __future__ import annotations

import json
import math
import random
import statistics as st
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from src.common.llm import LLMResponse

# --- a scripted client for the fan-out shape ---------------------------------


@dataclass
class FanoutScriptedClient:
    """Deterministic, free, and priced by the length of what it actually sent.

    WHY PROPORTIONAL PRICING IS NOT OPTIONAL HERE
    ----------------------------------------------
    The whole experiment is about `A/N`. A flat per-call price makes a full
    restart (a few large prompts) look as expensive as the analysis that avoids
    it (many small ones), which is the exact bias `src/eval/scripted.py`'s
    `cost_model` note already warns about. Tokens are charged as
    `len(text) / chars_per_token`, the same rule and divisor the scripted
    client uses.

    The usage rule is fixed and knowable: an analyst answers with the value in
    the document it can see, and follows a planted instruction **only while the
    instruction is visible in the prompt**. A replay that redacts the planted
    source therefore gets the honest answer, which is what makes recovery
    meaningful rather than stipulated.
    """

    docs: list[dict[str, Any]]
    marker: str
    token: str
    chars_per_token: int = 4
    model: str = "scripted-fanout"
    calls: int = 0
    total_tokens: int = 0
    throttled_s: float = 0.0
    # Probability that an analyst ignores a visible instruction. 0.0 is the
    # deterministic default; the noise experiments raise it.
    miss_rate: float = 0.0
    seed: int = 20260917
    _rng: random.Random = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def _answer(self, prompt: str) -> str:
        visible = [d for d in self.docs if d["name"].upper() in prompt]
        if len(visible) == 1:
            doc = visible[0]
            poisoned = doc.get("injected") and self.marker in prompt
            if poisoned and self._rng.random() >= self.miss_rate:
                return self.token
            return doc["value"]
        # The aggregator: reproduce every value it can see, in order.
        out = []
        for doc in self.docs:
            if self.token in prompt and doc.get("injected"):
                out.append(self.token)
            elif doc["value"] in prompt:
                out.append(doc["value"])
        return "\n".join(out)

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        json_output: bool = False,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls += 1
        text = self._answer(prompt)
        prompt_tokens = max(1, len(prompt + (system or "")) // self.chars_per_token)
        output_tokens = max(1, len(text) // self.chars_per_token)
        self.total_tokens += prompt_tokens + output_tokens
        return LLMResponse(
            text=text, model=self.model, prompt_tokens=prompt_tokens,
            output_tokens=output_tokens, total_tokens=prompt_tokens + output_tokens,
            thoughts_tokens=0, attempts=1, latency_s=0.0, slept_s=0.0,
        )


# --- the tape ----------------------------------------------------------------


@dataclass
class TapeStep:
    k: int
    source_id: str
    event_id: str
    tainted: bool
    cumulative_analysis: int


def read_tape(trace_path: Path) -> list[TapeStep]:
    """The investigation, replayed exactly, from the append-only trace.

    Counts every non-pipeline usage record, so self-report and counterfactual
    calls are both charged -- a gate that stops before a check still paid for
    whatever was issued to reach it.
    """
    steps: list[TapeStep] = []
    running = 0
    k = 0
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        kind = record.get("record")
        if kind == "usage" and record.get("purpose") not in (None, "pipeline", "replay"):
            running += int(record.get("total_tokens") or 0)
        elif kind == "check":
            k += 1
            steps.append(TapeStep(
                k=k,
                source_id=record.get("source_id", ""),
                event_id=record.get("target_event", ""),
                tainted=record.get("verdict") == "tainted",
                cumulative_analysis=running,
            ))
    return steps


# --- one measured case -------------------------------------------------------


@dataclass
class Case:
    """Everything the oracle and every gate need, measured from one full run."""

    case_id: str
    family: str          # "fanout" | "chain"
    shape: str           # human-readable design
    workers: int
    poisoned: int
    intent: str

    # measured, by running the complete process
    n_restart: int       # N
    a_full: int          # A
    r_replay: int        # R (what the executed plan actually cost)
    events: int
    escalated: bool
    unsafe: int
    task_success: bool

    # free structural signals, available BEFORE any analysis token is spent
    structural_prior: float
    compromise_p: float
    b2_events: int
    b1_events: int
    flagged_sources: int
    exposure_edges: int
    graph_depth: int
    max_fanout: int
    downstream_events: int
    region_pairs: int = 0
    mean_event_cost: float = 0.0

    tape: list[TapeStep] = field(default_factory=list)

    # --- the oracle ---
    @property
    def recover_cost(self) -> int:
        """A + min(R, N). The `min` is Gate 2, already in the system."""
        return self.a_full + min(self.r_replay, self.n_restart)

    @property
    def oracle_recover(self) -> bool:
        """Would investigating have been economically preferable? MEASURED."""
        return self.recover_cost < self.n_restart

    @property
    def oracle_margin(self) -> float:
        """How decisive the oracle is, as a fraction of N. Near 0 = borderline."""
        return (self.n_restart - self.recover_cost) / max(1, self.n_restart)

    @property
    def a_over_n(self) -> float:
        return self.a_full / max(1, self.n_restart)

    @property
    def f_true(self) -> float:
        return min(1.0, self.r_replay / max(1, self.n_restart))

    def to_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "tape"}
        out["tape"] = [s.__dict__ for s in self.tape]
        out["oracle_recover"] = self.oracle_recover
        out["oracle_margin"] = round(self.oracle_margin, 4)
        out["a_over_n"] = round(self.a_over_n, 4)
        out["f_true"] = round(self.f_true, 4)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Case":
        data = dict(data)
        tape = [TapeStep(**s) for s in data.pop("tape", [])]
        for derived in ("oracle_recover", "oracle_margin", "a_over_n", "f_true"):
            data.pop(derived, None)
        return cls(tape=tape, **data)


# --- gates -------------------------------------------------------------------


@dataclass
class GateDecision:
    recover: bool
    cost_spent: int          # analysis tokens the GATE itself spent
    checks_used: int
    detail: str = ""


Gate = Callable[[Case], GateDecision]


def gate_always_recover(case: Case) -> GateDecision:
    """The current production behaviour: no Gate 1 at all."""
    return GateDecision(True, 0, 0, "no gate")


def gate_always_restart(case: Case) -> GateDecision:
    """The degenerate opposite. Included because any proposed gate must beat
    both trivial policies to be worth its complexity."""
    return GateDecision(False, 0, 0, "no gate")


def gate_oracle(case: Case) -> GateDecision:
    """Perfect foresight. The ceiling, not a candidate."""
    return GateDecision(case.oracle_recover, 0, 0, "oracle")


def make_structural_gate(a_estimate: Callable[[Case], float]) -> Gate:
    """H2: the free cost-weighted B2 closure predicts the footprint well enough.

    Decision: investigate iff  Ahat/N + f_structural < 1.
    """

    def gate(case: Case) -> GateDecision:
        n = max(1, case.n_restart)
        ahat = a_estimate(case)
        total = ahat / n + case.structural_prior
        return GateDecision(
            total < 1.0, 0, 0,
            f"Ahat/N={ahat / n:.2f} + f_struct={case.structural_prior:.2f}"
            f" = {total:.2f}",
        )

    return gate


def make_p_gate(threshold: float) -> Gate:
    """H1: P (compromise probability) is informative about the footprint.

    Investigate only when P is below a threshold -- the reading under test is
    "a run very likely compromised is likely widely compromised".
    """

    def gate(case: Case) -> GateDecision:
        return GateDecision(
            case.compromise_p < threshold, 0, 0,
            f"P={case.compromise_p:.3f} vs {threshold}",
        )

    return gate


def make_peek_gate(k: int, a_estimate: Callable[[Case], float]) -> Gate:
    """H3: spend a small fixed budget, estimate f from the early verdicts.

    After `k` checks, the observed tainted fraction stands in for `f`, and the
    same economic rule decides. Costs exactly what those `k` checks cost.
    """

    def gate(case: Case) -> GateDecision:
        if not case.tape:
            return GateDecision(True, 0, 0, "no candidates")
        window = case.tape[:k]
        spent = window[-1].cumulative_analysis
        rate = sum(1 for s in window if s.tainted) / len(window)
        n = max(1, case.n_restart)
        ahat = a_estimate(case)
        total = ahat / n + rate
        return GateDecision(
            total < 1.0, spent, len(window),
            f"k={len(window)} rate={rate:.2f} Ahat/N={ahat / n:.2f}",
        )

    return gate


def make_sprt_gate(margin: float = 0.15, max_checks: int = 1000,
                   a_estimate: Callable[[Case], float] | None = None) -> Gate:
    """H0: cost-derived SPRT on the sequence of verdicts.

    Uses the production `SPRTState` with hypotheses from `config_for`, fed the
    real verdict sequence off the tape.
    """

    estimator = a_estimate or a_estimate_pre

    def gate(case: Case) -> GateDecision:
        from src.recovery.sprt_investigate import SPRTState, config_for

        a_estimate = estimator
        if not case.tape:
            return GateDecision(True, 0, 0, "no candidates")
        # A_full is NOT available at gate time. Using it here was a leak, and
        # it flattered the SPRT badly: it scored 94% on held-out with the true
        # analysis cost and is re-measured below with the estimate a deployed
        # gate would actually have.
        config = config_for(a_estimate(case), max(1, case.n_restart),
                            margin=margin)
        state = SPRTState(config=config)
        for step in case.tape[:max_checks]:
            state.observe(step.tainted)
            verdict = state.decision()
            if verdict == "abort_restart":
                return GateDecision(
                    False, step.cumulative_analysis, step.k,
                    f"SPRT abort at k={step.k} f*={config.f_star:.2f}",
                )
            if verdict == "proceed_selective":
                return GateDecision(
                    True, step.cumulative_analysis, step.k,
                    f"SPRT proceed at k={step.k}",
                )
        last = case.tape[min(len(case.tape), max_checks) - 1]
        return GateDecision(True, last.cumulative_analysis, last.k,
                            "SPRT never decided; continued")

    return gate


def make_hybrid_gate(k: int, a_estimate: Callable[[Case], float]) -> Gate:
    """H4: structural upper bound first, then a few real checks if it is close.

    The structural prior is an upper bound on `f`, so when even the upper bound
    says investigate, investigate without spending anything. Only when the
    upper bound says restart is the cheap empirical peek worth buying -- that
    is the only case where the bound might be wrong in the expensive direction.
    """

    def gate(case: Case) -> GateDecision:
        n = max(1, case.n_restart)
        ahat = a_estimate(case)
        optimistic = ahat / n + case.structural_prior
        if optimistic < 1.0:
            return GateDecision(True, 0, 0,
                                f"upper bound already pays ({optimistic:.2f})")
        if not case.tape:
            return GateDecision(False, 0, 0, "bound says no, nothing to check")
        window = case.tape[:k]
        spent = window[-1].cumulative_analysis
        rate = sum(1 for s in window if s.tainted) / len(window)
        total = ahat / n + rate
        return GateDecision(
            total < 1.0, spent, len(window),
            f"bound {optimistic:.2f} then k={len(window)} rate={rate:.2f}"
            f" -> {total:.2f}",
        )

    return gate


# --- A estimators, which every economic gate needs ---------------------------


def a_estimate_oracle(case: Case) -> float:
    """Perfect knowledge of A. Isolates error due to f alone."""
    return float(case.a_full)


def a_estimate_pre(case: Case) -> float:
    """A estimated from the trace ALONE, before any analysis token is spent.

    A counterfactual check re-issues one event's prompt, so it costs about what
    that event cost; the number of checks is bounded by the exposure pairs in
    the structural region. This is the same rule `_sprt_config_from_trace` uses
    in production, and it is an UPPER bound -- group testing, the frontier
    expansion and lazy self-report all cut the real count below it.

    `scale` corrects that bound. It is calibrated on the DEVELOPMENT set only
    and then frozen; see `docs/gate1/02-experiments.md`.
    """
    return A_SCALE * case.region_pairs * case.mean_event_cost


# Calibrated on the DEVELOPMENT family (65 cases) and frozen before the
# held-out set was ever built. Median of A_full / (region_pairs *
# mean_event_cost) = 0.431; the raw product is an upper bound because group
# testing, the frontier expansion and lazy self-report all settle more than one
# pair per call. Spread on dev: 0.057 .. 0.533.
A_SCALE = 0.431


def a_estimate_structural(case: Case) -> float:
    """Kept under its old name so the comparison table stays readable."""
    return a_estimate_pre(case)


def _per_check_cost(case: Case) -> float:
    """LEAK -- mean analysis cost per check, known only AFTER the
    investigation. Retained solely to label the diagnostic rows that use it."""
    if not case.tape:
        return 0.0
    return case.tape[-1].cumulative_analysis / max(1, len(case.tape))


# --- scoring -----------------------------------------------------------------


@dataclass
class GateScore:
    name: str
    n: int
    false_restart: int
    false_recovery: int
    correct: int
    gate_tokens: list[int]
    realized_cost: list[float]
    oracle_cost: list[float]
    restart_cost: list[float]
    always_recover_cost: list[float]
    borderline_errors: int = 0
    borderline_n: int = 0

    @property
    def false_restart_rate(self) -> float:
        return self.false_restart / max(1, self.n)

    @property
    def false_recovery_rate(self) -> float:
        return self.false_recovery / max(1, self.n)

    @property
    def accuracy(self) -> float:
        return self.correct / max(1, self.n)

    @property
    def total_vs_restart(self) -> float:
        return sum(self.realized_cost) / max(1.0, sum(self.restart_cost))

    @property
    def regret_vs_oracle(self) -> float:
        return (sum(self.realized_cost) - sum(self.oracle_cost)) / max(
            1.0, sum(self.oracle_cost))

    def line(self) -> str:
        p95 = (sorted(self.gate_tokens)[int(0.95 * (len(self.gate_tokens) - 1))]
               if self.gate_tokens else 0)
        border = (f"{self.borderline_errors}/{self.borderline_n}"
                  if self.borderline_n else "-")
        return (
            f"  {self.name:<26}{self.accuracy:>8.1%}"
            f"{self.false_restart_rate:>10.1%}{self.false_recovery_rate:>11.1%}"
            f"{st.mean(self.gate_tokens) if self.gate_tokens else 0:>9.0f}"
            f"{p95:>7.0f}{self.total_vs_restart:>10.2f}"
            f"{self.regret_vs_oracle:>10.1%}{border:>10}"
        )


BORDERLINE = 0.10   # |oracle margin| below this fraction of N is borderline


def score_gate(name: str, gate: Gate, cases: Iterable[Case]) -> GateScore:
    """Realized cost of following this gate, against the measured oracle."""
    score = GateScore(name, 0, 0, 0, 0, [], [], [], [], [])
    for case in cases:
        decision = gate(case)
        score.n += 1
        score.gate_tokens.append(decision.cost_spent)

        # What following this decision actually costs.
        if decision.recover:
            # The gate's own spend is part of the full investigation, not extra:
            # a peek gate's k checks are the first k checks of A_full.
            realized = case.recover_cost
        else:
            realized = decision.cost_spent + case.n_restart
        score.realized_cost.append(realized)
        score.oracle_cost.append(min(case.recover_cost, case.n_restart))
        score.restart_cost.append(case.n_restart)
        score.always_recover_cost.append(case.recover_cost)

        borderline = abs(case.oracle_margin) < BORDERLINE
        if borderline:
            score.borderline_n += 1
        if decision.recover == case.oracle_recover:
            score.correct += 1
        else:
            if borderline:
                score.borderline_errors += 1
            if decision.recover:
                score.false_recovery += 1
            else:
                score.false_restart += 1
    return score


def header() -> str:
    return (f"  {'gate':<26}{'acc':>8}{'falseRST':>10}{'falseREC':>11}"
            f"{'gateTok':>9}{'p95':>7}{'vsRstrt':>10}{'regret':>10}"
            f"{'border':>10}")
