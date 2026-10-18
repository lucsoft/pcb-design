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

# Every constant comes from calc.yaml, which is also what README.md's numbers
# are checked against. Holding them here as well is how a figure and its
# caption come to disagree -- the figures were drawn from one copy of these
# values and the prose from another for most of this design's life.
from calc import values                                        # noqa: E402

Q = values("led-matrix-controller")                # SI base units throughout
FULL_W = Q["MODULE_FULL_W"]
EFF, CAP = Q["ETA_MODULE"], Q["CAP_THERMAL"]
LEDS = Q["LEDS_PER_MODULE"]
US, RESET = Q["T_PER_LED"] * 1e6, Q["T_RESET"] * 1e3           # us, ms
CONN_A, V = Q["CONN_RATING"], Q["V_BUS"]
AVAIL = Q["p_modules"]                             # 180 W less the controller
RHO_CU = Q["RHO_CU"] * 1e6                         # SI ohm*m to ohm*mm2/m
CSA = [Q["CSA_025"], Q["CSA_034"], Q["CSA_REF"], Q["CSA_ALT"], Q["CSA_100"]]
CSA = [a * 1e6 for a in CSA]                       # SI m2 to mm2
OUT = Path(__file__).resolve().parent / "figures"


def brightness(total, avail):
    return min(CAP, avail * EFF / (total * FULL_W))


def fps(per_chain):
    return 1000.0 / (per_chain * LEDS * US / 1000.0 + RESET)


def fig_brightness(ax, pal):
    xs = list(range(4, 41))
    for i, (avail, label) in enumerate(
            ((Q["p_modules_28v"], "28 V / 140 W"),
             (AVAIL, "36 V / 180 W  (chosen)"),
             (Q["p_modules_48v"], "48 V / 240 W  (kills modules)"))):
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
    ax.legend(loc="lower left", framealpha=0.9)


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
    # The LM5069's fault-timer ceiling at 12.7/channel is gone: the TPS16630
    # ramps under dV/dt control, so ramp time is independent of load capacitance.
    # The binding constraint is power, which this figure does not show.
    ax.plot([10], [fps(10)], "o", color=pal[1], zorder=5)
    annotate(ax, 10, fps(10), "10/channel\n= 90 fps", color=pal[1], dx=-78, dy=-56)


def fig_current(ax, pal):
    """Per-channel current against the picoMAX contact rating."""
    xs = list(range(4, 25))
    ax.plot(xs, [min(AVAIL / 2, x / 2 * FULL_W * CAP / EFF) / V for x in xs],
            color=pal[0], label="per channel, 2 channels")
    ax.set_title("Per-channel current against the connector rating")
    ax.set_xlabel("modules on the controller")
    ax.set_ylabel("current per output (A)")
    ax.set_ylim(0, 11)
    ax.set_xlim(4, 24)
    limit_line(ax, CONN_A, "picoMAX 3.5 contact rating 10 A", side="right")
    annotate(ax, 12, 3.2, "20 modules:\n2.43 A balanced = 24%\n2.80 A worst case = 28%", color=pal[0])
    ax.legend(loc="upper left")


def csa(a):
    """A cross-section as IEC 60228 writes it: 0.25, 0.5, 1.0 -- never 1."""
    out = f"{a:.2f}".rstrip("0")
    return out + "0" if out.endswith(".") else out


def fig_cable(ax, pal):
    """Drop in the controller-to-first-module cable.

    Modules sit adjacent on the wall and chain board-to-board, so only this one
    cable has length. It carries the whole channel current.
    """
    # 2.80 A, not the balanced 2.43 A: one channel at the thermal cap with the
    # other dark is the real per-channel worst case, and it is what decides the
    # length limit the text recommends.
    L = [x / 2 for x in range(1, 41)]
    for i, a in enumerate(CSA):
        rt = 2 * RHO_CU / a
        ax.plot(L, [Q["i_channel_worst"] * rt * x for x in L],
                color=pal[i], label=f"{csa(a)} mm\u00b2")
    ax.set_title("Drop in the controller-to-first-module cable "
                 f"({Q['i_channel_worst']:.2f} A worst case)")
    ax.set_xlabel("cable length (m)")
    ax.set_ylabel("voltage drop (V)")
    ax.set_ylim(0, 4)
    ax.set_xlim(0, 20)
    limit_line(ax, Q["v_drop_allowed"], "5% of 36 V", side="left")
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
