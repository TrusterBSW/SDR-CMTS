# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Where --irq-herd puts the other interrupts (sdr_cmts.irq_target): the other hyperthread of the
CPU taking the B210's USB interrupts; without SMT (i5-9500), that CPU itself, never a CMTS core;
nothing when the USB CPU is unknown."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sdr_cmts  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


for name, smt, usb, want in (("hyperthreading : le jumeau du CPU USB", {0, 6}, {0}, {6}),
                             ("sans hyperthreading : le CPU USB lui-même", {0}, {0}, {0}),
                             ("CPU USB inconnu : rien", set(), set(), set())):
    sdr_cmts.usb_irq_cpus = lambda siblings=True, smt=smt, usb=usb: smt if siblings else usb
    got = sdr_cmts.irq_target()
    check(name, got == want, f"{sorted(got)}")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
