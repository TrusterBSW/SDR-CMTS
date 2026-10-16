"""What a modem says about itself (cmts/modeminfo.py), for the status page: the capabilities of a
real REG-REQ of the Sagem F@st 3686 (tests/data/sagem3686/registration.pcap), a DOCSIS 3.0
capability set built here (receivers, transmitters, US range, symbol rates), the DHCP of a modem
(option 60, option 43 sub-options, option 125 CableLabs capabilities) and of the devices behind it
(eRouter, eMTA), found in Ethernet frames as the bridge sees them."""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import cmcfg, modeminfo  # noqa: E402
from cmts import docsis_mac as mac  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def frames(path):
    with open(path, "rb") as f:
        f.read(24)
        while True:
            h = f.read(16)
            if len(h) < 16:
                return
            yield f.read(struct.unpack("<IIII", h)[2])


# a real REG-REQ
info = None
for fr in frames(os.path.join(os.path.dirname(__file__), "data", "sagem3686", "registration.pcap")):
    if len(fr) > 30 and (fr[0] & 0xFC) == 0xC0:
        hl = 6 + (fr[1] if fr[0] & 1 else 0)
        if fr[hl + 18] == mac.REG_REQ:
            info = modeminfo.from_regreq(cmcfg.parse(fr[hl + 22:len(fr) - 4]), False)
            break
s = (info or {}).get("capacités", {}).get("résumé", {})
check("REG-REQ réel du Sagem : version, confidentialité, fragmentation, OUI",
      s.get("confidentialité") == "BPI+" and s.get("fragmentation") == "oui" and "version DOCSIS" in s
      and info.get("identifiant fabricant (OUI)") == "24:7f:20", f"{s.get('version DOCSIS')}, {len(s)} champs")

caps = b"".join(cmcfg.tlv(t, bytes([v])) for t, v in ((2, 3), (29, 24), (24, 8), (20, 1), (21, 0b111000), (39, 1), (6, 1)))
r = modeminfo.capabilities(caps)["résumé"]
check("capacités DOCSIS 3.0 : récepteurs, émetteurs, plage, débits symbole, IPv6",
      r["version DOCSIS"] == "DOCSIS 3.0" and r["récepteurs descendants"] == 24 and r["émetteurs montants"] == 8
      and "5-85" in r["plage montante"] and r["débits symbole montants"] == "1.28, 2.56, 5.12 Msym/s" and r["IPv6"] == "oui",
      str(r))


def dhcp_frame(chaddr, opts):
    p = struct.pack(">BBBBIHH", 1, 1, 6, 0, 0x1234, 0, 0) + bytes(16) + chaddr.ljust(16, b"\0") + bytes(192)
    p += b"\x63\x82\x53\x63" + b"".join(bytes([c, len(v)]) + v for c, v in opts) + b"\xff"
    udp = struct.pack(">HHHH", 68, 67, 8 + len(p), 0) + p
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0, bytes(4), b"\xff" * 4) + udp
    return b"\xff" * 6 + chaddr + b"\x08\x00" + ip


cm = bytes.fromhex("247f20a838b8")
o43 = b"".join(cmcfg.tlv(t, v) for t, v in ((2, b"ECM"), (3, b"ECM:EMTA:EROUTER"), (4, b"SN123"), (5, b"1.0"),
                                              (6, b"FAST3686_OP_1.2"), (8, b"6C2E85"), (9, b"F@st 3686"),
                                              (10, b"Sagemcom")))
o125 = struct.pack(">IB", 4491, 2 + len(caps)) + cmcfg.tlv(5, caps)
m = modeminfo.dhcp_of_frame(dhcp_frame(cm, [(53, b"\x01"), (60, b"docsis3.0:"), (43, o43), (125, o125)]))
d = modeminfo.from_dhcp(m) if m else {}
check("DHCP du modem : nature, composants, série, modèle, fabricant, OUI",
      d.get("nature") == "modem câble (eCM)" and d.get("composants") == "ECM:EMTA:EROUTER" and d.get("numéro de série") == "SN123"
      and d.get("modèle") == "F@st 3686" and d.get("fabricant") == "Sagemcom" and d.get("OUI") == "6C2E85"
      and d.get("composants (détail)") == "modem câble, téléphonie (eMTA), routeur intégré", str(d)[:120])
check("DHCP du modem : capacités de l'option 125", d.get("capacités (DHCP)", {}).get("récepteurs descendants") == 24)
er = modeminfo.from_dhcp(modeminfo.dhcp_of_frame(dhcp_frame(bytes.fromhex("247f20a838b9"),
                                                            [(53, b"\x01"), (60, b"erouter1.0"), (12, b"box")])))
mta = modeminfo.from_dhcp(modeminfo.dhcp_of_frame(dhcp_frame(bytes.fromhex("247f20a838ba"),
                                                             [(53, b"\x01"), (60, b"pktc2.0:051f0101")])))
check("équipements derrière : eRouter et eMTA reconnus", er["nature"].endswith("(eRouter)") and er["nom"] == "box"
      and mta["nature"].endswith("(eMTA)"))
arp = b"\xff" * 6 + cm + b"\x08\x06" + bytes(28)
check("trame qui n'est pas du DHCP : ignorée", modeminfo.dhcp_of_frame(arp) is None
      and modeminfo.dhcp_of_frame(dhcp_frame(cm, [(53, b"\x01")])[:100]) is None)
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
