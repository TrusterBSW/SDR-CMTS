# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""CM-STATUS (MULPIv3.0 6.4.34, 10.4.3): the MDD of the primary channel enables the reports (TLV 11
per event type, upstream bits in TLV 7.2); a CM-STATUS is acknowledged (CM-STATUS-ACK, same
transaction and event); a downstream channel the modem reports lost leaves its bonded channels, and
comes back when it reports it acquired; a repeated report is acknowledged again but acted on once."""
import os
import struct
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import cmcfg                               # noqa: E402
from cmts import docsis_mac as mac                   # noqa: E402
from cmts.downstream import DownstreamMux            # noqa: E402
from cmts.ranging import CmtsCore, Modem             # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


CM = bytes.fromhex("247f20a838b8")
plan = [(1, 306e6, 256, True), (2, 314e6, 256, False), (3, 322e6, 256, False)]
mdd = mac.mdd_msg(bytes(6), 1, 1, plan, [1, 2], status_events=sorted(mac.CM_STATUS_EVENTS))
body = mac.parse_us_frame(mdd)
tl = cmcfg.parse(mdd[6 + 20:-4])
ev = sorted(dict(cmcfg.parse(v))[1][0] for t, v in tl if t == 11)
us_masks = {dict(cmcfg.parse(v))[1][0]: struct.unpack(">H", dict(cmcfg.parse(v))[2])[0] for t, v in tl if t == 7}
check("MDD : TLV 11 pour chaque événement activé", ev == [1, 2, 4, 5, 6, 7, 8], str(ev))
check("MDD : bits montants 6-8 dans TLV 7.2", all(m_ == 0x1C0 for m_ in us_masks.values()), str(us_masks))
plain = mac.mdd_msg(bytes(6), 1, 2, plan, [1, 2])
check("MDD d'un canal secondaire : pas de TLV 11", all(t != 11 for t, _ in cmcfg.parse(plain[6 + 20:-4])))

mux = DownstreamMux(qam=256, sps=4)
core = CmtsCore(mux, log=lambda s: logs.append(s))
logs = []
m = Modem(CM, 1)
core.modems[CM], core.by_sid[1] = m, m
core.bonding = [(1, 306e6), (2, 314e6), (3, 322e6)]
core.bonded[CM] = 0x1001
core.bonded_channels[CM] = [1, 3]                    # channel 2 missed at registration


def report(tid, event, ds):
    tlvs = b"".join(cmcfg.tlv(1, cmcfg.tlv(4, bytes([c]))) for c in ds)
    f = mac.mgmt_frame(mac.CM_STATUS, struct.pack(">HB", tid, event) + tlvs, CM, da=mux.sa, version=4)
    info = mac.parse_us_frame(f)
    core._dispatch(NS(us_ch=1, sid=1), NS(power_db=-14.0), f, info, "9")


report(1, 5, [2])
ack = mac.parse_us_frame(mux.ucast[-1][0])
check("CM-STATUS acquitté (même transaction, même événement)",
      ack["mtype"] == mac.CM_STATUS_ACK and ack["da"] == CM and ack["body"][:3] == struct.pack(">HB", 1, 5))
check("canal 2 retrouvé : de nouveau dans l'agrégation", core.bonded_channels[CM] == [1, 2, 3], str(core.bonded_channels))
report(1, 5, [2])
check("répétition : acquittée encore, traitée une fois",
      len(mux.ucast) == 2 and sum("CM-STATUS] 24" in s_ and "verrouillage" in s_ for s_ in logs) == 1, str(logs))
report(2, 2, [3])
check("canal 3 perdu : retiré", core.bonded_channels[CM] == [1, 2], str(core.bonded_channels))
report(3, 2, [1])
check("le primaire n'est jamais retiré", core.bonded_channels[CM] == [1, 2], str(core.bonded_channels))
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
