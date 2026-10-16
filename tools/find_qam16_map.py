#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Find the 16-QAM label -> point mapping of a real modem from its own data bursts.

    python3 tools/find_qam16_map.py captures/bursts_ko/*.npz

The bursts (IUC 5/6, 16-QAM, saved by sdr_cmts.py when they fail to decode) are demodulated
down to grid decisions (I level, Q level); then every plausible mapping is tried: per-axis Gray
or natural codes on any bit positions, and quadrant-based codes (2 bits select the quadrant, 2
select the point inside it, the other quadrants obtained by rotation or by mirroring). The right
mapping is the one whose first Reed-Solomon codeword decodes and whose MAC header checks: a
random mapping passing both is out of reach (RS T=8 alone rejects all 300/300 random words).
Prints the table to give to us_phy.set_qam16_map().
"""
import itertools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cmts import cdsp, docsis_mac as mac, us_phy  # noqa: E402
from cmts.rs import RSCodec  # noqa: E402

L = np.array([-3, -1, 1, 3]) / np.sqrt(10)


def grid_decisions(path):
    """Burst -> grid index per data symbol (4 * I level + Q level), or None."""
    z = np.load(path)
    iuc = int(z["iuc"])
    p = dict(mac.BURST_PROFILES[iuc])
    p["mod_qpsk"] = False
    prof = us_phy.BurstProfile(iuc=iuc, **p)
    grid = np.array([L[g >> 2] + 1j * L[g & 3] for g in range(16)], np.complex64)
    us_phy.set_qam16_map(grid)                        # decided index = grid index
    d = us_phy.BurstDemod(float(z["fs"]), 1.28e6 if float(z["fs"]) < 5e6 else 2.56e6)
    d.prof[iuc] = prof
    got = {}
    orig = cdsp.dd_track

    def spy(r, pts, ka=0.08, kb=0.004):
        out = orig(r, pts, ka, kb)
        got["idx"] = out[1]
        return out
    cdsp.dd_track = spy
    try:
        s0 = float(z["start_sample"])
        d.detect(z["iq"], iuc, max(0, s0 - 60), s0 + 60, max_frame=2048)
    finally:
        cdsp.dd_track = orig
    return got.get("idx"), prof


def candidates():
    """grid index -> label tables (16 ints)."""
    perms = list(itertools.permutations(range(4)))
    seen = set()
    # A: per axis, any 2-bit code on any two label bit positions
    for pos in itertools.permutations(range(4)):      # label bits feeding I hi, I lo, Q hi, Q lo
        for ci in perms:                              # level index -> 2-bit value (I)
            for cq in perms:
                t = [0] * 16
                for g in range(16):
                    vi, vq = ci[g >> 2], cq[g & 3]
                    lab = (((vi >> 1) & 1) << (3 - pos[0])) | ((vi & 1) << (3 - pos[1])) \
                        | (((vq >> 1) & 1) << (3 - pos[2])) | ((vq & 1) << (3 - pos[3]))
                    t[g] = lab
                key = tuple(t)
                if key not in seen:
                    seen.add(key)
                    yield key
    # B: quadrant bits + inner bits, rotation or mirror symmetry
    quad_of = lambda g: (1 if (g >> 2) >= 2 else 0) << 1 | (1 if (g & 3) >= 2 else 0)   # (I>0, Q>0)
    for pos in itertools.permutations(range(4)):      # label bits: quadrant hi, lo, inner hi, lo
        for cquad in perms:                           # quadrant (I>0,Q>0 as 2 bits) -> 2-bit value
            for cin in perms:                         # inner point of the reference quadrant -> value
                for sym in ("rot", "mirror"):
                    t = [0] * 16
                    for g in range(16):
                        i, q = g >> 2, g & 3
                        qd = quad_of(g)
                        ii, qq = (i - 2) if i >= 2 else (1 - i), (q - 2) if q >= 2 else (1 - q)
                        if sym == "rot":              # bring the point back to quadrant (+,+) by rotation
                            x, y = (i - 1.5), (q - 1.5)
                            k = {3: 0, 1: 1, 0: 2, 2: 3}[qd]
                            for _ in range(k):
                                x, y = y, -x
                            ii, qq = int(x + 1.5) - 2, int(y + 1.5) - 2
                        inner = ii << 1 | qq          # 0..3 inside the quadrant
                        vq_, vi_ = cquad[qd], cin[inner]
                        lab = (((vq_ >> 1) & 1) << (3 - pos[0])) | ((vq_ & 1) << (3 - pos[1])) \
                            | (((vi_ >> 1) & 1) << (3 - pos[2])) | ((vi_ & 1) << (3 - pos[3]))
                        t[g] = lab
                    key = tuple(t)
                    if len(set(key)) == 16 and key not in seen:
                        seen.add(key)
                        yield key


def try_map(table, gidx, prof):
    lab = np.asarray(table)[gidx]
    bits = ((lab[:, None] >> np.array([3, 2, 1, 0])) & 1).astype(np.uint8).ravel()
    bits = us_phy.scramble(bits, prof.seed)
    raw = np.packbits(bits[:bits.size // 8 * 8])
    n = prof.fec_k + 2 * prof.fec_t
    if raw.size < n:
        return None
    try:
        msg, _ = cdsp.rs_decode(raw[:n].tobytes(), 2 * prof.fec_t) if cdsp.AVAILABLE else RSCodec(prof.fec_t).decode(raw[:n])
    except ValueError:
        return None
    for rev in (True, False):
        fb = bytes(us_phy._BITREV[np.frombuffer(msg, np.uint8)]) if rev else msg
        if mac.parse_us_frame(fb):
            return "LSB-first" if rev else "MSB-first"
    return None


def search_with_preamble(path, cands):
    """Bursts whose 16-QAM preamble could not be found (nopre_*.npz): for every candidate map,
    install it (the preamble symbols follow from it) and run the full detection + decoding."""
    z = np.load(path)
    iuc = int(z["iuc"])
    p = dict(mac.BURST_PROFILES[iuc])
    p["mod_qpsk"] = False
    fs = float(z["fs"])
    d = us_phy.BurstDemod(fs, 1.28e6 if fs < 5e6 else 2.56e6)
    d.prof[iuc] = us_phy.BurstProfile(iuc=iuc, **p)
    s0 = float(z["start_sample"])
    g = float(z["grant_syms"]) if "grant_syms" in z else None
    for t in cands:
        pts = [None] * 16
        for gi, lab in enumerate(t):
            pts[lab] = complex(L[gi >> 2], L[gi & 3])
        us_phy.set_qam16_map(pts)
        b = d.detect(z["iq"], iuc, max(0, s0 - 120), s0 + 60, max_frame=2048, grant_syms=g)
        if b is not None and b.frame is not None and not b.inverted:   # mirrored maps decode too, inverted
            return t, b
    return None, None


def main():
    files = sys.argv[1:]
    if files and os.path.basename(files[0]).startswith("nopre_"):
        cands = list(candidates())
        print(f"{len(cands)} correspondances candidates, préambule 16-QAM inclus")
        t, b = search_with_preamble(files[0], cands)
        if t is None:
            print("aucune correspondance ne décode")
            return 1
        print(f"trouvée sur {os.path.basename(files[0])} : MER {b.mer:.1f} dB, {len(b.frame)} octets, table {t}")
        good = sum(search_with_preamble(f, [t])[0] is not None for f in files[1:])
        print(f"vérification : {good}/{len(files) - 1} autres rafales décodées avec la même table")
        pts = [None] * 16
        for gi, lab in enumerate(t):
            pts[lab] = complex(L[gi >> 2], L[gi & 3])
        print("[" + ", ".join(f"({p.real:+.4f}{p.imag:+.4f}j)" for p in pts) + "]")
        return 0
    bursts = [b for b in (grid_decisions(f) for f in files) if b[0] is not None]
    print(f"{len(bursts)} rafales démodulées sur {len(files)}")
    cands = list(candidates())
    print(f"{len(cands)} correspondances candidates")
    found = []
    for t in cands:
        order = try_map(t, *bursts[0])
        if order:
            ok = sum(try_map(t, *b) is not None for b in bursts)
            found.append((ok, t, order))
            print(f"  candidate : {ok}/{len(bursts)} rafales, octets {order}, table {t}")
    if not found:
        print("aucune correspondance ne décode : autre famille, ou rafales trop dégradées")
        return 1
    ok, t, order = max(found)
    pts = [None] * 16
    for g, lab in enumerate(t):
        pts[lab] = complex(L[g >> 2], L[g & 3])
    print("table label -> point (pour us_phy.set_qam16_map) :")
    print("[" + ", ".join(f"({p.real:+.4f}{p.imag:+.4f}j)" for p in pts) + "]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
