#!/usr/bin/env python3
"""Lab monitor: watch the downstream of OUR test CMTS (ds_tx.py) on the lab coax with a
second receiver, decode it continuously, print live diagnostics and write a pcap.

Everything (data, unicast management, broadcast management) is written to the pcap
ONLY when the stream comes from the lab CMTS (source MAC of the SYNC messages = --lab-mac,
the same value as ds_tx.py --mac). On any other network the monitor falls back to
broadcast management messages only.

  uhd_find_devices                                      # note the two serial numbers
  ./ds_tx.py --args type=b200,serial=AAAA --freq 474e6 --gain 40       # B210 #1 (TX)
  ./lab_monitor.py --args serial=BBBB --freq 474e6 --pcap lab.pcap     # B210 #2 (RX)
  wireshark lab.pcap

Offline test, without hardware:
  ./ds_tx.py --out t.cfile --seconds 3 && ./lab_monitor.py --file t.cfile --pcap t.pcap
"""
import argparse
import collections
import struct
import time

import numpy as np
from scipy.signal import oaconvolve

from cmts import rx_j83a as rx
from cmts.j83a import rrc_taps
from cmts.ts_demux import DocsisDemux

RRC = rrc_taps(4, 16).astype(np.float32)
SKIP = 650000  # symbols used by the equalizer to converge
MTYPES = {1: "SYNC", 2: "UCD", 3: "MAP", 4: "RNG-REQ", 5: "RNG-RSP", 6: "REG-REQ", 7: "REG-RSP",
          8: "UCC-REQ", 9: "UCC-RSP", 12: "BPKM-REQ", 13: "BPKM-RSP", 14: "REG-ACK", 29: "UCD29",
          33: "MDD", 35: "UCD35", 44: "REG-REQ-MP", 45: "REG-RSP-MP"}


class PcapWriter:
    def __init__(self, path):
        self.f = open(path, "wb")
        self.f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 143))  # DLT_DOCSIS

    def write(self, t, frame):
        sec = int(t)
        self.f.write(struct.pack("<IIII", sec, int((t - sec) * 1e6), len(frame), len(frame)) + frame)

    def close(self):
        self.f.close()


def decode(x, fs, qam, invert_first=True):
    """IQ chunk -> dict with TS packets and PHY diagnostics, or None."""
    z0 = rx.channelize(x, fs, 0)
    for inv in (invert_first, not invert_first):
        z = np.conj(z0) if inv else z0
        z = oaconvolve(z, RRC).astype(np.complex64)
        s, tau = rx.timing_recover(z)
        s, fo = rx.freq_correct(s)
        sym, _, mer = rx.equalize(s, qam)
        m = 8 if qam == 256 else 6
        d = rx.diff_decode(sym, m)
        for ph in range(1 if m == 8 else 4):
            b = rx.symbols_to_bytes(d[SKIP:], m, ph)
            off, _ = rx.find_sync(b)
            if off is not None:
                ts = rx.fast_outer_decode(b[off:])
                return dict(ts=ts, mer=mer, fo=fo * rx.SYMRATE, inverted=inv,
                            ppm=(tau[-1] - tau[0]) / (4 * s.size) * 1e6, m=m)
    return dict(ts=None, mer=mer, fo=fo * rx.SYMRATE, inverted=None, ppm=0)


def frame_info(f):
    """(is_mgmt, mgmt type, SA, DA multicast?) for a DOCSIS MAC frame."""
    if f[0] >> 6 != 3 or f[0] not in (0xC0, 0xC2, 0xC3):
        return False, None, None, None
    hl = 6 + (f[1] if f[0] & 1 else 0)
    if len(f) < hl + 20:
        return True, None, None, None
    return True, f[hl + 18], bytes(f[hl + 6:hl + 12]), bool(f[hl] & 1)


def analyze(res, t0, pcap, lab_mac, state):
    ts = res["ts"]
    ok = ts[(ts[:, 1] & 0x80) == 0]
    dm = DocsisDemux()
    sync_pos, sync_val = [], []
    for i, p in enumerate(ts):
        if p[1] & 0x80:
            continue
        dm.push_packet(p)
        # SYNC position for the jitter check (SYNC at the start of a packet, as ds_tx.py and SFR do)
        if (int(p[1]) & 0x1F) << 8 | int(p[2]) == 0x1FFE and p[1] & 0x40 and p[4] == 0 and p[5] == 0xC0:
            n = i * 204 + 31
            sync_pos.append((n + 204 * (n % 12)) * 8 // res["m"])
            sync_val.append(int.from_bytes(bytes(p[31:35]), "big"))
    pid = ((ok[:, 1].astype(int) & 0x1F) << 8) | ok[:, 2]
    kinds = collections.Counter()
    sources = set()
    for f in dm.frames:
        mg, mt, sa, _ = frame_info(f)
        if mt == 1:
            sources.add(sa)
    is_lab = bool(sources) and sources <= {lab_mac}
    if sources and not is_lab and not state.get("warned"):
        print("!! SYNC source MAC is not --lab-mac: not our CMTS, only broadcast management is recorded")
        state["warned"] = True
    sym_t = 1 / rx.SYMRATE
    written = 0
    for k, f in enumerate(dm.frames):
        mg, mt, sa, mcast = frame_info(f)
        kinds[MTYPES.get(mt, mt) if mg else "DATA"] += 1
        if pcap and (is_lab or (mg and mcast)):
            pcap.write(t0 + k * 1e-6, f)  # frame order kept; timestamps are approximate
            written += 1
    # SYNC timestamp jitter vs symbol position (10.24 MHz counts per symbol = 1280/869)
    jit = None
    if len(sync_pos) >= 3:
        pos = np.array(sync_pos, float)
        val = np.unwrap(np.array(sync_val, float), period=2 ** 32)
        a, c = np.polyfit(pos, val, 1)
        jit = (np.ptp(val - (a * pos + c)) / 10.24e6 * 1e9, a / (1280 / 869))
    return dict(kinds=kinds, n=len(ts), rs_bad=np.mean(ts[:, 1] & 0x80 != 0), cc=dm.cc_errors,
                null=np.mean(pid == 0x1FFF), jit=jit, written=written, lab=is_lab, pcap=pcap is not None,
                sync_iv=np.median(np.diff(sync_val)) / 10240 if len(sync_val) > 1 else None)


def report(i, res, st):
    phy = (f"{res['qam']}-QAM  MER {res['mer']:4.1f} dB  porteuse {res['fo']:+6.0f} Hz  horloge {res['ppm']:+5.1f} ppm  "
           f"spectre {'inversé' if res['inverted'] else 'normal'}")
    if st is None:
        print(f"[{i}] {phy}  -> pas de synchro MPEG")
        return
    k = st["kinds"]
    j = f"gigue SYNC {st['jit'][0]:.0f} ns p-p (horloge x{st['jit'][1]:.6f})" if st["jit"] else "gigue SYNC ?"
    print(f"[{i}] {phy}\n     TS {st['n']} paquets, RS en erreur {100 * st['rs_bad']:.2f} %, CC err {st['cc']}, "
          f"nuls {100 * st['null']:.1f} %, SYNC/{st['sync_iv'] or 0:.2f} ms, {j}\n     trames: "
          + ", ".join(f"{t}:{n}" for t, n in k.most_common())
          + (f"  -> pcap +{st['written']}{'' if st['lab'] else ' (gestion diffusée seulement)'}"
             if st["pcap"] else "  (pas de --pcap : rien n'est enregistré)"), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=474e6, help="lab downstream frequency (Hz)")
    ap.add_argument("--qam", type=int, default=0, choices=(0, 64, 256), help="0 = auto (64 or 256)")
    ap.add_argument("--args", default="", help="UHD args of the monitoring B210, e.g. serial=XXXX")
    ap.add_argument("--gain", type=float, default=20)
    ap.add_argument("--seconds", type=float, default=0.5, help="capture length per iteration")
    ap.add_argument("--count", type=int, default=0, help="iterations (0 = until Ctrl-C)")
    ap.add_argument("--lab-mac", default="02:00:00:ca:fe:01", help="MAC of our CMTS (ds_tx.py --mac)")
    ap.add_argument("--pcap", help="pcap file to write (DLT_DOCSIS)")
    ap.add_argument("--save-iq", help="also save each raw IQ chunk with this prefix (fc32)")
    ap.add_argument("--file", help="offline: read a ds_tx.py --out file (fc32 @ 13.904 MS/s)")
    a = ap.parse_args()
    lab_mac = bytes.fromhex(a.lab_mac.replace(":", ""))
    pcap = PcapWriter(a.pcap) if a.pcap else None
    state = {}

    if a.file:
        fs = 2 * rx.SYMRATE
        data = rx.load_hackrf(a.file, fmt="fc32", skip=0)
        n = int(max(a.seconds, 0.3) * fs)
        chunks = iter([data[k:k + n] for k in range(0, data.size - n + 1, n)])
        get = lambda: next(chunks, None)  # noqa: E731
    else:
        from network_load import B210, FS
        fs = FS
        args = "type=b200,master_clock_rate=40e6" + ("," + a.args if a.args else "")
        dev = B210(a.gain, args)
        lvl = np.abs(dev.capture(a.freq, 200000))
        print(f"niveau d'entrée: rms {np.sqrt(np.mean(lvl ** 2)):.3f} (pleine échelle 1.0), "
              f"crête {lvl.max():.2f}" + ("  !! saturation: baisse --gain" if lvl.max() > 0.95 else "")
              + ("  !! trop faible: monte --gain" if np.sqrt(np.mean(lvl ** 2)) < 0.02 else ""))
        get = lambda: dev.capture(a.freq, int(a.seconds * fs))  # noqa: E731

    i = 0
    try:
        while a.count == 0 or i < a.count:
            t0 = time.time()
            x = get()
            if x is None:
                break
            i += 1
            if a.save_iq:
                x.astype(np.complex64).tofile(f"{a.save_iq}_{i:04d}.cfile")
            for q in ([a.qam] if a.qam else [state.get("qam", 64), 320 - state.get("qam", 64)]):
                res = decode(x, fs, q)  # auto: try the last working modulation first
                if res["ts"] is not None:
                    state["qam"] = q
                    break
            res["qam"] = q
            st = analyze(res, t0, pcap, lab_mac, state) if res["ts"] is not None else None
            report(i, res, st)
    except KeyboardInterrupt:
        pass
    finally:
        if pcap:
            pcap.close()
            print(f"pcap écrit: {a.pcap}")


if __name__ == "__main__":
    main()
