"""Upstream receiver in its own process (cmts/usproc.py): samples pushed into the shared ring,
the grant sent through the queue, the decoded burst back through on_burst; also the processed
DOCSIS time (MAP Ack Time), the statistics, and a clean stop. Uses a real ATDMA 64-QAM burst of
the Sagem 3686 (5.12 Msym/s, 13.904 MS/s) placed in noise."""
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import docsis_mac as mac                 # noqa: E402
from cmts.upstream import Timebase                 # noqa: E402
from cmts.usproc import RemoteUpstream             # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


if __name__ == "__main__":
    mac.configure_upstream("atdma", 6)
    mcr, decim, fs = 27.808e6, 2, 13.904e6
    tb = Timebase(mcr, 0, 4)
    got, ev_done = [], threading.Event()

    def on_burst(ev):
        got.append(ev)
        if ev.burst is not None:
            ev_done.set()
    logs = []
    rx = RemoteUpstream(fs, decim, 5.12e6, tb, 256, on_burst, "atdma", 6, log=logs.append)
    rx.start()
    z = np.load(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "atdma64_5m12_long_1.npz"))
    rng = np.random.default_rng(0)
    O = 200_000                                         # window position in the stream (samples)
    total = O + z["iq"].size + 300_000
    stream = (rng.standard_normal(total) + 1j * rng.standard_normal(total)).astype(np.complex64) * 10 ** -3.2
    stream[O:O + z["iq"].size] = z["iq"]
    s0 = O + float(z["start_sample"])                   # grant start (sample)
    start_ts = tb.ts(s0 * decim)
    end_ts = tb.ts((s0 + float(z["grant_syms"]) * fs / 5.12e6) * decim)
    q0 = tb.ts(50_000 * decim)                          # a quiet grant first: noise floor
    q1 = tb.ts((50_000 + 34 * 348) * decim)
    rx.add_grants([(int(round(q0)), int(round(q1)), mac.IUC_ADV_LONG, 1),
                   (int(round(start_ts)), int(round(end_ts)), mac.IUC_ADV_LONG, 1)])
    t0 = time.time()
    for i in range(0, total, 20_000):                   # the RX thread's blocks
        rx.push(i * decim, stream[i:i + 20_000])
    ev_done.wait(15)
    dt = time.time() - t0
    bursts = [e for e in got if e.burst is not None]
    b = bursts[0].burst if bursts else None
    check("rafale reçue du processus montant et décodée",
          b is not None and b.frame is not None and b.info.get("fc") == mac.FC_CONCAT,
          f"{len(b.frame)} octets, MER {b.mer:.1f} dB, en {dt:.2f} s (démarrage du processus compris)" if b and b.frame else str(got))
    time.sleep(1.2)                                     # one statistics message at least
    check("heure traitée (MAP Ack Time) publiée", rx.processed_ts >= int(round(end_ts)), f"{rx.processed_ts} >= {int(round(end_ts))}")
    check("statistiques remontées", rx.stats.get("decoded", 0) >= 1 and rx.noise_dbfs() is not None,
          f"{rx.stats}, bruit {rx.noise_dbfs()}")
    rx.stop()
    check("arrêt propre du processus", not rx.alive())
    print("PASS" if ok_all else "FAIL")
    sys.exit(0 if ok_all else 1)
