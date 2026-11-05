"""C2: the typed-interface ablation, on exact ground truth.

`FINAL_COST_OPTIMIZATION_RESEARCH.md` §9.3 item 3 asked for "a typed-interface
variant of the scripted chain (Researcher findings passed as parsed fields), to
show mechanism (b) on exact ground truth". This is it.

WHAT IS MANIPULATED, AND WHAT IS HELD CONSTANT
-----------------------------------------------
Exactly one thing changes between the two arms: what a producer hands to a
consumer at the researcher -> coder boundary.

  arm `free`  (the ablated version, and the shipped interface)
      the consumer receives the producer's REPLY VERBATIM, including the
      trailing `[ref <hash>]` provenance suffix.

  arm `typed` (full IREC with a typed interface)
      the consumer receives the finding's content field only. `[ref <hash>]`
      is dropped, because it is a pointer to what the producer consulted and
      not part of what the consumer reads.

Held identical: scenario, variant, workflow, seed, pricing model, contamination
placement, detector, the models (`ScriptedClient`, same seed in both arms), the
complete production prompt assembly, and **IREC's recovery logic byte-for-byte**
-- the typed interface is a property of the WORKLOAD, not of the recovery
method, so `src/recovery/irec.py` is identical in both arms.

The projection is applied only to model-produced findings. A planted
inter-agent message is forwarded untouched, so the ablation manipulates the
interface between honest agents and does not also alter the attack. That is a
deliberate design choice and it bounds what the result can say: a real typed
interface would also constrain what an attacker can inject, and not modelling
that makes this estimate of the interface's benefit conservative on safety and
possibly optimistic on nothing -- the attack path is unchanged in both arms.

WHY THE TWO ARMS' ABSOLUTE COSTS ARE NOT DIRECTLY COMPARABLE
-------------------------------------------------------------
Changing the interface changes the consumer's prompt, so the two arms produce
DIFFERENT original traces. That is the manipulation, not a flaw, but it means
absolute token counts across arms are confounded by the trace difference. The
comparable quantities are, per arm:

  * `R = C_IREC / min(C_B0, C_B2)` -- each arm's IREC against its OWN baselines
  * the mechanism counts: spliced / re-executed / cutoff-kept / changed
  * safety, recall, escapes, task success, verification, work preserved

    python -m docs.research.prototype.irec_typed_ablation

Free, deterministic, no key, no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.eval.attacks import build
from src.eval.baselines import b0_full_restart, b2_topology_closure, discarded_events
from src.eval.detectors import build as build_detector
from src.eval.experiment import (
    WORKFLOWS,
    _fresh_tools,
    _original_run,
    run_baseline_recovery,
)
from src.eval.metrics import ground_truth_events
from src.eval.scripted import ScriptedClient
from src.recovery.irec import recover_irec
from src.tracing.logger import read_trace

SEED = 20260906
REF_SUFFIX = re.compile(r"\s*\[ref [0-9a-f]+\]\s*$")


def typed_projection(text: str) -> str:
    """The typed field a consumer actually reads.

    The scripted chain's findings have the shape
    `'On <topic>: <body> [ref <hash>]'`. The `[ref ...]` suffix is a pointer to
    what the producer consulted; it moves whenever the consulted content moves,
    even when the substantive finding does not. A consumer that parses the
    finding reads the topic and the body. So the projection is the reply with
    that suffix removed -- nothing else is touched, and no similarity judgment
    is involved anywhere.
    """
    return REF_SUFFIX.sub("", text)


ARMS: dict[str, Any] = {"free": None, "typed": typed_projection}


def one_arm(arm: str, scenario: str, influencing: bool, workflow: str,
            cost_model: str, tmp: Path) -> dict:
    projection = ARMS[arm]
    variant = "influencing" if influencing else "exposed_only"
    stem = f"ab-{arm}-{scenario}-{variant}-{workflow}-{cost_model}"
    orig_path = tmp / f"{stem}.jsonl"

    # --- the original run, with this arm's interface ------------------------
    _result, _client, true_inf = _original_run(
        scenario, influencing, orig_path, SEED, "none", workflow=workflow,
        cost_model=cost_model, handoff_projection=projection)
    original = read_trace(orig_path)
    original.validate()
    attack = build(scenario, influencing)
    flagged = set(build_detector("oracle").flag(original).sources())
    truth = ground_truth_events(original, true_influence=true_inf)

    # --- IREC on it, identical recovery logic in both arms ------------------
    out = tmp / f"{stem}-IREC.jsonl"
    result = recover_irec(
        original, flagged, ScriptedClient(seed=SEED + 1, cost_model=cost_model),
        out, task=original.meta.get("task"),
        tools=_fresh_tools(attack, out, extended=(workflow == "long")),
        handoff_hook=attack.handoff_hook,
        handoff_projection=projection,
        workflow_kwargs=dict(WORKFLOWS[workflow]))
    discarded = set(discarded_events(result.report, result.invalidated))

    # --- this arm's own baselines, for the ratio ----------------------------
    base = {}
    for name, discard in (("B0", b0_full_restart(original, flagged)),
                          ("B2", b2_topology_closure(original, flagged))):
        bout = tmp / f"{stem}-{name}.jsonl"
        bres, brep = run_baseline_recovery(
            name, discard, original,
            ScriptedClient(seed=SEED + 1, cost_model=cost_model),
            sorted(flagged), attack, bout,
            tools=_fresh_tools(attack, bout, extended=(workflow == "long")))
        base[name] = {"tokens": brep.replay_tokens,
                      "unsafe": len(truth - set(discard)),
                      "task": bres.task_success}

    closure = set(b2_topology_closure(original, flagged))
    cheapest = min(base["B0"]["tokens"], base["B2"]["tokens"])
    # Did the projection actually take effect in this arm's trace?
    handoff_sources = [s.content for s in original.sources
                       if s.kind == "agent_message"]
    refs_present = sum(1 for c in handoff_sources if REF_SUFFIX.search(c))

    return {
        "arm": arm, "scenario": scenario, "variant": variant,
        "workflow": workflow, "cost_model": cost_model,
        "events": len(original.events),
        "model_events": len(result.report.spliced) + result.calls,
        "truth_events": len(truth),
        "contamination_fraction": len(truth) / max(1, len(original.events)),
        "flagged": sorted(flagged),
        # mechanism
        "spliced": result.spliced,
        "reexecuted": result.calls,
        "cutoff_kept": result.unchanged,
        "changed": result.changed,
        "cutoff_yield_num": result.unchanged,
        "cutoff_yield_den": result.calls,
        # safety and correctness, counts with denominators
        "discarded": len(discarded),
        "unsafe_num": len(truth - discarded),
        "unsafe_den": len(truth),
        "recall_num": len(truth & discarded),
        "recall_den": len(truth),
        "escapes_num": len(truth - closure),
        "escapes_den": len(truth),
        "task_success": result.task_success,
        "verify_ok": result.verification.ok,
        "verify_reasons": list(result.verification.reasons),
        "work_preserved_num": len(original.events) - len(discarded),
        "work_preserved_den": len(original.events),
        # cost, fully accounted
        "irec_tokens": result.replay_tokens,
        "irec_analysis_tokens": result.analysis_tokens,
        "irec_total": result.recovery_tokens,
        "b0_tokens": base["B0"]["tokens"],
        "b2_tokens": base["B2"]["tokens"],
        "R": result.recovery_tokens / cheapest if cheapest else None,
        "b2_unsafe": base["B2"]["unsafe"],
        "b0_task": base["B0"]["task"],
        "b2_task": base["B2"]["task"],
        # did the manipulation land?
        "handoff_sources": len(handoff_sources),
        "handoff_sources_with_ref": refs_present,
        "divergence": result.divergence,
        "notes": result.notes,
    }


def band(fraction: float) -> str:
    """Contamination extent, so results can be reported by size.

    The scripted chain has no small/medium/large regimes -- those belong to the
    56-agent workload, which has no typed handoff to ablate. Banding by the
    measured contaminated fraction is the nearest honest equivalent and the
    bands are stated rather than implied.
    """
    if fraction < 0.20:
        return "small (<20%)"
    if fraction < 0.40:
        return "medium (20-40%)"
    return "large (>=40%)"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=Path("data/runs/irec-typed/ablation.json"))
    args = ap.parse_args()

    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for cost_model in ("flat", "proportional"):
            for workflow in ("short", "long"):
                for scenario in ("A", "B", "C"):
                    for influencing in (True, False):
                        pair = {}
                        for arm in ("free", "typed"):
                            pair[arm] = one_arm(arm, scenario, influencing,
                                                workflow, cost_model, tmp)
                            rows.append(pair[arm])
                        f, t = pair["free"], pair["typed"]
                        print(f"{cost_model:<13}{workflow:<6}"
                              f"{scenario}/{f['variant']:<13}"
                              f"free: spl {f['spliced']:>2} re {f['reexecuted']:>2} "
                              f"cut {f['cutoff_kept']:>2} tok {f['irec_total']:>5} "
                              f"R {f['R']:.2f} unsafe {f['unsafe_num']}/{f['unsafe_den']}"
                              f"   |   typed: spl {t['spliced']:>2} "
                              f"re {t['reexecuted']:>2} cut {t['cutoff_kept']:>2} "
                              f"tok {t['irec_total']:>5} R {t['R']:.2f} "
                              f"unsafe {t['unsafe_num']}/{t['unsafe_den']}",
                              flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    report(rows)
    print(f"\nwritten to {args.out}")


def report(rows: list[dict]) -> None:
    import statistics as st

    def arm(name):
        return [r for r in rows if r["arm"] == name]

    print("\n" + "=" * 104)
    print("DID THE MANIPULATION LAND?")
    print("=" * 104)
    for name in ("free", "typed"):
        g = arm(name)
        tot = sum(r["handoff_sources"] for r in g)
        ref = sum(r["handoff_sources_with_ref"] for r in g)
        print(f"  {name:<6} handed-on agent_message sources carrying a "
              f"[ref ...] suffix: {ref}/{tot}")

    print("\n" + "=" * 104)
    print("SAFETY AND CORRECTNESS -- exact counts with denominators")
    print("=" * 104)
    print(f"  {'arm':<7}{'cells':>7}{'unsafe':>12}{'recall':>14}{'escapes':>12}"
          f"{'task ok':>10}{'verify ok':>11}")
    for name in ("free", "typed"):
        g = arm(name)
        print(f"  {name:<7}{len(g):>7}"
              f"{sum(r['unsafe_num'] for r in g):>7}/"
              f"{sum(r['unsafe_den'] for r in g):<4}"
              f"{sum(r['recall_num'] for r in g):>9}/"
              f"{sum(r['recall_den'] for r in g):<4}"
              f"{sum(r['escapes_num'] for r in g):>7}/"
              f"{sum(r['escapes_den'] for r in g):<4}"
              f"{sum(1 for r in g if r['task_success']):>6}/{len(g):<3}"
              f"{sum(1 for r in g if r['verify_ok']):>7}/{len(g):<3}")

    print("\n" + "=" * 104)
    print("MECHANISM AND COST, PAIRED PER CELL")
    print("=" * 104)
    print(f"  {'arm':<7}{'spliced':>9}{'re-exec':>9}{'cutoff kept':>13}"
          f"{'changed':>9}{'IREC tok':>10}{'R mean':>9}{'work kept':>11}")
    for name in ("free", "typed"):
        g = arm(name)
        print(f"  {name:<7}{sum(r['spliced'] for r in g):>9}"
              f"{sum(r['reexecuted'] for r in g):>9}"
              f"{sum(r['cutoff_kept'] for r in g):>13}"
              f"{sum(r['changed'] for r in g):>9}"
              f"{sum(r['irec_total'] for r in g):>10}"
              f"{st.fmean(r['R'] for r in g):>9.3f}"
              f"{sum(r['work_preserved_num'] for r in g)}/"
              f"{sum(r['work_preserved_den'] for r in g)}")

    print("\n  paired per-cell differences (typed - free):")
    keys = {}
    for r in rows:
        k = (r["cost_model"], r["workflow"], r["scenario"], r["variant"])
        keys.setdefault(k, {})[r["arm"]] = r
    print(f"  {'cell':<40}{'d spliced':>11}{'d re-exec':>11}{'d cutoff':>10}"
          f"{'d tokens':>10}{'d R':>8}{'d work kept':>13}")
    agg = {"spliced": [], "re": [], "cut": [], "tok": [], "R": [], "wk": []}
    for k in sorted(keys):
        f, t = keys[k].get("free"), keys[k].get("typed")
        if not f or not t:
            continue
        d = (t["spliced"] - f["spliced"], t["reexecuted"] - f["reexecuted"],
             t["cutoff_kept"] - f["cutoff_kept"],
             t["irec_total"] - f["irec_total"], t["R"] - f["R"],
             t["work_preserved_num"] - f["work_preserved_num"])
        for name, val in zip(agg, d):
            agg[name].append(val)
        label = f"{k[0]}/{k[1]}/{k[2]}/{k[3]}"
        if any(x != 0 for x in d):
            print(f"  {label:<40}{d[0]:>+11}{d[1]:>+11}{d[2]:>+10}"
                  f"{d[3]:>+10}{d[4]:>+8.3f}{d[5]:>+13}")
    print(f"  {'cells where anything differed':<40}"
          f"{sum(1 for i in range(len(agg['tok'])) if any(agg[k][i] for k in agg)):>11}"
          f" of {len(agg['tok'])}")
    print(f"  {'totals':<40}{sum(agg['spliced']):>+11}{sum(agg['re']):>+11}"
          f"{sum(agg['cut']):>+10}{sum(agg['tok']):>+10}"
          f"{st.fmean(agg['R']):>+8.3f}{sum(agg['wk']):>+13}")

    print("\n" + "=" * 104)
    print("BY CONTAMINATION EXTENT (the scripted chain has no small/medium/large "
          "regimes; banded by measured fraction)")
    print("=" * 104)
    print(f"  {'band':<17}{'arm':<7}{'cells':>6}{'spliced':>9}{'re-exec':>9}"
          f"{'cutoff':>8}{'IREC tok':>10}{'R mean':>9}{'unsafe':>9}")
    for b in ("small (<20%)", "medium (20-40%)", "large (>=40%)"):
        for name in ("free", "typed"):
            g = [r for r in arm(name) if band(r["contamination_fraction"]) == b]
            if not g:
                continue
            print(f"  {b:<17}{name:<7}{len(g):>6}"
                  f"{sum(r['spliced'] for r in g):>9}"
                  f"{sum(r['reexecuted'] for r in g):>9}"
                  f"{sum(r['cutoff_kept'] for r in g):>8}"
                  f"{sum(r['irec_total'] for r in g):>10}"
                  f"{st.fmean(r['R'] for r in g):>9.3f}"
                  f"{sum(r['unsafe_num'] for r in g):>5}/"
                  f"{sum(r['unsafe_den'] for r in g):<3}")

    div = [r for r in rows if r.get("divergence")]
    print(f"\n  control-flow divergences: {len(div)}")
    for r in div:
        print(f"    {r['arm']} {r['scenario']}/{r['variant']}/{r['workflow']}: "
              f"{r['divergence']}")


if __name__ == "__main__":
    main()
