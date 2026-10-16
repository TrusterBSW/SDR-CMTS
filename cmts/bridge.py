# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Layer 2 bridge between the cable side (DOCSIS Packet PDUs) and a Linux TAP interface (cmts0).

Upstream: every Ethernet frame a modem sends (DHCP, TFTP, ToD, ARP, CPE traffic...) is written
to the TAP; downstream: every frame Linux sends on the TAP is put in a DOCSIS Packet PDU on the
downstream. Whatever runs on cmts0 (provision.py, dnsmasq, a Linux bridge to a real network...)
thus sees the modems as if they were on an Ethernet segment: a "bridged" CMTS.

The TAP is created once (sudo) and owned by the user, so the CMTS then opens it without root.
"""
import fcntl
import getpass
import os
import shutil
import collections
import struct
import subprocess
import threading
import time

import numpy as np

TUNSETIFF = 0x400454CA
IFF_TAP, IFF_NO_PI = 0x0002, 0x1000


def ensure_tap(name, log):
    """Create the TAP (owned by the current user, link up, ignored by NetworkManager) if needed."""
    if os.path.exists(f"/sys/class/net/{name}"):
        return True
    user = getpass.getuser()
    cmds = [["ip", "tuntap", "add", "dev", name, "mode", "tap", "user", user],
            ["ip", "link", "set", name, "up"]]
    if shutil.which("nmcli"):
        cmds.append(["nmcli", "device", "set", name, "managed", "no"])
    log(f"[PONT] création de l'interface {name} (sudo) : " + " ; ".join(" ".join(c) for c in cmds))
    for c in cmds:
        r = subprocess.run(["sudo", *c], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode and c[0] != "nmcli":
            log(f"[PONT] !! {' '.join(c)} : {r.stderr.strip()}")
            return False
    return True


# TAP queue towards this process, in frames. Linux's 1000 is ~46 ms at 260 Mbit/s: the read loop
# below (one os.read per frame, sharing the GIL with the TS packet builder) paused longer, and the
# TAP dropped its frames by bursts: 9,700 in a 15 s download, as many as TCP retransmitted
# (2026-10-11, 6 DS). The frames are dated and dropped after --ds-queue-ms only once read, so a
# longer TAP queue only absorbs the pauses of the reader.
TAP_QLEN = 10000
TAP_BATCH_FRAMES = 256        # frames per batch read (TapBridge with a TapFeed)
TAP_BATCH_BYTES = TAP_BATCH_FRAMES * 2048


class TapFeed:
    """Downstream frames between the TAP and the thread that builds the TS blocks.

    A download starting after a quiet spell puts the sender's whole window into the TAP at once.
    Read and turned into Packet PDUs frame by frame by the TAP thread, in Python, that thread held
    the GIL for ~37 ms, and the TS block encoder of the same process, which lets go of it at every
    C call, waited 18-25 ms for one block: the radios underflowed together and the modem lost a
    channel ~10 s (2026-10-15, two B210s; three stopgaps only made it rarer, 7 bursts out of 8).
    Now the TAP thread reads the frames by batches in C and hands over each batch whole (one
    Python step); the encoder thread itself turns them into PDUs (pump), before each block, at
    most a little more than its channels carry: no thread competes with it for that work, and a
    burst waits here instead of in front of it. Each frame keeps its arrival time: the queue
    limit and CoDel still count the whole wait; a batch older than max_age_s is dropped unbuilt."""

    def __init__(self, send, max_age_s=0.1):
        self.send = send                   # send(eth, arrival time): BondedForwarder via start_bridge
        self.max_age_s = max_age_s
        self.batches = collections.deque()  # (arrival time, frames back to back, sizes)
        self._cur = None
        self._off = self._i = 0
        self.stats = dict(frames=0, dropped=0, batches=0, max_batch=0)

    def add(self, t, data, lens):
        """TAP thread: one batch (deque append, thread-safe)."""
        self.batches.append((t, data, lens))
        self.stats["batches"] += 1
        self.stats["max_batch"] = max(self.stats["max_batch"], len(lens))

    def pump(self, max_bytes):
        """Encoder thread: frames into PDUs until max_bytes of frames; returns the bytes taken."""
        now = time.perf_counter()
        taken = 0
        while taken < max_bytes:
            if self._cur is None:
                if not self.batches:
                    break
                self._cur = self.batches.popleft()
                self._off = self._i = 0
            t, data, lens = self._cur
            if now - t > self.max_age_s:   # waited past the queue limit: dropped before any work
                self.stats["dropped"] += len(lens) - self._i
                self._cur = None
                continue
            size = int(lens[self._i])
            eth = data[self._off:self._off + size]
            self._off += size
            self._i += 1
            if self._i >= len(lens):
                self._cur = None
            self.send(eth, t)
            self.stats["frames"] += 1
            taken += size
        return taken

    def waiting(self):
        """Frames read from the TAP and not yet turned into PDUs."""
        n = sum(len(b[2]) for b in list(self.batches))
        return n + (len(self._cur[2]) - self._i if self._cur is not None else 0)



def set_qlen(name, qlen, log):
    cmd = ["ip", "link", "set", "dev", name, "txqueuelen", str(qlen)]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode:
        log(f"[PONT] !! file de {name} laissée telle quelle ({' '.join(cmd)} : {r.stderr.strip()})")


class TapBridge:
    def __init__(self, name, log):
        self.name, self.log = name, log
        self.fd = os.open("/dev/net/tun", os.O_RDWR)
        fcntl.ioctl(self.fd, TUNSETIFF, struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI))
        set_qlen(name, TAP_QLEN, log)
        self.stats = dict(up=0, down=0, bad_fcs=0)

    def upstream(self, eth):
        """Ethernet frame (no FCS) from a modem -> Linux."""
        os.write(self.fd, eth)
        self.stats["up"] += 1

    def start(self, downstream, stop, feed=None):
        """downstream(eth) is called for every frame Linux sends on the TAP; with `feed` (a
        TapFeed) the frames go there by batches instead, read in C (cdsp.tap_read_batch): one
        Python step per batch, the frames turned into PDUs by whoever pumps the feed."""
        from cmts import cdsp
        if feed is not None and cdsp.DS_AVAILABLE:
            fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            buf = np.empty(TAP_BATCH_BYTES, np.uint8)
            lens = np.empty(TAP_BATCH_FRAMES, np.int32)

            def loop():
                while not stop.is_set():
                    n = cdsp.tap_read_batch(self.fd, buf, lens, 200)
                    if n < 0:
                        break
                    if n:
                        total = int(lens[:n].sum())
                        feed.add(time.perf_counter(), buf[:total].tobytes(), lens[:n].copy())
                        self.stats["down"] += n
        else:
            def loop():
                while not stop.is_set():
                    try:
                        eth = os.read(self.fd, 2048)
                    except OSError:
                        break
                    self.stats["down"] += 1
                    downstream(eth)
        threading.Thread(target=loop, name="tap", daemon=True).start()

    def link_up(self):
        try:
            with open(f"/sys/class/net/{self.name}/operstate") as f:
                return f.read().strip()
        except OSError:
            return "?"


class BondedForwarder:
    """Downstream Packet PDUs for several modems, some of them with bonded downstream channels.

    A unicast frame goes to the modem that owns its destination address: the modem itself or a
    CPE behind it, learned from the source address of the frames each modem sends upstream (SID ->
    modem). For a bonded modem it carries that modem's DSID and its own sequence number, on the
    least loaded channel the modem acquired; otherwise, and for broadcast/multicast, the primary
    channel without a DSID. Until 2026-10-10 every unicast frame took the DSID of the FIRST bonded
    modem: a second bonded modem would have dropped all its unicast traffic.

    channels: [primary mux, secondary muxes...] (send_data()); core: CmtsCore (by_sid, bonded,
    bonded_channels)."""

    def __init__(self, channels, core, mac_mod):
        self.channels, self.core, self.mac = channels, core, mac_mod
        self.owner = {}                  # Ethernet address -> MAC of the modem it sits behind
        self.seq = {}                    # DSID -> next packet sequence number
        self.per_chan = [0] * len(channels)
        self.force_ch = None             # diagnostic: every bonded frame on this channel index

    def learn(self, eth, sid):
        """An upstream frame from `sid`: its source address belongs to that modem."""
        m = self.core.by_sid.get(sid)
        if m is not None and len(eth) >= 12:
            self.owner[bytes(eth[6:12])] = m.mac

    def modem_of(self, eth):
        """MAC of the bonded modem a unicast frame is for, or None (primary, no DSID)."""
        bonded = self.core.bonded
        if len(self.channels) < 2 or not bonded or eth[0] & 1:
            return None
        dst = bytes(eth[0:6])
        cm = self.owner.get(dst) or (dst if dst in bonded else None)
        if cm is None and len(bonded) == 1:  # one bonded modem, address not learned yet: as before
            cm = next(iter(bonded), None)
        return cm if cm in bonded else None

    def send(self, eth, t=None):
        """Queue one frame from the TAP (t: its arrival time, perf_counter); returns the channel
        index used."""
        cm = self.modem_of(eth)
        dsid = self.core.bonded.get(cm) if cm is not None else None
        if dsid is None:
            self.channels[0].send_data(self.mac.data_pdu(eth), t)
            self.per_chan[0] += 1
            return 0
        n = len(self.channels)
        usable = [c - 1 for c in self.core.bonded_channels.get(cm, range(1, n + 1)) if 0 < c <= n] or [0]
        # The acquired channels in turn, the next one after the last used, skipping one more than
        # 2 frames above the least loaded (frames waiting + TS packets not sent yet). With the
        # queues mostly empty (TS packets built in C since 2026-10-11), consecutive frames leave
        # at the same instant on several channels, and the Sagem lost 0.02-2.4 % of them (UDP
        # 150-280 Mbit/s, TCP 155 instead of 277 Mbit/s) unless the channel order follows the
        # sequence order: least loaded with ties to the first channel, or in random turn, did not
        # (in strict turn: 0 % lost)
        loads = {i: self.channels[i].backlog() for i in usable}
        lo = min(loads.values())
        last = getattr(self, "last", -1)
        k = min(usable, key=lambda i: (loads[i] > lo + 2, (i - last - 1) % n))
        self.last = k
        if self.force_ch is not None:
            k = self.force_ch
        s = self.seq.get(dsid, 0)
        self.seq[dsid] = (s + 1) & 0xFFFF
        self.channels[k].send_data(self.mac.data_pdu_dsid(eth, dsid, s), t)
        self.per_chan[k] += 1
        return k
