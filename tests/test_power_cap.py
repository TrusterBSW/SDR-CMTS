# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Upstream power commands to a modem in (or going to) Multiple Transmit Channel mode never above
Phi - Pload_min_set (PHYv3.0 6.2.18), from the transmit power it reports in its RNG-REQs:
  - Sagem 3686, 2026-11-11: single-channel ranging left it at 54.25 dBmV, over the 54 dBmV Phi of
    its 2-channel set: REG-ACK 210 (reject-dynamic-range-window-violation). Now brought to 54;
  - in MTC mode it ignored +12.25 dB past Phi and was asked again for ever: now asked up to Phi,
    then success at the cap;
  - one upstream channel, or no reported power: the plain correction toward the target."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import ranging                            # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


core = ranging.CmtsCore(None, log=lambda *a: None)
m = ranging.Modem(bytes(6), 1)
core.us_chans = [(1, 60e6)]
check("1 canal montant : pas de plafond", core._pr_cap(m) is None)
check("1 canal : correction vers la cible", core._power_step(12.0, 4 * 50, None) == (48, False))
core.us_chans = [(1, 60e6), (2, 53e6)]
check("2 canaux, fenêtre pas encore fixée : plafond Phi = 54", core._pr_cap(m) == 54.0)
step, ok = core._power_step(0.2, 4 * 54.25, core._pr_cap(m))
check("54,25 dBmV au niveau cible : ramené à 54, pas encore OK", (step, ok) == (-1, False), f"{step} {ok}")
step, ok = core._power_step(0.45, 4 * 54.0, core._pr_cap(m))
check("54 dBmV, 0,45 dB sous la cible : OK, aucune hausse", (step, ok) == (0, True), f"{step} {ok}")
m.drw_qdb = 0
step, ok = core._power_step(12.25, 4 * 42.75, core._pr_cap(m))
check("mode MTC, 42,75 dBmV, +12,25 voulus : +11,25 (jusqu'à Phi), pas encore OK", (step, ok) == (45, False),
      f"{step} {ok}")
step, ok = core._power_step(1.0 + 1.5, 4 * 54.0, core._pr_cap(m))
check("au plafond, encore 2,5 dB sous la cible : OK (success)", (step, ok) == (0, True), f"{step} {ok}")
m.drw_qdb = 16
check("Pload_min_set 4 dB : plafond 50", core._pr_cap(m) == 50.0)
step, ok = core._power_step(-0.5, 4 * 52.0, core._pr_cap(m))
check("52 dBmV au-dessus du plafond 50 : -2 dB, pas OK", (step, ok) == (-8, False), f"{step} {ok}")
check("sans puissance déclarée : correction simple", core._power_step(3.0, None, 50.0) == (12, False))
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
