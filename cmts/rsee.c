/* Reed-Solomon errors-and-erasures decoding with soft reliabilities, for weak downstream captures
 * (origin_listen.py: a HackRF tops out at ~25 dB MER, where hard decoding of 256-QAM loses ~27 % of
 * the MPEG packets). GF(256), p = 0x11D, roots alpha^fcr .. alpha^(fcr+nsym-1) (J.83 annex A:
 * fcr 0, nsym 16; the same code as the upstream's, cmts/usdsp.c).
 *
 *   rs_decode_ee(cw, n, nsym, fcr, era, nera)   erasures = byte indexes in cw; 2e + f <= nsym
 *   rs_decode_soft(cws, ncw, n, nsym, fcr, rel, maxera, status)
 *       every codeword: errors only, then erasing its 2, 4, ... maxera least reliable bytes until
 *       one decodes. status[i] = number of erasures used (0 = hard decoding), -1 = failed.
 */
#include <stdint.h>
#include <string.h>

static uint8_t GF_EXP[512];
static uint8_t GF_LOG[256];
static int gf_ready = 0;

static void gf_init(void)
{
    int x = 1;
    for (int i = 0; i < 255; i++) {
        GF_EXP[i] = (uint8_t)x;
        GF_LOG[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100) x ^= 0x11D;
    }
    for (int i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255];
    gf_ready = 1;
}

static inline uint8_t gmul(uint8_t a, uint8_t b)
{
    return (a && b) ? GF_EXP[GF_LOG[a] + GF_LOG[b]] : 0;
}

static inline uint8_t gdiv(uint8_t a, uint8_t b)
{
    return a ? GF_EXP[(GF_LOG[a] + 255 - GF_LOG[b]) % 255] : 0;
}

static inline uint8_t gpow(int e)
{
    e %= 255;
    if (e < 0) e += 255;
    return GF_EXP[e];
}

/* p(x) at x, coefficients lowest degree first */
static inline uint8_t peval(const uint8_t *p, int deg, uint8_t x)
{
    uint8_t y = 0;
    for (int i = deg; i >= 0; i--) y = gmul(y, x) ^ p[i];
    return y;
}

int rs_decode_ee(uint8_t *cw, int n, int nsym, int fcr, const int *era, int nera)
{
    if (!gf_ready) gf_init();
    if (nsym <= 0 || nsym > 64 || n <= nsym || n > 255 || nera > nsym) return -5;
    uint8_t S[64];
    int any = 0;
    for (int i = 0; i < nsym; i++) {                 /* S_i = r(alpha^(fcr+i)), cw[0] = x^(n-1) */
        uint8_t r = gpow(fcr + i), y = 0;
        for (int j = 0; j < n; j++) y = gmul(y, r) ^ cw[j];
        S[i] = y;
        any |= y;
    }
    if (!any) return 0;
    /* erasure locator G(x) = prod (1 - X_k x), X_k = alpha^(n-1-idx) */
    uint8_t G[65] = {1};
    int f = 0;
    for (int k = 0; k < nera; k++) {
        uint8_t X = gpow(n - 1 - era[k]);
        for (int i = f + 1; i >= 1; i--) G[i] ^= gmul(G[i - 1], X);
        f++;
    }
    /* modified syndromes T = S G mod x^nsym; T_f .. T_(nsym-1) follow the error locator */
    uint8_t T[64];
    for (int i = 0; i < nsym; i++) {
        uint8_t t = 0;
        for (int j = 0; j <= f && j <= i; j++) t ^= gmul(G[j], S[i - j]);
        T[i] = t;
    }
    uint8_t C[65] = {1}, B[65] = {1}, Tmp[65];
    int L = 0, m = 1, len = nsym - f;
    uint8_t b = 1;
    for (int k = 0; k < len; k++) {                  /* Berlekamp-Massey on T_f.. */
        uint8_t d = T[f + k];
        for (int i = 1; i <= L; i++) d ^= gmul(C[i], T[f + k - i]);
        if (!d) { m++; continue; }
        memcpy(Tmp, C, sizeof C);
        uint8_t coef = gdiv(d, b);
        for (int i = 0; i + m <= 64; i++) C[i + m] ^= gmul(coef, B[i]);
        if (2 * L <= k) { L = k + 1 - L; memcpy(B, Tmp, sizeof B); b = d; m = 1; }
        else m++;
    }
    if (2 * L + f > nsym) return -1;
    /* errata locator P = C G */
    uint8_t P[65];
    memset(P, 0, sizeof P);
    int deg = L + f;
    for (int i = 0; i <= L; i++)
        if (C[i])
            for (int j = 0; j <= f; j++) P[i + j] ^= gmul(C[i], G[j]);
    int pos[64], npos = 0;                            /* Chien: degree j in error if P(alpha^-j) = 0 */
    for (int j = 0; j < n; j++)
        if (!peval(P, deg, gpow(-j))) {
            if (npos == 64) return -2;
            pos[npos++] = j;
        }
    if (npos != deg) return -2;
    uint8_t O[64];                                    /* Omega = S P mod x^nsym */
    for (int i = 0; i < nsym; i++) {
        uint8_t o = 0;
        for (int j = 0; j <= deg && j <= i; j++) o ^= gmul(P[j], S[i - j]);
        O[i] = o;
    }
    for (int q = 0; q < npos; q++) {                  /* Forney */
        int j = pos[q];
        uint8_t xi = gpow(-j), num = peval(O, nsym - 1, xi), den = 0;
        for (int i = 1; i <= deg; i += 2) if (P[i]) den ^= gmul(P[i], gpow(-j * (i - 1)));
        if (!den) return -3;
        cw[n - 1 - j] ^= gmul(gdiv(num, den), gpow(j * (1 - fcr)));
    }
    for (int i = 0; i < nsym; i++) {
        uint8_t r = gpow(fcr + i), y = 0;
        for (int j = 0; j < n; j++) y = gmul(y, r) ^ cw[j];
        if (y) return -4;
    }
    return npos;
}

int rs_decode_soft(uint8_t *cws, int ncw, int n, int nsym, int fcr, const float *rel, int maxera,
                   int *status)
{
    int ok = 0;
    uint8_t keep[255];
    for (int c = 0; c < ncw; c++) {
        uint8_t *cw = cws + (long)c * n;
        const float *r = rel + (long)c * n;
        memcpy(keep, cw, n);
        status[c] = -1;
        if (rs_decode_ee(cw, n, nsym, fcr, 0, 0) >= 0) { status[c] = 0; ok++; continue; }
        /* the maxera least reliable bytes, most doubtful first (insertion into a short list) */
        int idx[64], cnt = 0;
        for (int j = 0; j < n; j++) {
            if (cnt < maxera) cnt++;
            else if (r[j] >= r[idx[cnt - 1]]) continue;
            int k = cnt - 1;
            while (k > 0 && r[idx[k - 1]] > r[j]) { idx[k] = idx[k - 1]; k--; }
            idx[k] = j;
        }
        for (int f = 2; f <= cnt; f += 2) {
            memcpy(cw, keep, n);
            if (rs_decode_ee(cw, n, nsym, fcr, idx, f) >= 0) { status[c] = f; ok++; break; }
        }
        if (status[c] < 0) memcpy(cw, keep, n);
    }
    return ok;
}
