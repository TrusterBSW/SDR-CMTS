# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Downstream frames from the TAP by batches (cmts/bridge.py TapFeed, cdsp.tap_read_batch): the C
reader takes every frame waiting in one call, up to its frame limit, sizes exact; the encoder side
turns them into PDUs in order, a byte budget at a time, each with its arrival time; a batch older
than the queue limit is dropped before any work. A datagram socket stands for the TAP (one frame
per read, as a TAP gives)."""
import os
import socket
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import bridge, cdsp  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


if not cdsp.DS_AVAILABLE:
    print("SKIP (pas de C)")
    sys.exit(0)

a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
b.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
b.setblocking(False)
rng = np.random.default_rng(3)
sizes = rng.integers(60, 1515, 300)
frames = [bytes([k & 0xFF]) * int(n) for k, n in enumerate(sizes)]
for f in frames:
    a.send(f)

buf = np.empty(bridge.TAP_BATCH_BYTES, np.uint8)
lens = np.empty(bridge.TAP_BATCH_FRAMES, np.int32)
n1 = cdsp.tap_read_batch(b.fileno(), buf, lens, 100)
got1 = [buf[o:o + s].tobytes() for o, s in zip(np.concatenate([[0], np.cumsum(lens[:n1])[:-1]]), lens[:n1])]
check("un appel : toutes les trames en attente, jusqu'à la limite du lot", n1 == bridge.TAP_BATCH_FRAMES, f"{n1}")
check("tailles et contenus exacts, dans l'ordre", got1 == frames[:n1])
n2 = cdsp.tap_read_batch(b.fileno(), buf, lens, 100)
check("l'appel suivant : le reste", n2 == 300 - n1, f"{n2}")
t0 = time.perf_counter()
n3 = cdsp.tap_read_batch(b.fileno(), buf, lens, 50)
dt = time.perf_counter() - t0
check("rien en attente : retour au bout du délai, 0 trame", n3 == 0 and 0.04 < dt < 0.5, f"{n3} en {dt * 1e3:.0f} ms")

sent = []
feed = bridge.TapFeed(lambda eth, t: sent.append((eth, t)), max_age_s=0.1)
t_arr = time.perf_counter()
data = b"".join(frames[:n1])
feed.add(t_arr, data, np.array([len(f) for f in frames[:n1]], np.int32))
budget = 20_000
taken = feed.pump(budget)
check("un pompage : un budget d'octets, pas plus d'une trame au-delà", budget <= taken < budget + 1515,
      f"{taken} octets, {len(sent)} trames")
check("l'heure d'arrivée suit chaque trame", all(t == t_arr for _, t in sent))
while feed.pump(budget):
    pass
check("tout le lot, dans l'ordre", [e for e, _ in sent] == frames[:n1], f"{len(sent)} trames")
check("plus rien en attente", feed.waiting() == 0)

sent.clear()
feed.add(time.perf_counter() - 0.2, data, np.array([len(f) for f in frames[:n1]], np.int32))
feed.pump(budget)
check("un lot plus vieux que la limite de file : jeté sans être traité",
      not sent and feed.stats["dropped"] == n1, f"{feed.stats['dropped']} jetées")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
