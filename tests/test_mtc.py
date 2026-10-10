"""Multiple Transmit Channel mode (DOCSIS 3.0 upstream bonding), receiver side:
  - queue-depth based request frame (FC 0xC8, 7 bytes) and segment header (8 bytes, HCS);
  - a data grant decoded as a segment (cmts/us_phy.py, seg=True): ATDMA 64-QAM burst filling its
    grant, every codeword decoded, the segment header checked;
  - Continuous Concatenation and Fragmentation reassembly (docsis_mac.Reassembler): MAC frames
    cut across segments sent on two channels, out of order, a lost segment, padding."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import docsis_mac as mac                  # noqa: E402
from cmts import us_phy                              # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


rng = np.random.default_rng(3)
CM = bytes.fromhex("247f20a838b8")
CMTS = bytes.fromhex("020000cafe01")


def eth(n, tag):
    return CMTS + CM + b"\x08\x00" + bytes([tag]) * (n - 14)


# ---------------------------------------------------------------- formats
q = mac.qreq_frame(0x123, 500)
info = mac.parse_us_frame(q)
check("requête en octets (FC 0xC8, 7 octets)", info and info["kind"] == "REQ" and info["total"] == 7
      and info["sid"] == 0x123 and info["qbytes"] == 500, str(info))
h = mac.seg_header(4321, ptr=17, sc=1, req=250)
d = mac.parse_seg_header(h)
check("en-tête de segment", d == dict(pfi=1, ptr=17, seq=4321, sc=1, req=250), str(d))
bad = bytearray(h)
bad[3] ^= 1
check("en-tête de segment : HCS faux détecté", mac.parse_seg_header(bytes(bad)) is None)

# ---------------------------------------------------------------- segment burst through the PHY
mac.configure_upstream("atdma", 6)
prof = us_phy.profiles()
FS, SR = 13.904e6, 5.12e6
demod = us_phy.BurstDemod(FS, SR)
iuc = mac.IUC_ADV_LONG
p = prof[iuc]
grant_ms = 40                                        # mini-slots of 128 symbols
grant_syms = grant_ms * 128
fill = int(round(grant_syms - p.guard - p.preamble.size)) * p.bps // 8
n2, k = 2 * p.fec_t, p.fec_k
ncw = -(-fill // (k + n2))
last = fill - (ncw - 1) * (k + n2) - n2
cap = (ncw - 1) * k + min(last, k)                   # information bytes of the grant
stream = mac.data_pdu(eth(700, 1)) + mac.data_pdu(eth(1400, 2)) + mac.data_pdu(eth(300, 3))
payload = stream[:cap - 8].ljust(cap - 8, b"\xff")  # the modem pads its segment (stuff bytes)
seg = mac.seg_header(0, ptr=0, req=100) + payload
sym = us_phy.encode_burst(p, seg)
n = int(sym.size * FS / SR) + 4000
x = us_phy.synth(sym, FS / SR, 1000.0, n) * 0.2
x = (x + 0.2 * 10 ** (-35 / 20) / np.sqrt(2) * (rng.normal(size=n) + 1j * rng.normal(size=n))).astype(np.complex64)
b = demod.detect(x, iuc, 900, 1100, max_frame=cap + 64, grant_syms=grant_syms, seg=True)
check("rafale de données en mode segment décodée en entier",
      b is not None and b.frame == seg and b.info["kind"] == "SEG" and b.info["seq"] == 0 and b.info["req"] == 100,
      f"{len(b.frame)} octets ({ncw} mots de code), MER {b.mer:.1f} dB" if b is not None and b.frame else
      (b.error if b is not None else "aucun préambule"))
b2 = demod.detect(x, iuc, 900, 1100, max_frame=cap + 64, grant_syms=grant_syms, seg=False)
check("la même rafale sans le mode segment : pas de trame MAC lue", b2 is None or b2.frame is None)

# ---------------------------------------------------------------- reassembly across two channels
frames = [mac.data_pdu(eth(int(rng.integers(64, 1500)), i % 256)) for i in range(40)]
data = b"".join(frames)
segs, pos, seq = [], 0, 0
starts = np.cumsum([0] + [len(f) for f in frames])
while pos < len(data):
    size = int(rng.integers(200, 1800))
    chunk = data[pos:pos + size]
    nxt = [s for s in starts if pos <= s < pos + len(chunk)]
    ptr = int(nxt[0] - pos) if nxt else None
    hdr = mac.parse_seg_header(mac.seg_header(seq, ptr))
    pad = b"\xff" * (size - len(chunk))              # last one padded up to its grant
    segs.append((seq % 2 + 1, hdr, chunk + pad))     # channels 1, 2, 1, 2...
    pos += len(chunk)
    seq += 1
order = list(range(len(segs)))
for i in range(0, len(order) - 1, 2):                # the two channels deliver out of step
    if rng.random() < 0.5:
        order[i], order[i + 1] = order[i + 1], order[i]
ra = mac.Reassembler(channels=(1, 2))
out = []
for t, i in enumerate(order):
    ch, hdr, pl = segs[i]
    out += ra.add(ch, hdr, pl, t * 0.001)
check("réassemblage sur 2 canaux, segments désordonnés", out == frames,
      f"{len(segs)} segments -> {len(out)}/{len(frames)} trames, {ra.stats}")
# one segment lost: the frames it carried part of are lost, the rest come back
lost = 7
ra = mac.Reassembler(channels=(1, 2))
out = []
for t, i in enumerate(range(len(segs))):
    if i == lost:
        continue
    ch, hdr, pl = segs[i]
    out += ra.add(ch, hdr, pl, t * 0.001)
a0, a1 = sum(len(s[2]) for s in segs[:lost]), sum(len(s[2]) for s in segs[:lost + 1])
hit = [j for j in range(len(frames)) if starts[j] < a1 and starts[j + 1] > a0]
expect = [f for j, f in enumerate(frames) if j not in hit and not (starts[j] < a1 < starts[j + 1])]
check("segment perdu : seules les trames qui le touchaient manquent",
      all(f in out for f in expect) and len(out) >= len(frames) - len(hit) - 2 and ra.stats["lost"] == 1,
      f"{len(out)}/{len(frames)} trames ({len(hit)} touchées), {ra.stats}")
# a segment decoded only partly (its end missing): the next segment's pointer tells where the next
# frame starts; the frame in progress, which does not end there, is dropped, not stitched
import itertools                                     # noqa: E402
fr = [mac.data_pdu(bytes([i]) * 600) for i in range(6)]
st = b"".join(fr)
starts = list(itertools.accumulate([0] + [len(x) for x in fr]))[:-1]


def first_start(a, b):
    n = [s_ - a for s_ in starts if a <= s_ < b]
    return n[0] if n else None


ra = mac.Reassembler(channels=(1,))
out = []
for a, b, q in ((0, 700, 0), (900, 2000, 1), (2000, len(st), 2)):   # bytes 700-900 never decoded
    out += ra.add(1, mac.parse_seg_header(mac.seg_header(q, first_start(a, b))), st[a:b], q * 0.001)
check("segment tronqué : réalignement sur le pointeur, aucune trame recollée de travers",
      [fr.index(x) for x in out if x in fr] == [0, 2, 3, 4, 5] and all(x in fr for x in out),
      f"{ra.stats}")
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
