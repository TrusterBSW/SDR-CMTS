#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Do the CMTS options really change the signal on the coax? Checked by a second radio.

For each case (a set of options), the CMTS transmits on one B210 (--tx-serial) and a second B210
(--witness-serial, receive only, on the same coax through the RX coupler) measures what is on the
air, without being told what to expect:
  1. sweep 202-450 MHz: the occupied 8 MHz channels, their centre and level;
  2. each channel demodulated (J.83 annex A receiver of cmts/rx_j83a.py): spectrum orientation,
     QAM order, MER, TS packets, DOCSIS frames;
  3. what the frames announce: the downstream channels of the MDD, the upstream channels of the
     UCDs (frequency, symbol rate, data burst modulation).
Then the measured values are compared with what each option should have changed.

    python3 tools/arg_effects.py                      # every case (on the 2950X: ~15 min)
    python3 tools/arg_effects.py --only qam64 gain    # some of them

Needs: UHD (python3-uhd), gr-dtv (gnuradio) for the RS decoder, two B210s on the same coax.
NEVER on an operator's network: the CMTS transmits.
"""
import argparse
import os
import signal
import struct
import subprocess
import sys
import time

import numpy as np
from scipy.signal import firwin

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from cmts import docsis_mac as mac  # noqa: E402
from cmts import rx_j83a as rx  # noqa: E402
from cmts.downstream import SYMRATE  # noqa: E402
from cmts.j83a import rrc_taps  # noqa: E402
from cmts.ts_demux import DocsisDemux  # noqa: E402

FS_SWEEP = 8 * SYMRATE                  # 55.616 MS/s per sweep step
FS_CHAN = 4 * SYMRATE                   # 27.808 MS/s for one channel (4 samples per symbol)
UCD_TYPES = {2, 29, 35}
MDD = 33
MOD = {1: "QPSK", 2: "16-QAM", 5: "64-QAM"}

BASE = ["--ds-channels", "2", "--us-channels", "1"]
# name, options added to BASE, checks on the measurement m (functions m -> (ok, text))
CASES = [
    ("base", [], {}),
    ("freq", ["--freq", "410e6"], {}),
    ("ds-channels 3", ["--ds-channels", "3"], {}),
    ("ds-spacing 16 MHz", ["--ds-spacing", "16e6"], {}),
    ("layout up", ["--ds-channels", "3", "--ds-layout", "up"], {}),
    ("qam64", ["--qam", "64"], {}),
    ("gain -10 dB", ["--gain", "-10"], {}),          # relative: replaced by (base gain - 10)
    ("scale /2", ["--scale", "0.15"], {}),
    ("no-invert", ["--no-invert"], {}),
    ("us-freq 45 MHz", ["--us-freq", "45e6"], {}),
    ("us 2.56 Msym/s 16-QAM TDMA", ["--us-mode", "tdma", "--us-mod", "16qam", "--us-rate", "16"], {}),
    ("us-channels 2", ["--us-channels", "2", "--us-spacing", "-7e6"], {}),        # (negative value: parser fixed)
]


def opt(args, name, default=None, conv=float):
    """Last value of an option in a list of arguments."""
    v = default
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            v = conv(args[i + 1])
        elif a.startswith(name + "="):
            v = conv(a.split("=", 1)[1])
    return v


class Witness:
    def __init__(self, serial, gain):
        import uhd
        self.uhd = uhd
        self.u = uhd.usrp.MultiUSRP(f"type=b200,serial={serial}")
        self.u.set_master_clock_rate(FS_SWEEP)
        self.u.set_rx_antenna("RX2", 0)
        self.u.set_rx_gain(gain, 0)
        st = uhd.usrp.StreamArgs("fc32", "sc16")
        st.channels = [0]
        self.stream = self.u.get_rx_stream(st)
        self.gain = gain

    def capture(self, fc, fs, n):
        uhd = self.uhd
        self.u.set_rx_rate(fs, 0)
        self.u.set_rx_bandwidth(min(56e6, fs), 0)
        self.u.set_rx_freq(uhd.types.TuneRequest(fc), 0)
        time.sleep(0.05)
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        cmd.num_samps = n + 20000
        cmd.stream_now = True
        self.stream.issue_stream_cmd(cmd)
        buf = np.zeros(n + 20000, np.complex64)
        md = uhd.types.RXMetadata()
        got = 0
        chunk = np.zeros(self.stream.get_max_num_samps() * 8, np.complex64)
        while got < buf.size:
            k = self.stream.recv(chunk, md, 2.0)
            if k == 0 and md.error_code != uhd.types.RXMetadataErrorCode.none:
                if md.error_code == uhd.types.RXMetadataErrorCode.timeout:
                    break
                continue
            k = min(k, buf.size - got)
            buf[got:got + k] = chunk[:k]
            got += k
        return buf[20000:got]


def sweep(w, lo=202e6, hi=450e6):
    """Occupied 8 MHz channels between lo and hi: [(centre Hz, level dBFS)], from the PSD."""
    freqs, psd = [], []
    step = 40e6                                       # the middle 40 MHz of each 55.6 MHz capture
    for fc in np.arange(lo + step / 2, hi, step):
        x = w.capture(fc, FS_SWEEP, 1 << 18)
        x = x - x.mean()
        nfft = 1 << 12
        seg = x[:x.size // nfft * nfft].reshape(-1, nfft) * np.hanning(nfft)
        p = np.fft.fftshift(np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0)) / (nfft * np.sum(np.hanning(nfft) ** 2) / nfft)
        f = fc + np.fft.fftshift(np.fft.fftfreq(nfft, 1 / FS_SWEEP))
        keep = np.abs(f - fc) < step / 2
        freqs.append(f[keep])
        psd.append(p[keep])
    f = np.concatenate(freqs)
    p = 10 * np.log10(np.concatenate(psd) + 1e-20)
    floor = np.percentile(p, 30)
    occ = p > floor + 12
    # contiguous runs of occupied bins (closing gaps under 0.5 MHz), kept when 6-9 MHz wide or a
    # multiple (adjacent channels merge into one run: split by 8 MHz)
    df = f[1] - f[0]
    chans = []
    i = 0
    while i < f.size:
        if not occ[i]:
            i += 1
            continue
        j = i
        while j + 1 < f.size and (occ[j + 1] or np.any(occ[j + 1:j + 1 + int(0.5e6 / df)])):
            j += 1
        width = f[j] - f[i]
        n = int(round(width / 8e6))
        if n >= 1 and abs(width - 8e6 * n) < 2.5e6:
            for k in range(n):
                a, b = f[i] + k * width / n, f[i] + (k + 1) * width / n
                sel = (f >= a) & (f < b)
                lin = 10 ** (p[sel] / 10)
                chans.append((float(np.sum(f[sel] * lin) / np.sum(lin)), float(10 * np.log10(np.sum(lin)))))
        i = j + 1
    return chans, floor


def _decode(y0, inverted, qam):
    """One stretch of a channel at 4 samples per symbol -> (MER, TS packets or None)."""
    m = 8 if qam == 256 else 6
    y = np.conj(y0) if inverted else y0
    y = np.convolve(y, rrc_taps(4, 16)).astype(np.complex64)             # matched filter
    s, _ = rx.timing_recover(y)
    s, _ = rx.freq_correct(s)
    sym, _, mer = rx.equalize(s, qam)
    # the equalizer converges over its first 400000 symbols (CMA): their bytes are noise and a
    # sync byte found among them puts the deinterleaver out of step
    if sym.size < 600000:                       # capture cut short (USB): nothing to decode
        return mer, None
    b = rx.symbols_to_bytes(rx.diff_decode(sym[450000:], m), m, 0)
    off, _ = rx.find_sync(b)
    return mer, (rx.outer_decode(b[off:]) if off is not None else None)


def _capture_channel(w, fc, seconds):
    """seconds of the channel at fc, at 4 samples per symbol, alone (neighbours filtered out); the
    capture 10 MHz off its centre: away from the witness's LO and DC, and from the I/Q image of a
    neighbour 8 MHz away."""
    fs, lo = FS_CHAN * 2, 10e6
    x = w.capture(fc + lo, fs, int(fs * seconds))
    lvl = dict(rms_dbfs=10 * np.log10(np.mean(np.abs(x) ** 2) + 1e-20), peak=float(np.max(np.abs(x))))
    x, _ = rx.iq_correct(x)
    x *= np.exp(2j * np.pi * lo / fs * np.arange(x.size)).astype(np.complex64)     # channel to 0 Hz
    x = np.convolve(x, firwin(401, 4.3e6, fs=fs)).astype(np.complex64)
    return rx.channelize(x, fs, 0.0), lvl


def demod(w, fc, seconds=0.6, tries=6):
    """One channel: spectrum orientation and QAM order found on a first capture (all four tried),
    then short captures decoded one by one until an MDD and a UCD were seen (each goes out once a
    second; the UCDs only on the primary channel) or `tries` captures: this test receiver
    sometimes loses lock within a long capture, a short one costs little."""
    found, best = None, None
    for attempt in range(3):                 # this receiver misses its lock now and then
        y, lvl = _capture_channel(w, fc, seconds)
        for inverted in (True, False):
            for qam in (256, 64):
                mer, ts = _decode(y, inverted, qam)
                if ts is not None and len(ts):
                    found = (inverted, qam)
                    break
                if best is None or mer > best["mer"]:
                    best = dict(ok=False, inverted=inverted, qam=qam, mer=mer, frames=[], **lvl)
            if found:
                break
        if found:
            break
    if not found:
        return best
    mers, n_ts, frames = [mer], 0, []
    for k in range(tries):
        if k:
            y, _ = _capture_channel(w, fc, seconds)
            mer, ts = _decode(y, *found)
            mers.append(mer)
        if ts is not None:
            dm = DocsisDemux()
            for t in ts:
                dm.push_packet(t)
            frames += dm.frames
            n_ts += len(ts)
        types = {mm[0] for mm in map(mgmt, frames) if mm}
        if MDD in types and types & UCD_TYPES:
            break
    return dict(ok=True, inverted=found[0], qam=found[1], mer=float(np.median(mers)), ts=n_ts,
                frames=frames, **lvl)


def tlvs(buf):
    i = 0
    while i + 2 <= len(buf):
        t, ln = buf[i], buf[i + 1]
        yield t, buf[i + 2:i + 2 + ln]
        i += 2 + ln


def mgmt(fr):
    """(message type, body) of a MAC management frame with a good HCS, None otherwise."""
    if len(fr) < 30 or (fr[0] & 0xFC) != 0xC0 or not mac.parse_us_frame(fr):
        return None
    hl = 6 + (fr[1] if fr[0] & 1 else 0)
    return fr[hl + 18], fr[hl + 20:]


def announced(frames):
    """What the downstream MAC messages announce: MDD channels, UCD upstream channels."""
    ds, us = {}, {}
    for fr in frames:
        mm = mgmt(fr)
        if mm is None:
            continue
        t, body = mm
        if t == MDD:
            for k, v in tlvs(body[4:]):
                if k == 1:
                    d = dict(tlvs(v))
                    if 1 in d and len(d.get(2, b"")) == 4:
                        ds[d[1][0]] = struct.unpack(">I", d[2])[0]
        elif t in UCD_TYPES:
            ch = body[0]
            d = {}
            for k, v in tlvs(body[4:]):
                if k == 1:
                    d["rate"] = v[0] * 160e3
                elif k == 2 and len(v) == 4:
                    d["freq"] = struct.unpack(">I", v)[0]
                elif k in (4, 5):
                    sub = dict(tlvs(v[1:]))
                    if v[0] in (6, 10) and 1 in sub:      # long data IUC (TDMA 6, ATDMA 10)
                        d["mod"] = MOD.get(sub[1][0], sub[1][0])
            d["mode"] = "ATDMA" if t == 29 else "TDMA"
            us[ch] = d
    return ds, us


def run_case(a, w, name, extra):
    args = BASE + extra
    if name.startswith("gain"):
        args = BASE + ["--gain", str(a.gain + float(extra[1]))]
    cmd = [sys.executable, a.cmts, "--args", f"type=b200,serial={a.tx_serial}", "--gain", str(a.gain),
           "--tap", "none", "--status-http", "none", "--log", "none", "--color", "never", "--cpus", a.cpus,
           "--no-irq-herd"] + args
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    lines = []
    try:
        while time.time() - t0 < 40:                       # on the air: first status line
            ln = p.stdout.readline()
            if not ln:
                break
            lines.append(ln)
            if "marge TX" in ln or "!! " in ln and "B210" in ln:
                break
        time.sleep(2)
        chans, floor = sweep(w)
        res = []
        for fc, lvl in chans:
            d = demod(w, fc)
            ds, us = announced(d["frames"]) if d and d["ok"] else ({}, {})
            res.append(dict(freq=fc, level=lvl, **{k: v for k, v in (d or {}).items() if k != "frames"},
                            mdd=ds, ucd=us))
        return dict(name=name, args=args, chans=res, floor=floor, log=lines)
    finally:
        p.send_signal(signal.SIGINT)
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        time.sleep(3)


def show(r):
    print(f"\n=== {r['name']} : {' '.join(r['args'])}")
    if not r["chans"]:
        print("   aucun canal trouvé ; dernières lignes du CMTS :")
        for ln in r["log"][-5:]:
            print("     " + ln.rstrip()[:160])
    for c in r["chans"]:
        line = f"   {c['freq'] / 1e6:7.2f} MHz  {c['level']:6.1f} dB"
        if c.get("ok"):
            line += (f"  {c['qam']}-QAM {'inversé' if c['inverted'] else 'non inversé'}  MER {c['mer']:.1f} dB  "
                     f"{c['ts']} paquets TS")
        elif "mer" in c:
            line += f"  pas de synchro (MER {c['mer']:.1f} dB au mieux)"
        if "rms_dbfs" in c:
            line += f"  (témoin {c['rms_dbfs']:.1f} dBFS, crête {c['peak']:.2f})"
        print(line)
        if c.get("mdd"):
            print("        MDD : " + ", ".join(f"{k}@{v / 1e6:.0f}" for k, v in sorted(c["mdd"].items())))
        for k, u in sorted(c.get("ucd", {}).items()):
            print(f"        UCD {k} : {u.get('freq', 0) / 1e6:.2f} MHz, {u.get('rate', 0) / 1e6:.2f} Msym/s, "
                  f"{u['mode']} {u.get('mod', '?')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tx-serial", default="000000367")
    ap.add_argument("--witness-serial", default="JN76WAJ")
    ap.add_argument("--witness-gain", type=float, default=30)
    ap.add_argument("--gain", type=float, default=49, help="TX gain of the CMTS in every case but 'gain'")
    ap.add_argument("--cmts", default="/usr/lib/sdr-cmts/sdr_cmts.py")
    ap.add_argument("--cpus", default="2-7,18-23")
    ap.add_argument("--only", nargs="*", help="cases whose name contains one of these words")
    a = ap.parse_args()
    cases = [c for c in CASES if not a.only or any(o in c[0] for o in a.only)]
    w = Witness(a.witness_serial, a.witness_gain)
    out = []
    for name, extra, _ in cases:
        r = run_case(a, w, name, extra)
        show(r)
        out.append(r)
    np.save(os.path.join(os.getcwd(), "arg_effects.npy"), np.array(out, dtype=object), allow_pickle=True)


if __name__ == "__main__":
    main()
