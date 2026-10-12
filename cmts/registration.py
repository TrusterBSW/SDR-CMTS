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


def capabilities_response(value, version=DOCSIS_VERSION, bonded=False, mtc=False):
    """mtc: Multiple Transmit Channel mode on (5.24 echoed; 5.37 = 0: MAPs and UCDs only on
    the primary downstream channel, where this CMTS sends them)."""
    out = b""
    for t, v in cmcfg.parse(value):
        if t == 24 and mtc:
            pass
        elif t == 37 and mtc:
            v = bytes(len(v))
        elif t in CAPS_OFF and not (bonded and t == 29):   # 29: multiple receive channels
            v = bytes(len(v))
        elif t == 2 and v:
            v = bytes([min(v[0], version)])
        out += cmcfg.tlv(t, v)
    return cmcfg.tlv(5, out)


def build_response(tlvs, primary_sid, new_sfid, new_sid, version=DOCSIS_VERSION, bonded=False, clusters=None,
                   criteria=False):
    """REG-REQ TLVs -> (REG-RSP TLV bytes, [(direction, reference, SFID, SID)]).
    clusters: Multiple Transmit Channel mode, {UCID: SID} of the first upstream flow (its SID
    cluster 0, TLV 47; no SID in the flow itself); the CM's other upstream flows are not
    supported then (refused with no SID). criteria: the SID Cluster Switching Criteria (47.3),
    all 0 = unlimited, as a real CMTS (ARRIS) sends them."""
    out, flows, first_us = b"", [], True
    for t, v in tlvs:
        if t == 5:
            out += capabilities_response(v, version, bonded, mtc=bool(clusters))
        if t not in (24, 25):
            continue
        subs = cmcfg.parse(v)
        ref = dict(subs).get(1)
        sfid = new_sfid()
        sid = None
        if t == 24:
            sid = primary_sid if first_us else (None if clusters else new_sid())
            if clusters and first_us:
                out += sid_cluster_assignment(sfid, clusters, criteria)
            first_us = False
        body = (cmcfg.tlv(1, ref) if ref else b"") + cmcfg.tlv(2, struct.pack(">I", sfid))
        if sid is not None and not clusters:
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


# ---------------------------------------------------------------- Multiple Transmit Channel mode
def _tlv(t, v):
    assert len(v) <= 255, (t, len(v))
    return bytes([t, len(v)]) + v


def tcc_encodings(first, adds, drw_qdb, keep_first=True, drw_action=0, technique=2, first_ref=1):
    """Transmit Channel Configuration (TLV 46, MULPIv3.0 C.1.5.1) of a REG-RSP-MP: the channel
    the CM ranged on (`first`, UCID) kept as it is, the channels of `adds` added with unicast
    ranging (technique 2, ranging parameters: those of `first`), and the Dynamic Range Window
    (Pload_min_set, 1/4 dB below Phi). adds: [(UCID, UCD contents (docsis_mac.ucd_body), ranging SID)].
    A TCC longer than 254 bytes is cut into successive TLV 46, the UCD (46.5) into fragments.
    keep_first: the "no action" TCC of `first`; drw_action: None = no DRW TCC; technique: 46.7
    (None: absent, broadcast initial ranging; 1: broadcast; 2, 3, 4 with ranging parameters);
    first_ref: the first TCC reference (a real CMTS, ARRIS, starts at 0)."""
    out = b""
    ref = first_ref - 1
    tccs = []
    if keep_first:
        ref += 1
        tccs.append([(1, bytes([ref])), (2, b"\x00"), (3, bytes([first]))])
    for ucid, ucd, rsid in adds:
        ref += 1
        t = [(1, bytes([ref])), (2, b"\x01"), (3, bytes([ucid])), (6, struct.pack(">H", rsid))]
        if technique is not None:
            t.append((7, bytes([technique])))
        if technique in (2, 3, 4):
            rp = _tlv(1, bytes([first])) + _tlv(2, struct.pack(">i", 0)) + _tlv(4, struct.pack(">b", 0))
            t.append((8, rp))
        tccs.append(t + [(5, ucd)])
    if drw_action is not None:
        ref += 1
        tccs.append([(1, bytes([ref])), (2, bytes([drw_action])), (3, b"\x00"),
                     (9, bytes([max(0, min(255, int(drw_qdb)))]))])
    for subs in tccs:
        head = _tlv(*subs[0])                              # TCC reference, repeated in each fragment
        cur = b""
        for t, v in subs:
            while True:
                room = 254 - len(cur) - 2
                if t == 5 and len(v) > room:               # UCD: as much as fits, the rest after
                    if room > 0:
                        cur += _tlv(5, v[:room])
                        v = v[room:]
                    out += _tlv(46, cur)
                    cur = head
                    continue
                if len(cur) + 2 + len(v) > 254:
                    out += _tlv(46, cur)
                    cur = head
                cur += _tlv(t, v)
                break
        if cur:
            out += _tlv(46, cur)
    return out


def sid_cluster_assignment(sfid, sids, criteria=False):
    """Service Flow SID Cluster Assignment (TLV 47, MULPIv3.0 C.1.5.2): one cluster (id 0) of the
    flow `sfid`, sids = {UCID: SID}; criteria: the SID Cluster Switching Criteria (47.3: maximum
    requests, outstanding bytes, total bytes, time per cluster) all 0, i.e. unlimited."""
    enc = _tlv(1, b"\x00")
    for ucid, sid in sorted(sids.items()):
        enc += _tlv(2, _tlv(1, bytes([ucid])) + _tlv(2, struct.pack(">H", sid)) + _tlv(3, b"\x01"))
    body = _tlv(1, struct.pack(">I", sfid)) + _tlv(2, enc)
    if criteria:
        body += _tlv(3, _tlv(1, b"\x00") + _tlv(2, bytes(4)) + _tlv(3, bytes(4)) + _tlv(4, bytes(2)))
    return _tlv(47, body)


def init_channel_timeout(seconds=60):
    """Initializing Channel Timeout (TLV 52, MULPIv3.0 C.1.5.5), seconds: 60 is the default, sent
    as such by a real CMTS (ARRIS)."""
    return _tlv(52, struct.pack(">H", seconds))


def parse_tcc_errors(tlvs):
    """REG-ACK TLV 46 (TCC error encodings 46.254) -> readable strings."""
    out = []
    for t, v in tlvs:
        if t != 46:
            continue
        d = cmcfg.parse(v)
        ref = dict(d).get(1, b"?")
        for s, sv in d:
            if s == 254:
                e = dict(cmcfg.parse(sv))
                out.append(f"TCC {ref.hex()} : paramètre {e.get(1, b'').hex()} erreur "
                           f"{int.from_bytes(e.get(2, b'0'), 'big')} {e.get(3, b'').decode('latin-1', 'replace')}")
    return out
