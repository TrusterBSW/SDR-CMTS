# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""What the CMTS changes on the host for a run, and how it is put back, whatever the end.

For a run (--performance, --irq-herd) the CMTS sets the CPU governor and energy/performance
preference to "performance" and moves the interrupts away from its cores. What it changed is
written here BEFORE any change, and set back to the KERNEL'S DEFAULTS (the boot governor, the
firmware's energy preference, /proc/irq/default_smp_affinity; the values found at the start only
when a default cannot be found):
  - when it stops (atexit);
  - by `sdr-cmts --restore-host`, which the systemd service runs after every stop (ExecStopPost),
    a SIGKILL or a crash included;
  - at the next start, if a run killed before its own restore left them.
(The deep C-state block, /dev/cpu_dma_latency, needs nothing: the kernel drops it with the process
that holds it.)

The file lives in /run (tmpfs: gone at reboot, when the kernel defaults are back anyway) and
carries the boot ID, so a stale one is never applied.
"""
import gzip
import json
import os
import platform
import re
import subprocess


def path():
    if os.access("/run", os.W_OK):
        return "/run/sdr-cmts-host-state.json"
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"sdr-cmts-host-state-{os.getuid()}.json")


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def load():
    try:
        with open(path()) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    return st if st.get("boot_id") == _boot_id() else None


def remember(kind, values):
    """Keeps the original {sysfs path: value} of one kind of setting ("cpufreq", "irq"), only for the
    paths not remembered yet: after a crash the first originals stay the originals."""
    st = load() or {"boot_id": _boot_id(), "cpufreq": {}, "irq": {}}
    known = st.setdefault(kind, {})
    for p, v in values.items():
        known.setdefault(p, v)
    tmp = path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path())


def write(values):
    """{path: value} written as root (sudo tee when not root), one call per distinct value
    -> None, or the error text."""
    by_val = {}
    for p, v in values.items():
        by_val.setdefault(v, []).append(p)
    err = None
    for v, paths in by_val.items():
        cmd = ["tee", *paths] if os.geteuid() == 0 else ["sudo", "-n", "tee", *paths]
        r = subprocess.run(cmd, input=v + "\n", text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode:
            # one IRQ refusing (managed by the kernel) must not keep the others back
            for p in paths if len(paths) > 1 else []:
                subprocess.run(cmd[:-len(paths)] + [p], input=v + "\n", text=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            err = r.stderr.strip() or f"tee : code {r.returncode}"
    return err


GOVERNORS = {"PERFORMANCE": "performance", "POWERSAVE": "powersave", "SCHEDUTIL": "schedutil",
             "ONDEMAND": "ondemand", "CONSERVATIVE": "conservative", "USERSPACE": "userspace"}


def kernel_default_governor():
    """The governor the kernel boots with: cpufreq.default_governor= on its command line, else its
    configuration (CONFIG_CPU_FREQ_DEFAULT_GOV_*); None if neither can be read."""
    try:
        with open("/proc/cmdline") as f:
            m = re.search(r"cpufreq\.default_governor=(\w+)", f.read())
        if m:
            return m.group(1)
    except OSError:
        pass
    rel = platform.release()
    for p in ("/proc/config.gz", f"/boot/config-{rel}", f"/lib/modules/{rel}/config", f"/usr/lib/modules/{rel}/config"):
        try:
            text = (gzip.open(p, "rt") if p.endswith(".gz") else open(p)).read()
        except OSError:
            continue
        m = re.search(r"^CONFIG_CPU_FREQ_DEFAULT_GOV_(\w+)=y", text, re.M)
        if m:
            return GOVERNORS.get(m.group(1))
    return None


def _first(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def defaults(remembered):
    """The kernel's defaults for the remembered {path: value}; a path whose default cannot be found
    keeps its remembered value."""
    out = dict(remembered)
    gov = kernel_default_governor()
    for p in remembered:
        if p.endswith("scaling_governor") and gov:
            avail = _first(p.replace("scaling_governor", "scaling_available_governors")).split()
            driver = _first(p.replace("scaling_governor", "scaling_driver"))
            if gov in avail:
                out[p] = gov
            elif "powersave" in avail and driver.startswith(("intel_pstate", "amd-pstate")):
                out[p] = "powersave"            # these drivers' own governors (no schedutil in active mode)
    for p in remembered:
        if p.endswith("energy_performance_preference"):
            g = out.get(p.replace("energy_performance_preference", "scaling_governor"))
            if g and g != "performance":
                out[p] = "default"              # the firmware's value (with "performance" the driver forces it)
            else:
                out.pop(p)                      # left to the driver
        elif "/proc/irq/" in p:
            mask = _first("/proc/irq/default_smp_affinity")
            if mask:
                out.pop(p)
                out[p.replace("smp_affinity_list", "smp_affinity")] = mask
    return out


def restore(why=""):
    """Sets every remembered setting back to the kernel's default (defaults()) and forgets them -> a line for the log, None if there
    was nothing to put back."""
    st = load()
    if not st:
        try:
            os.unlink(path())                     # another boot's file
        except OSError:
            pass
        return None
    done, errors = [], []
    cpufreq = defaults(st.get("cpufreq", {}))
    # the governor first: setting "performance" also forces the EPP, put back after it
    gov = {p: v for p, v in cpufreq.items() if p.endswith("scaling_governor")}
    epp = {p: v for p, v in cpufreq.items() if not p.endswith("scaling_governor")}
    for name, part in (("gouverneur", gov), ("préférence d'énergie", epp)):
        if part:
            e = write(part)
            if e:
                errors.append(f"{name} : {e}")
            else:
                done.append(f"{name} {'/'.join(sorted(set(part.values())))}")
    irq = defaults(st.get("irq", {}))
    if irq:
        e = write(irq)
        done.append(f"{len(irq)} interruptions" + (" (certaines refusées)" if e else ""))
    try:
        os.unlink(path())
    except OSError:
        pass
    return ("réglages de l'hôte remis aux défauts du noyau" + (f" ({why})" if why else "") + " : " + ", ".join(done)
            + (f" ; !! {'; '.join(errors)}" if errors else ""))
