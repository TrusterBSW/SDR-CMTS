"""Downstream multiplexer: schedules SYNC / UCD / MAP into the MPEG-TS stream and
turns it into baseband samples.

Time base: the DOCSIS 10.24 MHz timestamp is derived from the symbol counter
(6.952 Msym/s), so every timestamp is exactly consistent with the air time of
the byte that carries it, whatever the USB / host latency.
"""
import collections
import time

import numpy as np

from . import docsis_mac as mac
from .j83a import J83AEncoder, PulseShaper, RS_LEN, INTER_I

SYMRATE = 6_952_000
TS_PER_SYM_NUM, TS_PER_SYM_DEN = 1280, 869  # 10.24e6 / 6.952e6
TICK = 64                                   # 6.25 us time tick in 10.24 MHz counts
MAX_IL_DELAY = RS_LEN * (INTER_I - 1)       # bytes


class DownstreamMux:
    def __init__(self, qam=64, sps=2, us_freq=40e6, us_symrate_mult=8, minislot_ticks=4,
                 cmts_mac="02:00:00:ca:fe:01", sync_ms=10, ucd_ms=1000, map_ms=2,
                 map_lead_ms=4, im_every=5, im_len=48, ds_chan_id=1, us_chan_id=1,
                 ts_start=0, invert=True, scheduler=None, on_grants=None):
        # invert=True mirrors the spectrum (conjugate baseband): this is the orientation
        # observed on the SFR network with two independent receivers (B210 and HackRF)
        self.invert = invert
        self.enc = J83AEncoder(qam)
        self.shaper = PulseShaper(sps)
        self.m = self.enc.m
        self.sa = bytes.fromhex(cmts_mac.replace(":", ""))
        self.ts_start = ts_start
        self.pk = mac.TSPacketizer()
        self.p = 0                 # index of the next TS packet
        self.queue = []            # packets of a multi-packet message in progress
        self.us = dict(freq=us_freq, mult=us_symrate_mult, ms_ticks=minislot_ticks,
                       us_id=us_chan_id, ds_id=ds_chan_id, ccc=1)
        self.extra_us = []         # further upstream channels (add_upstream)
        self.ms_counts = minislot_ticks * TICK            # minislot length in 10.24 MHz counts
        self.map_len = int(round(map_ms * 10240 / self.ms_counts))
        self.map_lead = int(map_lead_ms * 10240)
        self.im_every, self.im_len = im_every, im_len
        self.im_every_idle = im_every
        self.im_busy = None             # () -> True while a modem may need Initial Maintenance
        self.sync_iv = int(sync_ms * 10240)
        self.ucd_iv = int(ucd_ms * 10240)
        self.next_sync = 0
        self.next_ucd = 0
        self.next_map_ms = None    # next alloc start, in minislots
        self.map_count = 0
        self.stats = dict(sync=0, ucd=0, map=0, im=0, null=0, ucast=0, data=0, data_drop=0, data_max_ms=0.0)
        # upstream hooks:
        #  scheduler(start_ms, free_ms) -> [(sid, iuc, n_minislots)] unicast grants for a MAP
        #  on_grants([(start_ts, end_ts, iuc, sid)]) receives every interval of every MAP
        #  ucast: (frame, callback(air_ts) or None) unicast messages sent as soon as possible
        self.scheduler = scheduler
        self.on_grants = on_grants
        self.ucast = collections.deque()
        # ack_ms() -> last upstream mini-slot actually processed (MAP "Ack Time"); None = old
        # behaviour (start - map length), which announces upstream processing that has not
        # happened yet: a modem then takes its unanswered REQ for a collision and retries at once
        self.ack_ms = None
        self.max_map_len = 320     # mini-slots: a 255 mini-slot grant + IM + contention fits
        self.min_contention = 8    # REQ mini-slots kept at the end of every MAP
        self.on_ucast = None       # on_ucast(frame, air_ts): every unicast message sent (pcap)
        # Data frames (bridge) wait in their own queue, after the MAC management messages
        # (RNG-RSP, REG-RSP: a RNG-RSP stuck behind 491 ms of TCP data was seen, T3 = 200 ms).
        # A frame that waited more than data_max_ms is dropped (TCP then slows down instead of
        # piling up seconds of delay). Several data frames are packed into the same TS packets.
        self.data = collections.deque()            # (enqueue time, frame)
        self.data_max_ms = 100.0
        self.data_pack = 8                         # TS packets per batch of data frames
        self.mdd = None            # MDD frame (DOCSIS 3.0), sent every mdd_iv when set
        self.mdd_iv = int(1.0 * 10240 * 1000)
        self.next_mdd = 0
        self._batch = []
        self.now_ts = ts_start     # air time of the packet being generated (read by other threads)

    # -------------------------------------------------------------- timing
    def byte_ts(self, n):
        """10.24 MHz timestamp at which RS-coded byte n (stream index) hits the air."""
        pos = n + RS_LEN * (n % INTER_I)            # after the interleaver
        sym = pos * 8 // self.m
        return self.ts_start + sym * TS_PER_SYM_NUM // TS_PER_SYM_DEN

    def pkt_air_ts(self, p):
        """Upper bound of the air time of packet p (last byte, max interleaver delay)."""
        n = p * RS_LEN + RS_LEN + MAX_IL_DELAY
        return self.ts_start + (n * 8 // self.m) * TS_PER_SYM_NUM // TS_PER_SYM_DEN

    # -------------------------------------------------------------- scheduling
    def _sync_packet(self):
        n = self.p * RS_LEN + 4 + 1 + mac.SYNC_TS_OFFSET  # TS hdr + pointer_field
        frame = mac.sync_msg(self.sa, self.byte_ts(n))
        (pkt,) = self.pk.pack([frame])
        return pkt

    def add_upstream(self, us_id, freq, scheduler=None, on_grants=None, ack_ms=None, im=True):
        """A further upstream channel (DOCSIS 3.0 upstream bonding): same symbol rate, mini-slot
        size and MAP length as the first one, its own UCD and MAP stream. scheduler / on_grants /
        ack_ms: as the attributes of the same name, for this channel; im: Initial Maintenance
        opportunities on it (needed for a modem to range there)."""
        u = dict(self.us, freq=freq, us_id=us_id, ccc=1, next_map_ms=None, map_count=0,
                 scheduler=scheduler, on_grants=on_grants, ack_ms=ack_ms, im=im)
        self.extra_us.append(u)
        return u

    def _due_map(self, now):
        """True when a MAP must be generated (first channel or a further one)."""
        if self.next_map_ms is None or self.next_map_ms * self.ms_counts < now + self.map_lead:
            return True
        return any(u["next_map_ms"] is None or u["next_map_ms"] * self.ms_counts < now + self.map_lead
                   for u in self.extra_us)

    def _map_frames(self, now):
        frames = []
        # the first channel: state in attributes (scheduler, on_grants, ack_ms, next_map_ms,
        # map_count), the further ones in their dict; same loop for all
        first = dict(self.us, scheduler=self.scheduler, on_grants=self.on_grants, ack_ms=self.ack_ms,
                     next_map_ms=self.next_map_ms, map_count=self.map_count, im=True)
        for u in [first] + self.extra_us:
            frames += self._channel_maps(u, now)
        self.next_map_ms, self.map_count = first["next_map_ms"], first["map_count"]
        return frames

    def _channel_maps(self, u, now):
        frames = []
        if u["next_map_ms"] is None or u["next_map_ms"] * self.ms_counts < now + self.map_lead // 2:
            u["next_map_ms"] = -(-(now + self.map_lead // 2) // self.ms_counts)
        while u["next_map_ms"] * self.ms_counts < now + self.map_lead:
            start = u["next_map_ms"]
            ies, off = [], 0
            # fewer Initial Maintenance opportunities while every known modem is online (each one is
            # a 1 ms-slack window to search upstream); as many as before when one is acquiring
            every = self.im_every if (self.im_busy is None or self.im_busy()) else self.im_every_idle
            im = u["im"] and u["map_count"] % every == 0
            free = self.max_map_len - (self.im_len if im else 0) - self.min_contention
            sched = u["scheduler"]
            for sid, iuc, n in (sched(start, free) if sched else []):
                if off + n > free:
                    break
                ies.append((sid, iuc, off, n))
                off += n
            if im:
                ies.append((mac.SID_BROADCAST, mac.IUC_INIT_MAINT, off, self.im_len))
                off += self.im_len
                self.stats["im"] += 1
            length = max(self.map_len, off + self.min_contention)   # longer only for big grants
            if off < length:
                ies.append((mac.SID_BROADCAST, mac.IUC_REQUEST, off, length - off))
            if u["on_grants"]:
                u["on_grants"]([((start + o) * self.ms_counts, (start + o + n) * self.ms_counts, iuc, sid)
                                for sid, iuc, o, n in ies])
            ies = [(sid, iuc & mac.IUC_MASK, o) for sid, iuc, o, _ in ies]   # (receiver flags off)
            ack = u["ack_ms"]() if u["ack_ms"] else start - self.map_len
            frames.append(mac.map_msg(self.sa, u["us_id"], u["ccc"], start,
                                      min(ack, start), ies, length))
            u["next_map_ms"] += length
            u["map_count"] += 1
            self.stats["map"] += 1
        return frames

    def next_packet(self):
        now = self.pkt_air_ts(self.p)
        self.now_ts = now
        if self.queue:
            pkt = self.queue.pop(0)
        elif (self.ucast or self.data) and now < self.next_sync and not (
                self.next_map_ms is None or self.next_map_ms * self.ms_counts < now + self.map_lead) \
                and (self.ucast or self._data_batch()):
            if self.ucast:                         # MAC management first
                frame, cb = self.ucast.popleft()
                frames = [frame]
                self.stats["ucast"] += 1
                if cb:
                    cb(now)
            else:
                frames = self._batch
            self.queue = self.pk.pack(frames)
            pkt = self.queue.pop(0)
            if self.on_ucast:
                for f in frames:
                    self.on_ucast(f, now)
        elif now >= self.next_sync:
            pkt = self._sync_packet()
            self.next_sync = now + self.sync_iv
            self.stats["sync"] += 1
        elif self._due_map(now):
            self.queue = self.pk.pack(self._map_frames(now))
            pkt = self.queue.pop(0)
        elif self.mdd is not None and now >= self.next_mdd:
            self.queue = self.pk.pack([self.mdd])
            pkt = self.queue.pop(0)
            self.next_mdd = now + self.mdd_iv
            self.stats["mdd"] = self.stats.get("mdd", 0) + 1
        elif now >= self.next_ucd:
            self.queue = self.pk.pack([mac.ucd_msg(self.sa, u["us_id"], u["ccc"], u["ms_ticks"],
                                                   u["ds_id"], u["freq"], u["mult"])
                                       for u in [self.us] + self.extra_us])
            pkt = self.queue.pop(0)
            self.next_ucd = now + self.ucd_iv
            self.stats["ucd"] += 1
        else:
            pkt = self.pk.null
            self.stats["null"] += 1
        self.p += 1
        return pkt

    def send_data(self, frame):
        """Queue a downstream data PDU (thread-safe: deque append)."""
        self.data.append((time.perf_counter(), frame))

    def _data_batch(self):
        """Take the data frames for the next TS packets (stale ones dropped). True if any."""
        now = time.perf_counter()
        batch, size = [], 0
        while self.data:
            t, f = self.data[0]
            wait = (now - t) * 1e3
            if wait > self.data_max_ms:
                self.data.popleft()
                self.stats["data_drop"] += 1
                continue
            if batch and size + len(f) > self.data_pack * 183:
                break
            self.data.popleft()
            self.stats["data_max_ms"] = max(self.stats["data_max_ms"], wait)
            batch.append(f)
            size += len(f)
        self.stats["data"] += len(batch)
        self._batch = batch
        return bool(batch)

    def packets(self, n):
        return np.stack([self.next_packet() for _ in range(n)])

    def samples(self, n_packets=256):
        """n_packets (multiple of 8) of TS -> complex64 baseband at sps * 6.952 MS/s."""
        return self.shape(self.enc.encode(self.packets(n_packets)))

    def shape(self, sym):
        x = self.shaper.process(sym)
        return np.conj(x) if self.invert else x


class SecondaryMux:
    """A non-primary downstream channel of a bonding group (DOCSIS 3.0): MDD, data and null
    packets only; SYNC, UCD and MAP stay on the primary channel. Same symbol clock as the
    primary (both come from the B210 master clock), its own TS continuity counter and encoder."""

    def __init__(self, qam=256, cmts_mac="02:00:00:ca:fe:01", invert=True):
        self.enc = J83AEncoder(qam)
        self.m = self.enc.m
        self.invert = invert
        self.sa = bytes.fromhex(cmts_mac.replace(":", ""))
        self.pk = mac.TSPacketizer()
        self.queue = []
        self.data = collections.deque()
        self.data_max_ms = 100.0
        self.data_pack = 8
        self.mdd = None
        self.mdd_every = int(SYMRATE * self.m / (RS_LEN * 8))   # TS packets per second
        self.p = 0
        self.next_mdd = 0
        self.on_ucast = None
        self.stats = dict(mdd=0, data=0, data_drop=0, null=0, data_max_ms=0.0)

    def send_data(self, frame):
        self.data.append((time.perf_counter(), frame))

    def next_packet(self):
        if self.queue:
            pkt = self.queue.pop(0)
        elif self.mdd is not None and self.p >= self.next_mdd:
            self.queue = self.pk.pack([self.mdd])
            pkt = self.queue.pop(0)
            self.next_mdd = self.p + self.mdd_every
            self.stats["mdd"] += 1
        else:
            now, batch, size = time.perf_counter(), [], 0
            while self.data:
                t, f = self.data[0]
                wait = (now - t) * 1e3
                if wait > self.data_max_ms:
                    self.data.popleft()
                    self.stats["data_drop"] += 1
                    continue
                if batch and size + len(f) > self.data_pack * 183:
                    break
                self.data.popleft()
                self.stats["data_max_ms"] = max(self.stats["data_max_ms"], wait)
                batch.append(f)
                size += len(f)
            if batch:
                self.stats["data"] += len(batch)
                self.queue = self.pk.pack(batch)
                pkt = self.queue.pop(0)
                if self.on_ucast:
                    for f in batch:
                        self.on_ucast(f, None)
            else:
                pkt = self.pk.null
                self.stats["null"] += 1
        self.p += 1
        return pkt

    def packets(self, n):
        return np.stack([self.next_packet() for _ in range(n)])
