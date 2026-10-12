/* Upstream burst receiver kernels (CMTS side), called through ctypes (the GIL is released
 * during each call, so decoding a burst no longer stalls the downstream feed).
 *
 * Written to stay valid for the planned upstream evolutions:
 *   - symbol rates 0.16 .. 5.12 Msym/s: interp_symbols works at any samples-per-symbol;
 *   - QPSK today, 8..64-QAM (DOCSIS 2.0 ATDMA) later: dd_track takes any constellation table;
 *   - RS T = 0..16 (ATDMA), any codeword length, any first consecutive root: rs_decode.
 * Every function is stateless and reentrant (tables are built once at load), so several
 * upstream channels can be decoded in parallel threads.
 */
#include <math.h>
#ifdef __AVX2__
#include <immintrin.h>
#endif
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ GF(256), p = 0x11D */
static uint8_t GF_EXP[512];
static uint8_t GF_LOG[256];

static inline uint8_t gmul(uint8_t a, uint8_t b)
{
    return (a && b) ? GF_EXP[GF_LOG[a] + GF_LOG[b]] : 0;
}

static inline uint8_t gdiv(uint8_t a, uint8_t b)   /* b != 0 */
{
    return a ? GF_EXP[(GF_LOG[a] + 255 - GF_LOG[b]) % 255] : 0;
}

static inline uint8_t gpow(int e)                  /* alpha^e, any integer e */
{
    e %= 255;
    if (e < 0) e += 255;
    return GF_EXP[e];
}

static void rmul_init(void);

/* ------------------------------------------------------------------ windowed-sinc table */
#define INTERP_HALF 8                 /* 16 taps */
#define INTERP_PHASES 1024
static float INTERP[INTERP_PHASES + 1][2 * INTERP_HALF];
static float INTERP2[INTERP_PHASES + 1][4 * INTERP_HALF];   /* each tap twice (re, im): 32 floats */
static uint8_t RMUL[64][256];        /* RMUL[i][y] = y * alpha^i: syndromes by table lookup */

__attribute__((constructor)) static void usdsp_init(void)
{
    int x = 1;
    for (int i = 0; i < 255; i++) {
        GF_EXP[i] = (uint8_t)x;
        GF_LOG[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100) x ^= 0x11D;
    }
    for (int i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255];
    /* Blackman-Harris windowed sinc, normalised to unit DC gain for every phase */
    for (int p = 0; p <= INTERP_PHASES; p++) {
        double mu = (double)p / INTERP_PHASES, sum = 0;
        for (int k = 0; k < 2 * INTERP_HALF; k++) {
            double d = (k - (INTERP_HALF - 1)) - mu;           /* tap distance in samples */
            double s = fabs(d) < 1e-12 ? 1.0 : sin(M_PI * d) / (M_PI * d);
            double w = (d + INTERP_HALF) / (2.0 * INTERP_HALF); /* 0..1 over the window */
            double win = 0.35875 - 0.48829 * cos(2 * M_PI * w) + 0.14128 * cos(4 * M_PI * w)
                         - 0.01168 * cos(6 * M_PI * w);
            INTERP[p][k] = (float)(s * win);
            sum += s * win;
        }
        for (int k = 0; k < 2 * INTERP_HALF; k++) {
            INTERP[p][k] /= (float)sum;
            INTERP2[p][2 * k] = INTERP2[p][2 * k + 1] = INTERP[p][k];
        }
    }
    rmul_init();                      /* after the GF tables it uses */
}

/* ------------------------------------------------------------------ Reed-Solomon decoder
 * cw: n bytes (information then nsym parity), corrected in place. Generator roots
 * alpha^fcr .. alpha^(fcr+nsym-1) (DOCSIS upstream: fcr = 0). Shortened codewords are just
 * shorter n. Returns the number of corrected bytes, or
 *   -1 more errors than nsym/2, -2 locator roots != degree, -3 Forney failure,
 *   -4 residual syndrome, -5 bad arguments. */
static void rmul_init(void)
{
    for (int i = 0; i < 64; i++)
        for (int y = 0; y < 256; y++)
            RMUL[i][y] = gmul((uint8_t)y, gpow(i));
}

int rs_decode(uint8_t *cw, int n, int nsym, int fcr)
{
    if (nsym <= 0 || nsym > 64 || n <= nsym || n > 255) return -5;
    uint8_t S[64];
    int any = 0;
    if (fcr >= 0 && fcr + nsym <= 64) {
        /* Horner (cw[0] = highest degree), one table lookup per byte and syndrome; the nsym
         * syndromes in the same pass over the bytes: independent chains the CPU overlaps
         * (one syndrome after the other was a single dependent chain each, ~6.4 us per
         * 240-byte codeword, 9 % of an upstream process, 2026-10-12) */
        uint8_t y[64];
        memset(y, 0, (size_t)nsym);
        for (int j = 0; j < n; j++) {
            uint8_t b = cw[j];
            for (int i = 0; i < nsym; i++) y[i] = RMUL[fcr + i][y[i]] ^ b;
        }
        for (int i = 0; i < nsym; i++) { S[i] = y[i]; any |= y[i]; }
    } else {
        for (int i = 0; i < nsym; i++) {
            uint8_t r = gpow(fcr + i), y = 0;
            for (int j = 0; j < n; j++) y = gmul(y, r) ^ cw[j];
            S[i] = y;
            any |= y;
        }
    }
    if (!any) return 0;
    /* Berlekamp-Massey: C = error locator, lowest degree first */
    uint8_t C[65] = {1}, B[65] = {1}, T[65];
    int L = 0, m = 1;
    uint8_t b = 1;
    for (int k = 0; k < nsym; k++) {
        uint8_t d = S[k];
        for (int i = 1; i <= L; i++) d ^= gmul(C[i], S[k - i]);
        if (!d) { m++; continue; }
        memcpy(T, C, sizeof C);
        uint8_t coef = gdiv(d, b);
        for (int i = 0; i + m <= nsym; i++) C[i + m] ^= gmul(coef, B[i]);
        if (2 * L <= k) { L = k + 1 - L; memcpy(B, T, sizeof B); b = d; m = 1; }
        else m++;
    }
    if (L > nsym / 2) return -1;
    /* Chien search: degree j (counted from the end) is in error if C(alpha^-j) = 0 */
    int pos[64], npos = 0;
    for (int j = 0; j < n; j++) {
        uint8_t v = 0;
        for (int i = 0; i <= L; i++) if (C[i]) v ^= gmul(C[i], gpow(-j * i));
        if (!v) {
            if (npos == 64) return -2;
            pos[npos++] = j;
        }
    }
    if (npos != L) return -2;
    /* Forney: Omega = S(x) C(x) mod x^nsym */
    uint8_t O[64];
    for (int i = 0; i < nsym; i++) {
        uint8_t o = 0;
        for (int j = 0; j <= L && j <= i; j++) o ^= gmul(C[j], S[i - j]);
        O[i] = o;
    }
    for (int q = 0; q < npos; q++) {
        int j = pos[q];
        uint8_t num = 0, den = 0;
        for (int i = 0; i < nsym; i++) if (O[i]) num ^= gmul(O[i], gpow(-j * i));
        for (int i = 1; i <= L; i += 2) if (C[i]) den ^= gmul(C[i], gpow(-j * (i - 1)));
        if (!den) return -3;
        cw[n - 1 - j] ^= gmul(gdiv(num, den), gpow(j * (1 - fcr)));
    }
    for (int i = 0; i < nsym; i++) {                 /* the result must be a codeword */
        uint8_t r = gpow(fcr + i), y = 0;
        for (int j = 0; j < n; j++) y = gmul(y, r) ^ cw[j];
        if (y) return -4;
    }
    return npos;
}

/* RS encoder (simulated modems, tests): parity[nsym] for msg[k]. */
void rs_encode(const uint8_t *msg, int k, int nsym, int fcr, uint8_t *parity)
{
    uint8_t g[65] = {1};
    int deg = 0;
    for (int i = 0; i < nsym; i++) {                 /* g(x) = prod (x - alpha^(fcr+i)) */
        uint8_t r = gpow(fcr + i);
        for (int j = deg + 1; j > 0; j--) g[j] ^= gmul(g[j - 1], r);
        deg++;
    }
    /* g[0] = 1 is the x^nsym coefficient after the loop above (highest first) */
    memset(parity, 0, nsym);
    for (int i = 0; i < k; i++) {
        uint8_t fb = msg[i] ^ parity[0];
        memmove(parity, parity + 1, nsym - 1);
        parity[nsym - 1] = 0;
        if (fb) for (int j = 0; j < nsym; j++) parity[j] ^= gmul(fb, g[j + 1]);
    }
}

/* ------------------------------------------------------------------ symbol sampling
 * y: complex input (interleaved float), ny samples. out[k] = y(t0 + k*step), band-limited
 * interpolation (16-tap windowed sinc, 1024 phases); 0 outside the input. */
void interp_symbols(const float *y, int ny, double t0, double step, int n, float *out)
{
    for (int k = 0; k < n; k++) {
        double t = t0 + k * step;
        double fl = floor(t);
        int i = (int)fl;
        int ph = (int)((t - fl) * INTERP_PHASES + 0.5);
        int first = i - (INTERP_HALF - 1);
#ifdef __AVX2__
        if (first >= 0 && first + 2 * INTERP_HALF <= ny) {
            /* the 16 complex samples and the doubled taps: four 8-wide FMAs, two chains (2026-10-10) */
            const float *b = y + 2 * first, *h2 = INTERP2[ph];
            __m256 a0 = _mm256_mul_ps(_mm256_loadu_ps(h2), _mm256_loadu_ps(b));
            __m256 a1 = _mm256_mul_ps(_mm256_loadu_ps(h2 + 8), _mm256_loadu_ps(b + 8));
            a0 = _mm256_fmadd_ps(_mm256_loadu_ps(h2 + 16), _mm256_loadu_ps(b + 16), a0);
            a1 = _mm256_fmadd_ps(_mm256_loadu_ps(h2 + 24), _mm256_loadu_ps(b + 24), a1);
            __m256 t8 = _mm256_add_ps(a0, a1);
            __m128 t4 = _mm_add_ps(_mm256_castps256_ps128(t8), _mm256_extractf128_ps(t8, 1));
            t4 = _mm_add_ps(t4, _mm_movehl_ps(t4, t4));
            out[2 * k] = _mm_cvtss_f32(t4);
            out[2 * k + 1] = _mm_cvtss_f32(_mm_shuffle_ps(t4, t4, 1));
            continue;
        }
#endif
        const float *h = INTERP[ph];
        float re = 0.f, im = 0.f;
        for (int q = 0; q < 2 * INTERP_HALF; q++) {
            int s = first + q;
            if (s < 0 || s >= ny) continue;
            re += h[q] * y[2 * s];
            im += h[q] * y[2 * s + 1];
        }
        out[2 * k] = re;
        out[2 * k + 1] = im;
    }
}

/* ------------------------------------------------------------------ decision-directed tracking
 * in: n symbols (gain and coarse frequency already removed). Constellation: m points
 * (interleaved re/im), any shape. 2nd-order PLL (phase gain ka, frequency gain kb), nearest
 * point decision. out: derotated symbols, idx: decided point, returns the mean |error|^2. */
float dd_track(const float *in, int n, const float *pts, int m, float ka, float kb,
               float *out, int *idx)
{
    float ph = 0.f, fr = 0.f;
    double err = 0.0;
    for (int k = 0; k < n; k++) {
        float cs = cosf(ph), sn = sinf(ph);
        float xr = in[2 * k], xi = in[2 * k + 1];
        float zr = xr * cs + xi * sn, zi = xi * cs - xr * sn;   /* z = x e^{-j ph} */
        int best = 0;
        float bd = 1e30f;
        for (int p = 0; p < m; p++) {
            float dr = zr - pts[2 * p], di = zi - pts[2 * p + 1];
            float dd = dr * dr + di * di;
            if (dd < bd) { bd = dd; best = p; }
        }
        float pr = pts[2 * best], pi = pts[2 * best + 1];
        /* phase error = arg(z conj(d)) */
        float e = atan2f(zi * pr - zr * pi, zr * pr + zi * pi);
        fr += kb * e;
        ph += fr + ka * e;
        if (ph > 3.14159265f) ph -= 6.2831853f;
        if (ph < -3.14159265f) ph += 6.2831853f;
        out[2 * k] = zr;
        out[2 * k + 1] = zi;
        idx[k] = best;
        err += bd;
    }
    return n ? (float)(err / n) : 0.f;
}

/* Same loop for a square QAM (16, 64...): the nearest point is found by rounding each axis
 * on the grid (level i at off + i * step, L levels) instead of comparing the m points;
 * lut[i * L + j] = index of the point (I level i, Q level j). 64-QAM: ~5x faster decisions. */
float dd_track_grid(const float *in, int n, const float *pts, int L, float off, float step,
                    const int *lut, float ka, float kb, float *out, int *idx)
{
    /* the derotation e^{-j ph} is kept as a phasor (c, s) rotated by the small step of each
     * symbol (2nd-order Taylor, renormalized every 8 symbols), recomputed exactly every 256
     * symbols; the phase error uses the small-angle form Im(z d*) / |d|^2: no sin / cos / atan2
     * per symbol. The decided point comes from the grid levels and 1 / |d|^2 from a table by grid
     * position, lut (label) off the critical path: 26 -> 19 ns per symbol, same decisions
     * (2026-10-12). pts is only used for the labels' order (through lut). */
    (void)pts;
    float ph = 0.f, fr = 0.f;
    double err = 0.0;
    float eacc = 0.f;
    float c = 1.f, s = 0.f;
    float istep = 1.f / step;
    float inv2[L * L], lev[L];
    for (int i = 0; i < L; i++) lev[i] = off + i * step;
    for (int i = 0; i < L; i++)
        for (int j = 0; j < L; j++) inv2[i * L + j] = 1.f / (lev[i] * lev[i] + lev[j] * lev[j] + 1e-12f);
    for (int k = 0; k < n; k++) {
        if ((k & 255) == 0) {
            if (ph > 3.14159265f || ph < -3.14159265f) ph = remainderf(ph, 6.2831853f);
            c = cosf(ph); s = sinf(ph);
            err += eacc; eacc = 0.f;
        }
        float xr = in[2 * k], xi = in[2 * k + 1];
        float zr = xr * c + xi * s, zi = xi * c - xr * s;
        int i = (int)lrintf((zr - off) * istep), j = (int)lrintf((zi - off) * istep);
        i = i < 0 ? 0 : (i >= L ? L - 1 : i);
        j = j < 0 ? 0 : (j >= L ? L - 1 : j);
        int q = i * L + j;
        float pr = lev[i], pi = lev[j];
        float dr = zr - pr, di = zi - pi;
        float e = (zi * pr - zr * pi) * inv2[q];
        e = e > 0.5f ? 0.5f : (e < -0.5f ? -0.5f : e);
        fr += kb * e;
        float dph = fr + ka * e;
        ph += dph;
        float cd = 1.f - 0.5f * dph * dph, sd = dph;          /* e^{j dph}, |dph| << 1 */
        float nc = c * cd - s * sd, ns = s * cd + c * sd;
        if ((k & 7) == 7) {                                   /* keep |phasor| = 1 */
            float g = 1.5f - 0.5f * (nc * nc + ns * ns);
            nc *= g; ns *= g;
        }
        c = nc; s = ns;
        out[2 * k] = zr;
        out[2 * k + 1] = zi;
        idx[k] = lut[q];
        eacc += dr * dr + di * di;
    }
    err += eacc;
    return n ? (float)(err / n) : 0.f;
}

/* ------------------------------------------------------------------ one burst, data stage
 * After the preamble is found (us_phy._detect), the rest of a burst in a few calls:
 *   burst_prep        symbols at ty0 + k * sps on the matched-filter output y, carrier offset
 *                     removed (fr cycles per symbol), gain and phase from the preamble; QAM: data
 *                     at unit mean power (first 300 data symbols above 0.25)
 *   eq_track          optional linear equalizer (taps centred), then decision-directed tracking
 *   symbols_to_bytes  decided labels -> bits (MSB first), descrambled, packed into bytes
 *   rs_decode_many    every codeword of a known layout
 * (Python did the same in ~15 numpy / ctypes steps: ~2.4 ms per long 64-QAM burst.) */
int burst_prep(const float *y, int ny, double ty0, double sps, int n, double fr,
               const float *pre, int npre, int qam_norm, float *r_out, float *d_out)
{
    interp_symbols(y, ny, ty0, sps, n, r_out);
    double gr = 0.0, gi = 0.0;
    double wr = cos(-2.0 * 3.14159265358979323846 * fr), wi = sin(-2.0 * 3.14159265358979323846 * fr);
    double pc = 1.0, ps = 0.0;                           /* e^{-j 2 pi fr k}, rotated each symbol */
    for (int k = 0; k < n; k++) {
        if ((k & 1023) == 0) {                           /* exact again every 1024 symbols */
            double a = -2.0 * 3.14159265358979323846 * fr * k;
            pc = cos(a); ps = sin(a);
        }
        float c = (float)pc, s = (float)ps;
        double npc = pc * wr - ps * wi;
        ps = pc * wi + ps * wr;
        pc = npc;
        float xr = r_out[2 * k], xi = r_out[2 * k + 1];
        r_out[2 * k] = xr * c - xi * s;
        r_out[2 * k + 1] = xr * s + xi * c;
        if (k < npre) {                               /* g = mean(r * conj(pre)) */
            gr += r_out[2 * k] * pre[2 * k] + r_out[2 * k + 1] * pre[2 * k + 1];
            gi += r_out[2 * k + 1] * pre[2 * k] - r_out[2 * k] * pre[2 * k + 1];
        }
    }
    if (npre <= 0) return -1;
    gr /= npre; gi /= npre;
    double g2 = gr * gr + gi * gi;
    if (g2 <= 0.0) return -1;
    float ir = (float)(gr / g2), ii = (float)(-gi / g2);   /* 1 / g */
    for (int k = 0; k < n; k++) {
        float xr = r_out[2 * k], xi = r_out[2 * k + 1];
        r_out[2 * k] = xr * ir - xi * ii;
        r_out[2 * k + 1] = xr * ii + xi * ir;
    }
    int nd = n - npre;
    float scale = 1.f;
    if (qam_norm) {
        double acc = 0.0;
        int cnt = 0;
        for (int k = 0; k < nd && k < 300; k++) {
            float dr = r_out[2 * (npre + k)], di = r_out[2 * (npre + k) + 1];
            float p2 = dr * dr + di * di;
            if (p2 > 0.0625f) { acc += p2; cnt++; }
        }
        if (cnt > 16) scale = (float)(1.0 / sqrt(acc / cnt));
    }
    for (int k = 0; k < 2 * nd; k++)
        d_out[k] = r_out[2 * npre + k] * scale;
    return 0;
}

float eq_track(const float *in, int n, const float *w, int ntaps, const float *pts, int m,
               int L, float off, float step, const int *lut, float ka, float kb,
               float *tmp, float *out, int *idx)
{
    const float *x = in;
    if (w && ntaps > 0) {                             /* y[k] = sum_j in[k + j - h] w[j] */
        int h = ntaps / 2;
        int k = 0;
#ifdef __AVX2__
        /* middle, 4 outputs at a time: x * (wr, wr) + swap(x) * (-wi, wi) per tap, 2 FMAs
         * (0.2 of the 0.67 ms of a 16,640-symbol burst in scalar, 2026-10-11) */
        for (; k < h && k < n; k++) {
            float ar = 0.f, ai = 0.f;
            for (int j = 0; j < ntaps; j++) {
                int i = k + j - h;
                if (i < 0 || i >= n) continue;
                float xr = in[2 * i], xi = in[2 * i + 1], wr = w[2 * j], wi = w[2 * j + 1];
                ar += xr * wr - xi * wi;
                ai += xr * wi + xi * wr;
            }
            tmp[2 * k] = ar;
            tmp[2 * k + 1] = ai;
        }
        for (; k + 4 + ntaps - h <= n; k += 4) {
            __m256 acc0 = _mm256_setzero_ps(), acc1 = _mm256_setzero_ps();
            const float *xp = in + 2 * (k - h);
            for (int j = 0; j < ntaps; j++) {
                __m256 xv = _mm256_loadu_ps(xp + 2 * j);
                __m256 sw = _mm256_permute_ps(xv, 0xB1);          /* (xi, xr) pairs */
                __m256 a = _mm256_set1_ps(w[2 * j]);
                __m256 b = _mm256_setr_ps(-w[2 * j + 1], w[2 * j + 1], -w[2 * j + 1], w[2 * j + 1],
                                          -w[2 * j + 1], w[2 * j + 1], -w[2 * j + 1], w[2 * j + 1]);
                if (j & 1) {
                    acc1 = _mm256_fmadd_ps(xv, a, acc1);
                    acc1 = _mm256_fmadd_ps(sw, b, acc1);
                } else {
                    acc0 = _mm256_fmadd_ps(xv, a, acc0);
                    acc0 = _mm256_fmadd_ps(sw, b, acc0);
                }
            }
            _mm256_storeu_ps(tmp + 2 * k, _mm256_add_ps(acc0, acc1));
        }
#endif
        for (; k < n; k++) {
            float ar = 0.f, ai = 0.f;
            if (k >= h && k + ntaps - h <= n) {             /* middle: no bounds to check */
                const float *xp = in + 2 * (k - h);
                for (int j = 0; j < ntaps; j++) {
                    float xr = xp[2 * j], xi = xp[2 * j + 1], wr = w[2 * j], wi = w[2 * j + 1];
                    ar += xr * wr - xi * wi;
                    ai += xr * wi + xi * wr;
                }
            } else {
                for (int j = 0; j < ntaps; j++) {
                    int i = k + j - h;
                    if (i < 0 || i >= n) continue;
                    float xr = in[2 * i], xi = in[2 * i + 1], wr = w[2 * j], wi = w[2 * j + 1];
                    ar += xr * wr - xi * wi;
                    ai += xr * wi + xi * wr;
                }
            }
            tmp[2 * k] = ar;
            tmp[2 * k + 1] = ai;
        }
        x = tmp;
    }
    if (L > 0)
        return dd_track_grid(x, n, pts, L, off, step, lut, ka, kb, out, idx);
    return dd_track(x, n, pts, m, ka, kb, out, idx);
}

int symbols_to_bytes(const int *idx, int n, int bps, const uint8_t *prbs, int nprbs, uint8_t *out)
{
    /* whole labels into a bit accumulator, bytes out as they fill; then the scrambler bits
     * (one byte per bit, as us_phy._prbs gives them) XORed 8 at a time. Bit by bit with a
     * branch per bit it cost 0.3 ms per 16,640-symbol 64-QAM burst (2026-10-11). */
    int nbytes = n * bps / 8, o = 0, nb = 0;
    uint64_t acc = 0;
    unsigned mask = (1u << bps) - 1;
    for (int k = 0; k < n && o < nbytes; k++) {
        acc = (acc << bps) | ((unsigned)idx[k] & mask);
        nb += bps;
        while (nb >= 8 && o < nbytes) {
            nb -= 8;
            out[o++] = (uint8_t)(acc >> nb);
        }
    }
    if (prbs) {
        int full = nprbs / 8 < nbytes ? nprbs / 8 : nbytes;
        for (int b = 0; b < full; b++) {
            const uint8_t *q = prbs + 8 * b;
            out[b] ^= (uint8_t)((q[0] << 7) | (q[1] << 6) | (q[2] << 5) | (q[3] << 4)
                                | (q[4] << 3) | (q[5] << 2) | (q[6] << 1) | q[7]);
        }
        for (int pos = 8 * full; pos < nprbs && pos < 8 * nbytes; pos++)
            out[pos >> 3] ^= (uint8_t)(prbs[pos] << (7 - (pos & 7)));
    }
    return nbytes;
}

int rs_decode_many(const uint8_t *buf, int nbuf, const int *info, int ncw, int nsym, int fcr,
                   uint8_t *msg_out, int *fixed)
{
    uint8_t cw[255];
    int pos = 0, o = 0;
    *fixed = 0;
    for (int i = 0; i < ncw; i++) {
        int n = info[i] + nsym;
        if (n > 255 || pos + n > nbuf) return i;      /* burst truncated */
        memcpy(cw, buf + pos, n);
        int r = rs_decode(cw, n, nsym, fcr);
        if (r < 0) return i;
        *fixed += r;
        memcpy(msg_out + o, cw, info[i]);
        o += info[i];
        pos += n;
    }
    return -1;
}

/* The last, shortened codeword of a burst that ended early: its information length is
 * unknown (2026-10-10, MTC segments). Tries lengths lmax down to lmin at buf: first a length
 * whose syndromes are all zero (a clean burst: one Horner pass each), then a full decode at
 * each length. Returns the length found (message in msg_out, corrections in *fixed) or -1.
 * One C call instead of up to 200 Python round trips (60 % of a burst's decoding time). */
int rs_find_last(const uint8_t *buf, int nbuf, int lmax, int lmin, int nsym, int fcr,
                 uint8_t *msg_out, int *fixed)
{
    uint8_t cw[255];
    *fixed = 0;
    if (nsym <= 0 || nsym > 64 || fcr < 0 || fcr + nsym > 64) return -1;
    {
        /* one pass: after j bytes, y[i] is syndrome i of buf[0 .. j) taken as a codeword, so
         * every length is checked on the way (one pass per length tried before, up to ~200) */
        uint8_t y[64];
        memset(y, 0, (size_t)nsym);
        int best = -1, nmax = lmax + nsym;
        if (nmax > nbuf) nmax = nbuf;
        if (nmax > 255) nmax = 255;
        for (int j = 0; j < nmax; j++) {
            uint8_t b = buf[j], any = 0;
            for (int i = 0; i < nsym; i++) { y[i] = RMUL[fcr + i][y[i]] ^ b; any |= y[i]; }
            if (!any && j + 1 - nsym >= lmin) best = j + 1 - nsym;
        }
        if (best >= 0) { memcpy(msg_out, buf, best); return best; }
    }
    for (int L = lmax; L >= lmin; L--) {
        int n = L + nsym;
        if (n > 255 || n > nbuf) continue;
        memcpy(cw, buf, n);
        int r = rs_decode(cw, n, nsym, fcr);
        if (r >= 0) { memcpy(msg_out, cw, L); *fixed = r; return L; }
    }
    return -1;
}

/* ------------------------------------------------------------------ matched filter
 * Full convolution of a complex signal by real taps (numpy.convolve(x, h, "full")):
 * out has nx + nh - 1 samples. */
/* Preamble correlation (us_phy._metric) at the contiguous positions p0 .. p0 + npos - 1 of y:
 * c1 = sum_{k<h} y[p + offs[k]] conj(pre[k]), c2 the same over k >= h (h = npre / 2),
 * e = sum |y[p + offs[k]]|^2, m = |c1| + |c2|, rho = m^2 / (npre e). The preamble symbols are the
 * outer loop and the positions the inner one: every pass is a contiguous complex axpy that
 * vectorizes (numpy gathered a positions x preamble array first: 15 % of an upstream process
 * under load on the i5-9500, 2026-10-10, mostly the empty REQ windows). work: 5 * npos floats.
 * The caller keeps p0 + npos - 1 + offs[npre - 1] inside y. */
void pre_metric(const float *y, int p0, int npos, const int *offs, const float *pre, int npre,
                float *work, float *m, float *rho, float *c1, float *c2)
{
    float *a1r = work, *a1i = work + npos, *a2r = work + 2 * npos, *a2i = work + 3 * npos, *e = work + 4 * npos;
    memset(work, 0, sizeof(float) * 5 * npos);
    int h = npre / 2;
    for (int k = 0; k < npre; k++) {
        float pr = pre[2 * k], pi = -pre[2 * k + 1];      /* conj(pre[k]) */
        const float *yk = y + 2 * (p0 + offs[k]);
        float *ar = k < h ? a1r : a2r, *ai = k < h ? a1i : a2i;
        for (int i = 0; i < npos; i++) {
            float xr = yk[2 * i], xi = yk[2 * i + 1];
            ar[i] += xr * pr - xi * pi;
            ai[i] += xr * pi + xi * pr;
            e[i] += xr * xr + xi * xi;
        }
    }
    for (int i = 0; i < npos; i++) {
        float m1 = sqrtf(a1r[i] * a1r[i] + a1i[i] * a1i[i]), m2 = sqrtf(a2r[i] * a2r[i] + a2i[i] * a2i[i]);
        float mm = m1 + m2;
        m[i] = mm;
        rho[i] = mm * mm / (npre * (e[i] + 1e-30f));
        c1[2 * i] = a1r[i]; c1[2 * i + 1] = a1i[i];
        c2[2 * i] = a2r[i]; c2[2 * i + 1] = a2i[i];
    }
}

static __thread float *fir_work = 0;
static __thread long fir_work_n = 0;

void fir_cr(const float *x, int nx, const float *h, int nh, float *out)
{
    /* out[n] = sum_k h[k] x[n - k], n = 0 .. nx + nh - 2 ("full", numpy.convolve). The input is
     * copied between nh - 1 zeros, the taps reversed and doubled (h h for re im): each output is
     * a contiguous dot product, run with four independent AVX2 accumulators. (The former pass per
     * tap read and wrote the whole output nh times: 9.5 % of an upstream process under load on the
     * i5-9500, 2026-10-10.) Sums in another order than numpy: equal to ~1e-7. */
    int ny = nx + nh - 1;
    int nf = (2 * nh + 31) & ~31;
    long need = 2L * (nx + 2 * (nh - 1)) + nf;
    if (need > fir_work_n) {
        free(fir_work);
        fir_work = (float *)malloc(sizeof(float) * need);
        fir_work_n = fir_work ? need : 0;
        if (!fir_work) return;
    }
    float *xp = fir_work;
    memset(xp, 0, sizeof(float) * 2 * (nh - 1));
    memcpy(xp + 2 * (nh - 1), x, sizeof(float) * 2 * nx);
    memset(xp + 2 * (nh - 1 + nx), 0, sizeof(float) * (2 * (nh - 1) + nf));
    float hr[nf];
    for (int j = 0; j < nf; j++) hr[j] = j < 2 * nh ? h[nh - 1 - j / 2] : 0.f;
    for (int n = 0; n < ny; n++) {
        const float *b = xp + 2 * n;
#ifdef __AVX2__
        __m256 a0 = _mm256_setzero_ps(), a1 = a0, a2 = a0, a3 = a0;
        for (int j = 0; j < nf; j += 32) {
            a0 = _mm256_fmadd_ps(_mm256_loadu_ps(hr + j), _mm256_loadu_ps(b + j), a0);
            a1 = _mm256_fmadd_ps(_mm256_loadu_ps(hr + j + 8), _mm256_loadu_ps(b + j + 8), a1);
            a2 = _mm256_fmadd_ps(_mm256_loadu_ps(hr + j + 16), _mm256_loadu_ps(b + j + 16), a2);
            a3 = _mm256_fmadd_ps(_mm256_loadu_ps(hr + j + 24), _mm256_loadu_ps(b + j + 24), a3);
        }
        __m256 t8 = _mm256_add_ps(_mm256_add_ps(a0, a1), _mm256_add_ps(a2, a3));
        __m128 t4 = _mm_add_ps(_mm256_castps256_ps128(t8), _mm256_extractf128_ps(t8, 1));
        t4 = _mm_add_ps(t4, _mm_movehl_ps(t4, t4));
        out[2 * n] = _mm_cvtss_f32(t4);
        out[2 * n + 1] = _mm_cvtss_f32(_mm_shuffle_ps(t4, t4, 1));
#else
        float ar = 0.f, ai = 0.f;
        for (int j = 0; j < 2 * nh; j += 2) {
            ar += hr[j] * b[j];
            ai += hr[j] * b[j + 1];
        }
        out[2 * n] = ar;
        out[2 * n + 1] = ai;
#endif
    }
}

/* ------------------------------------------------------------------ digital down-conversion
 * One upstream channel out of the wideband RX stream (several upstream channels received at
 * once): shift by `step` rad/sample (phase carried in *phase), low-pass FIR (real taps, ntaps)
 * and keep one sample in `decim`. hist: the ntaps-1 previous shifted samples (state, complex),
 * *skip: input samples to skip before the next output (state). Returns the outputs written. */

/* DDC helpers (2026-10-10): a work buffer kept between calls (a fresh malloc of ~0.5 MB per call
 * came back as new pages from the kernel every time), and the frequency shift computed by blocks
 * of 1024 samples from a table of w^k: no sample depends on the previous one, so the loop
 * vectorizes (the running oscillator z <- z w was a serial chain). */
static __thread float *ddc_work = 0;
static __thread long ddc_work_n = 0;
static float *ddc_buf(long nfloats)
{
    if (nfloats > ddc_work_n) {
        free(ddc_work);
        ddc_work = (float *)malloc(sizeof(float) * nfloats);
        ddc_work_n = ddc_work ? nfloats : 0;
    }
    return ddc_work;
}

__attribute__((optimize("fast-math")))
static void ddc_mix(const float *x, int n, double step, double ph, float *dst)
{
    float tr[1024], ti[1024];
    for (int k = 0; k < 1024; k++) { tr[k] = (float)cos(k * step); ti[k] = (float)sin(k * step); }
    for (int k0 = 0; k0 < n; k0 += 1024) {
        float zr = (float)cos(ph + k0 * step), zi = (float)sin(ph + k0 * step);
        int kn = n - k0 < 1024 ? n - k0 : 1024;
        const float *xs = x + 2 * k0;
        float *d = dst + 2 * k0;
        for (int k = 0; k < kn; k++) {
            float cr = zr * tr[k] - zi * ti[k], ci = zr * ti[k] + zi * tr[k];
            float xr = xs[2 * k], xi = xs[2 * k + 1];
            d[2 * k] = xr * cr - xi * ci;
            d[2 * k + 1] = xr * ci + xi * cr;
        }
    }
}

__attribute__((optimize("fast-math")))
int ddc_decim(const float *x, int n, double step, double *phase, const float *h, int ntaps,
              int decim, float *hist, int *skip, float *out)
{
    int nh = ntaps - 1, nout = 0;
    double ph = *phase;
    /* taps doubled (h0 h0 h1 h1 ...) and padded to a multiple of 32 floats: each output is then a
     * plain dot product over the interleaved samples, even lanes real, odd lanes imaginary,
     * which AVX2 runs 8 wide (the stride-2 loop took 0.28 core per channel on the i5-9500 with
     * no AVX-512, 2026-10-10) */
    int nf = (2 * ntaps + 31) & ~31;
    float hh[nf];
    for (int j = 0; j < nf; j++) hh[j] = j < 2 * ntaps ? h[j / 2] : 0.f;
    /* work buffer: history then the new block, shifted, then the padding the last dot reads */
    float *buf = ddc_buf(2L * (nh + n) + nf);
    if (!buf) return 0;
    memcpy(buf, hist, sizeof(float) * 2 * nh);
    ddc_mix(x, n, step, ph, buf + 2 * nh);
    memset(buf + 2L * (nh + n), 0, sizeof(float) * nf);
    int k = *skip;
    for (; k < n; k += decim) {
        const float *b = buf + 2 * k;                   /* taps over buf[k .. k + nh] */
        /* four independent 8-wide accumulators (AVX2): one accumulator made a single chain of 16
         * dependent FMAs, ~64 cycles per output on the i5-9500 */
#ifdef __AVX2__
        __m256 a0 = _mm256_setzero_ps(), a1 = a0, a2 = a0, a3 = a0;
        for (int j = 0; j < nf; j += 32) {
            a0 = _mm256_fmadd_ps(_mm256_loadu_ps(hh + j), _mm256_loadu_ps(b + j), a0);
            a1 = _mm256_fmadd_ps(_mm256_loadu_ps(hh + j + 8), _mm256_loadu_ps(b + j + 8), a1);
            a2 = _mm256_fmadd_ps(_mm256_loadu_ps(hh + j + 16), _mm256_loadu_ps(b + j + 16), a2);
            a3 = _mm256_fmadd_ps(_mm256_loadu_ps(hh + j + 24), _mm256_loadu_ps(b + j + 24), a3);
        }
        __m256 t8 = _mm256_add_ps(_mm256_add_ps(a0, a1), _mm256_add_ps(a2, a3));
        __m128 t4 = _mm_add_ps(_mm256_castps256_ps128(t8), _mm256_extractf128_ps(t8, 1));  /* r i r i */
        t4 = _mm_add_ps(t4, _mm_movehl_ps(t4, t4));                                      /* r i . . */
        float ar = _mm_cvtss_f32(t4), ai = _mm_cvtss_f32(_mm_shuffle_ps(t4, t4, 1));
#else
        float acc[8] = {0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f};
        for (int j = 0; j < nf; j += 8)
            for (int l = 0; l < 8; l++)
                acc[l] += hh[j + l] * b[j + l];
        float ar = (acc[0] + acc[2]) + (acc[4] + acc[6]), ai = (acc[1] + acc[3]) + (acc[5] + acc[7]);
#endif
        out[2 * nout] = ar;
        out[2 * nout + 1] = ai;
        nout++;
    }
    *skip = k - n;
    memcpy(hist, buf + 2 * n, sizeof(float) * 2 * nh);
    *phase = fmod(ph + n * step, 2 * M_PI);
    return nout;
}

/* ------------------------------------------------------------------ energy gate
 * Mean power of each block of blk complex samples (p[nb]) and the k-th smallest of them (k < nb,
 * quickselect on a copy): the energy gate of every MAP interval (UpstreamRx._handle) and the raw
 * scan. In numpy (einsum, partition) plus the Python around it, ~0.1 core per upstream channel
 * under a download (2026-10-11). */
float block_power(const float *x, int nb, int blk, int k, float *p)
{
    if (nb <= 0) return 0.f;
    float inv = 1.f / blk;
    for (int b = 0; b < nb; b++) {
        const float *v = x + 2L * b * blk;
        float s0 = 0.f, s1 = 0.f, s2 = 0.f, s3 = 0.f;
        int j = 0;
        for (; j + 4 <= 2 * blk; j += 4) {
            s0 += v[j] * v[j]; s1 += v[j + 1] * v[j + 1]; s2 += v[j + 2] * v[j + 2]; s3 += v[j + 3] * v[j + 3];
        }
        for (; j < 2 * blk; j++) s0 += v[j] * v[j];
        p[b] = (s0 + s1 + s2 + s3) * inv;
    }
    if (k < 0) k = 0;
    if (k >= nb) k = nb - 1;
    float *a = (float *)malloc(sizeof(float) * nb);
    if (!a) return p[k];
    memcpy(a, p, sizeof(float) * nb);
    int lo = 0, hi = nb - 1;
    while (lo < hi) {                                   /* Hoare quickselect */
        float piv = a[(lo + hi) / 2];
        int i = lo, j = hi;
        while (i <= j) {
            while (a[i] < piv) i++;
            while (a[j] > piv) j--;
            if (i <= j) { float t = a[i]; a[i] = a[j]; a[j] = t; i++; j--; }
        }
        if (k <= j) hi = j;
        else if (k >= i) lo = i;
        else break;
    }
    float r = a[k];
    free(a);
    return r;
}

/* ------------------------------------------------------------------ one segment burst, whole
 * The common case of a Multiple Transmit Channel upstream, start to end in one call: a data
 * grant of a modem whose equalizer is cached (us_phy.BurstDemod._seg_fast). Same steps, same
 * order and same thresholds as us_phy._detect + _equalized + _rs_frame(seg=True); anything off
 * that path (no preamble in this orientation, equalizer to refit, segment unreadable) returns
 * a negative status and Python decodes the burst its usual way. Python's glue around the C
 * kernels was ~36 % of an upstream process under upload (2026-10-12, 6 DS + 2 US, 8 flows).
 *   res[0..9]: t (input samples), rho, power (dBFS), peak, fr (cycles/symbol), data symbols,
 *              message bytes, RS corrections, equalizer (0 cached, 1 refitted: w_out + mse in
 *              res[9], 2 none fitted)
 *   returns 0, -1 no preamble, -3 segment unreadable, -4 buffer/alloc */
static int burst_len_c(const float *d, int n)
{
    if (n < 16) return 0;
    double acc = 0.0;
    for (int k = 0; k < 16; k++) acc += (double)d[2 * k] * d[2 * k] + (double)d[2 * k + 1] * d[2 * k + 1];
    int last = -1;
    for (int i = 0; i + 16 <= n; i++) {
        if (i > 0) {
            int a = i - 1, b = i + 15;
            acc += (double)d[2 * b] * d[2 * b] + (double)d[2 * b + 1] * d[2 * b + 1]
                 - ((double)d[2 * a] * d[2 * a] + (double)d[2 * a + 1] * d[2 * a + 1]);
        }
        if (acc / 16 > 0.25) last = i;
    }
    return last >= 0 ? last + 8 + 1 : 0;
}

/* Least squares fit of an ntaps (<= 32) complex equalizer, us_phy.equalize_fit: rows of `d`
 * (n symbols, sliding windows of ntaps) against the decisions ref = pts[idx[h .. n - h)], by
 * the normal equations (A^H A) w = A^H ref solved with partial pivoting in double. Returns 0,
 * or -1 if singular. */
static int eq_fit_ls(const float *d, int n, const float *pts, const int *idx, int ntaps, float *w)
{
    double Ar[32][33], Ai[32][33];
    int h = ntaps / 2, nr = n - ntaps + 1;
    if (ntaps > 32 || nr <= ntaps) return -1;
    for (int a = 0; a < ntaps; a++)
        for (int b = 0; b <= ntaps; b++) Ar[a][b] = Ai[a][b] = 0.0;
    for (int r = 0; r < nr; r++) {
        const float *x = d + 2 * r;
        int q = idx[r + h];
        double yr = pts[2 * q], yi = pts[2 * q + 1];
        for (int a = 0; a < ntaps; a++) {
            double ar = x[2 * a], ai = -x[2 * a + 1];          /* conj(x_a) */
            for (int b = a; b < ntaps; b++) {
                double br = x[2 * b], bi = x[2 * b + 1];
                Ar[a][b] += ar * br - ai * bi;
                Ai[a][b] += ar * bi + ai * br;
            }
            Ar[a][ntaps] += ar * yr - ai * yi;
            Ai[a][ntaps] += ar * yi + ai * yr;
        }
    }
    for (int a = 0; a < ntaps; a++)                          /* Hermitian: lower = conj(upper) */
        for (int b = 0; b < a; b++) { Ar[a][b] = Ar[b][a]; Ai[a][b] = -Ai[b][a]; }
    for (int c = 0; c < ntaps; c++) {
        int pv = c;
        double best = Ar[c][c] * Ar[c][c] + Ai[c][c] * Ai[c][c];
        for (int r = c + 1; r < ntaps; r++) {
            double v = Ar[r][c] * Ar[r][c] + Ai[r][c] * Ai[r][c];
            if (v > best) { best = v; pv = r; }
        }
        if (best < 1e-30) return -1;
        if (pv != c)
            for (int b = 0; b <= ntaps; b++) {
                double t = Ar[c][b]; Ar[c][b] = Ar[pv][b]; Ar[pv][b] = t;
                t = Ai[c][b]; Ai[c][b] = Ai[pv][b]; Ai[pv][b] = t;
            }
        double dr = Ar[c][c], di = Ai[c][c], dn = dr * dr + di * di;
        double ir = dr / dn, ii = -di / dn;                  /* 1 / pivot */
        for (int r = c + 1; r < ntaps; r++) {
            double fr = Ar[r][c] * ir - Ai[r][c] * ii, fi = Ar[r][c] * ii + Ai[r][c] * ir;
            for (int b = c; b <= ntaps; b++) {
                Ar[r][b] -= fr * Ar[c][b] - fi * Ai[c][b];
                Ai[r][b] -= fr * Ai[c][b] + fi * Ar[c][b];
            }
        }
    }
    double wr[32], wi[32];
    for (int c = ntaps - 1; c >= 0; c--) {
        double sr = Ar[c][ntaps], si = Ai[c][ntaps];
        for (int b = c + 1; b < ntaps; b++) {
            sr -= Ar[c][b] * wr[b] - Ai[c][b] * wi[b];
            si -= Ar[c][b] * wi[b] + Ai[c][b] * wr[b];
        }
        double dr = Ar[c][c], di = Ai[c][c], dn = dr * dr + di * di;
        wr[c] = (sr * dr + si * di) / dn;
        wi[c] = (si * dr - sr * di) / dn;
    }
    for (int a = 0; a < ntaps; a++) { w[2 * a] = (float)wr[a]; w[2 * a + 1] = (float)wi[a]; }
    return 0;
}

static double sym_mse(const float *o, const int *idx, const float *pts, int a, int b)
{
    double e = 0.0;
    for (int k = a; k < b; k++) {
        float er = o[2 * k] - pts[2 * idx[k]], ei = o[2 * k + 1] - pts[2 * idx[k] + 1];
        e += er * er + ei * ei;
    }
    return b > a ? e / (b - a) : 0.0;
}

int seg_fast(const float *x, int nx, int conj, const float *mf, int nmf, int mf_delay,
             double sps, double t_lo, double t_hi, float rho_min, int span,
             const float *pre, int npre, const int *offs, const int *uoffs, int up,
             int nsym_cap, double sps_d,
             const float *w, int ntaps, float mse_max, int eq_train,
             const float *pts, int M, int L, float off, float step, const int *lut, int bps,
             const uint8_t *prbs, int nprbs,
             const int *layout, int ncw, int nparity, int fec_k,
             uint8_t *msg_out, int msg_cap, float *sym_out, int *idx_out, int sym_cap, double *res,
             float *w_out)
{
    int status = -4;
    int ny = nx + nmf - 1;
    float *y = 0, *yu = 0, *wk = 0, *r = 0, *d = 0, *tmp = 0;
    uint8_t *raw = 0;
    y = malloc(sizeof(float) * 2 * (size_t)ny);
    if (!y) goto done;
    fir_cr(x, nx, mf, nmf, y);
    if (conj)
        for (int i = 0; i < ny; i++) y[2 * i + 1] = -y[2 * i + 1];
    /* ---- coarse search */
    int lo = (int)t_lo + mf_delay - 1;
    if (lo < 0) lo = 0;
    int hi = (int)ceil(t_hi) + mf_delay + 1;
    if (hi > ny - offs[npre - 1] - 1) hi = ny - offs[npre - 1] - 1;
    status = -1;
    if (hi <= lo) goto done;
    int npos = hi - lo;
    int nfine = 4 * up + 1;
    int nw = npos > nfine ? npos : nfine;
    wk = malloc(sizeof(float) * 11 * (size_t)nw);
    if (!wk) { status = -4; goto done; }
    float *m = wk + 5 * nw, *rho = wk + 6 * nw, *c1 = wk + 7 * nw, *c2 = wk + 9 * nw;
    pre_metric(y, lo, npos, offs, pre, npre, wk, m, rho, c1, c2);
    int i = 0;
    for (int k = 1; k < npos; k++) if (m[k] > m[i]) i = k;
    if (rho[i] < rho_min * 0.7f) goto done;
    int p0 = lo + i;
    /* ---- fine search on the oversampled matched filter output */
    double nmax = (double)(ny - p0) / sps - 2 * span;
    int nsym_max = (int)(nmax < nsym_cap ? nmax : nsym_cap);
    int a = p0 - 4 > 0 ? p0 - 4 : 0;
    int b = p0 + (int)ceil(npre * sps) + 24 + 2 * span;
    if (b > ny) b = ny;
    int nyu = (b - a) * up;
    yu = malloc(sizeof(float) * 2 * (size_t)(nyu > 0 ? nyu : 1));
    if (!yu) { status = -4; goto done; }
    interp_symbols(y, ny, (double)a, 1.0 / up, nyu, yu);
    int base = (p0 - a) * up;
    int f0 = base - 2 * up > 1 ? base - 2 * up : 1;
    int f1 = base + 2 * up + 1;
    if (f1 > nyu - uoffs[npre - 1] - 1) f1 = nyu - uoffs[npre - 1] - 1;
    if (f1 <= f0) goto done;
    int nf = f1 - f0;
    pre_metric(yu, f0, nf, uoffs, pre, npre, wk, m, rho, c1, c2);
    int j = 0;
    for (int k = 1; k < nf; k++) if (m[k] > m[j]) j = k;
    if (rho[j] < rho_min) goto done;
    float frac = 0.f;
    if (j > 0 && j < nf - 1) {
        float dd = m[j - 1] - 2 * m[j] + m[j + 1];
        frac = dd < 0 ? 0.5f * (m[j - 1] - m[j + 1]) / dd : 0.f;
    }
    int i0 = f0 + j;
    double t = a + (i0 + frac) / (double)up - mf_delay;
    int h = npre / 2;
    float zr = c2[2 * j] * c1[2 * j] + c2[2 * j + 1] * c1[2 * j + 1];      /* c2 conj(c1) */
    float zi = c2[2 * j + 1] * c1[2 * j] - c2[2 * j] * c1[2 * j + 1];
    double fr = atan2(zi, zr) / (2 * 3.14159265358979323846 * h);
    double ty0 = a + (i0 + frac) / (double)up;
    double nlim = (ny - 9 - ty0) / sps;
    int nsym = (int)(nsym_max < nlim ? nsym_max : nlim);
    if (nsym <= npre) goto done;
    int nd = nsym - npre;
    status = -4;
    if (nd > sym_cap) goto done;
    r = malloc(sizeof(float) * 2 * (size_t)nsym);
    d = malloc(sizeof(float) * 2 * (size_t)nd);
    tmp = malloc(sizeof(float) * 2 * (size_t)nd);
    raw = malloc((size_t)nd * bps / 8 + 1);
    if (!r || !d || !tmp || !raw) goto done;
    status = -1;
    if (burst_prep(y, ny, ty0, sps_d, nsym, fr, pre, npre, 1, r, d) < 0) goto done;
    /* ---- power and peak over the preamble, raw input */
    int s0 = (int)lround(t) > 0 ? (int)lround(t) : 0;
    int s1 = s0 + (int)(npre * sps);
    if (s1 > nx) s1 = nx;
    double pw = 0.0;
    float peak = 0.f;
    for (int k = s0; k < s1; k++) {
        float p2 = x[2 * k] * x[2 * k] + x[2 * k + 1] * x[2 * k + 1];
        pw += p2;
        if (p2 > peak) peak = p2;
    }
    double power = 10 * log10((s1 > s0 ? pw / (s1 - s0) : 0.0) + 1e-20);
    /* ---- cached equalizer, then the same check as _equalized */
    eq_track(d, nd, w, ntaps, pts, M, L, off, step, lut, 0.08f, 0.004f, tmp, sym_out, idx_out);
    int n = burst_len_c(sym_out, nd);
    if (n > eq_train) n = eq_train;
    int hh = ntaps / 2;
    res[8] = 0;                                     /* cached taps used */
    if (n < 4 * ntaps || n - 2 * hh <= 0 || sym_mse(sym_out, idx_out, pts, hh, n - hh) > mse_max) {
        /* _equalized: the cached taps no longer fit -> plain tracking, then a fresh fit,
         * kept (res[8] = 1, taps in w_out, mse in res[9]) only if it does better */
        float *d0 = malloc(sizeof(float) * 2 * (size_t)nd);
        int *i0 = malloc(sizeof(int) * (size_t)nd);
        status = -4;
        if (!d0 || !i0) { free(d0); free(i0); goto done; }
        if (L > 0) dd_track_grid(d, nd, pts, L, off, step, lut, 0.08f, 0.004f, d0, i0);
        else dd_track(d, nd, pts, M, 0.08f, 0.004f, d0, i0);
        int n0 = burst_len_c(d0, nd);
        if (n0 > eq_train) n0 = eq_train;
        res[8] = 2;                                 /* no equalizer for this burst */
        int used = 0;
        if (n0 >= 4 * ntaps && eq_fit_ls(d0, n0, pts, i0, ntaps, w_out) == 0) {
            eq_track(d0, nd, w_out, ntaps, pts, M, L, off, step, lut, 0.08f, 0.004f, tmp, sym_out, idx_out);
            double before = sym_mse(d0, i0, pts, hh, n0 - hh);
            double after = sym_mse(sym_out, idx_out, pts, hh, n0 - hh);
            if (after < before) { res[8] = 1; res[9] = after; used = 1; }
        }
        if (!used) {
            memcpy(sym_out, d0, sizeof(float) * 2 * (size_t)nd);
            memcpy(idx_out, i0, sizeof(int) * (size_t)nd);
        }
        free(d0); free(i0);
    }
    /* ---- bytes, then the segment's codewords */
    int nraw = symbols_to_bytes(idx_out, nd, bps, prbs, nprbs, raw);
    int fixed = 0, fx = 0, o = 0, pos = 0, start;
    status = -4;
    {
        int need = 0;
        for (int k = 0; k < ncw; k++) need += layout[k];
        if (need > msg_cap) goto done;
    }
    int bad = rs_decode_many(raw, nraw, layout, ncw, nparity, 0, msg_out, &fx);
    start = bad < 0 ? ncw : bad;
    if (start) {
        for (int k = 0; k < start; k++) o += layout[k];
        fixed = fx;
        pos = o + start * nparity;
    }
    status = -3;
    for (int k = start; k < ncw; k++) {
        int ninfo = layout[k], got = -1;
        if (pos + ninfo + nparity <= nraw && rs_decode_many(raw + pos, nraw - pos, &ninfo, 1, nparity, 0,
                                                            msg_out + o, &fx) < 0)
            got = ninfo;
        if (got < 0) {
            int lmax = (ninfo < fec_k ? ninfo : fec_k) - 1;
            got = rs_find_last(raw + pos, nraw - pos, lmax, 16, nparity, 0, msg_out + o, &fx);
        }
        if (got < 0) {
            if (o == 0) goto done;                          /* uncorrectable (bloc 0) */
            int a_ = pos * 8 / bps, b_ = (pos + ninfo + nparity) * 8 / bps;
            if (b_ <= nd) {                                 /* the burst's end, or an error in it? */
                double pp = 0.0;
                for (int q = a_; q < b_; q++)
                    pp += sym_out[2 * q] * sym_out[2 * q] + sym_out[2 * q + 1] * sym_out[2 * q + 1];
                if (b_ > a_ && pp / (b_ - a_) > 0.25) goto done;
            }
            break;
        }
        fixed += fx;
        o += got;
        pos += got + nparity;
        if (got < ninfo) break;
    }
    res[0] = t; res[1] = rho[j]; res[2] = power; res[3] = sqrt(peak); res[4] = fr;
    res[5] = nd; res[6] = o; res[7] = fixed;
    status = 0;
done:
    free(y); free(yu); free(wk); free(r); free(d); free(tmp); free(raw);
    return status;
}
