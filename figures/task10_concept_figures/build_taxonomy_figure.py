#!/usr/bin/env python3
"""Build Figure 1: representation-centric taxonomy (vector PDF)."""

from pathlib import Path
import sys

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

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


OUT = Path(__file__).resolve().parent / "representation_taxonomy.pdf"


ROWS = [
    ("R0", "Opaque callback", "Call site;\ninternals hidden", "Run time", "Runtime /\nexecution only", "Strong\nempirical", "strong"),
    ("R1", "Scalar metadata", "Cost / selectivity\nscalar", "Costing", "Cost model /\ncost-only", "Strong\nempirical", "strong"),
    ("R2", "Analytical /\nlearned summary", "Input-conditioned\nbehavior", "Analytical", "Cost estimator /\nwork estimate", "Partial\nempirical", "partial"),
    ("R3", "Adaptive /\nprofiled summary", "Profiles and\nfeedback", "Post-execution", "Estimator /\nreoptimizer", "Artifact-\nblocked", "blocked"),
    ("R4a", "Full/native\nrelational exposure", "Relational tree\nFroid / PostgreSQL", "Pre-plan\nrewrite", "Rewriter + enumerator\npath / join / pushdown", "Strong\nempirical", "strong"),
    ("R4b", "Selective\nrelational exposure", "Selective/residual\nPRISM", "Pre-plan\nrewrite", "Rewriter + enumerator\nselective plan reach", "Partial\nempirical", "partial"),
    ("R5", "Static semantics /\nproperties", "Dependencies\nand effects", "Pre-plan\nanalysis", "Rewriter + enumerator\nsafety / placement", "Literature-\nonly", "literature"),
    ("R6", "Execution /\ndataflow IR", "Compiler + plan IR\nQFusor", "Post-plan", "Execution optimizer\nfusion / vectorization", "Literature-\nonly", "literature"),
    ("R7", "Source-aware\nlearned structure", "Source CFG +\nSQL plan\nGRACEFUL", "Costing", "Learned estimator\ncost / placement model", "Partial\nempirical", "partial"),
        ("R8", "Cross-layer\nsynthesis", r"$S(U\mid Q,p)$" "\nhybrid evidence", "Proposed", "Multiple consumers\ninterface requirements", "Synthesis +\ncomponents", "synthesis"),
]


STATUS_STYLE = {
    "strong": (NAVY_LIGHT, NAVY, "solid"),
    "partial": (OCHRE_LIGHT, OCHRE, "dashed"),
    "literature": (LIGHT, MID, "solid"),
    "blocked": (WHITE, OCHRE, "dotted"),
    "synthesis": (WHITE, MID, "dashdot"),
}


def main() -> None:
    apply_style()
    fig, ax = plt.subplots(figsize=(7.25, 5.55))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # Title band states the hierarchy explicitly.
    ax.add_patch(Rectangle((0.008, 0.905), 0.984, 0.083, facecolor=NAVY, edgecolor=NAVY, linewidth=0))
    ax.text(0.024, 0.954, "Representation-Centric Taxonomy", color=WHITE,
            fontsize=10.2, fontweight="bold", va="center")
    ax.text(0.976, 0.954, "29 works  •  9 top-level regimes (R0–R8)", color=WHITE,
            fontsize=7.3, ha="right", va="center")
    ax.text(0.976, 0.924, "R4a/R4b are experimental subregimes of one top-level R4",
            color=WHITE, fontsize=6.7, ha="right", va="center")

    x = [0.012, 0.082, 0.257, 0.430, 0.548, 0.808, 0.988]
    headers = ["REGIME", "SHORT NAME", "REPRESENTATION\nFORM", "FIRST\nVISIBILITY", "CONSUMER /\nDECISION REACH", "EMPIRICAL\nSTATUS"]
    header_y = 0.855
    ax.add_patch(Rectangle((x[0], header_y), x[-1] - x[0], 0.043, facecolor=LIGHT,
                           edgecolor=GRID, linewidth=0.65))
    for i, label in enumerate(headers):
        ax.text((x[i] + x[i + 1]) / 2, header_y + 0.0215, label, ha="center", va="center",
                fontsize=5.9, fontweight="bold", color=INK, linespacing=1.05)

    row_h = 0.0695
    top = header_y
    for idx, row in enumerate(ROWS):
        y = top - (idx + 1) * row_h
        if idx % 2:
            ax.add_patch(Rectangle((x[0], y), x[-1] - x[0], row_h,
                                   facecolor="#FAFBFB", edgecolor="none"))
        for xv in x:
            ax.plot([xv, xv], [y, y + row_h], color=GRID, linewidth=0.35, zorder=1)
        ax.plot([x[0], x[-1]], [y, y], color=GRID, linewidth=0.35, zorder=1)

        regime, name, representation, visibility, consumer, status, key = row
        ax.text((x[0] + x[1]) / 2, y + row_h / 2, regime, ha="center", va="center",
                fontsize=7.3, fontweight="bold", color=NAVY if regime.startswith("R4") else INK)
        ax.text(x[1] + 0.006, y + row_h / 2, name, ha="left", va="center", fontsize=6.05, color=INK, linespacing=1.12)
        ax.text(x[2] + 0.006, y + row_h / 2, representation, ha="left", va="center", fontsize=5.95, color=INK, linespacing=1.12)
        ax.text(x[3] + 0.006, y + row_h / 2, visibility, ha="left", va="center", fontsize=5.95, color=INK, linespacing=1.12)
        ax.text(x[4] + 0.006, y + row_h / 2, consumer, ha="left", va="center", fontsize=5.85, color=INK, linespacing=1.12)
        face, edge, line = STATUS_STYLE[key]
        rounded_box(ax, (x[5] + 0.007, y + 0.014), x[6] - x[5] - 0.014, row_h - 0.028,
                    facecolor=face, edgecolor=edge, linewidth=0.85, linestyle=line, radius=0.009)
        ax.text((x[5] + x[6]) / 2, y + row_h / 2, status, ha="center", va="center",
                fontsize=5.65, color=INK, linespacing=1.08)

    # Bracket the R4 subrows so the split cannot be mistaken for a tenth regime.
    r4a_center = top - (4 + 0.5) * row_h
    r4b_center = top - (5 + 0.5) * row_h
    bx = x[0] + 0.006
    ax.plot([bx, bx], [r4b_center, r4a_center], color=NAVY, linewidth=1.4)
    ax.plot([bx, bx + 0.009], [r4a_center, r4a_center], color=NAVY, linewidth=1.4)
    ax.plot([bx, bx + 0.009], [r4b_center, r4b_center], color=NAVY, linewidth=1.4)

    legend_y = 0.070
    ax.text(0.014, legend_y + 0.030, "STATUS KEY", fontsize=6.0, fontweight="bold", color=INK)
    legend = [
        ("Strong empirical", "strong"),
        ("Partial empirical", "partial"),
        ("Literature-only", "literature"),
        ("Artifact-blocked", "blocked"),
        ("Synthesis + components", "synthesis"),
    ]
    lx = [0.102, 0.273, 0.442, 0.610, 0.780]
    for xpos, (label, key) in zip(lx, legend):
        face, edge, line = STATUS_STYLE[key]
        rounded_box(ax, (xpos, legend_y + 0.010), 0.020, 0.025, facecolor=face,
                    edgecolor=edge, linewidth=0.85, linestyle=line, radius=0.004)
        ax.text(xpos + 0.026, legend_y + 0.0225, label, ha="left", va="center", fontsize=5.4, color=INK)
    ax.text(0.014, 0.038, "UDFBench is a cross-regime benchmark anchor, not a representation regime.",
            fontsize=5.75, color=MID, va="center")
    ax.text(0.986, 0.017, "Status describes this project's evidence—not field-wide maturity.",
            fontsize=5.75, color=MID, va="center", ha="right")

    finish(fig, OUT)


if __name__ == "__main__":
    main()
