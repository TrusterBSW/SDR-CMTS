#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Downstream synthesis of 6 bonded channels: AVX2 (cmts/dsdsp.c, as the radio does it) against
the same computation in OpenCL on the integrated GPU, on identical blocks (64 TS packets per
channel, 256-QAM, 8 samples per symbol, 55.616 MS/s): time per stage, CPU used by the calling
thread, regularity, and the GPU result against the CPU one. OpenCL through ctypes (libOpenCL).

    python3 tools/gpu_bench.py                  # from the sources
    python3 tools/gpu_bench.py /usr/lib/sdr-cmts   # the installed package's code

i5-9500 / UHD Graphics 630 (2026-11-01): AVX2 0.80 core for the 6 channels (synthesis + scaling),
GPU 0.62 ms of computing per 1.88 ms block, 0.32 core left on the CPU (preparing and copying).
An experiment: the CMTS does not use the GPU (README, roadmap)."""
import ctypes as C
import sys
import time

import numpy as np

import os  # noqa: E402
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cmts import cdsp  # noqa: E402
from cmts.j83a import J83AEncoder, PulseShaper  # noqa: E402

NCH, SPS, BLOCK, QAM = 6, 8, 64, 256
FS = 6.952e6 * SPS
OFFS = [-20e6, -12e6, -4e6, 4e6, 12e6, 20e6]
STEPS = [2 * np.pi * f / FS for f in OFFS]
SCALE = 0.3 / np.sqrt(NCH)
NBLK = 400
rng = np.random.default_rng(1)


def ts_blocks(n):
    out = []
    for _ in range(n):
        b = rng.integers(0, 256, (NCH, BLOCK, 188), dtype=np.uint8)
        b[:, :, 0] = 0x47
        out.append(b)
    return out


blocks = ts_blocks(NBLK)
taps = PulseShaper(SPS).phases                 # (sps, T)
T = taps.shape[1]

# ---------------------------------------------------------------- CPU (AVX2), the radio's path
encs = [J83AEncoder(QAM) for _ in range(NCH)]
synths = [cdsp.ChannelSynth(taps, STEPS[k], True) for k in range(NCH)]
t_enc = t_syn = t_clip = 0.0
cpu0 = time.thread_time()
syms_all, out_cpu = [], []
for b in blocks:
    t0 = time.perf_counter()
    syms = [encs[k].encode(b[k]) for k in range(NCH)]
    t1 = time.perf_counter()
    x = np.zeros(syms[0].size * SPS, np.complex64)
    for k in range(NCH):
        synths[k].add(syms[k], x)
    t2 = time.perf_counter()
    cdsp.scale_clip(x, SCALE)
    t3 = time.perf_counter()
    t_enc += t1 - t0
    t_syn += t2 - t1
    t_clip += t3 - t2
    syms_all.append(syms)
    out_cpu.append(x)
cpu_used = time.thread_time() - cpu0
blk_s = BLOCK * 204 * 8 / 8 / 6.952e6
print(f"bloc : {syms_all[0][0].size} symboles x {NCH} canaux -> {out_cpu[0].size} échantillons ({blk_s * 1e3:.2f} ms d'antenne)")
print(f"CPU AVX2, par bloc : encodage J.83 {t_enc / NBLK * 1e3:.3f} ms, synthèse {t_syn / NBLK * 1e3:.3f} ms, "
      f"mise à l'échelle {t_clip / NBLK * 1e3:.3f} ms")
print(f"  soit en cœurs : encodage {t_enc / NBLK / blk_s:.3f}, synthèse {t_syn / NBLK / blk_s:.3f}, "
      f"mise à l'échelle {t_clip / NBLK / blk_s:.3f} ; total {cpu_used / NBLK / blk_s:.3f}")

# ---------------------------------------------------------------- OpenCL
cl = C.CDLL("libOpenCL.so.1")
vp, sz, u32, i32, u64 = C.c_void_p, C.c_size_t, C.c_uint32, C.c_int32, C.c_uint64
for name, res, args in (
        ("clGetPlatformIDs", i32, [u32, C.POINTER(vp), C.POINTER(u32)]),
        ("clGetDeviceIDs", i32, [vp, u64, u32, C.POINTER(vp), C.POINTER(u32)]),
        ("clGetDeviceInfo", i32, [vp, u32, sz, vp, C.POINTER(sz)]),
        ("clCreateContext", vp, [vp, u32, C.POINTER(vp), vp, vp, C.POINTER(i32)]),
        ("clCreateCommandQueue", vp, [vp, vp, u64, C.POINTER(i32)]),
        ("clCreateProgramWithSource", vp, [vp, u32, C.POINTER(C.c_char_p), C.POINTER(sz), C.POINTER(i32)]),
        ("clBuildProgram", i32, [vp, u32, C.POINTER(vp), C.c_char_p, vp, vp]),
        ("clGetProgramBuildInfo", i32, [vp, vp, u32, sz, vp, C.POINTER(sz)]),
        ("clCreateKernel", vp, [vp, C.c_char_p, C.POINTER(i32)]),
        ("clCreateBuffer", vp, [vp, u64, sz, vp, C.POINTER(i32)]),
        ("clSetKernelArg", i32, [vp, u32, sz, vp]),
        ("clEnqueueWriteBuffer", i32, [vp, vp, u32, sz, sz, vp, u32, vp, vp]),
        ("clEnqueueReadBuffer", i32, [vp, vp, u32, sz, sz, vp, u32, vp, vp]),
        ("clEnqueueNDRangeKernel", i32, [vp, vp, u32, C.POINTER(sz), C.POINTER(sz), C.POINTER(sz), u32, vp, vp]),
        ("clFinish", i32, [vp]),
        ("clFlush", i32, [vp]),
        ("clGetEventInfo", i32, [vp, u32, sz, vp, C.POINTER(sz)]),
        ("clGetEventProfilingInfo", i32, [vp, u32, sz, vp, C.POINTER(sz)]),
        ("clReleaseEvent", i32, [vp])):
    f = getattr(cl, name)
    f.restype, f.argtypes = res, args


def ok(r, what):
    if r != 0:
        raise RuntimeError(f"{what} : erreur OpenCL {r}")


n_pl = u32()
ok(cl.clGetPlatformIDs(0, None, C.byref(n_pl)), "plateformes")
pls = (vp * n_pl.value)()
ok(cl.clGetPlatformIDs(n_pl.value, pls, None), "plateformes")
dev = vp()
for pl in pls:
    if cl.clGetDeviceIDs(pl, 1 << 2, 1, C.byref(dev), None) == 0:
        break
name = C.create_string_buffer(256)
cl.clGetDeviceInfo(dev, 0x102B, 256, name, None)
cu, mhz = u32(), u32()
cl.clGetDeviceInfo(dev, 0x1002, 4, C.byref(cu), None)
cl.clGetDeviceInfo(dev, 0x100C, 4, C.byref(mhz), None)
print(f"GPU : {name.value.decode()} ({cu.value} unités, {mhz.value} MHz)")
err = i32()
ctx = cl.clCreateContext(None, 1, C.byref(dev), None, None, C.byref(err)); ok(err.value, "contexte")
q = cl.clCreateCommandQueue(ctx, dev, 1 << 1, C.byref(err)); ok(err.value, "file")

SRC = b"""
__kernel void synth(__global const float2 *x, __global const float *h, __global const float2 *base,
                    __global const float2 *tab, const int n, const int T, const int sps, const int nch,
                    const int nchunk, const float conj, const float scale, __global float2 *out)
{
    int i = get_global_id(0);                    /* one symbol: its 8 output samples */
    float2 acc[8];
    for (int p = 0; p < 8; p++) acc[p] = (float2)(0.f, 0.f);
    for (int c = 0; c < nch; c++) {
        __global const float2 *xc = x + c * (n + T - 1) + i + T - 1;
        float2 s[8];
        for (int p = 0; p < 8; p++) s[p] = (float2)(0.f, 0.f);
        for (int k = 0; k < T; k++) {
            float2 v = xc[-k];
            for (int p = 0; p < 8; p++) s[p] += h[p * T + k] * v;
        }
        for (int p = 0; p < 8; p++) {
            int m = i * 8 + p;
            float2 z1 = base[c * nchunk + (m >> 10)], z2 = tab[c * 1024 + (m & 1023)];
            float2 ph = (float2)(z1.x * z2.x - z1.y * z2.y, z1.x * z2.y + z1.y * z2.x);
            float2 v = (float2)(s[p].x, s[p].y * conj);
            acc[p] += (float2)(v.x * ph.x - v.y * ph.y, v.x * ph.y + v.y * ph.x);
        }
    }
    for (int p = 0; p < 8; p++) out[i * 8 + p] = clamp(acc[p] * scale, -1.f, 1.f);
}
"""
src = C.c_char_p(SRC)
prog = cl.clCreateProgramWithSource(ctx, 1, C.byref(src), None, C.byref(err)); ok(err.value, "programme")
if cl.clBuildProgram(prog, 1, C.byref(dev), b"-cl-fast-relaxed-math", None, None) != 0:
    log = C.create_string_buffer(65536)
    cl.clGetProgramBuildInfo(prog, dev, 0x1183, 65536, log, None)
    sys.exit("compilation : " + log.value.decode())
kern = cl.clCreateKernel(prog, b"synth", C.byref(err)); ok(err.value, "noyau")

n = syms_all[0][0].size
M = n * SPS
nchunk = (M + 1023) // 1024
h = np.ascontiguousarray(taps, np.float32)
tab = np.stack([np.exp(1j * STEPS[c] * np.arange(1024)) for c in range(NCH)]).astype(np.complex64)
xb = np.zeros((NCH, n + T - 1), np.complex64)
outg = np.zeros(M, np.complex64)


def buf(flags, size):
    b = cl.clCreateBuffer(ctx, flags, size, None, C.byref(err)); ok(err.value, "tampon")
    return b


RO, WO = 1 << 2, 1 << 1
b_x, b_h, b_base, b_tab, b_out = (buf(RO, xb.nbytes), buf(RO, h.nbytes), buf(RO, NCH * nchunk * 8),
                                  buf(RO, tab.nbytes), buf(WO, outg.nbytes))
ok(cl.clEnqueueWriteBuffer(q, b_h, 1, 0, h.nbytes, h.ctypes.data, 0, None, None), "écriture h")
ok(cl.clEnqueueWriteBuffer(q, b_tab, 1, 0, tab.nbytes, tab.ctypes.data, 0, None, None), "écriture tab")
args = [(C.sizeof(vp), C.byref(vp(b))) for b in (b_x, b_h, b_base, b_tab)] + \
       [(4, C.byref(i32(v))) for v in (n, T, SPS, NCH, nchunk)] + \
       [(4, C.byref(C.c_float(-1.0))), (4, C.byref(C.c_float(SCALE))), (C.sizeof(vp), C.byref(vp(b_out)))]
for k, (s, v) in enumerate(args):
    ok(cl.clSetKernelArg(kern, k, s, v), f"argument {k}")
gsize = (sz * 1)(n)

# the same symbols as the CPU run, the channel tails and phases carried block to block
tails = np.zeros((NCH, T - 1), np.complex64)
phase = np.zeros(NCH)
lat, cpu_blk, err_max, pow_ref, kt = [], [], 0.0, 0.0, []
cpu0 = time.thread_time()
for bi, syms in enumerate(syms_all):
    c0, t0 = time.thread_time(), time.perf_counter()
    for c in range(NCH):
        xb[c, :T - 1] = tails[c]
        xb[c, T - 1:] = syms[c]
        tails[c] = syms[c][-(T - 1):]
    base = np.exp(1j * (phase[:, None] + np.arange(nchunk)[None, :] * 1024 * np.array(STEPS)[:, None])).astype(np.complex64)
    phase = np.mod(phase + M * np.array(STEPS), 2 * np.pi)
    ok(cl.clEnqueueWriteBuffer(q, b_x, 0, 0, xb.nbytes, xb.ctypes.data, 0, None, None), "écriture x")
    ok(cl.clEnqueueWriteBuffer(q, b_base, 0, 0, base.nbytes, base.ctypes.data, 0, None, None), "écriture base")
    ev_k, ev_r = vp(), vp()
    ok(cl.clEnqueueNDRangeKernel(q, kern, 1, None, gsize, None, 0, None, C.byref(ev_k)), "noyau")
    ok(cl.clEnqueueReadBuffer(q, b_out, 0, 0, outg.nbytes, outg.ctypes.data, 0, None, C.byref(ev_r)), "lecture")
    cl.clFlush(q)
    st = i32(1)
    while True:                                   # sleeping wait: CL_EVENT_COMMAND_EXECUTION_STATUS
        cl.clGetEventInfo(ev_r, 0x11D3, 4, C.byref(st), None)
        if st.value <= 0:
            break
        time.sleep(0.0002)
    a0, a1 = u64(), u64()
    cl.clGetEventProfilingInfo(ev_k, 0x1282, 8, C.byref(a0), None)
    cl.clGetEventProfilingInfo(ev_k, 0x1283, 8, C.byref(a1), None)
    kt.append((a1.value - a0.value) / 1e9)
    cl.clReleaseEvent(ev_k); cl.clReleaseEvent(ev_r)
    lat.append(time.perf_counter() - t0)
    cpu_blk.append(time.thread_time() - c0)
    if bi >= 2:
        d = np.abs(outg - out_cpu[bi])
        err_max = max(err_max, float(d.max()))
        pow_ref = float(np.mean(np.abs(out_cpu[bi]) ** 2))
        mse = float(np.mean(d ** 2))
lat = np.array(lat[5:]) * 1e3
cpu_blk = np.array(cpu_blk[5:]) * 1e3
print(f"GPU, par bloc (envoi + calcul + retour) : médiane {np.median(lat):.3f} ms, p99 {np.percentile(lat, 99):.3f} ms, "
      f"max {lat.max():.3f} ms")
print(f"  CPU du fil appelant : {np.median(cpu_blk):.3f} ms par bloc = {np.median(cpu_blk) / (blk_s * 1e3):.3f} cœur "
      f"(contre {t_syn / NBLK / blk_s + t_clip / NBLK / blk_s:.3f} cœur pour synthèse + mise à l'échelle en AVX2)")
print(f"  écart GPU / CPU : max {err_max:.2e}, rapport signal / écart {10 * np.log10(pow_ref / max(mse, 1e-30)):.1f} dB")
kt = np.array(kt[5:]) * 1e3
print(f"  calcul seul sur le GPU : médiane {np.median(kt):.3f} ms, max {kt.max():.3f} ms = {np.median(kt) / (blk_s * 1e3) * 100:.0f} % du temps d'antenne")
print(f"  bloc complet : {np.median(lat) / (blk_s * 1e3) * 100:.0f} % du temps d'antenne")
