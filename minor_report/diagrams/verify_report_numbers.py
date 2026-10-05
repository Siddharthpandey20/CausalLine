"""Cross-check every numeric claim in the report against the project data."""
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")
from src.eval.mixed_validation_report import load_runs, row_for  # noqa: E402

t = Path("minor_report/main.tex").read_text(encoding="utf-8")
topo = json.loads(Path("minor_report/diagrams/topology.json").read_text("utf-8"))
rows = [row_for(x) for x in load_runs(Path("data/results/mixed56-workload-varied"))]
cells = json.loads(Path("data/results/campaign.json").read_text("utf-8"))

def m(reg, k):
    return statistics.fmean([r[k] for r in rows if r["regime"] == reg])

def wtl(reg):
    d = [(r["CausalLine_preserved"] - r["B2_preserved"]) * 100
         for r in rows if r["regime"] == reg]
    return (sum(1 for x in d if x > 1e-9), sum(1 for x in d if abs(x) <= 1e-9),
            sum(1 for x in d if x < -1e-9))

checks = []
def chk(label, claim, actual, ok=None):
    good = (claim == actual) if ok is None else ok
    checks.append((good, label, claim, actual))

# --- architecture -----------------------------------------------------------
chk("agents", 56, topo["agents_total"])
chk("events", 145, topo["events_total"])
chk("sources", 89, topo["sources_total"])
chk("control edges", 66, len(topo["control_edges"]))
chk("data edges", 78, len(topo["data_edges"]))
st = topo["stages"]
for name, key, a, e in (("A", "A acquisition", 12, 48),
                        ("B", "B normalise", 10, 30),
                        ("C hub", "C hub", 1, 2),
                        ("C spec", "C specialists", 12, 24),
                        ("D", "D verifiers", 10, 20),
                        ("E", "E reviewers", 8, 16)):
    chk(f"stage {name} agents", a, st[key]["agents"])
    chk(f"stage {name} events", e, st[key]["events"])
chk("gemini agents", 2, sum(1 for v in topo["routing"].values() if v == "gemini"))
chk("nvidia agents", 4, sum(1 for v in topo["routing"].values() if v == "nvidia"))
chk("local agents", 50, 56 - 6)

# --- main results table -----------------------------------------------------
for reg, b1, b2, cl, diff in (("small", 89.9, 89.9, 72.6, -17.38),
                              ("medium", 38.1, 37.4, 44.4, 7.03),
                              ("large", 9.2, 8.6, 23.7, 15.17)):
    chk(f"{reg} B1", b1, round(m(reg, "B1_preserved") * 100, 1))
    chk(f"{reg} B2", b2, round(m(reg, "B2_preserved") * 100, 1))
    chk(f"{reg} CL", cl, round(m(reg, "CausalLine_preserved") * 100, 1))
    d = statistics.fmean([(r["CausalLine_preserved"] - r["B2_preserved"]) * 100
                          for r in rows if r["regime"] == reg])
    chk(f"{reg} diff", diff, round(d, 2))
chk("small W/T/L", (2, 2, 1), wtl("small"))
chk("medium W/T/L", (5, 0, 0), wtl("medium"))
chk("large W/T/L", (5, 0, 0), wtl("large"))

# --- gaps -------------------------------------------------------------------
for reg, gap in (("small", 2.2), ("medium", 7.0), ("large", 15.2)):
    chk(f"{reg} gap", gap,
        round((m(reg, "f_structural") - m(reg, "f_true")) * 100, 1))
chk("large f_struct", 91.4, round(m("large", "f_structural") * 100, 1))
chk("large f_true", 76.3, round(m("large", "f_true") * 100, 1))

# --- safety -----------------------------------------------------------------
chk("unsafe total", 0, sum(r[f"{x}_unsafe"] for r in rows
                           for x in ("B0", "B1", "B2", "CausalLine")))
chk("escapes total", 0, sum(r["closure_escapes"] for r in rows))
chk("landed", 13, sum(1 for r in rows if r["payload_landed"]))
chk("medium landed", 3, sum(1 for r in rows
                            if r["regime"] == "medium" and r["payload_landed"]))
chk("degraded", 0, sum(len(r["degraded"]) for r in rows))

# --- escalation -------------------------------------------------------------
from collections import Counter
sc = Counter(r["delivered_scope"] for r in rows)
chk("selective", 12, sc["selective"])
chk("agent_restart", 2, sc["agent_restart"])
chk("restart_all", 1, sc["restart_all"])
chk("verification failures", 4, sum(r["verification_failures"] for r in rows))

# --- cost -------------------------------------------------------------------
for reg, b2t, ana, tot, ratio in (("small", 1595, 16312, 19598, 1.8),
                                  ("medium", 7975, 15796, 23771, 2.2),
                                  ("large", 9772, 15911, 25683, 2.5)):
    chk(f"{reg} B2 tok", b2t,
        round(m(reg, "B2_replay_tokens") + m(reg, "B2_analysis_tokens")))
    chk(f"{reg} CL analysis", ana, round(m(reg, "CausalLine_analysis_tokens")))
    chk(f"{reg} CL total", tot, round(m(reg, "CausalLine_analysis_tokens")
                                      + m(reg, "CausalLine_replay_tokens")))
    b0 = m(reg, "B0_replay_tokens") + m(reg, "B0_analysis_tokens")
    chk(f"{reg} vs B0", ratio,
        round((m(reg, "CausalLine_analysis_tokens")
               + m(reg, "CausalLine_replay_tokens")) / b0, 1))

# --- scripted matrix --------------------------------------------------------
chk("scripted cells", 96, len(cells))
chk("scripted reps", 30, cells[0]["repetitions"])
chk("scripted detectors", 4, len({c["detector"] for c in cells}))
clc = [c for c in cells if c["method"] == "CausalLine"]
chk("CausalLine cells", 24, len(clc))
chk("CausalLine unsafe cells", 0, sum(1 for c in clc if c["unsafe_run_rate"] > 0))

# --- repo facts -------------------------------------------------------------
loc = subprocess.run("find src -name '*.py' | xargs wc -l | tail -1",
                     shell=True, capture_output=True, text=True).stdout.split()
chk("source lines ~36k", True, 34000 < int(loc[0]) < 38000,
    ok=34000 < int(loc[0]) < 38000)

print(f"{'':2} {'claim':<26} {'in report':>14}  {'measured':>14}")
bad = 0
for good, label, claim, actual in checks:
    if not good:
        bad += 1
    print(f"{'ok' if good else '!!':2} {label:<26} {str(claim):>14}  "
          f"{str(actual):>14}")
print(f"\n{len(checks)} claims checked, {bad} mismatched")
