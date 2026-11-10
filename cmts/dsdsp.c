/* SPDX-License-Identifier: AGPL-3.0-only
 * Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md */
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
#include <errno.h>
#include <poll.h>
#include <unistd.h>
#ifdef __AVX2__
#include <immintrin.h>
#endif

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
#ifdef __AVX2__
/* AVX2 path for sps <= 8: the sps phases of one symbol read the same input samples, so one
 * 8-wide register holds them all (taps transposed to ht[k][p], zero beyond sps): 2 FMAs per tap
 * per symbol instead of 2*sps. Four symbols go at once (8 independent accumulators: the FMA
 * latency is hidden). The filtered samples land in planar buffers (re, im), then the shift and
 * the sum into acc (interleaved complex) run 8 samples at a time. 0.51 -> ~0.06 core per channel
 * at 8 samples per symbol on the i5-9500 (2026-10-10). */
static void shape_mix_add_avx2(const float *x, int n, const float *h, int sps, int ntaps, int conj,
                               double step, double *phase, float *acc)
{
    enum { CHS = 128 };
    float br[CHS * 8 + 8] __attribute__((aligned(32)));
    float bi[CHS * 8 + 8] __attribute__((aligned(32)));
    float tr[1024 + 8] __attribute__((aligned(32))), ti[1024 + 8] __attribute__((aligned(32)));
    float *ht = aligned_alloc(32, sizeof(float) * 8 * ntaps);
    if (!ht) return;
    for (int k = 0; k < ntaps; k++)
        for (int p = 0; p < 8; p++)
            ht[8 * k + p] = p < sps ? h[p * ntaps + k] : 0.f;
    for (int k = 0; k < 1024; k++) { tr[k] = (float)cos(k * step); ti[k] = (float)sin(k * step); }
    const __m256 cs = _mm256_set1_ps(conj ? -1.f : 1.f);
    double ph = *phase;
    for (int i0 = 0; i0 < n; i0 += CHS) {
        int ni = n - i0 < CHS ? n - i0 : CHS;
        /* x index of output symbol i, tap k: i0 + i + ntaps - 1 - k */
        const float *xb = x + 2 * (i0 + ntaps - 1);
        int i = 0;
        for (; i + 4 <= ni; i += 4) {
            __m256 r0 = _mm256_setzero_ps(), r1 = r0, r2 = r0, r3 = r0;
            __m256 m0 = r0, m1 = r0, m2 = r0, m3 = r0;
            const float *xs = xb + 2 * i;
            for (int k = 0; k < ntaps; k++) {
                __m256 hv = _mm256_load_ps(ht + 8 * k);
                const float *xk = xs - 2 * k;
                r0 = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xk), r0);
                m0 = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xk + 1), m0);
                r1 = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xk + 2), r1);
                m1 = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xk + 3), m1);
                r2 = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xk + 4), r2);
                m2 = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xk + 5), m2);
                r3 = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xk + 6), r3);
                m3 = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xk + 7), m3);
            }
            /* 8 lanes stored, sps valid: each next store overwrites the spare lanes */
            _mm256_storeu_ps(br + i * sps, r0);           _mm256_storeu_ps(bi + i * sps, _mm256_mul_ps(m0, cs));
            _mm256_storeu_ps(br + (i + 1) * sps, r1);     _mm256_storeu_ps(bi + (i + 1) * sps, _mm256_mul_ps(m1, cs));
            _mm256_storeu_ps(br + (i + 2) * sps, r2);     _mm256_storeu_ps(bi + (i + 2) * sps, _mm256_mul_ps(m2, cs));
            _mm256_storeu_ps(br + (i + 3) * sps, r3);     _mm256_storeu_ps(bi + (i + 3) * sps, _mm256_mul_ps(m3, cs));
        }
        for (; i < ni; i++) {
            __m256 r = _mm256_setzero_ps(), m = r;
            const float *xs = xb + 2 * i;
            for (int k = 0; k < ntaps; k++) {
                __m256 hv = _mm256_load_ps(ht + 8 * k);
                r = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xs - 2 * k), r);
                m = _mm256_fmadd_ps(hv, _mm256_broadcast_ss(xs - 2 * k + 1), m);
            }
            _mm256_storeu_ps(br + i * sps, r);
            _mm256_storeu_ps(bi + i * sps, _mm256_mul_ps(m, cs));
        }
        int m0 = i0 * sps, nm = ni * sps;
        for (int j0 = 0; j0 < nm; ) {                      /* table phasor, exact every 1024 */
            int mm = m0 + j0, kb = mm & 1023;
            int len = 1024 - kb < nm - j0 ? 1024 - kb : nm - j0;
            double a = ph + (mm - kb) * step;
            float zr = (float)cos(a), zi = (float)sin(a);
            __m256 vzr = _mm256_set1_ps(zr), vzi = _mm256_set1_ps(zi);
            float *as = acc + 2 * mm;
            int k = 0;
            for (; k + 8 <= len; k += 8) {
                __m256 t_r = _mm256_loadu_ps(tr + kb + k), t_i = _mm256_loadu_ps(ti + kb + k);
                __m256 pr = _mm256_fmsub_ps(vzr, t_r, _mm256_mul_ps(vzi, t_i));
                __m256 pi = _mm256_fmadd_ps(vzr, t_i, _mm256_mul_ps(vzi, t_r));
                __m256 xr = _mm256_loadu_ps(br + j0 + k), xi = _mm256_loadu_ps(bi + j0 + k);
                __m256 yr = _mm256_fmsub_ps(xr, pr, _mm256_mul_ps(xi, pi));
                __m256 yi = _mm256_fmadd_ps(xr, pi, _mm256_mul_ps(xi, pr));
                __m256 lo = _mm256_unpacklo_ps(yr, yi), hi = _mm256_unpackhi_ps(yr, yi);
                __m256 c0 = _mm256_permute2f128_ps(lo, hi, 0x20), c1 = _mm256_permute2f128_ps(lo, hi, 0x31);
                _mm256_storeu_ps(as + 2 * k, _mm256_add_ps(_mm256_loadu_ps(as + 2 * k), c0));
                _mm256_storeu_ps(as + 2 * k + 8, _mm256_add_ps(_mm256_loadu_ps(as + 2 * k + 8), c1));
            }
            for (; k < len; k++) {
                float pr = zr * tr[kb + k] - zi * ti[kb + k], pi = zr * ti[kb + k] + zi * tr[kb + k];
                float xr = br[j0 + k], xi = bi[j0 + k];
                as[2 * k] += xr * pr - xi * pi;
                as[2 * k + 1] += xr * pi + xi * pr;
            }
            j0 += len;
        }
    }
    free(ht);
    *phase = fmod(ph + (double)n * sps * step, 2 * M_PI);
}
#endif

__attribute__((optimize("fast-math"))) void shape_mix_add(const float *x, int n, const float *h, int sps, int ntaps, int conj,
                   double step, double *phase, float *acc)
{
    /* by chunks of 128 symbols: the polyphase filter writes a small buffer that stays in L1,
     * then the shift and sum run as a separate (vectorized) loop over it */
    enum { CHS = 128 };
    float buf[2 * CHS * 16];
    float tr[1024], ti[1024];
    if (sps > 16) return;
#ifdef __AVX2__
    if (sps <= 8) {
        shape_mix_add_avx2(x, n, h, sps, ntaps, conj, step, phase, acc);
        return;
    }
#endif
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

/* ------------------------------------------------------------------ MPEG-TS packing
 * MAC frames back to back -> TS packets of the DOCSIS PID, from a packet boundary: the same
 * packets as docsis_mac.TSPacketizer.pack (pointer_field when a frame starts in the packet,
 * a start on its last byte pushed to the next packet, the last packet stuffed with 0xFF).
 * data: the frames (n bytes), starts: offset of each frame (ns, increasing), *cc: continuity
 * counter (in/out), out: room for maxp packets. Returns the packets written, -1 if maxp is
 * too small. (2026-10-11: built byte string by byte string in Python, the TS packets of 6
 * channels took ~0.3-0.5 core of the MAC process.) */
#define DOCSIS_PID 0x1FFE
int ts_pack(const uint8_t *data, int n, const int *starts, int ns, int *cc, uint8_t *out, int maxp)
{
    int pos = 0, si = 0, np_ = 0, c = *cc;
    while (pos < n) {
        if (np_ >= maxp) return -1;
        uint8_t *pk = out + (long)TS_LEN * np_++;
        while (si < ns && starts[si] < pos) si++;
        int nxt = si < ns ? starts[si] - pos : -1;
        int pusi = nxt >= 0 && nxt < 183;
        pk[0] = 0x47;
        pk[1] = (pusi ? 0x40 : 0) | (DOCSIS_PID >> 8);
        pk[2] = DOCSIS_PID & 0xFF;
        pk[3] = 0x10 | c;
        c = (c + 1) & 0xF;
        int at, len;
        if (pusi) { pk[4] = (uint8_t)nxt; at = 5; len = 183; }
        else if (nxt == 183) { at = 4; len = 183; }       /* start on the last byte: stuffed */
        else { at = 4; len = 184; }
        int k = n - pos < len ? n - pos : len;
        memcpy(pk + at, data + pos, k);
        memset(pk + at + k, 0xFF, TS_LEN - at - k);
        pos += len;
    }
    *cc = c;
    return np_;
}

/* ------------------------------------------------------------------ TAP frames by batches
 * The frames waiting on a TAP file descriptor (opened O_NONBLOCK) in one call, without Python in
 * between: waits up to timeout_ms for the first one, then reads until none is left, maxn frames
 * or fewer than 2048 bytes of room. buf: the frames back to back, lens[i] the size of each.
 * Returns the number of frames, -1 on an error. (2026-10-15: a download starting after a quiet
 * spell put the sender's whole window into the TAP at once; read and handled frame by frame in
 * Python, the thread held the GIL for ~37 ms and the TS block encoder of the same process starved,
 * the radios underflowing together.) */
int tap_read_batch(int fd, uint8_t *buf, int cap, int32_t *lens, int maxn, int timeout_ms)
{
    struct pollfd p;
    p.fd = fd;
    p.events = POLLIN;
    p.revents = 0;
    int r = poll(&p, 1, timeout_ms);
    if (r <= 0)
        return (r < 0 && errno != EINTR) ? -1 : 0;
    int n = 0, used = 0;
    while (n < maxn && cap - used >= 2048) {
        ssize_t k = read(fd, buf + used, 2048);
        if (k < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
                break;
            return n ? n : -1;
        }
        if (k == 0)
            break;
        lens[n++] = (int32_t)k;
        used += (int)k;
    }
    return n;
}

/* ------------------------------------------------------------------ downstream data plane
 * The path of every downstream data frame, from the TAP batch to its TS packets, without Python
 * per frame (2026-11-10; before: TapFeed.pump -> BondedForwarder.send -> data_pdu(_dsid) ->
 * deque -> _data_batch with CoDel -> ts_pack, ~11 µs of Python and GIL per frame, and 2 cores for
 * 255 Mbit/s of small frames). Same rules as the Python it replaces (cmts/bridge.py,
 * cmts/downstream.py), which stays as the fallback:
 *   dp_push : frames -> Packet PDUs (DS-EHDR with the DSID and sequence number of a bonded
 *             modem, HCS, Ethernet FCS) on the channel chosen as BondedForwarder.send does;
 *   dp_take : the next batch of a channel (frames past the age limit and CoDel's dropped), packed
 *             into TS packets as ts_pack.
 * One thread pushes and takes (the TS block encoder), the routes are set from that same thread. */
#define DP_MAXCH 32
#define DP_RING (1 << 20)          /* bytes of PDUs per channel queue (~160 ms at 51 Mbit/s) */
#define DP_SLOTS 16384             /* frames per channel queue */
#define DP_HASH 2048               /* destination addresses (devices behind the modems) */
#define DP_MAXDSID 256

static uint32_t CRC32_T[8][256];
static uint16_t CCITT_T[256];

__attribute__((constructor)) static void dp_init(void)
{
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++)
            c = c & 1 ? (c >> 1) ^ 0xEDB88320u : c >> 1;
        CRC32_T[0][i] = c;
        uint16_t h = (uint16_t)i;
        for (int k = 0; k < 8; k++)
            h = h & 1 ? (h >> 1) ^ 0x8408 : h >> 1;
        CCITT_T[i] = h;
    }
    for (int i = 0; i < 256; i++)
        for (int t = 1; t < 8; t++)
            CRC32_T[t][i] = (CRC32_T[t - 1][i] >> 8) ^ CRC32_T[0][CRC32_T[t - 1][i] & 0xFF];
}

/* zlib.crc32 (slicing by 8) */
uint32_t dp_crc32(const uint8_t *p, int n)
{
    uint32_t c = 0xFFFFFFFFu;
    while (n >= 8) {
        uint32_t a, b;
        memcpy(&a, p, 4);
        memcpy(&b, p + 4, 4);
        a ^= c;
        c = CRC32_T[7][a & 0xFF] ^ CRC32_T[6][(a >> 8) & 0xFF] ^ CRC32_T[5][(a >> 16) & 0xFF] ^
            CRC32_T[4][a >> 24] ^ CRC32_T[3][b & 0xFF] ^ CRC32_T[2][(b >> 8) & 0xFF] ^
            CRC32_T[1][(b >> 16) & 0xFF] ^ CRC32_T[0][b >> 24];
        p += 8;
        n -= 8;
    }
    while (n--)
        c = (c >> 8) ^ CRC32_T[0][(c ^ *p++) & 0xFF];
    return c ^ 0xFFFFFFFFu;
}

/* docsis_mac.crc16_ccitt (the HCS) */
uint16_t dp_hcs(const uint8_t *p, int n)
{
    uint16_t c = 0xFFFF;
    while (n--)
        c = (c >> 8) ^ CCITT_T[(c ^ *p++) & 0xFF];
    return c ^ 0xFFFF;
}

typedef struct { double t; int off, len; } dp_slot;
typedef struct {           /* CoDel, as cmts/downstream.py CoDel */
    double target, interval, first_above, drop_next;
    int dropping, count, last_count;
} dp_codel;
typedef struct {
    uint8_t *ring;
    int head, tail;        /* bytes in use from head (oldest PDU) to tail */
    dp_slot *slot;
    int sh, n;             /* oldest slot, slots in use */
    int aqm;               /* CoDel on */
    dp_codel cd;
    double max_age;        /* s: a frame that waited longer is dropped (--ds-queue-ms) */
    long full;             /* frames refused: the queue was full */
} dp_chan;
typedef struct { uint8_t mac[6]; int used, dsid; uint32_t mask; } dp_route;
typedef struct {
    int nch;
    dp_chan ch[DP_MAXCH];
    int32_t *inflight;     /* TS packets / 8 of the batch each channel is sending (owned by Python) */
    int32_t *count;        /* frames queued per channel, kept here, read by Python without a call */
    /* the take buffers (dp_buffers, owned by Python): a take with few arguments, a ctypes call
     * costing ~0.3 µs per argument */
    uint8_t *out, *raw;
    int maxp, rawcap, maxf, cc;
    int32_t *flen, res[2];
    double wmax;
    dp_route tab[DP_HASH];
    int bonded;            /* bonded modems: none -> everything on the primary without DSID */
    int def_dsid;          /* destination not learned: DSID (-1: primary, no DSID) and channels */
    uint32_t def_mask;
    int dsid[DP_MAXDSID];
    uint16_t seq[DP_MAXDSID];
    int ndsid, last, force_ch;
    long per_chan[DP_MAXCH];
} dplane;

void *dp_new(int nch, int32_t *inflight, int32_t *count)
{
    if (nch < 1 || nch > DP_MAXCH)
        return NULL;
    dplane *p = calloc(1, sizeof(dplane));
    if (!p)
        return NULL;
    p->nch = nch;
    p->inflight = inflight;
    p->count = count;
    p->last = -1;
    p->force_ch = -1;
    p->def_dsid = -1;
    for (int i = 0; i < nch; i++) {
        p->ch[i].ring = malloc(DP_RING);
        p->ch[i].slot = malloc(sizeof(dp_slot) * DP_SLOTS);
        p->ch[i].max_age = 0.1;
        if (!p->ch[i].ring || !p->ch[i].slot) {
            for (int k = 0; k <= i; k++) {
                free(p->ch[k].ring);
                free(p->ch[k].slot);
            }
            free(p);
            return NULL;
        }
    }
    return p;
}

void dp_free(void *vp)
{
    dplane *p = vp;
    if (!p)
        return;
    for (int i = 0; i < p->nch; i++) {
        free(p->ch[i].ring);
        free(p->ch[i].slot);
    }
    free(p);
}

/* channel ch: age limit (s), CoDel target and interval (s; target 0: no CoDel) */
void dp_config(void *vp, int ch, double max_age, double target, double interval)
{
    dplane *p = vp;
    if (ch < 0 || ch >= p->nch)
        return;
    dp_chan *c = &p->ch[ch];
    c->max_age = max_age;
    c->aqm = target > 0;
    c->cd.target = target;
    c->cd.interval = interval;
}

void dp_force_channel(void *vp, int ch) { ((dplane *)vp)->force_ch = ch; }

void dp_buffers(void *vp, uint8_t *out, int maxp, uint8_t *raw, int rawcap, int32_t *flen, int maxf)
{
    dplane *p = vp;
    p->out = out;
    p->maxp = maxp;
    p->raw = raw;
    p->rawcap = rawcap;
    p->flen = flen;
    p->maxf = maxf;
}

/* around dp_take: the continuity counter in and out; frames sent, dropped, longest wait (ms) */
void dp_set_cc(void *vp, int cc) { ((dplane *)vp)->cc = cc; }
int dp_get_cc(void *vp) { return ((dplane *)vp)->cc; }
void dp_result(void *vp, int32_t *res, double *wmax)
{
    dplane *p = vp;
    res[0] = p->res[0];
    res[1] = p->res[1];
    *wmax = p->wmax;
}

static unsigned dp_hash(const uint8_t *m)
{
    uint32_t h = 2166136261u;
    for (int i = 0; i < 6; i++)
        h = (h ^ m[i]) * 16777619u;
    return h & (DP_HASH - 1);
}

/* the routes, rebuilt by the forwarder when they change: clear, then one dp_route_set per address */
void dp_routes_clear(void *vp, int bonded, int def_dsid, uint32_t def_mask)
{
    dplane *p = vp;
    memset(p->tab, 0, sizeof p->tab);
    p->bonded = bonded;
    p->def_dsid = def_dsid;
    p->def_mask = def_mask;
}

/* destination address -> DSID (-1: the primary channel, no DSID) and its channels (bit i: index i).
 * Returns 0, -1 when the table is full. */
int dp_route_set(void *vp, const uint8_t *mac, int dsid, uint32_t mask)
{
    dplane *p = vp;
    unsigned h = dp_hash(mac);
    for (int k = 0; k < DP_HASH; k++, h = (h + 1) & (DP_HASH - 1)) {
        dp_route *r = &p->tab[h];
        if (!r->used || !memcmp(r->mac, mac, 6)) {
            memcpy(r->mac, mac, 6);
            r->used = 1;
            r->dsid = dsid;
            r->mask = mask;
            return 0;
        }
    }
    return -1;
}

static const dp_route *dp_route_get(const dplane *p, const uint8_t *mac)
{
    unsigned h = dp_hash(mac);
    for (int k = 0; k < DP_HASH; k++, h = (h + 1) & (DP_HASH - 1)) {
        const dp_route *r = &p->tab[h];
        if (!r->used)
            return NULL;
        if (!memcmp(r->mac, mac, 6))
            return r;
    }
    return NULL;
}

static uint16_t *dp_seq_of(dplane *p, int dsid)
{
    for (int i = 0; i < p->ndsid; i++)
        if (p->dsid[i] == dsid)
            return &p->seq[i];
    if (p->ndsid >= DP_MAXDSID)
        return &p->seq[DP_MAXDSID - 1];
    p->dsid[p->ndsid] = dsid;
    p->seq[p->ndsid] = 0;
    return &p->seq[p->ndsid++];
}

/* next sequence number of a DSID (read by the Python forwarder when it takes over again) */
int dp_seq_get(void *vp, int dsid)
{
    dplane *p = vp;
    for (int i = 0; i < p->ndsid; i++)
        if (p->dsid[i] == dsid)
            return p->seq[i];
    return 0;
}

static uint8_t *dp_alloc(dp_chan *c, int len, double t, int32_t *count)
{
    if (c->n >= DP_SLOTS)
        return NULL;
    if (c->n == 0)
        c->head = c->tail = 0;
    int off;
    if (c->tail >= c->head) {              /* free: [tail, end) and [0, head) */
        if (DP_RING - c->tail >= len)
            off = c->tail;
        else if (c->head > len)
            off = 0;
        else
            return NULL;
    } else if (c->head - c->tail > len) {  /* wrapped: free [tail, head) */
        off = c->tail;
    } else {
        return NULL;
    }
    c->tail = off + len;
    dp_slot *s = &c->slot[(c->sh + c->n) % DP_SLOTS];
    s->t = t;
    s->off = off;
    s->len = len;
    c->n++;
    if (count)
        *count = c->n;
    return c->ring + off;
}

static void dp_pop(dp_chan *c, int32_t *count)
{
    c->sh = (c->sh + 1) % DP_SLOTS;
    if (--c->n == 0)
        c->head = c->tail = 0;
    else
        c->head = c->slot[c->sh].off;
    if (count)
        *count = c->n;
}

/* the channel of a bonded frame: the usable ones in turn after the last used, skipping one more
 * than 2 frames above the least loaded (BondedForwarder.send) */
static int dp_pick(dplane *p, uint32_t mask)
{
    int n = p->nch, lo = 1 << 30, best = -1, bkey = 1 << 30;
    int load[DP_MAXCH];
    mask &= n >= 32 ? 0xFFFFFFFFu : ((1u << n) - 1);
    if (!mask)
        mask = 1;
    for (int i = 0; i < n; i++)
        if (mask >> i & 1) {
            load[i] = p->ch[i].n + (p->inflight ? p->inflight[i] : 0);
            if (load[i] < lo)
                lo = load[i];
        }
    for (int i = 0; i < n; i++)
        if (mask >> i & 1) {
            int key = (load[i] > lo + 2) * n + (((i - p->last - 1) % n) + n) % n;
            if (key < bkey) {
                bkey = key;
                best = i;
            }
        }
    p->last = best;
    return p->force_ch >= 0 && p->force_ch < n ? p->force_ch : best;
}

/* Frames lens[st[0]..n) of a TAP batch (data, from byte st[1]), arrived at t (perf_counter s), into
 * PDUs on their channels, until max_bytes of frames; st (frame index, byte offset) advanced.
 * Returns the bytes taken; out[0] += frames queued, out[1] += frames refused (queue full). */
int dp_push(void *vp, const uint8_t *data, const int32_t *lens, int n, int32_t *st, double t, int max_bytes,
            long *out)
{
    dplane *p = vp;
    int i = st[0], off = st[1], taken = 0;
    while (i < n && taken < max_bytes) {
        const uint8_t *eth = data + off;
        int len = lens[i];
        off += len;
        i++;
        taken += len;
        int dsid = -1;
        uint32_t mask = 1;
        if (p->nch >= 2 && p->bonded && len >= 6 && !(eth[0] & 1)) {
            const dp_route *r = dp_route_get(p, eth);
            dsid = r ? r->dsid : p->def_dsid;
            mask = r ? r->mask : p->def_mask;
        }
        int k = dsid < 0 ? 0 : dp_pick(p, mask);
        int eh = dsid < 0 ? 0 : 6, plen = 6 + eh + len + 4;
        uint8_t *d = dp_alloc(&p->ch[k], plen, t, p->count ? &p->count[k] : NULL);
        if (!d) {
            p->ch[k].full++;
            out[1]++;
            continue;
        }
        int ln = eh + len + 4;
        d[0] = eh ? 0x01 : 0x00;
        d[1] = (uint8_t)eh;
        d[2] = (uint8_t)(ln >> 8);
        d[3] = (uint8_t)ln;
        if (eh) {
            uint16_t *sq = dp_seq_of(p, dsid);
            uint16_t s = *sq;
            *sq = (uint16_t)(s + 1);
            d[4] = 0x85;
            d[5] = (uint8_t)((dsid >> 16) & 0xF);
            d[6] = (uint8_t)(dsid >> 8);
            d[7] = (uint8_t)dsid;
            d[8] = (uint8_t)(s >> 8);
            d[9] = (uint8_t)s;
        }
        uint16_t h = dp_hcs(d, 4 + eh);
        d[4 + eh] = (uint8_t)h;
        d[5 + eh] = (uint8_t)(h >> 8);
        memcpy(d + 6 + eh, eth, len);
        uint32_t f = dp_crc32(eth, len);
        memcpy(d + 6 + eh + len, &f, 4);   /* little endian, as struct.pack("<I") (x86, arm64) */
        p->per_chan[k]++;
        out[0]++;
    }
    st[0] = i;
    st[1] = off;
    return taken;
}

static int dp_codel_drop(dp_codel *c, double sojourn, double now, int backlog)
{
    int ok = 0;
    if (sojourn < c->target || backlog <= 1)
        c->first_above = 0.0;
    else if (c->first_above == 0.0)
        c->first_above = now + c->interval;
    else if (now >= c->first_above)
        ok = 1;
    if (c->dropping) {
        if (!ok) {
            c->dropping = 0;
        } else if (now >= c->drop_next) {
            c->count++;
            c->drop_next += c->interval / sqrt((double)c->count);
            return 1;
        }
        return 0;
    }
    if (ok) {
        c->dropping = 1;
        int delta = c->count - c->last_count;
        c->count = (delta > 1 && now - c->drop_next < 16 * c->interval) ? delta : 1;
        c->last_count = c->count;
        c->drop_next = now + c->interval / sqrt((double)c->count);
        return 1;
    }
    return 0;
}

/* The next batches of channel ch (DownstreamMux._data_batch), back to back while fewer than
 * `limit` packets are built (the packet where a SYNC / MAP / MDD falls due, or the end of the
 * block: each batch starts where the Python loop would have started it). A batch: frames past the
 * age limit or dropped by CoDel skipped, then frames until pack_bytes, gathered into raw (their
 * sizes into flen, for a pcap) and packed into TS packets (out, at most maxp; continuity counter
 * cc). Returns the packets (0: no frame); then dp_result: frames sent, dropped, the longest wait
 * (ms) of the frames sent. Buffers: dp_buffers; continuity counter: dp_set_cc / dp_get_cc. */
int dp_take(void *vp, int ch, double now, int pack_bytes, int limit)
{
    dplane *p = vp;
    dp_chan *c = &p->ch[ch];
    int32_t *cnt = p->count ? &p->count[ch] : NULL, *res = p->res, *flen = p->flen;
    int *cc = &p->cc;
    uint8_t *out = p->out, *raw = p->raw;
    int maxp = p->maxp, rawcap = p->rawcap, maxf = p->maxf;
    double *wmax = &p->wmax;
    int np_ = 0, nf = 0, used = 0;
    res[0] = res[1] = 0;
    wmax[0] = 0.0;
    while (np_ < limit && c->n) {
        int b0 = nf, size = 0, starts[512];
        uint8_t *r = raw + used;
        while (c->n) {
            dp_slot *s = &c->slot[c->sh];
            double wait = now - s->t;
            if (wait > c->max_age || (c->aqm && dp_codel_drop(&c->cd, wait, now, c->n))) {
                dp_pop(c, cnt);
                res[1]++;
                continue;
            }
            int k = nf - b0;
            if (k && (size + s->len > pack_bytes || k >= 512))
                break;
            /* room for this frame and the packets of the batch so far: else the batch ends here */
            if (used + size + s->len > rawcap || nf >= maxf ||
                np_ + (size + s->len) / 183 + 2 > maxp)
                break;
            memcpy(r + size, c->ring + s->off, s->len);
            starts[k] = size;
            flen[nf++] = s->len;
            size += s->len;
            if (wait * 1e3 > wmax[0])
                wmax[0] = wait * 1e3;
            dp_pop(c, cnt);
        }
        if (nf == b0)
            break;
        int k = ts_pack(r, size, starts, nf - b0, cc, out + (long)TS_LEN * np_, maxp - np_);
        if (k < 0)
            return -1;
        np_ += k;
        used += size;
    }
    res[0] = nf;
    return np_;
}

/* frames queued on channel ch; refused (queue full) */
int dp_queued(void *vp, int ch) { return ((dplane *)vp)->ch[ch].n; }
long dp_full(void *vp, int ch) { return ((dplane *)vp)->ch[ch].full; }
long dp_per_chan(void *vp, int ch) { return ((dplane *)vp)->per_chan[ch]; }
