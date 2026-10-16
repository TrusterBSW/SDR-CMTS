#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Regression on real upstream bursts: INIT ranging RNG-REQ of a Sagem F@st 3686 captured by
the B210 RX2 (3.476 MS/s, 1.28 Msym/s QPSK, sdr_cmts.py --save-bursts, 2026-10-08), one of
them saturated (peak > 1). They fixed the scrambler phase and the LSB-first MAC byte order."""
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import docsis_mac as mac          # noqa: E402
from cmts.us_phy import BurstDemod          # noqa: E402

files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "raw_*.npz")))
d = BurstDemod(3.476e6, 1.28e6)
ok = 0
for f in files:
    z = np.load(f)
    x, i0 = z["iq"], int(z["first"])
    bu = d.detect(x, mac.IUC_INIT_MAINT, max(0, i0 - 400), i0 + 400)
    good = bool(bu and bu.frame and bu.info.get("crc_ok") and bu.info.get("mtype") == mac.RNG_REQ
                and bu.info["da"] == bytes.fromhex("020000cafe01") and bu.bitrev and not bu.inverted)
    ok += good
    print(f"{'OK ' if good else 'KO '} {os.path.basename(f)}: "
          + (f"RNG-REQ de {bu.info['sa'].hex(':')}, MER {bu.mer:.1f} dB, crête {bu.peak:.2f}, {bu.freq:+.0f} Hz"
             if bu and bu.frame else f"{bu.error if bu else 'pas de préambule'}"))
# Data bursts (2026-10-09): DHCP DISCOVER in a 77 mini-slot IUC 6 grant. The modem fills the
# grant: its last RS codeword is shortened AND zero-padded (116 information bytes for 113 of frame).
data = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "data_*.npz")))
for f in data:
    z = np.load(f)
    x, i0 = z["iq"], int(z["start_sample"])
    grant = (14 if "tftp" in f else 77) * 32 - 1e-4   # as in the receiver: a difference of large floats
    bu = d.detect(x, mac.IUC_LONG_DATA, max(0, i0 - 40), i0 + 40, max_frame=2048, grant_syms=grant)
    eth, fcs = mac.pdu_ethernet(bu.frame, bu.info) if bu and bu.frame else (b"", False)
    good = bool(fcs and eth[6:12] == bytes.fromhex("247f20a838b8"))
    ok += good
    print(f"{'OK ' if good else 'KO '} {os.path.basename(f)}: "
          + (f"trame de données {len(bu.frame)} octets, FCS Ethernet bon, MER {bu.mer:.1f} dB" if good
             else f"{bu.error if bu else 'pas de préambule'}"))
files += data
# 16-QAM data bursts (2026-10-09, DOCSIS 1.x TDMA: the preamble is 16-QAM too), 39 mini-slot grants
q16 = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "qam16_dhcp_*.npz")))
from cmts.us_phy import BurstProfile  # noqa: E402
p16 = dict(mac.BURST_PROFILES[mac.IUC_LONG_DATA])
p16["mod_qpsk"] = False
d16 = BurstDemod(3.476e6, 1.28e6)
d16.prof[mac.IUC_LONG_DATA] = BurstProfile(iuc=mac.IUC_LONG_DATA, **p16)
for f in q16:
    z = np.load(f)
    x, i0 = z["iq"], float(z["start_sample"])
    bu = d16.detect(x, mac.IUC_LONG_DATA, max(0, i0 - 120), i0 + 60, max_frame=2048, grant_syms=39 * 32 - 1e-4)
    eth, fcs = mac.pdu_ethernet(bu.frame, bu.info) if bu and bu.frame else (b"", False)
    good = bool(fcs and eth[6:12] == bytes.fromhex("247f20a838b8") and not bu.inverted)
    ok += good
    print(f"{'OK ' if good else 'KO '} {os.path.basename(f)}: "
          + (f"16-QAM, trame {len(bu.frame)} octets, FCS bon, MER {bu.mer:.1f} dB" if good else f"{bu.error if bu else 'pas de préambule'}"))
files += q16
# 16-QAM at 2.56 Msym/s, long concatenations (36 frames, 3030 bytes; one padded with zero codewords
# to the end of a larger grant in advance)
q16b = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "qam16_2m56_*.npz")))
d16b = BurstDemod(6.952e6, 2.56e6)
d16b.prof[mac.IUC_LONG_DATA] = BurstProfile(iuc=mac.IUC_LONG_DATA, **p16)
for f in q16b:
    z = np.load(f)
    x, i0, g = z["iq"], float(z["start_sample"]), float(z["grant_syms"])
    bu = d16b.detect(x, mac.IUC_LONG_DATA, max(0, i0 - 60), i0 + 60, max_frame=int(g / 2) + 64, grant_syms=g)
    subs = mac.split_concat(bu.frame, bu.info) if bu and bu.frame and bu.info["fc"] == mac.FC_CONCAT else []
    good = bool(subs) and len(subs) == bu.info["parm"] and all(
        s["kind"] != "DATA" or mac.pdu_ethernet(b, s)[1] for b, s in subs)
    ok += good
    print(f"{'OK ' if good else 'KO '} {os.path.basename(f)}: "
          + (f"16-QAM 2,56 Msym/s, concaténation de {len(subs)} trames ({len(bu.frame)} octets), MER {bu.mer:.1f} dB"
             if good else f"{bu.error if bu else 'pas de préambule'}"))
files += q16b
# ATDMA 64-QAM at 5.12 Msym/s (UCD type 29), 4486-symbol grants: long concatenations that failed
# to decode before the per-burst equalizer (MER 24.5 dB raw, ~35 dB equalized)
q64 = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "atdma64_5m12_*.npz")))
mac.configure_upstream("atdma", 6)
d64 = BurstDemod(13.904e6, 5.12e6)
d64.prof[mac.IUC_ADV_LONG] = BurstProfile(iuc=mac.IUC_ADV_LONG, **mac.BURST_PROFILES[mac.IUC_ADV_LONG])
for f in q64:
    z = np.load(f)
    x, t0, g = z["iq"], float(z["t"]), float(z["grant_syms"])
    bu = d64.detect(x, mac.IUC_ADV_LONG, max(0, t0 - 60), t0 + 60, max_frame=int(g * 6 / 8) + 64, grant_syms=g)
    subs = mac.split_concat(bu.frame, bu.info) if bu and bu.frame and bu.info["fc"] == mac.FC_CONCAT else []
    good = bool(subs) and len(subs) == bu.info["parm"] and bu.mer > 30
    ok += good
    print(f"{'OK ' if good else 'KO '} {os.path.basename(f)}: "
          + (f"ATDMA 64-QAM 5,12 Msym/s, concaténation de {len(subs)} trames ({len(bu.frame)} octets), MER égalisée {bu.mer:.1f} dB"
             if good else f"{bu.error if bu else 'pas de préambule'} (MER {bu.mer if bu else 0:.1f} dB)"))
files += q64
# Long ATDMA 64-QAM bursts (16 600-17 400 symbols, 64 KB Max Traffic Burst): the Sagem's symbol
# clock is ~4 ppm off ours, the timing slips 0.07 symbol along the burst and the MER fell from
# 37 to 16 dB. A fresh demodulator must measure the offset on the first one and decode both.
qd = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "atdma64_drift_*.npz")))
dd = BurstDemod(13.904e6, 5.12e6)
dd.prof[mac.IUC_ADV_LONG] = BurstProfile(iuc=mac.IUC_ADV_LONG, **mac.BURST_PROFILES[mac.IUC_ADV_LONG])
for f in qd:
    z = np.load(f)
    x, t0, g = z["iq"], float(z["t"]), float(z["grant_syms"])
    bu = dd.detect(x, mac.IUC_ADV_LONG, max(0, t0 - 60), t0 + 60, max_frame=int(g * 6 / 8) + 64, grant_syms=g, eq_key=1)
    good = bool(bu and bu.frame) and -8e-6 < dd.drift.get(1, 0) < -1e-6
    ok += good
    print(f"{'OK ' if good else 'KO '} {os.path.basename(f)}: rafale de {g:.0f} symboles, "
          + (f"{len(bu.frame)} octets, MER {bu.mer:.1f} dB, écart d'horloge mesuré {dd.drift.get(1, 0) * 1e6:+.2f} ppm"
             if bu and bu.frame else f"{bu.error if bu else 'pas de préambule'}"))
files += qd
print("PASS" if files and ok == len(files) else "FAIL")
sys.exit(0 if files and ok == len(files) else 1)
