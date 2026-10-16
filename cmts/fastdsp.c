/* SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md */
/* Polyphase complex FIR interpolator, same semantics as numpy.convolve(..., "valid")
 * applied per phase: out[i*sps + p] = sum_k h[p][k] * x[i + ntaps-1 - k] */
void polyfir(const float *x, int nx, const float *h, int sps, int ntaps, float *out)
{
    int nout = nx - ntaps + 1;
    for (int i = 0; i < nout; i++) {
        const float *xs = x + 2 * (i + ntaps - 1);
        for (int p = 0; p < sps; p++) {
            const float *hp = h + p * ntaps;
            float re = 0.f, im = 0.f;
            for (int k = 0; k < ntaps; k++) {
                re += hp[k] * xs[-2 * k];
                im += hp[k] * xs[-2 * k + 1];
            }
            out[2 * (i * sps + p)] = re;
            out[2 * (i * sps + p) + 1] = im;
        }
    }
}

/* Blind (CMA) then decision-directed LMS T-spaced equalizer with a 2nd order PLL.
 * x: complex input (n symbols, unit power), out: equalized+derotated symbols,
 * dec: decided grid index (gi*side + gq), side: QAM grid side (8 or 16),
 * scale: distance between grid points / 2 (= amplitude of point 1). */
#include <math.h>
#include <string.h>
void eq_pll(const float *x, int n, int ntaps, float mu_cma, float mu_dd, int n_cma,
            int side, float scale, float r2, float pll_a, float pll_b,
            float *out, int *dec)
{
    float wr[64], wi[64];
    memset(wr, 0, sizeof wr); memset(wi, 0, sizeof wi);
    int c = ntaps / 2;
    wr[c] = 1.f;
    float ph = 0.f, fr = 0.f;
    for (int k = 0; k + ntaps <= n; k++) {
        const float *xs = x + 2 * k;
        float yr = 0.f, yi = 0.f;
        for (int t = 0; t < ntaps; t++) {   /* y = sum w[t] * x[k+t] */
            float ar = xs[2 * t], ai = xs[2 * t + 1];
            yr += wr[t] * ar - wi[t] * ai;
            yi += wr[t] * ai + wi[t] * ar;
        }
        float cs = cosf(ph), sn = sinf(ph);
        float zr = yr * cs + yi * sn, zi = yi * cs - yr * sn;   /* z = y * e^-j ph */
        /* slicer on the square grid */
        int gi = (int)floorf((zr / scale + side - 1) * 0.5f + 0.5f);
        int gq = (int)floorf((zi / scale + side - 1) * 0.5f + 0.5f);
        if (gi < 0) gi = 0; if (gi >= side) gi = side - 1;
        if (gq < 0) gq = 0; if (gq >= side) gq = side - 1;
        float dr = (2 * gi + 1 - side) * scale, di = (2 * gq + 1 - side) * scale;
        float er, ei;  /* error in the equalizer (non-derotated) domain */
        float mu;
        if (k < n_cma) {
            float m2 = yr * yr + yi * yi;
            er = yr * (m2 - r2); ei = yi * (m2 - r2);
            mu = mu_cma;
        } else {
            float vr = zr - dr, vi = zi - di;          /* rotate back by e^+j ph */
            er = vr * cs - vi * sn; ei = vi * cs + vr * sn;
            mu = mu_dd;
            /* PLL: phase error = arg(z * conj(d)) ~ Im(z conj d)/|d|^2 */
            float pe = (zi * dr - zr * di) / (dr * dr + di * di);
            fr += pll_b * pe;
            ph += fr + pll_a * pe;
        }
        for (int t = 0; t < ntaps; t++) {  /* w -= mu * e * conj(x) */
            float ar = xs[2 * t], ai = xs[2 * t + 1];
            wr[t] -= mu * (er * ar + ei * ai);
            wi[t] -= mu * (ei * ar - er * ai);
        }
        if (ph > 3.14159265f) ph -= 6.2831853f;
        if (ph < -3.14159265f) ph += 6.2831853f;
        out[2 * k] = zr; out[2 * k + 1] = zi;
        dec[k] = gi * side + gq;
    }
}
