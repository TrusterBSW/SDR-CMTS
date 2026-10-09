"""Cross-check randomizer + RS(204,188) + interleaver against GNU Radio gr-dtv
(DVB-T outer coding is identical to DVB-C / J.83 Annex A)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import numpy as np
from gnuradio import gr, blocks, dtv
from cmts import j83a

rng = np.random.default_rng(1)
N = 8 * 40
pkts = rng.integers(0, 256, size=(N, 188), dtype=np.uint8)
pkts[:, 0] = 0x47

tb = gr.top_block()
src = blocks.vector_source_b(pkts.ravel().tolist(), False)
ed = dtv.dvbt_energy_dispersal(1)
rs = dtv.dvbt_reed_solomon_enc(2, 8, 0x11d, 255, 239, 8, 51, 8)
il = dtv.dvbt_convolutional_interleaver(136, 12, 17)
snk = blocks.vector_sink_b()
snk_rs = blocks.vector_sink_b(1632)
tb.connect(src, ed, rs, il, snk)
tb.connect(rs, snk_rs)
tb.run()
ref = np.array(snk.data(), dtype=np.uint8)
ref_rs = np.array(snk_rs.data(), dtype=np.uint8)

enc = j83a.J83AEncoder(64)
r = pkts.reshape(-1, 8, 188) ^ j83a.RAND_MASK
r[:, 0, 0] = 0xB8
cw = j83a.rs_encode(r.reshape(-1, 188))
mine_il = enc.interleave(cw)

n = min(ref_rs.size, cw.size)
print("RS+randomizer bytes compared:", n, "equal:", np.array_equal(ref_rs[:n], cw.ravel()[:n]))
n = min(ref.size, mine_il.size)
print("interleaver bytes compared:", n, "equal:", np.array_equal(ref[:n], mine_il[:n]))
if not np.array_equal(ref[:n], mine_il[:n]):
    d = np.nonzero(ref[:n] != mine_il[:n])[0]
    print("first diff at", d[:10])
