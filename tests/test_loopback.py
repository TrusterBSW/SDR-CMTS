# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""End-to-end check: modulate with DownstreamMux, then demodulate with an
independent receiver chain (spec Boolean differential decoder written literally,
gr-dtv deinterleaver + RS decoder + descrambler), extract DOCSIS frames and
dissect them with tshark."""
import os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import numpy as np
from gnuradio import gr, blocks, dtv
from cmts.downstream import DownstreamMux
from cmts import j83a
from cmts.ts_demux import DocsisDemux, write_pcap

QAM = int(sys.argv[1]) if len(sys.argv) > 1 else 64
SPS = 2
mux = DownstreamMux(qam=QAM, sps=SPS)
m = mux.m
q = m - 2
ts, syms, samp = [], [], []
for _ in range(16):
    P = mux.packets(256)
    s = mux.enc.encode(P)
    ts.append(P); syms.append(s); samp.append(mux.shaper.process(s))
ts = np.concatenate(ts); tx_syms = np.concatenate(syms); x = np.concatenate(samp)
print(f"{QAM}-QAM: {ts.shape[0]} TS packets, {x.size} samples")

# --- channel: small noise
rng = np.random.default_rng(0)
x = x + (rng.normal(size=x.size) + 1j * rng.normal(size=x.size)) * 0.01

# --- matched filter + timing (delay found by correlation with the first symbols)
h = j83a.rrc_taps(SPS, 12)
y = np.convolve(x, h)
best = max(range(0, 200), key=lambda d: abs(np.vdot(y[d:d + 2000 * SPS:SPS], tx_syms[:2000])))
r = y[best::SPS][:tx_syms.size]
r /= np.sqrt(np.mean(np.abs(r) ** 2))
const = j83a.qam_table(m)
idx = np.concatenate([np.argmin(np.abs(c[:, None] - const[None, :]), axis=1)  # hard decision
                      for c in np.array_split(r, max(1, r.size // 20000))])
print("symbol decision errors vs tx:", np.count_nonzero(np.abs(const[idx] - tx_syms) > 1e-3))

# --- differential decoding, literally inverting the EN 300 429 Boolean expression
def enc_bool(A, B, Ip, Qp):
    x = A ^ B
    I = ((1 - x) & (A ^ Ip)) | (x & (A ^ Qp))
    Q = ((1 - x) & (B ^ Qp)) | (x & (B ^ Ip))
    return I, Q
LUT = {}
for Ip in (0, 1):
    for Qp in (0, 1):
        for A in (0, 1):
            for B in (0, 1):
                LUT[(Ip, Qp) + enc_bool(A, B, Ip, Qp)] = (A << 1) | B
Ik = (idx >> (q + 1)) & 1
Qk = (idx >> q) & 1
ab = np.empty_like(idx)
Ip, Qp = 0, 0
for k in range(idx.size):
    ab[k] = LUT[(Ip, Qp, Ik[k], Qk[k])]
    Ip, Qp = Ik[k], Qk[k]
sym = (ab << q) | (idx & ((1 << q) - 1))
bits = ((sym[:, None] >> np.arange(m - 1, -1, -1)) & 1).astype(np.uint8)
rx_bytes = np.packbits(bits.ravel())

# --- gr-dtv: deinterleave, RS decode, descramble
tb = gr.top_block()
src = blocks.vector_source_b(rx_bytes.tolist(), False)
dil = dtv.dvbt_convolutional_deinterleaver(136, 12, 17)
rsd = dtv.dvbt_reed_solomon_dec(2, 8, 0x11d, 255, 239, 8, 51, 8)
des = dtv.dvbt_energy_descramble(8)
snk = blocks.vector_sink_b()
tb.connect(src, dil, rsd, des, snk)
tb.run()
out = np.array(snk.data(), dtype=np.uint8)
out = out[:out.size // 188 * 188].reshape(-1, 188)
# align with the transmitted TS
ok_sync = np.mean(out[:, 0] == 0x47)
off = next(o for o in range(0, 64) if np.array_equal(out[0, 4:], ts[o, 4:]) or
           np.array_equal(out[1, 4:], ts[o + 1, 4:]))
n = min(out.shape[0], ts.shape[0] - off) - 1
match = np.mean([np.array_equal(out[i, 1:], ts[off + i, 1:]) for i in range(n)])
print(f"TS sync bytes ok: {ok_sync:.3f}, packets equal to transmitted: {match:.4f} ({n} pkts, offset {off})")

# --- DOCSIS frames
dm = DocsisDemux()
for p in out:
    dm.push_packet(p)
pcap = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"loopback_{QAM}.pcap")
write_pcap(pcap, dm.frames)
print("DOCSIS frames:", len(dm.frames), "CC errors:", dm.cc_errors, "->", pcap)
summary = subprocess.run(["tshark", "-r", pcap, "-o", "docsis.check_fcs:TRUE", "-T", "fields",
                          "-e", "docsis_mgmt.type", "-e", "docsis.hcs.status", "-e", "docsis_sync.cmts_timestamp"],
                         capture_output=True, text=True).stdout.split("\n")
types = {}
sync_ts = []
for line in summary:
    if not line.strip():
        continue
    f = line.split("\t")
    types[(f[0], f[1])] = types.get((f[0], f[1]), 0) + 1
    if f[0] == "1" and len(f) > 2 and f[2]:
        sync_ts.append(int(f[2]))
print("tshark (mgmt type, HCS status) counts:", types)
# SYNC timestamps: should advance like the symbol clock (10.24 MHz)
sync_pk = [i for i, p in enumerate(out) if p[1] & 0x40 and p[5] == 0xC0]
d_ts = np.diff(sync_ts)
d_pk = np.diff(sync_pk) * (j83a.RS_LEN * 8 // m) * 1280 / 869
print("SYNC interval ms:", np.round(d_ts[:5] / 10240, 3), "max |ts - symbol clock| =",
      np.max(np.abs(d_ts - d_pk)), "counts")
