#!/usr/bin/env python3
"""Build the E5-DC figure and summary tables from frozen results."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

STUDY_SOURCE = Path(__file__).resolve().parents[1]
HERE = Path(os.environ.get("STUDY_OUTPUT_DIR", STUDY_SOURCE)).resolve()
NORMALIZED = HERE / ("normalized" if (HERE / "normalized").exists() else "results")
FIGURES = HERE / "figures"
TABLES = HERE / "tables"

K_GRID = [100, 200, 400, 800, 1600]
PRIMARY_K = 400
PHASE = "confirmation"

COLOR_M1 = "#b44d4d"
COLOR_M2 = "#4b8b5a"


def figure_style() -> None:
    """Times-family, 12 pt, all-black text, matching the other paper figures."""
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "STIXGeneral", "Nimbus Roman",
                       "Times", "DejaVu Serif"],
        "mathtext.fontset": "stix",
        "font.size": 12,
        "axes.labelsize": 12,
        "axes.titlesize": 12,
        "legend.fontsize": 12,
        "xtick.labelsize": 12,
        "ytick.labelsize": 12,
        "text.color": "black",
        "axes.labelcolor": "black",
        "axes.edgecolor": "black",
        "xtick.color": "black",
        "ytick.color": "black",
        "pdf.fonttype": 42,
    })


def load_summary() -> dict[tuple[str, int, str, str], dict[str, Any]]:
    with (NORMALIZED / "e5dc_cell_summary.csv").open(encoding="utf-8") as handle:
        return {(r["phase"], int(r["K"]), r["segment"], r["representation"]): r
                for r in csv.DictReader(handle)}


def build_figure(summary: dict[tuple[str, int, str, str], dict[str, Any]]) -> None:
    figure_style()
    fig, axs = plt.subplots(1, 2, figsize=(7.25, 2.5))

    calls = {rep: [int(summary[(PHASE, k, "COSTLY", rep)]["udf_calls"]) for k in K_GRID]
             for rep in ("M1", "M2")}
    med = {rep: [float(summary[(PHASE, k, "COSTLY", rep)]["median_ms"]) for k in K_GRID]
           for rep in ("M1", "M2")}
    lo = {rep: [float(summary[(PHASE, k, "COSTLY", rep)]["ci_low_ms"]) for k in K_GRID]
          for rep in ("M1", "M2")}
    hi = {rep: [float(summary[(PHASE, k, "COSTLY", rep)]["ci_high_ms"]) for k in K_GRID]
          for rep in ("M1", "M2")}

    labels = {"M1": "M1 collapsed", "M2": "M2 conditioned"}
    colors = {"M1": COLOR_M1, "M2": COLOR_M2}
    markers = {"M1": "o", "M2": "s"}

    for rep in ("M1", "M2"):
        axs[0].plot(K_GRID, calls[rep], marker=markers[rep], color=colors[rep],
                    lw=1.4, ms=4, label=labels[rep])
        err = [[m - l for m, l in zip(med[rep], lo[rep])],
               [h - m for m, h in zip(med[rep], hi[rep])]]
        axs[1].errorbar(K_GRID, med[rep], yerr=err, marker=markers[rep],
                        color=colors[rep], lw=1.4, ms=4, capsize=2, label=labels[rep])

    for ax, ylabel in ((axs[0], "UDF invocations"), (axs[1], "median runtime (ms)")):
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_xticks(K_GRID, [str(k) for k in K_GRID])
        ax.set_xlabel("work-to-cost factor K", labelpad=2)
        ax.set_ylabel(ylabel, labelpad=2)
        ax.grid(axis="y", color="#dddddd", linewidth=0.5)
        ax.tick_params(pad=1.5)
        ax.axvline(PRIMARY_K, color="#999999", lw=0.8, ls="--", zorder=0)
    axs[0].legend(frameon=False, handlelength=1.4, handletextpad=0.4,
                  labelspacing=0.25, borderpad=0.0, loc="center left")

    fig.tight_layout(pad=0.25, w_pad=0.6)
    for suffix in ("pdf", "svg", "png"):
        fig.savefig(FIGURES / f"e5dc_decision_consequence.{suffix}", dpi=300,
                    bbox_inches="tight")
    plt.close(fig)


def build_tables(summary: dict[tuple[str, int, str, str], dict[str, Any]]) -> None:
    rows = []
    for phase in ("calibration", "confirmation", "heldout_scale"):
        for segment in ("COSTLY", "CHEAP"):
            for k in K_GRID:
                a = summary[(phase, k, segment, "M1")]
                b = summary[(phase, k, segment, "M2")]
                m1, m2 = float(a["median_ms"]), float(b["median_ms"])
                c1, c2 = int(a["udf_calls"]), int(b["udf_calls"])
                disjoint = (float(a["ci_high_ms"]) < float(b["ci_low_ms"])
                            or float(b["ci_high_ms"]) < float(a["ci_low_ms"]))
                rows.append({
                    "phase": phase, "K": k, "segment": segment,
                    "m1_join_methods": a["join_methods"], "m2_join_methods": b["join_methods"],
                    "m1_udf_calls": c1, "m2_udf_calls": c2,
                    "udf_call_ratio": round(max(c1, c2) / min(c1, c2), 2),
                    "m1_median_ms": round(m1, 3), "m2_median_ms": round(m2, 3),
                    "runtime_ratio": round(max(m1, m2) / min(m1, m2), 3),
                    "m1_ci_low_ms": round(float(a["ci_low_ms"]), 3),
                    "m1_ci_high_ms": round(float(a["ci_high_ms"]), 3),
                    "m2_ci_low_ms": round(float(b["ci_low_ms"]), 3),
                    "m2_ci_high_ms": round(float(b["ci_high_ms"]), 3),
                    "m1_cv": round(float(a["cv"]), 4), "m2_cv": round(float(b["cv"]), 4),
                    "ci_disjoint": disjoint,
                })
    path = TABLES / "e5dc_decision_consequence_table.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader(); writer.writerows(rows)

    lines = [
        r"\begin{table}[t]", r"\centering", r"\small",
        r"\caption{E5-DC confirmation run (held-out seed 41). The collapsed "
        r"representation M1 drives the join from the fact table; the conditioned "
        r"representation M2 drives it from the selective customer predicate. "
        r"Results are byte-identical in every cell.}",
        r"\label{tab:e5dc}",
        r"\begin{tabular}{rlrrrrr}", r"\toprule",
        r"$K$ & segment & M1 calls & M2 calls & ratio & M1 ms & M2 ms \\", r"\midrule",
    ]
    for segment in ("COSTLY", "CHEAP"):
        for k in K_GRID:
            a = summary[("confirmation", k, segment, "M1")]
            b = summary[("confirmation", k, segment, "M2")]
            c1, c2 = int(a["udf_calls"]), int(b["udf_calls"])
            lines.append(
                f"{k} & {segment} & {c1:,} & {c2:,} & "
                f"{max(c1, c2) / min(c1, c2):.1f}$\\times$ & "
                f"{float(a['median_ms']):.1f} & {float(b['median_ms']):.1f} \\\\")
        lines.append(r"\midrule")
    lines[-1] = r"\bottomrule"
    lines += [r"\end{tabular}", r"\end{table}"]
    (TABLES / "e5dc_decision_consequence_table.tex").write_text(
        "\n".join(lines).replace(",", "{,}").replace("{,}5", ",5"), encoding="utf-8")


def main() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    TABLES.mkdir(parents=True, exist_ok=True)
    summary = load_summary()
    build_figure(summary)
    build_tables(summary)
    print("figure and tables written")


if __name__ == "__main__":
    main()
