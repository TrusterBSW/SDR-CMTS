"""C kernels of the upstream receiver (cmts/usdsp.c) and of the downstream transmitter
(cmts/dsdsp.c), compiled on first use, with pure Python fallbacks (same results, much slower)
if no C compiler is available.

ctypes releases the GIL while a C function runs: a burst being decoded in the upstream worker
no longer holds up the downstream feed thread (a 1500-byte burst took 46 ms of GIL in Python,
more than the 37.6 ms USB reserve of the TX path).
"""
import ctypes
import os
import subprocess

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_RS_ERRORS = {-1: "too many errors", -2: "uncorrectable (locator roots)", -3: "uncorrectable (forney)",
              -4: "uncorrectable (residual syndrome)", -5: "bad codeword length"}


def _build(name):
    """cmts/<name>.c -> cmts/_<name>.so (rebuilt when the source is newer), loaded."""
    src, lib = os.path.join(_HERE, f"{name}.c"), os.path.join(_HERE, f"_{name}.so")
    try:
        if not os.path.exists(lib) or os.path.getmtime(lib) < os.path.getmtime(src):
            tmp = f"{lib}.{os.getpid()}.tmp"         # atomic: several processes may build at once
            subprocess.run(["cc", "-O3", "-march=native", "-shared", "-fPIC", "-o", tmp, src, "-lm"],
                           check=True, stderr=subprocess.PIPE)
            os.replace(tmp, lib)
        return ctypes.CDLL(lib)
    except (OSError, subprocess.CalledProcessError):
        return None


def _load():
    dll = _build("usdsp")
    if dll is None:
        return None
    F, U8, I32 = ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_int)
    dll.rs_decode.argtypes = [U8, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    dll.rs_decode.restype = ctypes.c_int
    dll.rs_encode.argtypes = [U8, ctypes.c_int, ctypes.c_int, ctypes.c_int, U8]
    dll.rs_encode.restype = None
    dll.interp_symbols.argtypes = [F, ctypes.c_int, ctypes.c_double, ctypes.c_double, ctypes.c_int, F]
    dll.interp_symbols.restype = None
    dll.dd_track.argtypes = [F, ctypes.c_int, F, ctypes.c_int, ctypes.c_float, ctypes.c_float, F, I32]
    dll.dd_track.restype = ctypes.c_float
    dll.dd_track_grid.argtypes = [F, ctypes.c_int, F, ctypes.c_int, ctypes.c_float, ctypes.c_float, I32,
                                  ctypes.c_float, ctypes.c_float, F, I32]
    dll.dd_track_grid.restype = ctypes.c_float
    D = ctypes.c_double
    dll.burst_prep.argtypes = [F, ctypes.c_int, D, D, ctypes.c_int, D, F, ctypes.c_int, ctypes.c_int, F, F]
    dll.burst_prep.restype = ctypes.c_int
    dll.eq_track.argtypes = [F, ctypes.c_int, F, ctypes.c_int, F, ctypes.c_int, ctypes.c_int, ctypes.c_float,
                             ctypes.c_float, I32, ctypes.c_float, ctypes.c_float, F, F, I32]
    dll.eq_track.restype = ctypes.c_float
    dll.symbols_to_bytes.argtypes = [I32, ctypes.c_int, ctypes.c_int, U8, ctypes.c_int, U8]
    dll.symbols_to_bytes.restype = ctypes.c_int
    dll.rs_decode_many.argtypes = [U8, ctypes.c_int, I32, ctypes.c_int, ctypes.c_int, ctypes.c_int, U8, I32]
    dll.rs_decode_many.restype = ctypes.c_int
    dll.rs_find_last.argtypes = [U8, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, U8, I32]
    dll.rs_find_last.restype = ctypes.c_int
    dll.ddc_decim.argtypes = [F, ctypes.c_int, ctypes.c_double, ctypes.POINTER(ctypes.c_double), F, ctypes.c_int,
                              ctypes.c_int, F, I32, F]
    dll.ddc_decim.restype = ctypes.c_int
    dll.fir_cr.argtypes = [F, ctypes.c_int, F, ctypes.c_int, F]
    dll.fir_cr.restype = None
    dll.pre_metric.argtypes = [F, ctypes.c_int, ctypes.c_int, I32, F, ctypes.c_int, F, F, F, F, F]
    dll.pre_metric.restype = None
    return dll


def _load_ds():
    dll = _build("dsdsp")
    if dll is None:
        return None
    F, U8, I32 = ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_int)
    D = ctypes.POINTER(ctypes.c_double)
    dll.j83a_encode.argtypes = [U8, ctypes.c_int, U8, ctypes.c_int, F, U8, I32, F]
    dll.j83a_encode.restype = ctypes.c_int
    dll.mix_add.argtypes = [F, ctypes.c_int, ctypes.c_double, D, F]
    dll.mix_add.restype = None
    dll.shape_mix_add.argtypes = [F, ctypes.c_int, F, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_double, D, F]
    dll.shape_mix_add.restype = None
    dll.scale_clip.argtypes = [F, ctypes.c_int, ctypes.c_float]
    dll.scale_clip.restype = None
    dll.add_into.argtypes = [F, F, ctypes.c_int]
    dll.add_into.restype = None
    return dll


_DLL = _load()
AVAILABLE = _DLL is not None
_DS = _load_ds()
DS_AVAILABLE = _DS is not None
J83A_HIST = 204 * 11                                  # interleaver memory (bytes)


def _u8(a):
    return a.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8))


def j83a_encode(pkts, rand_mask, m, constel, hist, rot):
    """TS packets (N x 188, N multiple of 8) -> complex64 QAM symbols (C, bit-exact with
    cmts/j83a.py). `hist` (uint8[J83A_HIST]) and `rot` (int32[1]) carry the encoder state."""
    pkts = np.ascontiguousarray(pkts, dtype=np.uint8)
    out = np.empty(pkts.shape[0] * 204 * 8 // m, dtype=np.complex64)
    n = _DS.j83a_encode(_u8(pkts), pkts.shape[0], _u8(rand_mask), m, _f(constel.view(np.float32)),
                        _u8(hist), rot.ctypes.data_as(ctypes.POINTER(ctypes.c_int)), _f(out.view(np.float32)))
    if n != out.size:
        raise ValueError(f"j83a_encode: {n}")
    return out


def mix_add(x, step, phase, acc):
    """acc += x * exp(j (phase[0] + k step)), phase[0] (float64 array) carried across calls:
    puts a channel at its frequency offset and sums it with the others (bonding)."""
    x = np.ascontiguousarray(x, dtype=np.complex64)
    if _DS is None:
        k = np.arange(x.size)
        acc += x * np.exp(1j * (phase[0] + step * k)).astype(np.complex64)
        phase[0] = (phase[0] + step * x.size) % (2 * np.pi)
        return acc
    _DS.mix_add(_f(x.view(np.float32)), x.size, float(step), phase.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                _f(acc.view(np.float32)))
    return acc


def _f(a):
    return a.ctypes.data_as(ctypes.POINTER(ctypes.c_float))


def rs_decode(cw, nsym, fcr=0):
    """bytes -> (message bytes, corrected count); ValueError if uncorrectable. C only (the
    Python codec in cmts/rs.py is the fallback, chosen by the caller)."""
    buf = np.frombuffer(bytes(cw), dtype=np.uint8).copy()
    r = _DLL.rs_decode(buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), buf.size, nsym, fcr)
    if r < 0:
        raise ValueError(_RS_ERRORS.get(r, f"rs error {r}"))
    return buf[:buf.size - nsym].tobytes(), r


def rs_encode(msg, nsym, fcr=0):
    m = np.frombuffer(bytes(msg), dtype=np.uint8).copy()
    par = np.zeros(nsym, dtype=np.uint8)
    _DLL.rs_encode(m.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), m.size, nsym, fcr,
                   par.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)))
    return bytes(msg) + par.tobytes()


def interp_symbols(y, t0, step, n):
    """y(t0 + k*step) for k < n (band-limited interpolation of a complex signal)."""
    y = np.ascontiguousarray(y, dtype=np.complex64)
    n = max(0, int(n))
    if _DLL is None:                                  # fallback: linear interpolation
        t = t0 + step * np.arange(n)
        i = np.clip(np.floor(t).astype(int), 0, max(0, y.size - 2))
        f = (t - i).astype(np.float32)
        return (y[i] * (1 - f) + y[np.minimum(i + 1, y.size - 1)] * f).astype(np.complex64)
    out = np.empty(n, dtype=np.complex64)
    _DLL.interp_symbols(_f(y.view(np.float32)), y.size, float(t0), float(step), n, _f(out.view(np.float32)))
    return out


def fir(x, taps):
    """numpy.convolve(x, taps) ("full") for a complex signal and real taps."""
    x = np.ascontiguousarray(x, dtype=np.complex64)
    h = np.ascontiguousarray(taps, dtype=np.float32)
    if _DLL is None or x.size == 0:
        return np.convolve(x, h).astype(np.complex64)
    out = np.empty(x.size + h.size - 1, dtype=np.complex64)
    _DLL.fir_cr(_f(x.view(np.float32)), x.size, _f(h), h.size, _f(out.view(np.float32)))
    return out


def pre_metric(y, p0, npos, offs, pre):
    """us_phy.BurstDemod._metric at the positions p0 .. p0 + npos - 1 (contiguous): (m, rho, c1, c2)."""
    y = np.ascontiguousarray(y, dtype=np.complex64)
    offs = np.ascontiguousarray(offs, dtype=np.int32)
    pre = np.ascontiguousarray(pre, dtype=np.complex64)
    work = np.empty(5 * npos, np.float32)
    m, rho = np.empty(npos, np.float32), np.empty(npos, np.float32)
    c1, c2 = np.empty(npos, np.complex64), np.empty(npos, np.complex64)
    _DLL.pre_metric(_f(y.view(np.float32)), int(p0), int(npos), offs.ctypes.data_as(_I32P),
                    _f(pre.view(np.float32)), pre.size, _f(work), _f(m), _f(rho),
                    _f(c1.view(np.float32)), _f(c2.view(np.float32)))
    return m, rho, c1, c2


_GRIDS = {}


def _grid(pts):
    """(L, offset, step, lut) when the constellation is a square grid of L x L equally spaced
    points (16/64-QAM), else None. Cached by the point table."""
    key = pts.tobytes()
    if key not in _GRIDS:
        g = None
        re, im = np.round(pts.real, 5), np.round(pts.imag, 5)
        lv = np.unique(re)
        L = lv.size
        if L > 2 and L * L == pts.size and np.array_equal(lv, np.unique(im)):
            d = np.diff(lv)
            if np.allclose(d, d[0], rtol=1e-3):
                step, off = float(d[0]), float(lv[0])
                lut = np.full(L * L, -1, dtype=np.int32)
                for k, p in enumerate(pts):
                    lut[int(round((p.real - off) / step)) * L + int(round((p.imag - off) / step))] = k
                g = (L, off, step, lut) if (lut >= 0).all() else None
        _GRIDS[key] = g
    return _GRIDS[key]


def dd_track(r, points, ka=0.08, kb=0.004):
    """Decision-directed 2nd-order phase tracking against any constellation.
    Returns (derotated symbols, decided point indices, mean squared error)."""
    r = np.ascontiguousarray(r, dtype=np.complex64)
    pts = np.ascontiguousarray(points, dtype=np.complex64)
    if _DLL is None:
        out = np.empty_like(r)
        idx = np.empty(r.size, dtype=np.int32)
        ph = fr = 0.0
        err = 0.0
        for n, v in enumerate(r):
            v = v * np.exp(-1j * ph)
            k = int(np.argmin(np.abs(pts - v)))
            e = np.angle(v * np.conj(pts[k]))
            fr += kb * e
            ph += fr + ka * e
            ph = (ph + np.pi) % (2 * np.pi) - np.pi
            out[n], idx[n] = v, k
            err += abs(v - pts[k]) ** 2
        return out, idx, err / max(1, r.size)
    out = np.empty_like(r)
    idx = np.empty(r.size, dtype=np.int32)
    g = _grid(pts)
    if g is not None:                                   # square QAM: decision by rounding
        L, off, step, lut = g
        e = _DLL.dd_track_grid(_f(r.view(np.float32)), r.size, _f(pts.view(np.float32)), L, off, step,
                               lut.ctypes.data_as(ctypes.POINTER(ctypes.c_int)), ka, kb,
                               _f(out.view(np.float32)), idx.ctypes.data_as(ctypes.POINTER(ctypes.c_int)))
        return out, idx, float(e)
    e = _DLL.dd_track(_f(r.view(np.float32)), r.size, _f(pts.view(np.float32)), pts.size,
                      ka, kb, _f(out.view(np.float32)), idx.ctypes.data_as(ctypes.POINTER(ctypes.c_int)))
    return out, idx, float(e)


def status():
    """One line for the console: which kernels run in C."""
    if AVAILABLE and DS_AVAILABLE:
        return "calcul DSP en C (montant usdsp, descendant dsdsp)"
    missing = [n for n, ok in (("montant", AVAILABLE), ("descendant", DS_AVAILABLE)) if not ok]
    return ("!! calcul DSP " + " et ".join(missing) + " en Python (lent) : compilateur C absent ou "
            "compilation ratée de cmts/usdsp.c / cmts/dsdsp.c (essaie : cc -O3 -shared -fPIC ...)")


# ---------------------------------------------------------------- one burst, data stage (C only)
_I32P = ctypes.POINTER(ctypes.c_int)
_U8P = ctypes.POINTER(ctypes.c_uint8)


def burst_prep(y, ty0, sps, n, fr, pre, qam_norm):
    """Symbols of a burst after its preamble is found: (all n symbols after carrier and
    preamble gain correction, data symbols ready for tracking) or None. See usdsp.c."""
    y = np.ascontiguousarray(y, dtype=np.complex64)
    pre = np.ascontiguousarray(pre, dtype=np.complex64)
    r = np.empty(n, np.complex64)
    d = np.empty(max(0, n - pre.size), np.complex64)
    if _DLL.burst_prep(_f(y.view(np.float32)), y.size, float(ty0), float(sps), int(n), float(fr),
                       _f(pre.view(np.float32)), pre.size, int(bool(qam_norm)),
                       _f(r.view(np.float32)), _f(d.view(np.float32))) < 0:
        return None
    return r, d


def eq_track(r, w, points, ka=0.08, kb=0.004):
    """Optional equalizer taps w (centred, None = none), then dd_track. (symbols, decisions, mse)."""
    r = np.ascontiguousarray(r, dtype=np.complex64)
    pts = np.ascontiguousarray(points, dtype=np.complex64)
    out = np.empty_like(r)
    tmp = np.empty_like(r)
    idx = np.empty(r.size, dtype=np.int32)
    g = _grid(pts)
    L, off, step, lut = g if g is not None else (0, 0.0, 0.0, np.zeros(1, np.int32))
    if w is not None:
        w = np.ascontiguousarray(w, dtype=np.complex64)
    e = _DLL.eq_track(_f(r.view(np.float32)), r.size,
                      _f(w.view(np.float32)) if w is not None else None, 0 if w is None else w.size,
                      _f(pts.view(np.float32)), pts.size, L, off, step, lut.ctypes.data_as(_I32P), ka, kb,
                      _f(tmp.view(np.float32)), _f(out.view(np.float32)), idx.ctypes.data_as(_I32P))
    return out, idx, float(e)


def symbols_to_bytes(idx, bps, prbs=None):
    """Decided labels -> bytes (bits MSB first, XORed with the scrambler bits prbs)."""
    idx = np.ascontiguousarray(idx, dtype=np.int32)
    out = np.empty(idx.size * bps // 8, np.uint8)
    pr = np.ascontiguousarray(prbs, dtype=np.uint8) if prbs is not None else None
    _DLL.symbols_to_bytes(idx.ctypes.data_as(_I32P), idx.size, bps,
                          pr.ctypes.data_as(_U8P) if pr is not None else None, 0 if pr is None else pr.size,
                          out.ctypes.data_as(_U8P))
    return out


def rs_decode_many(raw, info_lens, nsym, fcr=0):
    """Codewords back to back (info_lens[i] + nsym bytes each) -> (messages, corrected count,
    index of the first failing codeword or -1)."""
    buf = np.ascontiguousarray(raw, dtype=np.uint8)
    lens = np.ascontiguousarray(info_lens, dtype=np.int32)
    out = np.empty(int(lens.sum()), np.uint8)
    fixed = ctypes.c_int(0)
    bad = _DLL.rs_decode_many(buf.ctypes.data_as(_U8P), buf.size, lens.ctypes.data_as(_I32P), lens.size,
                              nsym, fcr, out.ctypes.data_as(_U8P), ctypes.byref(fixed))
    return out, fixed.value, bad


def rs_find_last(raw, lmax, lmin, nsym, fcr=0):
    """Shortened last codeword of unknown length at raw[0:] (usdsp.c rs_find_last) ->
    (message bytes, corrected count) or None."""
    buf = np.ascontiguousarray(raw, dtype=np.uint8)
    out = np.empty(max(lmax, 1), np.uint8)
    fixed = ctypes.c_int(0)
    L = _DLL.rs_find_last(buf.ctypes.data_as(_U8P), buf.size, lmax, lmin, nsym, fcr,
                          out.ctypes.data_as(_U8P), ctypes.byref(fixed))
    return None if L < 0 else (out[:L].tobytes(), fixed.value)


# ---------------------------------------------------------------- multichannel synthesis (C)
class ChannelSynth:
    """One bonded downstream channel: RRC shaping (same taps as j83a.PulseShaper), optional
    conjugation, frequency shift and sum into a shared buffer, in one C pass (dsdsp.c)."""

    def __init__(self, phases, step, conj):
        self.h = np.ascontiguousarray(phases, dtype=np.float32)          # (sps, ntaps)
        self.sps, self.ntaps = self.h.shape
        self.tail = np.zeros(self.ntaps - 1, np.complex64)
        self.step, self.conj = float(step), bool(conj)
        self.phase = np.zeros(1)

    def add(self, sym, acc):
        x = np.concatenate([self.tail, np.asarray(sym, np.complex64)])
        self.tail = x[-(self.ntaps - 1):].copy()
        _DS.shape_mix_add(_f(x.view(np.float32)), sym.size, _f(self.h.view(np.float32)), self.sps, self.ntaps,
                          int(self.conj), self.step, self.phase.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                          _f(acc.view(np.float32)))


def scale_clip(acc, scale):
    _DS.scale_clip(_f(acc.view(np.float32)), acc.size, float(scale))
    return acc


def add_into(dst, src):
    _DS.add_into(_f(dst.view(np.float32)), _f(src.view(np.float32)), dst.size)
    return dst


class Ddc:
    """One channel out of a wideband complex stream: shift by `offset` Hz, low-pass, decimate
    (usdsp.c ddc_decim), state carried across calls."""

    def __init__(self, fs, offset, decim, cutoff, ntaps=63):
        from scipy.signal import firwin
        self.h = firwin(ntaps, cutoff, fs=fs).astype(np.float32)
        self.step = -2 * np.pi * offset / fs
        self.phase = np.zeros(1)
        self.decim = int(decim)
        self.hist = np.zeros(ntaps - 1, np.complex64)
        self.skip = ctypes.c_int(0)

    def process(self, x):
        x = np.ascontiguousarray(x, dtype=np.complex64)
        out = np.empty(x.size // self.decim + 2, np.complex64)
        n = _DLL.ddc_decim(_f(x.view(np.float32)), x.size, self.step, self.phase.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                           _f(self.h.view(np.float32)), self.h.size, self.decim, _f(self.hist.view(np.float32)),
                           ctypes.byref(self.skip), _f(out.view(np.float32)))
        return out[:n]

