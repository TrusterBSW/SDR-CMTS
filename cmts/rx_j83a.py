"""J.83 Annex A (DVB-C / EuroDOCSIS downstream) receiver for offline IQ captures.

IQ -> channel select / resample to 4 sps -> RRC matched filter -> Oerder-Meyr
timing -> 4th power frequency correction -> CMA/DD equalizer + PLL (C) ->
differential decoding -> bytes -> gr-dtv deinterleaver + RS + descrambler -> TS.
"""
import ctypes

import numpy as np
from scipy.signal import resample_poly

from . import j83a
from .downstream import SYMRATE

_lib = None


def _eq_func():
    global _lib
    if _lib is None:
        j83a._load_fastdsp()  # compiles the .so if needed
        import os
        _lib = ctypes.CDLL(os.path.join(os.path.dirname(os.path.abspath(j83a.__file__)), "_fastdsp.so"))
        F = ctypes.POINTER(ctypes.c_float)
        I = ctypes.POINTER(ctypes.c_int)
        _lib.eq_pll.argtypes = [F, ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float,
                                ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float,
                                ctypes.c_float, ctypes.c_float, F, I]
    return _lib.eq_pll


def load_hackrf(path, skip=100000, count=-1, fmt="s8"):
    """Load an IQ capture: s8 = hackrf_transfer, fc32 = uhd_rx_cfile default, sc16 = uhd_rx_cfile -s."""
    dt = {"s8": np.int8, "sc16": np.int16, "fc32": np.float32}[fmt]
    raw = np.fromfile(path, dtype=dt, count=count)
    x = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    return x[skip:]


def iq_correct(x):
    """Remove DC and blindly compensate I/Q imbalance (x = s + b.conj(s) -> s).

    With a 5 MHz offset tune, the mirror image of the channel on the other side
    of the LO lands right on top of the wanted channel; uncorrected it limits
    the MER to ~23 dB on a HackRF."""
    x = x - np.mean(x)
    beta = np.mean(x * x) / (2 * np.mean(np.abs(x) ** 2))
    return (x - beta * np.conj(x)).astype(np.complex64), beta


def channelize(x, fs, f_off, sps=4):
    """Shift the channel at f_off (Hz, relative to capture center) to 0 and resample to sps*Rs."""
    x, _ = iq_correct(x)
    n = np.arange(x.size)
    x = x * np.exp(-2j * np.pi * f_off / fs * n).astype(np.complex64)
    from fractions import Fraction
    fr = Fraction(int(sps * SYMRATE), int(fs)).limit_denominator(5000)
    return resample_poly(x, fr.numerator, fr.denominator).astype(np.complex64)


def timing_recover(y, sps=4, block=4096):
    """Oerder-Meyr square-law timing per block, then cubic interpolation at symbol instants."""
    nblk = y.size // (sps * block)
    ph = np.empty(nblk)
    k = np.arange(sps * block)
    rot = np.exp(-2j * np.pi * k / sps)
    for b in range(nblk):
        seg = y[b * sps * block:(b + 1) * sps * block]
        ph[b] = np.angle(np.sum(np.abs(seg) ** 2 * rot))
    ph = np.unwrap(ph)
    tau = -ph / (2 * np.pi) * sps  # timing offset in samples
    centers = (np.arange(nblk) + 0.5) * block
    nsym = nblk * block
    ks = np.arange(4, nsym - 4)
    t = ks * sps + np.interp(ks, centers, tau)
    i = np.floor(t).astype(np.int64)
    mu = (t - i).astype(np.float32)
    valid = (i >= 1) & (i + 2 < y.size)
    i, mu = i[valid], mu[valid]
    xm1, x0, x1, x2 = y[i - 1], y[i], y[i + 1], y[i + 2]
    # cubic Lagrange
    c0 = -mu * (mu - 1) * (mu - 2) / 6
    c1 = (mu + 1) * (mu - 1) * (mu - 2) / 2
    c2 = -(mu + 1) * mu * (mu - 2) / 2
    c3 = (mu + 1) * mu * (mu - 1) / 6
    return (c0 * xm1 + c1 * x0 + c2 * x1 + c3 * x2).astype(np.complex64), tau


def freq_correct(s):
    """Coarse carrier offset from the 4th power line (cycles/symbol)."""
    n = 1 << int(np.log2(min(s.size, 1 << 20)))
    z = s[:n] ** 4
    Z = np.abs(np.fft.fft(z))
    k = np.argmax(Z)
    f = (k if k < n // 2 else k - n) / n / 4
    return s * np.exp(-2j * np.pi * f * np.arange(s.size)).astype(np.complex64), f


def equalize(s, qam, ntaps=15, n_cma=400000, mu_cma=1e-4, mu_dd=3e-5, pll_a=0.01, pll_b=5e-5):
    m = 6 if qam == 64 else 8
    side = 1 << (m // 2)
    const = j83a.qam_table(m)
    scale = float(np.min(np.abs(const.real)))
    r2 = float(np.mean(np.abs(const) ** 4) / np.mean(np.abs(const) ** 2))
    s = (s / np.sqrt(np.mean(np.abs(s) ** 2))).astype(np.complex64)
    out = np.zeros(s.size, dtype=np.complex64)
    dec = np.zeros(s.size, dtype=np.int32)
    F = ctypes.POINTER(ctypes.c_float)
    I = ctypes.POINTER(ctypes.c_int)
    _eq_func()(s.ctypes.data_as(F), s.size, ntaps, mu_cma, mu_dd, n_cma, side, scale, r2,
               pll_a, pll_b, out.ctypes.data_as(F), dec.ctypes.data_as(I))
    nv = s.size - ntaps
    out, dec = out[:nv], dec[:nv]
    # grid index -> symbol value (Ik Qk + LSBs) through the constellation table
    gi = np.rint((const.real / scale + side - 1) / 2).astype(int)
    gq = np.rint((const.imag / scale + side - 1) / 2).astype(int)
    lut = np.zeros(side * side, dtype=np.int32)
    lut[gi * side + gq] = np.arange(const.size)
    sym = lut[dec]
    ideal = const[sym]
    tail = slice(n_cma + 50000, None)
    err = np.mean(np.abs(out[tail] - ideal[tail]) ** 2)
    mer = 10 * np.log10(np.mean(np.abs(ideal[tail]) ** 2) / err) if err > 0 else 99
    return sym, out, mer


def diff_decode(sym, m):
    """Inverse of the EN 300 429 differential coding (rotation-invariant)."""
    q = m - 2
    rot = np.array([0, 3, 1, 2])[sym >> q]            # IkQk -> rotation
    d = (rot - np.concatenate([[rot[0]], rot[:-1]])) & 3
    ab = np.array([0, 2, 3, 1])[d]                     # rotation step -> AkBk
    return (ab << q) | (sym & ((1 << q) - 1))


def symbols_to_bytes(sym, m, phase=0):
    sym = sym[phase:]
    if m == 8:
        return sym.astype(np.uint8)
    sym = sym[:sym.size // 4 * 4].reshape(-1, 4)
    b0 = (sym[:, 0] << 2) | (sym[:, 1] >> 4)
    b1 = ((sym[:, 1] & 15) << 4) | (sym[:, 2] >> 2)
    b2 = ((sym[:, 2] & 3) << 6) | sym[:, 3]
    return np.stack([b0, b1, b2], 1).ravel().astype(np.uint8)


def find_sync(b):
    """Return the offset of a 0xB8 (inverted sync) byte with 204-periodic syncs, or None."""
    n = (b.size // 204 - 1) * 204
    best = None
    for off in range(204):
        col = b[off:off + n:204]
        score = np.mean((col == 0x47) | (col == 0xB8))
        if best is None or score > best[0]:
            best = (score, off)
    score, off = best
    if score < 0.5:
        return None, score
    col = b[off::204]
    first_b8 = np.nonzero(col == 0xB8)[0]
    if first_b8.size == 0:
        return None, score
    return off + 204 * int(first_b8[0]), score


def outer_decode(b):
    """Interleaved bytes starting at an inverted sync -> TS packets (gr-dtv blocks)."""
    from gnuradio import gr, blocks, dtv
    tb = gr.top_block()
    src = blocks.vector_source_b(b.tolist(), False)
    dil = dtv.dvbt_convolutional_deinterleaver(136, 12, 17)
    rsd = dtv.dvbt_reed_solomon_dec(2, 8, 0x11D, 255, 239, 8, 51, 8)
    des = dtv.dvbt_energy_descramble(8)
    snk = blocks.vector_sink_b()
    tb.connect(src, dil, rsd, des, snk)
    tb.run()
    out = np.array(snk.data(), dtype=np.uint8)
    return out[:out.size // 188 * 188].reshape(-1, 188)


def fast_outer_decode(b):
    """Numpy-only version of outer_decode for streams starting at an inverted sync byte:
    deinterleave, derandomize, and flag (TEI bit) packets whose RS parity does not match.
    No error correction: meant for good signals (MER > 30 dB) and statistics."""
    n = (b.size - j83a.RS_LEN * (j83a.INTER_I - 1)) // (8 * j83a.RS_LEN) * (8 * j83a.RS_LEN)
    if n <= 0:
        return np.zeros((0, 188), dtype=np.uint8)
    k = np.arange(n)
    x = b[k + j83a.RS_LEN * (k % j83a.INTER_I)].reshape(-1, j83a.RS_LEN)  # inverse Forney
    rand = x[:, :188]
    bad = np.any(j83a.rs_encode(rand)[:, 188:] != x[:, 188:], axis=1)
    ts = (rand.reshape(-1, 8, 188) ^ j83a.RAND_MASK).reshape(-1, 188)
    ts[:, 0] = 0x47
    ts[bad, 1] |= 0x80
    return ts
