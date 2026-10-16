#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Spectrum scan with hackrf_transfer in RECEIVE mode only (no sweep firmware needed).
Steps of 16 MHz (keeps the central 16 of 20 MHz), averages PSD, writes CSV freq_MHz,dBFS/Hz."""
import argparse, os, subprocess, tempfile
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--start", type=float, default=2)
ap.add_argument("--stop", type=float, default=1002)
ap.add_argument("--lna", type=int, default=16)
ap.add_argument("--vga", type=int, default=20)
ap.add_argument("--out", default="scan.csv")
a = ap.parse_args()

FS, STEP, NFFT = 20e6, 16e6, 1024
tmp = os.path.join(tempfile.gettempdir(), "scan.iq")
rows = []
for fc in np.arange(a.start * 1e6 + STEP / 2, a.stop * 1e6, STEP):
    subprocess.run(["hackrf_transfer", "-r", tmp, "-f", str(int(fc)), "-s", str(int(FS)),
                    "-n", str(2_000_000), "-a", "0", "-l", str(a.lna), "-g", str(a.vga)],
                   capture_output=True, check=True)
    x = np.fromfile(tmp, dtype=np.int8).astype(np.float32).view(np.complex64) / 128
    x = x[200_000:]                      # drop settling
    x -= x.mean()                        # remove DC
    seg = x[: x.size // NFFT * NFFT].reshape(-1, NFFT) * np.hanning(NFFT)
    psd = np.fft.fftshift((np.abs(np.fft.fft(seg, axis=1)) ** 2).mean(0))
    f = fc + np.fft.fftshift(np.fft.fftfreq(NFFT, 1 / FS))
    keep = np.abs(f - fc) <= STEP / 2
    keep &= np.abs(f - fc) > 100e3       # hide the DC spike
    rows.append(np.c_[f[keep] / 1e6, 10 * np.log10(psd[keep] + 1e-20)])
    print(f"{fc / 1e6:7.1f} MHz  peak {10 * np.log10(psd.max()):6.1f} dB", flush=True)
np.savetxt(a.out, np.concatenate(rows), delimiter=",", fmt="%.4f")
