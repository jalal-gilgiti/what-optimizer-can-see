#!/usr/bin/env python3
"""Build Task 8 publication figures from frozen, validated evidence."""

from pathlib import Path
import csv
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def style():
    plt.rcParams.update({
        "font.family": "serif",
        # STIXGeneral is a Times-metric TTF bundled with matplotlib; it is
        # preferred over the Nimbus Roman OTF, which fonttype-42 embeds badly.
        "font.serif": ["Times New Roman", "STIXGeneral", "Nimbus Roman",
                       "Times", "DejaVu Serif"],
        "mathtext.fontset": "stix",
        "font.size": 7.5,
        "axes.labelsize": 7.5,
        "axes.titlesize": 7.5,
        "legend.fontsize": 7.5,
        "xtick.labelsize": 7.5,
        "ytick.labelsize": 7.5,
        "text.color": "black",
        "axes.labelcolor": "black",
        "axes.edgecolor": "black",
        "xtick.color": "black",
        "ytick.color": "black",
        "pdf.fonttype": 42,
    })


def box(ax, x, y, w, h, text, color="#e8eef7", edge="#254b75"):
    p = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.015",
                       facecolor=color, edgecolor=edge, linewidth=1.1)
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=8, linespacing=1.15)


def representation_chain():
    fig, ax = plt.subplots(figsize=(7.25, 1.7))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    labels = ["Representation\nwhat is encoded", "Provenance\nhow obtained",
              "Decision-time\nvisibility", "Optimizer\nconsumer",
              "Estimate /\ndecision reach", "Execution\nconsequence"]
    xs = [0.01, 0.175, 0.34, 0.505, 0.67, 0.835]
    for x, label in zip(xs, labels):
        box(ax, x, 0.42, 0.145, 0.31, label)
    for x in xs[:-1]:
        ax.add_patch(FancyArrowPatch((x + 0.145, 0.575), (x + 0.165, 0.575),
                                    arrowstyle="-|>", mutation_scale=10,
                                    linewidth=1.0, color="#333333"))
    box(ax, 0.31, 0.07, 0.38, 0.19,
        "Acquisition / transformation cost and coverage",
        color="#fff1d6", edge="#9a6417")
    ax.add_patch(FancyArrowPatch((0.50, 0.27), (0.50, 0.40),
                                arrowstyle="-|>", mutation_scale=10,
                                linewidth=1.0, color="#9a6417"))
    fig.tight_layout(pad=0.2)
    fig.savefig(OUT / "representation_chain.pdf", bbox_inches="tight")
    plt.close(fig)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def controlled_progression():
    fig, axs = plt.subplots(1, 4, figsize=(7.25, 1.75))
    colors = {"SQLite": "#2f6b9a", "DuckDB": "#d9862c", "PostgreSQL": "#4b8b5a"}
    paths = {
        "SQLite": ROOT / "results/normalized/e1_amplification.csv",
        "DuckDB": ROOT / "results/normalized/e1_duckdb.csv",
        "PostgreSQL": ROOT / "results/normalized/postgresql-20260829T084826Z/e1_postgresql.csv",
    }
    for name, path in paths.items():
        rows = [r for r in read_csv(path) if int(r["R"]) == 10000]
        xs = [int(r["Lambda"]) for r in rows]
        col = "median_runtime_ms" if name == "PostgreSQL" else "median_runtime"
        ys0 = [float(r[col]) for r in rows]
        ys = [y / ys0[0] for y in ys0]
        axs[0].plot(xs, ys, marker="o", ms=3, lw=1.2, label=name, color=colors[name])
    axs[0].set_xscale("log"); axs[0].set_yscale("log")
    axs[0].set_title("E1: amplification", pad=3)
    axs[0].set_xlabel(r"loop work $\Lambda$", labelpad=1.5)
    axs[0].set_ylabel("runtime / runtime at 1", labelpad=1.5)
    axs[0].legend(frameon=False, loc="upper left", handlelength=1.4,
                  handletextpad=0.4, labelspacing=0.25, borderpad=0.0)

    names = ["SQLite", "DuckDB", "PostgreSQL"]
    e2 = [99.1539, 95.4074, 98.2766]
    axs[1].bar(range(3), e2, color=[colors[n] for n in names], width=.65)
    axs[1].set_xticks(range(3), ["SQLite", "DuckDB", "PG"])
    axs[1].set_ylim(0, 110); axs[1].set_title("E2: placement", pad=3)
    axs[1].set_ylabel("max controlled ratio", labelpad=1.5)

    qlabels = [r"$Q_{all}$", r"$Q_{low}$", r"$Q_{high}$"]
    axs[2].bar([x - .18 for x in range(3)], [1, 50.5, 1.980198], width=.36,
               label="M1 global", color="#b44d4d")
    axs[2].bar([x + .18 for x in range(3)], [1, 1, 1], width=.36,
               label="M2 conditioned", color="#4b8b5a")
    axs[2].set_yscale("log"); axs[2].set_xticks(range(3), qlabels)
    axs[2].set_ylim(top=400)  # headroom so the tall bar clears the legend
    axs[2].set_title("E3: query conditioning", pad=3); axs[2].set_ylabel("work Q-error", labelpad=1.5)
    axs[2].legend(frameon=False, handlelength=1.4, handletextpad=0.4,
                  labelspacing=0.25, borderpad=0.0)

    hs = [20, 100, 1000]
    axs[3].plot(hs, [2, 10, 100], marker="o", label="true work ratio", color="#4b318f")
    axs[3].plot(hs, [1.333333, 1.818182, 1.980198], marker="s", label="M1 POS Q-error", color="#b44d4d")
    axs[3].plot(hs, [1.5, 5.5, 50.5], marker="^", label="M1 NEG Q-error", color="#d9862c")
    axs[3].plot(hs, [1, 1, 1], marker=".", label="M2", color="#4b8b5a")
    axs[3].set_xscale("log"); axs[3].set_yscale("log")
    axs[3].set_title("E4: joint dependence", pad=3); axs[3].set_xlabel("high bound H", labelpad=1.5)
    axs[3].set_ylabel("ratio / work Q-error", labelpad=1.5)
    axs[3].legend(frameon=False, fontsize=7.5, handlelength=1.4,
                  handletextpad=0.4, labelspacing=0.25, borderpad=0.0)
    for ax in axs:
        ax.grid(axis="y", color="#dddddd", linewidth=.5)
        ax.tick_params(pad=1.5)
    fig.tight_layout(pad=0.25, w_pad=0.5)
    fig.savefig(OUT / "controlled_progression.pdf", bbox_inches="tight")
    plt.close(fig)


def native_chain():
    fig, ax = plt.subplots(figsize=(7.25, 2.2))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    labels = ["SQL-function\nbody", "Native\ninlining", "Relational predicates,\njoins, cardinalities",
              "PostgreSQL\nplanner", "Path / pushdown /\njoin decision", "Measured\nexecution"]
    xs = [0.01, .17, .33, .51, .67, .84]
    widths = [.13, .13, .15, .13, .145, .145]
    for x, w, label in zip(xs, widths, labels): box(ax, x, .58, w, .25, label, color="#e7f4ea", edge="#3f7650")
    for x, w, nx in zip(xs[:-1], widths[:-1], xs[1:]):
        ax.add_patch(FancyArrowPatch((x+w, .705), (nx, .705), arrowstyle="-|>", mutation_scale=9, lw=1))
    ax.text(.5, .39, "Observed chain (natural decisions): 2 of 3 cases changed", ha="center", weight="bold")
    ax.text(.18, .20, "Controlled PK\nSeq Scan → Index Only Scan\n114.375 → 0.232 ms (stress case)", ha="center", va="center", fontsize=7.5)
    ax.text(.50, .20, "Q14 clean-date\nsame physical shape\n748.896 → 498.851 ms", ha="center", va="center", fontsize=7.5)
    ax.text(.82, .20, "Q14 grounded\nFunction Scan removed; joins/pushdown\n78.234 → 34.056 ms", ha="center", va="center", fontsize=7.5)
    fig.tight_layout(pad=.2)
    fig.savefig(OUT / "native_inlining_chain.pdf", bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    style()
    representation_chain()
    controlled_progression()
    native_chain()
