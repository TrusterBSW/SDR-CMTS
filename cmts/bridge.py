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
import struct
import subprocess
import threading
import time

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

# A download starting after a quiet spell: the sender's whole window lands in the TAP queue at
# once (iperf3 on the CMTS host: memory speed), and this thread, each read returning at once,
# went through hundreds of frames in Python without ever sleeping (37 ms, 2026-10-15). The TS
# block encoder of the same process lets go of the GIL at every C call: it got it back only at
# forced switches, the radios waited 18-25 ms for one block and underflowed together, the modem
# dropping a channel for ~10 s. A short pause every BURST_FRAMES frames read without waiting
# bounds that; in steady traffic the reads wait (ACK-clocked arrivals) and nothing changes.
# Capacity in a burst: ~32 frames per ~1 ms, ~3 times what 4 DS carry. Not enough alone: see
# mini_cmts.py (SWITCH_INTERVAL_S, and the yield of start_bridge while the encoder is late).
BURST_FRAMES = 32
BURST_PAUSE_S = 150e-6
BURST_READ_S = 20e-6          # a read faster than this found the frame already queued


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

    def start(self, downstream, stop):
        """downstream(eth) is called for every frame Linux sends on the TAP."""
        def loop():
            run = 0                            # frames read in a row without waiting
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    eth = os.read(self.fd, 2048)
                except OSError:
                    break
                run = run + 1 if time.perf_counter() - t0 < BURST_READ_S else 0
                if run >= BURST_FRAMES:
                    run = 0
                    time.sleep(BURST_PAUSE_S)
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

    def send(self, eth):
        """Queue one frame from the TAP; returns the channel index used."""
        cm = self.modem_of(eth)
        dsid = self.core.bonded.get(cm) if cm is not None else None
        if dsid is None:
            self.channels[0].send_data(self.mac.data_pdu(eth))
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
        self.channels[k].send_data(self.mac.data_pdu_dsid(eth, dsid, s))
        self.per_chan[k] += 1
        return k
