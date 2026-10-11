"""Real ATDMA 64-QAM 5.12 Msym/s windows (Sagem 3686, 2026-10-09) where the guard time (147
samples) is shorter than an energy block (174 samples): the tail of the previous burst and the
burst of this grant form one energy cluster. UpstreamRx._handle searched only where that cluster
began and missed the preamble (32 grants in 3 minutes); it must now find and decode the burst,
with ONE search: the cluster is searched up to the grant start (2026-10-11; before, a second
search at the grant start found it, two matched filters per back-to-back burst)."""
import collections
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import docsis_mac as mac, upstream, us_phy   # noqa: E402

mac.configure_upstream("atdma", 6)
u = upstream.UpstreamRx.__new__(upstream.UpstreamRx)   # only what _handle uses
u.fs = 13.904e6
u.demod = us_phy.BurstDemod(u.fs, 5.12e6)
u.prof = upstream.profiles()
u.ms_samples = 348.0                     # 128-symbol mini-slots
u.gate, u.ko_dir, u.save_dir, u.im_slack = 10 ** 0.8, None, None, 0
u.stats = collections.Counter()
u.recent = collections.deque(maxlen=16)
u.burst_level = None
u.rx_tick0, u.decim = 0, 1
u.tb = type("TB", (), {"ts": lambda self, t: 0})()
got = []
u.on_burst = got.append
files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "atdma64_merged_tail_*.npz")))
ok = 0
for f in files:
    z = np.load(f)
    s0, g = float(z["start_sample"]), float(z["grant_syms"])
    u.recent.clear()
    got.clear()
    u.floor = 10 ** -6.4                 # bench noise floor (-64 dBFS)
    u._handle(z["iq"], 0, s0, s0 + g * u.demod.sps, 2 * u.ms_samples, 0.0, 0, mac.IUC_ADV_LONG, 1, mac.IUC_ADV_LONG)
    b = got[0].burst if got else None
    good = b is not None and b.frame is not None
    ok += good
    print(f"{'OK ' if good else 'KO '} {os.path.basename(f)}: "
          + (f"rafale trouvée au début du grant, {len(b.frame)} octets, MER {b.mer:.1f} dB" if good else "rafale manquée"))
# an empty grant whose lead-in holds the tail of the previous burst: no preamble search
# (a grant in advance in every MAP made these ~490/s; searching them lagged the upstream 1 s)
import time
z = np.load(files[0])
x = (np.random.default_rng(1).standard_normal(z["iq"].size) * 1j + 0j).astype(np.complex64) * 10 ** -3.2
x[:600] += z["iq"][:600]                                     # the tail, then noise only
s0 = float(z["start_sample"])
got.clear()
n0 = u.stats["searched"]
t = time.perf_counter()
for _ in range(50):
    u.floor = 10 ** -6.4
    u._handle(x, 0, s0, s0 + 34 * 128 * u.demod.sps, 2 * u.ms_samples, 0.0, 0, mac.IUC_ADV_LONG, 1, mac.IUC_ADV_LONG)
dt = (time.perf_counter() - t) / 50
empty = len(got) == 50 and all(e.burst is None for e in got) and u.stats["searched"] == n0
print(f"{'OK ' if empty else 'KO '} grant vide avec la queue de la rafale précédente : vide, sans recherche, {dt * 1e3:.2f} ms")
one = u.stats["grant_start_search"] == 0 and u.stats["searched"] == len(files)
print(f"{'OK ' if one else 'KO '} une seule recherche par rafale : {u.stats['searched']} recherches, "
      f"{u.stats['grant_start_search']} de secours au début du grant")
good = files and ok == len(files) and one and empty
print("PASS" if good else "FAIL")
sys.exit(0 if good else 1)
