"""Upstream receiver in a process of its own.

The upstream worker (MAP windows, preamble search, equalizer, decoding) used to be a thread of
mini_cmts.py. Under load it needed ~0.8 core of Python and shared the GIL with the downstream
encoder, shaper and bridge threads: on 2026-10-09 it fell up to 1 s behind (the RX buffer
length), which delayed every upstream frame and pushed the loaded latency to 1.1 s.

RemoteUpstream has the interface mini_cmts.py uses of cmts.upstream.UpstreamRx, but:
  - push() (RX thread) writes the samples into a shared-memory ring;
  - add_grants() (downstream thread) writes the MAP intervals into a shared ring;
  - a spawned process runs the real UpstreamRx on that ring and sends back its BurstEvents,
    log lines and statistics; a thread of this process hands the events to on_burst (CmtsCore),
    so the MAC layer stays where it was.
Shared control words: written samples, device tick of sample 0, processed DOCSIS time (MAP Ack),
liveness, intervals written to the ring.
"""
import collections
import multiprocessing as mp
import queue
import threading
import time
from multiprocessing import shared_memory

import numpy as np

_WRITTEN, _TICK0, _PROCESSED, _ALIVE, _GW = range(5)
_GRING = 1 << 16                     # MAP intervals in flight (start, end, IUC, SID), shared ring


class RemoteUpstream:
    def __init__(self, fs, decim, symrate, timebase, ms_counts, on_burst, us_mode, us_bits,
                 history_s=1.0, log=print, **rx_kwargs):
        self.fs, self.decim, self.tb = fs, decim, timebase
        self.on_burst = on_burst
        self.log = log
        n = int(history_s * fs)
        self._shm = shared_memory.SharedMemory(create=True, size=n * 8)
        self._ctl_shm = shared_memory.SharedMemory(create=True, size=8 * 5)
        self._g_shm = shared_memory.SharedMemory(create=True, size=8 * 4 * _GRING)
        self.buf = np.ndarray(n, np.complex64, self._shm.buf)
        self.ctl = np.ndarray(5, np.int64, self._ctl_shm.buf)
        self.ctl[:] = (0, -1, 0, 0, 0)
        self.gring = np.ndarray((_GRING, 4), np.int64, self._g_shm.buf)
        self._gw = 0
        self.rx_tick0 = None
        self.written = 0
        self.stats = collections.Counter()
        self.timing = {}
        self.lag_max_ms = 0.0
        self._noise = None
        self._params = dict(fs=fs, decim=decim, symrate=symrate, ms_counts=ms_counts, n=n,
                            us_mode=us_mode, us_bits=us_bits, history_s=history_s, rx_kwargs=rx_kwargs)
        self._ctx = mp.get_context("spawn")        # no fork of a process holding the B210
        self._q_in = self._ctx.Queue()
        self._q_out = self._ctx.Queue()
        self._proc = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ interface of UpstreamRx
    def start(self):
        """Start the worker process (once the time base is final: after the TX start time)."""
        self._params["tb"] = self.tb
        self._proc = self._ctx.Process(target=_worker_main, name="upstream",
                                       args=(self._shm.name, self._ctl_shm.name, self._g_shm.name, self._q_in, self._q_out,
                                             self._params), daemon=True)
        self._proc.start()
        threading.Thread(target=self._events, name="us-events", daemon=True).start()

    def add_grants(self, grants):
        # written straight into a shared ring (one producer: the downstream thread), published by
        # the write counter: no pickling, no extra thread. A queue made the intervals reach the
        # upstream process up to 0.8 s late while this process's GIL was busy (2026-10-09).
        for g in grants:
            self.gring[self._gw % _GRING] = g
            self._gw += 1
        self.ctl[_GW] = self._gw

    def push(self, tick, x):
        """Same as UpstreamRx.push, into the shared ring; `written` is published after the data."""
        if self.rx_tick0 is None:
            self.rx_tick0 = tick
            self.written = 0
            self.ctl[_TICK0] = int(tick)
        n0 = int(round((tick - self.rx_tick0) / self.decim))
        if n0 > self.written:
            self._write(np.zeros(min(n0 - self.written, self.buf.size), np.complex64), self.written)
            self.written = n0
        elif n0 < self.written:
            x = x[self.written - n0:]
            n0 = self.written
        self._write(x, n0)
        self.written = n0 + x.size
        self.ctl[_WRITTEN] = self.written

    def _write(self, x, n0):
        L = self.buf.size
        i = n0 % L
        k = min(x.size, L - i)
        self.buf[i:i + k] = x[:k]
        if k < x.size:
            self.buf[:x.size - k] = x[k:]

    @property
    def processed_ts(self):
        return int(self.ctl[_PROCESSED])

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
        for shm in (self._shm, self._ctl_shm, self._g_shm):
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


def _worker_main(shm_name, ctl_name, g_name, q_in, q_out, p):
    import faulthandler
    import os
    import signal
    from cmts import docsis_mac as mac
    from cmts import upstream
    # diagnostic: kill -USR1 <pid> writes the Python stacks of this process
    _dump = open(f"/tmp/usproc_{os.getpid()}.stack", "w")
    faulthandler.register(signal.SIGUSR1, file=_dump, all_threads=True)
    mac.configure_upstream(p["us_mode"], p["us_bits"])
    shm = shared_memory.SharedMemory(name=shm_name, track=False)
    ctl_shm = shared_memory.SharedMemory(name=ctl_name, track=False)
    g_shm = shared_memory.SharedMemory(name=g_name, track=False)
    buf = np.ndarray(p["n"], np.complex64, shm.buf)
    ctl = np.ndarray(5, np.int64, ctl_shm.buf)
    gring = np.ndarray((_GRING, 4), np.int64, g_shm.buf)
    gr = 0                                             # next ring entry to read
    pending = []                                       # events of one loop turn, sent together
    rx = upstream.UpstreamRx(p["fs"], p["decim"], p["symrate"], p["tb"], p["ms_counts"],
                             pending.append, history_s=p["history_s"],
                             log=lambda s: q_out.put(("log", s)), buf=buf, **p["rx_kwargs"])
    ctl[_ALIVE] = 1
    t_stats = time.monotonic() + 0.5
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
            if rx.rx_tick0 is None and ctl[_TICK0] >= 0:
                rx.rx_tick0 = int(ctl[_TICK0])
            rx.written = int(ctl[_WRITTEN])
            t0 = time.perf_counter()
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
                st = dict(rx.stats)
                st["windows_queued"] = len(rx.windows)
                q_out.put(("stats", st, rx.noise_dbfs(), rx.lag_max_ms, dict(rx.timing)))
                rx.lag_max_ms = 0.0
                rx.timing.clear()
            if not n and t1 - t0 < 0.002:
                time.sleep(0.002)
    finally:
        ctl[_ALIVE] = 0
        del buf, ctl, gring
        shm.close()
        ctl_shm.close()
        g_shm.close()
