# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Decoding threads of the upstream receiver (UpstreamRx decode_threads, --us-decode-threads):
a stream of synthetic 64-QAM 5.12 Msym/s segment bursts (Multiple Transmit Channel mode) in
unicast data grants, with request windows between them, decoded with 1, 2 and 3 threads: the
same events in the same order (interval order, empty grants included), the same segments, the
same arrival times; processed_ts at the end of the last interval; and the time each takes
(only indicative here: 36 intervals, the equalizer cache cold. Warm, 150 intervals, i5-11300H:
2/3/4 threads 1.3/1.8/2.2x faster than 1)."""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import cdsp, docsis_mac as mac, upstream, us_phy   # noqa: E402

mac.configure_upstream("atdma", 6)
FS, SYMRATE = 13.904e6, 5.12e6
SPS = FS / SYMRATE
IUC = mac.IUC_ADV_LONG
TPC = FS / 10.24e6                         # samples per 10.24 MHz count (decimation 1)
MS_COUNTS = 256                            # 128-symbol mini-slots
ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


if not cdsp.AVAILABLE:
    print("SKIP pas de compilateur C : usdsp.c absent")
    sys.exit(77)
prof = us_phy.profiles()[IUC]
pts, bps = us_phy.constellation(prof)
t, k = prof.fec_t, prof.fec_k


def layout_for(gsym):
    fill = int(round(gsym - prof.guard - prof.preamble.size)) * prof.bps // 8
    n = -(-fill // (k + 2 * t))
    last = fill - (n - 1) * (k + 2 * t) - 2 * t
    return [k] * (n - 1) + ([min(last, k)] if last >= 16 else [])


def segment_symbols(seq, gsym, nbytes, rng):
    """The symbols of a segment of nbytes (header + payload) filling a grant of gsym symbols."""
    seg = mac.seg_header(seq, 0, req=0) + rng.integers(0, 256, nbytes - 8, dtype=np.uint8).tobytes()
    data, pos = b"", 0
    for n in layout_for(gsym):
        if pos >= len(seg):
            break
        block = seg[pos:pos + n]
        pos += len(block)
        data += prof.rs.encode(bytes(us_phy._BITREV[np.frombuffer(block, np.uint8)]))
    bits = us_phy.scramble(np.unpackbits(np.frombuffer(data, np.uint8)), prof.seed)
    bits = np.concatenate([bits, np.zeros(-bits.size % bps, np.uint8)])
    sym = np.concatenate([prof.preamble, pts[bits.reshape(-1, bps) @ (1 << np.arange(bps - 1, -1, -1))]])
    return np.convolve(sym, [1.0, 0.04 - 0.03j, 0.01j])[:sym.size], seg


rng = np.random.default_rng(11)
grants, segs, pieces = [], [], []
pos = 4000                                  # samples
for i in range(36):
    ms = int(rng.integers(30, 200))
    gsym = ms * 128
    g0 = pos
    g1 = g0 + gsym * SPS
    req = (int((g1 + 600) / TPC), int((g1 + 600 + 4 * 348) / TPC))   # a request window after it
    if i % 6 == 5:                          # a grant the modem leaves empty
        segs.append(None)
    else:
        full = sum(layout_for(gsym - 200))
        nb = full if i % 3 else int(rng.integers(40, max(41, full - 30)))
        sym, seg = segment_symbols(i + 1, gsym - 200, nb, rng)
        frac = rng.random() * 0.3
        x = us_phy.synth(sym, SPS, 64 + frac, int(64 + (sym.size + 40) * SPS)) * 10 ** (-20 / 20)
        pieces.append((int(g0) - 64, x))
        segs.append(seg)
    grants.append((int(g0 / TPC), int(g1 / TPC), IUC | mac.IUC_SEG, 1))
    grants.append(req + (mac.IUC_REQUEST, mac.SID_BROADCAST))
    pos = int(g1 + 600 + 4 * 348 + 2000)
N = pos + 20000
stream = ((rng.standard_normal(N) + 1j * rng.standard_normal(N)) * 10 ** (-73 / 20) / np.sqrt(2)).astype(np.complex64)
for s0, x in pieces:
    stream[s0:s0 + x.size] += x.astype(np.complex64)


class TB:
    tpc = TPC

    def tick(self, ts):
        return ts * TPC

    def ts(self, tick):
        return tick / TPC


def run(threads):
    ev = []
    rx = upstream.UpstreamRx(FS, 1, SYMRATE, TB(), MS_COUNTS, ev.append, history_s=N / FS, buf=stream.copy(),
                             decode_threads=threads)
    rx.rx_tick0 = 0
    rx.floor = 10 ** (-73 / 10)
    rx.add_grants(grants)
    rx.written = N
    us_phy._prbs(prof.seed, 1 << 18)
    t0 = time.perf_counter()
    while rx.windows or rx._pending:
        rx.process()
    return ev, time.perf_counter() - t0, rx


ev1, t1, rx1 = run(1)
data1 = [e for e in ev1 if e.sid == 1]
got = [e.burst.frame if e.burst is not None and e.burst.frame is not None else None for e in data1]
want_ok = all((g is not None and g[:len(s)] == s[:len(g)]) if s is not None else g is None
              for g, s in zip(got, segs))
check("1 fil : un événement par grant de données, segments décodés, grants vides vides",
      len(data1) == len(segs) and want_ok, f"{sum(g is not None for g in got)}/{sum(s is not None for s in segs)}")
for n in (2, 3):
    ev, tn, rxn = run(n)
    same = len(ev) == len(ev1) and all(
        a.sid == b.sid and a.iuc == b.iuc and a.start_ts == b.start_ts
        and (a.burst is None) == (b.burst is None)
        and (a.burst is None or (a.burst.frame == b.burst.frame and abs(a.rx_ts - b.rx_ts) < 0.05))
        for a, b in zip(ev1, ev))
    check(f"{n} fils = 1 fil : mêmes événements, même ordre, mêmes segments", same, f"{len(ev)} événements")
    check(f"{n} fils : processed_ts à la fin du dernier intervalle, plus rien en attente",
          rxn.processed_ts == max(g[1] for g in grants) and not rxn._pending)
    # ("fast" may differ: decoded side by side, the first bursts of a SID find no equalizer in
    # the cache yet and take the Python path; once it is filled, all go through C)
    check(f"{n} fils : mêmes statistiques de rafales", all(rxn.stats[s] == rx1.stats[s] for s in ("bursts", "decoded")),
          f"{dict((s, rxn.stats[s]) for s in ('bursts', 'decoded'))}, en C {rxn.stats['fast']} (1 fil : {rx1.stats['fast']})")
    print(f"   temps : 1 fil {1e3 * t1:.0f} ms, {n} fils {1e3 * tn:.0f} ms ({t1 / max(tn, 1e-9):.2f}x, "
          f"{os.cpu_count()} CPU)")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
