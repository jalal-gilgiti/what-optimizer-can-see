#!/usr/bin/env python3
"""Build Figure 2: evidence-derived representation-to-decision framework."""

from pathlib import Path
import sys

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

sys.path.insert(0, str(Path(__file__).resolve().parent))
from figure_style import (  # noqa: E402
    GRID,
    INK,
    LIGHT,
    MID,
    NAVY,
    NAVY_LIGHT,
    OCHRE,
    OCHRE_LIGHT,
    WHITE,
    apply_style,
    finish,
    rounded_box,
)


OUT = Path(__file__).resolve().parent / "representation_to_decision_framework.pdf"


def arrow(ax, start, end, *, color=INK, linewidth=0.95, style="-|>", zorder=3):
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle=style, mutation_scale=8,
                                color=color, linewidth=linewidth, zorder=zorder))


def main() -> None:
    apply_style()
    fig, ax = plt.subplots(figsize=(7.25, 6.15))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.add_patch(Rectangle((0.008, 0.912), 0.984, 0.076, facecolor=NAVY, edgecolor=NAVY, linewidth=0))
    ax.text(0.024, 0.958, "Evidence-Derived Representation-to-Decision Framework",
            color=WHITE, fontsize=9.8, fontweight="bold", va="center")
    ax.text(0.976, 0.930,
            "Optimization reach is governed by adequacy, decision-time consumability, and acquisition cost",
            color=WHITE, fontsize=6.7, ha="right", va="center")

    # Central chain: seven distinct handoffs; no box is presented as a deployed component.
    labels = [
        ("PROCEDURAL\nBEHAVIOR", "loops, branches\naccess, effects"),
        ("REPRESENTATION", "scalar, profiled\nrelational\nsource / execution IR"),
        ("PROVENANCE /\nACQUISITION", "metadata, analysis\nrewrite, profile\nlearn"),
        ("FIRST\nVISIBILITY", "definition, pre-plan\ntransformation\nexecution"),
        ("OPTIMIZER\nCONSUMER", "cost model, planner\nrewriter\nexecution optimizer"),
        ("DECISION\nREACH", "cost, placement, path\njoin / predicate\nexecution strategy"),
        ("EXECUTION\nCONSEQUENCE", "plan shape, work\nruntime\noverhead"),
    ]
    left, gap, width, height, y = 0.014, 0.0105, 0.1305, 0.135, 0.715
    centers = []
    for i, (heading, detail) in enumerate(labels):
        x = left + i * (width + gap)
        centers.append(x + width / 2)
        rounded_box(ax, (x, y), width, height, facecolor=NAVY_LIGHT,
                    edgecolor=NAVY, linewidth=0.9, radius=0.011)
        ax.text(x + width / 2, y + 0.092, heading, ha="center", va="center",
                fontsize=5.55, fontweight="bold", color=NAVY, linespacing=1.05)
        ax.text(x + width / 2, y + 0.038, detail, ha="center", va="center",
                fontsize=5.0, color=INK, linespacing=1.17)
        if i:
            arrow(ax, (x - gap + 0.001, y + height / 2), (x - 0.002, y + height / 2))

    # Controlled adequacy evidence is attached to the links it isolates.
    ax.text(0.014, 0.670, "CONTROLLED ADEQUACY EVIDENCE", fontsize=6.3,
            fontweight="bold", color=INK)
    evidence = [
        (0.014, 0.232, "E1  Amplification /\nloop work", "$R$ alone omits $A_U$"),
        (0.256, 0.232, "E3  Query-conditioned\niteration", r"$S_0(U)$ can omit $S(U\mid Q,p)$"),
        (0.498, 0.232, "E4  Branch-conditioned\nloop work", "Marginals omit joint\n$B_U$–$P_U$ dependence"),
        (0.740, 0.246, "E2  Placement\nsensitivity", "Work matters only\nwhere it is paid"),
    ]
    for x, w, title, detail in evidence:
        rounded_box(ax, (x, 0.562), w, 0.086, facecolor=WHITE, edgecolor=GRID,
                    linewidth=0.75, radius=0.008)
        ax.text(x + 0.008, 0.620, title, fontsize=5.35, fontweight="bold", color=INK,
                va="center", linespacing=1.05)
        ax.text(x + 0.008, 0.580, detail, fontsize=4.9, color=MID, va="center", linespacing=1.07)
    # Placement evidence spans representation visibility through decision reach.
    ax.plot([centers[0], centers[5]], [0.550, 0.550], color=GRID, linewidth=0.65)
    for c in [centers[0], centers[1], centers[3], centers[5]]:
        ax.plot([c, c], [0.550, 0.560], color=GRID, linewidth=0.65)

    ax.text(0.014, 0.515, "REPRODUCED MECHANISMS AND BOUNDARIES", fontsize=6.3,
            fontweight="bold", color=INK)
    mech = [
        (0.014, 0.232, "Task 3 • native exposure", "Relationalization → pre-plan\nenumerator → path / join / pushdown\nComplete observed chain", "complete"),
        (0.256, 0.232, "Task 4 • PRISM", "Selective exposure; transformation\ncost and emission-to-consumption\nboundary", "boundary"),
        (0.498, 0.232, "GRACEFUL", "Source CFG + SQL plan →\nlearned estimator; prediction and\nrepresentability boundary", "boundary"),
        (0.740, 0.246, "Task 5 / R3 audit", "Adaptive/profiled executable artifact\nunavailable; behavior not promoted\nto experimental evidence", "blocked"),
    ]
    for x, w, title, detail, kind in mech:
        face = NAVY_LIGHT if kind == "complete" else (OCHRE_LIGHT if kind == "boundary" else WHITE)
        edge = NAVY if kind == "complete" else OCHRE
        line = "solid" if kind == "complete" else ("dashed" if kind == "boundary" else "dotted")
        rounded_box(ax, (x, 0.398), w, 0.098, facecolor=face, edgecolor=edge,
                    linewidth=0.85, linestyle=line, radius=0.008)
        ax.text(x + 0.008, 0.470, title, fontsize=5.55, fontweight="bold", color=INK, va="center")
        ax.text(x + 0.008, 0.430, detail, fontsize=4.65, color=INK, va="center", linespacing=1.12)

    # Cross-cutting constraints remain independent of representation richness.
    ax.text(0.014, 0.365, "CROSS-CUTTING BOUNDARIES", fontsize=6.3,
            fontweight="bold", color=INK)
    constraints = [
        ("Acquisition / transformation cost", "analysis, rewrite, profiling, inference"),
        ("Semantic coverage / legality", "effects, unsupported constructs, valid equivalence"),
        ("Uncertainty / artifact fidelity", "exact vs. estimated; available vs. unreproducible"),
    ]
    cx = [0.014, 0.342, 0.670]
    for x, (title, detail) in zip(cx, constraints):
        rounded_box(ax, (x, 0.286), 0.316, 0.061, facecolor=LIGHT, edgecolor=MID,
                    linewidth=0.7, radius=0.007)
        ax.text(x + 0.010, 0.326, title, fontsize=5.45, fontweight="bold", color=INK, va="center")
        ax.text(x + 0.010, 0.302, detail, fontsize=4.95, color=MID, va="center")

    # Synthesis band is explicitly a requirements interface, not an architecture.
    rounded_box(ax, (0.014, 0.098), 0.972, 0.145, facecolor=WHITE, edgecolor=NAVY,
                linewidth=0.9, linestyle="dashdot", radius=0.010)
    ax.text(0.030, 0.215, "EVIDENCE-DERIVED SUMMARY / INTERFACE REQUIREMENTS — NOT A DEPLOYED OPTIMIZER",
            fontsize=6.2, fontweight="bold", color=NAVY, va="center")
    ax.text(0.030, 0.169, r"Definition-time  $S_0(U)$", fontsize=7.0, color=INK, va="center")
    arrow(ax, (0.192, 0.169), (0.266, 0.169), color=NAVY)
    ax.text(0.282, 0.169, r"Query / plan-conditioned  $S(U\mid Q,p)$", fontsize=7.0, color=INK, va="center")
    ax.text(0.030, 0.124,
            r"$S(U)=\langle A_U, B_U, P_U, D_U, E_U, \Omega_U\rangle$",
            fontsize=7.2, color=INK, va="center")
    ax.text(0.380, 0.128,
            "amplification • branch/path • dependence\ndata access • effects • provenance/uncertainty/coverage",
            fontsize=5.05, color=MID, va="center", linespacing=1.15)
    ax.text(0.970, 0.169,
            "Selected components instantiated;\ncomplete six-field interface and\nend-to-end integration not implemented",
            fontsize=5.15, color=OCHRE, ha="right", va="center", linespacing=1.20)

    ax.text(0.014, 0.053,
            "Reading rule: information is actionable only if it survives every handoff to a consumer with relevant decision reach.",
            fontsize=5.55, color=INK, va="center")
    ax.text(0.986, 0.021,
            "Solid = complete observed chain  •  dashed = measured boundary  •  dotted = artifact boundary",
            fontsize=5.05, color=MID, va="center", ha="right")

    finish(fig, OUT)


if __name__ == "__main__":
    main()
