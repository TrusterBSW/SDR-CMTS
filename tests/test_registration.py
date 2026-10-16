# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Data path and registration without hardware:
  1. a long data burst (IUC 6, a whole DHCP DISCOVER) demodulated and decoded to Ethernet;
  2. REG-REQ -> REG-RSP (SFID, SID, capabilities, CMTS MIC) -> REG-ACK -> online, the REG-RSP
     checked by Wireshark's DOCSIS dissector;
  3. a big request gets a grant in a longer MAP, and the MAP Ack Time follows the upstream.
"""
import os, struct, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import mini_cmts
from cmts import cmcfg, netpkt
from cmts import docsis_mac as mac
from cmts.pcap import PcapWriter
from cmts.ranging import Modem
from cmts.sim_modem import SimModem
from cmts.upstream import TS_RATE

CM = bytes.fromhex("247f20a838b8")
ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def dhcp_discover():
    bootp = struct.pack(">BBBBIHH", 1, 1, 6, 0, 0x12345678, 0, 0x8000) + bytes(16) + CM.ljust(16, b"\0")
    bootp += bytes(192) + b"\x63\x82\x53\x63" + bytes([53, 1, 1, 60, 9]) + b"docsis3.0" + b"\xff"
    bootp = bootp.ljust(548, b"\0")
    udp = struct.pack(">HHHH", 68, 67, 8 + len(bootp), 0) + bootp
    iph = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0, bytes(4), b"\xff" * 4)
    return b"\xff" * 6 + CM + b"\x08\x00" + iph + udp


# the DOCSIS 1.1 / TDMA single-channel configuration this test checks (defaults changed on 2026-10-10)
a = mini_cmts.parser().parse_args(["--sim", "1", "--us-mode", "tdma", "--us-mod", "16qam", "--us-rate", "16", "--ds-channels", "1",
                                     "--no-grant-ahead-hold", "--grant-ahead-every", "1", "--grant-ahead-scale", "1"])
mux, core, rx, tb, mcr, decim = mini_cmts.build(a, log=lambda *x: None)

# ---- 1. long data burst
events = []
rx.on_burst = events.append
rx.push(0, mini_cmts.np.zeros(0, mini_cmts.np.complex64))
eth = dhcp_discover()
frame = mac.data_pdu(eth)
t0 = int(0.2 * TS_RATE) // mux.ms_counts * mux.ms_counts
n_ms = 90
rx.add_grants([(t0 - 40 * mux.ms_counts, t0 - 8 * mux.ms_counts, mac.IUC_REQUEST, mac.SID_BROADCAST),  # quiet: noise floor
               (t0, t0 + n_ms * mux.ms_counts, mac.IUC_LONG_DATA, 1)])
cm = SimModem(CM, rx, delay_us=0, power_db=-20, freq_hz=200, snr_db=30, symrate=a.us_rate * 160e3,
              ms_counts=mux.ms_counts)
cm._send(t0, frame, mac.IUC_LONG_DATA, "REQ")
rx.push(0, cm.render(0, int(rx.sample_of_ts(t0 + 0.01 * TS_RATE))))
rx.process()
got = [e for e in events if e.burst is not None]
b = got[0].burst if got else None
dec = b is not None and b.frame == frame
check("rafale de données IUC 6 décodée", dec,
      f"{len(frame)} octets, MER {b.mer:.1f} dB, RS corr {b.rs_fixed}" if b else "rien reçu")
if dec:
    e2, fcs = mac.pdu_ethernet(b.frame, b.info)
    check("trame Ethernet extraite, FCS bon", e2 == eth and fcs, netpkt.summary(e2))

# ---- 2. registration
class Sent(list):
    def append(self, item):
        super().append(item[0])


sent = mux.ucast = Sent()
m = Modem(CM, 1)
m.state = "ranged"
core.modems[CM], core.by_sid[1] = m, m
cfg = cmcfg.parse(cmcfg.build("docsis-lab"))
caps = cmcfg.tlv(5, b"".join(cmcfg.tlv(t, bytes([v])) for t, v in [(1, 1), (2, 3), (3, 1), (6, 1), (24, 4), (29, 8)]))
body = struct.pack(">H", 1) + b"".join(cmcfg.tlv(t, v) for t, v in cfg) + caps


class B:
    power_db = -20.0


def reg(mtype, body):
    f = mac.mgmt_frame(mtype, body, CM, da=mux.sa, fc=0xC2)
    core._registration(mac.parse_us_frame(f), B())


reg(mac.REG_REQ, body)
rsp = sent[-1] if sent else b""
info = mac.parse_us_frame(rsp) if rsp else None
check("REG-RSP envoyée", info and info.get("mtype") == mac.REG_RSP and info["body"][2] == 0,
      f"{len(rsp)} octets, réponse {info['body'][2] if info else '?'}")
tl = dict(cmcfg.parse(info["body"][3:])) if info else {}
us = dict(cmcfg.parse(tl.get(24, b"")))
capr = dict(cmcfg.parse(tl.get(5, b"")))
check("flux montant : SFID et SID 1", 2 in us and us.get(3) == b"\x00\x01", f"SFID {struct.unpack('>I', us[2])[0] if 2 in us else '?'}")
check("capacités : version 1.1, concaténation acceptée, fragmentation/multicanal off",
      capr.get(2) == b"\x01" and capr.get(1) == b"\x01" and capr.get(3) == b"\x00" and capr.get(24) == b"\x00"
      and capr.get(29) == b"\x00", str({k: v.hex() for k, v in capr.items()}))
check("état registering", m.state == "registering")
with tempfile.TemporaryDirectory() as d:
    p = PcapWriter(os.path.join(d, "r.pcap"))
    p.write(0, rsp)
    p.close()
    r = subprocess.run(["tshark", "-r", os.path.join(d, "r.pcap"), "-V"], capture_output=True, text=True)
    bad = "Malformed" in r.stdout or "Expert Info (Error" in r.stdout
    check("Wireshark lit la REG-RSP", r.returncode == 0 and "Registration Response" in r.stdout and not bad,
          next((l.strip() for l in r.stdout.splitlines() if "Response:" in l), ""))
reg(mac.REG_ACK, struct.pack(">HB", 1, 0))
check("REG-ACK -> en ligne", m.state == "online")

# ---- 3. big grant and Ack Time
core.pending[1] = 200
rx.processed_ts = 50 * mux.ms_counts
grants = []
mux.on_grants = grants.extend
frames = []
while not any(iuc == mac.IUC_LONG_DATA for _, _, iuc, _ in grants):
    frames += mux._map_frames(mux.now_ts + 10240)
    mux.now_ts += 10240
g = [w for w in grants if w[2] == mac.IUC_LONG_DATA][0]
check("grant de 200 mini-slots dans un MAP allongé", (g[1] - g[0]) // mux.ms_counts == 200 and g[3] == 1)
ack = struct.unpack(">I", frames[-1][6 + 20 + 8:6 + 20 + 12])[0]   # MAC hdr, mgmt hdr, 4 bytes, Alloc Start
check("Ack Time = upstream traité", ack == 50, f"ack {ack}")

# ---- 4. the real REG-REQ of the Sagem 3686 (2026-10-09): its CMTS MIC checks with our secret
real = os.path.join(os.path.dirname(__file__), "data", "sagem3686", "registration.pcap")
d, i, seen = open(real, "rb").read()[24:], 0, False
while i < len(d):
    _, _, n, _ = struct.unpack("<IIII", d[i:i + 16])
    f, i = d[i + 16:i + 16 + n], i + 16 + n
    inf = mac.parse_us_frame(f)
    if inf and inf.get("mtype") == mac.REG_REQ:
        tl = cmcfg.parse(inf["body"][2:])
        seen = True
        check("CMTS MIC du vrai REG-REQ (TLV 57 ajouté par le modem exclu)",
              cmcfg.cmts_mic(tl, "docsis-lab") == dict(tl)[7], f"TLV {[t for t, _ in tl]}")
        break
check("REG-REQ réel présent dans le pcap", seen)

# ---- 5. a concatenated burst (ARP + DHCP DISCOVER + REQ) through the receiver and the core
a2 = mini_cmts.parser().parse_args(["--sim", "1", "--us-mode", "tdma", "--us-mod", "16qam", "--us-rate", "16", "--ds-channels", "1",
                                     "--no-grant-ahead-hold", "--grant-ahead-every", "1", "--grant-ahead-scale", "1"])
mux2, core2, rx2, _, _, _ = mini_cmts.build(a2, log=lambda *x: None)
m2 = Modem(CM, 1)
m2.state = "online"
core2.modems[CM], core2.by_sid[1] = m2, m2
got_eth = []
core2.on_data = lambda eth, ok, sid: got_eth.append((eth, ok))
rx2.push(0, mini_cmts.np.zeros(0, mini_cmts.np.complex64))
arp = b"\xff" * 6 + CM + b"\x08\x06" + struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1) + CM + bytes(4) + bytes(6) + bytes([10, 10, 0, 1])
burst = mac.concat_frame([mac.data_pdu(arp.ljust(60, b"\0")), mac.data_pdu(dhcp_discover()), mac.req_frame(1, 20)])
t1 = int(0.3 * TS_RATE) // mux2.ms_counts * mux2.ms_counts
n2 = 110
rx2.add_grants([(t1 - 40 * mux2.ms_counts, t1 - 8 * mux2.ms_counts, mac.IUC_REQUEST, mac.SID_BROADCAST),
                (t1, t1 + n2 * mux2.ms_counts, mac.IUC_LONG_DATA, 1)])
cm2 = SimModem(CM, rx2, delay_us=0, power_db=-20, freq_hz=150, snr_db=30, symrate=a2.us_rate * 160e3,
               ms_counts=mux2.ms_counts)
cm2._send(t1, burst, mac.IUC_LONG_DATA, "REQ")
rx2.push(0, cm2.render(0, int(rx2.sample_of_ts(t1 + 0.01 * TS_RATE))))
rx2.process()
check("rafale concaténée : 2 trames Ethernet + 1 requête extraites",
      len(got_eth) == 2 and all(ok for _, ok in got_eth) and got_eth[1][0] == dhcp_discover()
      and core2.pending.get(1) == 20 and core2.n["concat"] == 1,
      f"{len(got_eth)} trames, requête {core2.pending.get(1)} mini-slots, concat {core2.n['concat']}")

# ---- 5b. the real concatenated bursts of the Sagem 3686 (2026-10-09, FC 0xF8): every one splits
# into its announced number of frames, every Ethernet FCS checks
real_c = os.path.join(os.path.dirname(__file__), "data", "sagem3686", "concat_real.pcap")
d, i, n_ok, n_all, n_fr = open(real_c, "rb").read()[24:], 0, 0, 0, 0
while i < len(d):
    _, _, n, _ = struct.unpack("<IIII", d[i:i + 16])
    fr, i = d[i + 16:i + 16 + n], i + 16 + n
    inf = mac.parse_us_frame(fr)
    subs = mac.split_concat(fr, inf) if inf and inf["fc"] == mac.FC_CONCAT else []
    good = bool(subs) and len(subs) == inf["parm"] and all(
        s["kind"] != "DATA" or mac.pdu_ethernet(b, s)[1] for b, s in subs)
    n_ok += good
    n_all += 1
    n_fr += len(subs)
check("vraies rafales concaténées du Sagem (FC 0xF8) découpées, FCS bons", n_all and n_ok == n_all,
      f"{n_ok}/{n_all} rafales, {n_fr} trames")

# ---- 5c. downstream queues: management before data, data frames packed, stale data dropped
from cmts.downstream import DownstreamMux
from cmts.ts_demux import DocsisDemux
mx = DownstreamMux()
mx.packets(400)                                              # past the first SYNC/UCD/MAP
acks = [mac.data_pdu(bytes([0, 1, 2, 3, 4, i]) + CM + b"\x08\x00" + bytes(46)) for i in range(20)]
for f in acks:
    mx.send_data(f)
rsp = mac.rng_rsp_msg(mx.sa, CM, 1, 1, 0, 0, 0, mac.RNG_SUCCESS)
mx.ucast.append((rsp, None))
dm = DocsisDemux()
n_pk = 0
for _ in range(200):
    dm.push_packet(mx.next_packet())
    n_pk += 1
uc = [f for f in dm.frames if f in acks or f == rsp]
check("file DS : gestion d'abord, puis les 20 trames de données intactes et dans l'ordre",
      uc == [rsp] + acks, f"{len(uc)} trames")
used = mx.stats["data"]
check("file DS : petites trames regroupées (moins d'un paquet MPEG par trame)",
      used == 20 and len(acks) * len(acks[0]) < 20 * 188, f"{len(acks[0])} octets par trame")
mx.data_max_ms = 1.0
mx.send_data(acks[0])
import time as _t
_t.sleep(0.01)
for _ in range(50):
    mx.next_packet()
check("file DS : une trame qui a trop attendu est jetée", mx.stats["data_drop"] == 1 and not mx.data)

# ---- 6. grants in advance
core2.pending.clear()
core2.grant_ahead_s = 0.1
core2.grant_ahead_hold = True                     # --grant-ahead-hold (the default keeps the former rule)
mux2.now_ts = 10_000 * mux2.ms_counts          # DOCSIS clock at the time of the requests below


class B2:
    power_db = -20.0


core2._request(1, 30, B2())
g1 = core2.schedule(10_000, 300)
g2 = core2.schedule(10_080, 300)
# (a grant in advance carries mac.IUC_AHEAD for the upstream receiver; stripped from the MAP)
check("grant en avance : la demande puis le même grant au MAP suivant",
      g1 == [(1, mac.IUC_LONG_DATA, 30)] and g2 == [(1, mac.IUC_LONG_DATA | mac.IUC_AHEAD, 30)] and core2.n["ahead"] == 1,
      f"{g1} puis {g2}")
core2._request(1, 0, B2())                                   # piggyback 0: nothing more right now
check("un piggyback de 0 ne coupe pas les grants en avance (Sagem : ~100/s)",
      core2.schedule(10_160, 300) == [(1, mac.IUC_LONG_DATA | mac.IUC_AHEAD, 30)])
from cmts.upstream import BurstEvent
core2.on_burst(BurstEvent(mac.IUC_LONG_DATA, 1, 10_240 * mux2.ms_counts))   # one grant came back empty
check("un grant revenu vide ne les coupe pas non plus", core2.schedule(10_240, 300) == [(1, mac.IUC_LONG_DATA | mac.IUC_AHEAD, 30)])
core2._request(1, 12, B2())
core2.schedule(10_250, 300)                                  # the request itself
check("taille : celle de la dernière demande (12)",
      core2.schedule(10_260, 300) == [(1, (mac.IUC_SHORT_DATA if 12 <= mac.BURST_PROFILES[mac.IUC_SHORT_DATA]["max_burst"] else mac.IUC_LONG_DATA) | mac.IUC_AHEAD, 12)])
mux2.now_ts = 10_400 * mux2.ms_counts + int(0.08 * 10.24e6)   # 80 ms later, still inside the window
class B3:
    power_db, frame, info, error, mer, rho, head, freq = -20.0, b"x", {"kind": "OTHER"}, "", 30.0, 1.0, "", 0.0
core2.log = lambda *a: None
core2.on_burst(BurstEvent(mac.IUC_LONG_DATA, 1, mux2.now_ts, B3()))   # data received: window extended
until = core2.active[1][0]
check("une rafale de données prolonge la fenêtre de 100 ms", until == mux2.now_ts + int(0.1 * 10.24e6))
core2.active[1] = (0, 30)                                    # window over (DOCSIS time)
check("fin de fenêtre : plus de grant en avance", core2.schedule(10_800, 300) == [] and 1 not in core2.active)
core2.pending.clear()
core2.active.clear()
core2.pending.clear()
core2.active.clear()
core2.grant_ahead_every, core2.grant_ahead_scale = 3, 3.0    # one MAP in 3, 3x larger
core2._maps = 0
core2._request(1, 20, B2())
core2.schedule(10_900, 300)                                  # the request itself
gs = [core2.schedule(10_910 + 10 * i, 300) for i in range(6)]
check("un grant en avance tous les 3 MAP, 3 fois plus gros",
      sum(1 for g in gs if g) == 2 and all(g == [(1, mac.IUC_LONG_DATA | mac.IUC_AHEAD, 60)] for g in gs if g), str(gs))
core2.grant_ahead_every, core2.grant_ahead_scale = 1, 1.0
core2.grant_ahead_hold = False                    # former rule (default)
core2._request(1, 30, B2())
core2.schedule(11_000, 300)
core2._request(1, 0, B2())
check("règle par défaut : une demande de 0 arrête les grants en avance", core2.schedule(11_080, 300) == [])
core2.pending.clear()
core2.active.clear()
core2.grant_ahead_s = 0
core2._request(1, 30, B2())
core2.schedule(10_400, 300)
check("--grant-ahead-ms 0 : une demande = un grant", core2.schedule(10_480, 300) == [])

# ---- 7. Multiple Transmit Channel mode: a queue-depth request beyond one grant (255 mini-slots)
# is granted over the next MAPs (it was dropped beyond the first grant), and the flow's next
# request replaces what is left (it is the modem's queue depth)
from cmts.ranging import Modem  # noqa: E402
core2.pending.clear()
core2.active.clear()
core2.grant_ahead_s = 0                          # requests only
core2.mtc_owed = False                           # (the former rule: --no-mtc-owed)
mm = Modem(b"\x24\x7f\x20\xa8\x38\xb8", 1)
mm.mtc = True
core2.by_sid[1] = mm
core2.mtc_sids = {1}
b255 = core2._bytes_255()
core2._request_bytes(1, 60_000, B2())
gs = [core2.schedule(12_000 + 10 * i, 300) for i in range(-(-60_000 // b255) + 2)]
sizes = [g[0][2] for g in gs if g]
check("MTC : une demande de 60 000 octets = grants de 255 mini-slots aux MAP suivants",
      len(sizes) == -(-60_000 // b255) and all(n == 255 for n in sizes[:-1]) and gs[-1] == [],
      f"{b255} octets par grant de 255 ; {sizes}")
core2._request_bytes(1, 60_000, B2())
core2.schedule(13_100, 300)
core2._request_bytes(1, 1_000, B2())             # the modem's queue is down to 1000 bytes
g = core2.schedule(13_110, 300)
check("MTC : la demande suivante remplace le reste", len(g) == 1 and g[0][2] < 255 and core2.schedule(13_120, 300) == [],
      str(g))
core2.mtc_sids = set()

# ---- 8. --mtc-owed: requests add up (MULPIv3.0: only bytes not requested before), a Data Grant
# Pending (zero length) in every MAP while bytes are owed, padding clears the debt
core2.pending.clear()
core2.pending_more.clear()
core2.active.clear()
core2.owed.clear()
core2.mtc_owed = True
core2.mtc_sids = {1}
core2._request_bytes(1, 30_000, B2())
core2._request_bytes(1, 30_000, B2())            # 30 000 more, not the queue depth
gs = [core2.schedule(14_000 + 10 * i, 300) for i in range(-(-60_000 // b255) + 1)]
data = [x for g in gs for x in g if x[2]]
pend = [x for g in gs for x in g if x[2] == 0]
check("--mtc-owed : deux demandes de 30 000 octets = 60 000 accordés",
      sum(core2._bytes_in(x[2]) for x in data) >= 60_000 and gs[-1] == [], f"{[x[2] for x in data]}")
check("--mtc-owed : grant pending dans chaque MAP tant qu'il reste dû, après les grants",
      len(pend) == len(data) - 1 and all(g[-1][2] == 0 for g in gs[:len(data) - 1]), str(gs[:3]))
core2._request_bytes(1, 50_000, B2())
core2.on_burst(type("E", (), dict(burst=None, iuc=mac.IUC_ADV_LONG, sid=1))())   # (empty grant: no change)
check("--mtc-owed : la dette reste après un grant sans rafale", core2.owed.get(1, 0) == 50_000)
core2.owed.clear()
core2.mtc_owed = False
core2.mtc_sids = set()

print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
