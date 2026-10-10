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

    # two upstream channels (DOCSIS 3.0 upstream bonding): one wideband stream, channel 1 at
    # -3.5 MHz and channel 2 at +3.5 MHz from its centre, the same real burst on both at the same
    # time (plus a copy of the noise); one process per channel shares the ring. Rings: 27.808 MS/s
    # (master clock, 1-3 DS) and 55.616 / 3 = 18.539 MS/s (5-6 DS: the ring at a third of the
    # master clock, mini_cmts.build_us_bonding)
    from scipy.signal import resample_poly
    from cmts.usproc import SampleRing
    for wmcr, wdec, up_p, up_q, factor in ((mcr, 1, 2, 1, 2), (55.616e6, 3, 4, 3, 1)):
        wfs = wmcr / wdec
        wtb = Timebase(wmcr, 0, 8)
        ring = SampleRing(wfs, wdec, history_s=1.0)
        got2 = {1: [], 2: []}
        done2 = {1: threading.Event(), 2: threading.Event()}

        def on_ch(ch):
            def f(ev):
                got2[ch].append(ev)
                if ev.burst is not None:
                    done2[ch].set()
            return f
        offs = {1: -3.5e6, 2: +3.5e6}
        rxs = {ch: RemoteUpstream(wfs, wdec, 5.12e6, wtb, 256, on_ch(ch), "atdma", 6, log=logs.append, ring=ring,
                                  ddc=dict(offset=offs[ch], factor=factor, cutoff=4.2e6), name=f"upstream{ch}")
               for ch in (1, 2)}
        for r in rxs.values():
            r.start()
        up = resample_poly(stream, up_p, up_q).astype(np.complex64)
        n = np.arange(up.size)
        wide = sum(up * np.exp(2j * np.pi * offs[ch] / wfs * n).astype(np.complex64) for ch in (1, 2))
        wide = wide.astype(np.complex64)
        for r in rxs.values():                          # same DOCSIS times: both time bases start at 0
            r.add_grants([(int(round(q0)), int(round(q1)), mac.IUC_ADV_LONG, 1),
                          (int(round(start_ts)), int(round(end_ts)), mac.IUC_ADV_LONG, 1)])
        for i in range(0, wide.size, 40_000):
            rxs[1].push(i * wdec, wide[i:i + 40_000])   # once: the ring is shared
        for ch in (1, 2):
            done2[ch].wait(15)
        for ch in (1, 2):
            bb = [e for e in got2[ch] if e.burst is not None]
            b2 = bb[0].burst if bb else None
            check(f"2 canaux montants, anneau {wfs / 1e6:.3f} MS/s : canal {ch} ({offs[ch] / 1e6:+.1f} MHz) "
                  f"extrait (DDC) et décodé", b2 is not None and b2.frame == b.frame,
                  f"MER {b2.mer:.1f} dB (seul : {b.mer:.1f}), arrivée {bb[0].err_counts:+.2f} comptes "
                  f"(seul : {bursts[0].err_counts:+.2f})" if b2 is not None and b2.frame else str(got2[ch]))
        for r in rxs.values():
            r.stop()
        ring.close()
    print("PASS" if ok_all else "FAIL")
    sys.exit(0 if ok_all else 1)
