# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""How many J.83 + shaping threads sdr_cmts.synth_threads chooses: one per 2 channels from 4 channels on,
one per channel when the host has the channels (of every B210) + 4 physical cores; never fewer than
--synth-threads; physical_cores counts each core once."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sdr_cmts  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


real = sdr_cmts.physical_cores()
check("cœurs physiques lus", 1 <= real <= (os.cpu_count() or 1), f"{real} sur {os.cpu_count()} CPU")


def threads(cores, *args):
    sdr_cmts.physical_cores = lambda: cores
    a = sdr_cmts.parser().parse_args(list(args))
    return sdr_cmts.synth_threads(a, sdr_cmts.radio_channels(a))

for name, cores, args, want in (
        ("i5-9500, 6 cœurs, 6 DS : un fil pour 2 canaux", 6, ["--ds-channels", "6"], 3),
        ("Xeon E5-2630 v4, 10 cœurs, 6 DS : un fil par canal", 10, ["--ds-channels", "6"], 6),
        ("10 cœurs, 4 DS : un fil par canal", 10, ["--ds-channels", "4"], 4),
        ("4 cœurs, 4 DS : 2 fils", 4, ["--ds-channels", "4"], 2),
        ("3 DS : le défaut (2)", 16, ["--ds-channels", "3"], 2),
        ("--synth-threads 5 au-dessus de la règle : gardé", 6, ["--ds-channels", "6", "--synth-threads", "5"], 5),
        ("2 B210 × 6 DS, 10 cœurs : un fil pour 2 canaux", 10, ["--ds-channels", "12", "--ds-radios", "2"], 3),
        ("2 B210 × 6 DS, 16 cœurs : un fil par canal", 16, ["--ds-channels", "12", "--ds-radios", "2"], 6)):
    got = threads(cores, *args)
    check(name, got == want, f"{got} (attendu {want})")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
