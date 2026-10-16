# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Host settings changed for a run (cmts/hoststate.py), on stand-in sysfs files: the originals are
kept before any change, a second run after a crash does not overwrite them, restore() puts every
one back (governor before the energy preference) and forgets them, a file from another boot is
never applied."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import hoststate  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


d = tempfile.mkdtemp()
state = os.path.join(d, "state.json")
hoststate.path = lambda: state
order = []


def write(values):
    for p, v in values.items():
        order.append(os.path.basename(p))
        with open(p, "w") as f:
            f.write(v + "\n")


hoststate.write = write
files = {k: os.path.join(d, k) for k in ("scaling_governor", "energy_performance_preference", "smp_affinity_list")}
for k, v in (("scaling_governor", "powersave"), ("energy_performance_preference", "balance_performance"),
             ("smp_affinity_list", "3")):
    write({files[k]: v})
order.clear()

hoststate.remember("cpufreq", {files["scaling_governor"]: "powersave",
                               files["energy_performance_preference"]: "balance_performance"})
hoststate.remember("irq", {files["smp_affinity_list"]: "3"})
write({files["scaling_governor"]: "performance", files["energy_performance_preference"]: "performance",
       files["smp_affinity_list"]: "0"})
# a run killed here; the next one remembers what it finds (already changed): must not win
hoststate.remember("cpufreq", {files["scaling_governor"]: "performance"})
hoststate.remember("irq", {files["smp_affinity_list"]: "0"})
order.clear()
msg = hoststate.restore("test")
vals = {k: open(p).read().strip() for k, p in files.items()}
check("les premiers originaux sont rétablis", vals == {"scaling_governor": "powersave",
                                                      "energy_performance_preference": "balance_performance",
                                                      "smp_affinity_list": "3"}, str(vals))
check("gouverneur avant la préférence d'énergie", order.index("scaling_governor") < order.index("energy_performance_preference"))
check("fichier d'état effacé ensuite", not os.path.exists(state) and hoststate.restore() is None, msg)
hoststate.remember("irq", {files["smp_affinity_list"]: "5"})
st = json.load(open(state))
st["boot_id"] = "un-autre-demarrage"
json.dump(st, open(state, "w"))
check("fichier d'un autre démarrage : jamais appliqué", hoststate.restore() is None
      and open(files["smp_affinity_list"]).read().strip() == "3" and not os.path.exists(state))
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
