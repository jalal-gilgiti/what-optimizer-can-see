"""Shared publication style for the Task 10 vector concept figures."""

from matplotlib import pyplot as plt
from matplotlib.patches import FancyBboxPatch


NAVY = "#274C66"
NAVY_LIGHT = "#E8F0F4"
OCHRE = "#9A6700"
OCHRE_LIGHT = "#FBF1D8"
INK = "#20262B"
MID = "#66727A"
GRID = "#AEB8BE"
LIGHT = "#F4F6F7"
WHITE = "#FFFFFF"


def apply_style() -> None:
    """Use restrained, embedded, print-safe typography."""
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 7.4,
            "mathtext.fontset": "dejavusans",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "axes.unicode_minus": False,
            "figure.facecolor": WHITE,
            "savefig.facecolor": WHITE,
        }
    )


def rounded_box(ax, xy, width, height, *, facecolor=WHITE, edgecolor=GRID,
                linewidth=0.8, linestyle="solid", radius=0.012, zorder=2):
    patch = FancyBboxPatch(
        xy,
        width,
        height,
        boxstyle=f"round,pad=0.006,rounding_size={radius}",
        facecolor=facecolor,
        edgecolor=edgecolor,
        linewidth=linewidth,
        linestyle=linestyle,
        zorder=zorder,
    )
    ax.add_patch(patch)
    return patch


def finish(fig, path) -> None:
    """Write a tightly cropped vector PDF with no rasterized artists."""
    fig.savefig(path, format="pdf", bbox_inches="tight", pad_inches=0.025)
    plt.close(fig)
