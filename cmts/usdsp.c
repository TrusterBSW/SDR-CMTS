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
#include <stdint.h>
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
        for (int k = 0; k < 2 * INTERP_HALF; k++) INTERP[p][k] /= (float)sum;
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
    for (int i = 0; i < nsym; i++) {                 /* Horner, cw[0] = highest degree */
        uint8_t y = 0;
        if (fcr + i >= 0 && fcr + i < 64) {          /* table: one lookup per byte */
            const uint8_t *M = RMUL[fcr + i];
            for (int j = 0; j < n; j++) y = M[y] ^ cw[j];
        } else {
            uint8_t r = gpow(fcr + i);
            for (int j = 0; j < n; j++) y = gmul(y, r) ^ cw[j];
        }
        S[i] = y;
        any |= y;
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
        const float *h = INTERP[ph];
        float re = 0.f, im = 0.f;
        int first = i - (INTERP_HALF - 1);
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
     * symbol (2nd-order Taylor, renormalized), recomputed exactly every 256 symbols; the phase
     * error uses the small-angle form Im(z d*) / |d|^2: no sin / cos / atan2 per symbol */
    float ph = 0.f, fr = 0.f;
    double err = 0.0;
    float c = 1.f, s = 0.f;
    for (int k = 0; k < n; k++) {
        if ((k & 255) == 0) { c = cosf(ph); s = sinf(ph); }
        float xr = in[2 * k], xi = in[2 * k + 1];
        float zr = xr * c + xi * s, zi = xi * c - xr * s;
        int i = (int)lrintf((zr - off) / step), j = (int)lrintf((zi - off) / step);
        i = i < 0 ? 0 : (i >= L ? L - 1 : i);
        j = j < 0 ? 0 : (j >= L ? L - 1 : j);
        int best = lut[i * L + j];
        float pr = pts[2 * best], pi = pts[2 * best + 1];
        float dr = zr - pr, di = zi - pi;
        float e = (zi * pr - zr * pi) / (pr * pr + pi * pi + 1e-12f);
        if (e > 0.5f) e = 0.5f;
        if (e < -0.5f) e = -0.5f;
        fr += kb * e;
        float dph = fr + ka * e;
        ph += dph;
        if (ph > 3.14159265f) ph -= 6.2831853f;
        if (ph < -3.14159265f) ph += 6.2831853f;
        float cd = 1.f - 0.5f * dph * dph, sd = dph;          /* e^{j dph}, |dph| << 1 */
        float nc = c * cd - s * sd, ns = s * cd + c * sd;
        float g = 1.5f - 0.5f * (nc * nc + ns * ns);          /* keep |phasor| = 1 */
        c = nc * g; s = ns * g;
        out[2 * k] = zr;
        out[2 * k + 1] = zi;
        idx[k] = best;
        err += dr * dr + di * di;
    }
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
        for (int k = 0; k < n; k++) {
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
    int nbits = n * bps, nbytes = nbits / 8, pos = 0;
    uint8_t acc = 0;
    int nb = 0, o = 0;
    for (int k = 0; k < n && o < nbytes; k++) {
        for (int b = bps - 1; b >= 0 && o < nbytes; b--) {
            uint8_t bit = (uint8_t)((idx[k] >> b) & 1);
            if (prbs && pos < nprbs) bit ^= prbs[pos];
            pos++;
            acc = (uint8_t)((acc << 1) | bit);
            if (++nb == 8) { out[o++] = acc; acc = 0; nb = 0; }
        }
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

/* ------------------------------------------------------------------ matched filter
 * Full convolution of a complex signal by real taps (numpy.convolve(x, h, "full")):
 * out has nx + nh - 1 samples. */
void fir_cr(const float *x, int nx, const float *h, int nh, float *out)
{
    /* written as one multiply-accumulate pass per tap (no reduction): the compiler vectorizes
     * the inner loop without -ffast-math, so the result stays exactly reproducible */
    int ny = nx + nh - 1;
    memset(out, 0, sizeof(float) * 2 * ny);
    /* by blocks of output samples that stay in cache, taps in the same order: identical sums */
    const int CH = 2048;
    for (int c0 = 0; c0 < ny; c0 += CH) {
        int c1 = c0 + CH < ny ? c0 + CH : ny;
        for (int k = 0; k < nh; k++) {
            int i0 = c0 - k < 0 ? 0 : c0 - k, i1 = c1 - k < nx ? c1 - k : nx;
            if (i1 <= i0) continue;
            float hk = h[k];
            float *o = out + 2 * (i0 + k);
            const float *xi = x + 2 * i0;
            for (int i = 0; i < 2 * (i1 - i0); i++) o[i] += hk * xi[i];
        }
    }
}
