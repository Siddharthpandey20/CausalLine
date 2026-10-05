"""Render every figure in the minor-project report from measured data.

Nothing here is drawn by hand from memory. The architecture figures are built
from `topology.json`, which `diagrams/introspect_topology.py` derives from a
real executed trace; the result figures are built from `results.json`, which is
derived from the campaign result files in `data/results/`.

    python diagrams/make_figures.py

Outputs vector PDFs into `figures/`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIG = ROOT / "figures"
FIG.mkdir(exist_ok=True)

# --- palette -----------------------------------------------------------------
# Validated with the data-viz validator (light mode, surface #fcfcfb):
# lightness band PASS, chroma floor PASS, CVD separation PASS (worst adjacent
# dE 9.1 protan), normal-vision floor PASS (dE 22.9). The contrast WARN on the
# two lighter slots is relieved the way the method requires: every bar carries a
# visible value label, and every figure has a legend.
#
# Colour follows the entity and the order is fixed across every figure: a method
# keeps its hue whether or not the others are present.
C_CAUSALLINE = "#2a78d6"   # slot 1, blue   - the method under test
C_B2 = "#eb6834"           # slot 2, orange - topology closure, the comparator
C_B1 = "#1baf7a"           # slot 3, aqua   - agent taint
C_B0 = "#eda100"           # slot 4, yellow - full restart
C_STRUCT = "#eb6834"       # structural closure reuses the comparator hue
C_TRUE = "#2a78d6"

INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#8a8982"
GRID = "#e3e3df"
SURFACE = "#fcfcfb"

# provider hues for the agent graphs, same categorical order
P_LOCAL = "#1baf7a"
P_GEMINI = "#2a78d6"
P_NVIDIA = "#eb6834"
P_EXEC = "#52514e"

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["DejaVu Serif", "Times New Roman"],
    "font.size": 7.2,
    "axes.labelsize": 7.5,
    "axes.titlesize": 8,
    "legend.fontsize": 6.8,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "axes.edgecolor": MUTED,
    "axes.linewidth": 0.6,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "pdf.fonttype": 42,
})

COL = 3.45      # IEEE single column, inches
FULL = 7.16     # IEEE double column


def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def thin(ax, left=True, bottom=True):
    """Recessive axes: no top/right spines, muted grid behind the marks."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_visible(left)
    ax.spines["bottom"].set_visible(bottom)
    ax.tick_params(colors=INK2, length=2.5, width=0.6)
    ax.set_axisbelow(True)


# =============================================================================
# Fig. 1  CausalLine recovery pipeline
# =============================================================================
def fig_architecture():
    fig, ax = plt.subplots(figsize=(FULL, 2.95))
    ax.set_xlim(0, 100); ax.set_ylim(0, 42); ax.axis("off")

    def box(x, y, w, h, title, body, fc="#ffffff", ec=MUTED, lw=0.8):
        ax.add_patch(FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.35,rounding_size=0.9",
            facecolor=fc, edgecolor=ec, linewidth=lw))
        ax.text(x + w / 2, y + h - 2.2, title, ha="center", va="top",
                fontsize=7.2, color=INK, fontweight="bold", linespacing=1.3)
        ax.text(x + w / 2, y + h - 9.2, body, ha="center", va="top",
                fontsize=6.2, color=INK2, linespacing=1.5)

    def arrow(x1, y1, x2, y2, color=INK2, style="-|>", lw=0.9, ls="-"):
        ax.add_patch(FancyArrowPatch(
            (x1, y1), (x2, y2), arrowstyle=style, mutation_scale=7,
            color=color, linewidth=lw, linestyle=ls,
            shrinkA=0, shrinkB=0))

    # runtime stage
    box(1, 23, 17, 17, "1. Provenance\nrecording",
        "during execution\nevents, exposures,\nsources, ingestion", fc="#eef4fc")
    # post-detection stages
    box(21, 23, 17, 17, "2. Influence\nattribution",
        "self-report +\ncounterfactual\nreplay", fc="#ffffff")
    box(41, 23, 17, 17, "3. Contamination\npropagation",
        "influence, derivation,\ningestion\n(not reachability)", fc="#ffffff")
    box(61, 23, 17, 17, "4. Planning &\nselective replay",
        "invalidation set,\ncheckpoints,\nbyte-exact splice", fc="#ffffff")
    box(81, 23, 18, 17, "5. Verification",
        "task check, memory,\nredaction check", fc="#ffffff")

    for x in (18, 38, 58, 78):
        arrow(x, 31.5, x + 3, 31.5)

    ax.text(9.5, 41.4, "runtime", ha="center", fontsize=6.4, color=MUTED,
            style="italic")
    ax.text(60, 41.4, "after the detector fires", ha="center", fontsize=6.4,
            color=MUTED, style="italic")

    # escalation ladder
    box(21, 3, 24, 13, "pass -> accept recovery",
        "selective: only influenced\nevents recomputed", fc="#eef9f4",
        ec=P_LOCAL)
    box(50, 3, 22, 13, "fail -> agent restart",
        "widen to whole agents", fc="#fff6e8", ec=C_B0)
    box(76, 3, 23, 13, "fail -> full restart",
        "discard everything\n(guaranteed fallback)", fc="#fdeee8", ec=C_B2)

    # Both branches leave VERIFICATION, and a pass never leads to a restart.
    ax.add_patch(FancyArrowPatch(
        (86, 23), (40, 16.4), arrowstyle="-|>", mutation_scale=7,
        color=P_LOCAL, linewidth=0.9, shrinkA=0, shrinkB=0,
        connectionstyle="arc3,rad=0.12"))
    ax.add_patch(FancyArrowPatch(
        (90, 23), (63, 16.4), arrowstyle="-|>", mutation_scale=7,
        color=C_B0, linewidth=0.9, shrinkA=0, shrinkB=0,
        connectionstyle="arc3,rad=0.10"))
    arrow(72, 9.5, 76, 9.5, color=C_B2)
    ax.text(47, 20.6, "verified", fontsize=6.2, color=P_LOCAL, ha="center")
    ax.text(79, 20.0, "not verified", fontsize=6.2, color=C_B0, ha="center")
    ax.text(74, 11.6, "fails\nagain", fontsize=5.9, color=C_B2, ha="center",
            linespacing=1.2)

    ax.text(0.6, 0.0, "Escalation ladder: the recovery is never shipped "
            "unverified, so the worst case is restart's outcome.",
            fontsize=6.2, color=INK2)

    fig.savefig(FIG / "fig_architecture.pdf", bbox_inches="tight", dpi=600)
    plt.close(fig)


# =============================================================================
# Fig. 2  Stage-level agent workflow (who calls whom)
# =============================================================================
def fig_agent_workflow():
    topo = load("topology.json")
    stages = topo["stages"]
    fig, ax = plt.subplots(figsize=(FULL, 2.6))
    ax.set_xlim(-4, 102); ax.set_ylim(-3, 32); ax.axis("off")

    # (label, n agents, provider of the stage, x)
    nodes = [
        ("Stage A\nacquisition", stages["A acquisition"]["agents"], P_LOCAL, 4),
        ("Stage B\nnormalise", stages["B normalise"]["agents"], P_LOCAL, 20),
        ("Stage C\nhub", stages["C hub"]["agents"], P_GEMINI, 36),
        ("Stage C\nspecialists", stages["C specialists"]["agents"], "mix", 50),
        ("Stage D\nverifiers", stages["D verifiers"]["agents"], "mix", 65),
        ("Stage E\nreviewers", stages["E reviewers"]["agents"], "mix", 79),
        ("Stage F\nfinalise", 3, "mix", 93),
    ]
    W, H, Y = 11.0, 12, 11
    for label, n, prov, x in nodes:
        fc = {"mix": "#ffffff", P_LOCAL: "#eef9f4",
              P_GEMINI: "#eef4fc"}.get(prov, "#ffffff")
        ec = P_GEMINI if prov == P_GEMINI else MUTED
        ax.add_patch(FancyBboxPatch(
            (x - W / 2, Y), W, H, boxstyle="round,pad=0.3,rounding_size=0.8",
            facecolor=fc, edgecolor=ec, linewidth=0.9))
        ax.text(x, Y + H - 2.2, label, ha="center", va="top", fontsize=6.6,
                color=INK, fontweight="bold", linespacing=1.3)
        ax.text(x, Y + 2.6, f"{n} agent" + ("s" if n != 1 else ""),
                ha="center", fontsize=6.4, color=INK2)

    # edges, annotated with the measured multiplicity
    counts = {}
    import re
    for e, n in topo["control_edges"].items():
        a, b = e.split("->")
        counts[(re.sub(r"\d+", "", a), re.sub(r"\d+", "", b))] = \
            counts.get((re.sub(r"\d+", "", a), re.sub(r"\d+", "", b)), 0) + n
    labels = [counts.get(k, 0) for k in
              [("acq", "norm"), ("norm", "hub"), ("hub", "spec"),
               ("spec", "ver"), ("ver", "rev"), ("rev", "synth")]]
    xs = [(4, 20), (20, 36), (36, 50), (50, 65), (65, 79), (79, 93)]
    for (x1, x2), lab in zip(xs, labels):
        ax.add_patch(FancyArrowPatch(
            (x1 + W / 2, Y + H / 2), (x2 - W / 2, Y + H / 2),
            arrowstyle="-|>", mutation_scale=7, color=INK2, linewidth=0.9,
            shrinkA=0, shrinkB=0))
        ax.text((x1 + x2) / 2, Y + H / 2 + 2.3, str(lab), ha="center",
                fontsize=5.9, color=MUTED)

    # the hub broadcast, which is the structural point of the topology
    # Routed as a staple strictly below the stage boxes, so it crosses no
    # label. The boxes occupy y in [Y, Y+H]; this sits at y = Y - 5.
    yb = Y - 5
    ax.plot([36, 36], [Y, yb], color=P_GEMINI, lw=0.9, ls=(0, (3, 2)))
    ax.plot([36, 50], [yb, yb], color=P_GEMINI, lw=0.9, ls=(0, (3, 2)))
    ax.add_patch(FancyArrowPatch(
        (50, yb), (50, Y), arrowstyle="-|>", mutation_scale=7,
        color=P_GEMINI, linewidth=0.9, linestyle=(0, (3, 2)),
        shrinkA=0, shrinkB=0))
    ax.text(43, 0.2, "hub roster broadcast: every specialist is EXPOSED to "
            "every record, each USES one",
            ha="center", fontsize=6.2, color=P_GEMINI)

    # the memory channel has no call-graph edge back to its writer
    ax.add_patch(FancyArrowPatch(
        (20, Y + H), (20, Y + H + 5), arrowstyle="-|>", mutation_scale=7,
        color=MUTED, linewidth=0.8, linestyle=(0, (2, 2))))
    ax.text(20, Y + H + 5.6, "memory", ha="center", fontsize=6.2, color=INK2)

    handles = [plt.Line2D([], [], marker="s", ls="", markersize=5,
                          markerfacecolor=c, markeredgecolor=MUTED, label=l)
               for c, l in (("#eef9f4", "local Llama-3-8B"),
                            ("#eef4fc", "Gemini-3.8-Flash"),
                            ("#ffffff", "mixed providers"))]
    ax.legend(handles=handles, loc="upper right", frameon=False, ncol=3,
              bbox_to_anchor=(1.02, 1.10), handletextpad=0.4, columnspacing=1.2)

    fig.savefig(FIG / "fig_agent_workflow.pdf", bbox_inches="tight", dpi=600)
    plt.close(fig)


# =============================================================================
# Fig. 3  The 56-agent invocation graph, every node, from a real trace
# =============================================================================
def fig_agent_trace():
    topo = load("topology.json")
    routing = topo["routing"]
    fig, ax = plt.subplots(figsize=(FULL, 3.15))

    layers = [
        ("A", [f"acq{i}" for i in range(1, 13)]),
        ("B", [f"norm{j}" for j in range(1, 11)]),
        ("C", ["hub"]),
        ("C'", [f"spec{k}" for k in range(1, 13)]),
        ("D", [f"ver{v}" for v in range(1, 11)]),
        ("E", [f"rev{r}" for r in range(1, 9)]),
        ("F", ["synth", "audit", "executor"]),
    ]
    pos, lane = {}, {}
    for li, (name, agents) in enumerate(layers):
        lane[name] = li
        n = len(agents)
        for ai, a in enumerate(agents):
            y = (n - 1) / 2 - ai
            pos[a] = (li * 1.0, y * (7.0 / max(n, 1)))

    def colour(a):
        if a == "executor":
            return P_EXEC
        return {"gemini": P_GEMINI, "nvidia": P_NVIDIA}.get(
            routing.get(a, "local"), P_LOCAL)

    for e in topo["control_edges"]:
        a, b = e.split("->")
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                        arrowprops=dict(arrowstyle="-|>", color="#c9c9c4",
                                        lw=0.45, shrinkA=3.2, shrinkB=3.2,
                                        mutation_scale=5))
    # the norm->spec data edge has no control edge; draw it distinctly
    for e in topo["data_edges"]:
        a, b = e.split("->")
        if a.startswith("norm") and b.startswith("spec") and a in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                        arrowprops=dict(arrowstyle="-|>", color="#f2c4ad",
                                        lw=0.45, ls=(0, (2, 2)),
                                        shrinkA=3.2, shrinkB=3.2,
                                        mutation_scale=5))

    # Nodes are dots, not labelled circles: 56 inline labels are illegible at
    # column width, and the lane headers plus the four named singletons carry
    # the identity instead.
    singles = {"hub": "hub", "synth": "synth", "audit": "audit",
               "executor": "executor"}
    for a, (x, y) in pos.items():
        big = a in singles
        ax.scatter([x], [y], s=74 if big else 30, c=colour(a),
                   edgecolors="#ffffff", linewidths=0.8, zorder=3)
        if big:
            ax.text(x + 0.12, y, singles[a], ha="left", va="center",
                    fontsize=5.8, color=INK, zorder=4)

    for name, agents in layers:
        li = lane[name]
        ax.text(li, 4.3, {"A": "A\nacquire", "B": "B\nnormalise",
                          "C": "C\nhub", "C'": "C\nspecialists",
                          "D": "D\nverify", "E": "E\nreview",
                          "F": "F\nfinalise"}[name],
                ha="center", va="bottom", fontsize=6.3, color=INK2,
                linespacing=1.25)

    ax.set_xlim(-0.45, 6.95); ax.set_ylim(-5.2, 5.6); ax.axis("off")
    handles = [plt.Line2D([], [], marker="o", ls="", markersize=5.2,
                          markerfacecolor=c, markeredgecolor="#ffffff",
                          label=l)
               for c, l in ((P_LOCAL, "local Llama-3-8B (50)"),
                            (P_GEMINI, "Gemini-3.8-Flash (2)"),
                            (P_NVIDIA, "NVIDIA Nemotron (4)"),
                            (P_EXEC, "executor, no model call"))]
    handles += [plt.Line2D([], [], color="#c9c9c4", lw=0.9,
                           label="control edge (recorded parent)"),
                plt.Line2D([], [], color="#f2c4ad", lw=0.9, ls=(0, (2, 2)),
                           label="data edge without a control edge")]
    ax.legend(handles=handles, loc="lower center", frameon=False, ncol=3,
              bbox_to_anchor=(0.5, -0.12), handletextpad=0.5,
              columnspacing=1.4)

    fig.savefig(FIG / "fig_agent_trace.pdf", bbox_inches="tight", dpi=600)
    plt.close(fig)


# =============================================================================
# Fig. 4  Exposure versus influence
# =============================================================================
def fig_exposure_influence():
    res = load("results.json")["varied"]
    regimes = ["small", "medium", "large"]
    struct = [res[r]["fstruct_mean"] * 100 for r in regimes]
    true = [res[r]["f_mean"] * 100 for r in regimes]

    fig, ax = plt.subplots(figsize=(COL, 2.25))
    x = range(len(regimes)); w = 0.36
    b1 = ax.bar([i - w / 2 for i in x], struct, w, color=C_STRUCT,
                label="structural closure (discarded by B2)", linewidth=0)
    b2 = ax.bar([i + w / 2 for i in x], true, w, color=C_TRUE,
                label="true influence (canary)", linewidth=0)
    for bars in (b1, b2):
        for b in bars:
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 1.6,
                    f"{b.get_height():.0f}", ha="center", fontsize=6.2,
                    color=INK2)
    for i, (s, t) in enumerate(zip(struct, true)):
        ax.text(i, max(s, t) + 8.5, f"gap {s - t:.1f} pts", ha="center",
                fontsize=6.2, color=INK, fontweight="bold")

    ax.set_xticks(list(x)); ax.set_xticklabels(regimes)
    ax.set_ylabel("% of the 145-event trace")
    ax.set_ylim(0, 112)
    ax.grid(axis="y", color=GRID, linewidth=0.5)
    thin(ax)
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(-0.02, 1.02))
    fig.savefig(FIG / "fig_exposure_influence.pdf", bbox_inches="tight", dpi=600)
    plt.close(fig)


# =============================================================================
# Fig. 5  Work preserved by method
# =============================================================================
def fig_preserved():
    res = load("results.json")["varied"]
    regimes = ["small", "medium", "large"]
    series = [("B0 full restart", "b0", C_B0),
              ("B1 agent taint", "b1", C_B1),
              ("B2 topology closure", "b2", C_B2),
              ("CausalLine", "cl", C_CAUSALLINE)]

    fig, ax = plt.subplots(figsize=(COL, 2.55))
    x = range(len(regimes)); w = 0.2
    for si, (label, key, colour) in enumerate(series):
        vals = [res[r].get(key, 0) * 100 if key != "b0" else 0.0
                for r in regimes]
        offs = [i + (si - 1.5) * w for i in x]
        bars = ax.bar(offs, vals, w * 0.92, color=colour, label=label,
                      linewidth=0)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 1.5, f"{v:.0f}",
                    ha="center", fontsize=5.8,
                    color=INK2 if v > 0.6 else C_B0)
    ax.set_xticks(list(x)); ax.set_xticklabels(regimes)
    ax.set_ylabel("work preserved (%)")
    ax.set_ylim(0, 104)
    ax.grid(axis="y", color=GRID, linewidth=0.5)
    thin(ax)
    ax.legend(frameon=False, loc="upper right", ncol=1, handlelength=1.1)
    fig.savefig(FIG / "fig_preserved.pdf", bbox_inches="tight", dpi=600)
    plt.close(fig)


# =============================================================================
# Fig. 6  What the extra tokens buy
# =============================================================================
def fig_cost():
    res = load("results.json")["varied"]
    regimes = ["small", "medium", "large"]
    gain = [res[r]["diff"] for r in regimes]
    ratio = [(res[r]["cl_ana"] + res[r]["cl_rep"]) / res[r]["b0_tok"]
             for r in regimes]

    fig, ax = plt.subplots(figsize=(COL, 2.3))
    bars = ax.bar(range(len(regimes)), gain, 0.5,
                  color=[C_CAUSALLINE if g > 0 else C_B2 for g in gain],
                  linewidth=0)
    for i, (b, g, r) in enumerate(zip(bars, gain, ratio)):
        va = "bottom" if g > 0 else "top"
        ax.text(i, g + (1.6 if g > 0 else -2.4), f"{g:+.1f}", ha="center",
                va=va, fontsize=6.6, color=INK, fontweight="bold")
        # Cost annotation sits in its own band below every bar, so it cannot
        # collide with the value label of a negative bar.
        ax.text(i, -32.5, f"{r:.1f}x restart cost", ha="center",
                fontsize=6.0, color=INK2)
    ax.axhline(0, color=MUTED, linewidth=0.7)
    ax.set_xticks(range(len(regimes))); ax.set_xticklabels(regimes)
    ax.set_ylabel("preserved work vs B2 (pts)")
    ax.set_ylim(-36, 22)
    ax.grid(axis="y", color=GRID, linewidth=0.5)
    thin(ax, bottom=False)
    ax.text(0.02, 0.97, "blue = gain, orange = loss", transform=ax.transAxes,
            fontsize=6.2, color=INK2, va="top")
    fig.savefig(FIG / "fig_cost.pdf", bbox_inches="tight", dpi=600)
    plt.close(fig)


if __name__ == "__main__":
    fig_architecture()
    fig_agent_workflow()
    fig_agent_trace()
    fig_exposure_influence()
    fig_preserved()
    fig_cost()
    for f in sorted(FIG.glob("*.pdf")):
        print(f"  {f.name:<34} {f.stat().st_size/1024:6.1f} KB")
    print("figures written to", FIG)
