"""DOCSIS TDMA upstream burst PHY (CM-SP-PHYv3.0 6.2, DOCSIS 1.x type-4 burst descriptors).

CM side (used to simulate a modem):
    MAC bytes -> RS(k+2T) blocks (zero-fill) -> scrambler x^15+x^14+1 -> preamble prepend
    -> QPSK0 mapping -> root raised cosine alpha = 0.25
CMTS side (BurstDemod): matched filter, preamble search (arrival time, carrier phase,
frequency offset, power), QPSK slicing with phase tracking, descrambling, RS decoding.

Conventions checked on a real modem (Sagem F@st 3686, 2026-10-08, known-plaintext analysis
of its INIT ranging bursts):
  - each MAC byte enters its RS symbol bit-reversed (MAC bytes are serialized LSB first), the
    RS symbols themselves go MSB first into the scrambler;
  - the scrambler output is delay element 1 (the latest feedback bit): the sequence starts with
    seed bit 0, then the x^15+x^14+1 feedback bits.
The demodulator still checks the HCS on both bit orders and reports which one matched.
"""
from dataclasses import dataclass
import functools

import numpy as np

from . import cdsp
from . import docsis_mac as mac
from .rs import RSCodec

ALPHA = 0.25          # upstream roll-off
SPAN = 8              # RRC half length in symbols
SYMRATE_UNIT = 160e3  # UCD symbol rate unit
UP = 8                # oversampling of the matched filter output for the fine timing search
_BITREV = np.array([int(f"{b:08b}"[::-1], 2) for b in range(256)], dtype=np.uint8)


@dataclass
class BurstProfile:
    iuc: int
    preamble_len: int = 64     # bits
    preamble_off: int = 0      # bits
    fec_t: int = 0
    fec_k: int = 16
    seed: int = 0x152
    max_burst: int = 0
    guard: int = 8             # symbols
    last_cw_short: bool = False
    scrambler: bool = True
    mod_qpsk: bool = True
    diff: bool = False
    atdma: bool = False        # DOCSIS 2.0 burst: QPSK preamble whatever the data modulation
    bits: int = 0              # data bits per symbol (ATDMA: 2, 4, 6); 0 = from mod_qpsk

    @property
    def preamble(self):
        """Preamble symbols. DOCSIS 1.x TDMA modulates the preamble like the data: QPSK, or 16-QAM
        with the burst's own label -> point map (the UCD preamble length is then a multiple of 4
        bits): a 16-QAM burst is not found without the right map (seen on the Sagem, 2026-10-09)."""
        if (self.mod_qpsk or self.atdma) and "_pre" in self.__dict__:
            return self.__dict__["_pre"]                  # cached (rebuilt 4x per burst before)
        bits = np.unpackbits(np.frombuffer(mac.PREAMBLE, dtype=np.uint8))
        bits = bits[self.preamble_off:self.preamble_off + self.preamble_len]
        if self.mod_qpsk or self.atdma:
            self.__dict__["_pre"] = qpsk_map(bits)
            return self.__dict__["_pre"]
        labels = bits.reshape(-1, 4) @ np.array([8, 4, 2, 1])
        return QAM16_POINTS[labels]

    @functools.cached_property
    def rs(self):
        return RSCodec(self.fec_t) if self.fec_t else None

    def codeword_info_lengths(self, nbytes):
        """Information bytes of each codeword needed for `nbytes` of MAC data (minimum burst)."""
        if not self.fec_t:
            return [nbytes]
        k = self.fec_k
        full, rem = divmod(nbytes, k)
        out = [k] * full
        if rem:
            out.append(max(rem, 16) if self.last_cw_short else k)
        return out

    @property
    def bps(self):
        """Bits per data symbol (the preamble is always QPSK)."""
        return self.bits or (2 if self.mod_qpsk else 4)

    def burst_symbols(self, nbytes):
        nb = sum(n + 2 * self.fec_t for n in self.codeword_info_lengths(nbytes))
        return self.preamble.size + -(-nb * 8 // self.bps)


def profiles():
    """IUC -> BurstProfile, exactly as announced in our UCD."""
    out = {}
    for iuc, p in mac.BURST_PROFILES.items():
        out[iuc] = BurstProfile(iuc=iuc, **p)
    return out


# ---------------------------------------------------------------- bits / symbols
def qpsk_map(bits):
    """PHY Figure 6-21: label x2 x1 = (I, Q), x2 first on the wire, 1 -> positive."""
    b = np.asarray(bits, dtype=np.int8).reshape(-1, 2)
    return ((2 * b[:, 0] - 1) + 1j * (2 * b[:, 1] - 1)).astype(np.complex64) / np.sqrt(2)


# Constellations of the upstream receiver, indexed by the symbol label (its bits, MSB first on
# the wire): the decided index IS the label. QPSK: PHY Figure 6-21. 16-QAM (DOCSIS 1.x TDMA,
# data bursts and their preamble): label I1 Q1 I0 Q0, I1/Q1 = sign (1 = positive), I0/Q0 =
# magnitude (1 = outer point), i.e. the quadrant from the first two bits and points mirrored
# from one quadrant to the next. Found on the Sagem 3686's own bursts (2026-10-09, 4/4 DHCP
# DISCOVER decoded, MER 36.5 dB) with tools/find_qam16_map.py.
QPSK_POINTS = qpsk_map(np.array([[b >> 1, b & 1] for b in range(4)]).ravel())


def _qam16_docsis():
    pts = np.empty(16, np.complex64)
    for lab in range(16):
        i = (1 if lab & 8 else -1) * (3 if lab & 2 else 1)
        q = (1 if lab & 4 else -1) * (3 if lab & 1 else 1)
        pts[lab] = complex(i, q) / np.sqrt(10)
    return pts


QAM16_POINTS = _qam16_docsis()


def _qam64_guess():
    """64-QAM label -> point, a first guess extending the 16-QAM structure (I2 Q2 = signs, then
    magnitude bits I1 Q1 I0 Q0 Gray-coded from the centre); to be checked on the modem's bursts."""
    gray = {0b00: 1, 0b01: 3, 0b11: 5, 0b10: 7}
    pts = np.empty(64, np.complex64)
    for lab in range(64):
        si, sq = (lab >> 5) & 1, (lab >> 4) & 1
        mi = ((lab >> 3) & 1) << 1 | ((lab >> 1) & 1)
        mq = ((lab >> 2) & 1) << 1 | (lab & 1)
        pts[lab] = complex((1 if si else -1) * gray[mi], (1 if sq else -1) * gray[mq]) / np.sqrt(42)
    return pts


QAM64_POINTS = _qam64_guess()


def set_qam64_map(points):
    global QAM64_POINTS
    QAM64_POINTS = np.asarray(points, dtype=np.complex64)


def set_qam16_map(points):
    """Install the 16-QAM label -> point table (16 complex values, unit mean power)."""
    global QAM16_POINTS
    QAM16_POINTS = np.asarray(points, dtype=np.complex64)


def constellation(prof):
    """(points indexed by label, bits per symbol) of a burst profile."""
    b = prof.bps
    return {2: (QPSK_POINTS, 2), 4: (QAM16_POINTS, 4), 6: (QAM64_POINTS, 6)}[b]


def index_bits(idx, bps):
    """Decided point indices -> bit array (MSB of each index first)."""
    return ((idx[:, None] >> np.arange(bps - 1, -1, -1)) & 1).astype(np.uint8).ravel()


@functools.lru_cache(maxsize=8)
def _prbs(seed, n):
    """Scrambler sequence (PHY Figure 6-8): delay element i holds seed bit i-1, element 14 XOR
    element 15 is fed back into element 1, and the data are XORed with element 1. The first bit
    is therefore seed bit 0, then the feedback bits (verified on a real modem)."""
    r = seed & 0x7FFF
    out = np.empty(n, dtype=np.uint8)
    for i in range(n):
        out[i] = r & 1
        fb = ((r >> 13) ^ (r >> 14)) & 1
        r = ((r << 1) | fb) & 0x7FFF
    return out


def scramble(bits, seed):
    """Same operation both ways (XOR with the PRBS restarted at each burst)."""
    n = bits.size
    return bits ^ _prbs(seed, max(4096, 1 << int(np.ceil(np.log2(max(n, 1))))))[:n]


EQ_TAPS = 11            # symbol-spaced equalizer length
EQ_TRAIN = 2000         # symbols used to fit it (500: MER 0.5-1.5 dB lower; fits are rare with the cache)
EQ_REFIT = 200          # bursts decoded with a cached equalizer before it is fitted again


def _burst_len(data):
    """Symbols of the burst in `data` (unit-power symbols; the window runs on into noise)."""
    a = np.abs(np.asarray(data)) ** 2
    if a.size < 16:
        return 0
    c = np.concatenate([[0.0], np.cumsum(a, dtype=np.float64)])
    p = (c[16:] - c[:-16]) / 16                    # 16-symbol moving average (cumulative sum)
    on = np.nonzero(p > 0.25)[0]
    return int(on[-1]) + 8 + 1 if on.size else 0


def _eq_apply(data, w):
    h = EQ_TAPS // 2
    padded = np.concatenate([np.zeros(h, np.complex64), np.asarray(data, np.complex64), np.zeros(h, np.complex64)])
    return np.lib.stride_tricks.sliding_window_view(padded, EQ_TAPS) @ w


def _eq_mse(out, idx, pts, n):
    h = EQ_TAPS // 2
    return float(np.mean(np.abs(out[h:n - h] - pts[idx[h:n - h]]) ** 2))


def equalize_fit(data, idx, pts):
    """Decision-directed linear equalizer, fitted by least squares on the burst's own first
    decisions. Without it the bench upstream is ISI-limited: Sagem 3686 at 5.12 Msym/s
    (2026-10-09) gave MER 24.5 dB raw, 32-35 dB once equalized with 11 taps, ending the
    late-codeword failures of long 64-QAM bursts. data: phase-tracked symbols, idx: decisions.
    Returns (symbols, decisions, taps, mse); taps None when the fit does not help."""
    h = EQ_TAPS // 2
    n = min(_burst_len(data), EQ_TRAIN)
    if n < 4 * EQ_TAPS:
        return data, idx, None, None
    rows = np.lib.stride_tricks.sliding_window_view(data[:n], EQ_TAPS)
    ref = pts[idx[h:n - h]]
    w = np.linalg.lstsq(rows, ref, rcond=None)[0].astype(np.complex64)
    out, idx2, _ = (cdsp.eq_track(data, w, pts) if cdsp.AVAILABLE      # residual phase drift
                    else cdsp.dd_track(_eq_apply(data, w), pts))
    before = float(np.mean(np.abs(data[h:n - h] - ref) ** 2))
    after = _eq_mse(out, idx2, pts, n)
    return (out, idx2, w, after) if after < before else (data, idx, None, None)


def estimate_drift(y, ty0, sps, n, fr, pre, pts, nseg=8, span=0.2, steps=9):
    """Symbol clock offset of a long QAM burst (relative: -4e-6 = the modem's symbols come 4 ppm
    early), from the timing that fits each eighth of the burst best. The Sagem 3686 runs ~4 ppm
    off our clock: over a 16 000-symbol burst the timing slips by 0.07 symbol and the MER fell
    from 37 to 16 dB along it (2026-10-09, 64 KB Max Traffic Burst). ~20 ms; None if too short."""
    taus = np.linspace(-span, span, steps)
    errs, L = [], None
    for tau in taus:
        r, d = cdsp.burst_prep(y, ty0 + tau * sps, sps, n, fr, pre, True)
        o, idx, _ = cdsp.dd_track(d, pts)
        if L is None:
            L = _burst_len(o)
            if L < 2000:
                return None
        e = np.abs(o[:L] - pts[idx[:L]]) ** 2
        seg = L // nseg
        errs.append([e[i * seg:(i + 1) * seg].mean() for i in range(nseg)])
    errs = np.array(errs)
    best = []
    for j in range(nseg):
        i = int(np.argmin(errs[:, j]))
        off = 0.0
        if 0 < i < steps - 1:
            a, b, c = errs[i - 1, j], errs[i, j], errs[i + 1, j]
            den = a - 2 * b + c
            off = 0.5 * (a - c) / den if den > 0 else 0.0
        best.append(taus[i] + off * (taus[1] - taus[0]))
    centers = (np.arange(nseg) + 0.5) * (L // nseg)
    return float(np.polyfit(centers, best, 1)[0])


def equalize(data, idx, pts):
    """equalize_fit() for one burst: returns (symbols, decisions)."""
    out, idx2, _, _ = equalize_fit(data, idx, pts)
    return out, idx2


# ---------------------------------------------------------------- modulator (CM side)
def encode_burst(prof, frame):
    """MAC bytes -> complex symbols of one burst (preamble included, no guard)."""
    frame = bytes(frame)
    data = b""
    pos = 0
    for n in prof.codeword_info_lengths(len(frame)):
        block = frame[pos:pos + n].ljust(n, b"\0")   # zero-fill (Multiple Transmit Channel mode off)
        block = bytes(_BITREV[np.frombuffer(block, np.uint8)])   # MAC bytes LSB first
        pos += n
        data += prof.rs.encode(block) if prof.rs else block
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
    if prof.scrambler:
        bits = scramble(bits, prof.seed)
    pts, bps = constellation(prof)
    bits = np.concatenate([bits, np.zeros(-bits.size % bps, np.uint8)])
    labels = bits.reshape(-1, bps) @ (1 << np.arange(bps - 1, -1, -1))
    return np.concatenate([prof.preamble, pts[labels]])


def rrc(t, alpha=ALPHA):
    """Root raised cosine impulse response at times t (in symbols), unit energy."""
    t = np.asarray(t, dtype=float)
    h = np.empty_like(t)
    z = np.abs(t) < 1e-9
    s = np.abs(np.abs(4 * alpha * t) - 1) < 1e-9
    o = ~(z | s)
    to = t[o]
    h[o] = (np.sin(np.pi * to * (1 - alpha)) + 4 * alpha * to * np.cos(np.pi * to * (1 + alpha))) / \
        (np.pi * to * (1 - (4 * alpha * to) ** 2))
    h[z] = 1 - alpha + 4 * alpha / np.pi
    h[s] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                 + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
    return h


def synth(symbols, sps, delay, n_out):
    """Baseband burst: sum_k s_k rrc((n - delay)/sps - k), n = 0..n_out-1 (unit power for
    unit-power symbols). `delay` (samples, fractional) is the center of the first symbol."""
    out = np.zeros(n_out, dtype=np.complex128)
    w = int(np.ceil(SPAN * sps))
    for k, s in enumerate(symbols):
        c = delay + k * sps
        n0, n1 = max(0, int(np.floor(c)) - w), min(n_out, int(np.ceil(c)) + w + 1)
        if n0 < n1:
            n = np.arange(n0, n1)
            out[n0:n1] += s * rrc((n - c) / sps)
    return out


# ---------------------------------------------------------------- demodulator (CMTS side)
class _DecodeError(ValueError):
    def __init__(self, msg, header_ok):
        super().__init__(msg)
        self.header_ok = header_ok


@dataclass
class Burst:
    iuc: int
    t: float              # arrival of the center of the first preamble symbol (window samples)
    rho: float            # normalized preamble correlation (0..1)
    power_db: float       # dBFS over the burst
    freq: float           # carrier offset (Hz)
    mer: float            # dB, data symbols
    frame: bytes = None   # decoded MAC bytes (None if RS / HCS failed)
    info: dict = None     # parse_us_frame() result
    rs_fixed: int = 0
    bitrev: bool = False
    inverted: bool = False
    swapped: bool = False  # decoded with the other data profile than the grant's (detect alt_iuc)
    error: str = ""
    peak: float = 0.0     # max |sample| over the preamble (1.0 = ADC full scale)
    head: str = ""        # first bytes of the first RS codeword when its MAC header fails (debug)


class BurstDemod:
    def __init__(self, fs, symrate):
        self.fs, self.symrate = fs, symrate
        self.sps = fs / symrate
        n = np.arange(-int(np.ceil(SPAN * self.sps)), int(np.ceil(SPAN * self.sps)) + 1)
        self.mf = (rrc(n / self.sps) / self.sps).astype(np.float32)  # y(kT) = s_k
        self.mf_delay = (self.mf.size - 1) // 2
        self.prof = profiles()
        self.invert_first = False
        # Equalizer per modem (detect(eq_key=SID)): the coax channel is static, so the taps
        # fitted on one burst serve the next ones (~2.5 ms saved per long 64-QAM burst),
        # refitted every EQ_REFIT bursts, when the error doubles, or after a decoding failure.
        self.eq = {}
        # Symbol clock offset per modem (relative), measured on a long QAM burst that failed and
        # applied to every burst of that modem (see estimate_drift)
        self.drift = {}
        self._last = None

    def _equalized(self, data_in, pts, eq_key):
        """Phase tracking + equalizer: the cached taps of this modem when they still fit,
        else a fresh fit (kept for the next bursts)."""
        c = self.eq.get(eq_key) if eq_key is not None else None
        if c is not None and c["uses"] < EQ_REFIT:
            out, idx, _ = (cdsp.eq_track(data_in, c["w"], pts) if cdsp.AVAILABLE
                           else cdsp.dd_track(_eq_apply(data_in, c["w"]), pts))
            n = min(_burst_len(out), EQ_TRAIN)
            if n >= 4 * EQ_TAPS and _eq_mse(out, idx, pts, n) <= 2 * c["mse"]:
                c["uses"] += 1
                return out, idx
        data, idx, _ = cdsp.dd_track(data_in, pts)
        out, idx2, w, mse = equalize_fit(data, idx, pts)
        if w is not None and eq_key is not None:
            self.eq[eq_key] = dict(w=w, mse=mse, uses=0)
        return out, idx2

    def matched(self, x):
        return cdsp.fir(x, self.mf)     # "full": y[n] is input time n - mf_delay

    def _metric(self, y, pos, pre, offs):
        """Preamble correlation at integer positions `pos` of y, symbol offsets `offs`.
        Returns (|c1|+|c2|, normalized rho, c1, c2) with c1/c2 the two preamble halves."""
        idx = pos[:, None] + offs[None, :]
        r = y[idx]
        z = r * np.conj(pre)[None, :]
        h = pre.size // 2
        c1, c2 = z[:, :h].sum(1), z[:, h:].sum(1)
        e = (np.abs(r) ** 2).sum(1) + 1e-30
        m = np.abs(c1) + np.abs(c2)
        return m, m ** 2 / (pre.size * e), c1, c2

    def detect(self, x, iuc, t_lo=0.0, t_hi=None, rho_min=0.35, max_frame=64, grant_syms=None, eq_key=None,
               seg=False, alt_iuc=None, retry=True):
        """seg: a Multiple Transmit Channel mode data grant (segment header ON): the burst is a
        segment header and the grant's share of the service flow's byte stream, decoded whole.
        alt_iuc: another profile to try when the grant's own fails (res.swapped then True); tried
        BEFORE the equalizer / clock drift of eq_key are re-estimated: re-estimated from a burst
        read with the wrong profile, the drift came out wrong and spoiled the next bursts of that
        SID too (2026-10-10). retry=False: no re-estimation at all (a partial look at a burst)."""
        self._last = None
        self._seg = seg
        res = self._detect_any(x, iuc, t_lo, t_hi, rho_min, max_frame, grant_syms, eq_key)
        if res is not None and res.frame is None and alt_iuc in self.prof:
            last = self._last
            res2 = self._detect_any(x, alt_iuc, t_lo, t_hi, rho_min, max_frame, grant_syms, eq_key)
            if res2 is not None and res2.frame is not None:
                res2.swapped = True
                return res2
            self._last = last
        if res is None or res.frame is not None or eq_key is None or not retry:
            return res
        retry = self.eq.pop(eq_key, None) is not None             # fresh equalizer fit
        last = self._last
        if last is not None and cdsp.AVAILABLE:                    # long QAM burst: clock offset?
            slope = estimate_drift(**last)
            if slope is not None and abs(slope) > 0.5e-6:
                self.drift[eq_key] = self.drift.get(eq_key, 0.0) + slope
                retry = True
        if retry:
            res = self._detect_any(x, iuc, t_lo, t_hi, rho_min, max_frame, grant_syms, eq_key)
        return res

    def _detect_any(self, x, iuc, t_lo, t_hi, rho_min, max_frame, grant_syms, eq_key):
        """Find and decode the strongest burst of profile `iuc` whose first symbol lies in
        [t_lo, t_hi] (samples of x). Returns a Burst, or None if no preamble is found.
        max_frame: longest MAC frame decoded (bytes); bounds the demodulation work.
        grant_syms: length of the data grant in symbols, when the burst fills it (see _decode)."""
        res = None
        for inv in (self.invert_first, not self.invert_first):
            res = self._detect(np.conj(x) if inv else x, iuc, t_lo, t_hi, rho_min, max_frame, grant_syms, eq_key)
            if res is not None:
                res.inverted = inv
                if res.frame is not None:
                    self.invert_first = inv
                return res
        return res

    def _detect(self, x, iuc, t_lo, t_hi, rho_min, max_frame, grant_syms=None, eq_key=None):
        prof = self.prof[iuc]
        pre = prof.preamble
        sps = self.sps
        y = self.matched(np.asarray(x, dtype=np.complex64))
        t_hi = x.size if t_hi is None else t_hi
        # ---- coarse search at the input rate
        offs = np.round(np.arange(pre.size) * sps).astype(int)
        lo = max(0, int(t_lo) + self.mf_delay - 1)
        hi = min(y.size - offs[-1] - 1, int(np.ceil(t_hi)) + self.mf_delay + 1)
        if hi <= lo:
            return None
        pos = np.arange(lo, hi)
        m, rho, _, _ = self._metric(y, pos, pre, offs)
        i = int(np.argmax(m))
        if rho[i] < rho_min * 0.7:
            return None
        p0 = pos[i]
        # ---- fine search on the oversampled matched filter output
        nsym_max = int(min((y.size - p0) / sps - 2 * SPAN, prof.burst_symbols(max_frame)))
        # (only the preamble is oversampled: the symbols are then interpolated in C)
        a = max(0, p0 - 4)
        b = min(y.size, p0 + int(np.ceil(pre.size * sps)) + 24 + 2 * SPAN)
        yu = cdsp.interp_symbols(y, a, 1.0 / UP, (b - a) * UP)   # yu[n] = y(a + n / UP)
        uoffs = np.round(np.arange(pre.size) * sps * UP).astype(int)
        base = (p0 - a) * UP
        fpos = np.arange(max(1, base - 2 * UP), min(yu.size - uoffs[-1] - 1, base + 2 * UP + 1))
        m, rho, c1, c2 = self._metric(yu, fpos, pre, uoffs)
        j = int(np.argmax(m))
        if rho[j] < rho_min:
            return None
        frac = 0.0
        if 0 < j < m.size - 1:
            d = m[j - 1] - 2 * m[j] + m[j + 1]
            frac = 0.5 * (m[j - 1] - m[j + 1]) / d if d < 0 else 0.0
        i0 = fpos[j]
        t = a + (i0 + frac) / UP - self.mf_delay          # input-sample time of symbol 0
        h = pre.size // 2
        dphi = np.angle(c2[j] * np.conj(c1[j]))
        freq = dphi / (2 * np.pi * h / self.symrate)
        # ---- symbols: band-limited interpolation at the exact symbol instants (C)
        ty0 = a + (i0 + frac) / UP                         # symbol 0 in samples of y
        nsym = int(min(nsym_max, (y.size - 9 - ty0) / sps))
        if nsym <= pre.size:
            return None
        pts, bps = constellation(prof)
        if cdsp.AVAILABLE:                                 # same steps in one C call
            sps_d = sps * (1 + self.drift.get(eq_key, 0.0))    # this modem's symbol clock
            res = cdsp.burst_prep(y, ty0, sps_d, nsym, freq / self.symrate, pre, bps > 2)
            if res is None:
                return None
            r, data_in = res
            if bps > 2 and nsym > 4000:
                self._last = dict(y=y, ty0=ty0, sps=sps_d, n=nsym, fr=freq / self.symrate, pre=pre, pts=pts)
        else:
            k = np.arange(nsym)
            r = cdsp.interp_symbols(y, ty0, sps, nsym)
            r = r * np.exp(-2j * np.pi * freq * k / self.symrate).astype(np.complex64)
            g = np.mean(r[:pre.size] * np.conj(pre))
            r = r / g
            data_in = r[pre.size:]
            if bps > 2:
                # QAM: the QPSK preamble sets gain and phase, the data amplitude is set on the data
                # themselves (unit mean power), ignoring the noise after the end of the burst
                head = data_in[:300]
                sig = head[np.abs(head) > 0.25]
                if sig.size > 16:
                    data_in = data_in / np.sqrt(np.mean(np.abs(sig) ** 2))
        # power over the preamble (raw input samples; unit-power symbols -> burst power)
        s0 = max(0, int(round(t)))
        seg = np.abs(x[s0:s0 + int(pre.size * sps)])
        power = 10 * np.log10(np.mean(seg ** 2) + 1e-20)
        if bps > 2:
            data, idx = self._equalized(data_in, pts, eq_key)
        else:
            data, idx, _ = cdsp.dd_track(data_in, pts)        # decision-directed phase tracking (C)
        dec = pts[idx]
        out = Burst(iuc=iuc, t=t, rho=float(rho[j]), power_db=float(power), freq=float(freq), mer=0.0,
                    peak=float(seg.max()) if seg.size else 0.0)
        if cdsp.AVAILABLE:
            nb = idx.size * bps
            raw = cdsp.symbols_to_bytes(idx, bps, _prbs(prof.seed, max(4096, 1 << int(np.ceil(np.log2(max(nb, 1))))))
                                        if prof.scrambler else None)
        else:
            bits = index_bits(idx, bps)
            if prof.scrambler:
                bits = scramble(bits, prof.seed)
            raw = np.packbits(bits[:bits.size // 8 * 8])
        fill = None
        if grant_syms:                     # data bytes (parity included) of a burst filling its grant
            # rounded: the grant comes from differences of large sample positions (447.9999...)
            fill = int(round(grant_syms - prof.guard - pre.size)) * prof.bps // 8
        self._decode(prof, raw, out, fill)
        if out.info and bps == 2:
            # refine the carrier offset on the whole burst, now that the data symbols are known
            # (QPSK bursts only: the ranging corrections come from them; skipped on long QAM data
            # bursts, where it cost ~0.3 ms for a figure nobody uses)
            nb = min(r.size, prof.burst_symbols(out.info["total"]))
            ref = np.concatenate([pre, dec[:nb - pre.size]])
            z = r[:nb] * np.conj(ref)
            d = nb // 2
            out.freq += float(np.angle(np.sum(z[d:2 * d] * np.conj(z[:d]))) * self.symrate / (2 * np.pi * d))
        nmer = (prof.burst_symbols(out.info["total"]) - pre.size) if out.info else min(data.size, 176)
        nmer = min(nmer, 2000)                         # MER on the first 2000 symbols at most
        e = np.abs(data[:nmer] - dec[:nmer]) ** 2
        out.mer = float(-10 * np.log10(np.mean(e) + 1e-12)) if nmer else 0.0
        return out

    def _decode(self, prof, raw, out, fill=None):
        """raw scrambled-off bytes -> RS decode codeword by codeword -> MAC frame."""
        t = prof.fec_t
        if not t:
            frame = bytes(raw)
            for rev in (True, False):
                fb = bytes(_BITREV[np.frombuffer(frame, np.uint8)]) if rev else frame
                info = mac.parse_us_frame(fb)
                if info:
                    out.frame, out.info, out.bitrev = fb[:info["total"]], info, rev
                    return
            out.error = "HCS"
            return
        layouts = [None]                   # None: codewords sized from the MAC header
        if fill:
            # A modem fills its data grant: full codewords, then the last one shortened AND
            # zero-padded up to the end of the grant (Sagem 3686, 2026-10-09: a 553-byte DHCP
            # DISCOVER in 77 mini-slots = 220 + 220 + 116 information bytes, 3 bytes of padding).
            n2, k = 2 * t, prof.fec_k
            n = -(-fill // (k + n2))
            last = fill - (n - 1) * (k + n2) - n2
            if last >= 16:                 # shortest shortened codeword: 16 information bytes
                layouts.insert(0, [k] * (n - 1) + [min(last, k)])
            elif n > 1:                    # leftover too short for a codeword: unused
                layouts.insert(0, [k] * (n - 1))
        # segment header ON grant: the whole grant as a segment first; a plain MAC frame still
        # tried after (the grants around the REG-RSP-MP that switches the modem to this mode)
        modes = [(lay, False) for lay in layouts]
        if getattr(self, "_seg", False) and fill:
            modes = [(layouts[0], True)] + modes
        best = None                        # (header parsed?, error) of the most telling failure
        for rev in (True, False):          # LSB-first MAC bytes, as the real modem does
            for lay, seg in modes:
                try:
                    frame, info, fixed = self._rs_frame(prof, raw, rev, lay, out, seg)
                    out.frame, out.info = frame, info
                    out.rs_fixed, out.bitrev, out.error = fixed, rev, ""
                    return
                except _DecodeError as e:
                    if best is None or e.header_ok >= best[0]:
                        best = (e.header_ok, f"RS/{e}")
        out.error = best[1] if best else "RS"

    @staticmethod
    def _rs1(prof, cw):
        """One RS codeword -> (message bytes, corrected symbols); ValueError if uncorrectable."""
        cw = np.ascontiguousarray(cw, dtype=np.uint8)
        if cdsp.AVAILABLE:
            msg, fixed, bad = cdsp.rs_decode_many(cw, [cw.size - 2 * prof.fec_t], 2 * prof.fec_t)
            if bad >= 0:
                raise ValueError("uncorrectable")
            return msg.tobytes(), fixed
        return prof.rs.decode(bytes(cw))

    def _rs_frame(self, prof, raw, rev, layout, out, seg=False):
        """RS-decode `raw` with the given codeword information lengths (None: from the header,
        the last codeword tried at its minimum length then up to 8 bytes longer)."""
        t, k = prof.fec_t, prof.fec_k
        flip = (lambda b: bytes(_BITREV[np.frombuffer(b, np.uint8)])) if rev else (lambda b: b)
        if seg:
            # a segment: the grant's full codewords, as many as the modem sent. With nothing more to
            # send, the Sagem ends its burst early with a shortened last codeword (2026-10-10: 78 +
            # 78 + 78 + 63 bytes in a 723-byte grant); idle, it pads the whole grant with 0xFF.
            # Decoding the grant's whole layout lost every such burst (10-15 % of the upstream
            # under a speedtest, MER 44 dB): codewords are decoded one by one, and the first one
            # that fails at full length is tried shortened (it is then the last).
            n2 = 2 * t
            msgs, fixed, pos = [], 0, 0
            for i, n_info in enumerate(layout):
                got = None
                for L in [n_info] + list(range(min(n_info, prof.fec_k) - 1, 15, -1)):
                    if pos + L + n2 > len(raw):
                        continue
                    try:
                        got = self._rs1(prof, raw[pos:pos + L + n2])
                        break
                    except ValueError:
                        if not (msgs or L == n_info):
                            pass
                if got is None:
                    if not msgs:
                        raise _DecodeError("uncorrectable (bloc 0)", False)
                    break                                 # what came before is the segment
                m_, nf = got
                msgs.append(m_)
                fixed += nf
                pos += len(m_) + n2
                if len(m_) < n_info:
                    break                                 # a shortened codeword ends the burst
            data = flip(b"".join(msgs))
            hdr = mac.parse_seg_header(data)
            if hdr is None:
                out.head += f"{'LSB' if rev else 'MSB'}-first {data[:12].hex(' ')} ; "
                raise _DecodeError("HCS segment", False)
            return data, dict(kind="SEG", total=len(data), **hdr), fixed
        if layout is not None and cdsp.AVAILABLE:          # known layout: codewords in C
            msg0, fixed, bad = cdsp.rs_decode_many(raw, layout[:1], 2 * t)
            if bad == 0:
                raise _DecodeError("uncorrectable (bloc 0)", False)
            head = flip(msg0.tobytes())
            info = mac.parse_us_frame(head)
            if info is None:
                out.head += f"{'LSB' if rev else 'MSB'}-first {head[:12].hex(' ')} ; "
                raise _DecodeError("HCS", False)
            # only the codewords the MAC header says hold data: a modem pads a too large grant
            # with whole codewords of zeros, not worth decoding
            need, m = info["total"], 0
            while m < len(layout) and sum(layout[:m]) < need:
                m += 1
            msg = msg0
            if m > 1:
                rest, fx, bad = cdsp.rs_decode_many(raw[layout[0] + 2 * t:], layout[1:m], 2 * t)
                fixed += fx
                if bad >= 0:
                    raise _DecodeError(f"uncorrectable (bloc {bad + 1})", True)
                msg = np.concatenate([msg0, rest])
            data = flip(msg.tobytes())
            if len(data) < info["total"]:
                raise _DecodeError(f"trame de {info['total']} octets, {len(data)} décodés", True)
            frame = data[:info["total"]]
            return frame, mac.parse_us_frame(frame), fixed
        msgs, fixed, pos = [], 0, 0

        def decode(n_info):
            cw = raw[pos:pos + n_info + 2 * t]
            if cw.size < n_info + 2 * t:
                raise _DecodeError("burst truncated", header_ok)
            try:
                return prof.rs.decode(bytes(cw))
            except ValueError as e:
                raise _DecodeError(f"{e} (bloc {len(msgs)})", header_ok)

        header_ok = False
        lens = layout or [k]
        i = 0
        while i < len(lens):
            if layout is None and i == len(lens) - 1 and i > 0:     # last codeword: padding tolerated
                for extra in range(9):
                    try:
                        msg, nfix = decode(min(lens[i] + extra, k))
                        break
                    except _DecodeError:
                        if extra == 8 or lens[i] + extra >= k:
                            raise
            else:
                msg, nfix = decode(lens[i])
            fixed += nfix
            pos += len(msg) + 2 * t
            msgs.append(msg)
            if i == 0:
                head = flip(msg)
                info = mac.parse_us_frame(head)
                if info is None:
                    out.head += f"{'LSB' if rev else 'MSB'}-first {head[:12].hex(' ')} ; "
                    raise _DecodeError("HCS", False)
                header_ok = True
                if layout is None:
                    lens = prof.codeword_info_lengths(info["total"])
            i += 1
        data = flip(b"".join(msgs))
        if len(data) < info["total"]:
            raise _DecodeError(f"trame de {info['total']} octets, {len(data)} décodés", True)
        frame = data[:info["total"]]
        return frame, mac.parse_us_frame(frame), fixed
