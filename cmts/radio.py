"""The B210 in a process of its own (mini_cmts.py --radio-process).

With every thread in one process, the transmit chain (J.83 encoding, shaping, timed send) and the
RX reads shared the GIL with the MAC layer, the bridge and the upstream event threads. On the
Proxmox host (i5-9500, Python 1.4x slower than on the laptop) 3 downstream channels under a
speedtest left the encoder/shaper with ~1 ms of margin: TX blocks dropped, 66-109 ms stalls, the
modem losing its secondary channels (2026-10-10), with the process using ~1.5 of its 3 cores.

Split:
  - this process (MAC): the downstream muxes build the TS packets of every channel, block by
    block, into a PacketRing in shared memory (semaphores: a full ring blocks the MAC encoder
    thread without holding its GIL);
  - the radio process: the USRP, J.83 encoders (one per channel, their state lives there),
    shaping / channel synthesis, the timed send loop with its local catch-up, the RX loop writing
    straight into the upstream SampleRing (shared memory already read by the upstream
    processes), the TX watchdog. Log lines and the TX part of the status line come back through
    a queue.
The DOCSIS time base is the same: TX sample 0 goes out at tick t_start * master clock, the radio
process starts both streams exactly as the single-process loop did.

Several B210s (mini_cmts.py --ds-radios 2): one radio process per device, each with its own ring
of TS blocks (its share of the bonded channels, adjacent in frequency). The first one (primary)
also receives the upstream and publishes `origin`, the host time of its device time 0; the others
(p["follow"]) transmit only, set their device time on that origin and send block k with the same
timestamp as the primary's block k (the blocks they get before knowing it are dropped, but
counted). With a common 10 MHz reference (p["clock_source"] = "external") the devices then never
drift; the residual offset is the host's set_time_now jitter (~0.1-1 ms), within the modem's
resequencing window (DSID wait time 10 ms).

Without one, each B210 runs on its own oscillator (a few ppm apart): a follower's channels would
slide against the primary's by that much (1 ms every few minutes) until the modem's resequencing
gave up. Every radio fits its device time against the host's monotonic clock (ClockFit, common
to all processes); the primary publishes its line in shared memory (`clock`), and a follower
predicts, for each block, the time of its own clock at which the primary puts the same block on
the air. When its schedule is off by more than 3/4 of a J.83-A randomization group (8 TS packets,
~235 us), it inserts a group of null packets into the block, or drops one (the one with the most
null packets): the stream stays a valid J.83-A stream (interleaver and randomizer aligned on 8
packets, no phase or symbol timing jump), only its content moves by one group.
"""
import collections
import multiprocessing as mp
import os
import queue
import threading
import time
from multiprocessing import shared_memory

import numpy as np

from cmts.usproc import attach_shm

TS_LEN = 188
GROUP = 8                     # TS packets per J.83-A randomization cycle (and 8 x 204 = 136 x 12 bytes)
NULL_TS = np.frombuffer(bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * (TS_LEN - 4), np.uint8)


class ClockFit:
    """Device time against the host's monotonic clock (time.perf_counter, the same in every
    process): a straight line through the readings of the last `window_s` seconds.
    model: (host time of reference, device time then, device seconds per host second)."""

    def __init__(self, window_s=120.0, min_points=10, reject_s=500e-6):
        self.window_s, self.min_points, self.reject_s = window_s, min_points, reject_s
        self.pts = collections.deque()
        self.model = None
        self.rejected = 0                 # readings far off the line (in a row)

    def add(self, host, dev):
        """A reading; one more than reject_s off the current line is left out (right after
        set_time_now a B210 still returned its former time once: 0.72 s instead of 0.20,
        2026-10-15), and 5 such in a row start the line again (the clock was set)."""
        if self.model is not None:
            h0, d0, k = self.model
            if abs(dev - (d0 + k * (host - h0))) > self.reject_s:
                self.rejected += 1
                if self.rejected < 5:
                    return
                self.pts.clear()
                self.model = None
            self.rejected = 0
        elif self.pts and abs((dev - self.pts[-1][1]) - (host - self.pts[-1][0])) > self.reject_s:
            self.pts.clear()              # (no line yet: keep only readings that follow each other)
        self.pts.append((host, dev))
        while host - self.pts[0][0] > self.window_s:
            self.pts.popleft()
        if len(self.pts) >= self.min_points:
            a = np.array(self.pts)
            k, c = np.polyfit(a[:, 0] - a[-1, 0], a[:, 1], 1)
            self.model = (float(a[-1, 0]), float(c), float(k))


def follower_time(primary, follower, t_primary):
    """The time of the follower's device clock when the primary's reads t_primary (models of
    ClockFit), through the host clock."""
    hp, dp, kp = primary
    hf, df, kf = follower
    return df + kf * (hp + (t_primary - dp) / kp - hf)


def group_action(err_s, group_s, hyst=0.75):
    """+1: insert a group of null packets (the block would go out more than hyst groups early),
    -1: drop a group (that late), 0: none. err_s: wanted minus scheduled time of the block."""
    if err_s > hyst * group_s:
        return 1
    if err_s < -hyst * group_s:
        return -1
    return 0


def regroup(blk, action):
    """blk (channels, packets, 188) with a group of null packets in front (+1), or without the
    group of GROUP packets holding the most null packets (-1)."""
    if action > 0:
        nulls = np.broadcast_to(NULL_TS, (blk.shape[0], GROUP, TS_LEN))
        return np.concatenate([nulls, blk], axis=1)
    if action < 0 and blk.shape[1] >= 2 * GROUP:
        pid = ((blk[:, :, 1].astype(np.int32) & 0x1F) << 8) | blk[:, :, 2]
        null = (pid == 0x1FFF).reshape(blk.shape[0], -1, GROUP).sum(axis=(0, 2))
        g = int(np.argmax(null))
        return np.concatenate([blk[:, :g * GROUP], blk[:, (g + 1) * GROUP:]], axis=1)
    return blk


class PacketRing:
    """TS blocks (channels x block x 188 bytes) in shared memory, one producer (the MAC encoder
    thread), one consumer (the radio process). `free` counts empty slots, `full` filled ones."""

    def __init__(self, nch, block, slots, ctx=None, attach=None):
        self.shape = (nch, block, TS_LEN)
        self.slots = slots
        size = slots * nch * block * TS_LEN
        if attach is None:
            ctx = ctx or mp.get_context("spawn")
            self.shm = shared_memory.SharedMemory(create=True, size=size)
            self.seq_shm = shared_memory.SharedMemory(create=True, size=8 * slots)
            self.free, self.full = ctx.Semaphore(slots), ctx.Semaphore(0)
            self._own = True
        else:
            name, seq_name, self.free, self.full = attach
            self.shm = attach_shm(name)
            self.seq_shm = attach_shm(seq_name)
            self._own = False
        self.buf = np.ndarray((slots,) + self.shape, np.uint8, self.shm.buf)
        # each slot's block number (since the MAC's first block): a follower radio times block k
        # from k itself, so blocks it never got (not started yet, ring full) cannot shift it
        self.seqs = np.ndarray((slots,), np.int64, self.seq_shm.buf)
        self.w = self.r = 0                    # local indices (one writer, one reader)
        self.last_seq = -1

    def handle(self):
        return self.shm.name, self.seq_shm.name, self.free, self.full

    def put(self, pks, stop, seq=0, wait=True):
        """pks: one (block, 188) array per channel, seq: the block's number. False if `stop` was
        set while waiting; wait=False: None (block not queued) when the ring is full."""
        while not self.free.acquire(timeout=0.2 if wait else 0):
            if not wait:
                return None
            if stop.is_set():
                return False
        i = self.w % self.slots
        slot = self.buf[i]
        for k, p in enumerate(pks):
            slot[k] = p
        self.seqs[i] = seq
        self.w += 1
        self.full.release()
        return True

    def get(self, timeout=0.2):
        if not self.full.acquire(timeout=timeout):
            return None
        i = self.r % self.slots
        out = self.buf[i].copy()
        self.last_seq = int(self.seqs[i])
        self.r += 1
        self.free.release()
        return out

    def close(self):
        del self.buf
        del self.seqs
        for shm in (self.shm, self.seq_shm):
            try:
                shm.close()
                if self._own:
                    shm.unlink()
            except (FileNotFoundError, BufferError):
                pass


class RadioProcess:
    """Main-process side: starts the radio process, relays its messages."""

    def __init__(self, params, sample_ring, nch, block, slots, log, origin=None, name="radio", clock=None):
        self._ctx = mp.get_context("spawn")      # no fork of a process holding the GIL's threads
        self.name = name
        self._origin = origin                  # shared Value: host time of the primary's device time 0
        self._clock = clock                    # shared Array: the primary's ClockFit model (see radio_main)
        self.pk = PacketRing(nch, block, slots, ctx=self._ctx)
        self.params = params
        self.log = log
        self._q = self._ctx.Queue()
        self._stop = self._ctx.Event()
        self.started = threading.Event()
        self.failed = None
        self.dropped = self.dropped_logged = 0   # blocks a follower missed (ring full)
        self.wall0 = None                      # Unix time of TX sample 0 on the air
        self._agg = {}                         # TX/RX counters since the last status line
        self.last = {}
        self._lock = threading.Lock()
        sr = sample_ring
        self._ring = (sr.shm.name, sr.ctl_shm.name, sr.buf.size, sr.fs, sr.decim) if sr is not None else None
        self._proc = None

    def start(self):
        self._proc = self._ctx.Process(target=radio_main, name=self.name,
                                       args=(self.params, self.pk.handle(), self._ring, self._q, self._stop,
                                             self._origin, self._clock), daemon=True)
        self._proc.start()
        threading.Thread(target=self._events, name="radio-events", daemon=True).start()

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None else None

    def alive(self):
        return self._proc is not None and self._proc.is_alive()

    def _events(self):
        while True:
            try:
                kind, *rest = self._q.get(timeout=0.5)
            except queue.Empty:
                if self._proc is not None and not self._proc.is_alive():
                    if not self.started.is_set():
                        self.failed = self.failed or f"processus radio arrêté (code {self._proc.exitcode})"
                        self.started.set()
                    return
                continue
            except (EOFError, OSError):
                return
            if kind == "log":
                self.log(rest[0])
            elif kind == "status":
                st = rest[0]
                with self._lock:
                    agg = self._agg
                    agg.update(U=st["U"], L=st["L"], L_ms=st["L_ms"], O=st["O"])
                    agg["gap_ms"] = max(agg.get("gap_ms", 0.0), st["gap_ms"])
                    agg["margin_ms"] = min(agg.get("margin_ms", float("inf")), st["margin_ms"])
            elif kind == "started":
                self.wall0 = rest[0]
                self.started.set()
            elif kind == "fatal":
                self.failed = rest[0]
                self.started.set()

    def status(self):
        """TX part of the status line (as the single-process loop prints it), since the last call."""
        with self._lock:
            agg, self._agg = self._agg, {}
        self.last = dict(agg)                  # for the status page (mini_cmts.py web_snapshot)
        if "U" not in agg:
            return "radio: pas encore d'état | "
        m = agg.get("margin_ms", float("inf"))
        marg = f"marge TX min {m:.1f} ms | " if m < float("inf") else ""
        # counters not reset by a status line keep their totals
        self._agg.update(U=agg["U"], L=agg["L"], L_ms=agg["L_ms"], O=agg["O"])
        return (f"U {agg['U']} L {agg['L']} (~{agg['L_ms']:.0f} ms) RX écart max {agg.get('gap_ms', 0.0):.0f} ms "
                f"O {agg['O']} | {marg}")

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            self._proc.join(3)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(1)
        self.pk.close()


class TxChain:
    """TS packets of every channel (block) -> baseband samples: J.83 encoders (state kept), then
    one channel: RRC shaping, conjugation, scale and clip, as mux.shape in the single-process loop;
    several: cdsp.ChannelSynth per channel (shaping + shift + sum in C), split into groups computed
    by helper threads from 3 channels on, each thread encoding its own channels too (with the AVX2
    synthesis, encoding all 5 channels in the calling thread left it the bottleneck at 55.6 MS/s).
    p: the radio parameters (qams, steps, sps, invert, scale, synth_threads)."""

    def __init__(self, p, stop):
        from cmts import cdsp
        from cmts.j83a import J83AEncoder, PulseShaper
        self._cdsp = cdsp
        self.p, self.stop = p, stop
        self.nch = nch = len(p["steps"])
        self.encs = [J83AEncoder(qam) for qam in p["qams"]]
        self.shaper1 = PulseShaper(p["sps"]) if nch == 1 else None
        taps = PulseShaper(p["sps"]).phases
        self.synths = [cdsp.ChannelSynth(taps, p["steps"][k], p["invert"]) for k in range(nch)]
        self.scale = p["scale"] / np.sqrt(nch)
        self.bits = {64: 6, 256: 8}[p["qams"][0]]           # symbols per block = 204 * 8 / bits per packet
        ngroups = 1 if nch <= 2 else min(nch, p["synth_threads"])
        self.groups = [list(range(nch))[g::ngroups] for g in range(ngroups)]
        self.helpers = []
        for g in self.groups[1:]:
            h = dict(ch=g, todo=queue.Queue(maxsize=1), done=queue.Queue(maxsize=1))
            self.helpers.append(h)
            threading.Thread(target=self._help_loop, args=(h,), name="synth", daemon=True).start()

    def _help_loop(self, h):
        while not self.stop.is_set():
            try:
                blk, acc = h["todo"].get(timeout=0.2)
            except queue.Empty:
                continue
            for k in h["ch"]:
                self.synths[k].add(self.encs[k].encode(blk[k]), acc)
            h["done"].put(acc)

    def __call__(self, blk):
        p = self.p
        if self.nch == 1:
            x = self.shaper1.process(self.encs[0].encode(blk[0]))
            if p["invert"]:
                x = np.conj(x)
            x = x * p["scale"]
            np.clip(x.real, -1, 1, out=x.real)
            np.clip(x.imag, -1, 1, out=x.imag)
            return x
        n = len(blk[0]) * 204 * 8 // self.bits * p["sps"]
        for h in self.helpers:
            h["todo"].put((blk, np.zeros(n, np.complex64)))
        x = np.zeros(n, np.complex64)
        for k in self.groups[0]:
            self.synths[k].add(self.encs[k].encode(blk[k]), x)
        for h in self.helpers:
            self._cdsp.add_into(x, h["done"].get())
        return self._cdsp.scale_clip(x, self.scale)


def radio_main(p, pk_handle, ring_handle, q, stop_ev, origin=None, clock=None):
    """The radio process: see the module docstring. p: dict built by mini_cmts.py. origin: shared
    Value (host time of the primary device's time 0), set by the primary, read by a follower.
    clock: shared Array (host ref, device time, rate, valid, samples of the primary's block 0 past
    seq * block), the primary's ClockFit model, published by the primary, read by a follower."""
    import faulthandler
    import signal

    def log(s):
        q.put(("log", s))
    _dump = open(f"/tmp/radio_{os.getpid()}.stack", "w")
    faulthandler.register(signal.SIGUSR1, file=_dump, all_threads=True)   # kill -USR1: stacks
    # Ctrl+C (the terminal signals the whole process group) and systemctl stop are for the MAC
    # process, which stops this one in order: interrupted here, the radio aborted inside UHD and
    # the MAC waited for it until systemd killed everything (2026-10-15)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    os.environ.setdefault("UHD_LOG_FASTPATH_DISABLE", "1")
    from cmts.sdrdev import open_radio
    from cmts.stallwatch import StallWatch
    from cmts.usproc import SampleRing

    pk = PacketRing(*p["pk_shape"], attach=pk_handle)
    has_rx = ring_handle is not None and p.get("rx", True)
    follow = bool(p.get("follow"))
    tag = p.get("tag", "RADIO")
    ring = None
    if has_rx:
        shm_name, ctl_name, n, rfs, rdecim = ring_handle
        ring = SampleRing(rfs, rdecim, attach=(shm_name, ctl_name, n))
    mcr, fs_tx = p["mcr"], p["fs_tx"]
    try:
        dev = open_radio(p, has_rx)
    except Exception as e:                     # noqa: BLE001 - reported to the main process
        q.put(("fatal", f"{'B210' if p.get('driver', 'uhd') == 'uhd' else 'SDR'} : {e}"))
        return
    log(f"[{tag}] {dev.describe()}")
    if dev.warning():
        log(dev.warning())
    err = dev.check()
    if err:
        q.put(("fatal", err))
        return
    dev.open_streams()
    if getattr(dev, "note", None):
        log(f"[{tag}] {dev.note}")

    stop = threading.Event()
    q_tx = queue.Queue(maxsize=p["tx_queue"])
    counters = dict(U=0, L=0, O=0)
    n_sent = [0]

    def air_s():                               # seconds of downstream on the air (log times)
        return n_sent[0] / fs_tx

    # ------------------------------------------------------------------ encode + shape
    chain = TxChain(p, stop)
    n_nom = p["pk_shape"][1] * 204 * 8 // chain.bits * p["sps"]      # samples of a block as the MAC sends it
    g_samples = GROUP * 204 * 8 // chain.bits * p["sps"]             # samples of a group of 8 TS packets
    fit = ClockFit()                                                 # this device against the host
    adj = [0]                          # follower: samples its blocks are moved by (groups inserted - dropped)
    regr = dict(ins=0, drop=0, err=None, t_log=time.monotonic() + 30)
    regroup_on = follow and clock is not None and not p["tx_untimed"]

    def realign(seq, blk):
        """Follower: the block with a group of null packets more or less when the primary's clock
        says it would go out more than 3/4 of a group early or late (module docstring)."""
        if not regroup_on or clock[3] <= 0 or fit.model is None:
            return blk
        primary = (clock[0], clock[1], clock[2])
        want = follower_time(primary, fit.model, p["t_start"] + (seq * n_nom + clock[4]) / fs_tx)
        err = want - (p["t_start"] + (seq * n_nom + adj[0]) / fs_tx)
        regr["err"] = err
        act = group_action(err, g_samples / fs_tx)
        if act:
            blk = regroup(blk, act)
            adj[0] += act * g_samples
            regr["ins" if act > 0 else "drop"] += 1
        if time.monotonic() > regr["t_log"]:
            regr["t_log"] = time.monotonic() + 60
            log(f"[{tag}] recalage sur la principale : écart {err * 1e6:+.0f} µs, dérive "
                f"{(fit.model[2] / clock[2] - 1) * 1e6:+.3f} ppm, groupes +{regr['ins']} / -{regr['drop']} "
                f"(décalage cumulé {adj[0] / fs_tx * 1e3:+.2f} ms)")
        return blk

    slow = dict(t_log=0.0)

    def slow_log(what):
        """A pipeline step far over its usual time, at most once a second (which side waited)."""
        now = time.monotonic()
        if now - slow["t_log"] > 1.0:
            slow["t_log"] = now
            log(f"[{tag}] !! {what}")

    def encoder():
        while not stop.is_set():
            t0 = time.perf_counter()
            blk = pk.get()
            if blk is None:
                continue
            t1 = time.perf_counter()
            seq = pk.last_seq
            a_k = adj[0]
            x = chain(realign(seq, blk))
            t2 = time.perf_counter()
            if t2 - t1 > 3 * p["blk_ms"] * 1e-3 or (t1 - t0 > 10e-3 and seq > 100):
                slow_log(f"encodeur : bloc {seq} attendu {(t1 - t0) * 1e3:.0f} ms (MAC), "
                         f"calculé en {(t2 - t1) * 1e3:.0f} ms")
            while not stop.is_set():
                try:
                    q_tx.put((seq, x, a_k), timeout=0.2)
                    break
                except queue.Full:
                    pass

    last_push = [time.monotonic()]

    def rx_loop():
        buf = np.zeros((1, dev.rx_spp * 8), dtype=np.complex64)
        while not stop.is_set():
            n, tick, overflow = dev.recv(buf)
            if overflow:
                counters["O"] += 1
                log(f"[RX] !! overflow à t={air_s():.2f} s (échantillons montants perdus)")
            if n:
                ring.push(tick, buf[0, :n])
                now_p = time.monotonic()
                counters["rx_gap_ms"] = max(counters.get("rx_gap_ms", 0.0), (now_p - last_push[0]) * 1e3)
                last_push[0] = now_p

    def async_loop():
        while not stop.is_set():
            ev = dev.tx_event(0.2)
            if ev:
                counters[ev] += 1

    def watch_stop():
        stop_ev.wait()
        stop.set()

    spp = dev.tx_spp

    def l_ms(n):
        return n * spp / fs_tx * 1e3
    wd = StallWatch(log, counters, air_s, l_ms=l_ms)
    workers = [threading.Thread(target=f, daemon=True) for f in (encoder, async_loop)]
    for t in workers + [threading.Thread(target=watch_stop, daemon=True)]:
        t.start()
    t_start = p["t_start"]
    if follow:
        # a follower: the blocks it gets before the primary's origin is known are dropped but
        # counted (n_sent), so that its block k keeps the timestamp of the primary's block k
        while origin is None or origin.value <= 0:
            if stop.is_set():
                return
            try:
                q_tx.get(timeout=0.05)          # (dropped: the block numbers keep the timing)
            except queue.Empty:
                pass
        h0 = time.time()
        dev.set_time(h0 - origin.value)
        h1 = time.time()
        wall0 = origin.value + t_start
        log(f"[{tag}] horloge alignée sur la radio principale (appel {1e3 * (h1 - h0):.2f} ms)")
    else:
        while q_tx.qsize() < p["tx_queue"]:
            if stop.is_set():
                return
            time.sleep(0.005)
        h0 = time.time()
        dev.set_time(0.0)
        h1 = time.time()
        if origin is not None:
            origin.value = (h0 + h1) / 2           # host time of device time 0, for the followers
        wall0 = time.time() + t_start
        if has_rx:
            dev.start_rx(t_start)
            workers.append(threading.Thread(target=rx_loop, daemon=True))
            workers[-1].start()
    q.put(("started", wall0))
    first = True
    tick0 = round(t_start * mcr)
    ticks_per_sample = round(mcr / fs_tx)
    usb_ms = p["tx_frames"] * spp / fs_tx * 1e3
    log(f"[{tag}] émission (processus séparé, pid {os.getpid()}) ; latence downstream "
        f"~{(p['tx_queue'] + 3) * p['blk_ms'] + usb_ms:.0f} ms (T3 du modem: 200 ms), réserve USB {usb_ms:.0f} ms")
    t_last = time.time()
    wd.last = time.perf_counter()
    wd.armed_at = wd.last + t_start + 0.5
    sync = (-1e9, 0.0)
    if follow:                                 # on the primary's timeline already: catch up at once
        p0 = time.perf_counter()
        sync = (p0, dev.time_now())
        fit.add(*sync)
    t_skip_log = 0.0
    p_off = 0                                  # primary: samples between its n_sent and seq * block
    catching_up = False
    m_min = float("inf")
    threading.Thread(target=wd.run, args=(stop,), name="watchdog", daemon=True).start()
    try:
        while not stop.is_set():
            t_get = time.perf_counter()
            try:
                seq, x, a_k = q_tx.get(timeout=0.2)
            except queue.Empty:
                continue
            t_got = time.perf_counter()
            if follow:                         # block seq of the primary's timeline (+ realignment)
                n_sent[0] = seq * n_nom + a_k
            elif clock is not None:
                p_off = n_sent[0] - seq * n_nom
            # local catch-up with hysteresis, as in the single-process loop (mini_cmts.py)
            if sync[0] > 0 and not p["tx_untimed"]:
                ahead = t_start + n_sent[0] / fs_tx - (sync[1] + time.perf_counter() - sync[0])
                if ahead < -1e-3:
                    catching_up = True
                elif ahead > 8e-3:
                    catching_up = False
                if catching_up:
                    n_sent[0] += x.size
                    counters["skip_ms"] = counters.get("skip_ms", 0.0) + x.size / fs_tx * 1e3
                    if time.time() - t_skip_log > 1.0:
                        t_skip_log = time.time()
                        log(f"[TX] !! émission en retard de {-ahead * 1e3:.0f} ms : blocs jetés avant envoi pour "
                            f"rattraper l'horaire ({counters['skip_ms']:.0f} ms jetés au total)")
                    continue
            tick = tick0 + n_sent[0] * ticks_per_sample if (n_sent[0] == 0 or first or not p["tx_untimed"]) else None
            n_sent[0] += x.size
            dev.send(x, tick, first)
            t_sent = time.perf_counter()
            if (t_got - t_get > 10e-3 or t_sent - t_got > 10e-3) and n_sent[0] > (t_start + 2.0) * fs_tx:
                slow_log(f"envoi : bloc {seq} attendu {(t_got - t_get) * 1e3:.0f} ms (encodeur), "
                         f"envoyé en {(t_sent - t_got) * 1e3:.0f} ms (USB)")
            first = False
            wd.feed()
            now_p = time.perf_counter()
            if now_p - sync[0] > 1.0 and n_sent[0] * ticks_per_sample > t_start * mcr:
                p0 = time.perf_counter()
                t_dev = dev.time_now()
                now_p = time.perf_counter()
                if now_p - p0 > 5e-3:
                    slow_log(f"lecture de l'heure de l'appareil : {(now_p - p0) * 1e3:.0f} ms (envoi arrêté pendant ce temps)")
                if now_p - p0 < 2e-3:
                    sync = ((p0 + now_p) / 2, t_dev)
                    if now_p - p0 < 1e-3:      # (the USB round trip is the reading's uncertainty)
                        fit.add(*sync)
                        if clock is not None and not follow and fit.model is not None:
                            clock[0], clock[1], clock[2] = fit.model
                            clock[4] = p_off
                            clock[3] = 1.0
            if sync[0] > 0:
                m_min = min(m_min, t_start + n_sent[0] / fs_tx - (sync[1] + now_p - sync[0]))
            if time.time() - t_last > 1.0:
                t_last = time.time()
                gap, counters["rx_gap_ms"] = counters.get("rx_gap_ms", 0.0), 0.0
                q.put(("status", dict(U=counters["U"], L=counters["L"], L_ms=l_ms(counters["L"]),
                                      O=counters["O"], gap_ms=gap, margin_ms=m_min * 1e3)))
                m_min = float("inf")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        dev.stop_rx()
        dev.end_burst()
        if counters["U"] or counters["L"]:
            log(f"!! TX: {counters['U']} underflows, {counters['L']} paquets en retard jetés "
                f"(~{l_ms(counters['L']):.0f} ms de downstream)")
        for t in workers:                      # out of UHD before it is released (0.2 s timeouts)
            t.join(1.0)
        dev.close()
        pk.close()
        if ring is not None:
            ring.detach()
