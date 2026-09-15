"""
Phase 0: what the recovery planner actually selects, counted rather than claimed.

An audit reported that across 24 configurations `invalidate` and `replay` were
selected 73 and 22 times while `restart(agent)`, `isolate` and `restart_all`
were selected **zero** times, and concluded from that that the safe
frontier / checkpoint machinery has no practical effect. Earlier campaigns had
observed escalation reaching `restart_all` under the blind and pessimistic
detectors, so the two statements cannot both be describing the same quantity.

They are not. This module measures three different things and prints them
apart, because conflating them is what produced the disagreement:

  first choice     what `plan_recovery()` -> `greedy_cover()` returns. This is
                   the planner's own selection over the action vocabulary and
                   it is the only thing the audit's count could have been.

  after escalation what actually ran. `recover()` may reject the selective plan
                   at verification and widen to `agent_restart` and then
                   `restart_all` (src/recovery/verify.py). Those widenings are
                   NOT actions from the vocabulary -- they are scopes -- which
                   is precisely why they are invisible to a count of selected
                   actions.

  frontier bound   whether the safe frontier is inert or merely out-bid. For
                   every configuration we record the cheapest `restart(agent)`
                   the vocabulary offered, what that same restart would have
                   cost from INIT instead of from the frontier, and what the
                   winner cost. A frontier that never wins but still bounds the
                   price of the action it loses to is doing something; a
                   frontier that is always INIT is not.

    python -m src.eval.action_census            # 24 configurations
    python -m src.eval.action_census --quick    # oracle only, 6 configurations
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.eval.attacks import build
from src.eval.detectors import build as build_detector

# Registers "heuristic" and "classifier" in the detector registry, exactly as
# src/eval/campaign.py does. Imported for the side effect so this census covers
# the same four detectors the campaign matrix does.
import src.eval.classifier_detector  # noqa: F401,E402
from src.eval.experiment import SCENARIOS, VARIANTS, _fresh_tools, _original_run
from src.eval.scripted import ScriptedClient
from src.recovery.causalline import recover
from src.recovery.planner import INIT, candidate_actions, plan_recovery, restart_action
from src.recovery.verify import ESCALATION_ORDER
from src.tracing.checkpoints import CheckpointStore, checkpoint_path_for
from src.tracing.graphs import EventGraph
from src.tracing.logger import read_trace

ACTION_KINDS = ("invalidate", "replay", "restart", "isolate", "restart_all")
DETECTORS = ("oracle", "heuristic", "pessimistic", "blind")


@dataclass
class ConfigCensus:
    """One (scenario, variant, detector) configuration."""

    scenario: str
    variant: str
    detector: str
    flagged: list[str] = field(default_factory=list)
    taint_events: int = 0
    first_choice: list[str] = field(default_factory=list)
    first_choice_cost: int = 0
    final_scope: str = ""
    scopes_run: list[str] = field(default_factory=list)
    escalations: int = 0
    verified: bool = False
    # Step 1 evidence. Every "ratio" here is greedy_cover()'s own score,
    # cost / targets-broken, evaluated on the first iteration where the whole
    # taint is still uncovered -- i.e. the comparison the greedy actually made.
    frontiers: dict[str, str] = field(default_factory=dict)
    winner: str = ""
    winner_ratio: float | None = None
    best_restart: str = ""
    best_restart_ratio: float | None = None
    best_restart_from_init_ratio: float | None = None
    restart_all_ratio: float | None = None
    # agent -> (cost of restart(agent) from its frontier, cost from INIT).
    # Every agent, not only the best-scoring one: "the frontier never wins the
    # greedy" and "the frontier never shrinks anything" are different claims
    # and only the second one would make Step 1 inert.
    restart_costs: dict[str, list[int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "variant": self.variant,
            "detector": self.detector,
            "flagged": self.flagged,
            "taint_events": self.taint_events,
            "first_choice": self.first_choice,
            "first_choice_cost": self.first_choice_cost,
            "final_scope": self.final_scope,
            "scopes_run": self.scopes_run,
            "escalations": self.escalations,
            "verified": self.verified,
            "frontiers": self.frontiers,
            "winner": self.winner,
            "winner_ratio": self.winner_ratio,
            "best_restart": self.best_restart,
            "best_restart_ratio": self.best_restart_ratio,
            "best_restart_from_init_ratio": self.best_restart_from_init_ratio,
            "restart_all_ratio": self.restart_all_ratio,
            "restart_costs": self.restart_costs,
        }


def census_for(
    scenario: str,
    influencing: bool,
    detector_name: str,
    workdir: Path,
    seed: int = 20260906,
) -> ConfigCensus:
    variant = "influencing" if influencing else "exposed_only"
    stem = f"census-{scenario}-{variant}-{detector_name}"
    path = workdir / f"{stem}.jsonl"

    _outcome, _client, _truth = _original_run(scenario, influencing, path, seed)
    original = read_trace(path)
    original.validate()
    attack = build(scenario, influencing)
    verdict = build_detector(detector_name).flag(original)
    flagged = verdict.sources()
    checkpoints = CheckpointStore.load(checkpoint_path_for(path))

    out = ConfigCensus(
        scenario=scenario, variant=variant, detector=detector_name, flagged=flagged
    )
    if not flagged:
        # A detector that names nothing gives the planner nothing to plan. The
        # configuration is still counted: "no action selected" is the honest
        # entry, and dropping it would let the blind control vanish from a
        # table whose whole purpose is to show the control firing.
        out.final_scope = "not-run (detector flagged nothing)"
        return out

    plan = plan_recovery(original, flagged, checkpoints)
    out.taint_events = len(plan.taint.events)
    out.first_choice = [a.kind for a in plan.selected]
    out.first_choice_cost = sum(a.cost for a in plan.selected)
    out.frontiers = {
        agent: ("INIT" if cp is INIT else cp.event_id)
        for agent, cp in plan.frontiers.items()
    }

    order = EventGraph.from_trace(original).topological_order()
    actions = candidate_actions(original, set(plan.taint.events), plan.frontiers, order)
    taint = set(plan.taint.events)

    def ratio(action: Any) -> float | None:
        """greedy_cover()'s own score for this action on the first iteration.

        Step 2 covers one singleton target per tainted event (D-047), so the
        number of targets an action breaks is just how much of Taint it
        invalidates. An action that breaks nothing is never selected and gets
        None rather than infinity, so it cannot be silently ranked.
        """
        hits = len(action.invalidates & frozenset(taint))
        if action.kind == "restart_all":
            hits = len(taint)
        return action.cost / hits if hits else None

    if plan.selected:
        out.winner = plan.selected[0].label()
        out.winner_ratio = ratio(plan.selected[0])

    scored_restarts = [
        (r, a) for a in actions if a.kind == "restart" and (r := ratio(a)) is not None
    ]
    if scored_restarts:
        best_ratio, best = min(scored_restarts, key=lambda pair: pair[0])
        out.best_restart = best.label()
        out.best_restart_ratio = best_ratio
        # The same agent restarted with no frontier at all. If this equals the
        # frontier score, Step 1 bought nothing here; if it is larger, the
        # frontier bounded the price of an action that still lost.
        out.best_restart_from_init_ratio = ratio(
            restart_action(original, best.target, None, order)
        )
    out.restart_all_ratio = ratio(next(a for a in actions if a.kind == "restart_all"))

    for action in actions:
        if action.kind != "restart":
            continue
        from_init = restart_action(original, action.target, None, order)
        out.restart_costs[action.target] = [action.cost, from_init.cost]

    rec_out = workdir / f"{stem}-recover.jsonl"
    tools = _fresh_tools(attack, rec_out)
    recovered = recover(
        original,
        flagged,
        ScriptedClient(seed=seed + 1),
        rec_out,
        tools=tools,
        checkpoints=checkpoints,
        handoff_hook=attack.handoff_hook,
    )
    out.final_scope = recovered.scope
    out.escalations = recovered.escalations
    out.verified = recovered.ok
    # Every scope the ladder actually replayed at, not only the last one. A run
    # that ends `exhausted` replayed at selective, then agent_restart, then
    # restart_all -- counting only the final scope loses the two widenings in
    # between, which is the same conflation this module exists to undo.
    out.scopes_run = list(ESCALATION_ORDER[: recovered.escalations + 1])
    return out


def run_census(
    detectors: tuple[str, ...] = DETECTORS,
    workdir: str | Path = "data/runs/census",
) -> list[ConfigCensus]:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    rows: list[ConfigCensus] = []
    for detector in detectors:
        for scenario in SCENARIOS:
            for _name, influencing in VARIANTS:
                label = "influencing" if influencing else "exposed_only"
                print(f"-- {detector}/{scenario}/{label}", flush=True)
                rows.append(census_for(scenario, influencing, detector, workdir))
    return rows


# --- reporting ---------------------------------------------------------------


# What replaying at a scope corresponds to in action-vocabulary terms. The
# selective scope runs whatever the planner chose, so it has no fixed entry.
SCOPE_TO_ACTION = {
    "selective": None,
    "agent_restart": "restart",
    "restart_all": "restart_all",
}


def _cell(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def render(rows: list[ConfigCensus]) -> str:
    first: Counter = Counter()
    after: Counter = Counter()
    planned = 0
    for row in rows:
        if not row.first_choice:
            continue
        planned += 1
        first.update(row.first_choice)
        for scope in row.scopes_run:
            widened = SCOPE_TO_ACTION.get(scope)
            if widened is None:
                after.update(row.first_choice)
            else:
                after[widened] += 1

    lines = [
        f"{len(rows)} configurations "
        f"({len(SCENARIOS)} scenarios x {len(VARIANTS)} variants x "
        f"{len({r.detector for r in rows})} detectors); "
        f"{planned} produced a plan",
        "",
        "(a) PLANNER FIRST CHOICE -- what greedy_cover() selected",
        f"{'action':<14}{'times selected':>16}",
        "-" * 30,
    ]
    for kind in ACTION_KINDS:
        lines.append(f"{kind:<14}{first.get(kind, 0):>16}")
    lines += [
        "",
        "(b) AFTER ESCALATION -- every scope the ladder actually replayed at",
        f"{'action/scope':<14}{'times run':>16}",
        "-" * 30,
    ]
    for kind in ACTION_KINDS:
        lines.append(f"{kind:<14}{after.get(kind, 0):>16}")

    scopes = Counter(r.final_scope for r in rows)
    lines += ["", "final scope reached:"]
    for scope, n in sorted(scopes.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {scope:<34}{n:>4}")

    lines += [
        "",
        "(c) IS THE SAFE FRONTIER INERT, OR JUST OUT-BID?",
        "greedy score = cost / tainted events broken; lower wins.",
        f"{'det':<12}{'scen':<5}{'variant':<14}{'winner':<22}"
        f"{'score':>8}{'restart(a)':>12}{'same@INIT':>11}{'restart_all':>13}"
        f"{'front':>7}",
        "-" * 104,
    ]
    bounded = 0
    outbid = 0
    scorable = 0
    for row in sorted(rows, key=lambda r: (r.detector, r.scenario, r.variant)):
        if not row.first_choice:
            continue
        verified = sum(
            1 for a, e in row.frontiers.items() if e != "INIT" and a != "user"
        )
        if row.best_restart_ratio is not None:
            scorable += 1
            if (
                row.best_restart_from_init_ratio is not None
                and row.best_restart_ratio < row.best_restart_from_init_ratio
            ):
                bounded += 1
            if (
                row.winner_ratio is not None
                and row.winner_ratio <= row.best_restart_ratio
            ):
                outbid += 1
        lines.append(
            f"{row.detector:<12}{row.scenario:<5}{row.variant:<14}"
            f"{row.winner:<22}{_cell(row.winner_ratio):>8}"
            f"{_cell(row.best_restart_ratio):>12}"
            f"{_cell(row.best_restart_from_init_ratio):>11}"
            f"{_cell(row.restart_all_ratio):>13}{verified:>7}"
        )
    lines += [
        "",
        f"configurations where a restart(agent) was scorable at all: "
        f"{scorable}/{planned}",
        f"  ... of those, the frontier made it strictly cheaper than the same "
        f"restart from INIT: {bounded}/{scorable}",
        f"  ... of those, it lost or tied the greedy comparison on score: "
        f"{outbid}/{scorable}",
    ]

    pairs = 0
    shrunk = 0
    saved = 0
    for row in rows:
        for _agent, (frontier_cost, init_cost) in row.restart_costs.items():
            pairs += 1
            if frontier_cost < init_cost:
                shrunk += 1
                saved += init_cost - frontier_cost
    lines += [
        "",
        "(d) DOES THE FRONTIER SHRINK ANYTHING AT ALL? (every agent, not only "
        "the best-scoring one)",
        f"  (configuration, agent) restart actions offered: {pairs}",
        f"  restart(agent) strictly cheaper from its frontier than from INIT: "
        f"{shrunk} ({shrunk / pairs:.0%})" if pairs else "  none offered",
        f"  tokens the frontier removed from those restarts, in total: {saved}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    detectors = ("oracle",) if "--quick" in args else DETECTORS
    rows = run_census(detectors=detectors)
    print()
    print(render(rows))
    out = Path("data/results/action-census.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([r.to_dict() for r in rows], indent=2), encoding="utf-8")
    print()
    print(f"written to {out}")
