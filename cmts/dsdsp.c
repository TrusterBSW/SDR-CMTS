/* Downstream transmitter kernels (CMTS side), called through ctypes (GIL released).
 *
 *   j83a_encode : ITU-T J.83 Annex A, TS packets -> QAM symbols, bit-exact with the numpy
 *                 reference of cmts/j83a.py (randomizer, RS(204,188), Forney interleaver
 *                 I=12 M=17, byte -> m-tuple, differential coding of the 2 MSBs, mapping).
 *                 64- and 256-QAM; the state (interleaver memory, last rotation) lives in
 *                 buffers owned by the caller, so every channel has its own encoder.
 *   mix_add     : acc += x * e^{j phase}, phase advancing by `step` rad/sample and carried
 *                 across calls: frequency placement and sum of several channels (bonding).
 */
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define TS_LEN 188
#define RS_LEN 204
#define INTER_I 12
#define INTER_M 17
#define HIST (RS_LEN * (INTER_I - 1))      /* interleaver memory, bytes */

static uint8_t GF_EXP[512], GF_LOG[256];
static uint8_t RS_GT[256][16];             /* RS_GT[fb][j] = fb * g[j+1] */

static inline uint8_t gmul(uint8_t a, uint8_t b)
{
    return (a && b) ? GF_EXP[GF_LOG[a] + GF_LOG[b]] : 0;
}

__attribute__((constructor)) static void dsdsp_init(void)
{
    int x = 1;
    for (int i = 0; i < 255; i++) {
        GF_EXP[i] = (uint8_t)x;
        GF_LOG[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100) x ^= 0x11D;
    }
    for (int i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255];
    uint8_t g[17] = {1};                    /* roots lambda^0 .. lambda^15, g[0] = x^16 */
    for (int i = 0; i < 16; i++) {
        uint8_t r = GF_EXP[i];
        for (int j = i + 1; j > 0; j--) g[j] ^= gmul(g[j - 1], r);
    }
    for (int b = 0; b < 256; b++)
        for (int j = 0; j < 16; j++) RS_GT[b][j] = gmul((uint8_t)b, g[j + 1]);
}

/* quadrant rotation tables (EN 300 429 figures 7/8, same as cmts/j83a.py) */
static const uint8_t DROT_OF_AB[4] = {0, 3, 1, 2};
static const uint8_t IQ_OF_ROT[4] = {0, 2, 3, 1};

/* pkts: npkt x 188 bytes (npkt multiple of 8), rand_mask: 8 x 188 (sync bytes 0),
 * m: 6 (64-QAM) or 8 (256-QAM), constel: 2^m complex points (interleaved),
 * hist: HIST bytes of interleaver memory, rot: last rotation (in/out),
 * out: npkt*204*8/m complex symbols. Returns the number of symbols. */
int j83a_encode(const uint8_t *pkts, int npkt, const uint8_t *rand_mask, int m,
                const float *constel, uint8_t *hist, int *rot, float *out)
{
    if (npkt <= 0 || npkt % 8 || (m != 6 && m != 8)) return -1;
    int n = npkt * RS_LEN;
    uint8_t *x = malloc(HIST + n);           /* interleaver input: memory then new bytes */
    if (!x) return -2;
    memcpy(x, hist, HIST);
    uint8_t *cw = x + HIST;
    for (int p = 0; p < npkt; p++) {         /* randomizer + RS(204,188) */
        const uint8_t *in = pkts + p * TS_LEN, *mk = rand_mask + (p % 8) * TS_LEN;
        uint8_t *c = cw + p * RS_LEN;
        for (int i = 0; i < TS_LEN; i++) c[i] = in[i] ^ mk[i];
        if (p % 8 == 0) c[0] = 0xB8;         /* inverted sync of each 8-packet group */
        uint8_t reg[16] = {0};
        for (int i = 0; i < TS_LEN; i++) {   /* LFSR: shift by one byte and add fb * g */
            const uint8_t *gt = RS_GT[c[i] ^ reg[0]];
            for (int j = 0; j < 15; j++) reg[j] = reg[j + 1] ^ gt[j];
            reg[15] = gt[15];
        }
        memcpy(c + TS_LEN, reg, 16);
    }
    /* Forney interleaver on the byte stream: y[t] = x[t - 204 (t mod 12)], then symbols */
    int q = m - 2, r = *rot, ns = 0;
    unsigned mask = (1u << q) - 1, acc = 0;
    int nacc = 0;
    for (int t = 0; t < n; t++) {
        uint8_t y = x[HIST + t - RS_LEN * (t % INTER_I)];
        acc = (acc << 8) | y;
        nacc += 8;
        while (nacc >= m) {                   /* MSB first */
            unsigned s = (acc >> (nacc - m)) & ((1u << m) - 1);
            nacc -= m;
            r = (r + DROT_OF_AB[s >> q]) & 3;
            unsigned d = ((unsigned)IQ_OF_ROT[r] << q) | (s & mask);
            out[2 * ns] = constel[2 * d];
            out[2 * ns + 1] = constel[2 * d + 1];
            ns++;
        }
    }
    memcpy(hist, x + n, HIST);               /* last HIST bytes of the interleaver input */
    *rot = r;
    free(x);
    return ns;
}

/* acc[k] += x[k] * e^{j (phase + k step)}; *phase is advanced by n*step (mod 2 pi).
 * Phasors come from a per-call table of 1024 rotations times one exact start phasor per
 * block: no sample-to-sample dependency, so the inner loop vectorizes, and no drift. */
void mix_add(const float *x, int n, double step, double *phase, float *acc)
{
    float tr[1024], ti[1024];
    for (int k = 0; k < 1024; k++) { tr[k] = (float)cos(k * step); ti[k] = (float)sin(k * step); }
    double ph = *phase;
    for (int k0 = 0; k0 < n; k0 += 1024) {
        float zr = (float)cos(ph + k0 * step), zi = (float)sin(ph + k0 * step);
        int m = n - k0 < 1024 ? n - k0 : 1024;
        const float *xs = x + 2 * k0;
        float *as = acc + 2 * k0;
        for (int k = 0; k < m; k++) {
            float pr = zr * tr[k] - zi * ti[k], pi = zr * ti[k] + zi * tr[k];
            float xr = xs[2 * k], xi = xs[2 * k + 1];
            as[2 * k] += xr * pr - xi * pi;
            as[2 * k + 1] += xr * pi + xi * pr;
        }
    }
    *phase = fmod(ph + n * step, 2 * M_PI);
}

/* ------------------------------------------------------------------ multichannel synthesis
 * One channel of a bonded downstream in one pass: polyphase RRC interpolation of its symbols
 * (x = the ntaps-1 previous symbols then the n new ones, h = sps phases x ntaps), optional
 * conjugation (inverted spectrum), shift by `step` rad/sample (phase carried in *phase) and sum
 * into acc (n * sps complex samples). Same result as PulseShaper.process + conj + mix_add, without
 * the intermediate array (the 4-channel TX needed ~0.35 core per channel in two passes). */
__attribute__((optimize("fast-math"))) void shape_mix_add(const float *x, int n, const float *h, int sps, int ntaps, int conj,
                   double step, double *phase, float *acc)
{
    /* by chunks of 128 symbols: the polyphase filter writes a small buffer that stays in L1,
     * then the shift and sum run as a separate (vectorized) loop over it */
    enum { CHS = 128 };
    float buf[2 * CHS * 16];
    float tr[1024], ti[1024];
    if (sps > 16) return;
    for (int k = 0; k < 1024; k++) { tr[k] = (float)cos(k * step); ti[k] = (float)sin(k * step); }
    double ph = *phase;
    float csign = conj ? -1.f : 1.f;
    for (int i0 = 0; i0 < n; i0 += CHS) {
        int ni = n - i0 < CHS ? n - i0 : CHS;
        for (int i = 0; i < ni; i++) {
            const float *xs = x + 2 * (i0 + i + ntaps - 1);
            for (int p = 0; p < sps; p++) {
                const float *hp = h + p * ntaps;
                float re = 0.f, im = 0.f;
                for (int k = 0; k < ntaps; k++) {
                    re += hp[k] * xs[-2 * k];
                    im += hp[k] * xs[-2 * k + 1];
                }
                buf[2 * (i * sps + p)] = re;
                buf[2 * (i * sps + p) + 1] = im * csign;
            }
        }
        int m0 = i0 * sps, nm = ni * sps;
        for (int j0 = 0; j0 < nm; ) {                      /* table phasor, exact every 1024 */
            int m = m0 + j0, kb = m & 1023;
            int len = 1024 - kb < nm - j0 ? 1024 - kb : nm - j0;
            double a = ph + (m - kb) * step;
            float zr = (float)cos(a), zi = (float)sin(a);
            const float *bs = buf + 2 * j0;
            float *as = acc + 2 * m;
            for (int k = 0; k < len; k++) {
                float pr = zr * tr[kb + k] - zi * ti[kb + k], pi = zr * ti[kb + k] + zi * tr[kb + k];
                float xr = bs[2 * k], xi = bs[2 * k + 1];
                as[2 * k] += xr * pr - xi * pi;
                as[2 * k + 1] += xr * pi + xi * pr;
            }
            j0 += len;
        }
    }
    *phase = fmod(ph + (double)n * sps * step, 2 * M_PI);
}

/* acc *= scale, each component clipped to [-1, 1] (the numpy pass cost ~0.19 core at 41.7 MS/s) */
void scale_clip(float *acc, int n, float scale)
{
    for (int k = 0; k < 2 * n; k++) {
        float v = acc[k] * scale;
        acc[k] = v > 1.f ? 1.f : (v < -1.f ? -1.f : v);
    }
}

/* dst += src (n complex): sum of the partial channel groups computed in parallel */
void add_into(float *dst, const float *src, int n)
{
    for (int k = 0; k < 2 * n; k++) dst[k] += src[k];
}
