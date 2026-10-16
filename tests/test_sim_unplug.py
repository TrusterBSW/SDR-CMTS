# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""The SDR unplugged under a running CMTS, with the simulated radio (cmts/sdrdev.py SimRadio,
SDR_CMTS_SDR_SIM): the radio process aborts as UHD did on 2026-10-16; the MAC must stop on its
own within seconds, log its stop line and leave with code 3 (sdr_cmts.RADIO_LOST, for systemd to
start it again), with no process and no shared memory left behind. The MAC used to hang in
RadioProcess.stop(): multiprocessing.Event.set() waits for a sleeper that had died."""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def shm():
    try:
        return {f for f in os.listdir("/dev/shm") if f.startswith("psm_")}
    except OSError:
        return set()


before = shm()
env = dict(os.environ, SDR_CMTS_SDR_SIM="1", SDR_CMTS_SIM_UNPLUG_S="8")
with tempfile.TemporaryDirectory() as d:
    t0 = time.monotonic()
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "sdr_cmts.py"), "--ds-channels", "1",
                          "--tap", "none", "--status-http", "none", "--no-performance", "--no-irq-herd",
                          "--rt-prio", "0", "--color", "never", "--log", "none"],
                         cwd=d, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = p.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        check("le CMTS s'arrête seul", False, "toujours là 60 s après (bloqué)")
dt = time.monotonic() - t0
if "/dev/shm trop petit" in out:               # a container with a small /dev/shm: nothing to test
    print("SKIP (/dev/shm trop petit pour les anneaux : --shm-size=1g)")
    sys.exit(0)
check("le CMTS s'arrête seul après la perte de la radio", p.returncode is not None and dt < 45, f"{dt:.0f} s")
check("code de sortie 3 (radio perdue) : systemd le relance", p.returncode == 3, f"code {p.returncode}")
check("débranchement vu et arrêt journalisé", "[SIM] débranchement" in out and "processus radio arrêté" in out
      and "arrêt;" in out)
time.sleep(1)
check("aucune mémoire partagée laissée", not (shm() - before), str(sorted(shm() - before))[:80])
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
