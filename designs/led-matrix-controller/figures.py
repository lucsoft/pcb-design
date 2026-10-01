#!/usr/bin/env python3
"""Regenerate the figures in designs/led-matrix-controller/figures/.

    nix-shell --run 'python3 designs/led-matrix-controller/figures.py'

Brightness and frame rate are deliberately separate charts. They are different
measures on different scales, so putting them on one plot with two y-axes would
make their crossing point an artefact of the scaling rather than a fact.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
from plot import figure, save, build, limit_line, annotate

FULL_W, EFF, CAP = 10.8, 0.75, 0.70      # per-module full-white W, buck eff, thermal cap
LEDS, US, RESET = 36, 30.0, 0.28         # per module; 24 bits @ 800 kbps; latch
CONN_A, V = 10.0, 36.0   # picoMAX 3.5 contact rating
AWG = {18: 0.0210, 20: 0.0333, 22: 0.0530, 24: 0.0842}   # ohm/m, one conductor
OUT = Path(__file__).resolve().parent / "figures"


def brightness(total, avail):
    return min(CAP, avail * EFF / (total * FULL_W))


def fps(per_chain):
    return 1000.0 / (per_chain * LEDS * US / 1000.0 + RESET)


def fig_brightness(ax, pal):
    xs = list(range(4, 41))
    for i, (avail, label) in enumerate(
            ((135.0, "28 V / 140 W"), (175.0, "36 V / 180 W  (chosen)"),
             (235.0, "48 V / 240 W  (kills modules)"))):
        ax.plot(xs, [brightness(x, avail) * 100 for x in xs],
                color=pal[i], label=label,
                linestyle="--" if avail > 200 else "-")
    ax.set_title("Sustained brightness falls with module count")
    ax.set_xlabel("modules on the controller")
    ax.set_ylabel("brightness (% of full white)")
    ax.set_ylim(0, 80)
    limit_line(ax, CAP * 100, "module thermal ceiling 70%", side="left")
    ax.axvline(20, color=pal[1], linewidth=1, alpha=0.35, zorder=0)
    annotate(ax, 20, 8, "  20 modules\n  (target)", color=pal[1])
    ax.legend(loc="center right")


def fig_fps(ax, pal):
    xs = [x / 2 for x in range(4, 61)]
    ax.plot(xs, [fps(x) for x in xs], color=pal[0], label="one data channel")
    ax.set_title("Frame rate depends only on modules per channel")
    ax.set_xlabel("modules per channel")
    ax.set_ylabel("frames per second")
    ax.set_ylim(0, 320)
    ax.set_xlim(2, 30)
    limit_line(ax, 100, "100 fps preferred", side="right")
    limit_line(ax, 60, "60 fps floor", side="right")
    ax.plot([10], [fps(10)], "o", color=pal[1], zorder=5)
    annotate(ax, 10, fps(10), "  10/channel = 90 fps", color=pal[1])


def fig_current(ax, pal):
    """Per-channel current against the picoMAX contact rating."""
    xs = list(range(4, 25))
    ax.plot(xs, [min(175.0 / 2, x / 2 * FULL_W * CAP / EFF) / V for x in xs],
            color=pal[0], label="per channel, 2 channels")
    ax.set_title("Per-channel current against the connector rating")
    ax.set_xlabel("modules on the controller")
    ax.set_ylabel("current per output (A)")
    ax.set_ylim(0, 11)
    ax.set_xlim(4, 24)
    limit_line(ax, CONN_A, "picoMAX 3.5 contact rating 10 A", side="left")
    annotate(ax, 20, 2.43, "  20 modules: 2.43 A\n  = 24% of rating", color=pal[0])
    ax.legend(loc="upper left")


def fig_cable(ax, pal):
    """Drop in the controller-to-first-module cable.

    Modules sit adjacent on the wall and chain board-to-board, so only this one
    cable has length. It carries the whole channel current.
    """
    L = [x / 2 for x in range(1, 41)]
    for i, g in enumerate((18, 20, 22, 24)):
        rt = 2 * AWG[g]
        ax.plot(L, [2.43 * rt * x for x in L], color=pal[i], label=f"{g} AWG")
    ax.set_title("Drop in the controller-to-first-module cable (2.43 A)")
    ax.set_xlabel("cable length (m)")
    ax.set_ylabel("voltage drop (V)")
    ax.set_ylim(0, 4)
    ax.set_xlim(0, 20)
    limit_line(ax, 0.05 * V, "5% of 36 V", side="left")
    limit_line(ax, 0.02 * V, "2%", side="left")
    ax.legend(loc="upper left")


if __name__ == "__main__":
    written = []
    for fn, stem in ((fig_brightness, "brightness-vs-modules"),
                     (fig_fps, "fps-vs-modules-per-channel"),
                     (fig_current, "current-vs-connector-rating"),
                     (fig_cable, "cable-drop-vs-hop-length")):
        written += build(fn, str(OUT / stem))
    print(f"{len(written)} files written to {OUT}/")
    for w in sorted(set(p.name for p in written)):
        print(f"  {w}")
