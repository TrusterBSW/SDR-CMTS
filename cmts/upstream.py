"""Upstream receiver: RX samples + the MAP schedule -> decoded bursts with ranging measurements.

Time base: the B210 transmits the downstream and receives the upstream with ONE master clock.
The TX stream starts at a known device tick, and the downstream timestamp is derived from the
symbol counter (cmts/downstream.py), so every DOCSIS timestamp maps to an exact device tick:

    tick(ts) = tx_start_tick + shaper_delay + (ts - ts_start) * master_clock / 10.24 MHz

and every RX sample carries its device tick (UHD metadata). The expected arrival of a burst is
tick(start of its first mini-slot); the difference with the measured arrival is the ranging
error. Fixed latencies (B210 TX/RX pipelines, the modem's own delays) are absorbed by ranging.
"""
import collections
import os
import time
from dataclasses import dataclass

import numpy as np

from . import docsis_mac as mac
from .j83a import PulseShaper
from .us_phy import BurstDemod, profiles

TS_RATE = 10.24e6
SYMRATE_DS = 6_952_000
DATA_IUCS = (mac.IUC_SHORT_DATA, mac.IUC_LONG_DATA, mac.IUC_ADV_SHORT, mac.IUC_ADV_LONG, mac.IUC_ADV_UGS)


def tx_symbol_delay(sps):
    """TX sample index at which downstream symbol 0 peaks (pulse shaper group delay)."""
    imp = np.zeros(64, dtype=np.complex64)
    imp[0] = 1
    return int(np.argmax(np.abs(PulseShaper(sps).process(imp))))


class Timebase:
    def __init__(self, mcr, tx_start_tick, sps, ts_start=0):
        self.mcr = mcr
        self.tpc = mcr / TS_RATE                       # device ticks per 10.24 MHz count
        self.origin = tx_start_tick + tx_symbol_delay(sps) * mcr / (sps * SYMRATE_DS)
        self.ts_start = ts_start

    def tick(self, ts):
        return self.origin + (ts - self.ts_start) * self.tpc

    def ts(self, tick):
        return self.ts_start + (tick - self.origin) / self.tpc


@dataclass
class BurstEvent:
    iuc: int              # interval type the burst was found in
    sid: int              # SID of the grant (0x3FFF = broadcast)
    start_ts: int         # expected arrival (start of the interval), 10.24 MHz counts
    burst: object = None  # us_phy.Burst, None if nothing was received in a unicast grant
    err_counts: float = 0.0   # arrival - expected, 10.24 MHz counts (positive = late)
    rx_ts: float = 0.0        # measured arrival, 10.24 MHz counts


DATA_SWAP = {mac.IUC_SHORT_DATA: mac.IUC_LONG_DATA, mac.IUC_LONG_DATA: mac.IUC_SHORT_DATA,
             mac.IUC_ADV_SHORT: mac.IUC_ADV_LONG, mac.IUC_ADV_LONG: mac.IUC_ADV_SHORT}


def _is_padding(bu):
    """A segment carrying nothing: no MAC frame starts in it and its bytes are all stuff (0xFF)."""
    info = bu.info or {}
    body = bu.frame[mac.SEG_HDR_LEN:]
    return info.get("kind") == "SEG" and not info.get("pfi") and len(body) > 0 and body.count(mac.STUFF) == len(body)


class UpstreamRx:
    """Keeps the recent RX samples (indexed by device tick) and demodulates every scheduled
    upstream interval once its samples have arrived. Thread use: add_grants() from the
    downstream thread, push() from the RX thread, process() from a worker thread."""

    def __init__(self, fs, decim, symrate, timebase, ms_counts, on_burst, history_s=1.0,
                 gate_db=8.0, save_dir=None, im_slack_us=1000.0, log=print, ko_dir=None, buf=None):
        self.fs, self.decim, self.tb = fs, decim, timebase
        self.demod = BurstDemod(fs, symrate)
        self.prof = profiles()
        self.ms_counts = ms_counts
        self.ms_samples = ms_counts * timebase.tpc / decim
        # Initial Maintenance: the modem is not ranged yet, accept its burst this far before or
        # after the interval (the Sagem 3686 arrives ~183 us early on the B210 bench)
        self.im_slack = im_slack_us * 1e-6 * fs
        self.on_burst = on_burst
        self.gate = 10 ** (gate_db / 10)
        # sample ring; `buf` given: filled by another process (cmts.usproc), push() unused here
        self.buf = np.zeros(int(history_s * fs), dtype=np.complex64) if buf is None else buf
        self.rx_tick0 = None        # device tick of absolute RX sample 0
        self.written = 0            # absolute index of the next sample to come
        self.windows = collections.deque()
        self.floor = None           # noise power per sample (linear)
        self.burst_level = None     # power of the decoded unicast data bursts (linear, running mean)
        self.save_dir = save_dir
        self.ko_dir = ko_dir        # data bursts that fail to decode are always kept here (debug)
        if ko_dir:
            os.makedirs(ko_dir, exist_ok=True)
        self.log = log
        self.stats = collections.Counter()
        self.processed_ts = 0       # end of the last processed interval (counts)
        self.lag_max_ms = 0.0       # worst processing delay since the last status line
        self.lag_now_ms = 0.0
        self.timing = {}            # diagnostic: "<IUC><+ burst | - none>" -> [windows, seconds]
        self.recent = collections.deque(maxlen=16)   # absolute sample times of recent bursts
        # raw energy scan of the whole RX stream, independent of the MAP (diagnostics)
        self.sched = collections.deque(maxlen=3000)    # recent scheduled intervals, for lookups
        self.raw_pos = None
        self.raw_next = 0.0
        self.raw_floor = None
        self.raw_log = collections.deque(maxlen=50)    # wall times of the printed raw events
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

    # ------------------------------------------------------------ inputs
    def add_grants(self, grants):
        # IUC | mac.IUC_SEG: a data grant of a Multiple Transmit Channel mode flow (segments)
        clean = [(s, e, iuc & ~mac.IUC_SEG, sid) for s, e, iuc, sid in grants]
        self.windows.extend((s, e, iuc & ~mac.IUC_SEG, sid, bool(iuc & mac.IUC_SEG)) for s, e, iuc, sid in grants)
        self.sched.extend(clean)

    def push(self, tick, x):
        """RX samples whose first one was sampled at device `tick`. Gaps (overflows) are
        zero-filled so that the absolute sample index always equals the tick offset."""
        if self.rx_tick0 is None:
            self.rx_tick0 = tick
            self.written = 0
        n0 = int(round((tick - self.rx_tick0) / self.decim))
        if n0 > self.written:
            self._write(np.zeros(min(n0 - self.written, self.buf.size), np.complex64), self.written)
            self.stats["gap_samples"] += n0 - self.written
            self.written = n0
        elif n0 < self.written:     # overlap: keep only the new part
            x = x[self.written - n0:]
            n0 = self.written
        self._write(x, n0)
        self.written = n0 + x.size

    def _write(self, x, n0):
        L = self.buf.size
        i = n0 % L
        k = min(x.size, L - i)
        self.buf[i:i + k] = x[:k]
        if k < x.size:
            self.buf[:x.size - k] = x[k:]

    def _read(self, n0, n1):
        L = self.buf.size
        if n0 < 0 or n0 < self.written - L or n1 > self.written:
            return None
        i = n0 % L
        if i + (n1 - n0) <= L:
            return self.buf[i:i + n1 - n0].copy()
        return np.concatenate([self.buf[i:], self.buf[:i + n1 - n0 - L]])

    def sample_of_ts(self, ts):
        return (self.tb.tick(ts) - self.rx_tick0) / self.decim

    # ------------------------------------------------------------ processing
    def process(self):
        """Demodulate every interval whose samples are complete. Returns the count handled."""
        if self.rx_tick0 is None:
            return 0
        done = 0
        while self.windows:
            start_ts, end_ts, iuc, sid, seg = self.windows[0]
            prof_iuc = mac.IUC_REQUEST if iuc == mac.IUC_REQUEST else iuc
            if prof_iuc not in self.prof:   # data grants: not demodulated yet
                self.windows.popleft()
                continue
            prof = self.prof[prof_iuc]
            s0 = self.sample_of_ts(start_ts)
            s1 = self.sample_of_ts(end_ts)
            pre = 2 * self.ms_samples                   # accept bursts up to 2 mini-slots early
            post = 0.0
            if iuc == mac.IUC_INIT_MAINT:
                pre, post = max(pre, self.im_slack), self.im_slack
            if iuc in DATA_IUCS:                        # a data burst fills its own grant
                n_burst = 2 * self.ms_samples
            else:
                n_burst = prof.burst_symbols(64) * self.demod.sps
            a = int(s0 - pre) - 64
            b = int(s1 + post + n_burst) + 64
            if b > self.written:
                break
            self.windows.popleft()
            done += 1
            self.processed_ts = end_ts
            # how far behind the RX stream this interval is handled (the buffer holds history_s)
            lag = (self.written - s1) / self.fs * 1e3
            self.lag_now_ms = lag
            if lag > self.lag_max_ms:
                self.lag_max_ms = lag
            x = self._read(a, b)
            if x is None:
                self.stats["late"] += 1
                continue
            t0, nb0 = time.perf_counter(), self.stats["bursts"]
            self._handle(x, a, s0, s1, pre, post, start_ts, iuc, sid, prof_iuc, seg)
            key = f"{iuc}{'+' if self.stats['bursts'] > nb0 else '-'}"
            c = self.timing.setdefault(key, [0, 0.0])
            c[0] += 1
            c[1] += time.perf_counter() - t0
        return done

    def _detect_data(self, x, prof_iuc, t_lo, t_hi, gsym, eq_key, seg, data, burst_end=None, grant_end=None):
        """detect() for one interval. Multiple Transmit Channel mode data grant (seg): the modem
        answers every grant, one it has no data for with a segment of stuff bytes; the first
        codeword alone tells (no frame start, nothing but 0xFF), and the rest is not demodulated.
        Decoding every such grant whole (up to 255 mini-slots), 100+ grants in advance per second
        and channel, put the upstream processes 200-450 ms behind (2026-10-10: speedtest 18/0.4
        Mbit/s, against 92/20 with one channel without segments)."""
        # padding fills the whole grant: energy that stops well before the grant's end is data
        early_end = burst_end is not None and grant_end is not None and burst_end < grant_end - 4 * self.ms_samples
        if seg and not early_end:
            # only the start of the window: the matched filter would otherwise run over the
            # whole grant (70,000 samples for 255 mini-slots) before the preamble search
            pr = self.prof[prof_iuc]
            lo = max(0, int(t_lo) - 64)
            hi = min(x.size, int(t_hi + pr.burst_symbols(pr.fec_k) * self.demod.sps) + 256)
            head = self.demod.detect(x[lo:hi], prof_iuc, t_lo - lo, t_hi - lo, max_frame=pr.fec_k, grant_syms=gsym,
                                     eq_key=eq_key, seg=True, retry=False)
            if head is not None and head.frame is not None and _is_padding(head):
                head.t += lo
                self.stats["padding"] += 1
                return head
        max_frame = int(gsym * self.prof[prof_iuc].bps / 8) + 64 if data else 64
        if seg and burst_end is not None:
            # the burst ends where its energy does (a modem with little data ends its segment
            # early): demodulated up to there plus one codeword, not the whole grant. Whole, a
            # long-data grant cost 5-6 ms with a few TCP ACKs in it, and the upstream process
            # was ~90 % busy under a speedtest (2026-10-10)
            pr = self.prof[prof_iuc]
            cw = pr.fec_k + 2 * pr.fec_t
            span = (burst_end - t_lo) / self.demod.sps            # symbols from the earliest start
            max_frame = min(max_frame, int(span * pr.bps / 8) + 2 * cw)
            x = x[:min(x.size, int(burst_end + (cw * 8 / pr.bps + 64) * self.demod.sps))]
        return self.demod.detect(x, prof_iuc, t_lo, t_hi, max_frame=max_frame,
                                 grant_syms=gsym, eq_key=eq_key, seg=seg,
                                 alt_iuc=DATA_SWAP.get(prof_iuc) if data else None)

    def _handle(self, x, a, s0, s1, pre, post, start_ts, iuc, sid, prof_iuc, seg=False):
        # energy gate on mini-slot sized blocks
        blk = max(8, int(self.ms_samples / 2))
        nb = x.size // blk
        v = np.ascontiguousarray(x[:nb * blk], np.complex64).view(np.float32).reshape(nb, 2 * blk)
        p = np.einsum("ij,ij->i", v, v) / blk          # power per block (no sqrt as with abs()**2)
        lowest = float(np.partition(p, nb // 5)[nb // 5])   # 20th percentile, without interpolation
        if not self.floor:
            # never from zero-filled samples (an RX gap, or the part of the stream an upstream
            # process skips at its start): a floor of 0 made every block "hot" and every window
            # a full preamble search, for good (2026-10-10, 2 upstream channels: 100 % CPU)
            self.floor = lowest or None
            if self.floor is None:
                return
        thr = max(self.floor, 1e-12) * self.gate
        hot = np.nonzero(p > thr)[0]
        unicast = sid != mac.SID_BROADCAST
        found = []
        # (energy only outside the grant is a neighbouring burst: an unused grant)
        # (one block of grace at the start: the previous burst's tail often spills into it)
        inside = hot[(hot * blk >= s0 - a) & (hot * blk < s1 - a - blk)]
        # A unicast data grant or a request contention region with no energy of its own is
        # empty: no preamble search. The tail of the previous burst in its lead-in used to start
        # one (1-5.5 ms instead of 0.1 ms per window); with a grant in advance in every MAP
        # (~490/s, 2026-10-09) the upstream thread fell up to 1 s behind.
        empty_grant = ((unicast and iuc in DATA_IUCS) or iuc == mac.IUC_REQUEST) and not inside.size
        # clusters of energetic blocks; one that is there from the window start and ends before
        # the interval itself is the tail of the previous burst: never searched (any interval
        # type; IM windows, 1 ms of slack each side, cost 1-1.7 ms each because of these)
        clusters = []
        if hot.size and not empty_grant:
            cut = np.nonzero(np.diff(hot) > 2)[0]
            for c0, c1 in zip(np.concatenate([[0], cut + 1]), np.concatenate([cut, [hot.size - 1]])):
                if not (hot[c0] == 0 and hot[c1] * blk <= s0 - a):    # (same grace block)
                    clusters.append(hot[c0])
        burst_end = None                            # end of the energy inside the grant (samples of x)
        if seg and hot.size and iuc in DATA_IUCS:
            ing = hot[(hot * blk >= s0 - a - 2 * blk) & (hot * blk < s1 - a)]
            if ing.size:
                burst_end = (int(ing[-1]) + 2) * blk
        if clusters:
            starts = clusters
            # a cluster that begins with the tail of the previous burst is searched from the
            # start of the interval, not from that tail
            first = int((s0 - a - pre) // blk)
            starts = sorted({max(h, first) for h in starts})
            if iuc == mac.IUC_INIT_MAINT:           # energy in another modem's unicast grant: not searched
                starts = [h for h in starts if not self._in_unicast_grant(a + h * blk + blk)]
            if iuc == mac.IUC_REQUEST and self.burst_level:
                # only ranged modems send in request slots, at the level of their data bursts; a
                # cluster 15 dB below it is the image of a burst on the other upstream channel
                # (IQ crosstalk ~-27 dB around the RX LO): ~80 such "bursts"/s were searched, failed
                # and logged during a speedtest with 2 channels (2026-10-10)
                keep = []
                for h in starts:
                    peak = float(p[h:h + 4].max()) if h < p.size else 0.0
                    if peak < self.burst_level * 10 ** -1.5:
                        self.stats["crosstalk_skip"] += 1
                    else:
                        keep.append(h)
                starts = keep
            for h in starts:
                t_lo = max(s0 - a - pre, h * blk - 2 * blk)
                t_hi = min(s1 - a + post, (h + 1) * blk + blk)
                if t_hi < t_lo:
                    continue
                self.stats["searched"] += 1
                data = iuc in DATA_IUCS
                # data bursts: up to the whole grant (a 16-QAM concatenation of 36 frames was
                # 3030 bytes; a 2048-byte cap cut it short)
                gsym = (s1 - s0) / self.demod.sps if data else None
                bu = self._detect_data(x, prof_iuc, t_lo, t_hi, gsym, sid if unicast else None, seg and data, data,
                                       burst_end, s1 - a)
                g0 = s0 - a                                 # grant start in x
                if (unicast and data and bu is not None and bu.frame is None
                        and abs(bu.t - g0) > 0.25 * self.ms_samples and t_lo < g0 + 8 < t_hi + 8 * self.ms_samples):
                    # a ranged modem starts on its grant: a "burst" found well off it that does
                    # not decode is the tail of the previous burst (back-to-back grants of one
                    # modem, MTC). It was taken and the real burst never searched: ~15 segments
                    # lost in one speedtest, all "50 us early, MER 13.4" (2026-10-10)
                    b2 = self._detect_data(x, prof_iuc, g0 - 8, g0 + 8, gsym, sid, seg and data, data, burst_end, s1 - a)
                    if b2 is not None and b2.frame is not None:
                        bu = b2
                        self.stats["regrant_search"] += 1
                if bu is not None and bu.swapped:
                    self.stats["iuc_swap"] += 1
                if bu is None:
                    continue
                if iuc == mac.IUC_REQUEST and bu.frame is None and bu.rho < 0.6:
                    # a weak "preamble" on the tail of the data burst just before the request
                    # slots (ATDMA 64-QAM, 2026-10-09: ~3/s, corr 0.35-0.48, 47 us early; real bursts 0.96+)
                    self.stats["false_pre"] += 1
                    continue
                if iuc == mac.IUC_INIT_MAINT and self._in_unicast_grant(a + bu.t):
                    self.stats["im_skip"] += 1
                    continue    # a ranged modem's burst in its own grant, left to that grant
                found.append(bu)
                if unicast or iuc == mac.IUC_INIT_MAINT:
                    break
            if not found and unicast and inside.size:
                # the guard time can be shorter than an energy block (5.12 Msym/s: 128-symbol
                # mini-slots, 174-sample blocks, 147 samples between bursts): the previous burst
                # and this one then form one cluster, searched where that tail begins. A ranged
                # modem starts at the grant start: search there.
                gsym = (s1 - s0) / self.demod.sps if iuc in DATA_IUCS else None
                bu = self._detect_data(x, prof_iuc, s0 - a - pre, s0 - a + 2 * blk, gsym,
                                       sid if unicast else None, seg and bool(gsym), bool(gsym), burst_end, s1 - a)
                if bu is not None:
                    self.stats["grant_start_search"] += 1
                    found.append(bu)
            if not found and iuc in DATA_IUCS and unicast and self.ko_dir and inside.size:
                # energy in a data grant but no preamble (e.g. 16-QAM with an unknown map): keep it
                np.savez(os.path.join(self.ko_dir, f"nopre_{time.strftime('%H%M%S')}_{start_ts}.npz"),
                         iq=x, fs=self.fs, iuc=iuc, sid=sid, t=-1.0, start_sample=s0 - a,
                         grant_syms=(s1 - s0) / self.demod.sps, frame=np.zeros(0, np.uint8))
        elif not hot.size:   # quiet window: track the noise floor
            self.floor = 0.9 * self.floor + 0.1 * lowest
        if not found and unicast:
            self.on_burst(BurstEvent(iuc, sid, start_ts))
            return
        for bu in found:
            t_abs = a + bu.t
            if any(abs(t_abs - t0) < self.ms_samples for t0 in self.recent):
                continue        # already decoded from the neighbouring interval
            self.recent.append(t_abs)
            self.stats["bursts"] += 1
            self.stats["decoded" if bu.frame is not None else "undecoded"] += 1
            if bu.frame is not None and unicast and iuc in DATA_IUCS:   # level of real bursts (linear)
                lvl = 10 ** (bu.power_db / 10)
                self.burst_level = lvl if not self.burst_level else 0.95 * self.burst_level + 0.05 * lvl
            rx_tick = self.rx_tick0 + (a + bu.t) * self.decim
            rx_ts = self.tb.ts(rx_tick)
            ev = BurstEvent(iuc, sid, start_ts, bu, rx_ts - start_ts, rx_ts)
            ko = self.ko_dir if bu.frame is None and iuc in DATA_IUCS else None
            for d in filter(None, (self.save_dir, ko)):
                np.savez(os.path.join(d, f"burst_{time.strftime('%H%M%S')}_{start_ts}.npz"),
                         iq=x, fs=self.fs, iuc=iuc, sid=sid, t=bu.t, start_sample=s0 - a,
                         grant_syms=(s1 - s0) / self.demod.sps if iuc in DATA_IUCS else 0.0,
                         frame=np.frombuffer(bu.frame or b"", np.uint8))
            self.on_burst(ev)

    def _in_unicast_grant(self, n):
        """True if a burst starting at absolute RX sample n lies in a unicast grant (Station
        Maintenance, data). The Initial Maintenance search reaches up to --im-slack-us before and
        after its interval; a ranged modem answering a unicast grant placed there must not be
        measured against the IM (on 2026-10-09 an SM burst 2 ms after the IM start got a
        +2000 us correction and the modem was lost). Contention (broadcast) intervals are not
        checked: the unranged Sagem lands 174 us early, often inside the preceding REQ region."""
        ts = self.tb.ts(self.rx_tick0 + n * self.decim)
        early = 2 * self.ms_counts                  # same tolerance as process() gives unicast grants
        # newest intervals first, stopping 20 ms before (3000 entries scanned each time cost ~0.5 ms)
        for w in reversed(list(self.sched)):
            if w[1] < ts - 0.02 * TS_RATE:
                break
            if w[3] != mac.SID_BROADCAST and w[2] != mac.IUC_INIT_MAINT and w[0] - early <= ts < w[1]:
                return True
        return False

    # ------------------------------------------------------------ raw scan (diagnostics)
    def scan_raw(self, thr_db=10.0, blk=32):
        """Look for energy anywhere in the RX stream, whatever the MAP says, and report where it
        falls relative to the scheduled intervals. Tells apart 'the modem is not heard' (level,
        frequency) from 'it is heard outside the windows' (time base) and 'heard but not decoded'."""
        if self.rx_tick0 is None:
            return
        # diagnostic only: at most every 100 ms, never while behind (it cost ~0.2 core under load)
        now = time.monotonic()
        if self.raw_pos is None or self.lag_now_ms > 5:
            self.raw_pos = self.written
            return
        if now < self.raw_next:
            return
        self.raw_next = now + 0.1
        end = self.written - 4 * blk
        self.raw_pos = max(self.raw_pos, end - int(0.03 * self.fs))   # the last 30 ms of each 100
        if end - self.raw_pos < int(0.02 * self.fs):
            return
        x = self._read(self.raw_pos, end)
        if x is None:                       # fell behind: skip ahead
            self.raw_pos = end
            return
        nb = x.size // blk
        p = (np.abs(x[:nb * blk]) ** 2).reshape(nb, blk).mean(1)
        med = float(np.median(p))
        self.raw_floor = med if self.raw_floor is None else 0.9 * self.raw_floor + 0.1 * med
        hot = np.nonzero(p > max(self.raw_floor, 1e-15) * 10 ** (thr_db / 10))[0]
        nxt = self.raw_pos + nb * blk
        if hot.size:
            groups, g0, g1 = [], hot[0], hot[0]
            for h in hot[1:]:
                if h - g1 > 3:
                    groups.append((g0, g1))
                    g0 = h
                g1 = h
            groups.append((g0, g1))
            if g1 >= nb - 2:                # still going on at the end: rescan it next time
                nxt = self.raw_pos + groups[-1][0] * blk
                groups = groups[:-1]
            for g0, g1 in groups:
                self._raw_event(x, g0 * blk, (g1 + 1) * blk, p[g0:g1 + 1])
        self.raw_pos = nxt

    def _raw_event(self, x, i0, i1, p):
        dur_us = (i1 - i0) / self.fs * 1e6
        if dur_us < 40:                     # glitches (TX underflow transients...), not bursts
            self.stats["raw_glitch"] += 1
            return
        self.stats["raw"] += 1
        # The details below (FFT of 32768 points, two passes over the 3000 recent intervals) cost
        # several ms per event: only for an event that will be printed (at most 5 per second) or
        # saved. Computed for every burst, they took the upstream process 1 s behind under an
        # upload (2026-10-09).
        now = time.time()
        while self.raw_log and now - self.raw_log[0] > 1:
            self.raw_log.popleft()
        printed = len(self.raw_log) < 5
        if not printed and not self.save_dir:
            return
        sched = list(self.sched)            # snapshot: the downstream thread appends meanwhile
        n0 = self.raw_pos + i0
        ts = self.tb.ts(self.rx_tick0 + n0 * self.decim)
        pw = 10 * np.log10(p.mean())
        seg = x[i0:i1]
        # QPSK: the 4th power removes the modulation and leaves a line at 4x the carrier offset
        S = np.abs(np.fft.fft(seg.astype(np.complex128) ** 4, 1 << 15))
        fc = float(np.fft.fftfreq(1 << 15, 1 / self.fs)[np.argmax(S)] / 4)
        names = {mac.IUC_REQUEST: "REQ", mac.IUC_INIT_MAINT: "IM", mac.IUC_STATION_MAINT: "SM"}
        inside = [w for w in sched if w[0] - 2 * 256 <= ts < w[1]]
        ims = [w for w in sched if w[2] == mac.IUC_INIT_MAINT and w[0] - 2 * 256 <= ts]
        if inside:
            w = inside[-1]
            where = f"dans {names.get(w[2], w[2])} ({(ts - w[0]) / TS_RATE * 1e6:+.1f} µs / son début)"
        else:
            where = "hors de toute fenêtre"
            if sched:
                where += f" (MAP connues de {sched[0][0] / TS_RATE:.3f} à {sched[-1][1] / TS_RATE:.3f} s)"
        if not inside or inside[-1][2] != mac.IUC_INIT_MAINT:
            where += (f", {(ts - ims[-1][0]) / TS_RATE * 1e6:+.1f} µs / début du dernier IM "
                      f"({(ims[-1][1] - ims[-1][0]) / TS_RATE * 1e6:.0f} µs de long)" if ims else ", aucun IM connu")
        if printed:
            self.raw_log.append(now)
            self.log(f"[US-BRUT] énergie à t={ts / TS_RATE:.6f} s: {dur_us:.0f} µs, {pw:.1f} dBFS "
                     f"(bruit {10 * np.log10(self.raw_floor):.1f}), porteuse {fc / 1e3:+.1f} kHz, {where}")
        if self.save_dir:
            a = max(0, i0 - int(0.0005 * self.fs))
            np.savez(os.path.join(self.save_dir, f"raw_{time.strftime('%H%M%S')}_{int(ts)}.npz"),
                     iq=x[a:i1 + int(0.0005 * self.fs)], fs=self.fs, ts=ts, first=i0 - a,
                     sched=np.array([w for w in sched if abs(w[0] - ts) < TS_RATE * 0.01], dtype=np.int64))

    def noise_dbfs(self):
        return 10 * np.log10(self.floor) if self.floor else None
