# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Receiver check on a synthetic capture: our modulator -> 20 MS/s, +5 MHz offset,
carrier error, clock error, noise, int8 quantization -> rx_j83a -> TS."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import numpy as np
from scipy.signal import resample_poly
from cmts.downstream import DownstreamMux
from cmts import rx_j83a as rx

QAM = int(sys.argv[1]) if len(sys.argv) > 1 else 256
mux = DownstreamMux(qam=QAM, sps=4)
P, X = [], []
for _ in range(40):
    p = mux.packets(256); P.append(p); X.append(mux.shaper.process(mux.enc.encode(p)))
P = np.concatenate(P); x = np.concatenate(X)
# 27.808 MS/s -> 20 MS/s with a 30 ppm clock error
y = resample_poly(x, 625, 869)
from scipy.signal import resample
y = resample(y, int(round(y.size / 1.00003)))  # 30 ppm clock error (FFT resampling)
n = np.arange(y.size)
y = y * np.exp(2j * np.pi * (5e6 + 7300) / 20e6 * n)
rng = np.random.default_rng(1)
y = y + (rng.normal(size=y.size) + 1j * rng.normal(size=y.size)) * 0.02 * np.std(y)
y = y / np.std(y) * 20
y = np.clip(np.round(y.real), -127, 127) + 1j * np.clip(np.round(y.imag), -127, 127)

z = rx.channelize(y.astype(np.complex64), 20e6, 5e6)
from cmts.j83a import rrc_taps
z = np.convolve(z, rrc_taps(4, 16)).astype(np.complex64)
s, tau = rx.timing_recover(z)
s, f = rx.freq_correct(s)
print(f"freq offset est {f * 6.952e6:.0f} Hz; timing drift {tau[-1] - tau[0]:.2f} samples")
m = 6 if QAM == 64 else 8
sym, eq, mer = rx.equalize(s, QAM)
print(f"MER {mer:.1f} dB")
d = rx.diff_decode(sym, m)
for ph in range(4 if m == 6 else 1):
    b = rx.symbols_to_bytes(d, m, ph)
    off, score = rx.find_sync(b)
    if off is not None:
        break
print("sync score", score, "offset", off, "phase", ph)
ts = rx.outer_decode(b[off:])
ok = np.mean(ts[:, 0] == 0x47)
pk = set(map(bytes, P))
match = np.mean([bytes(t) in pk for t in ts[5:]])
print(f"TS packets {ts.shape[0]}, sync ok {ok:.3f}, identical to transmitted {match:.3f}")
