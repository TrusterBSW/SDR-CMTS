"""Reed-Solomon over GF(256), p(x) = x^8+x^4+x^3+x^2+1, generator roots
alpha^fcr .. alpha^(fcr+2T-1). DOCSIS upstream uses fcr = 0 (PHYv3.0 6.2.4.1).
Shortened codes are handled implicitly (leading zeros are never transmitted)."""
import numpy as np

from . import cdsp

_C = cdsp if cdsp.AVAILABLE else None
PRIM = 0x11D
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= PRIM
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def div(a, b):
    if b == 0:
        raise ZeroDivisionError
    return 0 if a == 0 else EXP[(LOG[a] - LOG[b]) % 255]


def poly_eval(p, x):
    """p[0] is the highest degree coefficient."""
    y = 0
    for c in p:
        y = mul(y, x) ^ c
    return y


class RSCodec:
    def __init__(self, t, fcr=0):
        self.t = t
        self.nsym = 2 * t
        self.fcr = fcr
        g = [1]
        for i in range(self.nsym):
            r = EXP[fcr + i]
            ng = [0] * (len(g) + 1)
            for j, c in enumerate(g):
                ng[j] ^= c
                ng[j + 1] ^= mul(c, r)
            g = ng
        self.gen = g

    def encode(self, msg):
        """msg bytes -> msg + 2T parity bytes."""
        if _C is not None:
            return _C.rs_encode(msg, self.nsym, self.fcr)
        reg = [0] * self.nsym
        for b in msg:
            fb = b ^ reg[0]
            reg = reg[1:] + [0]
            if fb:
                for j in range(self.nsym):
                    reg[j] ^= mul(fb, self.gen[j + 1])
        return bytes(msg) + bytes(reg)

    def syndromes(self, cw):
        return [poly_eval(cw, EXP[self.fcr + i]) for i in range(self.nsym)]

    def decode(self, cw):
        """Returns (message bytes, number of corrected bytes); raises ValueError if uncorrectable.
        Uses the C decoder (cmts/usdsp.c, same results, ~60x faster, GIL released) if built."""
        if _C is not None:
            return _C.rs_decode(cw, self.nsym, self.fcr)
        return self.decode_py(cw)

    def decode_py(self, cw):
        """Pure Python reference decoder."""
        cw = list(cw)
        n = len(cw)
        S = self.syndromes(cw)
        if not any(S):
            return bytes(cw[:n - self.nsym]), 0
        # Berlekamp-Massey -> error locator sigma (lowest degree first)
        C, B = [1], [1]
        L, m, b = 0, 1, 1
        for k in range(self.nsym):
            d = S[k]
            for i in range(1, L + 1):
                if i < len(C):
                    d ^= mul(C[i], S[k - i])
            if d == 0:
                m += 1
                continue
            T = C[:]
            coef = div(d, b)
            C = C + [0] * (len(B) + m - len(C))
            for i, bb in enumerate(B):
                C[i + m] ^= mul(coef, bb)
            if 2 * L <= k:
                L, B, b, m = k + 1 - L, T, d, 1
            else:
                m += 1
        sigma = C[:L + 1]
        if L > self.t:
            raise ValueError("too many errors")
        # Chien search: position j (from the end, degree j) is in error if sigma(alpha^-j) = 0
        err_pos = []
        for j in range(n):
            xinv = EXP[(255 - j) % 255]
            v = 0
            for i, c in enumerate(sigma):
                v ^= mul(c, EXP[(LOG[xinv] * i) % 255]) if c and i else (c if i == 0 else 0)
            if v == 0:
                err_pos.append(j)
        if len(err_pos) != L:
            raise ValueError("uncorrectable (locator roots %d != %d)" % (len(err_pos), L))
        # Forney: omega = S(x) * sigma(x) mod x^2T
        omega = [0] * self.nsym
        for i in range(self.nsym):
            for j, c in enumerate(sigma):
                if i - j >= 0:
                    omega[i] ^= mul(c, S[i - j])
        for j in err_pos:
            X = EXP[j]
            Xinv = EXP[(255 - j) % 255]
            num = 0
            for i, o in enumerate(omega):
                num ^= mul(o, EXP[(LOG[Xinv] * i) % 255]) if o else 0
            den = 0  # sigma'(Xinv): odd terms
            for i in range(1, len(sigma), 2):
                den ^= mul(sigma[i], EXP[(LOG[Xinv] * (i - 1)) % 255]) if sigma[i] else 0
            if den == 0:
                raise ValueError("uncorrectable (forney)")
            e = mul(div(num, den), EXP[(j * (1 - self.fcr)) % 255])
            cw[n - 1 - j] ^= e
        if any(self.syndromes(cw)):
            raise ValueError("uncorrectable (residual syndrome)")
        return bytes(cw[:n - self.nsym]), len(err_pos)
