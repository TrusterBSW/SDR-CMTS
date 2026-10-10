"""C kernels of the upstream receiver (cmts/usdsp.c) against the Python references:
Reed-Solomon (T 3..16, any length, 0..T+2 errors), band-limited interpolation, decision-directed
tracking (QPSK and a 64-QAM grid, for the ATDMA step), and their speed."""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cmts import cdsp
from cmts.rs import RSCodec

ok_all = cdsp.AVAILABLE
print(f"{'OK ' if cdsp.AVAILABLE else 'KO '} bibliothèque C compilée et chargée")
rng = np.random.default_rng(7)


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


# ---- Reed-Solomon
same_enc = same_dec = total = 0
for t in (3, 5, 8, 10, 16):
    py = RSCodec(t)
    for _ in range(120):
        k = int(rng.integers(16, 255 - 2 * t))
        msg = bytes(rng.integers(0, 256, k, dtype=np.uint8))
        cw_py, cw_c = py.encode(msg), cdsp.rs_encode(msg, 2 * t)
        same_enc += cw_py == cw_c
        bad = bytearray(cw_py)
        ne = int(rng.integers(0, t + 3))
        for p in rng.choice(len(bad), ne, replace=False):
            bad[p] ^= int(rng.integers(1, 256))
        try:
            r_py = py.decode(bytes(bad))
        except ValueError:
            r_py = None
        try:
            r_c = cdsp.rs_decode(bytes(bad), 2 * t)
        except ValueError:
            r_c = None
        same_dec += r_py == r_c and (ne > t or r_c == (msg, ne))
        total += 1
check("RS : codage C = Python", same_enc == total, f"{same_enc}/{total}")
check("RS : décodage C = Python (0 à T+2 erreurs, T = 3..16)", same_dec == total, f"{same_dec}/{total}")

# ---- interpolation of a band-limited signal (RRC-like spectrum, 2.7 samples per symbol)
n = 4000
f = np.fft.rfftfreq(8 * n)
spec = (rng.standard_normal(f.size) + 1j * rng.standard_normal(f.size)) * (f < 0.23 / 8)
ref = np.fft.irfft(spec, 8 * n)                       # 8x oversampled reference
ref = (ref + 1j * np.roll(ref, 123)) / np.std(ref)
y = ref[::8].astype(np.complex64)                     # the "input rate" samples
t = 100 + np.arange(1000) * 2.7156 + 0.37             # symbol instants in input samples
want = np.interp(t * 8, np.arange(ref.size), ref.real) + 1j * np.interp(t * 8, np.arange(ref.size), ref.imag)
got = cdsp.interp_symbols(y, t[0], 2.7156, t.size)
err_db = 10 * np.log10(np.mean(np.abs(got - want) ** 2) / np.mean(np.abs(want) ** 2))
check("interpolation (16 coefficients) : erreur relative", err_db < -45, f"{err_db:.1f} dB")

# ---- decision-directed tracking
qpsk = np.array([(-1 - 1j), (-1 + 1j), (1 - 1j), (1 + 1j)]) / np.sqrt(2)
g = np.arange(-7, 8, 2)
qam64 = (g[:, None] + 1j * g[None, :]).ravel() / np.sqrt(42)
for name, pts, snr in (("QPSK", qpsk, 20), ("64-QAM", qam64, 32)):
    k = rng.integers(0, pts.size, 6000)
    s = pts[k] * np.exp(1j * (0.3 + 2 * np.pi * 2e-4 * np.arange(k.size)))   # phase + drift
    s += (rng.standard_normal(k.size) + 1j * rng.standard_normal(k.size)) * 10 ** (-snr / 20) / np.sqrt(2)
    out, idx, mse = cdsp.dd_track(s * np.exp(-0.3j), pts)
    ser = np.mean(idx[200:] != k[200:])
    check(f"suivi de phase {name} ({snr} dB, dérive de fréquence)", ser < 1e-3,
          f"erreurs symbole {ser:.1e}, MER {-10 * np.log10(mse):.1f} dB")

# ---- speed (6528 symbols = a 1500-byte burst at 1.28 Msym/s)
r = (qpsk[rng.integers(0, 4, 6528)]).astype(np.complex64)
t0 = time.perf_counter(); cdsp.dd_track(r, qpsk); t_tr = time.perf_counter() - t0
t0 = time.perf_counter(); cdsp.interp_symbols(np.tile(r, 3), 10.3, 2.7156, 6528); t_in = time.perf_counter() - t0
cw = RSCodec(8).encode(bytes(220))
t0 = time.perf_counter()
for _ in range(7):
    cdsp.rs_decode(cw, 16)
t_rs = time.perf_counter() - t0
print(f"    vitesse : suivi {t_tr * 1e3:.2f} ms, interpolation {t_in * 1e3:.2f} ms, 7 blocs RS {t_rs * 1e3:.2f} ms")
# ---- data stage of a burst in C (2026-10-09): same results as the numpy path
from cmts import us_phy
rng2 = np.random.default_rng(7)
for name, P in (("16-QAM", us_phy.QAM16_POINTS), ("64-QAM", us_phy.QAM64_POINTS)):
    P = P.astype(np.complex64)
    idx = rng2.integers(0, P.size, 6000).astype(np.int32)
    r = (P[idx] * np.exp(1j * (0.1 + 3e-4 * np.arange(idx.size))) + 0.03 * (rng2.standard_normal(idx.size)
         + 1j * rng2.standard_normal(idx.size))).astype(np.complex64)
    g = cdsp._GRIDS.pop(P.tobytes(), None)
    cdsp._GRIDS[P.tobytes()] = None                   # exhaustive search, for reference
    o_ref, i_ref, e_ref = cdsp.dd_track(r, P)
    del cdsp._GRIDS[P.tobytes()]
    o_g, i_g, e_g = cdsp.dd_track(r, P)
    # same decisions; the error differs slightly since the grid loop uses a phasor and the
    # small-angle phase error instead of sin / cos / atan2 per symbol
    check(f"{name} : décision sur grille = recherche exhaustive", np.array_equal(i_ref, i_g) and abs(e_ref - e_g) < 0.02 * e_ref,
          f"erreur {e_ref:.5f} / {e_g:.5f}")
    w = (np.eye(1, 11, 5)[0] + 0.05 * rng2.standard_normal(11)).astype(np.complex64)
    o1, i1, _ = cdsp.dd_track(us_phy._eq_apply(r, w), P)
    o2, i2, _ = cdsp.eq_track(r, w, P)
    check(f"{name} : égaliseur + suivi en C = numpy", np.array_equal(i1, i2) and np.max(np.abs(o1 - o2)) < 1e-4)
    pr = us_phy._prbs(0x152, 65536)
    bits = us_phy.scramble(us_phy.index_bits(i_ref, us_phy.constellation(
        type("P", (), {"bps": int(np.log2(P.size))})())[1]), 0x152)
    check(f"{name} : étiquettes -> octets désembrouillés en C = numpy",
          np.array_equal(cdsp.symbols_to_bytes(i_ref, int(np.log2(P.size)), pr), np.packbits(bits[:bits.size // 8 * 8])))
# symbols_to_bytes: every label width, an odd count, a scrambler sequence shorter than the bits
# (not a multiple of 8): the bits beyond it are not XORed
for bps in (2, 4, 6, 8):
    lab = rng2.integers(0, 1 << bps, 1001).astype(np.int32)
    bits = ((lab[:, None] >> np.arange(bps - 1, -1, -1)) & 1).astype(np.uint8).ravel()
    for npr in (0, 37, 4003, 20000):
        pr = rng2.integers(0, 2, npr).astype(np.uint8) if npr else None
        b2 = bits.copy()
        if npr:
            m = min(npr, b2.size)
            b2[:m] ^= pr[:m]
        ref = np.packbits(b2[:b2.size // 8 * 8])
        check(f"étiquettes -> octets, {bps} bits/symbole, embrouilleur de {npr} bits",
              np.array_equal(cdsp.symbols_to_bytes(lab, bps, pr), ref))
msgs = [rng2.integers(0, 256, k).astype(np.uint8).tobytes() for k in (224, 224, 60)]
cwb = np.frombuffer(b"".join(cdsp.rs_encode(m, 16) for m in msgs), np.uint8).copy()
cwb[[3, 250, 300]] ^= 0x5A
out, fixed, bad = cdsp.rs_decode_many(cwb, [224, 224, 60], 16)
check("RS de tous les mots de code en un appel", bad == -1 and fixed == 3 and out.tobytes() == b"".join(msgs))
cwb[240:260] ^= 0xFF                                 # 20 bad bytes in codeword 1 (T = 8)
check("RS en un appel : mot de code non corrigible repéré", cdsp.rs_decode_many(cwb, [224, 224, 60], 16)[2] == 1)
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
