# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Host settings changed for a run (cmts/hoststate.py), on stand-in sysfs files: what was changed is
written down before any change (a second run after a crash adds nothing over it), restore() sets
it back to the KERNEL'S DEFAULTS (the boot governor, or powersave for intel_pstate when the boot
one is not offered; the firmware's energy preference; /proc/irq/default_smp_affinity), the
governor before the energy preference, then forgets; a file from another boot is never applied;
without a known default, the value found at the start."""
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
cpu = os.path.join(d, "cpu0", "cpufreq")
irqd = os.path.join(d, "proc", "irq", "134")
os.makedirs(cpu)
os.makedirs(irqd)
state = os.path.join(d, "state.json")
hoststate.path = lambda: state
order = []


def write(values):
    for p, v in values.items():
        order.append(os.path.basename(p))
        with open(p, "w") as f:
            f.write(v + "\n")


def read(p):
    return open(p).read().strip()


hoststate.write = write
gov, epp = os.path.join(cpu, "scaling_governor"), os.path.join(cpu, "energy_performance_preference")
irq = os.path.join(irqd, "smp_affinity_list")
write({os.path.join(cpu, "scaling_available_governors"): "performance powersave",
       os.path.join(cpu, "scaling_driver"): "intel_pstate", gov: "powersave", epp: "balance_power", irq: "3"})
default_mask = open("/proc/irq/default_smp_affinity").read().strip()

hoststate.remember("cpufreq", {gov: "powersave", epp: "balance_power"})
hoststate.remember("irq", {irq: "3"})
write({gov: "performance", epp: "performance", irq: "0"})
hoststate.remember("cpufreq", {gov: "performance"})        # a run after a crash: adds nothing
order.clear()
hoststate.kernel_default_governor = lambda: "schedutil"   # Arch: not offered by intel_pstate
msg = hoststate.restore("test")
check("gouverneur : défaut du noyau (schedutil absent d'intel_pstate -> powersave)", read(gov) == "powersave", read(gov))
check("préférence d'énergie : celle du firmware (default)", read(epp) == "default", read(epp))
check("interruptions : affinité par défaut du noyau", read(os.path.join(irqd, "smp_affinity")) == default_mask,
      f"{read(os.path.join(irqd, 'smp_affinity'))} / {default_mask}")
check("gouverneur avant la préférence d'énergie", order.index("scaling_governor") < order.index("energy_performance_preference"))
check("fichier d'état effacé ensuite", not os.path.exists(state) and hoststate.restore() is None, msg)

write({gov: "performance", epp: "performance"})
hoststate.remember("cpufreq", {gov: "powersave", epp: "balance_power"})
hoststate.kernel_default_governor = lambda: "performance"  # Proxmox: boots in performance
order.clear()
hoststate.restore()
check("noyau qui démarre en performance : performance, préférence laissée au pilote",
      read(gov) == "performance" and "energy_performance_preference" not in order, str(order))

write({gov: "performance"})
hoststate.remember("cpufreq", {gov: "powersave"})
hoststate.kernel_default_governor = lambda: None
hoststate.restore()
check("défaut introuvable : la valeur trouvée au lancement", read(gov) == "powersave")

hoststate.remember("irq", {irq: "5"})
st = json.load(open(state))
st["boot_id"] = "un-autre-demarrage"
json.dump(st, open(state, "w"))
check("fichier d'un autre démarrage : jamais appliqué", hoststate.restore() is None and not os.path.exists(state))
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
