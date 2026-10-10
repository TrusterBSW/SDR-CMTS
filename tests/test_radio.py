"""--radio-process (cmts/radio.py), without the B210:
  - PacketRing: TS blocks written here arrive intact and in order in a spawned process;
  - SampleRing opened by name in another process: what that process pushes is seen here, with
    rx_tick0 / written read from the shared control words;
  - TxChain gives the same samples as the single-process transmit chain of mini_cmts.py
    (1 channel: mux.shape, scale, clip; 2-3 channels: ChannelSynth, helper threads, scale_clip)."""
import multiprocessing as mp
import os
import sys
import threading
import zlib

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import cdsp                                   # noqa: E402
from cmts.downstream import DownstreamMux, SecondaryMux, SYMRATE   # noqa: E402
from cmts.j83a import J83AEncoder, PulseShaper         # noqa: E402
from cmts.radio import PacketRing, TxChain              # noqa: E402
from cmts.usproc import SampleRing                      # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def consumer(handle, shape, n, q):
    ring = PacketRing(*shape, attach=handle)
    out = []
    while len(out) < n:
        b = ring.get(timeout=5)
        if b is None:
            break
        out.append(zlib.crc32(b.tobytes()))
    ring.close()
    q.put(out)


def pusher(attach, fs, decim, q):
    r = SampleRing(fs, decim, attach=attach)
    for k in range(5):
        r.push(1000 + k * 100 * decim, np.full(100, k + 1, np.complex64))
    r.detach()
    q.put("ok")


if __name__ == "__main__":
    ctx = mp.get_context("spawn")
    # ---------------------------------------------------------------- PacketRing
    shape = (3, 64, 2)
    ring = PacketRing(*shape, ctx=ctx)
    q = ctx.Queue()
    proc = ctx.Process(target=consumer, args=(ring.handle(), shape, 40, q))
    proc.start()
    rng = np.random.default_rng(3)
    sent = []
    stop = threading.Event()
    for _ in range(40):
        pks = [rng.integers(0, 256, (64, 188), dtype=np.uint8) for _ in range(3)]
        ring.put(pks, stop)
        sent.append(zlib.crc32(np.stack(pks).tobytes()))
    got = q.get(timeout=20)
    proc.join(5)
    ring.close()
    check("PacketRing : 40 blocs de 3 canaux, intacts et dans l'ordre (2 emplacements)", got == sent,
          f"{sum(a == b for a, b in zip(got, sent))}/40")

    # ---------------------------------------------------------------- SampleRing attach
    sr = SampleRing(1e6, 2, history_s=0.01)
    proc = ctx.Process(target=pusher, args=((sr.shm.name, sr.ctl_shm.name, sr.buf.size), 1e6, 2, q))
    proc.start()
    q.get(timeout=20)
    proc.join(5)
    check("SampleRing ouvert ailleurs : rx_tick0 et written partagés", sr.rx_tick0 == 1000 and sr.written == 500,
          f"{sr.rx_tick0} {sr.written}")
    check("SampleRing ouvert ailleurs : échantillons visibles ici",
          np.array_equal(sr.buf[:500].real, np.repeat(np.arange(1, 6), 100).astype(np.float32)))
    sr.close()

    # ---------------------------------------------------------------- TxChain == single process
    for nch in (1, 2, 3):
        sps = 2 if nch == 1 else 4
        fs_tx = sps * SYMRATE
        freqs = [306e6 + 8e6 * k for k in range(nch)]
        center = sum(freqs) / nch
        mux = DownstreamMux(qam=256, sps=sps)
        secs = [SecondaryMux(qam=256) for _ in range(nch - 1)]
        blocks = [[mux.packets(64)] + [m.packets(64) for m in secs] for _ in range(6)]
        steps = [2 * np.pi * (f - center) / fs_tx for f in freqs]
        p = dict(qams=[256] * nch, steps=steps, sps=sps, invert=True, scale=0.3, synth_threads=2)
        stop = threading.Event()
        chain = TxChain(p, stop)
        got = np.concatenate([chain(np.stack(b)) for b in blocks])
        stop.set()
        # the single-process loop of mini_cmts.py (encoder + shaper threads), on the same packets
        encs = [J83AEncoder(256) for _ in range(nch)]
        ref = []
        shaper = PulseShaper(sps)
        synths = [cdsp.ChannelSynth(PulseShaper(sps).phases, steps[k], True) for k in range(nch)]
        for b in blocks:
            syms = [encs[k].encode(b[k]) for k in range(nch)]
            if nch == 1:
                x = np.conj(shaper.process(syms[0])) * 0.3
                np.clip(x.real, -1, 1, out=x.real)
                np.clip(x.imag, -1, 1, out=x.imag)
            else:
                x = np.zeros(syms[0].size * sps, np.complex64)
                for k in range(nch):
                    acc = np.zeros_like(x)
                    synths[k].add(syms[k], acc)
                    cdsp.add_into(x, acc)
                cdsp.scale_clip(x, 0.3 / np.sqrt(nch))
            ref.append(x)
        ref = np.concatenate(ref)
        err = np.max(np.abs(got - ref)) / np.max(np.abs(ref))
        check(f"TxChain {nch} canal(aux) = chaîne d'un seul processus", got.size == ref.size and err < 1e-5,
              f"{got.size} échantillons, écart max relatif {err:.1e}")
    print("PASS" if ok_all else "FAIL")
    sys.exit(0 if ok_all else 1)
