#!/usr/bin/env python3
"""Weak 256-QAM downstream (HackRF, MER ~25 dB: ~10 % of the bytes wrong, beyond Reed-Solomon)
-> the raw MPEG packets, NOT corrected, with a reliability per byte, for messages that are sent
again and again (UCD, MAP) or at every modem reboot (REG-RSP-MP, config file) and can be voted
byte by byte across copies (origin_vote.py).

Sync tracking tolerates the symbol slips of long slices: the byte offset of the sync bytes is
found again every 48 packets, and every run of constant offset is deinterleaved on its own.

  ./origin_raw.py rb.s8 --fmt s8 --fs 20e6 --center 282e6 --freq 282e6 --out rb   -> rb_raw.npz
"""
import argparse
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

import origin_listen as L
from cmts import j83a
from cmts import rx_j83a as rx

R = j83a.RS_LEN
WIN = 48                                  # packets per sync search window


def raw_packets(b, rel):
    """bytes (one per 256-QAM symbol) + reliabilities -> (packets (N,188) derandomized, their
    reliabilities (N,188), byte index of each packet's sync in b)."""
    W = R * WIN
    nw = b.size // W
    if nw < 2:
        return np.zeros((0, 188), np.uint8), np.zeros((0, 188), np.float32), np.zeros(0, np.int64)
    m = np.isin(b[:nw * W], (0x47, 0xB8)).reshape(nw, WIN, R).sum(axis=1)
    best, score = m.argmax(axis=1), m.max(axis=1)
    ok = score >= WIN // 2
    pk, rl, at = [], [], []
    w = 0
    while w < nw:
        if not ok[w]:
            w += 1
            continue
        w1 = w
        while w1 + 1 < nw and ok[w1 + 1] and best[w1 + 1] == best[w]:
            w1 += 1
        start, end = w * W + int(best[w]), (w1 + 1) * W
        col = b[start:end:R]
        if col.size >= 16:
            ph = int(np.argmax([np.sum(col[p::8] == 0xB8) for p in range(8)]))
            s0 = start + R * ph
            n = (end - s0 - R * (j83a.INTER_I - 1)) // (8 * R) * (8 * R)
            if n > 0:
                k = np.arange(n)
                idx = s0 + k + R * (k % j83a.INTER_I)
                cw = b[idx].reshape(-1, R)[:, :188]
                pk.append((cw.reshape(-1, 8, 188) ^ j83a.RAND_MASK).reshape(-1, 188))
                rl.append(rel[idx].reshape(-1, R)[:, :188])
                at.append(s0 + R * np.arange(cw.shape[0]))
        w = w1 + 1
    if not pk:
        return np.zeros((0, 188), np.uint8), np.zeros((0, 188), np.float32), np.zeros(0, np.int64)
    return np.concatenate(pk), np.concatenate(rl), np.concatenate(at)


def slice_job(job):
    path, fmt, fs, offset, s0, s1, keep0, keep1, inv = job
    raw = np.memmap(path, dtype=np.int16 if fmt == "sc16" else np.int8, mode="r")
    seg = raw[2 * s0:2 * s1].astype(np.float32)
    x = (seg[0::2] + 1j * seg[1::2]).astype(np.complex64)
    z = L.channelize(x, fs, offset)
    sym, out, mer = L.demod(z, 256, inv)
    b = rx.symbols_to_bytes(rx.diff_decode(sym, 8), 8, 0)
    pk, rl, at = raw_packets(b, L.byte_reliability(out))
    t = s0 + at * fs / rx.SYMRATE                        # sample index (timing drift ignored)
    sel = (t >= keep0) & (t < keep1) & (at > 700000)     # after the blind equalizer converged
    sync = np.mean(np.isin(pk[sel, 0], (0x47, 0xB8))) if sel.any() else 0
    return s0, pk[sel], np.minimum(255, rl[sel] * 100).astype(np.uint8), t[sel], mer, sync


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iq")
    ap.add_argument("--fmt", choices=("sc16", "s8"), default="s8")
    ap.add_argument("--fs", type=float, default=20e6)
    ap.add_argument("--center", type=float, default=282e6)
    ap.add_argument("--freq", type=float, default=282e6)
    ap.add_argument("--chunk", type=float, default=1.5)
    ap.add_argument("--overlap", type=float, default=0.25)
    ap.add_argument("--start", type=float, default=0, help="seconds skipped at the start")
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", default="raw")
    a = ap.parse_args()
    item = 4 if a.fmt == "sc16" else 2
    n_all = os.path.getsize(a.iq) // item
    first = int(a.start * a.fs)
    last = n_all if a.seconds is None else min(n_all, first + int(a.seconds * a.fs))
    step, ov = int(a.chunk * a.fs), int(a.overlap * a.fs)
    jobs = [(a.iq, a.fmt, a.fs, a.freq - a.center, max(0, s - ov), min(last, s + step), s, s + step, True)
            for s in range(first, last, step) if min(last, s + step) - s > int(0.3 * a.fs)]
    t0 = float(open(a.iq + ".t0").read()) if os.path.exists(a.iq + ".t0") else 0.0
    t1 = time.time()
    res = []
    with ProcessPoolExecutor(a.jobs) as ex:
        for s0, pk, rl, t, mer, sync in ex.map(slice_job, jobs):
            res.append((s0, pk, rl, t))
            print(f"  {s0 / a.fs:6.1f} s : MER {mer:.1f} dB, {len(pk)} paquets, octets de synchro justes "
                  f"{100 * sync:.0f} %", flush=True)
    res.sort(key=lambda r: r[0])
    pk = np.concatenate([r[1] for r in res])
    rl = np.concatenate([r[2] for r in res])
    t = np.concatenate([r[3] for r in res])
    np.savez(f"{a.out}_raw.npz", pk=pk, rl=rl, t=t0 + t / a.fs)
    print(f"{len(pk)} paquets -> {a.out}_raw.npz ({time.time() - t1:.0f} s de calcul)")


if __name__ == "__main__":
    main()
