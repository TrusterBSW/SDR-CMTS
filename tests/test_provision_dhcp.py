# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Who provision.py's DHCP server takes for a cable modem: a docsis / pktc vendor class gets the
config file and no name server; an eRouter, a set-top box, a PC (any other class, or none) gets
the --dns servers and no config file (until 2026-11-10 they all got cm.cfg and no DNS: no
Internet behind the modems). --config-to-all brings the former behaviour back."""
import ipaddress
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import provision  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def server(**kw):
    d = object.__new__(provision.Dhcp)          # no socket: only the decision and the reply
    d.a = NS(**{**dict(lease=3600, config_name="cm.cfg", dns="1.1.1.1,9.9.9.9", config_to_all=False), **kw})
    d.net = ipaddress.IPv4Network("10.10.0.0/24")
    d.server = ipaddress.IPv4Address("10.10.0.1")
    return d


def options(pkt):
    i, out = 240, {}
    while i < len(pkt) and pkt[i] != 255:
        out[pkt[i]] = pkt[i + 2:i + 2 + pkt[i + 1]]
        i += 2 + pkt[i + 1]
    return out, pkt[108:236].rstrip(b"\0")


req = dict(xid=1, flags=0, giaddr="0.0.0.0", chaddr=bytes(6))
d = server()
for cls, modem in (("docsis3.0:", True), ("docsis3.0", True), ("pktc1.5:", True), ("eRouter1.0", False),
                   ("STB-NC", False), ("MSFT 5.0", False), ("", False)):
    got = d.is_modem(cls)
    o, f = options(d.reply(req, 5, ipaddress.IPv4Address("10.10.0.20"), got))
    want = (b"cm.cfg", None) if modem else (b"", bytes([1, 1, 1, 1, 9, 9, 9, 9]))
    check(f"« {cls or 'sans classe'} » : {'modem, cm.cfg sans DNS' if modem else 'appareil, DNS sans cm.cfg'}",
          got == modem and (f, o.get(6)) == want, f"fichier {f!r}, DNS {o.get(6)!r}")
check("--config-to-all : l'ancien comportement (un eRouter reçoit cm.cfg)", server(config_to_all=True).is_modem("eRouter1.0"))
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
