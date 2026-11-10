# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""The downstream data path in C (cmts/dsdsp.c dp_*, cdsp.DataPlane) against the Python path it
replaces (TapFeed -> BondedForwarder.send -> data_pdu(_dsid) -> mux queues -> _data_batch ->
ts_pack): the same TS packets on every channel, byte for byte, for a mix of frames (two bonded
modems, a CPE learned behind each, a modem not bonded, broadcast, unknown addresses, small and
full-size frames); the age limit and CoDel drop the same frames; a full queue refuses frames
instead of overwriting; the queue's ring wraps without mixing frames up; zlib.crc32 and the HCS."""
import os
import random
import sys
import time
import zlib
from types import SimpleNamespace as NS

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import cdsp, docsis_mac as mac                        # noqa: E402
from cmts.bridge import BondedForwarder, TapFeed               # noqa: E402
from cmts.downstream import CoDel, DownstreamMux, SecondaryMux   # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


if not cdsp.DS_AVAILABLE:
    print("SKIP pas de compilateur C : dsdsp.c absent")
    sys.exit(77)

rnd = random.Random(7)
d = [os.urandom(n) for n in (0, 1, 7, 8, 9, 63, 100, 1514)]
check("CRC32 = zlib.crc32", all(cdsp.crc32(x) == zlib.crc32(x) for x in d))
check("HCS = crc16_ccitt", all(cdsp.hcs(x) == mac.crc16_ccitt(x) for x in d))

A, B, C = bytes.fromhex("247f20a838b8"), bytes.fromhex("247f20aaaaaa"), bytes.fromhex("247f20cccccc")
CPE_A, CPE_B, CPE_C = bytes.fromhex("0011223344aa"), bytes.fromhex("0011223344bb"), bytes.fromhex("0011223344cc")
HOST = bytes.fromhex("7ec9086286cf")
NCH = 6


def system(use_c, bonded=None, chans_of=None):
    mux = DownstreamMux(qam=256, sps=8)
    mux.secondary = [SecondaryMux(qam=256) for _ in range(NCH - 1)]
    chans = [mux] + mux.secondary
    for i, m in enumerate(chans):
        m.mdd = mac.mdd_msg(mux.sa, 1, i + 1, [(k + 1, 306e6 + 8e6 * k, 256, k == 0) for k in range(NCH)], [1])
        m.data_max_ms = 1e9                      # no drop by age here: same frames on both paths
    core = NS(by_sid={1: NS(mac=A), 2: NS(mac=B), 3: NS(mac=C)},
              bonded=dict(bonded if bonded is not None else {A: 0x1001, B: 0x1002}),
              bonded_channels=dict(chans_of if chans_of is not None else {A: [1, 2, 3, 4, 5, 6], B: [1, 3, 5]}))
    fwd = BondedForwarder(chans, core, mac)
    fwd.learn(HOST + CPE_A + b"\x08\x00", 1)
    fwd.learn(HOST + CPE_B + b"\x08\x00", 2)
    fwd.learn(HOST + CPE_C + b"\x08\x00", 3)     # behind C, not bonded: primary
    plane = None
    if use_c:
        plane = cdsp.DataPlane(NCH)
        for i, m in enumerate(chans):
            plane.config(i, m.data_max_ms / 1e3)
        fwd.attach_plane(plane)
    feed = TapFeed(fwd.send, max_age_s=1e6, plane=plane, sync=fwd.sync if plane else None)
    return mux, chans, core, fwd, feed


def batches(seed, n):
    r = random.Random(seed)
    out = []
    for _ in range(n):
        frames = []
        for _ in range(r.choice((1, 3, 17, 64))):
            dst = r.choice((A, B, CPE_A, CPE_B, CPE_A, CPE_B, C, CPE_C, b"\xff" * 6,
                            bytes.fromhex("01005e000001"), bytes.fromhex("0000deadbeef")))
            size = r.choice((60, 64, 100, 576, 1500, 1514))
            frames.append(dst + HOST + b"\x08\x00" + bytes(r.getrandbits(8) for _ in range(size - 14)))
        out.append((b"".join(frames), np.array([len(f) for f in frames], np.int32)))
    return out


def run(use_c, seq, blocks=300, **kw):
    mux, chans, core, fwd, feed = system(use_c, **kw)
    out = [[] for _ in chans]
    it = iter(seq)
    for b in range(blocks):
        if b % 2 == 0:                           # a batch every other block: queues fill and empty
            nxt = next(it, None)
            if nxt is not None:
                feed.add(time.perf_counter(), *nxt)
        feed.pump(2 * 64 * NCH * 183)
        for k, m in enumerate(chans):
            out[k].append(m.packets(64))
    return [np.concatenate(o) for o in out], chans, fwd


seq = batches(1, 140)
py, chp, fwp = run(False, seq)
cc, chc, fwc = run(True, seq)
same = [np.array_equal(a, b) for a, b in zip(py, cc)]
check("mêmes paquets TS sur les 6 canaux (Python / C)", all(same), f"{same}")
check("mêmes trames par canal", [m.stats["data"] for m in chp] == [m.stats["data"] for m in chc],
      f"{[m.stats['data'] for m in chc]}")
check("même répartition (per_chan)", fwp.per_chan == fwc.plane.per_chan(), f"{fwp.per_chan} / {fwc.plane.per_chan()}")
check("des trames sur chaque canal", all(m.stats["data"] > 0 for m in chc))
check("numéros de séquence DSID suivis", [fwc.plane.seq(x) for x in (0x1001, 0x1002)] ==
      [fwp.seq.get(x) for x in (0x1001, 0x1002)], f"{[fwc.plane.seq(x) for x in (0x1001, 0x1002)]}")

# one bonded modem: an address not learned goes to it (as before 2026-10-10); none bonded: primary
for name, kw in (("un seul modem agrégé", dict(bonded={A: 0x1001}, chans_of={A: [1, 2, 3, 4, 5, 6]})),
                 ("aucun modem agrégé", dict(bonded={}, chans_of={}))):
    py, _, _ = run(False, seq[:60], 160, **kw)
    cc, _, _ = run(True, seq[:60], 160, **kw)
    check(f"{name} : mêmes paquets TS", all(np.array_equal(a, b) for a, b in zip(py, cc)))

# the routes follow a modem that bonds while running
mux, chans, core, fwd, feed = system(True, bonded={}, chans_of={})
feed.add(time.perf_counter(), *seq[0])
feed.pump(1 << 20)
before = [m.queue_len() for m in chans]
core.bonded[A] = 0x1001
core.bonded_channels[A] = [1, 2]
feed.add(time.perf_counter(), CPE_A + HOST + b"\x08\x00" + bytes(100), np.array([114], np.int32))
feed.pump(1 << 20)
after = [m.queue_len() for m in chans]
check("avant agrégation : tout sur le primaire", sum(before[1:]) == 0 and before[0] > 0, f"{before}")
check("modem agrégé en cours de route : sa trame sur un de ses canaux (1 ou 2), avec son DSID",
      [a - b for a, b in zip(after, before)] in ([1, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0]) and
      fwd.plane.seq(0x1001) == 1, f"{before} -> {after}")

# age limit and CoDel: the same frames dropped as the Python CoDel would
plane = cdsp.DataPlane(1)
plane.config(0, 0.100, 0.005, 0.100)
ref, q = CoDel(5), []
t0 = 1000.0
frame = HOST + HOST + b"\x08\x00" + bytes(1000)
lens = np.array([len(frame)], np.int32)
sent_c = sent_py = drop_c = drop_py = 0
for step in range(400):
    now = t0 + step * 0.002
    for _ in range(3):                           # 3 frames every 2 ms, 1 taken: a standing queue
        plane.push(frame, lens, [0, 0], now, 1 << 20)
        q.append(now)
    _, _, s, dr, _, _ = plane.take(0, now, 183, 0, limit=1)
    sent_c += s
    drop_c += dr
    while q:                                     # the Python _data_batch, one frame per batch
        wait = now - q[0]
        if wait * 1e3 > 100 or ref.drop(wait, now, len(q)):
            q.pop(0)
            drop_py += 1
            continue
        q.pop(0)
        sent_py += 1
        break
check("CoDel et limite d'âge : mêmes trames jetées que Python", (sent_c, drop_c) == (sent_py, drop_py),
      f"C {sent_c} envoyées {drop_c} jetées, Python {sent_py} / {drop_py}")
check("CoDel a bien jeté des trames", drop_c > 0)

# a full queue refuses frames, and the ring wraps without mixing them up
plane = cdsp.DataPlane(1)
plane.config(0, 1e3)
big = HOST + HOST + b"\x08\x00" + bytes(1500)
n_full = 0
for _ in range(1000):
    _, q_, f_ = plane.push(big, np.array([len(big)], np.int32), [0, 0], 0.0, 1 << 20)
    n_full += f_
check("file pleine : trames refusées, comptées", n_full > 0 and plane.refused(0) == n_full,
      f"{plane.queued(0)} en file, {n_full} refusées")
rnd = random.Random(3)
plane = cdsp.DataPlane(1)
plane.config(0, 1e3)
expect, got, nid = [], [], 0
for rnd_i in range(3000):
    for _ in range(rnd.randint(0, 40)):
        size = rnd.choice((60, 300, 1514))
        eth = HOST + HOST + b"\x08\x00" + nid.to_bytes(4, "big") + bytes(size - 18)
        nid += 1
        _, q_, f_ = plane.push(eth, np.array([len(eth)], np.int32), [0, 0], 0.0, 1 << 20)
        if q_:
            expect.append(mac.data_pdu(eth))
    # fewer bytes taken than pushed for the first 2000 rounds: the queue fills up (refusals), its
    # ring wraps around many times; then more: it drains
    lim = rnd.randint(1, 60) if rnd_i < 2000 else rnd.randint(100, 300)
    _, _, s, _, _, pdus = plane.take(0, 0.0, 32 * 183, 0, limit=lim, frames=True)
    got += pdus or []
while plane.queued(0):
    got += plane.take(0, 0.0, 32 * 183, 0, limit=400, frames=True)[5] or []
check("anneau qui boucle : toutes les trames, dans l'ordre, intactes", got == expect and plane.refused(0) > 0,
      f"{len(got)} / {len(expect)} trames, {nid} poussées, {plane.refused(0)} refusées (file pleine)")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
