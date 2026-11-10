# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Side of the upstream RX LO (sdr_cmts.rx_lo_offset, lo_folds): above the channel unless its 5th
or (mirrored) 3rd harmonic would fold a downstream channel onto the upstream; then below, if the
LO stays >= 52 MHz. 5 DS around 306 MHz, upstream 60 MHz: 5 x 64.2 = 321 MHz folded 314 / 322
(MER 21 -> 17 dB on the Xeon, 2026-11-11), so 55.8 MHz; --us-lo-offset still wins."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sdr_cmts  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def lo(*args):
    a = sdr_cmts.parser().parse_args(list(args))
    o = sdr_cmts.rx_lo_offset(a)
    return a, (a.us_freq + o) / 1e6


a, f = lo("--ds-channels", "5")
check("5 DS autour de 306, US 60 : OL sous le canal", abs(f - 55.8) < 1e-6, f"{f} MHz")
check("au-dessus (64,2 MHz), 314 et 322 se replieraient", sorted(sdr_cmts.lo_folds(a, 64.2e6)) == [314e6, 322e6])
check("en dessous, rien ne se replie", sdr_cmts.lo_folds(a, 55.8e6) == [])
a, f = lo("--ds-channels", "1")
check("1 DS à 306 : rien ne se replie, OL au-dessus comme avant", abs(f - 64.2) < 1e-6, f"{f} MHz")
a, f = lo("--ds-channels", "5", "--us-freq", "40e6")
check("US 40 MHz : OL à 52 MHz au moins, au-dessus", abs(f - 52.0) < 1e-6, f"{f} MHz")
a, f = lo("--ds-channels", "5", "--us-lo-offset", "4.2e6")
check("--us-lo-offset imposé : gardé", abs(f - 64.2) < 1e-6, f"{f} MHz")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
