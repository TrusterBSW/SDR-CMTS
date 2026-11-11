# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Edge PHY: the physical layer on an FPGA board over Ethernet instead of a B210 on USB
(sdr_cmts.py --sdr edgephy, --radio-process). The board (Fishball / Pluto+ clone, xc7z010 +
AD9363, ~/claude/pluto-dvb/docs/EDGEPHY.md) runs the same J.83-A chain as cmts/j83a.py and
cmts/dsdsp.c on 4 adjacent 8 MHz channels (FREQ + k x 8 MHz, 41.712 MS/s, 6 samples per symbol)
and takes one upstream channel out of its ADC (NCO, FIR, /4: 10.428 MS/s). One sample counter
(the tick, 41.712 MHz) times both directions: no clock line between radios.

This module replaces the device in the radio process (cmts/radio.py radio_main -> edgephy_main):
the MAC's blocks of TS packets go out as numbered J.83-A randomization groups, the upstream
samples come back into the shared SampleRing, as with a B210.

Protocol (little-endian):
  data, UDP 1100, PC -> board: "EPH1" u32 | group u64 | channel mask u32 | for each channel of the
      mask (slot 0..3, ascending), 8 x 188 bytes. Group n goes on the air at tick T0 + n x G
      exactly (G = 9792 in 256-QAM, 13056 in 64-QAM); a group missing in time is sent as 8 null
      packets, so the rule always holds.
  control, UDP 1101: "EPHQ" u32 | nonce u64 -> "EPHR" (REPLY_FMT): tick now, T0, epoch, counters.
      The board sends the upstream to the address of the last query.
  upstream, UDP 1102, board -> PC: 32-byte header (US_HDR) then nsamp x (I, Q) int8 or int16;
      sample k <-> tick + 4 k.
  HTTP 8080: /set?freq=&qam=&mask=&invert=&scale=&att=&us_freq=&us_enable=.

Time base: the CMTS keeps its device ticks (TX sample 0 at round(t_start x mcr)); here
device tick = board tick - base, base chosen at start so that the MAC's block 0 begins with group
n0, t_start seconds ahead. DOCSIS timestamps then follow from the group numbers exactly, as from
the B210's sample counter; the network latency never enters them. The board's fixed delay after
its DAC interface (AD9363 interpolation filters, analog) is a constant the ranging absorbs.

Transmission: the board starts silent (attenuation 89.75 dB). The CMTS never lowers it unless
told (--edgephy-att): on the coax of a live network it would transmit."""
import json
import math
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request

import numpy as np

MAGIC_DATA = 0x31485045          # "EPH1"
MAGIC_QUERY = 0x51485045         # "EPHQ"
MAGIC_REPLY = 0x52485045         # "EPHR"
MAGIC_US = 0x31535545            # "EUS1"
REPLY_FMT = "<IIQQQQIIIIII6I6Q"
REPLY_KEYS = ["magic", "version", "nonce", "tick", "t0", "groups", "epoch", "started", "sample_rate",
              "samples_per_group", "qam", "mask", "fpga_nulls", "fpga_late", "fpga_early", "fpga_resync",
              "fpga_syncerr", "dac_underflows", "rx_ok", "rx_late", "rx_early", "rx_bad", "skipped", "pushed"]
US_HDR = struct.Struct("<IIQIBBHII")     # magic, seq, tick, nsamp, fmt, decim, flags, gaps, lost
DATA_PORT, CTL_PORT, US_PORT, HTTP_PORT = 1100, 1101, 1102, 8080
SLOTS, SPACING = 4, 8e6
FS = 41.712e6                    # board sample rate = tick rate
SPS = 6
US_DECIM = 4
TS_LEN, GROUP = 188, 8
SILENT_ATT = 89.75               # the board's attenuation at start: nothing on the coax


def group_samples(qam):
    """Samples of one J.83-A randomization group (8 TS packets, 204 bytes each after RS) at 6/symbol."""
    return GROUP * 204 * 8 // {64: 6, 256: 8}[qam] * SPS


def ds_plan(freqs, spacing=SPACING):
    """The board's channel layout for the CMTS's downstream frequencies (MAC order, primary
    first): (frequency of slot 0, slot of each channel, channel mask). The board's channels are
    4 adjacent ones, FREQ + k x 8 MHz."""
    if not freqs or len(freqs) > SLOTS:
        raise ValueError(f"edge PHY : 1 à {SLOTS} canaux descendants ({len(freqs)} demandés)")
    if abs(spacing - SPACING) > 1:
        raise ValueError("edge PHY : canaux espacés de 8 MHz uniquement (--ds-spacing 8e6)")
    f0 = min(freqs)
    slots = []
    for f in freqs:
        k = (f - f0) / SPACING
        if abs(k - round(k)) > 1e-6 or round(k) >= SLOTS:
            raise ValueError(f"edge PHY : {f / 1e6:.3f} MHz hors des 4 canaux contigus à partir de "
                             f"{f0 / 1e6:.3f} MHz")
        slots.append(int(round(k)))
    if len(set(slots)) != len(slots):
        raise ValueError("edge PHY : deux canaux sur la même fréquence")
    return f0, slots, sum(1 << s for s in slots)


def pack_group(g, mask, by_slot):
    """One data datagram: group g, by_slot[s] = (8, 188) uint8 for each slot s of the mask."""
    parts = [struct.pack("<IQI", MAGIC_DATA, g, mask)]
    for s in range(SLOTS):
        if mask >> s & 1:
            parts.append(np.ascontiguousarray(by_slot[s], np.uint8).tobytes())
    return b"".join(parts)


def parse_reply(data):
    n = struct.calcsize(REPLY_FMT)
    if len(data) < n:
        return None
    r = dict(zip(REPLY_KEYS, struct.unpack(REPLY_FMT, data[:n])))
    return r if r["magic"] == MAGIC_REPLY else None


def parse_us(d):
    """An upstream datagram -> (seq, tick, flags, gaps, lost, samples complex64), None if malformed."""
    if len(d) < US_HDR.size:
        return None
    magic, seq, tick, ns, fmt, decim, flags, gaps, lost = US_HDR.unpack_from(d)
    if magic != MAGIC_US or decim != US_DECIM or fmt not in (1, 2):
        return None
    if len(d) != US_HDR.size + ns * (2 if fmt == 1 else 4):
        return None
    v = np.frombuffer(d, np.int8 if fmt == 1 else np.int16, offset=US_HDR.size, count=2 * ns)
    x = v.astype(np.float32).view(np.complex64) * np.float32(1 / (127.0 if fmt == 1 else 32767.0))
    return seq, tick, flags, gaps, lost, x


class Control:
    """The control link: queries, and the board's tick against the host's perf_counter (best round
    trips; the board's oscillator drifts a few ppm against the host's, refitted every second)."""

    def __init__(self, host, port=CTL_PORT, iface=None):
        self.addr = (host, port)
        self.s = bind_iface(socket.socket(socket.AF_INET, socket.SOCK_DGRAM), iface)
        self.s.settimeout(0.2)
        self.nonce = 0
        self.ref = None                       # (host perf_counter, board tick) of a good round trip
        self.rate = FS                        # board ticks per host second
        self._pts = []                        # recent (host, tick) of good round trips

    def query(self):
        """One round trip -> the reply dict with 'rtt' and 'host' (perf_counter at mid-trip), or None."""
        self.nonce += 1
        t1 = time.perf_counter()
        self.s.sendto(struct.pack("<IQ", MAGIC_QUERY, self.nonce), self.addr)
        while True:
            try:
                data, _ = self.s.recvfrom(1024)
            except (socket.timeout, OSError):
                return None
            t2 = time.perf_counter()
            r = parse_reply(data)
            if r is not None and r["nonce"] == self.nonce:
                r["rtt"], r["host"] = t2 - t1, (t1 + t2) / 2
                return r

    def fit(self, n=5):
        """n round trips: the best one becomes a clock point. -> its reply (None: no answer)."""
        best = None
        for _ in range(n):
            r = self.query()
            if r is not None and (best is None or r["rtt"] < best["rtt"]):
                best = r
        if best is None:
            return None
        self._pts.append((best["host"], best["tick"]))
        self._pts = [p for p in self._pts if best["host"] - p[0] < 60.0][-120:]
        if len(self._pts) >= 5 and self._pts[-1][0] - self._pts[0][0] > 4.0:
            a = np.array(self._pts, np.float64)
            k, c = np.polyfit(a[:, 0] - a[-1, 0], a[:, 1] - a[-1, 1], 1)
            self.rate = float(k)
            self.ref = (float(a[-1, 0]), float(a[-1, 1] + c))
        else:
            self.rate = best["sample_rate"] or FS
            self.ref = (best["host"], float(best["tick"]))
        return best

    def tick_at(self, host_t):
        return self.ref[1] + (host_t - self.ref[0]) * self.rate

    def host_at(self, tick):
        return self.ref[0] + (tick - self.ref[1]) / self.rate

    def close(self):
        self.s.close()


def bind_iface(s, iface):
    """The socket on that network interface (SO_BINDTODEVICE): a host with Wi-Fi and wired links
    on the board's subnet would otherwise send its 205 Mbit/s of groups over the Wi-Fi."""
    if iface:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
    return s


def configure(host, settings, port=HTTP_PORT, timeout=3.0):
    """HTTP /set on the board; -> its answer (text)."""
    q = urllib.parse.urlencode(settings)
    with urllib.request.urlopen(f"http://{host}:{port}/set?{q}", timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def board_settings(p):
    """The /set parameters for the radio parameters p (sdr_cmts.py run_split)."""
    s = dict(freq=int(round(p["ep_freq0"])), qam=p["qams"][0], mask=p["ep_mask"], invert=int(bool(p["invert"])),
             scale=f"{p['scale']:.4f}")
    if p.get("ep_us_freq"):
        s.update(us_freq=int(round(p["ep_us_freq"])), us_enable=1)
    else:
        s["us_enable"] = 0                     # (downstream only: --no-edgephy-us)
    if p.get("ep_att") is not None:
        s["att"] = f"{p['ep_att']:.2f}"
    return s


def describe(p, r):
    att = p.get("ep_att")
    return (f"edge PHY {p['ep_host']} : DS {len(p['ep_slots'])} canaux à partir de {p['ep_freq0'] / 1e6:.3f} MHz "
            f"(masque {p['ep_mask']:#x}), {p['qams'][0]}-QAM, {r['sample_rate'] / 1e6:.3f} MS/s, "
            f"{r['samples_per_group']} éch./groupe ; "
            + (f"US {p['ep_us_freq'] / 1e6:.2f} MHz à {r['sample_rate'] / US_DECIM / 1e6:.3f} MS/s ; "
               if p.get("ep_us_freq") else "pas de montant (--no-edgephy-us) ; ")
            + (f"atténuation {att:.2f} dB" if att is not None else
               "atténuation laissée telle quelle (89,75 dB au démarrage de la carte : rien sur le câble)"))


def edgephy_main(p, pk, ring, q, stop_ev, log):
    """The radio process's loop with an edge PHY board (radio_main calls it, p["driver"] ==
    "edgephy"): same messages to the MAC process as with a B210 ('started', 'status', 'log',
    'fatal'). pk: the PacketRing of TS blocks; ring: the upstream SampleRing (None: transmit only)."""
    host = p["ep_host"]
    ports = p.get("ep_ports", (DATA_PORT, CTL_PORT, US_PORT))
    mcr, t_start = p["mcr"], p["t_start"]
    lead_s = p.get("ep_lead_s", 0.015)        # groups handed over this long before their air time
    stop = threading.Event()
    if p.get("ep_http", HTTP_PORT):
        try:
            ans = configure(host, board_settings(p), port=p.get("ep_http", HTTP_PORT))
            log(f"[EDGEPHY] réglages envoyés à {host} : {ans.strip()[:200]}")
        except Exception as e:                 # noqa: BLE001 - reported to the main process
            q.put(("fatal", f"edge PHY {host} : réglage HTTP impossible ({e})"))
            return
    iface = p.get("ep_iface")
    ctl = Control(host, ports[1], iface)
    r = None
    t_end = time.monotonic() + 10
    while time.monotonic() < t_end and not stop_ev.value:
        r = ctl.fit(20)
        if r is not None and r["started"]:
            break
        time.sleep(0.2)                        # (a new qam or invert restarts the core: new T0)
    if r is None or not r["started"]:
        q.put(("fatal", f"edge PHY {host} : pas de réponse de contrôle (UDP {ports[1]}) ou cœur arrêté ; "
                        f"image edgephy chargée ? (sh /boot/bascule.sh edgephy)"))
        return
    G = group_samples(p["qams"][0])
    err = None
    if abs(r["sample_rate"] - mcr) > 1:
        err = f"cadence {r['sample_rate']} au lieu de {mcr:.0f}"
    elif r["samples_per_group"] != G:
        err = f"{r['samples_per_group']} éch./groupe au lieu de {G} (modulation ?)"
    elif r["qam"] not in (0, p["qams"][0]) or (r["mask"] and r["mask"] != p["ep_mask"]):
        err = f"carte en {r['qam']}-QAM masque {r['mask']:#x}, attendu {p['qams'][0]} / {p['ep_mask']:#x}"
    if err:
        q.put(("fatal", f"edge PHY {host} : {err}"))
        return
    log(f"[EDGEPHY] {describe(p, r)}")
    log(f"[EDGEPHY] horloge : aller-retour {r['rtt'] * 1e3:.2f} ms, epoch {r['epoch']}, T0 {r['t0']}")
    T0, epoch = r["t0"], r["epoch"]
    gpb = p["pk_shape"][1] // GROUP            # groups per MAC block
    by_ch = {s: i for i, s in enumerate(p["ep_slots"])}
    mask = p["ep_mask"]
    sd = bind_iface(socket.socket(socket.AF_INET, socket.SOCK_DGRAM), iface)
    sd.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
    data_addr = (host, ports[0])
    us = None
    if ring is not None and p.get("ep_us_freq"):
        us = bind_iface(socket.socket(socket.AF_INET, socket.SOCK_DGRAM), iface)
        us.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 << 20)
        us.bind(("", ports[2]))
        us.settimeout(0.2)
    # time base: the MAC's block 0 = group n0, t_start ahead; device tick = board tick - base
    now = time.perf_counter()
    n0 = math.ceil((ctl.tick_at(now) + t_start * mcr - T0) / G)
    air0 = T0 + n0 * G
    tick0 = round(t_start * mcr)
    base = air0 - tick0
    counters = dict(U=0, L=0, O=0, sent=0, skipped=0)
    st = dict(m_min=float("inf"), gap_ms=0.0, last_push=None, board=r)
    wall0 = time.time() + (air0 - ctl.tick_at(time.perf_counter())) / mcr
    q.put(("started", wall0))
    log(f"[EDGEPHY] émission : bloc 0 = groupe {n0} (tick {air0}), {gpb} groupes par bloc, avance "
        f"{lead_s * 1e3:.0f} ms ; réception montante {'UDP ' + str(ports[2]) if us else 'aucune'}")

    def rx_loop():
        last_seq, started = None, False
        while not stop.is_set():
            try:
                d = us.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            u = parse_us(d)
            if u is None:
                continue
            seq, tick, flags, _gaps, _lost, x = u
            if last_seq is not None and seq != (last_seq + 1) & 0xFFFFFFFF:
                counters["O"] += 1
            last_seq = seq
            dt = tick - base
            if not started:                    # the ring starts at the first sample from tick0 on
                if dt + US_DECIM * x.size <= tick0:
                    continue
                k = max(0, -(-(tick0 - dt) // US_DECIM))
                x, dt, started = x[k:], dt + US_DECIM * k, True
            ring.push(dt, x)
            now_p = time.monotonic()
            if st["last_push"] is not None:
                st["gap_ms"] = max(st["gap_ms"], (now_p - st["last_push"]) * 1e3)
            st["last_push"] = now_p

    def ctl_loop():
        nonlocal T0, epoch
        last_log = time.monotonic()
        prev = dict(r)
        while not stop.is_set():
            time.sleep(1.0)
            rr = ctl.fit(3)
            if rr is None:
                log(f"[EDGEPHY] !! pas de réponse de contrôle de {host}")
                continue
            if rr["t0"] != T0 or rr["epoch"] != epoch:
                q.put(("fatal", f"edge PHY {host} : le cœur a redémarré (epoch {epoch} -> {rr['epoch']}, nouveau T0) ; "
                                f"le temps DOCSIS ne peut pas suivre, redémarrage du CMTS"))
                stop.set()
                return
            st["board"] = rr
            d = {k: rr[k] - prev[k] for k in ("fpga_nulls", "fpga_late", "rx_late", "dac_underflows")}
            counters["U"] += d["dac_underflows"]
            if (any(d.values()) and time.monotonic() - last_log > 5) or time.monotonic() - last_log > 300:
                last_log = time.monotonic()
                log(f"[EDGEPHY] carte : groupes nuls +{d['fpga_nulls']}, en retard +{d['fpga_late']} (FPGA) / "
                    f"+{d['rx_late']} (démon), sous-débits DAC +{d['dac_underflows']} ; aller-retour "
                    f"{rr['rtt'] * 1e3:.2f} ms, dérive {(ctl.rate / mcr - 1) * 1e6:+.2f} ppm")
            prev = rr

    def watch_stop():
        while not stop_ev.value and not stop.is_set():
            time.sleep(0.1)
        stop.set()

    workers = [threading.Thread(target=f, daemon=True) for f in (ctl_loop, watch_stop)]
    if us is not None:
        workers.append(threading.Thread(target=rx_loop, daemon=True))
    for t in workers:
        t.start()
    t_status = time.monotonic()
    g_s = G / mcr
    try:
        while not stop.is_set():
            blk = pk.get()
            if blk is None:
                continue
            seq = pk.last_seq
            n_first = n0 + seq * gpb
            air = T0 + n_first * G
            wait = ctl.host_at(air - lead_s * mcr) - time.perf_counter()
            if wait > 0:
                if stop.wait(wait):
                    break
            ahead = (air - ctl.tick_at(time.perf_counter())) / mcr
            if ahead < 0:                      # late: the board would drop them (null groups)
                counters["L"] += gpb
                counters["skipped"] += 1
                continue
            st["m_min"] = min(st["m_min"], ahead)
            for j in range(gpb):
                by_slot = {s: blk[i, j * GROUP:(j + 1) * GROUP] for s, i in by_ch.items()}
                sd.sendto(pack_group(n_first + j, mask, by_slot), data_addr)
            counters["sent"] += gpb
            if time.monotonic() - t_status > 1.0:
                t_status = time.monotonic()
                gap, st["gap_ms"] = st["gap_ms"], 0.0
                q.put(("status", dict(U=counters["U"], L=counters["L"], L_ms=counters["L"] * g_s * 1e3,
                                      O=counters["O"], gap_ms=gap, margin_ms=st["m_min"] * 1e3)))
                st["m_min"] = float("inf")
    finally:
        stop.set()
        for t in workers:
            t.join(1.0)
        if counters["L"]:
            log(f"!! edge PHY : {counters['L']} groupes envoyés trop tard (~{counters['L'] * g_s * 1e3:.0f} ms de downstream)")
        sd.close()
        if us is not None:
            us.close()
        ctl.close()


def status_text(host, port=HTTP_PORT, timeout=2.0):
    """The board's /status (JSON when it gives JSON), for diagnostics."""
    with urllib.request.urlopen(f"http://{host}:{port}/status", timeout=timeout) as r:
        t = r.read().decode("utf-8", "replace")
    try:
        return json.loads(t)
    except ValueError:
        return t
