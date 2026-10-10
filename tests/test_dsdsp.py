"""C downstream kernels (cmts/dsdsp.c) against the numpy references: J.83A encoder bit-exact over
several consecutive calls (interleaver and rotation state), 64/256-QAM, random and null packets;
mix_add (frequency placement for bonding) against numpy; ChannelSynth (shaping + conjugation +
shift + sum in one pass, AVX2 for sps <= 8) against numpy over consecutive calls; speed."""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cmts import cdsp
from cmts.downstream import DownstreamMux, SYMRATE
from cmts.j83a import J83AEncoder

ok_all = cdsp.DS_AVAILABLE
print(f"{'OK ' if ok_all else 'KO '} bibliothèque C downstream compilée et chargée")
rng = np.random.default_rng(3)


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


for qam in (64, 256):
    c, py = J83AEncoder(qam), J83AEncoder(qam)
    py.use_c = False
    same, nsym = True, 0
    for call in range(12):
        n = 8 * int(rng.integers(1, 9))
        pk = rng.integers(0, 256, (n, 188), dtype=np.uint8)
        pk[:, 0] = 0x47
        nulls = rng.random(n) < 0.4                          # null packets take a shortcut in numpy
        pk[nulls] = 0xFF
        pk[nulls, :4] = [0x47, 0x1F, 0xFF, 0x10]
        a, b = c.encode(pk), py.encode(pk)
        same &= a.shape == b.shape and np.array_equal(a, b)
        nsym += a.size
    check(f"J.83A {qam}-QAM : C = numpy au bit près, 12 appels enchaînés", same and c.rot == py.rot
          and np.array_equal(c.hist, py.hist), f"{nsym} symboles")

# real DOCSIS stream (SYNC, UCD, MAP, null) through both
for qam in (64, 256):
    mux = DownstreamMux(qam=qam)
    pk = mux.packets(2048)
    c, py = J83AEncoder(qam), J83AEncoder(qam)
    py.use_c = False
    check(f"flux DOCSIS réel {qam}-QAM : C = numpy", np.array_equal(c.encode(pk), py.encode(pk)))

# mix_add against numpy, phase carried across calls
x = (rng.standard_normal(50000) + 1j * rng.standard_normal(50000)).astype(np.complex64)
acc = np.zeros(x.size, np.complex64)
ph = np.zeros(1)
step = 2 * np.pi * 8e6 / 27.808e6
cdsp.mix_add(x[:20000], step, ph, acc[:20000])
cdsp.mix_add(x[20000:], step, ph, acc[20000:])
ref = x * np.exp(1j * step * np.arange(x.size))
err = 10 * np.log10(np.mean(np.abs(acc - ref) ** 2) / np.mean(np.abs(ref) ** 2))
check("mix_add (décalage de fréquence, phase continue entre appels)", err < -100, f"erreur {err:.0f} dB")

# ChannelSynth: polyphase RRC + optional conjugation + shift, summed into acc, against numpy
from cmts.j83a import PulseShaper  # noqa: E402
for sps in (2, 3, 4, 5, 6, 8, 10):
    for conj in (False, True):
        ph_taps = PulseShaper(sps).phases
        ntaps = ph_taps.shape[1]
        stp = 2 * np.pi * rng.uniform(-0.45, 0.45) / 1.0
        syn = cdsp.ChannelSynth(ph_taps, stp, conj)
        sizes = (1000, 333, 7, 1, 2048, 129)
        sym = (rng.standard_normal(sum(sizes)) + 1j * rng.standard_normal(sum(sizes))).astype(np.complex64)
        base = (rng.standard_normal(sym.size * sps) + 1j * rng.standard_normal(sym.size * sps)).astype(np.complex64)
        acc_c = base.copy()
        o = 0
        for sz in sizes:
            syn.add(sym[o:o + sz], acc_c[o * sps:(o + sz) * sps])
            o += sz
        x = np.concatenate([np.zeros(ntaps - 1, np.complex64), sym]).astype(np.complex128)
        y = np.empty((sym.size, sps), complex)
        for p_ in range(sps):
            y[:, p_] = np.convolve(x, ph_taps[p_].astype(np.float64), "valid")
        y = y.ravel()
        if conj:
            y = np.conj(y)
        ref = base + y * np.exp(1j * stp * np.arange(y.size))
        err = 20 * np.log10(np.abs(acc_c - ref).max() / np.sqrt(np.mean(np.abs(y) ** 2)))
        check(f"ChannelSynth sps {sps}{' conjugué' if conj else ''} (6 appels enchaînés)", err < -90,
              f"erreur max {err:.0f} dB")

for qam in (64, 256):
    mux = DownstreamMux(qam=qam)
    bits = 6 if qam == 64 else 8
    n = int(SYMRATE * bits / (204 * 8) * 0.5) // 8 * 8
    pk = mux.packets(n)
    enc = J83AEncoder(qam)
    t = time.perf_counter(); enc.encode(pk); tc = (time.perf_counter() - t) * 2
    enc.use_c = False
    t = time.perf_counter(); enc.encode(pk); tp = (time.perf_counter() - t) * 2
    print(f"    {qam}-QAM : codage {tc:.3f} cœur en C, {tp:.3f} en numpy (x{tp / tc:.0f})")
n = 262144                                            # a realistic block, buffers already in memory
acc = np.ones(n, np.complex64)
xx = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
ph = np.zeros(1)
cdsp.mix_add(xx, step, ph, acc)
t = time.perf_counter()
for _ in range(20):
    cdsp.mix_add(xx, step, ph, acc)
tm = (time.perf_counter() - t) / 20 / (n / 27.808e6)
print(f"    mélange d'un canal à 27,8 MS/s : {tm:.3f} cœur en C (numpy, phaseurs précalculés : ~0,12)")
for sps in (5, 8):
    syn = cdsp.ChannelSynth(PulseShaper(sps).phases, 2 * np.pi * 8e6 / (sps * SYMRATE), True)
    sym = (rng.standard_normal(32768) + 1j * rng.standard_normal(32768)).astype(np.complex64)
    acc = np.zeros(sym.size * sps, np.complex64)
    syn.add(sym, acc)
    t = time.perf_counter()
    for _ in range(10):
        syn.add(sym, acc)
    ts = (time.perf_counter() - t) / 10 / (sym.size / SYMRATE)
    print(f"    synthèse d'un canal à {sps} éch./symbole ({sps * SYMRATE / 1e6:.2f} MS/s) : {ts:.3f} cœur")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
