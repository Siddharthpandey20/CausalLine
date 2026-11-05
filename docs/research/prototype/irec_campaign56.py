"""The consolidated 56-agent validation: B0, B2, CausalLine and IREC.

PAIRING IS BY CONSTRUCTION, NOT BY SEED MATCHING
-------------------------------------------------
All four methods are scored on the SAME 15 saved original traces. The recorded
B0 / B1 / B2 / CausalLine rows in `data/results/mixed56-workload-varied` were
produced inside the same run as each other, against that run's trace, with that
run's detector verdict and `_tools_for` fixtures. IREC is replayed against the
identical trace with the identical flagged set. So the workload, the
contamination placement and the seed are not merely matched -- they are the same
objects.

Re-running the originals would be WORSE for pairing, not better: a 56-agent
real-model run is non-deterministic, so fresh originals would put B0/B2/
CausalLine on different traces from the ones IREC was scored on. It would also
cost about 945,000 tokens against this design's 69,000, and roughly 170 Gemini
calls against a 20-per-day free tier.

WHAT IS RUN FRESH HERE
----------------------
IREC through the INTEGRATED module, `src/recovery/irec.py`, rather than the
prototype client used for the first 15-placement campaign. Those are two
separate implementations of the same two rules, and the integration path had not
been exercised end to end. Everything else is read from the recorded rows.

Accounting (`docs/research/IREC_COST_ACCOUNTING.md`):
  * defect A does NOT affect this path -- `src/eval/real_llm.py` already passes
    `tools=_tools_for(...)` to the baselines and to CausalLine. It was
    `experiment.py:run_cell`, the scripted path, only.
  * defect B DOES affect it -- CausalLine's recorded `replay_tokens` is its
    final attempt alone. The earlier attempts are recovered from the escalation
    traces the harness kept, and charged.
  * IREC's fallback term is additive and is never discounted. Nothing has
    needed it, so it appears as zero rather than being omitted.

The typed interface is NOT used: `handoff_projection` stays at its default of
`None`, which is the shipped free-text behaviour.

    python -m docs.research.prototype.irec_campaign56            # all 15
    python -m docs.research.prototype.irec_campaign56 --limit 2   # smoke

No key is ever printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.eval.baselines import b2_topology_closure, discarded_events
from src.eval.detectors import build as build_detector
from src.eval.mixed_campaign import build_ledgers, build_router
from src.eval.mixed_scenarios import MixedScenario
from src.eval.real_llm import _tools_for, ground_truth
from src.recovery.irec import recover_irec
from src.tracing.logger import read_trace
from src.tracing.mixed import Topology

PRIMARY = Path("data/results/mixed56-workload-varied")
OUT = Path("data/runs/irec-campaign56")


def saved_runs() -> list[dict]:
    out = []
    for path in sorted(PRIMARY.glob("seed*.json")):
        for run in json.loads(path.read_text(encoding="utf-8")).get("results", []):
            run["_file"] = path.name
            out.append(run)
    return out


def causalline_true_replay(run: dict) -> tuple[int, list[tuple[str, int]]]:
    """CausalLine's replay charged over EVERY attempt (accounting defect B).

    Attempt n is written to `-CausalLine`, then `-esc1`, then `-esc2`; the
    recorded figure matches the last of them.
    """
    row = next(r for r in run["rows"] if r["method"] == "CausalLine")
    stem = str(Path(run["trace_path"]).with_suffix(""))
    attempts: list[tuple[str, int]] = []
    total = 0
    for label, suffix in (("attempt 1", ""), ("attempt 2", "-esc1"),
                          ("attempt 3", "-esc2")):
        path = Path(stem + f"-CausalLine{suffix}.jsonl")
        if path.exists():
            spent = read_trace(path).pipeline_tokens()
            attempts.append((label, spent))
            total += spent
    if not attempts:
        total = row.get("replay_tokens", 0)
        attempts = [("attempt 1", total)]
    return total, attempts


def run_one(run: dict) -> dict:
    scenario = MixedScenario.build(
        run["regime"], topology=Topology(),
        seed=run["seed"] if run.get("vary_placement") else None)
    for key in ("test_id", "task", "payload", "marker", "token"):
        got = getattr(scenario, key, None)
        want = (run.get("scenario") or {}).get(key)
        if want and want != got:
            raise SystemExit(f"scenario rebuild mismatch on {key!r}")

    original = read_trace(Path(run["trace_path"]))
    original.validate()
    flagged_true = {s.id for s in original.sources if s.malicious}
    verdict = build_detector("oracle").flag(original)
    flagged = set(verdict.sources()) or flagged_true
    token = (run.get("scenario") or {}).get("token", "")
    truth_events, truth = ground_truth(original, sorted(flagged_true), token)
    closure = set(b2_topology_closure(original, sorted(flagged_true)))

    ledgers = build_ledgers()
    router = build_router(Topology(), ledgers)
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"irec-{run['seed']}-{run['regime']}.jsonl"

    started = time.time()
    failure = None
    result = None
    try:
        result = recover_irec(
            original, flagged, router, out,
            task=scenario.task,
            tools=_tools_for(scenario, out),
            handoff_hook=scenario.handoff_hook(),
            handoff_projection=None,          # shipped free-text behaviour
            workflow_kwargs=scenario.workflow_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 -- a dead run must not kill the campaign
        failure = f"{type(exc).__name__}: {exc}"

    record: dict[str, Any] = {
        "seed": run["seed"], "regime": run["regime"], "file": run["_file"],
        "events": len(original.events),
        "payload_landed": truth.payload_landed,
        "truth_events": len(truth_events),
        "b2_closure": len(closure),
        "flagged": sorted(flagged),
        "wall_clock_s": round(time.time() - started, 1),
        "failure": failure,
        "routing": dict(router.per_provider_calls),
        "degraded": list(router.degraded),
        "ledgers": {k: v.to_dict() for k, v in ledgers.items()},
        "implementation": "src/recovery/irec.py (integrated)",
    }

    if result is not None:
        # WHICH SET "DISCARDED" MEANS, AND WHY NOT `discarded_events` HERE
        # ------------------------------------------------------------------
        # `discarded_events(report, ...)` returns the invalidation set handed to
        # `replay()`, which is the right answer for every method that HAS one:
        # for B0/B1/B2 it is the full event-id set the method chose, non-model
        # events included. IREC's invalidation set is its re-executed MODEL
        # events only, so using it here would compare IREC's 53 model events
        # against B2's 137-event closure and call the difference "preserved
        # work". That is precisely the defect `discarded_events` was written to
        # prevent, arriving from the other direction: scoring one method on
        # model calls and the others on their full sets.
        #
        # Measured consequence of getting it wrong, on 20260917/large: 48
        # unsafe preservations and 47/95 recall, against 0 and 95/95 under the
        # definition below.
        #
        # So IREC is scored on what it did not preserve, read off the recovered
        # bytes: every model output it replaced, every event whose recomputed
        # bytes differ from the original (the messages, tool calls and memory
        # operations the pipeline rebuilt from a changed upstream), and -- issue
        # #20 -- every event whose stored output still holds flagged content.
        # Events the cutoff kept are PRESERVED, which is the mechanism's whole
        # point: their bytes are identical.
        recovered = read_trace(Path(result.recovered_path))
        payloads = [s.content for s in original.sources if s.id in flagged_true]
        discarded: set[str] = set()
        for event in original.events:
            text = original.output_text(event.id) or ""
            if recovered.has_event(event.id):
                if text != (recovered.output_text(event.id) or ""):
                    discarded.add(event.id)
            else:
                discarded.add(event.id)
            if any(p and len(p) >= 12 and p in text for p in payloads):
                discarded.add(event.id)
        kept = {e.id for e in original.events} - discarded
        survived = sorted(
            eid for eid in kept
            if any(p and len(p) >= 12 and p in (original.output_text(eid) or "")
                   for p in payloads))
        record.update({
            "spliced": result.spliced,
            "reexecuted": result.calls,
            "cutoff_kept": result.unchanged,
            "changed": result.changed,
            "irec_replay_tokens": result.replay_tokens,
            "irec_analysis_tokens": result.analysis_tokens,
            "irec_total_tokens": result.recovery_tokens,
            "irec_fallback": result.fallback,
            "irec_fallback_tokens": result.fallback_tokens,
            "discarded_num": len(discarded),
            "work_preserved_num": len(original.events) - len(discarded),
            "work_preserved_den": len(original.events),
            "unsafe_num": len(truth_events - discarded),
            "unsafe_den": len(truth_events),
            "recall_num": len(truth_events & discarded),
            "recall_den": len(truth_events),
            "escapes_num": len(truth_events - closure),
            "escapes_den": len(truth_events),
            "survived_bytes": survived,
            "task_success": result.task_success,
            "verify_ok": result.verification.ok,
            "verify_reasons": list(result.verification.reasons),
            "divergence": result.divergence,
            "notes": result.notes,
        })

    # --- the other three methods, from the recorded rows -------------------
    by = {r["method"]: r for r in run["rows"]}
    cl_true, cl_attempts = causalline_true_replay(run)
    cl = by["CausalLine"]
    record["methods"] = {
        "B0": {"replay": by["B0 full restart"]["replay_tokens"],
               "analysis": 0,
               "total": by["B0 full restart"]["replay_tokens"],
               "work_preserved": by["B0 full restart"]["work_preserved"],
               "unsafe": by["B0 full restart"]["unsafe_preservations"],
               "task": by["B0 full restart"].get("task_success")},
        "B1": {"replay": by["B1 agent taint"]["replay_tokens"],
               "analysis": 0,
               "total": by["B1 agent taint"]["replay_tokens"],
               "work_preserved": by["B1 agent taint"]["work_preserved"],
               "unsafe": by["B1 agent taint"]["unsafe_preservations"],
               "task": by["B1 agent taint"].get("task_success")},
        "B2": {"replay": by["B2 topology closure"]["replay_tokens"],
               "analysis": 0,
               "total": by["B2 topology closure"]["replay_tokens"],
               "work_preserved": by["B2 topology closure"]["work_preserved"],
               "unsafe": by["B2 topology closure"]["unsafe_preservations"],
               "task": by["B2 topology closure"].get("task_success")},
        "CausalLine": {"replay_recorded": cl.get("replay_tokens", 0),
                       "replay_corrected": cl_true,
                       "attempts": cl_attempts,
                       "analysis": cl.get("analysis_tokens", 0),
                       "total": cl_true + cl.get("analysis_tokens", 0),
                       "work_preserved": cl["work_preserved"],
                       "unsafe": cl["unsafe_preservations"],
                       "task": cl.get("task_success"),
                       "delivered_scope": next(
                           (n.split("scope=", 1)[1].strip()
                            for n in run.get("notes", [])
                            if n.startswith("succeeded at scope=")), "exhausted"),
                       "verification_failures": len(
                           [n for n in run.get("notes", [])
                            if n.startswith("verify failed")])},
    }
    record["original_task_success"] = run.get("task_success")
    return record


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT / "campaign.json")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    records = (json.loads(args.out.read_text(encoding="utf-8"))
               if args.out.exists() else [])
    done = {(r["seed"], r["regime"]) for r in records}
    targets = [r for r in saved_runs() if (r["seed"], r["regime"]) not in done]
    if args.limit:
        targets = targets[:args.limit]
    print(f"consolidated 56-agent validation: {len(targets)} placements to run, "
          f"{len(done)} already done", flush=True)

    for run in targets:
        rec = run_one(run)
        records.append(rec)
        args.out.write_text(json.dumps(records, indent=1), encoding="utf-8")
        if rec.get("failure"):
            print(f"-- {rec['seed']} {rec['regime']}: FAILURE {rec['failure']}",
                  flush=True)
            continue
        print(f"-- {rec['seed']} {rec['regime']}: spliced {rec['spliced']} "
              f"re-exec {rec['reexecuted']} cutoff {rec['cutoff_kept']} "
              f"tok {rec['irec_total_tokens']} "
              f"unsafe {rec['unsafe_num']}/{rec['unsafe_den']} "
              f"recall {rec['recall_num']}/{rec['recall_den']} "
              f"task {rec['task_success']} verify {rec['verify_ok']} "
              f"{rec['wall_clock_s']:.0f}s"
              + (f"  DEGRADED {rec['degraded']}" if rec.get("degraded") else ""),
              flush=True)
    print(f"\nwritten to {args.out}", flush=True)


if __name__ == "__main__":
    main()
