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
from cmts.bridge import TapBridge, ensure_tap
from cmts.downstream import DownstreamMux, SecondaryMux, SYMRATE
from cmts.j83a import PulseShaper
from cmts.pcap import PcapWriter
from cmts.usproc import RemoteUpstream
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


def ds_channels(a):
    """[(DCID, frequency)] of the downstream channels; the first one is primary."""
    return [(k + 1, a.freq + k * a.ds_spacing) for k in range(a.ds_channels)]


def build(a, log=print):
    if a.gain is None:                         # same level per channel whatever their number
        # 45 for one channel (~12 dBmV at the Sagem); bonded: +10 log10(N) to keep each channel's
        # level, +2 dB (the 2-channel bench ran at 50 for its best results, 2026-10-10)
        a.gain = 45 + round(10 * np.log10(max(1, a.ds_channels))) + (2 if a.ds_channels > 1 else 0)
    if a.ds_channels > 1:
        if a.ds_channels > 6:
            sys.exit("--ds-channels : 1 à 6 (au-delà, la bande du B210 et le processeur ne suivent plus)")
        # N 8 MHz channels side by side, each shaped at `sps` samples per symbol = master clock and
        # shifted around the TX centre: 2 -> 27.808 MS/s (+-4 MHz), 3-4 -> 41.712 MS/s (up to
        # +-12 MHz), 5-6 -> 55.616 MS/s (up to +-20 MHz); USB checked at all three (2026-10-10:
        # TX + 13.9 MS/s RX, no loss)
        # 1-3 channels: 27.808 MS/s (3 channels at -8/0/+8 MHz fill +-12 of the +-13.9 MHz band).
        # 4: 41.712, 5-6: 55.616 MS/s -- NOT WORKING with the Sagem 3686 (2026-10-10): with any
        # master clock but 27.808 MHz the modem never locks, even on 1 channel (tone, frequency
        # accuracy, TX response, RX path, timed/untimed all checked); cause unknown
        a.sps = 4 if a.ds_channels <= 3 else (6 if a.ds_channels <= 4 else 8)
        if os.environ.get("MINI_CMTS_DS_SPS"):     # diagnostic: force the samples per symbol
            a.sps = int(os.environ["MINI_CMTS_DS_SPS"])
        a.mcr_mult, a.docsis30 = int(os.environ.get("MINI_CMTS_MCR_MULT", "1")), True   # (diagnostic override)
    if a.mcr_mult is None:   # 27.8 MHz is enough while the offset stays inside the AD9361 passband
        a.mcr_mult = 2 if abs(rx_lo_offset(a)) <= 8e6 else 4
    mux = DownstreamMux(qam=a.qam, sps=a.sps, us_freq=a.us_freq, us_symrate_mult=a.us_rate,
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
    mux.on_grants = rx.add_grants
    mux.ack_ms = lambda: int(rx.processed_ts // mux.ms_counts)   # MAP Ack Time: upstream really processed
    mux.data_max_ms = a.ds_queue_ms
    mux.secondary = []
    if a.docsis30:                             # MAC Domain Descriptor: the modem runs in DOCSIS 3.0 mode
        chans = ds_channels(a)
        plan = [(cid, f, a.qam, cid == 1) for cid, f in chans]
        mux.mdd = mac.mdd_msg(mux.sa, 1, 1, plan, [1])
        for cid, f in chans[1:]:
            m2 = SecondaryMux(qam=a.qam, cmts_mac=a.mac, invert=not a.no_invert)
            m2.mdd = mac.mdd_msg(mux.sa, 1, cid, plan, [1])
            m2.data_max_ms = a.ds_queue_ms
            mux.secondary.append(m2)
        if len(chans) > 1:
            core.bonding = chans
    return mux, core, rx, tb, mcr, decim


def attach_pcap(path, mux, rx, wall_of_ts):
    """Every decoded upstream frame (RNG-REQ, REQ...) and every unicast message the CMTS sends
    (RNG-RSP) into a DLT_DOCSIS pcap. Both are stamped on the DOCSIS time base (burst arrival,
    RNG-RSP air time), so the gaps between them in Wireshark are the real ones on the cable;
    wall_of_ts() maps that time base to the Unix clock (to a few ms)."""
    pcap = PcapWriter(path)
    on_burst = rx.on_burst

    def tap(ev):
        if ev.burst is not None and ev.burst.frame:
            pcap.write(wall_of_ts(ev.rx_ts), ev.burst.frame)
        on_burst(ev)
    rx.on_burst = tap
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
        br.upstream(eth)
        if flim.ok():
            log(f"[PONT] ↑ {netpkt.summary(eth)}", console=lim.ok())

    channels = [mux] + list(getattr(mux, "secondary", []))
    seq = [0]
    per_chan = [0] * len(channels)
    force_ch = int(os.environ["MINI_CMTS_BOND_CH"]) - 1 if os.environ.get("MINI_CMTS_BOND_CH") else None
    mux.per_chan = per_chan

    def down(eth):
        if len(channels) > 1 and core.bonded and not (eth[0] & 1):
            # unicast to the bonded modem: DSID label + sequence number, on the least loaded channel
            mac_ = next(iter(core.bonded))
            dsid = core.bonded[mac_]
            usable = [c - 1 for c in core.bonded_channels.get(mac_, range(1, len(channels) + 1))
                      if 0 < c <= len(channels)] or [0]
            k = min(usable, key=lambda i: len(channels[i].data))   # least loaded acquired channel
            if force_ch is not None:                 # diagnostic: MINI_CMTS_BOND_CH=2 -> channel 2 only
                k = force_ch
            channels[k].send_data(mac.data_pdu_dsid(eth, dsid, seq[0]))
            seq[0] = (seq[0] + 1) & 0xFFFF
            per_chan[k] += 1
        else:
            mux.send_data(mac.data_pdu(eth))
            per_chan[0] += 1
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


def status_line(mux, core, rx, extra=""):
    st = rx.stats
    fl = rx.noise_dbfs()
    mods = "; ".join(f"{m['mac']} SID {m['sid']:#06x} {m['state']}"
                     + (f" ({m['err_us']:+.2f} µs, {m['power']:.1f} dBFS, {m['freq']:+.0f} Hz, MER {m['mer']:.1f})"
                        if 'err_us' in m else "") for m in core.summary()) or "aucun modem"
    lag, rx.lag_max_ms = rx.lag_max_ms, 0.0
    if os.environ.get("MINI_CMTS_TIMING") and rx.timing:   # diagnostic: worker time per window type
        log("[TEMPS] " + ", ".join(f"{k}: {v[0]} × {v[1] * 1e3 / max(v[0], 1):.2f} ms = {v[1]:.2f} s"
                                   for k, v in sorted(rx.timing.items())))
        rx.timing.clear()
    return (f"[{time.strftime('%H:%M:%S')}] {extra}US: bruit {'?' if fl is None else f'{fl:.1f}'} dBFS, "
            f"retard max {lag:.0f} ms" + (f" (file {st['windows_queued']}, abandonnées {st['late']})"
                                          if 'windows_queued' in st else "")
            + f", rafales {st['bursts']} (décodées {st['decoded']}, énergie brute {st['raw']}), "
            f"RNG-RSP {core.n_rsp} | grants {core.n['req']} demandés + {core.n['ahead']} en avance "
            f"({core.n['empty']} vides), concat {core.n['concat']} | {mods}")


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


def split_cpus(child_pid):
    """Give the upstream process (cmts/usproc.py) a physical core of its own, the last one
    allowed, and keep every thread of this process (TX feed, encoder, UHD) on the others: they
    no longer compete for the same cores (2026-10-09: TX gaps of 0.3-2 s under load)."""
    allowed = sorted(os.sched_getaffinity(0))

    def core(c):
        try:
            with open(f"/sys/devices/system/cpu/cpu{c}/topology/core_id") as f:
                return int(f.read())
        except OSError:
            return c
    cores = sorted({core(c) for c in allowed})
    if len(cores) < 3:
        return f"processus montant : CPU partagés ({','.join(map(str, allowed))}, trop peu de cœurs pour en réserver un)"
    mine = {c for c in allowed if core(c) == cores[-1]}
    rest = set(allowed) - mine
    try:
        os.sched_setaffinity(child_pid, mine)
        for tid in os.listdir("/proc/self/task"):
            try:
                os.sched_setaffinity(int(tid), rest)
            except OSError:
                pass
    except OSError as e:
        return f"!! répartition des CPU impossible : {e}"
    return (f"processus montant sur les CPU {','.join(map(str, sorted(mine)))}, émission sur "
            f"{','.join(map(str, sorted(rest)))}")


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
    gov = cpu_governor(a.performance)
    if gov:
        log(gov)
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
    usrp = uhd.usrp.MultiUSRP(args)
    usrp.set_tx_rate(fs_tx)
    usrp.set_tx_freq(uhd.types.TuneRequest(tx_center))
    # analog TX filter: wide enough for every channel (the outer edges are +-(span/2 + 4 MHz))
    span = (max(f for _, f in chans) - min(f for _, f in chans)) + 8e6
    usrp.set_tx_bandwidth(min(56e6, max(20e6, 1.5 * span)))
    usrp.set_tx_gain(a.gain)
    usrp.set_tx_antenna("TX/RX")
    usrp.set_rx_rate(mcr / decim)
    lo_off = rx_lo_offset(a)
    usrp.set_rx_freq(uhd.types.TuneRequest(a.us_freq, lo_off))
    usrp.set_rx_bandwidth(min(56e6, 2 * (abs(lo_off) + 2e6)))
    usrp.set_rx_gain(a.us_gain)
    usrp.set_rx_antenna("RX2")
    rx_err = usrp.get_rx_freq() - a.us_freq
    log(f"master clock {usrp.get_master_clock_rate() / 1e6:.6f} MHz | TX {usrp.get_tx_rate() / 1e6:.6f} MS/s "
          f"@ {usrp.get_tx_freq() / 1e6:.3f} MHz gain {usrp.get_tx_gain()} dB BW {usrp.get_tx_bandwidth() / 1e6:.0f} MHz | RX {usrp.get_rx_rate() / 1e6:.6f} MS/s "
          f"@ {usrp.get_rx_freq() / 1e6:.4f} MHz (LO {a.us_freq / 1e6 + lo_off / 1e6:.1f} MHz) gain {usrp.get_rx_gain()} dB")
    if abs(rx_err) > 1e3:
        log(f"!! fréquence RX obtenue {usrp.get_rx_freq() / 1e6:.4f} MHz au lieu de {a.us_freq / 1e6:.4f}: "
              f"essaie --us-lo-offset ou une autre --us-freq")
    if abs(usrp.get_tx_rate() - fs_tx) / fs_tx > 1e-6 or abs(usrp.get_rx_rate() * decim - mcr) > 1:
        log("!! débits inexacts: la base de temps commune TX/RX serait fausse")
        return 1
    tx_args = uhd.usrp.StreamArgs("fc32", "sc16")
    tx_args.args = "underflow_policy=next_packet"     # a late block is dropped, the stream goes on
    tx_stream = usrp.get_tx_stream(tx_args)
    rx_stream = usrp.get_rx_stream(uhd.usrp.StreamArgs("fc32", "sc16"))

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
        rx.start()                                # the time base is final from here on
        log("réception montante dans un processus séparé (cmts/usproc.py)")
        if a.cpus == "auto":
            log(split_cpus(rx.pid))
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
    m_min = float("inf")
    threading.Thread(target=wd.run, args=(stop,), name="watchdog", daemon=True).start()
    try:
        while True:
            x = q.get()
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
                sync = ((p0 + now_p) / 2, dev)
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
            rx.stop()
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
    g.add_argument("--tx-queue", type=int, default=6,
                   help="blocks buffered before the send thread (2.5 ms each; covers encoder delays, adds latency)")
    g.add_argument("--tx-frames", type=int, default=128,
                   help="UHD send frames in flight on USB (~0.15 ms each): reserve against delays of the "
                        "sending thread, adds the same latency")
    g.add_argument("--args", default="type=b200", help="UHD device args")
    g.add_argument("--rt-prio", type=int, default=50,
                   help="real-time priority (SCHED_RR 1-99) of the whole process, 0 = normal")
    g.add_argument("--cpus", default="auto",
                   help="CPUs for the whole process: auto = all but the core taking the USB interrupts, "
                        "all, or a list like 1-3,5-7")
    g.add_argument("--performance", action=argparse.BooleanOptionalAction, default=True,
                   help="put every CPU in the 'performance' governor for the run (sudo), restored on exit")
    g.add_argument("--tx-untimed", action="store_true",
                   help="only the first block is timed (old behaviour: an underflow shifts the downstream)")
    g = ap.add_argument_group("upstream")
    g.add_argument("--us-freq", type=float, default=60e6,
                   help="upstream center frequency (UCD); 53-64 MHz keeps the B210 LO >= 50 MHz with a "
                        "small offset (SFR uses 60.4 MHz)")
    g.add_argument("--us-rate", type=int, default=32,
                   help="symbol rate in 160 ksym/s units (16 = 2.56 Msym/s, 3.2 MHz; 8 = 1.28 Msym/s)")
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
    g.add_argument("--ds-queue-ms", type=float, default=100.0,
                   help="downstream data frames waiting longer than this are dropped (bounded latency)")
    g.add_argument("--ds-channels", type=int, default=2,
                   help="bonded downstream channels, 8 MHz apart (DOCSIS 3.0): 1-3 work (3: 142.8 Mbit/s "
                        "carried, 2026-10-10); 4-6 need a master clock the Sagem never locked on")
    g.add_argument("--synth-threads", type=int, default=2,
                   help="threads sharing the shaping of 3+ bonded downstream channels")
    g.add_argument("--ds-spacing", type=float, default=8e6, help="spacing of the bonded downstream channels (Hz)")
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
