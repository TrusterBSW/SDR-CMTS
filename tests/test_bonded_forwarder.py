"""Downstream unicast with two bonded modems (cmts/bridge.py BondedForwarder): each frame carries
the DSID of the modem its destination sits behind (the modem itself, or a CPE learned from the
upstream), its own sequence numbers per DSID, only on the channels that modem acquired;
broadcast and unknown destinations go on the primary channel without a DSID; with one bonded
modem, an address not learned yet still goes to it (behaviour before 2026-10-10)."""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import docsis_mac as mac                  # noqa: E402
from cmts.bridge import BondedForwarder              # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


class Chan:
    def __init__(self):
        self.data = []

    def send_data(self, pdu):
        self.data.append(pdu)

    def backlog(self):
        return len(self.data)


def dsid_of(pdu):
    """(DSID, sequence) of a Packet PDU with a DS-EHDR, or None."""
    if pdu[0] != 0x01 or pdu[4] != 0x85:
        return None
    return ((pdu[5] & 0xF) << 16) | (pdu[6] << 8) | pdu[7], (pdu[8] << 8) | pdu[9]


A, B = bytes.fromhex("247f20a838b8"), bytes.fromhex("247f20aaaaaa")
CPE_A, CPE_B = bytes.fromhex("247f20a838ba"), bytes.fromhex("0011223344bb")
HOST = bytes.fromhex("7ec90862 86cf".replace(" ", ""))
ma, mb = NS(mac=A), NS(mac=B)
core = NS(by_sid={1: ma, 2: ma, 3: mb, 4: mb}, bonded={A: 0x1001, B: 0x1002},
          bonded_channels={A: [1, 2], B: [1, 3]})
chans = [Chan(), Chan(), Chan()]
fwd = BondedForwarder(chans, core, mac)


def eth(dst, src=HOST, n=100):
    return dst + src + b"\x08\x00" + bytes(n)


fwd.learn(eth(HOST, CPE_A), 2)          # CPE behind A, seen on A's second SID (MTC link)
fwd.learn(eth(HOST, CPE_B), 3)
sent = []
for dst in (A, CPE_A, B, CPE_B, CPE_A, CPE_B, b"\xff" * 6, bytes.fromhex("0000deadbeef")):
    k = fwd.send(eth(dst))
    pdu = chans[k].data[-1]
    sent.append((dst, k, dsid_of(pdu)))
byd = {}
for dst, k, d in sent:
    if d:
        byd.setdefault(d[0], []).append((k, d[1]))
check("modem A et son CPE : DSID de A, canaux 1-2 seulement",
      all(d and d[0] == 0x1001 and k in (0, 1) for dst, k, d in sent if dst in (A, CPE_A)), str(sent[:2]))
check("modem B et son CPE : DSID de B, canaux 1 et 3 seulement",
      all(d and d[0] == 0x1002 and k in (0, 2) for dst, k, d in sent if dst in (B, CPE_B)), str(sent[2:4]))
check("numéros de séquence propres à chaque DSID",
      [s for _, s in byd[0x1001]] == [0, 1, 2] and [s for _, s in byd[0x1002]] == [0, 1, 2], str(byd))
check("diffusion et destination inconnue : primaire, sans DSID",
      sent[6][1] == 0 and sent[6][2] is None and sent[7][1] == 0 and sent[7][2] is None, str(sent[6:]))

# queues emptied as fast as they fill (TS packets built in C): the channels in turn, in sequence
# order, the Sagem lost frames otherwise (2026-10-11); a channel 3 frames above the least loaded
# is skipped
core6 = NS(by_sid={1: ma}, bonded={A: 0x1001}, bonded_channels={A: [1, 2, 3, 4, 5, 6]})
ch6 = [Chan() for _ in range(6)]
f6 = BondedForwarder(ch6, core6, mac)
order = []
for _ in range(12):
    order.append(f6.send(eth(A)))
    for c in ch6:
        c.data.clear()
check("files vides : canaux à tour de rôle", order == [0, 1, 2, 3, 4, 5] * 2, str(order))
ch6[1].data.extend([b""] * 3)
order = [f6.send(eth(A)) for _ in range(5)]
check("canal chargé (3 trames de plus) sauté", 1 not in order[:5] and order[0] == 0, str(order))

# one bonded modem: an address not learned yet still goes to it with its DSID (former behaviour)
core1 = NS(by_sid={1: ma}, bonded={A: 0x1001}, bonded_channels={A: [1, 2]})
ch1 = [Chan(), Chan()]
f1 = BondedForwarder(ch1, core1, mac)
k = f1.send(eth(bytes.fromhex("0000deadbeef")))
check("un seul modem agrégé : adresse pas encore apprise -> son DSID", dsid_of(ch1[k].data[-1])[0] == 0x1001)
# not bonded (one downstream channel, or modem not online yet): plain PDU on the primary
core0 = NS(by_sid={1: ma}, bonded={}, bonded_channels={})
ch0 = [Chan(), Chan()]
k = BondedForwarder(ch0, core0, mac).send(eth(A))
check("pas encore agrégé : primaire, sans DSID", k == 0 and dsid_of(ch0[0].data[-1]) is None)
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
