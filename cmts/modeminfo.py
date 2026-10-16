"""What a modem says about itself, for the status page (cmts/webstatus.py, "Capacités").

Two sources the CMTS sees anyway:
  - its REG-REQ(-MP): the Modem Capabilities (TLV 5, MULPIv3.0 C.1.3.1), its Receive Channel
    Profiles (TLV 48), its vendor ID (TLV 8);
  - the DHCP requests crossing the bridge: the modem's own (option 60 vendor class, option 43
    device type / components / serial number / versions / model / vendor, option 125 capabilities)
    and those of the devices behind it (eRouter, eMTA, PCs), each recognised by its vendor class.
Reading the DHCP on the bridge works whatever serves it (provision.py or another server).
"""
import struct
import time

from . import cmcfg

VERSION = {0: "DOCSIS 1.0", 1: "DOCSIS 1.1", 2: "DOCSIS 2.0", 3: "DOCSIS 3.0", 4: "DOCSIS 3.1"}
PRIVACY = {0: "BPI", 1: "BPI+", 2: "BPI+ v2"}
US_RANGE = {0: "standard (5-42/65 MHz)", 1: "standard et étendue (5-85 MHz)"}
# eSAFE / embedded components in option 43.3 (CableLabs eDOCSIS)
COMPONENTS = {"ECM": "modem câble", "EMTA": "téléphonie (eMTA)", "EDVA": "téléphonie (eDVA)", "EROUTER": "routeur intégré",
              "ESTB": "décodeur TV (eSTB)", "EPS": "portail (ePS)", "ETEA": "adaptateur TDM (eTEA)",
              "EDSG": "passerelle DSG (eDSG)"}
SYMRATES = (160, 320, 640, 1280, 2560, 5120)
# option 43 sub-options of a DOCSIS CM (CableLabs CANN DHCP)
OPT43 = {2: "type", 3: "composants", 4: "numéro de série", 5: "version matérielle", 6: "version logicielle",
         7: "version du boot", 8: "OUI", 9: "modèle", 10: "fabricant"}
CABLELABS_ENTERPRISE = 4491


def _int(v):
    return int.from_bytes(v, "big") if v else 0


def _text(v):
    s = v.decode(errors="replace").strip("\0 ")
    return s if s and all(31 < ord(c) < 127 or c.isalpha() for c in s) else v.hex()


def capabilities(value):
    """TLV 5 value -> {"résumé": {...}, "tout": [(name, value text)]}."""
    try:
        subs = cmcfg.parse(value)
    except ValueError:
        return {"résumé": {}, "tout": [("TLV 5 illisible", value.hex())]}
    d = dict(subs)
    s = {}
    if 2 in d:
        s["version DOCSIS"] = VERSION.get(_int(d[2]), f"code {_int(d[2])}")
    if 29 in d:
        s["récepteurs descendants"] = _int(d[29])
    if 24 in d:
        s["émetteurs montants"] = _int(d[24])
    if 6 in d:
        s["confidentialité"] = PRIVACY.get(_int(d[6]), str(_int(d[6])))
    if 20 in d:
        s["plage montante"] = US_RANGE.get(_int(d[20]), f"code {_int(d[20])} (au-delà de MULPIv3.0 I17)")
    if 21 in d:
        bits = _int(d[21])
        s["débits symbole montants"] = ", ".join(f"{r / 1000:g}" for k, r in enumerate(SYMRATES) if bits >> k & 1) \
            + " Msym/s"
    for k, name in ((1, "concaténation"), (3, "fragmentation"), (4, "PHS"), (5, "IGMP"), (39, "IPv6"),
                    (17, "L2VPN"), (12, "DCC")):
        if k in d:
            s[name] = "oui" if _int(d[k]) else "non"
    if 7 in d:
        s["SAID descendants"] = _int(d[7])
    if 8 in d:
        s["flux de service montants"] = _int(d[8])
    if 30 in d:
        s["DSID"] = _int(d[30])
    if 40 in d:
        s["puissance montante étendue"] = f"{_int(d[40]) / 4:g} dB" if _int(d[40]) else "non"
    every = []
    for t, v in subs:
        name = cmcfg.CAP_NAMES.get(t, f"5.{t}")
        every.append((f"5.{t} {name}", str(_int(v)) if len(v) <= 4 else v.hex()))
    return {"résumé": s, "tout": every}


def from_regreq(tlvs, mp):
    """What a REG-REQ(-MP) tells (list of (type, value))."""
    out = {"message": "REG-REQ-MP" if mp else "REG-REQ", "reçu": time.time()}
    for t, v in tlvs:
        if t == 5:
            out["capacités"] = capabilities(v)
        elif t == 8:
            out["identifiant fabricant (OUI)"] = ":".join(f"{b:02x}" for b in v)
    try:
        from .registration import describe_rcps
        rcps = describe_rcps(tlvs)
        if rcps:
            out["profils de réception"] = rcps
    except Exception:                           # noqa: BLE001 - a malformed RCP: shown without
        pass
    return out


def _opt43(v):
    out = {}
    try:
        for t, x in cmcfg.parse(v):
            name = OPT43.get(t)
            if name:
                printable = x and all(32 <= c < 127 for c in x)
                out[name] = _text(x) if printable or t != 8 else x.hex(":")
                if t == 3 and printable:
                    out["composants (détail)"] = ", ".join(COMPONENTS.get(c.strip().upper(), c.strip())
                                                          for c in _text(x).split(":") if c.strip())
    except ValueError:
        pass
    return out


def _opt125(v):
    """V-I vendor-specific information (RFC 3925): the CableLabs part's capabilities (sub-option 5)."""
    i = 0
    while i + 5 <= len(v):
        ent, n = struct.unpack(">IB", v[i:i + 5])
        data = v[i + 5:i + 5 + n]
        i += 5 + n
        if ent != CABLELABS_ENTERPRISE:
            continue
        try:
            for t, x in cmcfg.parse(data):
                if t == 5:
                    return x
        except ValueError:
            pass
    return None


def device_kind(vendor_class):
    c = vendor_class.lower()
    if c.startswith("docsis"):
        return "modem câble (eCM)"
    if c.startswith("pktc"):
        return "téléphonie (eMTA)"
    if c.startswith("erouter"):
        return "routeur intégré (eRouter)"
    if c.startswith(("dpoe", "stb", "esafe")):
        return "eSAFE"
    return "équipement"


def from_dhcp(m):
    """A parsed DHCP request (netpkt.parse_dhcp) -> what its client tells."""
    o = m["opts"]
    cls = o.get(60, b"").decode(errors="replace")
    out = {"MAC": ":".join(f"{b:02x}" for b in m["chaddr"]), "classe": cls or "-", "nature": device_kind(cls),
           "vu": time.time()}
    if 12 in o:
        out["nom"] = _text(o[12])
    if 50 in o and len(o[50]) == 4:
        out["adresse demandée"] = ".".join(map(str, o[50]))
    if 43 in o:
        out.update(_opt43(o[43]))
    caps = _opt125(o[125]) if 125 in o else None
    if caps is None and cls.lower().startswith("docsis") and ":" in cls:
        try:                                    # DOCSIS 1.x/2.0: "docsis2.0:" + capabilities in hex
            caps = bytes.fromhex(cls.split(":", 1)[1])
        except ValueError:
            caps = None
    if caps:
        out["capacités (DHCP)"] = capabilities(caps)["résumé"]
    return out


def dhcp_of_frame(eth):
    """The DHCP request carried by an Ethernet frame (IPv4, UDP to port 67), parsed; None if not
    one. Checked by fixed offsets first: called for every upstream frame of the bridge."""
    if len(eth) < 14 + 20 + 8 + 240 or eth[12:14] != b"\x08\x00" or eth[23] != 17:
        return None
    ihl = (eth[14] & 15) * 4
    udp = 14 + ihl
    if eth[udp + 2:udp + 4] != b"\x00\x43":
        return None
    from .netpkt import parse_dhcp
    m = parse_dhcp(bytes(eth[udp + 8:]))
    return m if m and m["op"] == 1 else None
