#!/usr/bin/env python3
"""Chart helper: consistent, accessible figures for the design docs.

Import this rather than configuring matplotlib per script, so every figure in
the repo shares one visual language and the accessibility rules hold by
construction instead of by memory.

    from plot import figure, PALETTE, save

    fig, ax = figure("Sustained brightness vs module count",
                     xlabel="modules", ylabel="brightness (%)")
    ax.plot(x, y, color=PALETTE[0], label="36 V / 180 W")
    ax.legend()
    save(fig, "designs/led-matrix-controller/figures/brightness")

`save` writes BOTH light and dark SVG. Dark is re-stepped from the same hues
against the dark surface, not an automatic inversion.

Rules this enforces, from the dataviz method:

- **Categorical hues in fixed order, never cycled.** PALETTE is ordered; take
  slots 0,1,2… A ninth series is not a new hue - fold it into "other" or facet.
- **One y-axis, ever.** Two measures of different scale go in two figures or a
  small-multiple grid. A dual-axis chart is the single most common chart error:
  the crossing point is an artefact of the two scales and means nothing.
- **Recessive grid and axes**, thin marks, no chartjunk.
- **A legend whenever there are two or more series**, so identity is never
  carried by colour alone.
"""

from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

# Categorical slots, validated for CVD separation on the adjacent pairlist.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
           "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
                "#d55181", "#008300", "#9085e9", "#e66767"]

_LIGHT = {"surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e",
          "grid": "#dcdcd8"}
_DARK = {"surface": "#1a1a19", "text": "#ffffff", "muted": "#c3c2b7",
         "grid": "#3a3a38"}


def _style(theme: dict) -> None:
    plt.rcParams.update({
        # Without a fixed salt matplotlib names its clip paths from a random
        # id, so re-running figures.py rewrites every SVG whether or not a
        # number moved -- and a figure commit stops saying anything.
        "svg.hashsalt": "pcb-design",
        "figure.facecolor": theme["surface"],
        "axes.facecolor": theme["surface"],
        "savefig.facecolor": theme["surface"],
        "text.color": theme["text"],
        "axes.labelcolor": theme["muted"],
        "axes.edgecolor": theme["grid"],
        "xtick.color": theme["muted"],
        "ytick.color": theme["muted"],
        "grid.color": theme["grid"],
        "axes.titlecolor": theme["text"],
        "font.size": 10,
        "axes.titlesize": 12,
        "axes.titleweight": "medium",
        "axes.titlelocation": "left",
        "axes.titlepad": 14,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.linewidth": 0.6,
        "grid.alpha": 0.7,
        "lines.linewidth": 2.0,
        "lines.markersize": 5,
        "legend.frameon": False,
        "figure.autolayout": True,
    })


def figure(title: str, xlabel: str = "", ylabel: str = "",
           size=(7.0, 4.2), dark: bool = False):
    """A styled figure and axis. Call once per chart."""
    _style(_DARK if dark else _LIGHT)
    fig, ax = plt.subplots(figsize=size)
    ax.set_title(title)
    if xlabel:
        ax.set_xlabel(xlabel)
    if ylabel:
        ax.set_ylabel(ylabel)
    ax.set_axisbelow(True)
    return fig, ax


def annotate(ax, x, y, text, color=None, dx=6, dy=6):
    """Direct label on a point. Use selectively — never one per point."""
    ax.annotate(text, (x, y), textcoords="offset points", xytext=(dx, dy),
                fontsize=9, color=color or plt.rcParams["text.color"])


def limit_line(ax, y, text, color=None, side="right"):
    """A horizontal reference line for a constraint (a rating, a target).

    `side` places the label left or right; pick whichever edge the legend and
    the data are not using. Label collisions are the usual failure of reference
    lines and they are only visible once rendered, so look at the output.
    """
    c = color or plt.rcParams["grid.color"]
    ax.axhline(y, color=c, linestyle="--", linewidth=1.2, zorder=0)
    x = ax.get_xlim()[1] if side == "right" else ax.get_xlim()[0]
    ax.annotate(text, (x, y), textcoords="offset points",
                xytext=(-4 if side == "right" else 4, 4),
                ha=side, fontsize=9,
                color=plt.rcParams["axes.labelcolor"])


def save(fig, stem: str, also_png: bool = True) -> list:
    """Write <stem>.svg. Pass the same stem to build() for the dark variant."""
    out = Path(stem)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = []
    svg = out.with_suffix(".svg")
    fig.savefig(svg, format="svg")
    written.append(svg)
    if also_png:
        png = out.with_suffix(".png")
        fig.savefig(png, format="png", dpi=160)
        written.append(png)
    plt.close(fig)
    return written


def build(draw, stem: str) -> list:
    """Render `draw(ax, palette)` in both light and dark.

    `draw` receives the axis and the mode-appropriate palette, so series colours
    come from the right steps rather than being flipped after the fact.
    """
    written = []
    for dark, suffix, pal in ((False, "", PALETTE), (True, "-dark", PALETTE_DARK)):
        fig, ax = figure("", dark=dark)
        draw(ax, pal)
        written += save(fig, f"{stem}{suffix}")
    return written


if __name__ == "__main__":
    print(__doc__)
