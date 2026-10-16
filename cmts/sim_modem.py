# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Minimal cable modem model for offline tests of the upstream chain (no hardware).

It reads the real MAP and RNG-RSP bytes produced by DownstreamMux, follows the CM ranging
state machine (broadcast initial ranging, T3, Station Maintenance, success) and answers with
real RNG-REQ / REQ bursts rendered as IQ samples at the B210 RX rate, with plant delay,
power error, carrier offset and noise. Its ranging offset, power and frequency are corrected
only through the RNG-RSP it decodes, so convergence proves the whole loop.
"""
import struct

import numpy as np

from . import docsis_mac as mac
from .us_phy import encode_burst, profiles, synth
from .upstream import TS_RATE

T3 = int(0.2 * TS_RATE)


class SimModem:
    def __init__(self, addr, rx, delay_us=11.7, power_db=-38.0, freq_hz=2300.0, snr_db=28.0,
                 symrate=1.28e6, ms_counts=256, req_every=0.5, seed=0, log=print):
        self.addr = addr
        self.ms_counts = ms_counts
        self.rx = rx                       # UpstreamRx (for its time base and sample rate)
        self.tb, self.fs, self.decim = rx.tb, rx.fs, rx.decim
        self.sps = rx.fs / symrate
        self.delay = delay_us * 1e-6 * TS_RATE       # round trip, counts
        self.offset = 0.0                            # ranging offset, counts (>0 = earlier)
        self.power, self.freq = power_db, freq_hz
        self.noise = 10 ** ((power_db - snr_db) / 20) / np.sqrt(2)
        self.snr_db = snr_db
        self.rng = np.random.default_rng(seed)
        self.prof = profiles()
        self.state = "init"                # init -> wait_sm -> ranged
        self.sid = None
        self.cmts = None
        self.t3 = None
        self.bursts = []                   # (first sample, samples)
        self.req_every = int(req_every * TS_RATE)
        self.next_req = None
        self.log = log
        self.tx_count = {"RNG-REQ": 0, "REQ": 0}
        self.t3_timeouts = 0
        self.success_ts = None

    # ------------------------------------------------------------ downstream
    def on_frame(self, f, air_ts):
        if f[0] not in (0xC2, 0xC3) or len(f) < 30:
            return
        hl = 6 + (f[1] if f[0] & 1 else 0)
        m = f[hl:]
        da, sa, mtype, body = m[0:6], m[6:12], m[18], m[20:-4]
        if mtype == mac.MAP:
            self._map(sa, body, air_ts)
        elif mtype == mac.RNG_RSP and da == self.addr:
            self._rng_rsp(body, air_ts)

    def _map(self, sa, body, air_ts):
        self.cmts = sa
        n, start = body[2], struct.unpack(">I", body[4:8])[0]
        ms = self.ms_counts
        for k in range(n):
            v = struct.unpack(">I", body[16 + 4 * k:20 + 4 * k])[0]
            sid, iuc, off = v >> 18, (v >> 14) & 15, v & 0x3FFF
            ts = (start + off) * ms
            if iuc == mac.IUC_INIT_MAINT and self.state == "init" and self.t3 is None:
                self._send(ts, mac.rng_req_msg(self.addr, sa, 0, 1), mac.IUC_INIT_MAINT, "RNG-REQ")
                self.t3 = ts + T3
            elif iuc == mac.IUC_STATION_MAINT and sid == self.sid and self.state in ("wait_sm", "ranged"):
                self._send(ts, mac.rng_req_msg(self.addr, sa, self.sid, 1), mac.IUC_STATION_MAINT, "RNG-REQ")
                if self.state == "wait_sm":
                    self.t3 = ts + T3
            elif iuc == mac.IUC_REQUEST and self.state == "ranged" and self.next_req is not None \
                    and ts >= self.next_req:
                self._send(ts, mac.req_frame(self.sid, 10), mac.IUC_REQUEST, "REQ")
                self.next_req = ts + self.req_every

    def _rng_rsp(self, body, air_ts):
        sid = struct.unpack(">H", body[0:2])[0]
        tl = {}
        i = 3
        while i + 2 <= len(body):
            t, ln = body[i], body[i + 1]
            tl[t] = body[i + 2:i + 2 + ln]
            i += 2 + ln
        ta = struct.unpack(">i", tl[1])[0] if 1 in tl else 0
        pq = struct.unpack(">b", tl[2])[0] if 2 in tl else 0
        fq = struct.unpack(">h", tl[3])[0] if 3 in tl else 0
        status = tl.get(5, b"\x01")[0]
        self.offset += ta
        self.power += pq / 4
        self.freq += fq
        self.sid = sid
        self.t3 = None
        if status == mac.RNG_SUCCESS:
            if self.state != "ranged":
                self.success_ts = air_ts
                self.next_req = air_ts + int(0.05 * TS_RATE)
            self.state = "ranged"
        elif self.state != "ranged":
            self.state = "wait_sm"

    def tick_time(self, now_ts):
        """CM timers (called with the current air time)."""
        if self.t3 is not None and now_ts > self.t3:
            self.t3 = None
            self.t3_timeouts += 1
            self.log(f"[SIM-CM] T3 timeout (état {self.state})")
            if self.state == "wait_sm":
                self.state = "init"

    # ------------------------------------------------------------ upstream
    def _send(self, ts, frame, iuc, name):
        sym = encode_burst(self.prof[iuc], frame)
        t = (self.tb.tick(ts + self.delay - self.offset) - self.rx.rx_tick0) / self.decim  # 1st symbol, samples
        n0 = int(np.floor(t)) - 64
        n = int(sym.size * self.sps) + 128
        x = synth(sym, self.sps, t - n0, n) * 10 ** (self.power / 20)
        x *= np.exp(1j * (2 * np.pi * self.freq * (n0 + np.arange(n)) / self.fs + 0.7))
        self.bursts.append((n0, x.astype(np.complex64)))
        self.tx_count[name] += 1

    def render(self, n0, n):
        out = (self.rng.normal(size=n) + 1j * self.rng.normal(size=n)).astype(np.complex64) * self.noise
        keep = []
        for b0, x in self.bursts:
            a, b = max(n0, b0), min(n0 + n, b0 + x.size)
            if a < b:
                out[a - n0:b - n0] += x[a - b0:b - b0]
            if b0 + x.size > n0 + n:
                keep.append((b0, x))
        self.bursts = keep
        return out
