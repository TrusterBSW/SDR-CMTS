# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Segment bursts (Multiple Transmit Channel mode) decoded whole in C (us_phy._seg_fast, usdsp.c
seg_fast) against the Python path (MINI_CMTS_US_FAST=0): same bytes, same timing, same MER, on
synthetic 64-QAM 5.12 Msym/s bursts with a little echo and noise; full grants, bursts that end
early (shortened last codeword), padding segments, a burst with a corrupted middle; and the time
each path takes."""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import cdsp, docsis_mac as mac, us_phy   # noqa: E402

mac.configure_upstream("atdma", 6)
FS, SYMRATE = 13.904e6, 5.12e6
SPS = FS / SYMRATE
IUC = mac.IUC_ADV_LONG
ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


prof = us_phy.profiles()[IUC]
pts, bps = us_phy.constellation(prof)
t, k = prof.fec_t, prof.fec_k


def layout_for(gsym):
    fill = int(round(gsym - prof.guard - prof.preamble.size)) * prof.bps // 8
    n = -(-fill // (k + 2 * t))
    last = fill - (n - 1) * (k + 2 * t) - 2 * t
    return [k] * (n - 1) + ([min(last, k)] if last >= 16 else [])


def burst(seq, gsym, nbytes, rng, pad=False, snr_db=33.0, corrupt=False):
    """A segment of nbytes (header + payload) in a grant of gsym symbols -> (window, start)."""
    lay = layout_for(gsym)
    payload = (b"\xff" * (nbytes - 8)) if pad else rng.integers(0, 256, nbytes - 8, dtype=np.uint8).tobytes()
    seg = mac.seg_header(seq, None if pad else 0, req=0) + payload
    data, pos = b"", 0
    for n in lay:
        if pos >= len(seg):
            break
        block = seg[pos:pos + n]
        pos += len(block)
        block = bytes(us_phy._BITREV[np.frombuffer(block, np.uint8)])
        data += prof.rs.encode(block)
    bits = np.unpackbits(np.frombuffer(data, np.uint8))
    bits = us_phy.scramble(bits, prof.seed)
    bits = np.concatenate([bits, np.zeros(-bits.size % bps, np.uint8)])
    labels = bits.reshape(-1, bps) @ (1 << np.arange(bps - 1, -1, -1))
    sym = np.concatenate([prof.preamble, pts[labels]])
    if corrupt:                                         # a hole in the middle of the burst
        m = sym.size // 2
        sym[m:m + 400] = pts[rng.integers(0, pts.size, 400)]
    sym = np.convolve(sym, [1.0, 0.04 - 0.03j, 0.01j])[:sym.size]    # small echo: equalizer work
    start = 300.0 + rng.random()
    n_out = int(start + (gsym + 60) * SPS)
    x = us_phy.synth(sym, SPS, start, n_out)
    x *= 10 ** (-20 / 20)
    noise = (rng.standard_normal(n_out) + 1j * rng.standard_normal(n_out)) / np.sqrt(2)
    x += noise * 10 ** ((-20 - snr_db) / 20)
    return x.astype(np.complex64), start, seg


def run(fast, cases, warm):
    os.environ["MINI_CMTS_US_FAST"] = "1" if fast else "0"
    d = us_phy.BurstDemod(FS, SYMRATE)
    xw, sw, gw = warm
    d.detect(xw, IUC, sw - 4, sw + 4, max_frame=int(gw * bps / 8) + 64, grant_syms=gw, eq_key=1, seg=True)
    out, dt = [], 0.0
    for x, s, g in cases:
        t0 = time.perf_counter()
        b = d.detect(x, IUC, s - 4, s + 4, max_frame=int(g * bps / 8) + 64, grant_syms=g, eq_key=1, seg=True)
        dt += time.perf_counter() - t0
        out.append(b)
    return out, dt, d


rng = np.random.default_rng(7)
G = 255 * 128 // 1                       # a 255 mini-slot grant, 128 symbols per mini-slot
g_long = 255 * 128 - 200
warm = burst(0, g_long, sum(layout_for(g_long)), rng)[:2] + (g_long,)
cases, kinds, segs = [], [], []
for i in range(40):
    kind = ["plein", "court", "bourrage", "abîmé"][i % 4] if i % 4 != 3 or i < 12 else "plein"
    g = int(rng.integers(40, 250)) * 128 - 200
    full = sum(layout_for(g))
    nb = full if kind in ("plein", "bourrage", "abîmé") else int(rng.integers(40, max(41, full - 30)))
    x, s, seg = burst(i + 1, g, nb, rng, pad=kind == "bourrage", corrupt=kind == "abîmé")
    cases.append((x, s, g))
    kinds.append(kind)
    segs.append(seg)

if not cdsp.AVAILABLE:
    print("SKIP (pas de C)")
    sys.exit(0)
us_phy._prbs(prof.seed, 1 << 18)                 # (built once per process, cached)
py, t_py, _ = run(False, cases, warm)
c, t_c, dc = run(True, cases, warm)
same = []
for a, b, kind, seg in zip(py, c, kinds, segs):
    fa = None if a is None else a.frame
    fb = None if b is None else b.frame
    eq = (fa == fb and (a is None or (abs(a.t - b.t) < 1e-3 and abs(a.mer - b.mer) < 0.3
                                      and a.rs_fixed == b.rs_fixed and abs(a.power_db - b.power_db) < 0.05)))
    same.append(eq)
    if not eq:
        print("   différence", kind, None if a is None else (a.t, a.mer, a.rs_fixed, len(fa or b"")),
              None if b is None else (b.t, b.mer, b.rs_fixed, len(fb or b"")))
good = [b is not None and b.frame is not None and b.frame[:len(s)] == s[:len(b.frame)] for b, s in zip(c, segs)]
check("chemin C = chemin Python sur 40 rafales", all(same), f"{sum(same)}/40")
check("segments pleins, courts et de bourrage décodés",
      all(g for g, kd in zip(good, kinds) if kd != "abîmé"), f"{sum(good)}/40")
check("rafales abîmées au milieu : rejetées comme en Python",
      all(s for s, kd in zip(same, kinds) if kd == "abîmé"))
n0 = dc.n_fast.get(0, 0)
check("le chemin C a pris les rafales lisibles avec l'égaliseur en cache", n0 >= 24, f"statuts {dc.n_fast}")
print(f"   temps : Python {1e3 * t_py / len(cases):.2f} ms/rafale, C {1e3 * t_c / len(cases):.2f} ms/rafale "
      f"({t_py / max(t_c, 1e-9):.1f}x)")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
