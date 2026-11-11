# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Edge PHY output (cmts/edgephy.py, --sdr edgephy) against a simulated board on localhost:
  - channel layout: the MAC's channels (primary first, "around") -> the board's 4 adjacent slots;
  - the radio process loop (edgephy_main) with the CMTS's own shared rings: every MAC block goes out
    as its groups n0 + seq x (block / 8) + j, each slot carrying its channel's 8 packets, before its
    air tick (T0 + n x G) and not more than the lead earlier;
  - the upstream datagrams land in the SampleRing at device tick = board tick - base, base being
    what puts the MAC's block 0 on group n0 (sample k of a datagram = its tick + 4 k), a lost
    datagram counted and its samples left as zeros;
  - a restarted board core (new T0) stops the radio process (fatal) instead of drifting."""
import os
import socket
import struct
import sys
import threading
import time
import types

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import edgephy as ep                        # noqa: E402
from cmts.radio import PacketRing                     # noqa: E402
from cmts.usproc import SampleRing                    # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


# ---------------------------------------------------------------- layout
f0, slots, mask = ep.ds_plan([306e6, 298e6, 314e6, 322e6])
check("4 DS autour de 306 : canal 0 à 298, emplacements 1/0/2/3, masque 0xf",
      (f0, slots, mask) == (298e6, [1, 0, 2, 3], 0xF), f"{f0} {slots} {mask:#x}")
check("1 DS : un emplacement", ep.ds_plan([306e6]) == (306e6, [0], 1))
for bad, why in (([298e6, 330e6], "hors des 4 canaux"), ([306e6, 310e6], "pas sur la grille de 8 MHz"),
                 ([1e6 * f for f in (290, 298, 306, 314, 322)], "5 canaux")):
    try:
        ep.ds_plan(bad)
        check(f"refusé : {why}", False)
    except ValueError:
        check(f"refusé : {why}", True)
check("groupe : 9792 éch. en 256-QAM, 13056 en 64-QAM", ep.group_samples(256) == 9792 and ep.group_samples(64) == 13056)

# ---------------------------------------------------------------- simulated board
PORTS = (41100, 41101, 41102)
FS = ep.FS


class Board:
    """Control replies on a tick running at FS from a random origin, records the data
    datagrams with the board tick at arrival, sends the upstream (ramp of the sample index)."""

    def __init__(self, qam=256, mask=0xF):
        self.t_origin = time.perf_counter() - 3.7
        self.T0, self.epoch, self.qam, self.mask = 123457, 3, qam, mask
        self.G = ep.group_samples(qam)
        self.groups = {}                       # n -> (tick at arrival, mask, payload)
        self.peer = None
        self.stop = threading.Event()
        self.c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.c.bind(("127.0.0.1", PORTS[1]))
        self.c.settimeout(0.1)
        self.d = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.d.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.d.bind(("127.0.0.1", PORTS[0]))
        self.d.settimeout(0.1)
        self.u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.us_seq, self.us_skip = 0, {1300}  # one "lost" (~0.25 s in: after the ring started)
        for f in (self._ctl, self._data, self._us):
            threading.Thread(target=f, daemon=True).start()

    def tick(self):
        return int((time.perf_counter() - self.t_origin) * FS)

    def _ctl(self):
        while not self.stop.is_set():
            try:
                d, addr = self.c.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                return
            magic, nonce = struct.unpack("<IQ", d[:12])
            if magic != ep.MAGIC_QUERY:
                continue
            self.peer = addr[0]
            vals = [ep.MAGIC_REPLY, 1, nonce, self.tick(), self.T0, 0, self.epoch, 1, int(FS), self.G,
                    self.qam, self.mask] + [0] * 12
            self.c.sendto(struct.pack(ep.REPLY_FMT, *vals), addr)

    def _data(self):
        while not self.stop.is_set():
            try:
                d = self.d.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            magic, g, m = struct.unpack("<IQI", d[:16])
            if magic == ep.MAGIC_DATA:
                self.groups[g] = (self.tick(), m, d[16:])

    def _us(self):
        ns = 2036
        nxt = None
        while not self.stop.is_set():
            time.sleep(0.002)
            if self.peer is None:
                continue
            if nxt is None:
                nxt = self.tick() // 4 * 4
            while nxt + 4 * ns <= self.tick():
                k = np.arange(ns) + nxt // 4
                iq = np.empty(2 * ns, np.int8)
                iq[0::2] = (k % 200 - 100).astype(np.int8)       # I: the sample index (mod 200)
                iq[1::2] = 7
                hdr = ep.US_HDR.pack(ep.MAGIC_US, self.us_seq, nxt, ns, 1, ep.US_DECIM, 0, 0, 0)
                if self.us_seq not in self.us_skip:
                    try:
                        self.u.sendto(hdr + iq.tobytes(), (self.peer, PORTS[2]))
                    except OSError:
                        return
                self.us_seq += 1
                nxt += 4 * ns

    def close(self):
        self.stop.set()
        for s in (self.c, self.d, self.u):
            s.close()


board = Board()
NCH, BLOCK = 4, 64
MCR = FS
T_START = 0.2
p = dict(ep_host="127.0.0.1", ep_ports=PORTS, ep_http=0, ep_att=None, ep_freq0=f0, ep_slots=slots, ep_mask=mask,
         ep_us_freq=60e6, mcr=MCR, t_start=T_START, qams=[256] * NCH, invert=True, scale=0.3,
         pk_shape=(NCH, BLOCK, 5), ep_lead_s=0.015)
pk = PacketRing(NCH, BLOCK, 5)
ring = SampleRing(FS / 4, 4, history_s=1.0)
msgs = []


class Q:
    def put(self, m):
        msgs.append(m)


stop_ev = types.SimpleNamespace(value=0)
logs = []
th = threading.Thread(target=ep.edgephy_main, args=(p, pk, ring, Q(), stop_ev, logs.append), daemon=True)
th.start()
NBLK = 60                                   # ~114 ms of downstream


def block(seq):
    """channel i, packet j: bytes [0x47, seq, i, j, ...]"""
    b = np.zeros((NCH, BLOCK, 188), np.uint8)
    b[:, :, 0] = 0x47
    b[:, :, 1] = seq % 256
    b[:, :, 2] = np.arange(NCH)[:, None]
    b[:, :, 3] = np.arange(BLOCK)[None, :]
    return b


ev = threading.Event()
for s in range(NBLK):
    pk.put(list(block(s)), ev, s)
t_wait = time.monotonic() + 3
while time.monotonic() < t_wait and len(board.groups) < NBLK * BLOCK // 8:
    time.sleep(0.05)
time.sleep(0.2)
started = [m for m in msgs if m[0] == "started"]
fatal = [m for m in msgs if m[0] == "fatal"]
check("démarré, pas d'erreur", started and not fatal, str(fatal or logs[:3]))
gpb = BLOCK // 8
gs = sorted(board.groups)
n0 = gs[0] if gs else None
check(f"{NBLK} blocs -> {NBLK * gpb} groupes numérotés n0 + k, sans trou",
      gs == list(range(n0, n0 + NBLK * gpb)) if gs else False, f"{len(gs)} groupes")
G = board.G
air0 = board.T0 + n0 * G if gs else 0
ok_payload, ok_time, early = True, True, []
for n in gs:
    t_arr, m, pl = board.groups[n]
    k, j = divmod(n - n0, gpb)
    air = board.T0 + n * G
    early.append((air - t_arr) / FS * 1e3)
    ok_time &= t_arr < air
    rows = np.frombuffer(pl, np.uint8).reshape(4, 8, 188)    # slots 0..3
    for s in range(4):
        i = slots.index(s)                  # MAC channel carried by slot s
        r = rows[s]
        ok_payload &= m == mask and bool(np.all(r[:, 1] == k % 256) and np.all(r[:, 2] == i)
                                         and np.all(r[:, 3] == np.arange(j * 8, j * 8 + 8)))
check("chaque emplacement porte les 8 paquets de son canal (ordre MAC -> fréquences)", ok_payload)
check("chaque groupe arrive avant son heure d'antenne", ok_time,
      f"avance {min(early):.1f} à {max(early):.1f} ms" if early else "")
check("pas plus de ~l'avance (15 ms) + un bloc avant", early and max(early) < 15 + 2.5 + 3, f"{max(early) if early else 0:.1f} ms")
wall0 = started[0][1] if started else 0
check("bloc 0 à t_start (heure Unix annoncée)", started and abs(wall0 - time.time()) < 1.0)

# upstream: device tick of ring sample i = rx_tick0 + 4 i; board tick = that + base
base = air0 - round(T_START * MCR)
tick0 = ring.rx_tick0
w = ring.written
ok_us = tick0 is not None and w > 1000
if ok_us:
    idx = np.arange(min(w, ring.buf.size))
    x = ring.buf[idx]
    board_k = (tick0 + 4 * idx + base) // 4                   # board sample index
    got = np.round(x.real * 127).astype(int)
    want = (board_k % 200 - 100)
    nz = x != 0
    match = np.mean(got[nz] == want[nz]) if nz.any() else 0
    holes = int(np.sum(~nz))
    check("montant : échantillons rangés à tick carte - base (rampe retrouvée)", match > 0.999,
          f"{match:.4f}, {w} échantillons, tick0 {tick0} (>= {round(T_START * MCR)})")
    check("montant : commence au plus tôt au tick de l'échantillon 0 descendant", tick0 >= round(T_START * MCR))
    check("montant : le datagramme perdu laisse ses 2036 échantillons à zéro", holes == 2036, f"{holes} zéros")
else:
    check("montant : des échantillons dans l'anneau", False, f"tick0 {tick0}, écrits {w}")

# board core restarted -> fatal
board.T0 += 5
t_wait = time.monotonic() + 3
while time.monotonic() < t_wait and not any(m[0] == "fatal" for m in msgs):
    time.sleep(0.05)
check("cœur de la carte redémarré (nouveau T0) : arrêt (fatal)", any(m[0] == "fatal" for m in msgs))
stop_ev.value = 1
th.join(3)
check("processus radio terminé", not th.is_alive())
board.close()
pk.close()
ring.close()

# ---------------------------------------------------------------- the whole CMTS on the simulated board
import http.server                                     # noqa: E402
import signal                                          # noqa: E402
import subprocess                                      # noqa: E402
import tempfile                                        # noqa: E402

HTTP = 48080
sets = []


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        sets.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", HTTP), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
board = Board()
env = dict(os.environ, SDR_CMTS_EDGEPHY_PORTS=",".join(map(str, PORTS + (HTTP,))))
HERE = os.path.dirname(os.path.abspath(__file__))
with tempfile.TemporaryDirectory() as d:
    pr = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "sdr_cmts.py"), "--sdr", "edgephy",
                           "--edgephy-host", "127.0.0.1", "--ds-channels", "4", "--tap", "none",
                           "--status-http", "none", "--no-performance", "--no-irq-herd", "--rt-prio", "0",
                           "--color", "never", "--log", "none", "--cpus", "all"],
                          cwd=d, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(10)
    n_mid = len(board.groups)
    pr.send_signal(signal.SIGINT)
    try:
        out, _ = pr.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        pr.kill()
        out, _ = pr.communicate()
if "/dev/shm trop petit" in out:
    print("SKIP (/dev/shm trop petit)")
    sys.exit(0)
gs = sorted(board.groups)
cont = bool(gs) and gs == list(range(gs[0], gs[0] + len(gs)))
check("CMTS complet : réglages envoyés (298 MHz, 256-QAM, masque 15, montant 60 MHz, sans atténuation)",
      any("freq=298000000" in x and "qam=256" in x and "mask=15" in x and "us_freq=60000000" in x
          and "att=" not in x for x in sets), str(sets)[:200])
check("CMTS complet : groupes continus pendant ~9 s", cont and len(gs) > 0.8 * 9 * ep.FS / board.G,
      f"{len(gs)} groupes ({len(gs) * board.G / ep.FS:.1f} s), continus {cont}")
check("CMTS complet : la carte reçoit SYNC/MAP (PID 0x1FFE) sur l'emplacement du primaire (306 = 1)",
      any(np.any(((r[:, 1].astype(int) & 0x1F) << 8 | r[:, 2]) == 0x1FFE)
          for r in (np.frombuffer(board.groups[n][2], np.uint8).reshape(4, 8, 188)[1] for n in gs[-200:])))
check("CMTS complet : ni erreur ni trace", "Traceback" not in out and "fatal" not in out.lower()
      and "edge PHY" in out and "[EDGEPHY] émission" in out, out[-600:] if "Traceback" in out else "")
check("CMTS complet : arrêt propre (Ctrl+C)", pr.returncode == 0, f"code {pr.returncode}")
board.close()
srv.shutdown()
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
