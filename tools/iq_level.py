#!/usr/bin/env python3
"""Levels of an IQ capture (sc16: B210 tools; s8: hackrf_transfer): peak and RMS in dBFS, clipped samples, and the
power of each 8 MHz channel in the band, to set the receive gain before a long capture.

  tools/iq_level.py cap.sc16 --fs 48e6 --center 298e6
"""
import argparse

import numpy as np


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iq")
    ap.add_argument("--fs", type=float, default=48e6)
    ap.add_argument("--center", type=float, default=298e6)
    ap.add_argument("--step", type=float, default=8e6, help="channel spacing (Hz)")
    ap.add_argument("--first", type=float, default=282e6, help="a channel center of the grid (Hz)")
    ap.add_argument("--width", type=float, default=6.8e6, help="channel width measured (Hz)")
    ap.add_argument("--fmt", choices=("sc16", "s8"), default="sc16")
    ap.add_argument("--span", type=float, default=0.2, help="seconds analysed")
    a = ap.parse_args()

    full = 32768 if a.fmt == "sc16" else 128
    raw = np.memmap(a.iq, dtype=np.int16 if a.fmt == "sc16" else np.int8, mode="r")
    raw = raw[: min(raw.size, int(2 * a.span * a.fs))].astype(np.float32)
    x = (raw[0::2] + 1j * raw[1::2]) / full
    x = x[int(0.02 * a.fs):]                               # skip the start (AGC/settling)
    a_abs = np.abs(x)
    clip = np.mean((np.abs(raw) >= 0.98 * full - 1))
    print(f"crête {20 * np.log10(a_abs.max() + 1e-12):6.1f} dBFS, efficace {10 * np.log10(np.mean(a_abs ** 2) + 1e-20):6.1f} dBFS, "
          f"écrêtés {100 * clip:.3f} %" + ("  !! trop de gain" if clip > 1e-4 else ""))
    nfft = 4096
    seg = x[: x.size // nfft * nfft].reshape(-1, nfft)
    psd = np.fft.fftshift(np.mean(np.abs(np.fft.fft(seg * np.hanning(nfft), axis=1)) ** 2, axis=0))
    f = a.center + np.fft.fftshift(np.fft.fftfreq(nfft, 1 / a.fs))
    db = 10 * np.log10(psd + 1e-20)
    floor = np.percentile(db, 5)
    lo, hi = a.center - 0.45 * a.fs, a.center + 0.45 * a.fs
    k0 = int(np.ceil((lo + a.width / 2 - a.first) / a.step))
    k1 = int(np.floor((hi - a.width / 2 - a.first) / a.step))
    print("canal (MHz)   niveau au-dessus du plancher (dB)")
    for k in range(k0, k1 + 1):
        fc = a.first + k * a.step
        m = np.abs(f - fc) < a.width / 2
        lvl = np.mean(db[m]) - floor
        flat = np.std(db[m])
        print(f"  {fc / 1e6:6.1f}     {lvl:5.1f}   {'#' * int(max(0, lvl) // 2)}"
              + ("   (plat : un signal QAM ?)" if lvl > 10 and flat < 2.5 else ""))


if __name__ == "__main__":
    main()
