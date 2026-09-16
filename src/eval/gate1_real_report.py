"""Score the frozen Gate 1 against the real-LLaMA falsification suite.

The gate is replayed OFFLINE against traces produced with `gate1_enabled=False`,
so it never influenced the `N`, `A`, `R` it is judged on. `A_SCALE` comes from
`calibrate_from_history`, which reads only the pre-existing campaign.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from pathlib import Path
from typing import Any

from src.eval.gate1_real import OUT, calibrate_from_history
from src.recovery.gate1 import DECISION_MARGIN, decide
from src.tracing.logger import read_trace


def score(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scales = {arm: calibrate_from_history(arm) for arm in ("eager", "lazy")}
    out = []
    for row in rows:
        if not row.get("ok"):
            continue
        trace = read_trace(Path(row["trace_path"]))
        flagged = [s.id for s in trace.sources if s.malicious]
        scale, n_cal, note = scales[row["arm"]]
        g = decide(trace, flagged, a_scale=scale)

        n, a, r = row["N"], row["A"], row["R"]
        recover_cost = a + min(r, n)
        oracle = recover_cost < n
        f_true = min(1.0, r / max(1, n))
        margin = (n - recover_cost) / max(1, n)
        realized = recover_cost if g.investigate else n

        if g.investigate == oracle:
            verdict = "correct INVESTIGATE" if oracle else "correct RESTART"
        elif oracle:
            verdict = "FALSE RESTART"
        else:
            verdict = "FALSE RECOVERY"

        out.append({**row,
                    "f_true": f_true,
                    "a_over_n": a / max(1, n),
                    "true_margin": margin,
                    "f_structural": g.f_structural,
                    "a_estimate": g.a_estimate,
                    "predicted": g.total,
                    "gate": "INVESTIGATE" if g.investigate else "RESTART",
                    "oracle": "INVESTIGATE" if oracle else "RESTART",
                    "verdict": verdict,
                    "recover_cost": recover_cost,
                    "realized": realized,
                    "best": min(recover_cost, n),
                    "a_scale": scale,
                    "calibration": note,
                    "n_calibration": n_cal})
    return out


def render(scored: list[dict[str, Any]]) -> str:
    L: list[str] = []
    L.append(f"GATE 1 -- REAL LLaMA FALSIFICATION SUITE, {len(scored)} runs")
    L.append(f"margin={DECISION_MARGIN} (frozen)   "
             f"A_SCALE eager={scored[0]['a_scale'] if scored else 0:.3f}")
    L.append("")

    # --- per case -----------------------------------------------------------
    L.append("1. PER-CASE RESULTS")
    L.append(f"  {'case':<22}{'fam':>4}{'N':>6}{'A':>6}{'R':>6}{'A/N':>6}"
             f"{'f_true':>7}{'margin':>8}{'f_str':>7}{'pred':>7}"
             f"{'gate':>12}{'oracle':>12}  verdict")
    for s in scored:
        L.append(f"  {s['case_id']+'#'+str(s['repeat']):<22}{s['family']:>4}"
                 f"{s['N']:>6}{s['A']:>6}{s['R']:>6}{s['a_over_n']:>6.2f}"
                 f"{s['f_true']:>7.2f}{s['true_margin']:>+8.2f}"
                 f"{s['f_structural']:>7.2f}{s['predicted']:>7.2f}"
                 f"{s['gate']:>12}{s['oracle']:>12}  "
                 f"{'ok' if 'correct' in s['verdict'] else s['verdict']}")

    # --- confusion ----------------------------------------------------------
    cm = defaultdict(int)
    for s in scored:
        cm[(s["oracle"], s["gate"])] += 1
    L += ["", "2. CONFUSION MATRIX",
          f"  {'':<22}{'gate INVESTIGATE':>18}{'gate RESTART':>15}"]
    for o in ("INVESTIGATE", "RESTART"):
        L.append(f"  oracle {o:<15}{cm[(o,'INVESTIGATE')]:>18}"
                 f"{cm[(o,'RESTART')]:>15}")
    n = len(scored)
    correct = cm[("INVESTIGATE", "INVESTIGATE")] + cm[("RESTART", "RESTART")]
    fr = cm[("INVESTIGATE", "RESTART")]
    fc = cm[("RESTART", "INVESTIGATE")]
    L += ["", f"  accuracy {correct}/{n} = {correct/max(1,n):.1%}",
          f"  FALSE RESTART  {fr}/{n} = {fr/max(1,n):.1%}",
          f"  FALSE RECOVERY {fc}/{n} = {fc/max(1,n):.1%}"]

    # --- economics ----------------------------------------------------------
    restart = sum(s["N"] for s in scored)
    always = sum(s["recover_cost"] for s in scored)
    gate = sum(s["realized"] for s in scored)
    oracle = sum(s["best"] for s in scored)
    L += ["", "3. ECONOMIC COST (tokens)",
          f"  {'policy':<26}{'total':>10}{'vs restart':>12}{'vs oracle':>11}"]
    for name, v in (("always investigate", always), ("always restart", restart),
                    ("GATE 1", gate), ("ORACLE", oracle)):
        L.append(f"  {name:<26}{v:>10}{v/max(1,restart):>12.2f}"
                 f"{v/max(1,oracle):>11.2f}")

    # --- error analysis -----------------------------------------------------
    L += ["", "4. FALSE RESTARTS (the error that discards recoverable work)"]
    fails = [s for s in scored if s["verdict"] == "FALSE RESTART"]
    if not fails:
        L.append("  none")
    else:
        L.append(f"  {'case':<24}{'A/N':>6}{'f_true':>8}{'f_str':>7}"
                 f"{'over':>7}{'pred':>7}{'regret':>8}")
        for s in fails:
            L.append(f"  {s['case_id']:<24}{s['a_over_n']:>6.2f}"
                     f"{s['f_true']:>8.2f}{s['f_structural']:>7.2f}"
                     f"{s['f_structural']-s['f_true']:>+7.2f}"
                     f"{s['predicted']:>7.2f}"
                     f"{s['realized']-s['best']:>8.0f}")
        L.append(f"  total false-restart regret: "
                 f"{sum(s['realized']-s['best'] for s in fails):.0f} tokens")

    L += ["", "5. FALSE RECOVERIES (regret is bounded by A, Gate 2 caps replay)"]
    fcs = [s for s in scored if s["verdict"] == "FALSE RECOVERY"]
    if not fcs:
        L.append("  none")
    else:
        L.append(f"  {'case':<24}{'A/N':>6}{'f_true':>8}{'regret':>8}{'A':>7}")
        for s in fcs:
            L.append(f"  {s['case_id']:<24}{s['a_over_n']:>6.2f}"
                     f"{s['f_true']:>8.2f}{s['realized']-s['best']:>8.0f}"
                     f"{s['A']:>7}")
        L.append(f"  total false-recovery regret: "
                 f"{sum(s['realized']-s['best'] for s in fcs):.0f} tokens")

    # --- by family ----------------------------------------------------------
    L += ["", "6. BY ADVERSARIAL FAMILY"]
    L.append(f"  {'fam':<4}{'name':<34}{'n':>3}{'correct':>9}{'fRST':>6}{'fREC':>6}")
    for fam in sorted({s["family"] for s in scored}):
        sub = [s for s in scored if s["family"] == fam]
        ok = sum(1 for s in sub if "correct" in s["verdict"])
        a = sum(1 for s in sub if s["verdict"] == "FALSE RESTART")
        b = sum(1 for s in sub if s["verdict"] == "FALSE RECOVERY")
        L.append(f"  {fam:<4}{sub[0]['family_name']:<34}{len(sub):>3}"
                 f"{ok:>9}{a:>6}{b:>6}")

    # --- break-even ---------------------------------------------------------
    L += ["", "7. BREAK-EVEN BEHAVIOUR (|true margin| < 0.15)"]
    near = [s for s in scored if abs(s["true_margin"]) < 0.15]
    if not near:
        L.append("  no case landed within 15% of break-even")
    else:
        for s in near:
            L.append(f"  {s['case_id']:<24} margin={s['true_margin']:+.3f} "
                     f"pred={s['predicted']:.2f} "
                     f"{'ok' if 'correct' in s['verdict'] else s['verdict']}")

    # --- calibration --------------------------------------------------------
    L += ["", "8. CALIBRATION (from prior campaign only, never this suite)"]
    for arm in sorted({s["arm"] for s in scored}):
        sub = [s for s in scored if s["arm"] == arm]
        err = [s["a_estimate"] / max(1, s["N"]) - s["a_over_n"] for s in sub]
        L.append(f"  {arm:<6} scale={sub[0]['a_scale']:.3f} "
                 f"({sub[0]['calibration']})")
        L.append(f"         mean Ahat/N - A/N = {st.mean(err):+.3f}  "
                 f"(n={len(sub)})")

    # --- f estimate ---------------------------------------------------------
    L += ["", "9. IS THE STRUCTURAL BOUND STILL AN UPPER BOUND?"]
    under = [s for s in scored if s["f_structural"] < s["f_true"] - 1e-9]
    L.append(f"  cases where f_struct < f_true: {len(under)}/{len(scored)}")
    L.append(f"  mean over-prediction f_struct - f_true = "
             f"{st.mean(s['f_structural']-s['f_true'] for s in scored):+.3f}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=OUT)
    args = parser.parse_args(argv)
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    scored = score(payload["rows"])
    print(render(scored))
    out = args.results.with_name("real-falsification-scored.json")
    out.write_text(json.dumps(scored, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
