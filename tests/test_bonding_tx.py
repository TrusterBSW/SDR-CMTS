"""Two bonded downstream channels as mini_cmts.py --ds-channels 2 builds them (each shaped at 4
samples per symbol, conjugated like the single channel, shifted to -4 / +4 MHz around the TX
centre, summed), then each channel taken back alone with the J.83A receiver: every TS packet must
come out as transmitted (primary: SYNC/UCD/MAP/MDD, secondary: MDD + data + null)."""
import os, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cmts import cdsp, docsis_mac as mac
from cmts import rx_j83a as rx
from cmts.downstream import DownstreamMux, SecondaryMux, SYMRATE
from cmts.j83a import PulseShaper, rrc_taps

QAM = 256
fs = 4 * SYMRATE
chans = [(1, 306e6), (2, 314e6)]
centre = 310e6
mux = DownstreamMux(qam=QAM, sps=4)
sec = SecondaryMux(qam=QAM)
plan = [(c, f, QAM, c == 1) for c, f in chans]
mux.mdd = mac.mdd_msg(mux.sa, 1, 1, plan, [1])
sec.mdd = mac.mdd_msg(mux.sa, 1, 2, plan, [1])
mux.mdd_iv = int(0.02 * 10.24e6)                  # MDD every 20 ms here (1 s on the air) so the
sec.mdd_every = 600                               # short capture holds several of them
for i in range(300):
    sec.send_data(mac.data_pdu(bytes([i % 256]) * 300))
shapers = [PulseShaper(4), PulseShaper(4)]
steps = [2 * np.pi * (f - centre) / fs for _, f in chans]
ph = [np.zeros(1), np.zeros(1)]
P = [[], []]
X = []
for _ in range(40):
    pk = [mux.packets(256), sec.packets(256)]
    acc = None
    for k in range(2):
        P[k].append(pk[k])
        y = np.conj(shapers[k].process((mux if k == 0 else sec).enc.encode(pk[k])))
        acc = np.zeros(y.size, np.complex64) if acc is None else acc
        cdsp.mix_add(y, steps[k], ph[k], acc)
    X.append(acc * 0.3 / np.sqrt(2))
x = np.concatenate(X)
ok_all = True
for k, (cid, f) in enumerate(chans):
    z = np.conj(rx.channelize(x, fs, f - centre))         # take the channel back, undo the inversion
    z = np.convolve(z, rrc_taps(4, 16)).astype(np.complex64)
    s, _ = rx.timing_recover(z)
    s, _ = rx.freq_correct(s)
    sym, _, mer = rx.equalize(s, QAM)
    d = rx.diff_decode(sym, 8)
    b = rx.symbols_to_bytes(d, 8, 0)
    off, _ = rx.find_sync(b)
    ts = rx.outer_decode(b[off:]) if off is not None else np.zeros((0, 188), np.uint8)
    from cmts.ts_demux import DocsisDemux
    dm = DocsisDemux()
    for t in ts:
        dm.push_packet(t)
    kinds = {}
    for fr in dm.frames[8:]:                         # the first frames: receiver still converging
        info = mac.parse_us_frame(fr)                   # HCS / CRC check of every MAC frame
        key = "HCS faux" if not info else (f"gestion type {fr[24]}" if info["kind"] == "MGMT" else info["kind"])
        kinds[key] = kinds.get(key, 0) + 1
    mdd_dcid = {fr[29] for fr in dm.frames if len(fr) > 30 and fr[0] == 0xC2 and fr[24] == mac.MDD}
    want = {"gestion type 33"} | ({"gestion type 1", "gestion type 3"} if cid == 1 else {"DATA"})
    good = mer > 30 and "HCS faux" not in kinds and want <= set(kinds) and mdd_dcid == {cid}
    ok_all &= good
    print(f"{'OK ' if good else 'KO '} canal {cid} ({f / 1e6:.0f} MHz) : MER {mer:.1f} dB, trames DOCSIS {kinds}, "
          f"MDD annonçant le canal {sorted(mdd_dcid)}")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
