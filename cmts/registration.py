"""CMTS side of registration (MULPIv3.0 10.2.6, DOCSIS 1.1 style REG-REQ / REG-RSP / REG-ACK).

    CM: REG-REQ (SID, config file TLVs + modem capabilities + CM MIC + CMTS MIC)
    CMTS: checks the CMTS MIC, gives every service flow an SFID (and a SID upstream), answers
          the modem capabilities with what this CMTS does -> REG-RSP (response 0 = okay)
    CM: REG-ACK -> the modem is online

The first upstream service flow keeps the SID given at ranging. Capabilities this CMTS does not
implement (fragmentation, PHS, DCC, multiple channels...) are answered "off" (concatenation is
accepted: several frames per burst), and
the DOCSIS version is capped at 1.1: our upstream is a DOCSIS 1.x TDMA channel.
"""
import struct

from . import cmcfg

DOCSIS_VERSION = 1           # modem capability 2: 0 = 1.0, 1 = 1.1, 2 = 2.0, 3 = 3.0
CAPS_OFF = (3, 4, 12, 15, 24, 29)      # fragmentation, PHS, DCC, expanded SIDs, multiple
#                                        transmit / receive channels (concatenation is accepted)

RESPONSE = {0: "ok", 1: "échec d'authentification", 2: "classe de service refusée"}


def capabilities_response(value, version=DOCSIS_VERSION, bonded=False):
    out = b""
    for t, v in cmcfg.parse(value):
        if t in CAPS_OFF and not (bonded and t == 29):   # 29: multiple receive channels
            v = bytes(len(v))
        elif t == 2 and v:
            v = bytes([min(v[0], version)])
        out += cmcfg.tlv(t, v)
    return cmcfg.tlv(5, out)


def build_response(tlvs, primary_sid, new_sfid, new_sid, version=DOCSIS_VERSION, bonded=False):
    """REG-REQ TLVs -> (REG-RSP TLV bytes, [(direction, reference, SFID, SID)])."""
    out, flows, first_us = b"", [], True
    for t, v in tlvs:
        if t == 5:
            out += capabilities_response(v, version, bonded)
        if t not in (24, 25):
            continue
        subs = cmcfg.parse(v)
        ref = dict(subs).get(1)
        sfid = new_sfid()
        sid = None
        if t == 24:
            sid = primary_sid if first_us else new_sid()
            first_us = False
        body = (cmcfg.tlv(1, ref) if ref else b"") + cmcfg.tlv(2, struct.pack(">I", sfid))
        if sid is not None:
            body += cmcfg.tlv(3, struct.pack(">H", sid))
        body += b"".join(cmcfg.tlv(s, sv) for s, sv in subs if s not in (1, 2, 3))
        out += cmcfg.tlv(t, body)
        flows.append(("montant" if t == 24 else "descendant", struct.unpack(">H", ref)[0] if ref else None,
                      sfid, sid))
    return out, flows


def receive_channel_config(cm_tlvs, channels):
    """RCC (TLV 49, MULPIv3.0 C.1.5.3) for a DOCSIS 3.0 modem, built from the Receive Channel
    Profile it reported (TLV 48, verbose: MDD TLV 4.2 = 1): one receive module, one receive
    channel per downstream channel, the first one primary. channels: [(DCID, frequency Hz)].
    Returns (TLV bytes, RCP-ID) or (b"", None) when the modem offers no profile that fits."""
    want = len(channels)
    best = None
    for t, v in cm_tlvs:
        if t != 48:
            continue
        sub = cmcfg.parse(v)
        rcp_id = dict(sub).get(1)
        rcs = [cmcfg.parse(x) for s, x in sub if s == 5]
        if rcp_id and len(rcs) >= want and (best is None or len(rcs) < best[1]):
            best = (rcp_id, len(rcs), rcs, [cmcfg.parse(x) for s, x in sub if s == 4])
    if best is None:
        return b"", None
    rcp_id, _, rcs, rms = best
    rm_index = dict(rms[0]).get(1, b"\x01") if rms else b"\x01"
    lowest = min(f for _, f in channels)
    body = cmcfg.tlv(1, rcp_id)
    body += cmcfg.tlv(4, cmcfg.tlv(1, rm_index) + cmcfg.tlv(4, struct.pack(">I", int(lowest))))
    for rc, (dcid, freq) in zip(rcs, channels):
        d = dict(rc)
        sub = cmcfg.tlv(1, d[1]) + cmcfg.tlv(2, d.get(2, b"\x40"))
        sub += cmcfg.tlv(4, struct.pack(">I", int(freq)))
        if dcid == channels[0][0]:
            sub += cmcfg.tlv(5, b"\x01")                    # primary downstream channel
        body += cmcfg.tlv(5, sub)
    return cmcfg.tlv(49, body), rcp_id


def dsid_encoding(dsid, dcids, wait_100us=100, warn_100us=50, holdoff_20ms=50):
    """DSID encodings (TLV 50) of a unicast resequencing DSID over the bonded channels."""
    reseq = cmcfg.tlv(1, b"\x01") + cmcfg.tlv(2, bytes(dcids)) + cmcfg.tlv(3, bytes([wait_100us])) \
        + cmcfg.tlv(4, bytes([warn_100us])) + cmcfg.tlv(5, struct.pack(">H", holdoff_20ms))
    body = cmcfg.tlv(1, dsid.to_bytes(3, "big")) + cmcfg.tlv(2, b"\x00") + cmcfg.tlv(3, reseq)
    return cmcfg.tlv(50, body)
