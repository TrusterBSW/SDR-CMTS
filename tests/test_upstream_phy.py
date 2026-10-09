"""Upstream burst PHY: modulate RNG-REQ / REQ bursts like a modem, add channel impairments,
demodulate with BurstDemod. Checks decoding, arrival time, frequency and power estimates."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import numpy as np
from cmts import docsis_mac as mac
from cmts import us_phy

FS = 27.808e6 / 8         # B210 rx rate used by mini_cmts.py (master clock / 8)
SYMRATE = 1.28e6
CMTS = bytes.fromhex("0200 00ca fe01".replace(" ", ""))
CM = bytes.fromhex("0024d1123456")
rng = np.random.default_rng(1)
demod = us_phy.BurstDemod(FS, SYMRATE)
prof = us_phy.profiles()


def channel(sym, delay, freq, snr_db, amp, n=6000, phase=None):
    x = us_phy.synth(sym, FS / SYMRATE, delay, n) * amp
    x *= np.exp(1j * (2 * np.pi * freq * np.arange(n) / FS + (rng.uniform(0, 2 * np.pi) if phase is None else phase)))
    nz = amp / np.sqrt(2) * 10 ** (-snr_db / 20)
    return (x + nz * (rng.normal(size=n) + 1j * rng.normal(size=n))).astype(np.complex64)


def run(name, frame, iuc, snr, freq, n=200, bitrev=False):
    sym = us_phy.encode_burst(prof[iuc], frame if not bitrev else bytes(us_phy._BITREV[np.frombuffer(frame, np.uint8)]))
    ok = 0
    terr, ferr, perr, mers = [], [], [], []
    for _ in range(n):
        delay = rng.uniform(300, 3000)
        amp = 10 ** (rng.uniform(-40, -10) / 20)
        b = demod.detect(channel(sym, delay, freq, snr, amp), iuc)
        if b is None:
            continue
        ok += b.frame == frame
        terr.append(b.t - delay)
        ferr.append(b.freq - freq)
        perr.append(b.power_db - 20 * np.log10(amp))
        mers.append(b.mer)
    terr = np.array(terr) / FS * 1e9
    print(f"{name:28s} SNR {snr:4.1f} dB f {freq:+6.0f} Hz: decoded {ok}/{n}, timing err "
          f"{np.mean(terr):+5.1f} ± {np.std(terr):4.1f} ns (max {np.max(np.abs(terr)):5.1f}), freq err "
          f"{np.mean(ferr):+5.0f} ± {np.std(ferr):4.0f} Hz, power err {np.mean(perr):+.2f} dB, MER {np.mean(mers):.1f} dB")
    return ok / n, np.max(np.abs(terr))


rng_req = mac.rng_req_msg(CM, CMTS, 0, 1)
assert len(rng_req) == 34, len(rng_req)
req = mac.req_frame(0x123, 17)
res = [run("RNG-REQ (IM)", rng_req, mac.IUC_INIT_MAINT, 30, 1500),
       run("RNG-REQ (IM)", rng_req, mac.IUC_INIT_MAINT, 15, -4000),
       run("RNG-REQ (IM)", rng_req, mac.IUC_INIT_MAINT, 10, 8000),
       run("RNG-REQ, LSB-first bytes", rng_req, mac.IUC_INIT_MAINT, 25, 0, n=50, bitrev=True),
       run("REQ (no FEC)", req, mac.IUC_REQUEST, 25, 2000),
       run("REQ (no FEC)", req, mac.IUC_REQUEST, 15, 2000)]
# noise only: no false decode
fa = sum(1 for _ in range(100) if (b := demod.detect((rng.normal(size=6000) + 1j * rng.normal(size=6000)).astype(np.complex64) * 0.01,
                                                     mac.IUC_INIT_MAINT)) is not None and b.frame is not None)
print("false decodes on noise only:", fa, "/ 100")
good = all(r[0] >= 0.99 for r in res[:2]) and res[4][0] >= 0.99 and res[0][1] < 50 and fa == 0
print("PASS" if good else "FAIL")
