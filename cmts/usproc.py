# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Upstream receiver in a process of its own.

The upstream worker (MAP windows, preamble search, equalizer, decoding) used to be a thread of
sdr_cmts.py. Under load it needed ~0.8 core of Python and shared the GIL with the downstream
encoder, shaper and bridge threads: on 2026-10-09 it fell up to 1 s behind (the RX buffer
length), which delayed every upstream frame and pushed the loaded latency to 1.1 s.

RemoteUpstream has the interface sdr_cmts.py uses of cmts.upstream.UpstreamRx, but:
  - push() (RX thread) writes the samples into a shared-memory ring;
  - add_grants() (downstream thread) writes the MAP intervals into a shared ring;
  - a spawned process runs the real UpstreamRx on that ring and sends back its BurstEvents,
    log lines and statistics; a thread of this process hands the events to on_burst (CmtsCore),
    so the MAC layer stays where it was.
Shared control words: written samples, device tick of sample 0, processed DOCSIS time (MAP Ack),
liveness, intervals written to the ring.

Several upstream channels (DOCSIS 3.0 upstream bonding): one wideband RX stream covering all of
them goes into one SampleRing; one RemoteUpstream (one process) per channel reads it and takes its
channel out (`ddc`: frequency shift, low-pass, decimation in C, cdsp.Ddc) into a narrowband ring
of its own, then demodulates exactly as for a single channel.
"""
import collections
import multiprocessing as mp
import queue
import threading
import time
from multiprocessing import resource_tracker, shared_memory

import numpy as np

_WRITTEN, _TICK0, _PROCESSED, _ALIVE, _GW = range(5)
_GRING = 1 << 16                     # MAP intervals in flight (start, end, IUC, SID), shared ring


DDC_TAPS = 63


def attach_shm(name):
    """Attach to a segment another process created, without registering it with the resource
    tracker, which would unlink it when this process exits. track=False is Python 3.13+; before
    (Debian 12: 3.11), the registration is skipped by hand."""
    try:
        return shared_memory.SharedMemory(name=name, track=False)
    except TypeError:
        reg = resource_tracker.register
        resource_tracker.register = lambda *a, **k: None
        try:
            return shared_memory.SharedMemory(name=name)
        finally:
            resource_tracker.register = reg


class SampleRing:
    """RX samples in shared memory: written by the RX thread (push), read by the upstream
    process(es). Control words: written samples, device tick of sample 0. The RX thread may be
    in another process (cmts/radio.py): attach=(shm name, control name, size) opens the ring
    created here; rx_tick0 and written are read from the control words, wherever push() runs."""

    def __init__(self, fs, decim, history_s=1.0, attach=None):
        self.fs, self.decim = fs, decim
        if attach is None:
            n = int(history_s * fs)
            self.shm = shared_memory.SharedMemory(create=True, size=n * 8)
            self.ctl_shm = shared_memory.SharedMemory(create=True, size=8 * 2)
        else:
            name, ctl_name, n = attach
            self.shm = attach_shm(name)
            self.ctl_shm = attach_shm(ctl_name)
        self.buf = np.ndarray(n, np.complex64, self.shm.buf)
        self.ctl = np.ndarray(2, np.int64, self.ctl_shm.buf)
        if attach is None:
            self.ctl[:] = (0, -1)
        self._tick0 = None                     # writer side (push)
        self._written = 0

    @property
    def rx_tick0(self):
        t = int(self.ctl[_TICK0])
        return None if t < 0 else t

    @property
    def written(self):
        return int(self.ctl[_WRITTEN])

    def push(self, tick, x):
        """Same as UpstreamRx.push, into the shared ring; `written` is published after the data."""
        if self._tick0 is None:
            self._tick0 = tick
            self._written = 0
            self.ctl[_TICK0] = int(tick)
        n0 = int(round((tick - self._tick0) / self.decim))
        if n0 > self._written:
            self._write(np.zeros(min(n0 - self._written, self.buf.size), np.complex64), self._written)
            self._written = n0
        elif n0 < self._written:
            x = x[self._written - n0:]
            n0 = self._written
        self._write(x, n0)
        self._written = n0 + x.size
        self.ctl[_WRITTEN] = self._written

    def _write(self, x, n0):
        L = self.buf.size
        i = n0 % L
        k = min(x.size, L - i)
        self.buf[i:i + k] = x[:k]
        if k < x.size:
            self.buf[:x.size - k] = x[k:]

    def detach(self):
        """Radio process side: close without unlinking (the creator unlinks)."""
        del self.buf, self.ctl
        for shm in (self.shm, self.ctl_shm):
            try:
                shm.close()
            except BufferError:
                pass

    def close(self):
        for shm in (self.shm, self.ctl_shm):
            try:
                shm.close()
                shm.unlink()
            except (FileNotFoundError, BufferError):
                pass


class RemoteUpstream:
    def __init__(self, fs, decim, symrate, timebase, ms_counts, on_burst, us_mode, us_bits,
                 history_s=1.0, log=print, ring=None, ddc=None, name="upstream", **rx_kwargs):
        """fs, decim: the RX stream. ring: a SampleRing shared with other channels (default: one
        of its own). ddc: dict(offset=Hz, factor=decimation, cutoff=Hz) -> this channel is
        taken out of the (wideband) ring at `offset` from its centre; fs / decim are then those
        of the ring, the receiver runs at fs / factor."""
        self.ring = ring if ring is not None else SampleRing(fs, decim, history_s)
        self._own_ring = ring is None
        self.ddc = ddc
        self.name = name
        if ddc:
            # the FIR delays its output by (taps - 1) / 2 wideband samples: narrow sample j is
            # wideband sample j * factor - 31, i.e. the narrow stream starts 31 samples earlier
            fs, decim = fs / ddc["factor"], decim * ddc["factor"]
            self._tick_shift = -(DDC_TAPS - 1) // 2 * self.ring.decim
        else:
            self._tick_shift = 0
        self.fs, self.decim, self.tb = fs, decim, timebase
        self.on_burst = on_burst
        self.log = log
        self._ctl_shm = shared_memory.SharedMemory(create=True, size=8 * 5)
        self._g_shm = shared_memory.SharedMemory(create=True, size=8 * 4 * _GRING)
        self.ctl = np.ndarray(5, np.int64, self._ctl_shm.buf)
        self.ctl[:] = (0, -1, 0, 0, 0)
        self.gring = np.ndarray((_GRING, 4), np.int64, self._g_shm.buf)
        self._gw = 0
        self._last_end = 0                         # end of the last interval granted (DOCSIS counts)
        self.stats = collections.Counter()
        self.timing = {}
        self.lag_max_ms = 0.0
        self._noise = None
        self._params = dict(fs=fs, decim=decim, symrate=symrate, ms_counts=ms_counts,
                            n=self.ring.buf.size, ring_fs=self.ring.fs, ring_decim=self.ring.decim,
                            tick_shift=self._tick_shift, ddc=ddc,
                            us_mode=us_mode, us_bits=us_bits, history_s=history_s, rx_kwargs=rx_kwargs)
        self._ctx = mp.get_context("spawn")        # no fork of a process holding the B210
        self._q_in = self._ctx.Queue()
        self._q_out = self._ctx.Queue()
        self._proc = None
        self._stop = threading.Event()

    @property
    def rx_tick0(self):
        t = self.ring.rx_tick0
        return None if t is None else t + self._tick_shift

    @property
    def written(self):
        return self.ring.written

    # ------------------------------------------------------------ interface of UpstreamRx
    def start(self):
        """Start the worker process (once the time base is final: after the TX start time)."""
        self._params["tb"] = self.tb
        self._proc = self._ctx.Process(target=_worker_main, name=self.name,
                                       args=(self.ring.shm.name, self.ring.ctl_shm.name, self._ctl_shm.name,
                                             self._g_shm.name, self._q_in, self._q_out, self._params), daemon=True)
        self._proc.start()
        threading.Thread(target=self._events, name="us-events", daemon=True).start()

    def add_grants(self, grants):
        # written straight into a shared ring (one producer: the downstream thread), published by
        # the write counter: no pickling, no extra thread. A queue made the intervals reach the
        # upstream process up to 0.8 s late while this process's GIL was busy (2026-10-09).
        for g in grants:
            self.gring[self._gw % _GRING] = g
            self._gw += 1
            self._last_end = max(self._last_end, g[1])
        self.ctl[_GW] = self._gw

    def push(self, tick, x):
        """RX samples into the ring (once per ring: the first channel when it is shared)."""
        self.ring.push(tick, x)

    @property
    def processed_ts(self):
        return int(self.ctl[_PROCESSED])

    @property
    def lag_now_ms(self):
        """How far behind the RX stream the worker is (ms): DOCSIS time of the samples received,
        or of the end of the last interval granted if earlier, minus the end of the last interval
        it handled. Computed here, not by the worker: a worker short of CPU would not report."""
        t0 = self.ring.rx_tick0
        if t0 is None or not self._last_end:
            return 0.0
        rec = self.tb.ts(t0 + self.ring.written * self.ring.decim)
        return max(0.0, (min(rec, self._last_end) - self.processed_ts) / 10.24e3)

    def process(self):          # done by the worker process
        return 0

    def scan_raw(self):
        pass

    def noise_dbfs(self):
        return self._noise

    def sample_of_ts(self, ts):
        return (self.tb.tick(ts) - self.rx_tick0) / self.decim

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None else None

    def alive(self):
        return self._proc is not None and self._proc.is_alive()

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            self._q_in.put(("stop",))
            self._proc.join(3)
            if self._proc.is_alive():
                self._proc.terminate()
        if self._own_ring:
            self.ring.close()
        for shm in (self._ctl_shm, self._g_shm):
            try:
                shm.close()
                shm.unlink()
            except (FileNotFoundError, BufferError):
                pass

    # ------------------------------------------------------------ results from the worker
    def _events(self):
        while not self._stop.is_set():
            try:
                kind, *rest = self._q_out.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                return
            if kind == "bursts":
                for ev in rest[0]:
                    self.on_burst(ev)
            elif kind == "log":
                self.log(rest[0])
            elif kind == "stats":
                stats, noise, lag, timing = rest
                self.stats, self._noise = collections.Counter(stats), noise
                self.lag_max_ms = max(self.lag_max_ms, lag)
                for k, (n, t) in timing.items():
                    c = self.timing.setdefault(k, [0, 0.0])
                    c[0] += n
                    c[1] += t


def _worker_main(ring_name, rctl_name, ctl_name, g_name, q_in, q_out, p):
    import faulthandler
    import os
    import signal
    from cmts import cdsp
    from cmts import docsis_mac as mac
    from cmts import upstream
    # diagnostic: kill -USR1 <pid> writes the Python stacks of this process
    _dump = open(f"/tmp/usproc_{os.getpid()}.stack", "w")
    faulthandler.register(signal.SIGUSR1, file=_dump, all_threads=True)
    # Ctrl+C (the terminal signals the whole process group) and systemctl stop are for the MAC
    # process, which stops this one in order: interrupted here, the radio aborted inside UHD and
    # the MAC waited for it until systemd killed everything (2026-10-15)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    mac.configure_upstream(p["us_mode"], p["us_bits"])
    shm = attach_shm(ring_name)
    rctl_shm = attach_shm(rctl_name)
    ctl_shm = attach_shm(ctl_name)
    g_shm = attach_shm(g_name)
    ring = np.ndarray(p["n"], np.complex64, shm.buf)
    rctl = np.ndarray(2, np.int64, rctl_shm.buf)
    ctl = np.ndarray(5, np.int64, ctl_shm.buf)
    gring = np.ndarray((_GRING, 4), np.int64, g_shm.buf)
    gr = 0                                             # next ring entry to read
    pending = []                                       # events of one loop turn, sent together
    ddc = p["ddc"]
    if ddc:                                            # this channel out of the wideband ring
        dd = cdsp.Ddc(p["ring_fs"], ddc["offset"], ddc["factor"], ddc["cutoff"], ntaps=DDC_TAPS)
        buf = np.zeros(int(p["history_s"] * p["fs"]), np.complex64)
    else:
        buf = ring
    rx = upstream.UpstreamRx(p["fs"], p["decim"], p["symrate"], p["tb"], p["ms_counts"],
                             pending.append, history_s=p["history_s"],
                             log=lambda s: q_out.put(("log", s)), buf=buf, **p["rx_kwargs"])
    ctl[_ALIVE] = 1
    t_stats = time.monotonic() + 0.5
    debug = bool(os.environ.get("SDR_CMTS_US_DEBUG"))     # diagnostic: ring/window positions every 2 s
    t_debug = 0.0
    # garbage collector pauses, per generation, in the timing statistics (SDR_CMTS_TIMING)
    import gc
    gc_t0 = [0.0]

    def gc_cb(phase, info):
        if phase == "start":
            gc_t0[0] = time.perf_counter()
        else:
            c = rx.timing.setdefault(f"gc{info['generation']}", [0, 0.0])
            c[0] += 1
            c[1] += time.perf_counter() - gc_t0[0]
            m = rx.timing.setdefault(f"gc{info['generation']}_max", [1, 0.0])
            m[1] = max(m[1], time.perf_counter() - gc_t0[0])
    gc.callbacks.append(gc_cb)
    # diagnostic: SDR_CMTS_PROFILE_US=<file> profiles this process (pstats <file>.<pid>, every 20 s)
    prof_path = os.environ.get("SDR_CMTS_PROFILE_US")
    prof = None
    if prof_path:
        import cProfile
        prof = cProfile.Profile()
        prof.enable()
        t_dump = time.monotonic() + 20
    ddc_stop = threading.Event()
    ddc_nw = [0]                                       # narrow samples in buf, published by ddc_loop
    ddc_valid = [DDC_TAPS]                             # first narrow sample past a skip and the FIR's warm-up

    def ddc_loop():
        """The DDC in a thread of its own (2026-10-11): its C call releases the GIL, so it runs
        beside the demodulation. 6 DS + 2 US on shared cores: one thread doing both was busy
        72 % and waiting for a core 25 % of the time under upload, 0.1-2 s behind."""
        dpos = nw = 0
        L, Ln = ring.size, buf.size
        while not ddc_stop.is_set():
            written = int(rctl[_WRITTEN])
            if written - dpos > L // 2:                # far behind: skip, keep the indices right
                skip = (written - L // 4 - dpos) // ddc["factor"] * ddc["factor"]
                dpos += skip
                nw += skip // ddc["factor"]
                rx.stats["ddc_skipped"] += skip
                # buf keeps zeros over the skipped part, and the FIR restarts from them: no
                # window is read before this (a noise floor taken there stuck at -270 dBFS)
                ddc_valid[0] = nw + DDC_TAPS
            if dpos >= written:
                time.sleep(0.001)
                continue
            t0 = time.perf_counter()
            while dpos < written:
                i = dpos % L
                k = min(written - dpos, L - i, 1 << 16)
                y = dd.process(ring[i:i + k])
                j = nw % Ln
                m = min(y.size, Ln - j)
                buf[j:j + m] = y[:m]
                buf[:y.size - m] = y[m:]
                nw += y.size
                dpos += k
            ddc_nw[0] = nw                             # once the samples are in buf
            c = rx.timing.setdefault("ddc", [0, 0.0])
            c[0] += 1
            c[1] += time.perf_counter() - t0
    ddc_th = None
    if ddc:
        ddc_th = threading.Thread(target=ddc_loop, name="ddc", daemon=True)
        ddc_th.start()
    try:
        while True:
            try:
                while True:
                    msg = q_in.get_nowait()
                    if msg[0] == "stop":
                        return
            except queue.Empty:
                pass
            gw = int(ctl[_GW])
            if gw > gr:
                i0, i1 = gr % _GRING, gw % _GRING
                rows = gring[i0:i1] if i0 < i1 else np.concatenate([gring[i0:], gring[:i1]])
                rx.add_grants([tuple(int(v) for v in r) for r in rows])
                gr = gw
            if rx.rx_tick0 is None and rctl[_TICK0] >= 0:
                rx.rx_tick0 = int(rctl[_TICK0]) + p["tick_shift"]
            t0 = time.perf_counter()
            rx.written = ddc_nw[0] if ddc else int(rctl[_WRITTEN])
            if ddc:
                rx.valid_from = ddc_valid[0]
            n = rx.process()
            t1 = time.perf_counter()
            rx.scan_raw()
            c = rx.timing.setdefault("scan_raw", [0, 0.0])
            c[0] += 1
            c[1] += time.perf_counter() - t1
            if pending:
                q_out.put(("bursts", list(pending)))
                pending.clear()
            ctl[_PROCESSED] = int(rx.processed_ts)
            if time.monotonic() > t_stats:
                t_stats = time.monotonic() + 0.5
                if debug and time.monotonic() > t_debug:
                    t_debug = time.monotonic() + 2.0
                    w0 = rx.windows[0] if rx.windows else None
                    s_w = None if w0 is None or rx.rx_tick0 is None else rx.sample_of_ts(w0[0])
                    q_out.put(("log", f"[US-DEBUG {ddc['offset'] / 1e6 if ddc else 0:+.1f} MHz] tick0 anneau "
                               f"{int(rctl[_TICK0])} récepteur {rx.rx_tick0} | écrits large {int(rctl[_WRITTEN])} "
                               f"étroit {rx.written} | 1re fenêtre {None if s_w is None else int(s_w)} "
                               f"({len(rx.windows)} en file) | traité ts {rx.processed_ts} | "
                               f"seuil {rx.floor} | en retard {rx.stats['late']}"))
                st = dict(rx.stats)
                st["windows_queued"] = len(rx.windows)
                q_out.put(("stats", st, rx.noise_dbfs(), rx.lag_max_ms, dict(rx.timing)))
                rx.lag_max_ms = 0.0
                rx.timing.clear()
            if prof and time.monotonic() > t_dump:
                prof.dump_stats(f"{prof_path}.{os.getpid()}")
                prof.enable()                          # dump_stats() disables it
                t_dump = time.monotonic() + 20
            if not n and t1 - t0 < 0.002:
                time.sleep(0.002)
    finally:
        ddc_stop.set()
        if ddc_th is not None:
            ddc_th.join(1.0)
        ctl[_ALIVE] = 0
        del ring, rctl, ctl, gring, buf
        shm.close()
        rctl_shm.close()
        ctl_shm.close()
        g_shm.close()
