# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""ITU-T J.83 Annex A (= ETSI EN 300 429, DVB-C) downstream encoder.

Chain: MPEG-TS 188 B -> randomizer (PRBS 1+x^14+x^15, groups of 8 packets)
       -> RS(204,188) t=8 -> convolutional interleaver I=12 M=17
       -> byte to m-tuple -> differential coding of the 2 MSBs -> QAM mapping.
Everything is vectorized with numpy and stateful across calls so the output is
one continuous symbol stream.
"""
import ctypes
import os
import subprocess

import numpy as np

from . import cdsp as _cdsp

TS_LEN = 188
RS_LEN = 204
INTER_I = 12
INTER_M = 17


# ---------------------------------------------------------------- randomizer
def _prbs_bytes():
    """1503 PRBS bytes for one 8-packet group (EN 300 429 7.1)."""
    reg = 0b000000010101001  # stages 1..15 = "100101010000000", stage 1 = bit 0
    out = np.zeros(8 * TS_LEN - 1, dtype=np.uint8)
    for i in range(out.size):
        b = 0
        for _ in range(8):
            fb = ((reg >> 13) ^ (reg >> 14)) & 1
            reg = ((reg << 1) | fb) & 0x7FFF
            b = (b << 1) | fb
        out[i] = b
    return out


def _randomizer_mask():
    """XOR mask for 8 packets (8 x 188). Sync bytes are not XORed (handled apart)."""
    prbs = _prbs_bytes()
    mask = np.zeros(8 * TS_LEN, dtype=np.uint8)
    mask[1:] = prbs  # PRBS runs during sync bytes 2..8 but is not applied there
    mask = mask.reshape(8, TS_LEN)
    mask[:, 0] = 0
    return mask


RAND_MASK = _randomizer_mask()
_RAND_MASK_C = np.ascontiguousarray(RAND_MASK, dtype=np.uint8)


# ---------------------------------------------------------------- RS(204,188)
def _gf_tables():
    exp = np.zeros(512, dtype=np.int32)
    log = np.zeros(256, dtype=np.int32)
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D  # x^8 + x^4 + x^3 + x^2 + 1
    exp[255:510] = exp[0:255]
    return exp, log


GF_EXP, GF_LOG = _gf_tables()


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return int(GF_EXP[GF_LOG[a] + GF_LOG[b]])


def _rs_generator():
    g = [1]
    for i in range(16):  # roots lambda^0 .. lambda^15
        root = int(GF_EXP[i])
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= gf_mul(c, root)
        g = ng
    return g  # g[0] = x^16 coefficient (=1)


def _rs_parity_table():
    """T[pos, byte] = 16 parity bytes contributed by `byte` at message position pos.

    RS is linear over GF(2^8) so parity(msg) = XOR_pos T[pos, msg[pos]].
    """
    g = _rs_generator()
    k = TS_LEN
    unit = np.zeros((k, 16), dtype=np.uint8)
    for pos in range(k):  # parity of the message with a single 1 at `pos`
        reg = [0] * 16
        for i in range(k):
            fb = (1 if i == pos else 0) ^ reg[0]
            reg = reg[1:] + [0]
            if fb:
                for j in range(16):
                    reg[j] ^= gf_mul(fb, g[j + 1])
        unit[pos] = reg
    table = np.zeros((k, 256, 16), dtype=np.uint8)
    for b in range(1, 256):
        lb = GF_LOG[b]
        nz = unit != 0
        v = np.zeros_like(unit, dtype=np.int32)
        v[nz] = GF_EXP[GF_LOG[unit[nz]] + lb]
        table[:, b, :] = v
    return table


RS_TABLE = _rs_parity_table()


def rs_encode(pkts):
    """pkts: (N,188) uint8 -> (N,204) uint8 systematic RS codewords."""
    n = pkts.shape[0]
    par = np.bitwise_xor.reduce(RS_TABLE[np.arange(TS_LEN)[None, :], pkts], axis=1)
    out = np.empty((n, RS_LEN), dtype=np.uint8)
    out[:, :TS_LEN] = pkts
    out[:, TS_LEN:] = par
    return out


def _null_codewords():
    null = np.full((8, TS_LEN), 0xFF, dtype=np.uint8)
    null[:, :4] = [0x47, 0x1F, 0xFF, 0x10]
    r = null ^ RAND_MASK
    r[0, 0] = 0xB8
    return rs_encode(r)


NULL_CW = _null_codewords()


# ---------------------------------------------------------------- QAM mapping
def _gray_decode(bits):
    v = 0
    acc = 0
    for b in bits:  # MSB first
        acc ^= b
        v = (v << 1) | acc
    return v


def qam_table(m):
    """Complex constellation indexed by the *differentially coded* m-bit symbol
    (Ik Qk q-LSBs), EN 300 429 figures 7/8, normalized to unit mean power."""
    q = m - 2
    rot ={0b00: 1, 0b10: 1j, 0b11: -1, 0b01: -1j}  # quadrant 1..4: 0, +pi/2, +pi, +3pi/2
    pts = np.zeros(1 << m, dtype=np.complex128)
    for s in range(1 << m):
        iq = s >> q
        lsb = [(s >> (q - 1 - i)) & 1 for i in range(q)]  # b_{q-1} .. b0
        # In quadrant 1, Q index uses the odd bits (b_{q-1}, ..., b1),
        # I index uses the even bits (b_{q-2}, ..., b0), both Gray coded.
        qbits = lsb[0::2]
        ibits = lsb[1::2]
        x = 2 * _gray_decode(ibits) + 1
        y = 2 * _gray_decode(qbits) + 1
        pts[s] = (x + 1j * y) * rot[iq]
    pts /= np.sqrt(np.mean(np.abs(pts) ** 2))
    return pts


# rotation (in units of pi/2) of quadrant index Ik Qk, and its inverse
_ROT_OF_IQ = np.array([0, 3, 1, 2])          # IQ=00->0, 01->3, 10->1, 11->2
_IQ_OF_ROT = np.array([0b00, 0b10, 0b11, 0b01], dtype=np.int32)
# increment of the rotation for the input MSBs Ak Bk (from the Boolean expression of 8.)
_DROT_OF_AB = np.array([0, 3, 1, 2], dtype=np.uint8)  # AB=00->0, 01->+3, 10->+1, 11->+2


class J83AEncoder:
    """Stateful TS -> QAM symbol encoder. Feed multiples of 8 packets."""

    def __init__(self, qam=64):
        if qam not in (64, 256):
            raise ValueError("qam must be 64 or 256")
        self.m = 6 if qam == 64 else 8
        self.const = qam_table(self.m).astype(np.complex64)
        self.hist = np.zeros(RS_LEN * (INTER_I - 1), dtype=np.uint8)  # interleaver memory
        self.rot = 0  # rotation of the last transmitted symbol
        self.use_c = _cdsp.DS_AVAILABLE
        self._rot = np.zeros(1, dtype=np.int32)

    def interleave(self, cw):
        """Forney interleaver: y[t] = x[t - 204*(t mod 12)] on the byte stream."""
        x = np.concatenate([self.hist, cw.ravel()])
        h = self.hist.size
        n = cw.size
        t = np.arange(n)
        y = x[h + t - RS_LEN * (t % INTER_I)]
        self.hist = x[-h:]
        return y

    def bytes_to_symbols(self, b):
        if self.m == 8:
            return b.astype(np.int32)
        t = b.reshape(-1, 3).astype(np.int32)  # 3 bytes -> 4 six-bit symbols, MSB first
        s = np.empty((t.shape[0], 4), dtype=np.int32)
        s[:, 0] = t[:, 0] >> 2
        s[:, 1] = ((t[:, 0] & 3) << 4) | (t[:, 1] >> 4)
        s[:, 2] = ((t[:, 1] & 15) << 2) | (t[:, 2] >> 6)
        s[:, 3] = t[:, 2] & 63
        return s.ravel()

    def diff_encode(self, sym):
        q = self.m - 2
        d = _DROT_OF_AB[sym >> q]
        d[0] += self.rot
        rot = np.cumsum(d, dtype=np.uint8) & 3  # uint8 wrap-around keeps the value mod 4
        self.rot = int(rot[-1])
        return (_IQ_OF_ROT[rot] << q) | (sym & ((1 << q) - 1))

    def encode(self, pkts):
        """pkts: (N,188) uint8 with N % 8 == 0 -> complex64 symbols (C if built, else numpy)."""
        pkts = np.asarray(pkts, dtype=np.uint8)
        assert pkts.ndim == 2 and pkts.shape[1] == TS_LEN and pkts.shape[0] % 8 == 0
        if self.use_c:
            self._rot[0] = self.rot
            out = _cdsp.j83a_encode(pkts, _RAND_MASK_C, self.m, self.const, self.hist, self._rot)
            self.rot = int(self._rot[0])
            return out
        return self.encode_py(pkts)

    def encode_py(self, pkts):
        """numpy reference encoder (state shared with encode())."""
        r = pkts.reshape(-1, 8, TS_LEN) ^ RAND_MASK
        r[:, 0, 0] = 0xB8  # inverted sync of each 8-packet group
        r = r.reshape(-1, TS_LEN)
        # null packets randomize to one of 8 fixed patterns: reuse their precomputed codewords
        is_null = (pkts[:, 1] == 0x1F) & (pkts[:, 2] == 0xFF) & (pkts[:, 3] == 0x10) \
            & (pkts[:, 4:] == 0xFF).all(axis=1)
        cw = np.empty((r.shape[0], RS_LEN), dtype=np.uint8)
        idx = np.nonzero(~is_null)[0]
        if idx.size:
            cw[idx] = rs_encode(r[idx])
        nidx = np.nonzero(is_null)[0]
        cw[nidx] = NULL_CW[nidx % 8]
        y = self.interleave(cw)
        sym = self.diff_encode(self.bytes_to_symbols(y))
        return self.const[sym]

    def symbols_per_packet(self):
        return RS_LEN * 8 // self.m


# ---------------------------------------------------------------- pulse shaping
def rrc_taps(sps, span=12, alpha=0.15):
    """Root raised cosine, `span` symbols long, unit energy at the symbol rate."""
    n = np.arange(-span * sps // 2, span * sps // 2 + 1) / sps
    h = np.zeros(n.size)
    for i, t in enumerate(n):
        if abs(t) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(4 * alpha * t) - 1) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                         + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * t * (1 - alpha)) + 4 * alpha * t * np.cos(np.pi * t * (1 + alpha))) / \
                   (np.pi * t * (1 - (4 * alpha * t) ** 2))
    return h / np.sqrt(np.sum(h ** 2))


def _load_fastdsp():
    """Compile (once) and load the C polyphase FIR; None if no compiler."""
    here = os.path.dirname(os.path.abspath(__file__))
    src, lib = os.path.join(here, "fastdsp.c"), os.path.join(here, "_fastdsp.so")
    try:
        if not os.path.exists(lib) or os.path.getmtime(lib) < os.path.getmtime(src):
            subprocess.run(["cc", "-O3", "-march=native", "-ffast-math", "-shared", "-fPIC",
                            "-o", lib, src], check=True)
        dll = ctypes.CDLL(lib)
        f = dll.polyfir
        P = ctypes.POINTER(ctypes.c_float)
        f.argtypes = [P, ctypes.c_int, P, ctypes.c_int, ctypes.c_int, P]
        f.restype = None
        return f
    except (OSError, subprocess.CalledProcessError):
        return None


_POLYFIR = _load_fastdsp()


class PulseShaper:
    """Stateful polyphase interpolator (integer sps) with RRC alpha=0.15."""

    def __init__(self, sps=2, span=12):
        self.sps = sps
        h = rrc_taps(sps, span) * np.sqrt(sps)  # unit output power for unit-power symbols
        L = int(np.ceil(h.size / sps)) * sps
        h = np.concatenate([h, np.zeros(L - h.size)])
        self.phases = h.reshape(-1, sps).T.astype(np.float32)  # (sps, taps_per_phase)
        self.tail = np.zeros(self.phases.shape[1] - 1, dtype=np.complex64)

    def process(self, sym):
        x = np.concatenate([self.tail, sym]).astype(np.complex64)
        self.tail = x[-(self.phases.shape[1] - 1):]
        if _POLYFIR is not None:
            P = ctypes.POINTER(ctypes.c_float)
            out = np.empty(sym.size * self.sps, dtype=np.complex64)
            ph = np.ascontiguousarray(self.phases)
            _POLYFIR(x.ctypes.data_as(P), x.size, ph.ctypes.data_as(P), self.sps,
                     ph.shape[1], out.ctypes.data_as(P))
            return out
        out = np.empty((sym.size, self.sps), dtype=np.complex64)
        for p in range(self.sps):
            hp = self.phases[p]
            out[:, p] = np.convolve(x.real, hp, "valid") + 1j * np.convolve(x.imag, hp, "valid")
        return out.ravel()
