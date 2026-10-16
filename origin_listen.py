#!/usr/bin/env python3
"""Long capture of EuroDOCSIS downstream channels (receive only) -> what concerns ONE modem.

Decodes an IQ file recorded by tools/origin_rx.sh (several minutes, one or more channels in the
band), slice by overlapping slice, and keeps ONLY:
  - the management messages sent to all modems (SYNC, UCD, MAP, MDD...);
  - every frame addressed to --mac: its unicast management (RNG-RSP, REG-RSP-MP, DBC...) and
    its own IP traffic (DHCP, TFTP, ToD);
  - the broadcast DHCP replies for --mac.
Every other frame (the data of other subscribers) is discarded as it is decoded.

Outputs, per channel: <prefix>_<MHz>.pcap (DLT_DOCSIS, Wireshark), and when seen: the config
file the modem downloaded by TFTP (<prefix>_<name>), its REG-RSP(-MP) TLVs (<prefix>_regrsp.bin)
with a readable dump. The raw IQ file holds everyone's traffic: delete it once decoded.

  ./origin_listen.py cap.sc16 --fs 48e6 --center 298e6 --freq 282e6 --freq 314e6 \\
      --mac 24:7f:20:a8:38:b8 --out origin
"""
import argparse
import collections
import os
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor

from fractions import Fraction

import ctypes

import numpy as np
from scipy.signal import oaconvolve, resample_poly

from cmts import cdsp, cmcfg, j83a
from cmts import rx_j83a as rx
from cmts.j83a import rrc_taps
from cmts.pcap import DLT_DOCSIS
from cmts.ts_demux import DocsisDemux

DOCSIS_PID = 0x1FFE
RRC = rrc_taps(4, 16).astype(np.complex64)
SOFT = True                       # 256-QAM: RS errors-and-erasures from symbol reliabilities (--hard)
_rsee = None
RS_STATS = [0, 0, 0, 0]           # last slice: packets, hard RS ok, recovered with erasures, lost
MGMT_NAMES = {1: "SYNC", 2: "UCD", 3: "MAP", 4: "RNG-REQ", 5: "RNG-RSP", 6: "REG-REQ", 7: "REG-RSP",
              8: "UCC-REQ", 9: "UCC-RSP", 12: "BPKM-REQ", 13: "BPKM-RSP", 14: "REG-ACK", 15: "DSA-REQ",
              16: "DSA-RSP", 17: "DSA-ACK", 18: "DSC-REQ", 19: "DSC-RSP", 20: "DSC-ACK", 21: "DSD-REQ",
              22: "DSD-RSP", 23: "DCC-REQ", 24: "DCC-RSP", 25: "DCC-ACK", 26: "DCI-REQ", 27: "DCI-RSP",
              28: "UP-DIS", 29: "UCD2", 30: "INIT-RNG-REQ", 33: "MDD", 34: "B-INIT-RNG-REQ", 35: "UCD3",
              36: "DBC-REQ", 37: "DBC-RSP", 38: "DBC-ACK", 39: "DPV-REQ", 40: "DPV-RSP", 41: "CM-STATUS",
              42: "CM-CTRL-REQ", 43: "CM-CTRL-RSP", 44: "REG-REQ-MP", 45: "REG-RSP-MP", 49: "OCD", 50: "DPD"}


# ---------------------------------------------------------------- demodulation, slice by slice
def channelize(x, fs, f_off, sps=4):
    """rx.channelize for wide captures, lighter in memory and time: DC removed (no I/Q balance
    correction, the B210's calibration is enough), frequency shift in place block by block,
    polyphase resampling to sps x 6.952 MS/s."""
    x -= x.mean()
    w, blk = -2 * np.pi * f_off / fs, 1 << 20
    for i in range(0, x.size, blk):
        x[i:i + blk] *= np.exp(1j * w * np.arange(i, min(i + blk, x.size))).astype(np.complex64)
    fr = Fraction(int(sps * rx.SYMRATE), int(fs)).limit_denominator(5000)
    return resample_poly(x, fr.numerator, fr.denominator).astype(np.complex64)


def demod(z, qam, invert):
    if invert:                                           # spectral inversion: mirrored constellation
        z = np.conj(z)
    z = oaconvolve(z, RRC).astype(np.complex64)
    s, _ = rx.timing_recover(z)
    s, _ = rx.freq_correct(s)
    sym, out, mer = rx.equalize(s, qam)
    return sym, out, mer


def byte_reliability(out):
    """256-QAM, one byte per symbol: distance (in half grid steps) of each equalized symbol to the
    nearest decision boundary; the byte also depends on the previous symbol's quadrant through the
    differential coding of its 2 MSBs, hence its distance to the axes too."""
    const = j83a.qam_table(8)
    scale = float(np.min(np.abs(const.real)))
    side = 16

    def dist(v):
        u = v / scale
        o = np.clip(2 * np.floor(u / 2) + 1, -(side - 1), side - 1)
        d = 1 - np.abs(u - o)
        return np.where((np.abs(o) == side - 1) & (np.abs(u) > side - 1), 1 + np.abs(u) - (side - 1), d)

    rel = np.minimum(dist(out.real), dist(out.imag)).astype(np.float32)
    axis = np.minimum(np.abs(out.real), np.abs(out.imag)) / scale
    rel[1:] = np.minimum(rel[1:], axis[:-1])
    return rel


def soft_outer_decode(b, rel):
    """Bytes starting at an inverted sync, with their reliabilities -> (TS packets with the TEI bit
    set when RS failed, per packet: erasures used or -1). Deinterleave as cmts.rx_j83a, then
    cmts/rsee.c: errors only, else erasing the 2, 4 .. 16 least reliable bytes."""
    global _rsee
    if _rsee is None:
        _rsee = cdsp._build("rsee")
        U8, F, I32 = (ctypes.POINTER(t) for t in (ctypes.c_uint8, ctypes.c_float, ctypes.c_int))
        _rsee.rs_decode_soft.argtypes = [U8, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, F,
                                         ctypes.c_int, I32]
    R = j83a.RS_LEN
    n = (b.size - R * (j83a.INTER_I - 1)) // (8 * R) * (8 * R)
    k = np.arange(n)
    idx = k + R * (k % j83a.INTER_I)
    cw = np.ascontiguousarray(b[idx].reshape(-1, R))
    r = np.ascontiguousarray(rel[idx].reshape(-1, R))
    st = np.zeros(cw.shape[0], dtype=np.int32)
    _rsee.rs_decode_soft(cw.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), cw.shape[0], R, 16, 0,
                         r.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), 16,
                         st.ctypes.data_as(ctypes.POINTER(ctypes.c_int)))
    ts = (cw[:, :188].reshape(-1, 8, 188) ^ j83a.RAND_MASK).reshape(-1, 188)
    ts[:, 0] = 0x47
    ts[st < 0, 1] |= 0x80
    return ts, st


def slice_ts(x, fs, offset, mode):
    """IQ slice -> (TS packets of PID 0x1FFE that passed RS, their sample index in the slice, MER).
    mode: [qam, invert] or None (tries 256/64-QAM, both orientations, and fills it)."""
    tries = [mode] if mode[0] else [(q, inv) for q in (256, 64) for inv in (True, False)]
    z = channelize(x, fs, offset)
    for qam, inv in tries:
        sym, out, mer = demod(z, qam, inv)
        m = 6 if qam == 64 else 8
        d = rx.diff_decode(sym, m)
        for ph in range(4 if m == 6 else 1):
            b = rx.symbols_to_bytes(d, m, ph)
            off, _ = rx.find_sync(b)
            if off is not None:
                break
        if off is not None:
            mode[:] = [qam, inv]
            break
    else:
        return None, None, mer
    if SOFT and m == 8:
        ts, st = soft_outer_decode(b[off:], byte_reliability(out)[off:])
    else:
        ts, st = rx.outer_decode(b[off:]), None
    skip = (650000 * m // 8 - off) // 204 + 20           # equalizer still converging (blind CMA)
    idx = np.arange(ts.shape[0])
    if st is not None:
        t = st[max(2, skip):]
        RS_STATS[:] = [t.size, int(np.sum(t == 0)), int(np.sum(t > 0)), int(np.sum(t < 0))]
    keep = (idx >= max(2, skip)) & ((ts[:, 1] & 0x80) == 0) \
        & ((((ts[:, 1].astype(int) & 0x1F) << 8) | ts[:, 2]) == DOCSIS_PID)
    # packet p starts at byte off + 204 p (the deinterleaver delay, ~2 ms, is left out), one
    # byte = 8/m symbols, one symbol = fs / 6.952 MS/s samples
    pos = ((off + 204 * idx[keep]) * 8 / m + ph) * fs / rx.SYMRATE
    return ts[keep], pos, mer


def channel_frames(raw, fs, offset, chunk_s, overlap_s, log, keep):
    """Whole capture of one channel -> [(sample index, MAC frame bytes, keep(frame))] for the
    frames keep() accepts (the others are dropped as soon as they are decoded). The packets of
    the overlaps count once: the next slice resumes right after the previous slice's last DOCSIS
    packet."""
    n_all = raw.size // 2
    step, ov = int(chunk_s * fs), int(overlap_s * fs)
    dm, frames, mode = DocsisDemux(), [], [None, None]
    rs_tot = [0, 0, 0, 0]
    last, last_pos, mers, lost_joins, n_frames = None, -1.0, [], 0, 0
    for a in range(0, n_all, step):
        s0 = max(0, a - ov)
        s1 = min(n_all, a + step)
        if s1 - s0 < int(0.3 * fs):
            break
        seg = raw[2 * s0:2 * s1].astype(np.float32)
        x = (seg[0::2] + 1j * seg[1::2]).astype(np.complex64)
        ts, pos, mer = slice_ts(x, fs, offset, mode)
        if ts is None:
            log(f"   {a / fs:6.1f} s : pas de synchro MPEG (MER {mer:.1f} dB)")
            last = None
            continue
        mers.append(mer)
        pos = pos + s0
        start = 0
        if last is not None:
            hit = [j for j in range(min(len(ts), 4000)) if pos[j] < a + 0.05 * fs and
                   np.array_equal(ts[j], last)]
            if hit:
                start = hit[-1] + 1
            else:
                start = int(np.searchsorted(pos, last_pos + 1))
                lost_joins += 1
                dm.cc = None                              # possible gap: resynchronize on a PUSI
                dm.buf, dm.synced = b"", False
        for p, ps in zip(ts[start:], pos[start:]):
            n0 = len(dm.frames)
            dm.push_packet(p)
            for f in dm.frames[n0:]:
                k = keep(f)
                if k:
                    frames.append((ps, f, k))
                n_frames += 1
            dm.frames.clear()
        if len(ts):
            last, last_pos = ts[-1].copy(), pos[-1]
        n_rs, *_ = RS_STATS
        if n_rs:
            rs_tot[:] = [u + v for u, v in zip(rs_tot, RS_STATS)]
        if a // step % 10 == 0:
            log(f"   {a / fs:6.1f} s : MER {mer:.1f} dB, {n_frames} trames DOCSIS, {len(frames)} gardées"
                + (f", RS perdus {100 * RS_STATS[3] / n_rs:.1f} %" if n_rs else ""))
    qam, inv = mode
    log(f"   {qam}-QAM{' inversé' if inv else ''}, MER moyen {np.mean(mers) if mers else 0:.1f} dB, "
        f"{n_frames} trames, erreurs de continuité {dm.cc_errors}, raccords incertains {lost_joins}")
    if rs_tot[0]:
        n = rs_tot[0]
        log(f"   RS : {100 * rs_tot[1] / n:.1f} % corrigés seuls, {100 * rs_tot[2] / n:.1f} % avec effacements, "
            f"{100 * rs_tot[3] / n:.2f} % perdus")
    return frames


# ---------------------------------------------------------------- what to keep
def mac_hdr(f):
    """-> (FC, header length incl. EHDR and HCS, EHDR bytes)."""
    ehl = f[1] if f[0] & 1 else 0
    return f[0], 6 + ehl, bytes(f[4:4 + ehl])


def ipv4_udp(f, off):
    """Ethernet payload at f[off:] -> (src ip, dst ip, sport, dport, UDP payload) or None."""
    if len(f) < off + 14 + 28:
        return None
    et, p = struct.unpack(">H", f[off + 12:off + 14])[0], off + 14
    if et == 0x8100:
        et, p = struct.unpack(">H", f[p + 2:p + 4])[0], p + 4
    if et != 0x0800 or f[p + 9] != 17:
        return None
    ihl = (f[p] & 15) * 4
    sport, dport, ln = struct.unpack(">HHH", f[p + ihl:p + ihl + 6])
    return bytes(f[p + 12:p + 16]), bytes(f[p + 16:p + 20]), sport, dport, bytes(f[p + ihl + 8:p + ihl + ln])


def keep_frame(f, mac):
    """-> 'diffusion', 'modem', 'dhcp' or None (frame discarded)."""
    if len(f) < 6:
        return None
    fc, off, _ = mac_hdr(f)
    if len(f) < off + 12:
        return None
    da = bytes(f[off:off + 6])
    mgmt = fc in (0xC0, 0xC2, 0xC3)
    if mgmt and da[0] & 1:
        return "diffusion"
    if da == mac:
        return "modem"
    if fc & 0xC0 == 0 and da == b"\xff" * 6:
        u = ipv4_udp(f, off)
        if u and u[2] == 67 and len(u[4]) >= 34 and u[4][28:34] == mac:
            return "dhcp"
    return None


def mgmt_type(f):
    fc, off, _ = mac_hdr(f)
    if fc not in (0xC0, 0xC2, 0xC3) or len(f) < off + 20:
        return None
    return f[off + 18]                    # DA SA LEN, then DSAP SSAP control version TYPE reserved


def bpi_on(ehdr):
    """DS extended header: a BPI element (type 1) with its enable bit -> encrypted payload."""
    i = 0
    while i < len(ehdr):
        t, ln = ehdr[i] >> 4, ehdr[i] & 15
        if t == 1 and ln >= 1 and ehdr[i + 1] & 0x80:
            return True
        i += 1 + ln
    return False


# ---------------------------------------------------------------- DHCP, TFTP, REG-RSP
def dhcp_summary(p):
    yi, si = p[16:20], p[20:24]
    file = p[108:236].split(b"\0")[0].decode("latin-1")
    opts, i = {}, 240
    while i < len(p) and p[i] != 255:
        if p[i] == 0:
            i += 1
            continue
        opts[p[i]] = p[i + 2:i + 2 + p[i + 1]]
        i += 2 + p[i + 1]
    ip = lambda b: ".".join(map(str, b[:4]))
    kind = {2: "OFFER", 5: "ACK", 6: "NAK"}.get(opts.get(53, b"\0")[0], "?")
    out = [f"DHCP {kind} : adresse {ip(yi)}, serveur TFTP {ip(si)}, fichier « {file} »"]
    names = {1: "masque", 3: "routeur", 2: "décalage horaire", 4: "serveur d'heure", 6: "DNS", 7: "syslog",
             51: "bail (s)", 54: "serveur DHCP", 66: "serveur TFTP (66)", 67: "fichier (67)", 43: "options constructeur",
             125: "options constructeur (125)"}
    for k, v in sorted(opts.items()):
        if k == 53:
            continue
        if k in (1, 3, 4, 6, 7, 54) and len(v) % 4 == 0:
            val = ", ".join(ip(v[j:j + 4]) for j in range(0, len(v), 4))
        elif k in (2, 51) and len(v) == 4:
            val = str(struct.unpack(">i", v)[0])
        elif k in (66, 67):
            val = v.decode("latin-1")
        else:
            val = v.hex()
        out.append(f"   option {k} {names.get(k, '')} : {val}")
    return out


class Tftp:
    """TFTP downloads to the modem (DATA blocks from the server), keyed by (server, ports)."""

    def __init__(self):
        self.x = {}

    def push(self, u):
        src, dst, sport, dport, p = u
        if len(p) < 4 or sport in (67, 68, 37, 53, 123):
            return
        op = struct.unpack(">H", p[:2])[0]
        key = (src, sport, dport)
        if op == 6:                                       # OACK: negotiated block size
            fields = p[2:].split(b"\0")
            opts = dict(zip(fields[0::2], fields[1::2]))
            self.x.setdefault(key, {"blk": 512, "data": {}})["blk"] = int(opts.get(b"blksize", b"512") or 512)
        elif op == 3:
            t = self.x.setdefault(key, {"blk": 512, "data": {}})
            t["data"][struct.unpack(">H", p[2:4])[0]] = p[4:]

    def files(self):
        out = []
        for key, t in self.x.items():
            d = t["data"]
            if not d:
                continue
            n = max(d)
            complete = all(k in d for k in range(1, n + 1)) and len(d[n]) < t["blk"]
            out.append((key, b"".join(d.get(k, b"") for k in range(1, n + 1)), complete, n))
        return out


SUB_NAMES = {5: cmcfg.CAP_NAMES, 24: cmcfg.SF_NAMES, 25: cmcfg.SF_NAMES}


def dump_tlvs(buf, depth=0, names=cmcfg.NAMES):
    """Readable lines for a TLV list, nested TLVs shown when the value parses as TLVs."""
    lines, pad = [], "   " * (depth + 1)
    try:
        tl = cmcfg.parse(buf)
    except ValueError:
        return [pad + buf.hex()]
    for t, v in tl:
        label = f"{t} {names.get(t, '')}".rstrip()
        nested = False
        if len(v) >= 3 and depth < 4 and t not in (6, 7) and not (depth > 0 and len(v) <= 4):
            try:
                sub = cmcfg.parse(v)
                nested = bool(sub) and sum(2 + len(sv) for _, sv in sub) == len(v)
            except ValueError:
                pass
        if nested:
            lines.append(pad + label)
            lines += dump_tlvs(v, depth + 1, SUB_NAMES.get(t, {}) if depth == 0 else {})
        else:
            num = f" ({int.from_bytes(v, 'big')})" if 1 <= len(v) <= 4 else ""
            txt = f" « {v.decode()} »" if len(v) > 4 and all(32 <= c < 127 for c in v) else ""
            lines.append(f"{pad}{label}: {v.hex()}{num}{txt}")
    return lines


# ---------------------------------------------------------------- main
def write_pcap(path, recs):
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, DLT_DOCSIS))
        for t, fr in recs:
            sec = int(t)
            f.write(struct.pack("<IIII", sec, min(999999, int((t - sec) * 1e6)), len(fr), len(fr)) + bytes(fr))


def segment_job(job):
    """Samples [s0, s1) of one channel -> its kept frames, with absolute sample indexes. Runs in a
    process of its own. Decoding starts `overlap` + 0.1 s before s0 (equalizer convergence, MPEG
    and MAC resynchronization) and keeps only the frames completed from s0 on: the previous
    segment, which ends at s0, has the earlier ones."""
    path, fs, center, fq, mac, chunk, overlap, s0, s1, fmt = job
    raw = np.memmap(path, dtype=np.int16 if fmt == "sc16" else np.int8, mode="r")
    m = min(s0, int((overlap + 0.1) * fs))
    t1 = time.time()
    lines = []
    log = lambda s: lines.append(f"[{fq / 1e6:g} {s0 / fs:.0f} s] {s.strip()}")
    frames = channel_frames(raw[2 * (s0 - m):2 * s1], fs, fq - center, chunk, overlap, log,
                            lambda f: keep_frame(f, mac))
    print(f"[{fq / 1e6:g}] {s0 / fs:6.1f}-{s1 / fs:.1f} s décodé en {time.time() - t1:.0f} s", flush=True)
    return [(p + s0 - m, f, k) for p, f, k in frames if p >= m], lines


def channel_job(fq, frames, seg_lines, t0, fs, out):
    """One channel's frames, segments joined -> its pcap, and what main() gathers (REG-RSP
    fragments, DHCP replies, UDP datagrams to the modem)."""
    tag = f"{fq / 1e6:g}"
    lines = [f"== {tag} MHz"] + seg_lines
    t1 = time.time()
    kept, kinds, ucast, regrsp, dhcp, udp = [], collections.Counter(), collections.Counter(), [], [], []
    for pos, f, k in frames:
        t = t0 + pos / fs
        kept.append((t, f))
        kinds[k] += 1
        mt = mgmt_type(f)
        if k == "diffusion":
            ucast[("diffusion", MGMT_NAMES.get(mt, mt))] += 1
            continue
        if mt is not None:
            ucast[("modem", MGMT_NAMES.get(mt, mt))] += 1
            _, off, _ = mac_hdr(f)
            ln = struct.unpack(">H", f[off + 12:off + 14])[0]       # from DSAP on, CRC left out
            body = bytes(f[off + 20:off + 14 + ln])
            if mt == 7:
                regrsp.append((t, 1, 1, body[3:]))
            elif mt == 45 and len(body) >= 5:
                regrsp.append((t, body[4], body[3], body[5:]))       # fragment seq, count
            continue
        fc, off, eh = mac_hdr(f)
        if bpi_on(eh):
            ucast[("modem", "données chiffrées (BPI)")] += 1
            continue
        u = ipv4_udp(f, off)
        if not u:
            ucast[("modem", "données (non UDP)")] += 1
            continue
        ucast[("modem", f"UDP {u[2]}->{u[3]}")] += 1
        if u[2] == 67:
            dhcp.append((t, u[4]))
        else:
            udp.append(u)
    pcap = f"{out}_{tag}.pcap"
    write_pcap(pcap, kept)
    lines.append(f"   gardées {dict(kinds)} -> {pcap}")
    for (who, what), n in sorted(ucast.items(), key=lambda kv: (kv[0][0], -kv[1])):
        lines.append(f"      {who:9s} {what}: {n}")
    return dict(log="\n".join(lines), regrsp=regrsp, dhcp=dhcp, udp=udp)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iq", help="IQ file: sc16 (tools/origin_rx.sh, B210) or s8 (hackrf_transfer)")
    ap.add_argument("--fmt", choices=("sc16", "s8"), default="sc16")
    ap.add_argument("--fs", type=float, default=48e6, help="sample rate (Hz)")
    ap.add_argument("--center", type=float, default=298e6, help="capture center frequency (Hz)")
    ap.add_argument("--freq", type=float, action="append", help="channel to decode (Hz), repeatable "
                    "(default: 282 and 314 MHz, the origin network's primary channels on 2026-10-06)")
    ap.add_argument("--mac", default="24:7f:20:a8:38:b8", help="the modem whose frames are kept")
    ap.add_argument("--t0", type=float, help="time of the first sample (epoch s); default: <iq>.t0")
    ap.add_argument("--chunk", type=float, default=1.5, help="slice length (s)")
    ap.add_argument("--overlap", type=float, default=0.25, help="slice overlap (s), > equalizer convergence")
    ap.add_argument("--seconds", type=float, help="decode only the first seconds")
    ap.add_argument("--out", default="origin", help="output prefix")
    ap.add_argument("--jobs", type=int, default=os.cpu_count(),
                    help="decoding processes (~2 GB each); every channel is cut in time segments")
    ap.add_argument("--hard", action="store_true", help="hard RS decoding only (gr-dtv), no erasures")
    a = ap.parse_args()
    global SOFT
    SOFT = not a.hard

    mac = bytes.fromhex(a.mac.replace(":", ""))
    t0 = a.t0
    if t0 is None:
        try:
            t0 = float(open(a.iq + ".t0").read().split()[0])
        except OSError:
            t0 = os.path.getmtime(a.iq) - os.path.getsize(a.iq) / (4 if a.fmt == "sc16" else 2) / a.fs
    n = os.path.getsize(a.iq) // (4 if a.fmt == "sc16" else 2)
    if a.seconds:
        n = min(n, int(a.seconds * a.fs))
    log = lambda s: print(s, flush=True)
    log(f"{a.iq} : {n / a.fs:.1f} s à {a.fs / 1e6:g} MS/s, centre {a.center / 1e6:g} MHz, "
        f"début {time.strftime('%H:%M:%S', time.localtime(t0))}")

    # every channel cut in time segments (>= 10 s), decoded in parallel then joined back
    freqs = a.freq or [282e6, 314e6]
    nseg = max(1, min(-(-a.jobs // len(freqs)) * 4, int(n / a.fs // 10)))
    cuts = [n * i // nseg for i in range(nseg + 1)]
    jobs = [(a.iq, a.fs, a.center, fq, mac, a.chunk, a.overlap, cuts[i], cuts[i + 1], a.fmt)
            for fq in freqs for i in range(nseg)]
    with ProcessPoolExecutor(min(len(jobs), a.jobs)) as ex:
        segs = list(ex.map(segment_job, jobs))
    results = []
    for c, fq in enumerate(freqs):
        part = segs[c * nseg:(c + 1) * nseg]
        results.append(channel_job(fq, [x for fr, _ in part for x in fr], [l for _, ls in part for l in ls],
                                   t0, a.fs, a.out))
    tftp, regrsp, seen_dhcp = Tftp(), [], []
    for r in results:
        log(r["log"])
        regrsp += r["regrsp"]
        seen_dhcp += r["dhcp"]
        for u in r["udp"]:
            tftp.push(u)

    for t, p in seen_dhcp:
        log(f"[{time.strftime('%H:%M:%S', time.localtime(t))}] " + "\n".join(dhcp_summary(p)))
    for (src, sport, dport), data, complete, n in tftp.files():
        name = f"{a.out}_tftp_{'.'.join(map(str, src))}_{dport}.cfg"
        with open(name, "wb") as f:
            f.write(data)
        log(f"TFTP de {'.'.join(map(str, src))}:{sport} : {len(data)} octets, {n} blocs, "
            f"{'complet' if complete else 'INCOMPLET'} -> {name}")
        if complete:
            tl, problems = cmcfg.check(data)
            log("\n".join(dump_tlvs(data)))
            log("   " + ("; ".join(problems) if problems else "CM MIC correct"))
    # REG-RSP(-MP): the fragments of each sending (a retry repeats them all), the last complete one
    sets, cur = [], None
    for t, seq, nfrag, body in sorted(regrsp):
        if seq == 1:
            cur = {"t": t, "n": nfrag, "parts": {}}
            sets.append(cur)
        if cur is not None:
            cur["parts"][seq] = body
    full = [s for s in sets if len(s["parts"]) == s["n"]]
    if sets:
        log(f"REG-RSP : {len(sets)} envoi(s), {len(full)} complet(s)")
    for k, s in enumerate(full[-1:] if full else sets[-1:]):
        body = b"".join(s["parts"][i] for i in sorted(s["parts"]))
        with open(f"{a.out}_regrsp.bin", "wb") as f:
            f.write(body)
        log(f"REG-RSP de {time.strftime('%H:%M:%S', time.localtime(s['t']))} : {len(s['parts'])}/{s['n']} "
            f"fragment(s), {len(body)} octets de TLV -> {a.out}_regrsp.bin")
        log("\n".join(dump_tlvs(body)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
