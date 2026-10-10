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
"""
import multiprocessing as mp
import os
import queue
import threading
import time
from multiprocessing import shared_memory

import numpy as np

TS_LEN = 188

# UHD runs the AD9361 TX quadrature calibration when the streams open (set_active_chains); it
# loops a test tone back through the RX analog filter, at ~1/8 of the master clock and up. With
# the RX filter at the upstream width (12.4 MHz), the tone is cut from 41.712 MHz on: the TX
# image (mirror of the signal around the LO) stayed at -21..-24 dBc instead of -51, the channel
# at the LO sat on its own mirror and the Sagem never locked above 34.76 MHz (2026-10-10). The
# streams open with this RX bandwidth, then the RX filter goes back to the upstream width.
TX_CAL_RX_BW = 56e6


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
            self.free, self.full = ctx.Semaphore(slots), ctx.Semaphore(0)
            self._own = True
        else:
            name, self.free, self.full = attach
            self.shm = shared_memory.SharedMemory(name=name, track=False)
            self._own = False
        self.buf = np.ndarray((slots,) + self.shape, np.uint8, self.shm.buf)
        self.w = self.r = 0                    # local indices (one writer, one reader)

    def handle(self):
        return self.shm.name, self.free, self.full

    def put(self, pks, stop):
        """pks: one (block, 188) array per channel. False if `stop` was set while waiting."""
        while not self.free.acquire(timeout=0.2):
            if stop.is_set():
                return False
        slot = self.buf[self.w % self.slots]
        for k, p in enumerate(pks):
            slot[k] = p
        self.w += 1
        self.full.release()
        return True

    def get(self, timeout=0.2):
        if not self.full.acquire(timeout=timeout):
            return None
        out = self.buf[self.r % self.slots].copy()
        self.r += 1
        self.free.release()
        return out

    def close(self):
        del self.buf
        try:
            self.shm.close()
            if self._own:
                self.shm.unlink()
        except (FileNotFoundError, BufferError):
            pass


class RadioProcess:
    """Main-process side: starts the radio process, relays its messages."""

    def __init__(self, params, sample_ring, nch, block, slots, log):
        self._ctx = mp.get_context("spawn")      # no fork of a process holding the GIL's threads
        self.pk = PacketRing(nch, block, slots, ctx=self._ctx)
        self.params = params
        self.log = log
        self._q = self._ctx.Queue()
        self._stop = self._ctx.Event()
        self.started = threading.Event()
        self.failed = None
        self.wall0 = None                      # Unix time of TX sample 0 on the air
        self._agg = {}                         # TX/RX counters since the last status line
        self._lock = threading.Lock()
        sr = sample_ring
        self._ring = (sr.shm.name, sr.ctl_shm.name, sr.buf.size, sr.fs, sr.decim)
        self._proc = None

    def start(self):
        self._proc = self._ctx.Process(target=radio_main, name="radio",
                                       args=(self.params, self.pk.handle(), self._ring, self._q, self._stop),
                                       daemon=True)
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


def radio_main(p, pk_handle, ring_handle, q, stop_ev):
    """The radio process: see the module docstring. p: dict built by mini_cmts.py."""
    import faulthandler
    import signal

    def log(s):
        q.put(("log", s))
    _dump = open(f"/tmp/radio_{os.getpid()}.stack", "w")
    faulthandler.register(signal.SIGUSR1, file=_dump, all_threads=True)   # kill -USR1: stacks
    os.environ.setdefault("UHD_LOG_FASTPATH_DISABLE", "1")
    import uhd
    from cmts.stallwatch import StallWatch
    from cmts.usproc import SampleRing

    pk = PacketRing(*p["pk_shape"], attach=pk_handle)
    shm_name, ctl_name, n, rfs, rdecim = ring_handle
    ring = SampleRing(rfs, rdecim, attach=(shm_name, ctl_name, n))
    mcr, fs_tx = p["mcr"], p["fs_tx"]
    try:
        usrp = uhd.usrp.MultiUSRP(p["args"])
        usrp.set_tx_rate(fs_tx)
        usrp.set_tx_freq(uhd.types.TuneRequest(p["tx_center"]))
        usrp.set_tx_bandwidth(p["tx_bw"])
        usrp.set_tx_gain(p["gain"])
        usrp.set_tx_antenna("TX/RX")
        usrp.set_rx_rate(mcr / p["decim"])
        if p["rx_lo_off"]:
            usrp.set_rx_freq(uhd.types.TuneRequest(p["rx_target"], p["rx_lo_off"]))
        else:
            usrp.set_rx_freq(uhd.types.TuneRequest(p["rx_target"]))
        usrp.set_rx_bandwidth(p["rx_bw"])
        usrp.set_rx_gain(p["rx_gain"])
        usrp.set_rx_antenna("RX2")
    except Exception as e:                     # noqa: BLE001 - reported to the main process
        q.put(("fatal", f"B210 : {e}"))
        return
    rx_err = usrp.get_rx_freq() - p["rx_target"]
    log(f"[RADIO] master clock {usrp.get_master_clock_rate() / 1e6:.6f} MHz | TX {usrp.get_tx_rate() / 1e6:.6f} MS/s "
        f"@ {usrp.get_tx_freq() / 1e6:.3f} MHz gain {usrp.get_tx_gain()} dB BW {usrp.get_tx_bandwidth() / 1e6:.0f} MHz"
        f" | RX {usrp.get_rx_rate() / 1e6:.6f} MS/s @ {usrp.get_rx_freq() / 1e6:.4f} MHz "
        f"(LO {(p['rx_target'] + p['rx_lo_off']) / 1e6:.1f} MHz) gain {usrp.get_rx_gain()} dB")
    if abs(rx_err) > 1e3:
        log(f"!! fréquence RX obtenue {usrp.get_rx_freq() / 1e6:.4f} MHz au lieu de {p['rx_target'] / 1e6:.4f}")
    if abs(usrp.get_tx_rate() - fs_tx) / fs_tx > 1e-6 or abs(usrp.get_rx_rate() * p["decim"] - mcr) > 1:
        q.put(("fatal", "débits inexacts: la base de temps commune TX/RX serait fausse"))
        return
    tx_args = uhd.usrp.StreamArgs("fc32", "sc16")
    tx_args.args = "underflow_policy=next_packet"
    usrp.set_rx_bandwidth(TX_CAL_RX_BW)        # UHD calibrates the TX quadrature as the streams open
    tx_stream = usrp.get_tx_stream(tx_args)
    rx_stream = usrp.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    usrp.set_rx_bandwidth(p["rx_bw"])          # back to the upstream width (TX_CAL_RX_BW)

    stop = threading.Event()
    q_tx = queue.Queue(maxsize=p["tx_queue"])
    counters = dict(U=0, L=0, O=0)
    n_sent = [0]

    def air_s():                               # seconds of downstream on the air (log times)
        return n_sent[0] / fs_tx

    # ------------------------------------------------------------------ encode + shape
    chain = TxChain(p, stop)

    def encoder():
        while not stop.is_set():
            blk = pk.get()
            if blk is None:
                continue
            x = chain(blk)
            while not stop.is_set():
                try:
                    q_tx.put(x, timeout=0.2)
                    break
                except queue.Full:
                    pass

    last_push = [time.monotonic()]

    def rx_loop():
        buf = np.zeros((1, rx_stream.get_max_num_samps() * 8), dtype=np.complex64)
        md = uhd.types.RXMetadata()
        while not stop.is_set():
            n = rx_stream.recv(buf, md, 0.5)
            if md.error_code == uhd.types.RXMetadataErrorCode.overflow:
                counters["O"] += 1
                log(f"[RX] !! overflow à t={air_s():.2f} s (échantillons montants perdus)")
            elif md.error_code not in (uhd.types.RXMetadataErrorCode.none,):
                continue
            if n:
                tick = md.time_spec.get_full_secs() * mcr + round(md.time_spec.get_frac_secs() * mcr)
                ring.push(tick, buf[0, :n])
                now_p = time.monotonic()
                counters["rx_gap_ms"] = max(counters.get("rx_gap_ms", 0.0), (now_p - last_push[0]) * 1e3)
                last_push[0] = now_p

    def async_loop():
        amd = uhd.types.TXAsyncMetadata()
        while not stop.is_set():
            if tx_stream.recv_async_msg(amd, 0.2):
                ec = amd.event_code
                if ec in (uhd.types.TXMetadataEventCode.underflow, uhd.types.TXMetadataEventCode.underflow_in_packet):
                    counters["U"] += 1
                elif ec == uhd.types.TXMetadataEventCode.time_error:
                    counters["L"] += 1

    def watch_stop():
        stop_ev.wait()
        stop.set()

    spp = tx_stream.get_max_num_samps()

    def l_ms(n):
        return n * spp / fs_tx * 1e3
    wd = StallWatch(log, counters, air_s, l_ms=l_ms)
    for f in (encoder, async_loop, watch_stop):
        threading.Thread(target=f, daemon=True).start()
    while q_tx.qsize() < p["tx_queue"]:
        if stop.is_set():
            return
        time.sleep(0.005)
    t_start = p["t_start"]
    usrp.set_time_now(uhd.types.TimeSpec(0.0))
    wall0 = time.time() + t_start
    cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
    cmd.stream_now = False
    cmd.time_spec = uhd.types.TimeSpec(t_start)
    rx_stream.issue_stream_cmd(cmd)
    threading.Thread(target=rx_loop, daemon=True).start()
    q.put(("started", wall0))
    md = uhd.types.TXMetadata()
    md.start_of_burst = True
    md.end_of_burst = False
    md.has_time_spec = True
    md.time_spec = uhd.types.TimeSpec(t_start)
    tick0 = round(t_start * mcr)
    ticks_per_sample = round(mcr / fs_tx)
    usb_ms = p["tx_frames"] * spp / fs_tx * 1e3
    log(f"[RADIO] émission (processus séparé, pid {os.getpid()}) ; latence downstream "
        f"~{(p['tx_queue'] + 3) * p['blk_ms'] + usb_ms:.0f} ms (T3 du modem: 200 ms), réserve USB {usb_ms:.0f} ms")
    t_last = time.time()
    wd.last = time.perf_counter()
    wd.armed_at = wd.last + t_start + 0.5
    sync = (-1e9, 0.0)
    t_skip_log = 0.0
    catching_up = False
    m_min = float("inf")
    threading.Thread(target=wd.run, args=(stop,), name="watchdog", daemon=True).start()
    try:
        while not stop.is_set():
            try:
                x = q_tx.get(timeout=0.2)
            except queue.Empty:
                continue
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
            if n_sent[0] == 0 or not p["tx_untimed"]:
                md.has_time_spec = True
                md.time_spec = uhd.types.TimeSpec.from_ticks(tick0 + n_sent[0] * ticks_per_sample, mcr)
            n_sent[0] += x.size
            sent = 0
            while sent < x.size:
                sent += tx_stream.send(x[sent:], md, 1.0)
                md.start_of_burst = False
                md.has_time_spec = False
            wd.feed()
            now_p = time.perf_counter()
            if now_p - sync[0] > 1.0 and n_sent[0] * ticks_per_sample > t_start * mcr:
                p0 = time.perf_counter()
                dev = usrp.get_time_now().get_real_secs()
                now_p = time.perf_counter()
                if now_p - p0 < 2e-3:
                    sync = ((p0 + now_p) / 2, dev)
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
        rx_stream.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
        md.end_of_burst = True
        tx_stream.send(np.zeros(1000, dtype=np.complex64), md)
        if counters["U"] or counters["L"]:
            log(f"!! TX: {counters['U']} underflows, {counters['L']} paquets en retard jetés "
                f"(~{l_ms(counters['L']):.0f} ms de downstream)")
        pk.close()
        ring.detach()
