"""Is the deferred self-report's "quality loss" an effect, or is it noise?

    python -m src.eval.selfreport_pairing --diagnose      # per-pair, one seed
    python -m src.eval.selfreport_pairing --sweep 30      # the replication
    python -m src.eval.selfreport_pairing --sweep 100     # tighter, ~1 hour

THE QUESTION
------------
`gate1/cost_without_quality_loss.md` disqualified the deferred self-report on
the chain matrix because it recovered **3.6 points less work** than the inline
baseline. `gate1/final_cost_direction.md` then closed the whole cost direction
partly on that figure. Both measured it **once, at one seed, on six cells**.

`ScriptedClient` injects a 25% self-report false-negative rate and a 10%
false-positive rate, so that matrix has sampling noise of its own, and its size
had never been reported. This module reports it.

WHY THE ARMS WERE NOT COMPARABLE
--------------------------------
`ScriptedClient._answer_self_report` draws its lie from `self._rng`, one stream
shared with the task answers. The draw a given (event, source) pair receives
therefore depends on how many draws preceded it -- and that differs between the
arms by construction, because the inline arm makes its self-report calls during
the pipeline and the deferred arm makes them later, interleaved with
counterfactual replays. Comparing them at one seed compares two different coins.

`PairedScriptedClient` below keys the lie to `(seed, audited output, source id)`
instead of to call order. The marginal rates are unchanged -- a hash is uniform
in [0,1) just as `random()` is -- so the client is exactly as imperfect, but
both arms now get the same answer for the same pair. That is common random
numbers, the standard fix for a paired comparison.

WHAT THIS MODULE MUST NOT DO
----------------------------
It patches `src.eval.experiment.ScriptedClient` for the duration of a sweep.
That is a measurement device, not a configuration: nothing here is imported by
the production evaluation, the patch is applied inside `main()` and never at
import time, and `src/eval/scripted.py` is not modified. Making the shipped
client pair-keyed would change every scripted number in the repository and is a
deliberate re-baselining, not a quiet fix (docs/15 section 8).

Findings: `docs/15-cost-direction-reopened.md`. Raw output:
`data/runs/seedsweep/sweep-30.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from src.eval import experiment as EX
from src.eval.scripted import ScriptedClient, _catalogue_ids

ARMS = ("hybrid", "lazy", "targeted_only")
WORK = Path("data/runs/seedsweep")
# Arbitrary but fixed, so the sweep is reproducible rather than whatever the
# clock said. Seed 0 is `experiment.run_cell`'s own default, so a 1-seed sweep
# reproduces the single-seed figure the documents report.
BASE_SEED = 20260906
SEED_STRIDE = 1000


class PairedScriptedClient(ScriptedClient):
    """Same rates, same imperfection; the lie is keyed to the pair, not to
    call order, so two arms asking about the same pair get the same answer."""

    def _draw(self, *parts: str) -> float:
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") / float(1 << 64)

    def _answer_self_report(self, prompt: str) -> str:
        if self.self_report_broken:
            return "I considered everything carefully."
        listed = _catalogue_ids(prompt)
        record = self._audited(prompt)
        truly_used = set(record.used) if record else set()
        # The audited OUTPUT identifies the call without reference to when it
        # was asked about, which is the whole point.
        audited = hashlib.sha256(
            (record.text if record else "").encode("utf-8")).hexdigest()[:16]

        used: list[str] = []
        unused: list[str] = []
        confidence: dict[str, float] = {}
        for sid in listed:
            actually = sid in truly_used
            rate = (self.self_report_false_negative_rate if actually
                    else self.self_report_false_positive_rate)
            lie = self._draw(str(self.seed), audited, sid, "lie") < rate
            (used if actually != lie else unused).append(sid)
            spread = self._draw(str(self.seed), audited, sid, "conf")
            confidence[sid] = round((0.35 + 0.35 * spread) if lie
                                    else (0.6 + 0.35 * spread), 2)
        return json.dumps({"used": used, "unused": unused,
                           "confidence": confidence})


# --- one matrix -------------------------------------------------------------

def matrix(arm: str, seed: int, tag: str) -> dict[str, Any]:
    """The six CausalLine cells, totalled."""
    rows = []
    for scenario in EX.SCENARIOS:
        for _variant, influencing in EX.VARIANTS:
            rows.extend(EX.run_cell(scenario, influencing,
                                    estimator_mode=arm, seed=seed,
                                    workdir=WORK / tag / arm))
    ours = [r for r in rows if r.method == "CausalLine"]
    return {
        "work_preserved": statistics.fmean(r.work_preserved for r in ours),
        "analysis_tokens": sum(r.analysis_tokens for r in ours),
        "recovery_tokens": sum(r.recovery_tokens for r in ours),
        "total_tokens": sum(r.analysis_tokens + r.recovery_tokens
                            for r in ours),
        "unsafe": sum(r.unsafe_preservations for r in ours),
        "pair_fn": sum(r.pair_false_negatives for r in ours),
        "pair_scored": sum(r.pair_scored for r in ours),
        "escalations": sum(r.escalations for r in ours),
        "task_success": sum(1 for r in ours if r.task_success),
        "cells": len(ours),
        "per_cell": [(f"{r.scenario}/{r.variant}", round(r.work_preserved, 4))
                     for r in ours],
    }


# --- the per-pair diagnosis -------------------------------------------------

def diagnose(seed: int = BASE_SEED) -> None:
    """Where the single-seed gap actually comes from, pair by pair.

    Reports, per cell: which events each arm asked a self-report about, which
    of the ones only the inline arm asked about are inside the B2 closure --
    the set a closure-scoped deferral would have covered -- and which
    (source, event) pairs ended with a different verdict.
    """
    from src.eval.baselines import b2_topology_closure
    from src.eval.closure_scoped import model_events
    from src.eval.detectors import Oracle
    from src.tracing.logger import read_trace

    def facts(path: Path) -> dict[str, Any]:
        trace = read_trace(path)
        flagged = Oracle().flag(trace).sources()
        return {
            "asked": {u.event_id for u in trace.usage
                      if u.purpose == "self_report" and u.event_id},
            "checks": {(c.source_id, c.target_event): (c.verdict, c.method)
                       for c in trace.checks},
            "closure": set(b2_topology_closure(trace, flagged)),
            "flagged": flagged,
            "model_events": model_events(trace),
        }

    totals = {"in_closure": 0, "outside": 0, "differing": 0}
    for scenario in EX.SCENARIOS:
        for variant, influencing in EX.VARIANTS:
            arms: dict[str, Any] = {}
            preserved: dict[str, float] = {}
            for arm in ("hybrid", "lazy"):
                rows = EX.run_cell(scenario, influencing, estimator_mode=arm,
                                   seed=seed, workdir=WORK / "diagnose" / arm)
                preserved[arm] = next(r.work_preserved for r in rows
                                      if r.method == "CausalLine")
                arms[arm] = facts(
                    WORK / "diagnose" / arm
                    / f"exp-{scenario}-{variant}-{arm}-oracle.jsonl")

            inline, deferred = arms["hybrid"], arms["lazy"]
            print("=" * 78)
            print(f"{scenario}/{variant}   hybrid {preserved['hybrid']:.3f}   "
                  f"lazy {preserved['lazy']:.3f}   "
                  f"delta {preserved['lazy'] - preserved['hybrid']:+.3f}")
            print(f"  flagged {inline['flagged']}   "
                  f"{len(inline['model_events'])} model events   "
                  f"closure {len(inline['closure'])}")
            print(f"  asked: hybrid {len(inline['asked'])}   "
                  f"lazy {len(deferred['asked'])}")

            missed = inline["asked"] - deferred["asked"]
            in_closure = {e for e in missed if e in inline["closure"]}
            totals["in_closure"] += len(in_closure)
            totals["outside"] += len(missed - in_closure)
            print(f"  asked by hybrid only: {len(missed)}   "
                  f"inside the closure {len(in_closure)} "
                  f"(what closure scoping would add)   "
                  f"outside {len(missed - in_closure)} (provably irrelevant)")

            pairs = set(inline["checks"]) | set(deferred["checks"])
            differing = [(p, inline["checks"].get(p), deferred["checks"].get(p))
                         for p in sorted(pairs)
                         if inline["checks"].get(p) != deferred["checks"].get(p)]
            totals["differing"] += len(differing)
            print(f"  pairs with a different verdict: {len(differing)}")
            for pair, a, b in differing:
                where = "in-closure" if pair[1] in inline["closure"] else "OUT"
                flag = "  <-- BOTH ARMS ASKED" if (
                    pair[1] in inline["asked"] and pair[1] in deferred["asked"]
                    and a and b and a[0] != b[0]) else ""
                print(f"     {pair[0]:>4} -> {pair[1]:<7} {where:<11} "
                      f"hybrid={a}  lazy={b}{flag}")

    print("=" * 78)
    print(f"events the inline arm asked about and the deferred arm did not:")
    print(f"  inside the closure  {totals['in_closure']:>3}  "
          f"<- a closure-scoped deferral would cover these")
    print(f"  outside the closure {totals['outside']:>3}  "
          f"<- provably irrelevant to the outcome (290/290 traces)")
    print(f"pairs ending with a different verdict: {totals['differing']}")
    print("\nA pair marked BOTH ARMS ASKED cannot be explained by scope: the "
          "two arms\nasked the same question and recorded different answers.")


# --- the sweep --------------------------------------------------------------

def paired_difference(a: list[dict], b: list[dict], key: str) -> dict[str, Any]:
    """b - a per seed, with the spread that decides whether it means anything."""
    diffs = [y[key] - x[key] for x, y in zip(a, b)]
    n = len(diffs)
    mean = statistics.fmean(diffs)
    sd = statistics.stdev(diffs) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 else 0.0
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    return {
        "mean": mean, "sd": sd, "lo": lo, "hi": hi,
        "crosses_zero": lo <= 0 <= hi,
        "wins": sum(1 for d in diffs if d > 1e-9),
        "ties": sum(1 for d in diffs if abs(d) <= 1e-9),
        "losses": sum(1 for d in diffs if d < -1e-9),
        "diffs": diffs,
    }


def sweep(tag: str, seeds: list[int]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {a: [] for a in ARMS}
    started = time.time()
    for i, seed in enumerate(seeds, 1):
        for arm in ARMS:
            out[arm].append(matrix(arm, seed, tag))
        if i % 5 == 0 or i == len(seeds):
            print(f"    {tag}: {i}/{len(seeds)} seeds "
                  f"({time.time() - started:.0f}s)", flush=True)
    return out


def render(tag: str, data: dict[str, list[dict]]) -> str:
    n = len(data["hybrid"])
    lines = ["=" * 78, f"{tag}  --  {n} seeds x 6 cells", "=" * 78]
    for arm in ARMS:
        rows = data[arm]
        w = [r["work_preserved"] for r in rows]
        lines.append(
            f"  {arm:<15}{statistics.fmean(w):>7.1%} "
            f"(sd {statistics.stdev(w) * 100 if n > 1 else 0:>4.2f} pts)  "
            f"analysis {statistics.fmean(r['analysis_tokens'] for r in rows):>6.0f}  "
            f"total {statistics.fmean(r['total_tokens'] for r in rows):>6.0f}  "
            f"unsafe {sum(r['unsafe'] for r in rows):>3}  "
            f"pairFN {sum(r['pair_fn'] for r in rows):>3}"
            f"/{sum(r['pair_scored'] for r in rows):<5} "
            f"task {sum(r['task_success'] for r in rows)}/{6 * n}")
    lines += ["", f"  paired per-seed differences",
              f"  {'comparison':<34}{'mean':>9}{'sd':>8}{'95% lo':>9}"
              f"{'95% hi':>9}  {'CI~0':>5}  W/T/L",
              "  " + "-" * 82]
    for key, scale, unit in (("work_preserved", 100, "pts"),
                             ("total_tokens", 1, "tok")):
        for arm in ("lazy", "targeted_only"):
            d = paired_difference(data["hybrid"], data[arm], key)
            lines.append(
                f"  {arm + ' - hybrid (' + unit + ')':<34}"
                f"{d['mean'] * scale:>9.2f}{d['sd'] * scale:>8.2f}"
                f"{d['lo'] * scale:>9.2f}{d['hi'] * scale:>9.2f}"
                f"{'yes' if d['crosses_zero'] else 'NO':>6}  "
                f"{d['wins']}/{d['ties']}/{d['losses']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--diagnose", action="store_true",
                        help="per-pair account of the single-seed gap")
    parser.add_argument("--sweep", type=int, default=0,
                        help="replicate across this many seeds")
    parser.add_argument("--regimes", default="shipped,paired",
                        help="which randomness regimes to run")
    parser.add_argument("--out", default=str(WORK / "sweep.json"))
    args = parser.parse_args(argv)

    if not args.diagnose and not args.sweep:
        parser.error("choose --diagnose or --sweep N")

    WORK.mkdir(parents=True, exist_ok=True)
    if args.diagnose:
        diagnose()
        if not args.sweep:
            return 0
        print()

    seeds = [BASE_SEED + SEED_STRIDE * i for i in range(args.sweep)]
    regimes = [r.strip() for r in args.regimes.split(",") if r.strip()]
    results: dict[str, Any] = {"n_seeds": args.sweep, "seeds": seeds}

    print(f"{args.sweep} seeds x {len(ARMS)} arms x {len(regimes)} regime(s) "
          f"= {args.sweep * len(ARMS) * len(regimes)} matrices\n")
    rendered = []
    for regime in regimes:
        if regime == "paired":
            # Applied here, never at import time: this is a measurement
            # device and must not be reachable from the production evaluation.
            EX.ScriptedClient = PairedScriptedClient
            print("  paired randomness (common random numbers)")
        else:
            EX.ScriptedClient = ScriptedClient
            print("  as-shipped randomness (arms unpaired)")
        data = sweep(regime, seeds)
        results[regime] = data
        rendered.append(render(
            "AS SHIPPED" if regime == "shipped" else "PAIRED", data))
    EX.ScriptedClient = ScriptedClient

    print()
    for block in rendered:
        print(block)
        print()
    Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
