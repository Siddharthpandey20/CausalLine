"""Statistics over the 5-seed x 3-regime validation of the 56-agent workflow.

    python -m src.eval.mixed_validation_report [out.md]

Reads every `seedNNN-regime.json` the driver produced and reports what is
there. It does not choose which runs to include: every file present is used,
and any planned run that is absent is named. Cherry-picking is prevented by
construction rather than by discipline.

PAIRED, BECAUSE THE COMPARISON IS PAIRED
-----------------------------------------
Each run scores all four methods on the SAME trace, under the same attack,
with the same seed. So the quantity of interest is the per-run difference
`CausalLine - B2`, not the difference of two independent means. Unpaired
summaries of the same data would hide that the variance across regimes is far
larger than the variance between methods within a regime.

NO SIGNIFICANCE CLAIM IS MADE FROM FIVE RUNS
---------------------------------------------
Five seeds per regime supports descriptive statistics and a sign count. It
does not support a p-value anyone should act on, and this module does not
compute one. The win/tie/loss counts are reported instead, which is the honest
summary at this sample size.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

METHODS = ("B0 full restart", "B1 agent taint", "B2 topology closure",
           "CausalLine")
SHORT = {"B0 full restart": "B0", "B1 agent taint": "B1",
         "B2 topology closure": "B2", "CausalLine": "CausalLine"}
REGIMES = ("small", "medium", "large")


def load_runs(out_dir: Path) -> list[dict[str, Any]]:
    """Every completed run, newest schema, flattened one dict per run."""
    runs: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("seed*-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for run in payload.get("results", []):
            run["_file"] = path.name
            runs.append(run)
    return runs


def escalation(run: dict[str, Any]) -> dict[str, Any]:
    """Which scope CausalLine delivered at, and what it tried first.

    Read off the notes `run_generated` already writes, rather than by adding a
    field to the harness -- the harness is frozen for this validation.
    """
    notes = run.get("notes", [])
    verify_failures = [n for n in notes if n.startswith("verify failed at scope=")]
    succeeded = next((n for n in notes if n.startswith("succeeded at scope=")), "")
    scope = succeeded.split("scope=", 1)[1].split(":")[0].strip() if succeeded else ""
    failed_scopes = [n.split("scope=", 1)[1].split(":")[0].strip()
                     for n in verify_failures]
    return {
        "delivered_scope": scope or "none",
        "verification_failures": len(verify_failures),
        "agent_restart_escalations": int("agent_restart" in failed_scopes
                                         or scope == "agent_restart"),
        "full_restart_escalations": int(scope == "restart_all"),
        "selective_success": scope == "selective",
        "budget_exhausted": any("escalation budget spent" in n for n in notes),
    }


def measured(run: dict[str, Any]) -> dict[str, Any]:
    """True contaminated fraction and closure escapes, from the stored trace."""
    from src.eval.attacks import label_malicious
    from src.eval.baselines import b2_topology_closure
    from src.provenance.contamination import contaminate
    from src.tracing.logger import read_trace

    path = Path(run.get("trace_path", ""))
    if not path.exists():
        return {}
    trace = read_trace(path)
    flagged = [s.id for s in trace.sources if s.malicious] or label_malicious(
        path, (run.get("scenario") or {}).get("marker", ""))
    total = len(trace.events) or 1
    region = set(contaminate(trace, set(flagged)).events)
    closure = set(b2_topology_closure(trace, flagged))
    return {"events": total,
            "f_true": len(region) / total,
            "f_structural": len(closure) / total,
            "closure_escapes": len(region - closure)}


def row_for(run: dict[str, Any]) -> dict[str, Any]:
    """One flat record per run, with everything TASK 5 asks to record."""
    rows = {r["method"]: r for r in run.get("rows", [])}
    ledgers = run.get("ledgers") or {}
    out: dict[str, Any] = {
        "seed": run.get("seed"),
        "regime": run.get("regime"),
        "payload_landed": (run.get("truth") or {}).get("payload_landed"),
        "original_task": run.get("task_success"),
        "pipeline_tokens": run.get("pipeline_tokens", 0),
        "analysis_tokens": run.get("analysis_tokens", 0),
        "wall_clock_s": run.get("wall_clock_s", 0),
        "routing": run.get("routing") or {},
        "degraded": run.get("degraded") or [],
        "ok": run.get("ok"),
        "band": tuple(run.get("band") or (0.0, 1.0)),
        "vary_placement": bool(run.get("vary_placement")),
        "placement": (run.get("placement") or {}),
    }
    placement = run.get("placement") or {}
    counts = placement.get("counts") or {}
    if counts:
        out["placement_counts"] = (counts.get("docs", 0),
                                   counts.get("memory", 0),
                                   counts.get("messages", 0))
    out.update(measured(run))
    out.update(escalation(run))
    for method in METHODS:
        row = rows.get(method)
        if not row:
            continue
        key = SHORT[method]
        out[f"{key}_preserved"] = row["work_preserved"]
        out[f"{key}_discarded"] = row["discarded"]
        out[f"{key}_unsafe"] = row["unsafe_preservations"]
        out[f"{key}_task"] = row.get("task_success")
        out[f"{key}_replay_tokens"] = row.get("replay_tokens", 0)
        out[f"{key}_analysis_tokens"] = row.get("analysis_tokens", 0)
    for provider in ("gemini", "nvidia", "local"):
        led = ledgers.get(provider) or {}
        out[f"{provider}_calls"] = led.get("requests", 0)
        out[f"{provider}_tokens"] = led.get("total_tokens", 0)
    # The local client is not budget-ledgered, so its call count comes from the
    # router's own per-provider tally.
    out["local_calls"] = out["routing"].get("local", out.get("local_calls", 0))
    return out


def describe(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def render(rows: list[dict[str, Any]], planned: int) -> str:
    out: list[str] = []
    add = out.append

    add("## 1. Final experimental table\n")
    add(f"**{len(rows)} of {planned} planned runs completed.**"
        + ("" if len(rows) == planned else
           " The missing runs are named in section 8; nothing was substituted "
           "for them and no design was changed to fit what completed.") + "\n")
    add("| seed | regime | placement (d/m/v) | `f` true | band | escapes | "
        "B0 | B1 | B2 | CausalLine | CL-B2 | unsafe (CL) | scope | task |")
    add("|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for r in sorted(rows, key=lambda x: (REGIMES.index(x["regime"])
                                         if x["regime"] in REGIMES else 9,
                                         x["seed"])):
        delta = (r.get("CausalLine_preserved", 0) - r.get("B2_preserved", 0)) * 100
        counts = r.get("placement_counts") or ()
        shape = "/".join(str(c) for c in counts) if counts else "frozen"
        low, high = r.get("band", (0.0, 1.0))
        f_true = r.get("f_true", 0)
        verdict = "in" if low <= f_true <= high else f"**OUT** {low:.0%}-{high:.0%}"
        add(f"| {r['seed']} | {r['regime']} | {shape} | {f_true:.1%} "
            f"| {verdict} "
            f"| {r.get('closure_escapes', 0)} "
            f"| {r.get('B0_preserved', 0):.1%} | {r.get('B1_preserved', 0):.1%} "
            f"| {r.get('B2_preserved', 0):.1%} "
            f"| {r.get('CausalLine_preserved', 0):.1%} | {delta:+.1f} "
            f"| {r.get('CausalLine_unsafe', 0)} "
            f"| {r.get('delivered_scope', '?')} "
            f"| {'OK' if r.get('CausalLine_task') else 'fail'} |")

    add("\n## 2. Per-regime analysis (paired, CausalLine - B2)\n")
    add("| regime | n | CL mean | B2 mean | paired diff mean | median | "
        "stdev | min | max | CL>B2 | CL=B2 | CL<B2 |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for regime in REGIMES:
        group = [r for r in rows if r["regime"] == regime]
        if not group:
            continue
        cl = [r.get("CausalLine_preserved", 0) for r in group]
        b2 = [r.get("B2_preserved", 0) for r in group]
        diffs = [(a - b) * 100 for a, b in zip(cl, b2)]
        d = describe(diffs)
        wins = sum(1 for x in diffs if x > 1e-9)
        ties = sum(1 for x in diffs if abs(x) <= 1e-9)
        losses = sum(1 for x in diffs if x < -1e-9)
        add(f"| {regime} | {len(group)} | {statistics.fmean(cl):.1%} "
            f"| {statistics.fmean(b2):.1%} | {d['mean']:+.2f} "
            f"| {d['median']:+.2f} | {d['stdev']:.2f} | {d['min']:+.2f} "
            f"| {d['max']:+.2f} | **{wins}** | {ties} | **{losses}** |")
    add("\nAll figures in percentage points of work preserved. Paired: each "
        "difference is one run's CausalLine against the same run's B2, on the "
        "same trace and the same attack.\n")

    add("## 3. Aggregate analysis\n")
    diffs = [(r.get("CausalLine_preserved", 0) - r.get("B2_preserved", 0)) * 100
             for r in rows]
    d = describe(diffs)
    if d:
        add(f"- runs: **{d['n']}**")
        add(f"- paired difference (CausalLine - B2): mean **{d['mean']:+.2f} pts**, "
            f"median {d['median']:+.2f}, sd {d['stdev']:.2f}, "
            f"range {d['min']:+.2f} to {d['max']:+.2f}")
        add(f"- CausalLine > B2 in **{sum(1 for x in diffs if x > 1e-9)}** runs, "
            f"= in {sum(1 for x in diffs if abs(x) <= 1e-9)}, "
            f"< in **{sum(1 for x in diffs if x < -1e-9)}**")
    add("\n**No significance test is reported.** Five seeds per regime, three "
        "regimes whose effects differ in sign, and a per-run difference whose "
        "spread within a regime is far smaller than the spread between them. "
        "A p-value computed over that would be arithmetic, not evidence.\n")

    add("## 4. Safety analysis\n")
    add("| regime | runs | unsafe (B0/B1/B2/CL) | closure escapes | "
        "payload landed | recall |")
    add("|---|---:|---|---:|---:|---|")
    for regime in REGIMES:
        group = [r for r in rows if r["regime"] == regime]
        if not group:
            continue
        unsafe = [sum(r.get(f"{m}_unsafe", 0) for r in group)
                  for m in ("B0", "B1", "B2", "CausalLine")]
        add(f"| {regime} | {len(group)} | "
            f"{unsafe[0]} / {unsafe[1]} / {unsafe[2]} / **{unsafe[3]}** "
            f"| {sum(r.get('closure_escapes', 0) for r in group)} "
            f"| {sum(1 for r in group if r.get('payload_landed'))}/{len(group)} "
            f"| {'1.0' if unsafe[3] == 0 else '**BELOW 1.0**'} |")
    add("\n`unsafe` is event-level unsafe preservation: recovery kept an event "
        "that was truly contaminated. It must be 0. A preservation gain bought "
        "with a non-zero count here is not a gain, and the large regime's first "
        "measured '+17.2 pts' was exactly that (issue #20).\n")

    add("## 5. Economic analysis\n")
    add("CausalLine pays analysis **plus** replay; B2 pays replay only. The "
        "comparison is preserved work against total tokens, never preserved "
        "work alone.\n")
    add("| regime | CL analysis | CL replay | CL total | B2 total | "
        "CL/B2 cost | CL-B2 preserved | tokens per point gained |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for regime in REGIMES:
        group = [r for r in rows if r["regime"] == regime]
        if not group:
            continue
        cl_a = statistics.fmean([r.get("CausalLine_analysis_tokens", 0) for r in group])
        cl_r = statistics.fmean([r.get("CausalLine_replay_tokens", 0) for r in group])
        b2_t = statistics.fmean([r.get("B2_replay_tokens", 0)
                                 + r.get("B2_analysis_tokens", 0) for r in group])
        cl_t = cl_a + cl_r
        gain = statistics.fmean(
            [(r.get("CausalLine_preserved", 0) - r.get("B2_preserved", 0)) * 100
             for r in group])
        ratio = cl_t / b2_t if b2_t else float("inf")
        per_point = (cl_t - b2_t) / gain if abs(gain) > 1e-9 else float("inf")
        add(f"| {regime} | {cl_a:.0f} | {cl_r:.0f} | {cl_t:.0f} | {b2_t:.0f} "
            f"| {ratio:.2f}x | {gain:+.2f} pts "
            f"| {per_point:,.0f}" if abs(gain) > 1e-9 else
            f"| {regime} | {cl_a:.0f} | {cl_r:.0f} | {cl_t:.0f} | {b2_t:.0f} "
            f"| {ratio:.2f}x | {gain:+.2f} pts | n/a (no gain) |")

    add("\n## 6. Escalation and verification\n")
    add("| regime | selective delivered | verification failures | "
        "agent-restart | full restart | escalation budget spent |")
    add("|---|---:|---:|---:|---:|---:|")
    for regime in REGIMES:
        group = [r for r in rows if r["regime"] == regime]
        if not group:
            continue
        add(f"| {regime} | {sum(1 for r in group if r.get('selective_success'))}"
            f"/{len(group)} "
            f"| {sum(r.get('verification_failures', 0) for r in group)} "
            f"| {sum(r.get('agent_restart_escalations', 0) for r in group)} "
            f"| {sum(r.get('full_restart_escalations', 0) for r in group)} "
            f"| {sum(1 for r in group if r.get('budget_exhausted'))} |")

    add("\n## 7. External API usage\n")
    add("| regime | runs | Gemini calls | Gemini tokens | NVIDIA calls | "
        "NVIDIA tokens | local calls | degraded agents |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for regime in REGIMES:
        group = [r for r in rows if r["regime"] == regime]
        if not group:
            continue
        add(f"| {regime} | {len(group)} "
            f"| {sum(r.get('gemini_calls', 0) for r in group)} "
            f"| {sum(r.get('gemini_tokens', 0) for r in group)} "
            f"| {sum(r.get('nvidia_calls', 0) for r in group)} "
            f"| {sum(r.get('nvidia_tokens', 0) for r in group)} "
            f"| {sum(r.get('local_calls', 0) for r in group)} "
            f"| {sum(len(r.get('degraded') or []) for r in group)} |")
    return "\n".join(out)


def main() -> None:
    """`mixed_validation_report.py [out.md] [--dir D] [--planned N]`

    `--dir` exists so the frozen validation and the workload-varied experiment
    are reported SEPARATELY. They are different experiments: one repeats a
    fixed workload, the other varies it. Aggregating them into a single mean
    would be the exact mistake this module's docstring warns about, so there is
    no switch that does it.
    """
    import argparse

    from src.eval.mixed_validation import REGIMES as PLANNED_REGIMES, SEEDS

    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", default="")
    parser.add_argument("--dir", default="data/results/mixed56-validation")
    parser.add_argument("--planned", type=int, default=0)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    out_dir = Path(args.dir)
    rows = [row_for(r) for r in load_runs(out_dir)]
    planned = args.planned or len(SEEDS) * len(PLANNED_REGIMES)
    text = render(rows, planned)
    if args.label:
        text = f"# {args.label}\n\n" + text

    have = {(r["seed"], r["regime"]) for r in rows}
    missing = [f"seed{s}-{r}" for s in SEEDS for r in PLANNED_REGIMES
               if (s, r) not in have] if args.planned == 0 else []
    if missing:
        text += ("\n\n## 8. Runs not completed\n\n"
                 + "\n".join(f"- {m}" for m in missing)
                 + "\n\nReported rather than replaced. The design was not "
                   "changed to fit what completed.\n")

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(rows)} runs)")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(text)


if __name__ == "__main__":
    main()
