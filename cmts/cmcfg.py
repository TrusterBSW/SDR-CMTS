"""DOCSIS CM configuration file (MULPIv3.0 annex C / D): TLV encoding, CM MIC, CMTS MIC.

Used by provision.py (it generates the file served by TFTP) and by the CMTS (it checks the
CMTS MIC of the REG-REQ and prints the TLVs). A config file is a list of TLVs (type 1 byte,
length 1 byte), then the CM MIC (TLV 6: MD5 of everything before it), the CMTS MIC (TLV 7:
HMAC-MD5 keyed with the CMTS shared secret over a fixed list of TLVs), an end marker (255) and
zero padding to a multiple of 4 bytes.

The CMTS MIC order below is the one of MULPIv3.0 D.3.1 as far as this lab needs it. A file
made by another tool with TLVs outside this list may give a different digest: the CMTS then
only warns (see mini_cmts.py --strict-mic).
"""
import hashlib
import hmac
import struct

END, PAD = 255, 0
CM_MIC, CMTS_MIC = 6, 7

# TLVs covered by the CMTS MIC, in digest order (MULPIv3.0 D.3.1)
# Only config file TLVs: those the modem adds to its REG-REQ (5 capabilities, 8 vendor ID, 12 IP
# address, 44 vendor capabilities, 57 initialization reason...) are left out. TLV 57 in this
# list once made a correct MIC look wrong (Sagem 3686, 2026-10-09).
# Nor the SNMP TLVs 53-55, which the modem keeps for itself and does not copy into its REG-REQ
# (with TLV 53 in the list, a correct MIC looked wrong, Sagem 3686, 2026-10-09).
CMTS_MIC_ORDER = [1, 2, 3, 4, 17, 43, 6, 18, 19, 20, 22, 23, 24, 25, 28, 29, 26, 35, 36, 37, 40,
                  41, 42, 45, 47, 49, 52, 56, 58, 59, 60, 61, 62, 63, 67, 68]

NAMES = {
    1: "fréquence descendante", 2: "canal montant", 3: "accès réseau", 4: "classe de service 1.0",
    5: "capacités du modem", 6: "CM MIC", 7: "CMTS MIC", 8: "vendor ID", 9: "fichier de mise à jour",
    10: "accès SNMP en écriture", 11: "objet SNMP", 12: "adresse IP du modem", 14: "MAC CPE",
    17: "BPI", 18: "CPE max", 19: "horodatage TFTP", 20: "adresse provisionnée TFTP",
    21: "serveur de mise à jour", 22: "classifieur montant", 23: "classifieur descendant",
    24: "flux de service montant", 25: "flux de service descendant", 26: "PHS", 28: "classifieurs max",
    29: "chiffrement (privacy)", 32: "CVC fabricant", 35: "contrôle de gestion abonné", 39: "mode 2.0", 44: "capacités constructeur", 57: "raison de l'initialisation",
    43: "extensions DOCSIS / constructeur", 53: "SNMPv1v2c", 255: "fin",
}

SF_NAMES = {
    1: "référence", 2: "SFID", 3: "SID", 4: "classe", 5: "erreur", 6: "type de jeu QoS", 7: "priorité",
    8: "débit max (bit/s)", 9: "rafale max (octets)", 10: "débit min réservé", 12: "délai actif",
    13: "délai admis", 14: "concaténation max", 15: "ordonnancement", 16: "politique de requête",
}

CAP_NAMES = {
    1: "concaténation", 2: "version DOCSIS", 3: "fragmentation", 4: "PHS", 5: "IGMP", 6: "BPI",
    7: "SAID descendants", 8: "SID montants", 9: "filtrage", 10: "taps/symbole", 11: "taps",
    12: "DCC", 13: "filtres IP", 14: "filtres LLC", 15: "SID étendus", 16: "ranging hold-off",
    17: "L2VPN", 18: "eSAFE", 19: "DUT", 20: "plage montante", 21: "débits symbole", 22: "SAC 2",
    23: "code hopping 2", 24: "canaux montants multiples", 25: "canaux montants 2.0",
    26: "canaux montants 1.x", 27: "SID clusters", 28: "SID par flux", 29: "canaux descendants multiples",
}


def tlv(t, v):
    if len(v) > 255:
        raise ValueError(f"TLV {t}: {len(v)} octets, 255 au plus")
    return bytes([t, len(v)]) + v


def parse(buf):
    """bytes -> list of (type, value); stops at the end marker, skips padding."""
    out, i = [], 0
    while i < len(buf):
        t = buf[i]
        if t == PAD:
            i += 1
            continue
        if t == END:
            break
        if i + 2 > len(buf) or i + 2 + buf[i + 1] > len(buf):
            raise ValueError(f"TLV {t} tronqué à l'octet {i}")
        n = buf[i + 1]
        out.append((t, bytes(buf[i + 2:i + 2 + n])))
        i += 2 + n
    return out


def cmts_mic(tlvs, secret):
    """HMAC-MD5 over the TLVs of CMTS_MIC_ORDER, in that order (each type: every occurrence,
    in file order)."""
    data = b"".join(tlv(t, v) for want in CMTS_MIC_ORDER for t, v in tlvs if t == want)
    return hmac.new(secret.encode(), data, hashlib.md5).digest()


def service_flow(ref, max_rate, upstream, max_burst=3044, prio=0, sched=2):
    sub = tlv(1, struct.pack(">H", ref)) + tlv(6, bytes([7]))       # provisioned+admitted+active
    sub += tlv(7, bytes([prio])) + tlv(8, struct.pack(">I", max_rate)) + tlv(9, struct.pack(">I", max_burst))
    if upstream:
        sub += tlv(14, struct.pack(">H", 0))                           # max concatenated burst: no
        #                                                                limit (1.1 default: 1522 B)
        sub += tlv(15, bytes([sched]))                                 # 2 = best effort
    return tlv(24 if upstream else 25, sub)


def snmp_community(name, net="10.10.0.0", mask="255.255.255.0"):
    """TLV 53 (SNMPv1v2c coexistence, MULPIv3.0 C.1.2.13): a read-only community, from one subnet
    only (TAddress = IPv4 + UDP port; port 0 / mask 0 = any port)."""
    ip = lambda a: bytes(int(x) for x in a.split("."))
    addr = tlv(1, ip(net) + b"\0\0") + tlv(2, ip(mask) + b"\0\0")
    return 53, tlv(1, name.encode()) + tlv(2, addr) + tlv(3, bytes([1]))


def build(secret, ds_rate=50_000_000, us_rate=5_000_000, max_cpe=4, network_access=True, extra=(),
          us_burst=3044):
    """A minimal DOCSIS 1.1+ config: network access, CPE limit, one best-effort service flow each
    way, no BPI. Returns the file bytes (MICs, end marker and padding included)."""
    body = [(3, bytes([1 if network_access else 0])), (18, bytes([max_cpe])), (29, bytes([0]))]
    body += [(t, v) for t, v in parse(service_flow(1, us_rate, True, max_burst=us_burst)
                                      + service_flow(2, ds_rate, False))]
    body += list(extra)
    raw = b"".join(tlv(t, v) for t, v in body)
    cm_mic = hashlib.md5(raw).digest()
    body.append((CM_MIC, cm_mic))
    out = raw + tlv(CM_MIC, cm_mic) + tlv(CMTS_MIC, cmts_mic(body, secret)) + bytes([END])
    return out + bytes(-len(out) % 4)


def check(buf, secret=None):
    """Config file bytes -> (tlvs, problems): CM MIC and (if a secret is given) CMTS MIC."""
    tlvs, problems = parse(buf), []
    i = buf.find(bytes([CM_MIC, 16]))
    mic = dict(tlvs).get(CM_MIC)
    if mic is None:
        problems.append("pas de CM MIC")
    elif hashlib.md5(buf[:i]).digest() != mic:
        problems.append("CM MIC faux")
    if secret is not None:
        got = dict(tlvs).get(CMTS_MIC)
        if got is None:
            problems.append("pas de CMTS MIC")
        elif cmts_mic(tlvs, secret) != got:
            problems.append("CMTS MIC faux (secret différent ?)")
    return tlvs, problems


def _val(v):
    if len(v) == 1:
        return str(v[0])
    if len(v) == 2:
        return str(struct.unpack(">H", v)[0])
    if len(v) == 4:
        return str(struct.unpack(">I", v)[0])
    return v.hex() if any(c < 32 or c > 126 for c in v) else repr(v.decode())


def describe(tlvs, indent="   "):
    """Readable lines for a TLV list (config file or REG-REQ body)."""
    lines = []
    for t, v in tlvs:
        name = NAMES.get(t, f"TLV {t}")
        if t in (24, 25, 22, 23, 5):
            names = SF_NAMES if t in (24, 25) else CAP_NAMES if t == 5 else {}
            try:
                subs = ", ".join(f"{names.get(s, s)}={_val(sv)}" for s, sv in parse(v))
            except ValueError:
                subs = v.hex()
            lines.append(f"{indent}{t:3d} {name}: {subs}")
        elif t in (6, 7):
            lines.append(f"{indent}{t:3d} {name}: {v.hex()}")
        else:
            lines.append(f"{indent}{t:3d} {name}: {_val(v)}")
    return lines
