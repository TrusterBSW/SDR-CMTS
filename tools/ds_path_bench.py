#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Cost of the downstream data path of the MAC, without radio nor TAP: frames as the TAP thread
hands them over (batches of 64), into PDUs, routed to a bonded modem over N channels, queued
(CoDel), packed into TS blocks as the encoder thread does (pump + packets per block). Prints the
CPU per frame and per second at full channel load.

    python3 tools/ds_path_bench.py [--channels 6] [--size 1514] [--seconds 3] [--python]

--python: the former path (TapFeed -> BondedForwarder.send -> Python queues), without the C data
plane (cdsp.DataPlane). i5-11300H laptop, 6 channels (2026-11-10): 1514-byte frames, Python 10.6 µs
per frame (0.25 core at 290 Mbit/s), C 2.9 µs (0.07); 100-byte frames, Python 6.4 µs (2 cores: it
falls behind), C 0.3 µs (0.10 core).
"""
import argparse
import os
import sys
import time
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import docsis_mac as mac                              # noqa: E402
from cmts.bridge import BondedForwarder, TapFeed               # noqa: E402
from cmts.downstream import CoDel, DownstreamMux, SecondaryMux   # noqa: E402
from cmts import cdsp                                            # noqa: E402
import numpy as np                                               # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--channels", type=int, default=6)
ap.add_argument("--size", type=int, default=1514)
ap.add_argument("--seconds", type=float, default=3.0)
ap.add_argument("--block", type=int, default=64)
ap.add_argument("--python", action="store_true", help="the Python path, without the C data plane")
a = ap.parse_args()

mux = DownstreamMux(qam=256, sps=8)
mux.secondary = [SecondaryMux(qam=256) for _ in range(a.channels - 1)]
chans = [mux] + mux.secondary
for m in chans:
    m.data_max_ms, m.aqm = 100.0, CoDel(30)
CM, CPE = bytes.fromhex("247f20a838b8"), bytes.fromhex("0011223344bb")
core = NS(by_sid={1: NS(mac=CM)}, bonded={CM: 0x1001}, bonded_channels={CM: list(range(1, a.channels + 1))})
fwd = BondedForwarder(chans, core, mac)
fwd.learn(bytes(6) + CPE + b"\x08\x00", 1)
plane = None
if not a.python:
    plane = cdsp.DataPlane(len(chans))
    for i, m in enumerate(chans):
        plane.config(i, m.data_max_ms / 1e3, m.aqm.target, m.aqm.interval)
    fwd.attach_plane(plane)
feed = TapFeed(fwd.send, max_age_s=0.1, plane=plane, sync=fwd.sync if plane else None)
frame = CPE + bytes.fromhex("7ec9086286cf") + b"\x08\x00" + os.urandom(a.size - 14)
batch_n = 64
batch = frame * batch_n
lens = np.full(batch_n, a.size, np.int32)
# frames per block the channels carry at most (183 bytes of a TS packet each)
per_block = a.block * a.channels * 183 // (a.size + 16)
budget = 2 * a.block * a.channels * 183
blocks = frames = 0
air = a.block * 204 * 8 / (6.952e6 * 8)              # s of air per block (256-QAM)
c0, t0 = time.process_time(), time.perf_counter()
fed = 0
while time.perf_counter() - t0 < a.seconds:
    while fed < (blocks + 2) * per_block:              # keep the queues just fed, as a saturated TAP
        feed.add(time.perf_counter(), batch, lens)
        fed += batch_n
    feed.pump(budget)
    for m in chans:
        m.packets(a.block)
    blocks += 1
cpu = time.process_time() - c0
sent = sum(m.stats["data"] for m in chans)
drop = sum(m.stats["data_drop"] for m in chans) + feed.stats["dropped"]
print(f"{'Python' if a.python else 'C'} : {a.channels} canaux, trames de {a.size} octets : {sent} trames en {blocks} blocs, {drop} jetées")
print(f"  {cpu / max(1, sent) * 1e6:.1f} µs CPU par trame, bloc {cpu / blocks * 1e6:.0f} µs "
      f"pour {air * 1e3:.2f} ms d'antenne : {cpu / blocks / air:.2f} cœur à pleine charge "
      f"({sent / blocks / air * a.size * 8 / 1e6:.0f} Mbit/s)")
