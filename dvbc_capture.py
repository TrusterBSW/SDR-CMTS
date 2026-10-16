#!/usr/bin/env python3
"""Demodulate an IQ capture of a DVB-C channel (J.83 Annex A, 6.875 Msym/s, receive only)
and write the MPEG transport stream to a .ts file (Wireshark opens it directly).

  uhd_rx_cfile -a "type=b200,master_clock_rate=32e6" -A RX2 --spec A:A -f 130e6 -r 16e6 -g 38 -N 48000000 -s ch.sc16
  ./dvbc_capture.py ch.sc16 --fs 16e6 --ts mux130.ts
  tsp -I file mux130.ts -P analyze -O drop        # services, PIDs, scrambling
"""
import argparse
import collections
import sys

import numpy as np

from cmts import rx_j83a as rx
from cmts.j83a import rrc_taps

DVBC_SYMRATE = 6.875e6


def demod(x, fs, offset, qam, invert, ntaps):
    z = rx.channelize(x, fs, offset)
    if invert:
        z = np.conj(z)
    z = np.convolve(z, rrc_taps(4, 16)).astype(np.complex64)
    s, _ = rx.timing_recover(z)
    s, f = rx.freq_correct(s)
    sym, _, mer = rx.equalize(s, qam, ntaps=ntaps)
    return sym, mer, f * rx.SYMRATE


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iq")
    ap.add_argument("--fs", type=float, default=16e6)
    ap.add_argument("--fmt", default="sc16", choices=("s8", "sc16", "fc32"))
    ap.add_argument("--offset", type=float, default=0, help="channel center relative to capture center (Hz)")
    ap.add_argument("--symrate", type=float, default=DVBC_SYMRATE)
    ap.add_argument("--qam", type=int, choices=(64, 256), help="default: try 256 then 64")
    ap.add_argument("--ntaps", type=int, default=31, help="equalizer length (operator TV channels show echoes)")
    ap.add_argument("--ts", help="write the transport stream here")
    a = ap.parse_args()

    rx.SYMRATE = a.symrate  # the receiver defaults to the EuroDOCSIS 6.952 Msym/s
    x = rx.load_hackrf(a.iq, fmt=a.fmt)
    peak = np.max(np.abs(np.concatenate([x.real, x.imag])))
    print(f"{x.size} samples, rms {np.std(x):.1f}, peak {peak:.0f}")
    found = None
    for qam in [a.qam] if a.qam else [256, 64]:
        for inv in (True, False):
            sym, mer, fo = demod(x, a.fs, a.offset, qam, inv, a.ntaps)
            m = 6 if qam == 64 else 8
            d = rx.diff_decode(sym, m)
            score = 0
            for ph in range(4 if m == 6 else 1):
                b = rx.symbols_to_bytes(d, m, ph)
                off, score = rx.find_sync(b)
                if off is not None:
                    found = (b, off, qam)
                    break
            print(f"{qam}-QAM{' (spectrum inverted)' if inv else ''}: MER {mer:.1f} dB, "
                  f"carrier offset {fo:.0f} Hz, sync score {score:.3f}")
            if found:
                break
        if found:
            break
    if not found:
        print("no MPEG sync found (not a J.83A channel, wrong symbol rate, or signal too poor)")
        return 1
    b, off, qam = found
    ts = rx.outer_decode(b[off:])
    # drop packets demodulated while the blind equalizer was still converging
    skip = (650000 * (6 if qam == 64 else 8) // 8 - off) // 204 + 20
    ts = ts[max(2, skip):]
    bad = (ts[:, 1] & 0x80) != 0
    good = ts[~bad]
    pids = collections.Counter(((good[:, 1].astype(int) & 0x1F) << 8) | good[:, 2])
    scr = collections.Counter(int(((p[1] & 0x1F) << 8) | p[2]) for p in good if p[3] & 0xC0)
    rate = a.symrate * (6 if qam == 64 else 8) * 188 / 204 / 1e6
    print(f"TS packets {ts.shape[0]} ({ts.shape[0] * 188 * 8 / (rate * 1e6):.2f} s at {rate:.2f} Mbit/s), "
          f"RS-uncorrectable {bad.mean():.4f}")
    print(f"{len(pids)} PIDs, {len(scr)} scrambled:",
          ", ".join(f"0x{p:04X}:{n}{'*' if p in scr else ''}" for p, n in pids.most_common(20)))
    if a.ts:
        good.tofile(a.ts)
        print("written", a.ts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
