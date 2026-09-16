"""The break-even measurement, computed from a campaign rather than projected.

    CausalLine wins  <=>  A + f.N < N  <=>  A/N + f < 1

`docs/local_llm_frontier/03` §3 *projected* that deferring the self-report takes
`A/N + f` from 1.58 to 0.92. This computes it from runs that were actually made,
in both arms, and says whether the projection held.

EVERY NUMBER HERE IS THE DELIVERED ONE
---------------------------------------
D-086: "work preserved" is two different quantities. What a method *identifies*
as preservable is not what its executed plan *delivered* -- on the chain
workflow CausalLine identified 61% and delivered 0%, because verification
refused to certify and the planner escalated to a full restart on 17 of 17
landed runs. `f` here is therefore `1 - delivered`, and the escalation count is
printed beside it, so the two can never be confused again.

    python -m src.eval.fanout_report
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import statistics as st
from collections import defaultdict
from pathlib import Path
from typing import Any

METHODS = ("B0 full restart", "B1 agent taint", "B2 topology closure", "CausalLine")
DEFAULT = Path("data/results/fanout/fanout-campaign.json")


def _rows(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for row in run.get("rows", []):
        data = row if isinstance(row, dict) else dataclasses.asdict(row)
        out[data["method"]] = data
    return out


def _ci(values: list[float]) -> tuple[float, float | None]:
    if not values:
        return float("nan"), None
    mean = st.mean(values)
    if len(values) < 3:
        # An interval over two points is decoration, not a measurement.
        return mean, None
    half = 1.96 * st.stdev(values) / math.sqrt(len(values))
    return mean, half


def _fmt(mean: float, half: float | None, pct: bool = False) -> str:
    if mean != mean:
        return "n/a"
    body = f"{mean:.1%}" if pct else f"{mean:.2f}"
    return body if half is None else f"{body} +/-{half:.2f}"


def render(payload: dict[str, Any]) -> str:
    runs = payload["results"]
    lines: list[str] = []
    lines.append(f"FAN-OUT FRONTIER -- localized contamination, {len(runs)} run(s)")
    lines.append(f"placement: {payload.get('placement', 'unknown')}")
    lines.append("")

    # --- 1. coverage ------------------------------------------------------
    lines.append("1. COVERAGE")
    lines.append(f"  {'design':<12}{'K':>4}{'arm':>8}{'n':>4}{'landed':>8}"
                 f"{'task ok':>9}{'events':>8}")
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for run in runs:
        groups[(run["test_id"], run["arm"])].append(run)
    for (test_id, arm), group in sorted(groups.items()):
        landed = sum(1 for r in group if (r.get("truth") or {}).get("payload_landed"))
        ok = sum(1 for r in group if r.get("task_success"))
        lines.append(f"  {test_id:<12}{group[0]['workers']:>4}{arm:>8}"
                     f"{len(group):>4}{landed:>8}{ok:>9}"
                     f"{st.mean(r['events'] for r in group):>8.0f}")

    # The control must never land, or the canary is not measuring influence.
    controls = [r for r in runs if "exp" in r["test_id"]]
    if controls:
        landed = sum(1 for r in controls
                     if (r.get("truth") or {}).get("payload_landed"))
        lines.append("")
        lines.append(f"  CONTROL: {landed} of {len(controls)} exposed-only run(s) "
                     f"landed" + ("  <-- THE CANARY IS NOT CLEAN" if landed else
                                  "  (as required)"))

    # --- 2. delivered recovery, per method --------------------------------
    landed_runs = [r for r in runs
                   if (r.get("truth") or {}).get("payload_landed")]
    lines += ["", "2. DELIVERED RECOVERY (landed runs only)",
              "   `preserved` is what the EXECUTED plan kept, after any "
              "escalation (D-086)."]
    if landed_runs:
        lines.append(f"  {'arm':<8}{'method':<22}{'preserved':>14}{'esc':>8}"
                     f"{'blast':>8}{'tokens':>10}{'unsafe':>8}")
        for arm in sorted({r["arm"] for r in landed_runs}):
            arm_runs = [r for r in landed_runs if r["arm"] == arm]
            for method in METHODS:
                vals, esc, blast, toks, unsafe = [], 0, [], [], 0
                for run in arm_runs:
                    row = _rows(run).get(method)
                    if not row:
                        continue
                    vals.append(row["work_preserved"])
                    esc += 1 if row.get("escalations") else 0
                    blast.append(float(row.get("blast_radius_events") or 0))
                    toks.append(float(row.get("recovery_tokens") or 0))
                    unsafe += row.get("unsafe_preservations") or 0
                if not vals:
                    continue
                mean, half = _ci(vals)
                lines.append(
                    f"  {arm:<8}{method:<22}{_fmt(mean, half, pct=True):>14}"
                    f"{esc:>4}/{len(vals):<3}{st.mean(blast):>8.1f}"
                    f"{st.mean(toks):>10.0f}{unsafe:>8}")

    # --- 3. the break-even number -----------------------------------------
    lines += ["", "3. BREAK-EVEN:  A/N + f  < 1.00 is the win condition",
              "   A = analysis tokens. N = B0's full-restart tokens.",
              "   f = 1 - delivered work preserved, by CausalLine."]
    lines.append(f"  {'arm':<8}{'K':>4}{'n':>4}{'A':>9}{'N':>9}{'A/N':>8}"
                 f"{'f':>7}{'A/N+f':>9}   verdict")
    by_arm_k: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for run in landed_runs:
        by_arm_k[(run["arm"], run["workers"])].append(run)
    summary: dict[str, list[float]] = defaultdict(list)
    for (arm, workers), group in sorted(by_arm_k.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        a_vals, n_vals, f_vals = [], [], []
        for run in group:
            rows = _rows(run)
            mine, base = rows.get("CausalLine"), rows.get("B0 full restart")
            if not mine or not base or not base.get("recovery_tokens"):
                continue
            a_vals.append(float(run["analysis_tokens"]))
            n_vals.append(float(base["recovery_tokens"]))
            f_vals.append(1.0 - float(mine["work_preserved"]))
        if not a_vals:
            continue
        A, N, f = st.mean(a_vals), st.mean(n_vals), st.mean(f_vals)
        total = A / N + f
        summary[arm].append(total)
        verdict = "WINS" if total < 1.0 else "loses"
        lines.append(f"  {arm:<8}{workers:>4}{len(a_vals):>4}{A:>9.0f}{N:>9.0f}"
                     f"{A / N:>8.2f}{f:>7.2f}{total:>9.2f}   {verdict}")

    # --- 4. did the projection hold? --------------------------------------
    lines += ["", "4. THE PROJECTION, CHECKED"]
    if "lazy" in summary and "eager" in summary:
        best_lazy = min(summary["lazy"])
        best_eager = min(summary["eager"])
        lines.append(f"  best A/N+f, eager arm : {best_eager:.2f}")
        lines.append(f"  best A/N+f, lazy arm  : {best_lazy:.2f}")
        lines.append(f"  projected (docs/local_llm_frontier/03 §3): 0.92")
        delta = best_lazy - 0.92
        lines.append(f"  difference from the projection: {delta:+.2f}")
        if best_lazy < 1.0:
            lines.append("  -> the win condition IS met, on measured runs, for "
                         "the first time in this project.")
        else:
            lines.append("  -> the win condition is NOT met. The projection did "
                         "not survive being run.")
    else:
        lines.append("  both arms are needed for this comparison; only one is "
                     "present.")

    # --- 5. paired sign test ----------------------------------------------
    lines += ["", "5. PAIRED SIGN TEST on delivered work preserved",
              "   (same run, so pair it; exact and two-sided)"]
    for baseline in ("B0 full restart", "B1 agent taint", "B2 topology closure"):
        wins = losses = ties = 0
        deltas = []
        for run in landed_runs:
            rows = _rows(run)
            mine, theirs = rows.get("CausalLine"), rows.get(baseline)
            if not mine or not theirs:
                continue
            a, b = mine["work_preserved"], theirs["work_preserved"]
            deltas.append(a - b)
            wins, losses, ties = (
                (wins + 1, losses, ties) if a > b else
                (wins, losses + 1, ties) if a < b else (wins, losses, ties + 1)
            )
        n = wins + losses
        if n:
            k = max(wins, losses)
            p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n)
        else:
            p = 1.0
        mark = "significant" if p < 0.05 else "NOT significant -- underpowered"
        lines.append(f"  CausalLine vs {baseline}: {wins}W-{losses}L-{ties}T, "
                     f"mean delta {st.mean(deltas) if deltas else 0:+.1%}, "
                     f"p={p:.5f} ({mark})")

    # --- 6. safety ---------------------------------------------------------
    total_unsafe = {
        m: sum((_rows(r).get(m) or {}).get("unsafe_preservations") or 0
               for r in landed_runs)
        for m in METHODS
    }
    lines += ["", "6. SAFETY"]
    if not landed_runs:
        lines.append("  no run landed, so nothing here tests safety and every "
                     "zero is arithmetic.")
    elif all(v == 0 for v in total_unsafe.values()):
        lines.append("  SAFETY WAS TIED -- every method had zero unsafe "
                     "preservations.")
        lines.append("  None is shown safer than another here. The "
                     "distinguishing results are")
        lines.append("  DELIVERED WORK PRESERVATION and COST.")
    else:
        lines.append("  unsafe preservations: " +
                     ", ".join(f"{m}={v}" for m, v in total_unsafe.items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=DEFAULT)
    args = parser.parse_args(argv)
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    print(render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
