# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Where the TX LO (and its leakage) sits (sdr_cmts.tx_center_for): between two channels for an
even count; moved by half a channel for an odd count when the samples hold every channel (5 DS
at 55.6 MS/s: 310 MHz, not the primary's 306); left in place when they would not (3 DS at
27.8 MS/s) and for a single channel (its chain does not shift it)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sdr_cmts  # noqa: E402
from cmts.downstream import SYMRATE  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def center(n, sps):
    a = sdr_cmts.parser().parse_args(["--ds-channels", str(n)])
    fr = [f for _, f in sdr_cmts.ds_channels(a)]
    c, moved = sdr_cmts.tx_center_for(fr, sps * SYMRATE)
    inside = [f for f in fr if abs(f - c) < 4e6 - 0.5e6]
    reach = max(abs(f - c) for f in fr) + 4e6
    return c / 1e6, moved, inside, reach <= sps * SYMRATE / 2


for n, sps, want, moved_w in ((5, 8, 310.0, True), (6, 8, 310.0, False), (4, 5, 310.0, False),
                              (2, 4, 310.0, False), (3, 4, 306.0, False), (1, 4, 306.0, False)):
    c, moved, inside, fits = center(n, sps)
    check(f"{n} DS à {sps * SYMRATE / 1e6:.1f} MS/s : OL à {want} MHz", c == want and moved == moved_w and fits,
          f"{c} MHz, dans un canal : {[f / 1e6 for f in inside]}")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
