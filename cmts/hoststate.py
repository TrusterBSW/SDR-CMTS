# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""What the CMTS changes on the host for a run, and how it is put back, whatever the end.

For a run (--performance, --irq-herd) the CMTS sets the CPU governor and energy/performance
preference to "performance" and moves the interrupts away from its cores. The original values are
written here BEFORE any change, and put back:
  - when it stops (atexit);
  - by `mini-cmts --restore-host`, which the systemd service runs after every stop (ExecStopPost),
    a SIGKILL or a crash included;
  - at the next start, if a run killed before its own restore left them.
(The deep C-state block, /dev/cpu_dma_latency, needs nothing: the kernel drops it with the process
that holds it.)

The file lives in /run (tmpfs: gone at reboot, when the kernel defaults are back anyway) and
carries the boot ID, so a stale one is never applied.
"""
import json
import os
import subprocess


def path():
    if os.access("/run", os.W_OK):
        return "/run/mini-cmts-host-state.json"
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", f"mini-cmts-host-state-{os.getuid()}.json")


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


def restore(why=""):
    """Puts back every remembered setting and forgets them -> a line for the log, None if there
    was nothing to put back."""
    st = load()
    if not st:
        try:
            os.unlink(path())                     # another boot's file
        except OSError:
            pass
        return None
    done, errors = [], []
    cpufreq = st.get("cpufreq", {})
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
    irq = st.get("irq", {})
    if irq:
        e = write(irq)
        done.append(f"{len(irq)} interruptions" + (" (certaines refusées)" if e else ""))
    try:
        os.unlink(path())
    except OSError:
        pass
    return ("réglages de l'hôte rétablis" + (f" ({why})" if why else "") + " : " + ", ".join(done)
            + (f" ; !! {'; '.join(errors)}" if errors else ""))
