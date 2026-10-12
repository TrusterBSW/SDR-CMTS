#!/usr/bin/env python3
"""Mini-CMTS, step 3: EuroDOCSIS downstream + upstream ranging on ONE USRP B210.

  B210 TX/RX (channel A) : downstream J.83A (as ds_tx.py)
  B210 RX2   (channel A) : upstream bursts (TDMA QPSK, 1.28 Msym/s, 60 MHz by default)

Both directions share the B210 master clock, so every DOCSIS timestamp maps to an exact RX
sample: the receiver knows where each MAP interval lands and measures the modem's timing
error to a few ns. The modem is taken through initial ranging (RNG-REQ -> RNG-RSP with a
temporary SID), unicast Station Maintenance until "success", then periodic maintenance.
Bandwidth requests (REQ, piggyback) get data grants (IUC 5/6); the modem's Ethernet frames are
bridged to a Linux TAP (cmts0) and back, and registration is handled (REG-REQ -> REG-RSP with
SFIDs/SIDs -> REG-ACK: online). DHCP, TFTP and ToD come from provision.py (--all-in-one starts it)
or from any server on cmts0.

  ./mini_cmts.py --all-in-one             # everything: CMTS + DHCP/TFTP/ToD (provision.py, sudo)
  ./mini_cmts.py                          # CMTS only, bench defaults (306 MHz, gain 49, US gain 20)
  ./mini_cmts.py --no-rsp                 # listen only: decode the RNG-REQ
  ./mini_cmts.py --save-bursts captures/bursts
  ./mini_cmts.py --sim 3                  # full loop with a simulated modem

Every hardware run writes captures/cmts_<date>_<time>.log (the console, without colours);
--log none to skip it. --pcap adds captures/cmts_<date>_<time>.pcap (decoded upstream frames
+ RNG-RSP sent).

NEVER connect this to the operator's network: closed coax lab only, with attenuators.
"""
import argparse
import atexit
import collections
import cProfile
import glob
import os
import subprocess
import queue
import shlex
import sys
import threading
import time

import numpy as np

from cmts import docsis_mac as mac
from cmts import cdsp, netpkt
from cmts.bridge import BondedForwarder, TapBridge, ensure_tap
from cmts.downstream import CoDel, DownstreamMux, SecondaryMux, SYMRATE
from cmts.j83a import PulseShaper
from cmts.pcap import PcapWriter
from cmts.radio import TX_CAL_RX_BW
from cmts.usproc import RemoteUpstream, SampleRing
from cmts.stallwatch import StallWatch
from cmts import termcolor
from cmts.ranging import CmtsCore
from cmts.ts_demux import DocsisDemux
from cmts.termcolor import log
from cmts.upstream import TS_RATE, Timebase, UpstreamRx

US_SYM_UNIT = 160e3


def rx_lo_offset(a):
    """UHD does not tune the B210 below 50 MHz: the LO sits above the upstream channel (3 MHz
    away from it, so the burst never sits on the DC / LO leakage) and the FPGA shifts it back."""
    if a.us_lo_offset is not None:
        return a.us_lo_offset
    half_bw = a.us_rate * US_SYM_UNIT * 1.25 / 2          # channel half width (roll-off 0.25)
    return max(3e6, half_bw + 1e6, 52e6 - a.us_freq)       # LO leakage outside the channel


def us_channels(a):
    """[(UCID, frequency)] of the upstream channels: --us-freq, then every --us-spacing."""
    return [(k + 1, a.us_freq + k * a.us_spacing) for k in range(a.us_channels)]


def ds_channels(a):
    """[(DCID, frequency)] of the downstream channels; the first one is primary."""
    k = 0                                           # index of the primary among the frequencies
    if getattr(a, "ds_layout", "up") == "around":
        # --freq (the primary: where the Sagem looks first) in the middle, the others on both
        # sides. 6 channels 306-346 MHz: the modem left 25-47 % of its upstream grants empty
        # under upload; 290-330 MHz with 306 primary: 0 %, 20.9 Mbit/s up, 247 down (2026-10-11)
        k = (a.ds_channels - 1) // 2
    freqs = [a.freq + (j - k) * a.ds_spacing for j in range(a.ds_channels)]
    if os.environ.get("MINI_CMTS_PRIMARY_IDX"):     # diagnostic: the k-th frequency is the primary
        k = int(os.environ["MINI_CMTS_PRIMARY_IDX"])
    freqs = [freqs[k]] + freqs[:k] + freqs[k + 1:]
    return [(j + 1, f) for j, f in enumerate(freqs)]


def build(a, log=print):
    if a.gain is None:                         # same level per channel whatever their number
        # 45 for one channel (~12 dBmV at the Sagem); bonded: +10 log10(N) to keep each channel's
        # level, +2 dB (the 2-channel bench ran at 50 for its best results, 2026-10-10)
        a.gain = 45 + round(10 * np.log10(max(1, a.ds_channels))) + (2 if a.ds_channels > 1 else 0)
    if a.ds_channels > 1:
        if a.ds_channels > 6:
            sys.exit("--ds-channels : 1 à 6 (au-delà, la bande du B210 et le processeur ne suivent plus)")
        # N 8 MHz channels side by side, each shaped at `sps` samples per symbol = master clock and
        # shifted around the TX centre.
        # 1-3 channels: 27.808 MS/s (3 channels at -8/0/+8 MHz fill +-12 of the +-13.9 MHz band).
        # 4 channels: 34.760 MS/s (5 per symbol; -12/-4/+4/+12 MHz fill +-16 of +-17.38): online on
        # 4 channels, 156 Mbit/s carried (2026-10-10, Proxmox host, --radio-process).
        # Above 34.76 MHz the Sagem used not to lock: UHD's TX quadrature calibration ran with the
        # RX filter at the upstream width and failed from 41.712 MHz on (radio.TX_CAL_RX_BW).
        # Since that fix it locks at 41.712 and 55.616 MHz; 5-6 channels at 55.616 MS/s run on the
        # Proxmox host with the AVX2 synthesis and 16 KB USB frames (2026-10-11).
        a.sps = 4 if a.ds_channels <= 3 else (5 if a.ds_channels <= 4 else 8)
        if a.ds_channels >= 4:
            # one synthesis thread per 2 channels (each thread encodes and synthesizes its own
            # channels, AVX2: ~0.17 core per channel at 55.6 MS/s). One per channel (6 at 55.6 MS/s)
            # was 9 real-time threads on 4 CPUs: the RX read waited, upstream decoding up to 220 ms
            # behind, ping 86-388 ms (2026-10-10)
            a.synth_threads = max(a.synth_threads, (a.ds_channels + 1) // 2)
        if os.environ.get("MINI_CMTS_DS_SPS"):     # diagnostic: force the samples per symbol
            a.sps = int(os.environ["MINI_CMTS_DS_SPS"])
        a.mcr_mult, a.docsis30 = int(os.environ.get("MINI_CMTS_MCR_MULT", "1")), True   # (diagnostic override)
    if a.mcr_mult is None:   # 27.8 MHz is enough while the offset stays inside the AD9361 passband
        a.mcr_mult = 2 if abs(rx_lo_offset(a)) <= 8e6 else 4
    mux = DownstreamMux(qam=a.qam, sps=a.sps, us_freq=a.us_freq, us_symrate_mult=a.us_rate,
                        map_lead_ms=float(os.environ.get("MINI_CMTS_MAP_LEAD_MS", 4)),   # (diagnostic override)
                        minislot_ticks=a.minislot, cmts_mac=a.mac, invert=not a.no_invert)
    core = CmtsCore(mux, target_db=a.us_target, sm_period=a.sm_period, respond=not a.no_rsp, log=log,
                    secret=a.shared_secret, strict_mic=a.strict_mic, grant_ahead_s=a.grant_ahead_ms / 1e3,
                    grant_ahead_hold=a.grant_ahead_hold, grant_ahead_every=a.grant_ahead_every,
                    grant_ahead_scale=a.grant_ahead_scale)
    core.docsis_version = 2 if a.us_mode == "atdma" else 1     # REG-RSP cap (3 with REG-REQ-MP)
    mux.scheduler = core.schedule
    mux.im_busy, mux.im_every_idle = core.acquiring, a.im_every_idle
    mcr = a.sps * SYMRATE * a.mcr_mult
    symrate = a.us_rate * US_SYM_UNIT
    # lowest RX rate with >= 2.5 samples per symbol; any integer decimation (41.712 MHz / 3 and
    # 55.616 / 4 give the same 13.904 MS/s as 27.808 / 2)
    decim = max(1, int(mcr // (2.5 * symrate)))
    if os.environ.get("MINI_CMTS_DECIM"):          # diagnostic: force the RX decimation
        decim = int(os.environ["MINI_CMTS_DECIM"])
    tb = Timebase(mcr, 0, a.sps)
    rx_kw = dict(gate_db=a.gate_db, save_dir=a.save_bursts, im_slack_us=a.im_slack_us,
                 ko_dir=None if a.sim else "captures/bursts_ko")
    if a.us_process and not a.sim:             # upstream worker in its own process (cmts/usproc.py)
        rx = RemoteUpstream(mcr / decim, decim, symrate, tb, mux.ms_counts, core.on_burst, a.us_mode,
                            {"qpsk": 2, "16qam": 4, "64qam": 6}[a.us_mod], log=log, **rx_kw)
    else:
        rx = UpstreamRx(mcr / decim, decim, symrate, tb, mux.ms_counts, core.on_burst, log=log, **rx_kw)
    core.ms_syms = int(round(a.minislot * 6.25e-6 * symrate))
    core.us_bits = {"qpsk": 2, "16qam": 4, "64qam": 6}[a.us_mod]
    if a.us_channels > 1:
        rx, decim = build_us_bonding(a, mux, core, tb, mcr, symrate, rx_kw, log)
        mux.on_grants = lambda g: rx.add_grants(core.mark_grants(g))     # segment grants flagged
    else:
        mux.on_grants = rx.add_grants
    mux.ack_ms = lambda: int(rx.processed_ts // mux.ms_counts)   # MAP Ack Time: upstream really processed
    # back-pressure: a channel whose receiver falls behind gets fewer grants (CmtsCore.rx_lag_ms)
    rx_by_ch = dict(zip([u for u, _ in us_channels(a)], getattr(rx, "all_channels", None) or [rx]))
    core.rx_lag_ms = lambda ch: getattr(rx_by_ch.get(ch), "lag_now_ms", 0.0)
    core.lag_ahead_ms, core.lag_data_ms = a.us_lag_limit / 2, a.us_lag_limit
    core.mtc_std = a.mtc_std
    core.mtc_owed = a.mtc_owed
    mux.data_max_ms = a.ds_queue_ms
    if a.ds_aqm:
        mux.aqm = CoDel(a.ds_aqm)
    mux.secondary = []
    if a.docsis30:                             # MAC Domain Descriptor: the modem runs in DOCSIS 3.0 mode
        chans = ds_channels(a)
        plan = [(cid, f, a.qam, cid == 1) for cid, f in chans]
        if os.environ.get("MINI_CMTS_MDD_MAX"):   # diagnostic: only the first k channels in the MDD
            plan = plan[:int(os.environ["MINI_CMTS_MDD_MAX"])]
        ucids = [u for u, _ in us_channels(a)]
        # several upstream channels: ranging only on the first one (ambiguity resolution list),
        # transmit power reporting on (without it no CM may get more than one upstream channel)
        mdd_kw = dict(ambiguity=[1], power_report=True) if len(ucids) > 1 else {}
        # CM-STATUS reports enabled (2026-10-10): a secondary channel the modem fails to acquire
        # at registration, then acquires (or a lost upstream channel...), is reported, not unknown
        mux.mdd = mac.mdd_msg(mux.sa, 1, 1, plan, ucids, status_events=sorted(mac.CM_STATUS_EVENTS), **mdd_kw)
        for cid, f in chans[1:]:
            m2 = SecondaryMux(qam=a.qam, cmts_mac=a.mac, invert=not a.no_invert)
            m2.mdd = mac.mdd_msg(mux.sa, 1, cid, plan, ucids, **mdd_kw)
            m2.data_max_ms = a.ds_queue_ms
            if a.ds_aqm:
                m2.aqm = CoDel(a.ds_aqm)
            mux.secondary.append(m2)
        if len(chans) > 1:
            core.bonding = chans
            if os.environ.get("MINI_CMTS_RCC_MAX"):   # diagnostic: only the first k bonded (all transmitted)
                core.bonding = chans[:int(os.environ["MINI_CMTS_RCC_MAX"])]
    return mux, core, rx, tb, mcr, decim


def build_us_bonding(a, mux, core, tb, mcr, symrate, rx_kw, log):
    """Several upstream channels (DOCSIS 3.0 upstream bonding): the B210 receives one wideband
    stream centred between the channels, at the lowest master clock / decim rate that holds them
    (at 55.616 MHz, 5-6 DS, the full rate would be 445 MB/s of USB with the TX); one upstream
    process per channel takes its channel out of the shared ring (cmts/usproc.py, C DDC).
    Channel 1 is the one modems range on; the others get their own UCD and MAP stream.
    Returns (receiver of channel 1, RX decimation)."""
    chans = us_channels(a)
    if a.sim or not a.us_process:
        sys.exit("--us-channels > 1 : sur le matériel, avec --us-process, uniquement")
    a.us_center = (chans[0][1] + chans[-1][1]) / 2
    width = symrate * 1.25
    span = abs(chans[-1][1] - chans[0][1]) + width
    if max(f for _, f in chans) + width / 2 > 65e6:
        log(f"!! canal montant au-delà de 65 MHz : hors de la bande montante EuroDOCSIS, le modem refuse "
            f"le TCC (REG-ACK 209, reject-bad-tcc, vu le 2026-10-10 avec 67 MHz)")
    if span > 0.8 * mcr:
        sys.exit(f"--us-channels/--us-spacing : {span / 1e6:.1f} MHz à "
                 f"recevoir, plus que les {0.8 * mcr / 1e6:.1f} MHz utiles à {mcr / 1e6:.3f} MS/s")
    # powers of 2 only: the B210's FPGA decimates by 2 with half-band filters; an odd factor goes
    # through its CIC alone, and 5 DS + 2 US at 55.616 MHz / 3 left the upstream at MER 14 dB
    # instead of 26 (2026-10-10). At /2 the same pair took 333 MB/s of USB: TX late, RX overflows.
    decim = max(d for d in (1, 2, 4, 8) if span <= 0.8 * mcr / d)
    if (mcr + mcr / decim) * 4 > 300e6:
        log(f"!! USB : {(mcr + mcr / decim) * 4 / 1e6:.0f} Mo/s (émission + réception large bande) : au-delà "
            f"d'environ 300 Mo/s, le B210 décroche (essai du 2026-10-10)")
    if os.environ.get("MINI_CMTS_DECIM"):          # diagnostic: force the RX decimation
        decim = int(os.environ["MINI_CMTS_DECIM"])
    fs = mcr / decim
    ring = SampleRing(fs, decim, history_s=0.5)
    # decimated before demodulation down to >= 2.3 samples per symbol (5.12 Msym/s occupies 6.4 MHz):
    # 24.332 MS/s / 2 = 2.38 per symbol, MER 35.8 dB instead of 36.7 on the real burst of
    # tests/test_usproc.py, for about half the demodulation work (2026-10-10)
    factor = max(1, int(fs // (2.3 * symrate)))
    bits = {"qpsk": 2, "16qam": 4, "64qam": 6}[a.us_mod]
    rxs = []
    for ucid, f in chans:
        def on_burst(ev, ucid=ucid):
            ev.us_ch = ucid
            core.on_burst(ev)
        rxs.append(RemoteUpstream(fs, decim, symrate, tb, mux.ms_counts, on_burst, a.us_mode, bits,
                                  history_s=1.0, log=log, ring=ring, name=f"upstream{ucid}",
                                  ddc=dict(offset=f - a.us_center, factor=factor, cutoff=width / 2 + 1e6),
                                  **rx_kw))
    for (ucid, f), r in zip(chans[1:], rxs[1:]):
        mux.add_upstream(ucid, f, scheduler=core.channel_scheduler(ucid),
                         on_grants=lambda g, r=r: r.add_grants(core.mark_grants(g)),
                         ack_ms=lambda r=r: int(r.processed_ts // mux.ms_counts), im=False)
    core.us_rx = rxs
    rxs[0].all_channels = rxs
    core.us_chans = chans
    log(f"{len(chans)} canaux montants : " + ", ".join(f"{u}@{f / 1e6:.2f} MHz" for u, f in chans)
        + f" ; réception large bande {fs / 1e6:.3f} MS/s centrée sur {a.us_center / 1e6:.2f} MHz, "
          f"1 processus par canal (DDC /{factor})")
    return rxs[0], decim


def attach_pcap(path, mux, rx, wall_of_ts):
    """Every decoded upstream frame (RNG-REQ, REQ...) and every unicast message the CMTS sends
    (RNG-RSP) into a DLT_DOCSIS pcap. Both are stamped on the DOCSIS time base (burst arrival,
    RNG-RSP air time), so the gaps between them in Wireshark are the real ones on the cable;
    wall_of_ts() maps that time base to the Unix clock (to a few ms)."""
    pcap = PcapWriter(path)
    for r in getattr(rx, "all_channels", None) or [rx]:
        def tap(ev, on_burst=r.on_burst):
            if ev.burst is not None and ev.burst.frame:
                pcap.write(wall_of_ts(ev.rx_ts), ev.burst.frame)
            on_burst(ev)
        r.on_burst = tap
    mux.on_ucast = lambda frame, air_ts: pcap.write(wall_of_ts(air_ts), frame)
    for m2 in getattr(mux, "secondary", []):
        m2.on_ucast = lambda frame, _ts: pcap.write(time.time(), frame)
    return pcap


class LineLimiter:
    """At most `rate` lines per second for a chatty source; the rest is counted."""
    def __init__(self, rate=20):
        self.rate, self.t, self.n, self.dropped = rate, 0.0, 0, 0

    def ok(self):
        now = time.time()
        if now - self.t >= 1:
            self.t, self.n = now, 0
        self.n += 1
        if self.n > self.rate:
            self.dropped += 1
            return False
        return True


def start_bridge(a, mux, core, stop):
    """Modems <-> Linux TAP (a.tap): upstream Packet PDUs out on the TAP, frames from the TAP
    into downstream Packet PDUs."""
    if not a.tap:
        core.on_data = lambda eth, ok, sid: log(f"[PONT] ↑ {netpkt.summary(eth)} (pas de --tap : jetée)")
        return None
    br = TapBridge(a.tap, log)
    lim = LineLimiter()                        # console
    flim = LineLimiter(200)                    # log file: every frame up to 200 lines/s, then counted

    def up(eth, ok, sid):
        if not ok:
            br.stats["bad_fcs"] += 1
            log(f"[PONT] !! ↑ FCS Ethernet faux, trame de SID {sid:#06x} jetée ({len(eth)} octets)")
            return
        fwd.learn(eth, sid)                        # which modem this address sits behind
        br.upstream(eth)
        if flim.ok():
            log(f"[PONT] ↑ {netpkt.summary(eth)}", console=lim.ok())

    fwd = BondedForwarder([mux] + list(getattr(mux, "secondary", [])), core, mac)
    if os.environ.get("MINI_CMTS_BOND_CH"):         # diagnostic: MINI_CMTS_BOND_CH=2 -> channel 2 only
        fwd.force_ch = int(os.environ["MINI_CMTS_BOND_CH"]) - 1
    mux.per_chan = fwd.per_chan

    def down(eth):
        fwd.send(eth)
        if flim.ok():
            log(f"[PONT] ↓ {netpkt.summary(eth)}", console=lim.ok())
    core.on_data = up
    br.start(down, stop)
    log(f"[PONT] modems <-> {a.tap} (état {br.link_up()})")
    return br


def start_provision(a):
    """--all-in-one: provision.py (DHCP, TFTP, ToD) as root on the TAP, its lines in our log.
    Started before the real-time priority is raised, so that it does not inherit it."""
    cmd = ["sudo", sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "provision.py"),
           "--iface", a.tap, "--secret", a.shared_secret] + shlex.split(a.prov_args)
    log(f"[PROV] lancement : {' '.join(cmd)}")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    def relay():
        for line in p.stdout:
            log(line.rstrip())
        if p.poll() not in (None, 0, -15):
            log(f"[PROV] !! provision.py s'est arrêté (code {p.returncode})")
    threading.Thread(target=relay, name="provision", daemon=True).start()

    def stop():
        if p.poll() is None:
            subprocess.run(["sudo", "kill", str(p.pid)], stderr=subprocess.DEVNULL)
            for c in [c for c in _children(p.pid)]:
                subprocess.run(["sudo", "kill", str(c)], stderr=subprocess.DEVNULL)
    atexit.register(stop)
    return p


def _children(pid):
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(x) for x in f.read().split()]
    except OSError:
        return []


def _take_lag(r):
    lag, r.lag_max_ms = r.lag_max_ms, 0.0
    return lag


_mtc_t = [time.monotonic()]


def mtc_line(core):
    """Diagnostic (MINI_CMTS_TIMING): Multiple Transmit Channel mode upstream since the last
    line, per second: grants (asked / in advance, mini-slots -> raw bytes at the data profile),
    bytes carried by the segments, padding, queue-depth requests, reassembly waits."""
    now = time.monotonic()
    dt, _mtc_t[0] = max(1e-3, now - _mtc_t[0]), now
    st, core.mtc_stat = core.mtc_stat, collections.defaultdict(collections.Counter)
    bpm = core.ms_syms * getattr(core, "us_bits", 6) / 8      # raw bytes per mini-slot
    parts = []
    for ch in sorted(c for c in st if c):
        c = st[ch]
        parts.append(f"US{ch}: grants {c['req_grants'] / dt:.0f}+{c['ahead_grants'] / dt:.0f} av./s "
                     f"{(c['req_ms'] + c['ahead_ms']) * bpm / dt / 1e3:.0f} ko/s bruts, segments {c['segs'] / dt:.0f}/s "
                     f"données {c['data_bytes'] / dt / 1e3:.0f} ko/s, bourrage {c['pad_bytes'] / dt / 1e3:.0f} ko/s "
                     f"({c['pad_segs'] / dt:.0f} seg/s vides)")
    r = st.get(0, {})
    if r:
        parts.append(f"requêtes {r['reqs'] / dt:.0f}/s moy {r['req_bytes'] / max(1, r['reqs']):.0f} o max {r['req_max']} o")
    waits = [m.reasm.wait for m in core.modems.values() if getattr(m, "reasm", None)]
    for w in waits:
        parts.append(f"réassemblage : {w[0] / dt:.0f} seg/s attendus, moy {w[1] / max(1, w[0]) * 1e3:.0f} ms max {w[2] * 1e3:.0f} ms")
        w[:] = [0, 0.0, 0.0]
    return "[MTC] " + " | ".join(parts)


def status_line(mux, core, rx, extra=""):
    st = rx.stats
    fl = rx.noise_dbfs()
    mods = "; ".join(f"{m['mac']} SID {m['sid']:#06x} {m['state']}"
                     + (f" ({m['err_us']:+.2f} µs, {m['power']:.1f} dBFS, {m['freq']:+.0f} Hz, MER {m['mer']:.1f})"
                        if 'err_us' in m else "")
                     + (" + " + ", ".join(f"US{u} SID {sid:#06x} {st}" for u, (sid, st) in sorted(m['links'].items()))
                        if m.get('links') else "")
                     + (f" [segments {m['reasm']['segments']}, trames {m['reasm']['frames']}, perdus {m['reasm']['lost']}]"
                        if m.get('reasm') else "") for m in core.summary()) or "aucun modem"
    lag, rx.lag_max_ms = rx.lag_max_ms, 0.0
    if os.environ.get("MINI_CMTS_TIMING") and rx.timing:   # diagnostic: worker time per window type
        log("[TEMPS] " + ", ".join(f"{k}: {v[0]} × {v[1] * 1e3 / max(v[0], 1):.2f} ms = {v[1]:.2f} s"
                                   for k, v in sorted(rx.timing.items())))
        rx.timing.clear()
        if core.mtc_stat:
            log(mtc_line(core))
    return (f"[{time.strftime('%H:%M:%S')}] {extra}US: bruit {'?' if fl is None else f'{fl:.1f}'} dBFS, "
            f"retard max {lag:.0f} ms" + (f" (file {st['windows_queued']}, abandonnées {st['late']})"
                                          if 'windows_queued' in st else "")
            + f", rafales {st['bursts']} (décodées {st['decoded']}, en C {st.get('fast', 0)}, énergie brute {st['raw']}"
            + (f", profil inversé {st['iuc_swap']}" if st.get('iuc_swap') else "") + "), "
            + "".join(f" | US{k + 2} : bruit {'?' if r.noise_dbfs() is None else f'{r.noise_dbfs():.1f}'} dBFS, "
                      f"retard max {_take_lag(r):.0f} ms, rafales {r.stats['bursts']} (décodées {r.stats['decoded']}, "
                      f"en C {r.stats.get('fast', 0)}"
                      + (f", profil inversé {r.stats['iuc_swap']}" if r.stats.get('iuc_swap') else "") + ")"
                      for k, r in enumerate(getattr(rx, "all_channels", [rx])[1:]))
            + f", RNG-RSP {core.n_rsp} | grants {core.n['req']} demandés + {core.n['ahead']} en avance "
            f"({core.n['empty']} vides), concat {core.n['concat']}"
            + ("".join(f", retenus {k} {v} MAP" for k, v in sorted(core.n_held.items())) if core.n_held else "")
            + f" | {mods}")


# ---------------------------------------------------------------- offline simulation
def run_sim(a, log=print):
    from cmts.sim_modem import SimModem
    mux, core, rx, tb, mcr, decim = build(a, log)
    wall0 = time.time()
    pcap = attach_pcap(a.pcap, mux, rx, lambda ts: wall0 + ts / TS_RATE) if a.pcap else None
    rx.push(0, np.zeros(0, np.complex64))       # RX sample 0 = device tick 0
    modem = SimModem(bytes.fromhex("0024d1c0ffee"), rx, delay_us=a.sim_delay_us, power_db=a.sim_power,
                     freq_hz=a.sim_freq, snr_db=a.sim_snr, symrate=a.us_rate * US_SYM_UNIT,
                     ms_counts=mux.ms_counts, log=log)
    dm = DocsisDemux()
    pending = []
    latency = int(a.sim_latency_ms * 1e-3 * TS_RATE)   # downstream generation -> air time
    rendered = 0
    t_last = 0
    log(f"simulation: {a.sim} s, modem {a.sim_delay_us} µs de retard, {a.sim_power} dBFS, {a.sim_freq:+.0f} Hz, "
        f"SNR {a.sim_snr} dB, latence TX {a.sim_latency_ms} ms; RX {mcr / decim / 1e6:.3f} MS/s")
    while mux.now_ts < a.sim * TS_RATE:
        for _ in range(64):
            ts = mux.pkt_air_ts(mux.p)
            dm.push_packet(mux.next_packet())
            if dm.frames:
                pending.extend((ts, f) for f in dm.frames)
                dm.frames.clear()
        now = mux.now_ts - latency                     # what is on the air right now
        while pending and pending[0][0] <= now:
            ts, f = pending.pop(0)
            modem.on_frame(f, ts)
        modem.tick_time(now)
        n_end = int(rx.sample_of_ts(now))
        if n_end > rendered:
            rx.push(rx.rx_tick0 + rendered * decim, modem.render(rendered, n_end - rendered))
            rendered = n_end
        rx.process()
        rx.scan_raw()
        if now - t_last > TS_RATE:
            t_last = now
            log(status_line(mux, core, rx, f"t={now / TS_RATE:.1f}s "))
    res = dict(state=modem.state, residual_us=(modem.delay - modem.offset) / TS_RATE * 1e6,
               power=modem.power, freq=modem.freq, t3=modem.t3_timeouts, tx=modem.tx_count,
               success_s=None if modem.success_ts is None else modem.success_ts / TS_RATE,
               rsp_delays=[e[2]["delay_ms"] for e in core.events if e[1] == "RNG-RSP"],
               reqs=sum(1 for e in core.events if e[1] == "REQ"), stats=dict(rx.stats))
    if pcap:
        pcap.close()
        log(f"pcap: {pcap.count} trames dans {a.pcap}")
    log(f"fin: modem {res['state']}, erreur de timing résiduelle {res['residual_us'] * 1e3:+.1f} ns, puissance "
        f"{res['power']:.2f} dBFS (cible {a.us_target}), fréquence {res['freq']:+.0f} Hz, T3 {res['t3']}, "
        f"rafales émises {res['tx']}, REQ décodées {res['reqs']}, ranging réussi à t={res['success_s']} s")
    return res


# ---------------------------------------------------------------- hardware
RT_HELP = """   pour l'autoriser (une fois, puis ferme et rouvre ta session) :
     sudo mkdir -p /etc/security/limits.d
     printf '@usrp - rtprio 99\\n@usrp - nice -20\\n' | sudo tee /etc/security/limits.d/99-usrp.conf
   (tu es déjà dans le groupe usrp ; vérifie après reconnexion avec : ulimit -r  -> 99)"""


def raise_priority(prio):
    """Real-time scheduling, set before UHD and the worker threads start so that every thread
    of the process inherits it: the desktop or a compilation can no longer delay the TX feed
    (underflows) or the RX reads (overflows). All threads get the same priority on purpose:
    with the Python GIL, a lower-priority thread holding it would block the others."""
    if prio <= 0:
        return "priorité normale (--rt-prio 0)"
    try:
        os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(prio))
        return f"priorité temps réel SCHED_RR {prio}"
    except PermissionError:
        pass
    try:
        os.setpriority(os.PRIO_PROCESS, 0, -20)
        return "!! temps réel refusé, nice -20 seulement\n" + RT_HELP
    except PermissionError:
        return "!! priorité normale : temps réel et nice refusés par le système\n" + RT_HELP


def tx_cores(a):
    """Cores the transmit side (radio, MAC layer, bridge) needs to itself: 6 DS + 2 US with 3
    was late on the Proxmox host; 4 DS + 2 US with 3 and 6 DS + 1 US with 4 ran (2026-10-11)."""
    return 4 if a.ds_channels >= 5 else (3 if a.ds_channels == 4 else 2)


def _all_tasks(pid):
    try:
        return [int(t) for t in os.listdir(f"/proc/{pid}/task")]
    except OSError:
        return [pid]


def split_cpus(child_pids, shared_pids=(), tx_need=2, mode="auto", prio=0):
    """Give the upstream process (cmts/usproc.py) a physical core of its own, the last one
    allowed, and keep every thread of this process (TX feed, encoder, UHD) on the others: they
    no longer compete for the same cores (2026-10-09: TX gaps of 0.3-2 s under load).
    tx_need: cores the transmit side needs (6 DS: 4). When cores of their own would leave it
    fewer (mode "auto"), or mode "shared": the upstream processes run on every core at a lower
    real-time priority (prio - 10), the transmit side preempting them (2026-10-11, 6 DS + 2 US:
    radio on 3 cores late, or both upstream processes on 1 core 0.6-1.6 s behind)."""
    allowed = sorted(os.sched_getaffinity(0))

    def core(c):
        try:
            with open(f"/sys/devices/system/cpu/cpu{c}/topology/core_id") as f:
                return int(f.read())
        except OSError:
            return c
    cores = sorted({core(c) for c in allowed})
    # (auto: only from 5 DS on, the case measured; fewer channels keep the split below)
    if mode == "shared" or (mode == "auto" and tx_need >= 4 and len(cores) - len(child_pids) < tx_need):
        return share_cpus(child_pids, allowed, prio)
    if len(cores) < 3:
        return f"processus montant : CPU partagés ({','.join(map(str, allowed))}, trop peu de cœurs pour en réserver un)"
    # one physical core per upstream process while at least two stay for the transmit side (a
    # 6-core PC without hyperthreading, 2 upstream channels: 2 cores each side); fewer cores
    # (the 4-core laptop): the last one or two, shared (3+ upstream channels: two)
    if len(cores) - len(child_pids) >= 2:
        take = cores[-len(child_pids):]
    else:
        take = cores[-2:] if len(child_pids) > 2 and len(cores) >= 3 else cores[-1:]
    if os.environ.get("MINI_CMTS_US_CORES"):      # diagnostic: physical cores for the upstream processes
        take = cores[-int(os.environ["MINI_CMTS_US_CORES"]):]
    mine = {c for c in allowed if core(c) in take}
    rest = set(allowed) - mine
    try:
        for pid in child_pids:
            os.sched_setaffinity(pid, mine)
        # this process and the radio process (cmts/radio.py, every thread) share the others
        for tid in _all_tasks(os.getpid()) + [t for pid in shared_pids for t in _all_tasks(pid)]:
            try:
                os.sched_setaffinity(tid, rest)
            except OSError:
                pass
    except OSError as e:
        return f"!! répartition des CPU impossible : {e}"
    return (f"processus montant sur les CPU {','.join(map(str, sorted(mine)))}, émission sur "
            f"{','.join(map(str, sorted(rest)))}")


def share_cpus(child_pids, allowed, prio):
    """split_cpus() without cores of their own: every upstream process on every allowed CPU,
    real-time priority 10 below the transmit side's (SCHED_RR, inherited by their later threads)."""
    us_prio = max(1, prio - 10) if prio > 0 else 0
    try:
        for pid in child_pids:
            for tid in _all_tasks(pid):
                os.sched_setaffinity(tid, allowed)
                if us_prio:
                    os.sched_setscheduler(tid, os.SCHED_RR, os.sched_param(us_prio))
    except OSError as e:
        return f"!! répartition des CPU impossible : {e}"
    return (f"processus montants sur les CPU {','.join(map(str, allowed))}, partagés avec l'émission"
            + (f" (priorité temps réel {us_prio}, l'émission passe devant)" if us_prio else ""))


def tx_dc_calibrate(uhd, usrp, tx_stream, rx_stream, tx_center, rx_restore, log, delta=0.02):
    """Measure the B210's TX LO leakage with its own RX and return the complex constant that,
    added to the TX baseband, cancels it. A channel sitting on the TX centre (3 bonded channels:
    the middle one) had the leakage in its middle, ~-31 dBc measured with the HackRF (2026-10-10).

    The RX is tuned 100 kHz above the TX centre (the leakage at -100 kHz, away from the RX's own
    DC); the TX sends a constant c (c0, c0 +- d, c0 + jd); the leakage power |L + g c|^2 at
    -100 kHz is a paraboloid in c whose minimum c* = -L/g follows from the four powers (no phase
    reference needed). Two rounds, the second around the first estimate with a smaller d.
    The TX is fed by a thread during each capture: send() returns before the samples are on the
    air, and a TX that runs dry in the middle of a capture biased the first version (2026-10-10:
    -18 dB on one start, -2 dB on the next). rx_restore(): retunes the RX to the upstream."""
    fs = usrp.get_rx_rate()
    usrp.set_rx_freq(uhd.types.TuneRequest(tx_center + 100e3, 2e6))
    n = int(fs * 0.02)
    blk_n = int(usrp.get_tx_rate() * 0.002)

    def power(c):
        stop = threading.Event()
        blk = np.full(blk_n, c, np.complex64)

        def feed():
            md = uhd.types.TXMetadata()
            md.start_of_burst, md.end_of_burst = True, False
            while not stop.is_set():
                tx_stream.send(blk, md)
                md.start_of_burst = False
            md.end_of_burst = True
            tx_stream.send(np.zeros(16, np.complex64), md)
        th = threading.Thread(target=feed, daemon=True)
        th.start()
        time.sleep(0.05)                                  # the constant is on the air by now
        buf = np.zeros((1, n), np.complex64)
        rmd = uhd.types.RXMetadata()
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        cmd.num_samps, cmd.stream_now = n, True
        rx_stream.issue_stream_cmd(cmd)
        got = 0
        while got < n:
            k = rx_stream.recv(buf[:, got:], rmd, 1.0)
            if k == 0:
                break
            got += k
        stop.set()
        th.join(1.0)
        x = buf[0, :got]
        t = np.arange(x.size) / fs
        return float(np.abs(np.mean(x * np.exp(2j * np.pi * 100e3 * t))) ** 2)

    def solve(c0, d):
        p0, p1, p2, p3 = power(c0), power(c0 + d), power(c0 - d), power(c0 + 1j * d)
        g2 = (p1 + p2 - 2 * p0) / (2 * d ** 2)
        if g2 <= 0:
            raise ValueError("mesure incohérente")
        cr = (d ** 2 - (p1 - p0) / g2) / (2 * d)
        ci = (d ** 2 - (p3 - p0) / g2) / (2 * d)
        return c0 + complex(cr, ci), p0
    try:
        c1, p_raw = solve(0j, delta)
        c2, _ = solve(c1, max(0.002, abs(c1) / 2))
        best = min(((power(c), c) for c in (c1, c2)), key=lambda pc: pc[0])
        pc, c = best
        rx_restore()
        if pc >= p_raw or abs(c) > 0.1:
            return 0j, f"calibration de la fuite TX : sans effet ({10 * np.log10(pc / p_raw):+.1f} dB), non appliquée"
        return c, (f"calibration de la fuite TX : correction {c.real:+.4f}{c.imag:+.4f}j (|c| {abs(c):.4f}), "
                   f"fuite {10 * np.log10(pc / p_raw):+.1f} dB")
    except (ValueError, RuntimeError) as e:
        rx_restore()
        return 0j, f"!! calibration de la fuite TX impossible : {e}"


def on_battery():
    """A warning line when the PC runs on battery, else None. On battery the laptop's processor
    is held near 3 GHz instead of 4: on 2026-10-10 the 3 DS + 2 US signal was then produced
    slower than real time and the downstream never started (charger unplugged)."""
    import glob
    mains = [p for p in glob.glob("/sys/class/power_supply/*") if _read(p + "/type") == "Mains"]
    if mains and not any(_read(p + "/online") == "1" for p in mains):
        return ("!! PC SUR BATTERIE (secteur débranché) : processeur bridé, l'émission risque de ne pas "
                "suivre le temps réel (3 DS et plus surtout). Branche le chargeur.")
    return None


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def herd_irqs(irq_cpus):
    """Move every hardware interrupt except the USB host controllers' onto `irq_cpus` (the core
    already left to the B210's USB interrupts). The kernel places them where it likes and moves
    them: the touchpad's i2c interrupt (IRQ 40, 21 million on CPU 3) took the CPU from our
    real-time threads whenever the PC was used (2026-10-09: TX gaps of 0.2-2 s, upstream process
    starved, 12-37 ms per window instead of 0.1). Needs sudo; returns a line for the log."""
    if not irq_cpus:
        return "interruptions : rien à déplacer (CPU de l'USB inconnu)"
    target = ",".join(map(str, sorted(irq_cpus)))
    try:
        with open("/proc/interrupts") as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return "interruptions : /proc/interrupts illisible"
    irqs = []
    for line in lines:
        head = line.split(":")[0].strip()
        if head.isdigit() and "xhci_hcd" not in line:
            irqs.append(head)
    moved, failed = 0, 0
    for irq in irqs:
        r = subprocess.run(["sudo", "-n", "tee", f"/proc/irq/{irq}/smp_affinity_list"], input=target,
                           capture_output=True, text=True)
        if r.returncode == 0:
            moved += 1
        else:
            failed += 1      # per-CPU timers and managed interrupts (NVMe queues) refuse: fine
    return f"interruptions : {moved} déplacées sur les CPU {target}, {failed} non déplaçables (gérées par le noyau)"


def usb_irq_cpus(siblings=True):
    """CPUs that serve the USB host controllers' interrupts, with their SMT siblings."""
    cpus = set()
    try:
        with open("/proc/interrupts") as f:
            lines = f.read().splitlines()
    except OSError:
        return cpus
    for line in lines:
        if "xhci_hcd" not in line:
            continue
        irq = line.split(":")[0].strip()
        try:
            with open(f"/proc/irq/{irq}/effective_affinity_list") as f:
                cpus |= parse_cpus(f.read())
        except (OSError, ValueError):
            pass
    for c in list(cpus) if siblings else []:
        try:
            with open(f"/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list") as f:
                cpus |= parse_cpus(f.read())
        except (OSError, ValueError):
            pass
    return cpus


def parse_cpus(text):
    out = set()
    for part in text.strip().split(","):
        if part:
            lo, _, hi = part.partition("-")
            out.update(range(int(lo), int(hi or lo) + 1))
    return out


def pin_cpus(spec):
    """Keep every thread of the process (UHD's included: set before they start) off the core that
    takes the B210's USB interrupts. With real-time priority, a thread of ours on that core starves
    the USB softirq work (ksoftirqd runs at normal priority) and the B210 feed stalls for up to
    hundreds of ms (seen on 2026-10-09: U/L bursts, one 550 ms gap)."""
    allowed = os.sched_getaffinity(0)
    if spec == "all":
        return f"CPU: tous ({len(allowed)})"
    if spec == "auto":
        irq = usb_irq_cpus()
        keep = allowed - irq
        if not irq or len(keep) < 2:
            return f"CPU: tous (interruptions USB sur {sorted(irq) or '?'}, pas d'isolement possible)"
        os.sched_setaffinity(0, keep)
        return f"CPU {','.join(map(str, sorted(keep)))} (interruptions USB sur {','.join(map(str, sorted(irq)))}, évitées)"
    keep = parse_cpus(spec)
    os.sched_setaffinity(0, keep)
    return f"CPU {','.join(map(str, sorted(keep)))} (--cpus)"


CPUFREQ = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/"


def _read_cpufreq(name):
    out = {}
    for path in sorted(glob.glob(CPUFREQ + name)):
        try:
            with open(path) as f:
                out[path] = f.read().strip()
        except OSError:
            pass
    return out


def _write_cpufreq(values):
    """{sysfs path: value} through sudo tee (one call per distinct value)."""
    by_val = {}
    for path, v in values.items():
        by_val.setdefault(v, []).append(path)
    for v, paths in by_val.items():
        r = subprocess.run(["sudo", "tee", *paths], input=v + "\n", text=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode:
            return r.stderr.strip() or f"sudo tee: code {r.returncode}"
    return None


def cpu_governor(want):
    """--performance: every CPU in the 'performance' governor for the run, the previous governor
    and energy/performance preference restored on exit. Otherwise only warn if it is not."""
    gov = _read_cpufreq("scaling_governor")
    if not gov:
        return None
    if all(v == "performance" for v in gov.values()):
        return "gouverneur CPU performance"
    current = sorted(set(gov.values()))
    if not want:
        return (f"!! gouverneur CPU {'/'.join(current)} : la fréquence varie et retarde l'émission. "
                f"Relance avec --performance")
    epp = _read_cpufreq("energy_performance_preference")
    err = _write_cpufreq({p: "performance" for p in gov})
    if err:
        return f"!! gouverneur CPU non modifié ({err}), reste {'/'.join(current)}"

    def restore():
        e = _write_cpufreq(gov) or (_write_cpufreq(epp) if epp else None)
        print(f"gouverneur CPU {'/'.join(current)} restauré" if not e else f"!! gouverneur CPU non restauré: {e}")
    atexit.register(restore)
    return f"gouverneur CPU performance (était {'/'.join(current)}, restauré à l'arrêt)"


# holds the PM QoS request until its stdin closes (our exit, even a crash): the kernel drops it
# with the file descriptor
_LATENCY_HOLDER = ("import os, struct, sys; fd = os.open('/dev/cpu_dma_latency', os.O_WRONLY); "
                   "os.write(fd, struct.pack('i', 0)); sys.stdin.read()")


def cpu_latency(want):
    """--performance: no deep C-state for the run (/dev/cpu_dma_latency = 0, released when the
    file is closed). On the Proxmox host (C10, 890 µs exit) the B210 at 27.8 MS/s both ways
    lost 13 RX overruns and 62 TX underruns in 90 s of benchmark_rate, 0 and 1 with this
    (2026-10-10); in the CMTS, 85 ms TX+RX stalls that cost the modem its second channel."""
    if not want or not os.path.exists("/dev/cpu_dma_latency"):
        return None
    if os.access("/dev/cpu_dma_latency", os.W_OK):
        cmd = [sys.executable, "-c", _LATENCY_HOLDER]
    else:
        cmd = ["sudo", "-n", sys.executable, "-c", _LATENCY_HOLDER]
    try:
        holder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        return f"!! états de veille profonds non bloqués ({e})"
    time.sleep(0.3)
    if holder.poll() is not None:
        err = holder.stderr.read().decode(errors="replace").strip().splitlines()
        return f"!! états de veille profonds non bloqués ({err[-1] if err else holder.returncode})"
    globals()["_latency_holder"] = holder       # the pipe stays open as long as we run
    return "états de veille profonds bloqués (/dev/cpu_dma_latency 0) pendant l'exécution"


def ds_fill(mux, prev):
    """Share of the TS packets of each downstream channel that were not null since the last
    call ("remplissage 97/96/... %"): a channel near 100 % is full. prev: dict kept by the caller."""
    out = []
    for k, m in enumerate([mux] + list(getattr(mux, "secondary", []))):
        p, nul = m.p, m.stats["null"]
        p0, n0 = prev.get(k, (p, nul))
        prev[k] = (p, nul)
        out.append(f"{100 * (1 - (nul - n0) / (p - p0)):.0f}" if p > p0 else "-")
    return "remplissage " + "/".join(out) + " %, "


def run_split(a, mux, core, rx, tb, mcr, decim, pcap, wall0, args, fs_tx, chans, tx_center):
    """--radio-process: the B210, J.83 encoding, shaping and the timed send in a process of
    their own (cmts/radio.py); this process keeps the MAC layer, the bridge and builds the TS
    packets of every channel. Same time base and start sequence as the single-process loop."""
    from cmts.radio import RadioProcess
    if not isinstance(rx, RemoteUpstream):
        sys.exit("--radio-process : avec --us-process uniquement (la réception écrit dans l'anneau partagé)")
    if a.tx_dc_cal:
        log("!! --tx-dc-cal ignoré avec --radio-process")
    span = (max(f for _, f in chans) - min(f for _, f in chans)) + 8e6
    if a.us_channels > 1:
        rx_target, lo_off = a.us_center, 0.0
        rx_bw = min(56e6, abs(a.us_spacing) * (a.us_channels - 1) + a.us_rate * US_SYM_UNIT * 1.25 + 4e6)
    else:
        rx_target, lo_off = a.us_freq, rx_lo_offset(a)
        rx_bw = min(56e6, 2 * (abs(lo_off) + 2e6))
    t_start = 0.5
    blk_ms = a.block * 204 * 8 / (SYMRATE * mux.m) * 1e3
    params = dict(args=args, mcr=mcr, fs_tx=fs_tx, tx_center=tx_center, tx_bw=min(56e6, max(20e6, 1.5 * span)),
                  gain=a.gain, decim=decim, rx_target=rx_target, rx_lo_off=lo_off, rx_bw=rx_bw,
                  rx_gain=a.us_gain, steps=[2 * np.pi * (f - tx_center) / fs_tx for _, f in chans],
                  qams=[a.qam] * len(chans), sps=a.sps, invert=mux.invert, scale=a.scale,
                  synth_threads=a.synth_threads, tx_queue=a.tx_queue, tx_frames=a.tx_frames,
                  tx_untimed=a.tx_untimed, t_start=t_start, blk_ms=blk_ms,
                  pk_shape=(len(chans), a.block, 2))
    # 2 blocks of TS packets in flight, as the 2 blocks of symbols between encoder and shaper did
    radio = RadioProcess(params, rx.ring, len(chans), a.block, 2, log)
    radio.start()
    stop = threading.Event()

    # diagnostic: MINI_CMTS_TSDUMP=<file> writes the TS blocks of every channel (channels x block x
    # 188 bytes per block) for MINI_CMTS_TSDUMP_S seconds once <file>.go exists
    dump = os.environ.get("MINI_CMTS_TSDUMP")
    dump_f, dump_end = None, None

    def encoder():
        nonlocal dump_f, dump_end
        while not stop.is_set():
            pks = [mux.packets(a.block)] + [m2.packets(a.block) for m2 in mux.secondary]
            if dump and dump_f is None and dump_end is None and os.path.exists(dump + ".go"):
                dump_f = open(dump, "wb")
                dump_end = time.monotonic() + float(os.environ.get("MINI_CMTS_TSDUMP_S", "10"))
            if dump_f is not None:
                dump_f.write(np.stack(pks).tobytes())
                if time.monotonic() > dump_end:
                    dump_f.close()
                    dump_f = None
                    log(f"[TS] flux enregistré dans {dump}")
            if not radio.pk.put(pks, stop):
                return
    threading.Thread(target=encoder, name="encoder", daemon=True).start()
    radio.started.wait()
    if radio.failed:
        stop.set()
        radio.stop()
        log(f"!! {radio.failed}")
        return 1
    wall0[0] = radio.wall0
    tb.origin += round(t_start * mcr)             # TX sample 0 goes out at tick t_start * mcr
    br = start_bridge(a, mux, core, stop)
    rxs = getattr(rx, "all_channels", None) or [rx]
    for r in rxs:
        r.start()                                 # the time base is final from here on
    log("réception montante dans un processus séparé (cmts/usproc.py)"
        + (f", un par canal ({len(rxs)})" if len(rxs) > 1 else "") + f" ; radio : pid {radio.pid}")
    if a.cpus == "auto":
        log(split_cpus([r.pid for r in rxs], shared_pids=[radio.pid], tx_need=tx_cores(a), mode=a.us_cpus,
                       prio=a.rt_prio))
    fill_prev = {}
    try:
        while True:
            time.sleep(5)
            if not radio.alive():
                log("!! processus radio arrêté")
                break
            st = mux.stats
            split = ("/".join(str(n) for n in mux.per_chan) + " par canal, ") if len(getattr(mux, "per_chan", [])) > 1 else ""
            pont = (f"pont ↑{br.stats['up']} ↓{br.stats['down']} ({split}{ds_fill(mux, fill_prev)}file DS "
                    f"{len(mux.data)}, attente max {st['data_max_ms']:.0f} ms, jetées {st['data_drop']}) | ") if br else ""
            st["data_max_ms"] = 0.0
            log("\n" + status_line(mux, core, rx, radio.status() + pont))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        radio.stop()
        for r in rxs:
            r.stop()
        if not rx._own_ring:
            rx.ring.close()
        log("\narrêt; " + status_line(mux, core, rx))
        if pcap:
            pcap.close()
            log(f"pcap: {pcap.count} trames dans {a.pcap} (wireshark, filtre docsis)"
                + (f", {pcap.dropped} perdues (file pleine)" if pcap.dropped else ""))
    return 0


def run_usrp(a):
    if a.tap and not ensure_tap(a.tap, log):
        log(f"!! interface {a.tap} indisponible : relance avec --tap none pour tourner sans pont")
        return 1
    if a.all_in_one:
        start_provision(a)
    log(pin_cpus(a.cpus))
    if a.cpus == "auto" and a.irq_herd:
        # onto the USB core's other hyperthread only: on the USB CPU itself, the network card's
        # interrupts (fast.com traffic) would compete with the B210's
        log(herd_irqs(usb_irq_cpus() - usb_irq_cpus(siblings=False)))
    log(cdsp.status())
    log(raise_priority(a.rt_prio))
    bat = on_battery()
    if bat:
        log(bat)
    gov = cpu_governor(a.performance)
    if gov:
        log(gov)
    lat = cpu_latency(a.performance)
    if lat:
        log(lat)
    # UHD's own one-letter U/L/O prints are replaced by the dated lines of StallWatch
    os.environ.setdefault("UHD_LOG_FASTPATH_DISABLE", "1")
    import uhd
    mux, core, rx, tb, mcr, decim = build(a, log)
    wall0 = [time.time()]                       # Unix time of DOCSIS timestamp 0, set at start
    pcap = attach_pcap(a.pcap, mux, rx, lambda ts: wall0[0] + ts / TS_RATE) if a.pcap else None
    fs_tx = a.sps * SYMRATE
    chans = ds_channels(a)
    tx_center = sum(f for _, f in chans) / len(chans)
    if len(chans) > 1:                         # same USB reserve in ms at the higher sample rate
        a.tx_frames = int(a.tx_frames * fs_tx / (2 * SYMRATE))
    # num_send_frames: samples already handed to USB are the only reserve against a delay of the
    # main (send) thread; the --tx-queue blocks before it only cover the encoder and shaper
    args = f"{a.args},master_clock_rate={mcr},num_recv_frames=256,num_send_frames={a.tx_frames}"
    if a.send_frame_size and "send_frame_size" not in a.args:
        # 16 KB USB frames instead of UHD's 8 KB: half the submissions; the send thread at 55.6 MS/s
        # went from 23 to 16 % of a core (kernel time 15.5 -> 10 %), and 6 DS lost its 1-6 ms late
        # sends and channel drops (2026-10-11, T53). --tx-frames halved to keep the same reserve.
        args += f",send_frame_size={a.send_frame_size}"
    if a.recv_frame_size and "recv_frame_size" not in a.args:
        # same on the receive side: 6 DS + 2 US, RX at 27.8 MS/s, the radio's RX thread went
        # from 24 to 14 % of a core (2026-10-11)
        args += f",recv_frame_size={a.recv_frame_size}"
    if a.radio_process:
        return run_split(a, mux, core, rx, tb, mcr, decim, pcap, wall0, args, fs_tx, chans, tx_center)
    usrp = uhd.usrp.MultiUSRP(args)
    usrp.set_tx_rate(fs_tx)
    usrp.set_tx_freq(uhd.types.TuneRequest(tx_center))
    # analog TX filter: wide enough for every channel (the outer edges are +-(span/2 + 4 MHz))
    span = (max(f for _, f in chans) - min(f for _, f in chans)) + 8e6
    usrp.set_tx_bandwidth(min(56e6, max(20e6, 1.5 * span)))
    usrp.set_tx_gain(a.gain)
    usrp.set_tx_antenna("TX/RX")
    usrp.set_rx_rate(mcr / decim)
    if a.us_channels > 1:
        # wideband: the LO between the channels (in the gap of --us-spacing - channel width)
        rx_target, lo_off = a.us_center, 0.0
        usrp.set_rx_freq(uhd.types.TuneRequest(rx_target))
        usrp.set_rx_bandwidth(min(56e6, abs(a.us_spacing) * (a.us_channels - 1) + a.us_rate * US_SYM_UNIT * 1.25 + 4e6))
    else:
        rx_target, lo_off = a.us_freq, rx_lo_offset(a)
        usrp.set_rx_freq(uhd.types.TuneRequest(a.us_freq, lo_off))
        usrp.set_rx_bandwidth(min(56e6, 2 * (abs(lo_off) + 2e6)))
    usrp.set_rx_gain(a.us_gain)
    usrp.set_rx_antenna("RX2")
    rx_err = usrp.get_rx_freq() - rx_target
    log(f"master clock {usrp.get_master_clock_rate() / 1e6:.6f} MHz | TX {usrp.get_tx_rate() / 1e6:.6f} MS/s "
          f"@ {usrp.get_tx_freq() / 1e6:.3f} MHz gain {usrp.get_tx_gain()} dB BW {usrp.get_tx_bandwidth() / 1e6:.0f} MHz | RX {usrp.get_rx_rate() / 1e6:.6f} MS/s "
          f"@ {usrp.get_rx_freq() / 1e6:.4f} MHz (LO {rx_target / 1e6 + lo_off / 1e6:.1f} MHz) gain {usrp.get_rx_gain()} dB")
    if abs(rx_err) > 1e3:
        log(f"!! fréquence RX obtenue {usrp.get_rx_freq() / 1e6:.4f} MHz au lieu de {rx_target / 1e6:.4f}: "
              f"essaie --us-lo-offset ou une autre --us-freq")
    if abs(usrp.get_tx_rate() - fs_tx) / fs_tx > 1e-6 or abs(usrp.get_rx_rate() * decim - mcr) > 1:
        log("!! débits inexacts: la base de temps commune TX/RX serait fausse")
        return 1
    tx_args = uhd.usrp.StreamArgs("fc32", "sc16")
    tx_args.args = "underflow_policy=next_packet"     # a late block is dropped, the stream goes on
    rx_bw = usrp.get_rx_bandwidth()
    usrp.set_rx_bandwidth(TX_CAL_RX_BW)       # UHD calibrates the TX quadrature as the streams open
    tx_stream = usrp.get_tx_stream(tx_args)
    rx_stream = usrp.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))
    usrp.set_rx_bandwidth(rx_bw)
    tx_dc = 0j
    if a.tx_dc_cal:
        rx_freq_now, rx_lo_now = usrp.get_rx_freq(), usrp.get_rx_freq() + lo_off
        tx_dc, msg = tx_dc_calibrate(uhd, usrp, tx_stream, rx_stream, tx_center,
                                     lambda: usrp.set_rx_freq(uhd.types.TuneRequest(rx_freq_now, rx_lo_now - rx_freq_now)), log)
        log(msg)

    stop = threading.Event()
    qsym = queue.Queue(maxsize=2)
    q = queue.Queue(maxsize=a.tx_queue)
    counters = dict(U=0, L=0, O=0)

    # channel k: shaped like the single-channel case, then shifted to its offset from the TX centre,
    # in one C pass per channel (cdsp.ChannelSynth); from 3 channels on, the channels are split
    # into groups computed by parallel threads (the C calls release the GIL)
    steps = [2 * np.pi * (f - tx_center) / fs_tx for _, f in chans]
    taps = PulseShaper(a.sps).phases
    synths = [cdsp.ChannelSynth(taps, steps[k], mux.invert) for k in range(len(chans))]
    scale = a.scale / np.sqrt(len(chans))
    ngroups = 1 if len(chans) <= 2 else min(len(chans), a.synth_threads)
    groups = [list(range(len(chans)))[g::ngroups] for g in range(ngroups)]
    helpers = []
    for g in groups[1:]:
        h = dict(ch=g, todo=queue.Queue(maxsize=1), done=queue.Queue(maxsize=1))
        helpers.append(h)

        def help_loop(h=h):
            while not stop.is_set():
                syms, acc = h["todo"].get()
                for k in h["ch"]:
                    synths[k].add(syms[k], acc)
                h["done"].put(acc)
        threading.Thread(target=help_loop, name="synth", daemon=True).start()

    def encoder():
        while not stop.is_set():
            syms = [mux.enc.encode(mux.packets(a.block))]
            syms += [m2.enc.encode(m2.packets(a.block)) for m2 in mux.secondary]
            qsym.put(syms)

    def shaper():
        while not stop.is_set():
            syms = qsym.get()
            if len(syms) == 1:
                x = mux.shape(syms[0]) * a.scale
                np.clip(x.real, -1, 1, out=x.real)
                np.clip(x.imag, -1, 1, out=x.imag)
            else:
                n = syms[0].size * a.sps
                for h in helpers:
                    h["todo"].put((syms, np.zeros(n, np.complex64)))
                x = np.zeros(n, np.complex64)
                for k in groups[0]:
                    synths[k].add(syms[k], x)
                for h in helpers:
                    cdsp.add_into(x, h["done"].get())
                cdsp.scale_clip(x, scale)
            if tx_dc:
                x += np.complex64(tx_dc)              # cancels the TX LO leakage (tx_dc_calibrate)
            q.put(x)

    last_push = [time.monotonic()]

    def rx_loop():
        buf = np.zeros((1, rx_stream.get_max_num_samps() * 8), dtype=np.complex64)
        md = uhd.types.RXMetadata()
        while not stop.is_set():
            n = rx_stream.recv(buf, md, 0.5)
            if md.error_code == uhd.types.RXMetadataErrorCode.overflow:
                counters["O"] += 1
                log(f"[RX] !! overflow à t={mux.now_ts / TS_RATE:.2f} s (échantillons montants perdus)")
            elif md.error_code not in (uhd.types.RXMetadataErrorCode.none,):
                continue
            if n:
                tick = md.time_spec.get_full_secs() * mcr + round(md.time_spec.get_frac_secs() * mcr)
                rx.push(tick, buf[0, :n].copy())
                now_p = time.monotonic()
                counters["rx_gap_ms"] = max(counters.get("rx_gap_ms", 0.0), (now_p - last_push[0]) * 1e3)
                last_push[0] = now_p

    def worker_timed():
        while not stop.is_set():
            n = rx.process()
            t0 = time.perf_counter()
            rx.scan_raw()
            c = rx.timing.setdefault("scan_raw", [0, 0.0])
            c[0] += 1
            c[1] += time.perf_counter() - t0
            if not n:
                time.sleep(0.002)

    def worker():
        # diagnostic: MINI_CMTS_PROFILE=<file> profiles this thread (pstats, rewritten every 20 s)
        prof_path = os.environ.get("MINI_CMTS_PROFILE")
        prof = cProfile.Profile() if prof_path else None
        t_dump = time.monotonic() + 20
        if prof:
            prof.enable()
        while not stop.is_set():
            n = rx.process()
            rx.scan_raw()
            if not n:
                time.sleep(0.002)
            if prof and time.monotonic() > t_dump:
                prof.dump_stats(prof_path)
                prof.enable()                          # dump_stats() disables it
                t_dump = time.monotonic() + 20

    def async_loop():
        amd = uhd.types.TXAsyncMetadata()
        while not stop.is_set():
            if tx_stream.recv_async_msg(amd, 0.2):
                ec = amd.event_code
                if ec in (uhd.types.TXMetadataEventCode.underflow, uhd.types.TXMetadataEventCode.underflow_in_packet):
                    counters["U"] += 1
                elif ec == uhd.types.TXMetadataEventCode.time_error:
                    counters["L"] += 1

    spp = tx_stream.get_max_num_samps()
    def l_ms(n):                                 # UHD reports one L per late USB packet
        return n * spp / fs_tx * 1e3
    wd = StallWatch(log, counters, lambda: mux.now_ts / TS_RATE, l_ms=l_ms)
    for f in (encoder, shaper, async_loop):
        threading.Thread(target=f, daemon=True).start()
    while q.qsize() < a.tx_queue:
        time.sleep(0.005)
    # common start time for TX and RX
    usrp.set_time_now(uhd.types.TimeSpec(0.0))
    t_start = 0.5
    wall0[0] = time.time() + t_start
    tb.origin += round(t_start * mcr)             # TX sample 0 goes out at tick t_start * mcr
    cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
    cmd.stream_now = False
    cmd.time_spec = uhd.types.TimeSpec(t_start)
    rx_stream.issue_stream_cmd(cmd)
    br = start_bridge(a, mux, core, stop)
    remote = isinstance(rx, RemoteUpstream)
    if remote:
        rxs = getattr(rx, "all_channels", None) or [rx]
        for r in rxs:
            r.start()                             # the time base is final from here on
        log("réception montante dans un processus séparé (cmts/usproc.py)"
            + (f", un par canal ({len(rxs)})" if len(rxs) > 1 else ""))
        if a.cpus == "auto":
            log(split_cpus([r.pid for r in rxs], tx_need=tx_cores(a), mode=a.us_cpus, prio=a.rt_prio))
    workers = [rx_loop] + ([] if remote else [worker_timed if os.environ.get("MINI_CMTS_TIMING") else worker])
    for f in workers:
        threading.Thread(target=f, daemon=True).start()
    md = uhd.types.TXMetadata()
    md.start_of_burst = True
    md.end_of_burst = False
    md.has_time_spec = True
    md.time_spec = uhd.types.TimeSpec(t_start)
    # Every block carries its own air time (tick of its first sample): after an underflow the
    # B210 resumes on schedule (a gap) instead of late (a shift of the whole downstream, which
    # would break the TX/RX time base), and a block that arrives too late is dropped ("L").
    tick0 = round(t_start * mcr)
    ticks_per_sample = round(mcr / fs_tx)
    n_sent = 0
    blk_ms = a.block * 204 * 8 / (SYMRATE * mux.m) * 1e3
    usb_ms = a.tx_frames * spp / fs_tx * 1e3
    log(f"émission; latence downstream ~{(a.tx_queue + 3) * blk_ms + usb_ms:.0f} ms (T3 du modem: 200 ms), "
        f"réserve USB {usb_ms:.0f} ms. Ctrl-C pour arrêter")
    t_last = time.time()
    # the first blocks fill the USB reserve at once, then send() waits for the stream start time:
    # the watchdog is armed only once the stream runs
    wd.last = time.perf_counter()
    wd.armed_at = wd.last + t_start + 0.5
    sync = (-1e9, 0.0)                          # (perf_counter, B210 time) pair, refreshed every second
    t_skip_log = [0.0]
    catching_up = [False]
    m_min = float("inf")
    threading.Thread(target=wd.run, args=(stop,), name="watchdog", daemon=True).start()
    try:
        while True:
            x = q.get()
            # A block already due (or due within 1 ms) is dropped here, not sent: sent, it would
            # reach the B210 late, be discarded there AND come back as one "late" message per USB
            # packet. On 2026-10-10 (3 DS + 2 US, CPU at its limit) 3 underflows after a speedtest
            # put the stream 0.3 s behind; ~14,000 such messages/s, handled in Python, took the
            # GIL from the encoder and shaper, which fell further behind: -13 s, never caught up.
            # Dropping locally costs nothing; the stream skips to its schedule (a gap on the air,
            # like an underflow, the DOCSIS time base unchanged).
            # Once late, blocks are dropped until a reserve of 8 ms is back (hysteresis): with a
            # plain threshold the stream settled 5 ms late for good after a hiccup, every block
            # late by 0-5 ms sent, discarded by the B210 and answered by "late" messages whose
            # handling kept the producer from catching up (2026-10-10, TX dead after a USB
            # adapter was plugged in).
            if sync[0] > 0 and not a.tx_untimed:
                ahead = t_start + n_sent / fs_tx - (sync[1] + time.perf_counter() - sync[0])
                if ahead < -1e-3:
                    catching_up[0] = True
                elif ahead > 8e-3:
                    catching_up[0] = False
                if catching_up[0]:
                    n_sent += x.size
                    counters["skip"] = counters.get("skip", 0) + 1
                    counters["skip_ms"] = counters.get("skip_ms", 0.0) + x.size / fs_tx * 1e3
                    if time.time() - t_skip_log[0] > 1.0:
                        t_skip_log[0] = time.time()
                        log(f"[TX] !! émission en retard de {-ahead * 1e3:.0f} ms : blocs jetés avant envoi pour "
                            f"rattraper l'horaire ({counters['skip_ms']:.0f} ms jetés au total)")
                    continue
            if n_sent == 0 or not a.tx_untimed:
                md.has_time_spec = True
                md.time_spec = uhd.types.TimeSpec.from_ticks(tick0 + n_sent * ticks_per_sample, mcr)
            n_sent += x.size
            sent = 0
            while sent < x.size:
                sent += tx_stream.send(x[sent:], md, 1.0)
                md.start_of_burst = False
                md.has_time_spec = False
            wd.feed()
            # margin: how long before its air time the end of this block was handed to UHD
            now_p = time.perf_counter()
            if now_p - sync[0] > 1.0 and n_sent * ticks_per_sample > t_start * mcr:
                p0 = time.perf_counter()
                dev = usrp.get_time_now().get_real_secs()
                now_p = time.perf_counter()
                if now_p - p0 < 2e-3:                 # a slow reading (busy USB, GIL) is off by up to
                    sync = ((p0 + now_p) / 2, dev)    # half its round trip: kept only when quick
            if sync[0] > 0:
                m_min = min(m_min, t_start + n_sent / fs_tx - (sync[1] + now_p - sync[0]))
            if time.time() - t_last > 5:
                t_last = time.time()
                marg = f"marge TX min {m_min * 1e3:.1f} ms | " if m_min < float("inf") else ""
                m_min = float("inf")
                st = mux.stats
                split = ("/".join(str(n) for n in mux.per_chan) + " par canal, ") if len(getattr(mux, "per_chan", [])) > 1 else ""
                pont = (f"pont ↑{br.stats['up']} ↓{br.stats['down']} ({split}file DS {len(mux.data)}, attente max "
                        f"{st['data_max_ms']:.0f} ms, jetées {st['data_drop']}) | ") if br else ""
                st["data_max_ms"] = 0.0
                gap, counters["rx_gap_ms"] = counters.get("rx_gap_ms", 0.0), 0.0
                log("\n" + status_line(mux, core, rx, f"U {counters['U']} L {counters['L']} (~{l_ms(counters['L']):.0f} ms) "
                                                       f"RX écart max {gap:.0f} ms "
                                                       f"O {counters['O']} | {marg}{pont}"))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        rx_stream.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
        md.end_of_burst = True
        tx_stream.send(np.zeros(1000, dtype=np.complex64), md)
        if isinstance(rx, RemoteUpstream):
            for r in getattr(rx, "all_channels", None) or [rx]:
                r.stop()
            if not rx._own_ring:                  # the wideband ring shared by the channels
                rx.ring.close()
        log("\narrêt; " + status_line(mux, core, rx))
        if pcap:
            pcap.close()
            log(f"pcap: {pcap.count} trames dans {a.pcap} (wireshark, filtre docsis)"
                + (f", {pcap.dropped} perdues (file pleine)" if pcap.dropped else ""))
        if counters["U"] or counters["L"]:
            log(f"!! TX: {counters['U']} underflows, {counters['L']} paquets en retard jetés (~{l_ms(counters['L']):.0f} ms "
                f"de downstream). Le downstream "
                  "reste calé sur la base de temps, mais chaque trou peut faire décrocher le modem un instant")


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("downstream (comme ds_tx.py)")
    g.add_argument("--freq", type=float, default=306e6, help="downstream center frequency (Hz); 306 MHz = Sagem bench")
    g.add_argument("--gain", type=float, default=None,
                   help="B210 TX gain dB; default 45 (~12 dBmV at the Sagem through one 1:2 splitter; 49 gave "
                        "16.4) plus 10*log10(channels) when bonding, so each channel keeps its level")
    g.add_argument("--qam", type=int, default=256, choices=(64, 256),
                   help="downstream QAM (256 since 2026-10-09: modem SNR 48.6 dB, 40.8 Mbit/s carried)")
    g.add_argument("--sps", type=int, default=2)
    g.add_argument("--mcr-mult", type=int, default=None,
                   help="master clock = sps*6.952M*this; default 2 (27.8 MHz, as validated with ds_tx.py), "
                        "4 (55.6 MHz) only when the RX LO offset is large (upstream below 53 MHz)")
    g.add_argument("--scale", type=float, default=0.3)
    g.add_argument("--mac", default="02:00:00:ca:fe:01")
    g.add_argument("--no-invert", action="store_true")
    g.add_argument("--block", type=int, default=64, help="TS packets per block (multiple of 8)")
    # 3 and 48 since 2026-10-11 (6 and 64 before): downstream latency ~36 -> ~25 ms, ping 100 -> 78 ms,
    # smallest TX margin 6.7 ms instead of 14-17 (6 DS + 2 US, no underflow nor late packet; T58)
    g.add_argument("--tx-queue", type=int, default=3,
                   help="blocks buffered before the send thread (~1.9 ms each; covers encoder delays, adds latency)")
    g.add_argument("--tx-frames", type=int, default=48,
                   help="UHD send frames in flight on USB (~0.3 ms each at 27.8 MS/s with 16 KB frames, scaled "
                        "with the sample rate: 14 ms of reserve): reserve against delays of the sending thread, "
                        "adds the same latency")
    g.add_argument("--send-frame-size", type=int, default=16360,
                   help="UHD USB send frame size (bytes; UHD's default 8176): larger frames, fewer USB "
                        "submissions, less kernel time in the send thread; 0 = UHD's default")
    g.add_argument("--recv-frame-size", type=int, default=16360,
                   help="UHD receive frame size in bytes (UHD: 8176): half the USB transfers of the RX "
                        "stream; 0 = UHD's")
    g.add_argument("--args", default="type=b200", help="UHD device args")
    g.add_argument("--rt-prio", type=int, default=50,
                   help="real-time priority (SCHED_RR 1-99) of the whole process, 0 = normal")
    g.add_argument("--cpus", default="auto",
                   help="CPUs for the whole process: auto = all but the core taking the USB interrupts, "
                        "all, or a list like 1-3,5-7")
    g.add_argument("--performance", action=argparse.BooleanOptionalAction, default=True,
                   help="put every CPU in the 'performance' governor and block the deep C-states "
                        "(/dev/cpu_dma_latency) for the run (sudo), restored on exit")
    g.add_argument("--tx-dc-cal", action=argparse.BooleanOptionalAction, default=False,
                   help="measure and cancel the TX LO leakage at start (the B210 RX tuned to the TX centre "
                        "for a moment). It lowered the leakage in the middle of 3 channels (-31 dBc) by 2-24 dB "
                        "but did not make the modem acquire channel 2 more often (2026-10-10): off by default")
    g.add_argument("--tx-untimed", action="store_true",
                   help="only the first block is timed (old behaviour: an underflow shifts the downstream)")
    g = ap.add_argument_group("upstream")
    g.add_argument("--us-freq", type=float, default=60e6,
                   help="upstream center frequency (UCD); 53-64 MHz keeps the B210 LO >= 50 MHz with a "
                        "small offset (SFR uses 60.4 MHz)")
    g.add_argument("--us-rate", type=int, default=32,
                   help="symbol rate in 160 ksym/s units (16 = 2.56 Msym/s, 3.2 MHz; 8 = 1.28 Msym/s)")
    g.add_argument("--us-channels", type=int, default=1,
                   help="upstream channels (DOCSIS 3.0 upstream bonding, experimental): >1 receives one "
                        "wideband stream and runs one upstream process per channel")
    g.add_argument("--us-spacing", type=float, default=-7e6,
                   help="upstream channel spacing (Hz); -7 MHz: the next channel 7 MHz below (60 -> 53 MHz: the "
                        "EuroDOCSIS upstream band ends at 65 MHz), 0.6 MHz between two 5.12 Msym/s channels "
                        "for the RX LO")
    g.add_argument("--us-mod", choices=("qpsk", "16qam", "64qam"), default="64qam",
                   help="modulation of the upstream data bursts (16qam: twice the bits per symbol; 64qam: "
                        "ATDMA only)")
    g.add_argument("--us-mode", choices=("tdma", "atdma"), default="atdma",
                   help="upstream channel type: tdma (DOCSIS 1.x, UCD type 2, up to 2.56 Msym/s 16-QAM) or atdma "
                        "(DOCSIS 2.0, UCD type 29, up to 5.12 Msym/s 64-QAM)")
    g.add_argument("--minislot", type=int, default=4, help="mini-slot size in 6.25 us ticks")
    g.add_argument("--us-gain", type=float, default=20, help="B210 RX gain dB (20: noise -70 dBFS, ranged bursts -20 dBFS)")
    g.add_argument("--us-lo-offset", type=float, default=None,
                   help="RX LO - upstream freq (Hz); default 3 MHz, more if needed to keep the LO >= 52 MHz")
    g.add_argument("--irq-herd", action=argparse.BooleanOptionalAction, default=True,
                   help="with --cpus auto: move every other hardware interrupt onto the USB core (sudo)")
    g.add_argument("--us-process", action=argparse.BooleanOptionalAction, default=True,
                   help="upstream demodulation in a separate process (own GIL and core); "
                        "--no-us-process: a thread, as before 2026-10-09")
    g.add_argument("--us-cpus", choices=("auto", "own", "shared"), default="auto",
                   help="with --cpus auto: 'own' gives each upstream process a core of its own; 'shared' runs "
                        "them on every core at a lower real-time priority than the transmit side; 'auto': "
                        "own cores unless that leaves the transmit side of 5-6 DS fewer than 4 (6 DS + 2 US "
                        "on 6 cores)")
    g.add_argument("--mtc-std", action=argparse.BooleanOptionalAction, default=True,
                   help="multi-channel upstream registration per MULPIv3.0, as a real CMTS (ARRIS) does it: "
                        "Dynamic Range Window in the TCC (action 2, mandatory in MTC mode) instead of the RNG-RSPs, "
                        "47.3 criteria at 0, TLV 52 = 60, TCC references from 0, initialization technique 3 "
                        "(--no-mtc-std: the former registration, no DRW TCC)")
    g.add_argument("--mtc-owed", action=argparse.BooleanOptionalAction, default=True,
                   help="multi-channel upstream: queue-depth requests add up (MULPIv3.0: a request only carries "
                        "bytes not requested before), granted on every channel each MAP, Data Grant Pending "
                        "while bytes are still owed (--no-mtc-owed: each request replaces the previous one)")
    g.add_argument("--us-lag-limit", type=float, default=150.0,
                   help="ms: an upstream channel whose receiver is this far behind its RX stream gets no data "
                        "grant until it catches up (half of it: no grant in advance); 0 = off")
    g.add_argument("--us-target", type=float, default=-14.0,
                   help="burst power the modems are ranged to (dBFS); -14 since 2026-10-10 (64-QAM, raw MER 21 -> 28 dB "
                        "with a -49 dBFS noise floor), -20 before")
    g.add_argument("--gate-db", type=float, default=8.0, help="energy above the noise floor to search a burst")
    g.add_argument("--im-slack-us", type=float, default=1000.0,
                   help="Initial Maintenance: accept bursts this early or late (µs, unranged modems)")
    g.add_argument("--sm-period", type=float, default=1.0, help="Station Maintenance period once ranged (s)")
    g.add_argument("--no-rsp", action="store_true", help="listen only: decode bursts, never answer")
    g.add_argument("--pcap", metavar="FILE", nargs="?", const="auto", default=None,
                   help="DOCSIS pcap of every decoded upstream frame and RNG-RSP sent; --pcap alone = "
                        "captures/cmts_<date>_<time>.pcap")
    g.add_argument("--log", metavar="FILE", default="auto",
                   help="copy of the console without colours; auto = captures/cmts_<date>_<time>.log on "
                        "hardware runs, none = no file")
    g = ap.add_argument_group("réseau et enregistrement des modems")
    g.add_argument("--tap", default="cmts0",
                   help="Linux TAP interface bridged to the modems (created with sudo if missing); none = no bridge")
    g.add_argument("--all-in-one", action="store_true",
                   help="also run provision.py (DHCP, TFTP + generated config file, ToD) on the TAP, through sudo")
    g.add_argument("--prov-args", default="", help="extra arguments for provision.py, e.g. \"--config my.cfg\"")
    g.add_argument("--shared-secret", default="docsis-lab", help="CMTS MIC shared secret (same as in the config file)")
    g.add_argument("--strict-mic", action="store_true", help="refuse a REG-REQ whose CMTS MIC is wrong")
    g.add_argument("--ds-aqm", type=float, default=30.0, metavar="MS",
                   help="CoDel on every downstream data queue, target MS (e.g. 15), interval 100 ms: drops one "
                        "frame now and then instead of bursts at the --ds-queue-ms limit; 0 = off")
    g.add_argument("--ds-queue-ms", type=float, default=100.0,
                   help="downstream data frames waiting longer than this are dropped (bounded latency)")
    g.add_argument("--ds-channels", type=int, default=6,
                   help="bonded downstream channels, 8 MHz apart (DOCSIS 3.0), 1-6 (5-6: master clock "
                        "55.616 MHz; 6 with --ds-layout around: 234-258 / 21 Mbit/s on the Proxmox host, "
                        "2026-10-11)")
    g.add_argument("--synth-threads", type=int, default=2,
                   help="threads sharing the shaping of 3+ bonded downstream channels")
    g.add_argument("--radio-process", action=argparse.BooleanOptionalAction, default=True,
                   help="B210, J.83 encoding, shaping and timed send in a process of their own "
                        "(cmts/radio.py): their GIL no longer shared with the MAC layer and the bridge")
    g.add_argument("--ds-spacing", type=float, default=8e6, help="spacing of the bonded downstream channels (Hz)")
    g.add_argument("--ds-layout", choices=("up", "around"), default="around",
                   help="bonded downstream channels: 'up' from --freq every --ds-spacing; 'around': --freq (the "
                        "primary) in the middle, the others on both sides (6 channels: the only layout where the "
                        "Sagem used its upstream grants, 2026-10-11)")
    g.add_argument("--docsis30", action="store_true",
                   help="send a MAC Domain Descriptor (MDD): the modem initialises in DOCSIS 3.0 mode "
                        "(experimental, needed for channel bonding)")
    g.add_argument("--grant-ahead-hold", action=argparse.BooleanOptionalAction, default=True,
                   help="keep the grants in advance through 0 piggyback requests and empty grants "
                        "(upload x?, but the upstream receiver must keep up: watch 'retard max')")
    g.add_argument("--im-every-idle", type=int, default=25,
                   help="Initial Maintenance every N MAPs while all known modems are online (5 otherwise)")
    g.add_argument("--grant-ahead-every", type=int, default=3,
                   help="a grant in advance every N MAPs only (fewer, larger bursts with --grant-ahead-scale)")
    g.add_argument("--grant-ahead-scale", type=float, default=4.0,
                   help="size of a grant in advance, in multiples of the last request (max 255 mini-slots)")
    g.add_argument("--grant-ahead-ms", type=float, default=100.0,
                   help="after each request, keep granting that SID the same size in every MAP for this "
                        "long (ms): frames no longer wait one request/MAP round trip each; 0 = off")
    g = ap.add_argument_group("upstream (suite)")
    g.add_argument("--save-bursts", metavar="DIR", help="save every detected burst (IQ .npz)")
    g = ap.add_argument_group("simulation (sans matériel)")
    g.add_argument("--sim", type=float, metavar="SECONDS", help="run with a simulated modem")
    g.add_argument("--sim-delay-us", type=float, default=11.7)
    g.add_argument("--sim-power", type=float, default=-38.0)
    g.add_argument("--sim-freq", type=float, default=2300.0)
    g.add_argument("--sim-snr", type=float, default=28.0)
    g.add_argument("--sim-latency-ms", type=float, default=30.0)
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="console detail: -v ranging and requests, -vv every bridged frame, -vvv raw energy "
                         "scan and network noise (the log file always gets everything)")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                    help="console colours; auto = only on a terminal (always: keeps them through tee, read with less -R)")
    return ap


def main():
    a = parser().parse_args()
    termcolor.setup(a.color)
    termcolor.set_verbosity(a.verbose)
    if a.us_mod == "64qam" and a.us_mode != "atdma":
        sys.exit("--us-mod 64qam demande --us-mode atdma (DOCSIS 1.x TDMA : QPSK ou 16-QAM)")
    # data bursts (TDMA IUC 5/6, ATDMA IUC 9/10) in the chosen modulation; REQ / IM / SM stay QPSK
    mac.configure_upstream(a.us_mode, {"qpsk": 2, "16qam": 4, "64qam": 6}[a.us_mod])
    # UHD's own [INFO] start-up lines only from -v on (its warnings and errors always)
    os.environ.setdefault("UHD_LOG_CONSOLE_LEVEL", "info" if a.verbose else "warning")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if a.tap == "none":
        a.tap = None
    if a.all_in_one and not a.tap:
        sys.exit("--all-in-one a besoin du pont (--tap)")
    for k in ("pcap", "log"):                   # automatic names; the log only on hardware runs
        v = getattr(a, k)
        if v == "none" or (v == "auto" and a.sim and k == "log"):
            setattr(a, k, None)
        elif v == "auto":
            os.makedirs("captures", exist_ok=True)
            setattr(a, k, os.path.join("captures", f"cmts_{stamp}.{k}"))
    if a.log:
        termcolor.open_log(a.log)
        log(f"journal: {a.log}" + (f" | pcap: {a.pcap}" if a.pcap else ""))
    if a.sim:
        res = run_sim(a, log)
        return 0 if res["state"] == "ranged" else 1
    return run_usrp(a)


if __name__ == "__main__":
    sys.exit(main())
