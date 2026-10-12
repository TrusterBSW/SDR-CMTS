#!/usr/bin/env python3
"""UCD burst profiles and MAP usage per upstream channel, from a pcap of sfr_listen.py
(broadcast management only), plus the grants of the SIDs given with --sid (the modem's own,
read in its REG-RSP-MP TLV 47).

  tools/sfr_maps.py ~/sfrcap/rb1/rb_282.pcap --sid 0x2589 --sid 0x258a
"""
import argparse
import collections
import struct
import sys

MOD = {1: "QPSK", 2: "16-QAM", 3: "8-QAM", 4: "32-QAM", 5: "64-QAM", 6: "128-QAM"}
IUC = {1: "requête", 2: "REQ/données", 3: "init maint", 4: "station maint", 5: "court", 6: "long",
       9: "ATDMA court", 10: "ATDMA long", 11: "ATDMA UGS", 7: "null", 8: "ack"}
DATA_IUC = {5, 6, 9, 10, 11}
TICK_US = 6.25                                     # minislot unit: 64 ticks of 10.24 MHz


def pcap_frames(path):
    with open(path, "rb") as f:
        f.read(24)
        while True:
            h = f.read(16)
            if len(h) < 16:
                return
            sec, usec, n, _ = struct.unpack("<IIII", h)
            yield sec + usec / 1e6, f.read(n)


def tlvs(buf):
    i = 0
    while i + 2 <= len(buf):
        t, ln = buf[i], buf[i + 1]
        yield t, buf[i + 2:i + 2 + ln]
        i += 2 + ln


def burst(v):
    d = dict(tlvs(v[1:]))
    g = lambda k, fmt=">B": struct.unpack(fmt, d[k])[0] if k in d else None
    out = f"{MOD.get(g(1), g(1))}"
    out += f", préambule {g(3, '>H')} bits (décalage {g(4, '>H')})"
    t = g(5)
    out += f", FEC T={t} k={g(6)}" if t else ", sans FEC"
    out += f", rafale max {g(8)} mini-slots, garde {g(9)} sym"
    out += f", dernier mot {'raccourci' if g(10) == 2 else 'fixe'}"
    if 12 in d:
        out += f", entrelaceur RS {g(12)}"
    if 14 in d:
        out += f", préambule type {g(14)}"
    if 11 in d:
        out += f", brouilleur {'oui' if g(11) == 1 else 'non'}"
    return v[0], out


def ucd_text(body):
    ch, ccc, ms, ds = body[:4]
    lines = [f"UCD canal {ch} (compteur {ccc}, DS {ds}) : mini-slot {ms} ticks = {ms * TICK_US:.2f} µs"]
    other = []
    for t, v in tlvs(body[4:]):
        if t == 1:
            lines.append(f"   débit symbole {v[0] * 160} ksym/s")
        elif t == 2:
            lines.append(f"   fréquence {struct.unpack('>I', v)[0] / 1e6:.3f} MHz")
        elif t == 3:
            lines.append(f"   motif de préambule {len(v)} octets")
        elif t in (4, 5):
            iuc, txt = burst(v)
            lines.append(f"   IUC {iuc:2d} {IUC.get(iuc, ''):12s}: {txt}")
        else:
            other.append(f"{t}={v.hex()}")
    if other:
        lines.append("   autres TLV : " + " ".join(other))
    return ch, ms, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcap")
    ap.add_argument("--sid", action="append", default=[], type=lambda s: int(s, 0))
    a = ap.parse_args()
    ucds, ms_ticks = {}, {}
    st = collections.defaultdict(lambda: dict(n=0, slots=collections.Counter(), ies=collections.Counter(),
                                              length=collections.Counter(), lead=[], req_per_map=[],
                                              grant_sz=collections.Counter(), bo=collections.Counter(),
                                              sids=set(), t0=None, t1=None))
    mine = collections.defaultdict(lambda: collections.defaultdict(list))   # sid -> ch -> [(t, iuc, slots)]
    for t, f in pcap_frames(a.pcap):
        if f[0] not in (0xC2, 0xC3, 0xC0):
            continue
        hl = 6 + (f[1] if f[0] & 1 else 0)
        if len(f) < hl + 24:
            continue
        mtype, body = f[hl + 18], f[hl + 20:-4]
        if mtype in (2, 29, 35):
            ch, ms, txt = ucd_text(body)
            ucds[ch], ms_ticks[ch] = txt, ms
        elif mtype == 3:
            ch, ccc, nie = body[0], body[1], body[2]
            start, ack = struct.unpack(">II", body[4:12])
            s = st[ch]
            s["n"] += 1
            s["t0"] = s["t0"] or t
            s["t1"] = t
            s["bo"][tuple(body[12:16])] += 1
            ies = [struct.unpack(">I", body[16 + 4 * k:20 + 4 * k])[0] for k in range(min(nie, (len(body) - 16) // 4))]
            ies = [(v >> 18, (v >> 14) & 15, v & 0x3FFF) for v in ies]
            nreq = 0
            for (sid, iuc, o), (_, _, o2) in zip(ies, ies[1:]):
                if iuc == 7:
                    break
                s["slots"][iuc] += o2 - o
                s["ies"][iuc] += 1
                if iuc == 1:
                    nreq += o2 - o
                if iuc in DATA_IUC and 0 < sid < 0x3E00:
                    s["grant_sz"][o2 - o] += 1
                    s["sids"].add(sid)
                if sid in a.sid:
                    mine[sid][ch].append((t, iuc, o2 - o, o2 - o == 0))
            null = [o for sid, iuc, o in ies if iuc == 7]
            if null:
                s["length"][null[0]] += 1
            s["req_per_map"].append(nreq)
    print(f"{len(ucds)} UCD, {sum(s['n'] for s in st.values())} MAP\n")
    for ch in sorted(ucds):
        print(ucds[ch])
    print()
    for ch in sorted(st):
        s, ms = st[ch], ms_ticks.get(ch, 0)
        dur = (s["t1"] - s["t0"]) or 1
        tot = sum(s["slots"].values()) or 1
        lens = s["length"].most_common(3)
        print(f"MAP canal {ch} : {s['n'] / dur:.0f} MAP/s, longueur (mini-slots) "
              + ", ".join(f"{L} ({L * ms * TICK_US / 1000:.2f} ms) x{n}" for L, n in lens)
              + f", fenêtres de backoff {s['bo'].most_common(2)}")
        print("   temps alloué : " + ", ".join(f"IUC {k} {IUC.get(k, '')} {100 * v / tot:.1f} %"
                                           for k, v in sorted(s["slots"].items())))
        r = s["req_per_map"]
        print(f"   requête : {sum(r) / max(len(r), 1):.1f} mini-slots par MAP ; grants de données : "
              f"{sum(s['grant_sz'].values()) / dur:.1f}/s, tailles les plus fréquentes "
              f"{s['grant_sz'].most_common(6)}, SID distincts {len(s['sids'])}")
    for sid in a.sid:
        print(f"\nSID {sid:#06x} :")
        for ch in sorted(mine[sid]):
            ev = mine[sid][ch]
            c = collections.Counter((iuc, z) for _, iuc, _, z in ev)
            sz = collections.Counter(n for _, iuc, n, z in ev if iuc in DATA_IUC and not z)
            print(f"   canal {ch} : {len(ev)} IE de {ev[0][0] % 86400 / 3600:.0f}h… "
                  f"({', '.join(f'IUC {i}{' (en attente)' if z else ''} x{n}' for (i, z), n in sorted(c.items()))}), "
                  f"tailles de grant {sz.most_common(5)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
