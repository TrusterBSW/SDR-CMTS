"""DOCSIS MAC downstream management messages (SYNC, UCD, MAP) and MPEG-TS packing."""
import struct
import zlib

import numpy as np

ALL_CM_MCAST = bytes.fromhex("01e02f000001")
DOCSIS_PID = 0x1FFE
NULL_PID = 0x1FFF

# MAC management message types
SYNC, UCD, MAP, RNG_REQ, RNG_RSP = 1, 2, 3, 4, 5
REG_REQ, REG_RSP, REG_ACK = 6, 7, 14
INIT_RNG_REQ, B_INIT_RNG_REQ = 30, 34
REG_REQ_MP, REG_RSP_MP = 44, 45
MDD = 33

# Interval Usage Codes
IUC_REQUEST, IUC_REQ_DATA, IUC_INIT_MAINT, IUC_STATION_MAINT = 1, 2, 3, 4
IUC_SHORT_DATA, IUC_LONG_DATA, IUC_NULL = 5, 6, 7
IUC_ADV_SHORT, IUC_ADV_LONG, IUC_ADV_UGS = 9, 10, 11      # DOCSIS 2.0 ATDMA data
UCD29 = 29

SID_BROADCAST = 0x3FFF


def crc16_ccitt(data):
    """HCS: CRC-CCITT (x^16+x^12+x^5+1) as in ITU-T X.25 (bit-reflected, init/xor 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def mac_frame(fc, payload, mac_parm=0):
    hdr = struct.pack(">BBH", fc, mac_parm, len(payload))
    return hdr + struct.pack("<H", crc16_ccitt(hdr)) + payload


def mgmt_frame(msg_type, body, sa, da=ALL_CM_MCAST, version=1, fc=0xC2):
    """MAC management message: MAC header + 802.3/LLC header + body + CRC-32."""
    llc = struct.pack(">BBBBBB", 0, 0, 0x03, version, msg_type, 0)
    msg = da + sa + struct.pack(">H", len(llc) + len(body)) + llc + body
    msg += struct.pack("<I", zlib.crc32(msg))
    return mac_frame(fc, msg)


# SYNC uses the timing MAC header (FC_PARM = 0); timestamp offset inside the frame:
# MAC hdr 6 + DA 6 + SA 6 + len 2 + LLC 6 = 26
SYNC_TS_OFFSET = 26


def sync_msg(sa, timestamp):
    return mgmt_frame(SYNC, struct.pack(">I", timestamp & 0xFFFFFFFF), sa, fc=0xC0)


def tlv(t, v):
    return bytes([t, len(v)]) + v


def burst_descriptor(iuc, mod_qpsk=True, preamble_len=64, preamble_off=0, fec_t=0, fec_k=16,
                     seed=0x152, max_burst=0, guard=8, last_cw_short=False, scrambler=True,
                     diff=False):
    sub = b"".join([
        tlv(1, bytes([1 if mod_qpsk else 2])),
        tlv(2, bytes([1 if diff else 2])),
        tlv(3, struct.pack(">H", preamble_len)),
        tlv(4, struct.pack(">H", preamble_off)),
        tlv(5, bytes([fec_t])),
        tlv(6, bytes([fec_k])),
        tlv(7, struct.pack(">H", seed)),
        tlv(8, bytes([max_burst])),
        tlv(9, bytes([guard])),
        tlv(10, bytes([2 if last_cw_short else 1])),
        tlv(11, bytes([1 if scrambler else 2])),
    ])
    return tlv(4, bytes([iuc]) + sub)


# Preamble superstring (QPSK0). Any pattern is legal; this one was picked for the burst
# receiver: each IUC uses its own segment (no overlap), and every segment has autocorrelation
# sidelobes below -16 dB (a periodic pattern such as cc cc .. 0d 0d .. makes the arrival
# time ambiguous by a few symbols, which ruins ranging).
PREAMBLE = bytes.fromhex(
    "a0bef4161b1077563ffe2b4e59083705ecea65210e1affc09e1b549c91897e2b2f820c4adf2e3db653a58d"
)

# Burst profiles announced in the UCD (DOCSIS 1.x type-4 descriptors, QPSK, close to a classic
# Cisco "qpsk" profile). preamble_len / preamble_off are in bits of PREAMBLE, guard in symbols.
BURST_PROFILES = {
    IUC_REQUEST: dict(preamble_len=64, preamble_off=0, fec_t=0, fec_k=16, max_burst=0, guard=8),
    IUC_INIT_MAINT: dict(preamble_len=128, preamble_off=64, fec_t=5, fec_k=34, guard=48),
    IUC_STATION_MAINT: dict(preamble_len=128, preamble_off=64, fec_t=5, fec_k=34, guard=48),
    IUC_SHORT_DATA: dict(preamble_len=72, preamble_off=192, fec_t=3, fec_k=76, max_burst=12, guard=8,
                         last_cw_short=True),
    IUC_LONG_DATA: dict(preamble_len=80, preamble_off=264, fec_t=8, fec_k=220, guard=8,
                        last_cw_short=True),
}


_TDMA_PROFILES = {k: dict(v) for k, v in BURST_PROFILES.items()}
# DOCSIS 2.0 ATDMA (UCD type 29, burst descriptors type 5), laid out like the SFR network's UCD 29
# (capture of 2026-10-06: 5.12 Msym/s, 16-QAM data in IUC 9/10, R-S interleaver off). The preamble
# is always QPSK (type 1 = QPSK0), whatever the data modulation.
_ATDMA_DATA = dict(preamble_len=104, preamble_off=192, guard=8, last_cw_short=True, atdma=True)
MOD5 = {2: 1, 4: 2, 6: 5}                                 # bits/symbol -> type-5 modulation code


def configure_upstream(mode="tdma", bits=4):
    """Burst profiles of the upstream channel: TDMA (UCD type 2, data IUC 5/6, QPSK or 16-QAM
    whose preamble is 16-QAM too) or ATDMA (UCD type 29, data IUC 9/10, QPSK/16/64-QAM)."""
    BURST_PROFILES.clear()
    if mode == "tdma":
        BURST_PROFILES.update({k: dict(v) for k, v in _TDMA_PROFILES.items()})
        if bits == 4:
            for iuc in (IUC_SHORT_DATA, IUC_LONG_DATA):
                BURST_PROFILES[iuc]["mod_qpsk"] = False
        elif bits != 2:
            raise ValueError("TDMA : QPSK ou 16-QAM seulement")
        return
    for iuc in (IUC_REQUEST, IUC_INIT_MAINT, IUC_STATION_MAINT):
        BURST_PROFILES[iuc] = dict(_TDMA_PROFILES[iuc], atdma=True)
    BURST_PROFILES[IUC_ADV_SHORT] = dict(_ATDMA_DATA, fec_t=6, fec_k=78, max_burst=14, bits=bits)
    BURST_PROFILES[IUC_ADV_LONG] = dict(_ATDMA_DATA, fec_t=8, fec_k=224, bits=bits)


def data_iucs():
    """(short, long) data IUCs of the configured upstream."""
    return (IUC_ADV_SHORT, IUC_ADV_LONG) if IUC_ADV_LONG in BURST_PROFILES else (IUC_SHORT_DATA, IUC_LONG_DATA)


def burst_descriptor5(iuc, bits=2, preamble_len=64, preamble_off=0, fec_t=0, fec_k=16, seed=0x152,
                      max_burst=0, guard=8, last_cw_short=False, scrambler=True, diff=False,
                      ir_depth=1, ir_block=2048, preamble_type=1, **_):
    sub = b"".join([
        tlv(1, bytes([MOD5[bits]])), tlv(2, bytes([1 if diff else 2])),
        tlv(3, struct.pack(">H", preamble_len)), tlv(4, struct.pack(">H", preamble_off)),
        tlv(5, bytes([fec_t])), tlv(6, bytes([fec_k])), tlv(7, struct.pack(">H", seed)),
        tlv(8, bytes([max_burst])), tlv(9, bytes([guard])), tlv(10, bytes([2 if last_cw_short else 1])),
        tlv(11, bytes([1 if scrambler else 2])), tlv(12, bytes([ir_depth])), tlv(13, struct.pack(">H", ir_block)),
        tlv(14, bytes([preamble_type])),
    ])
    return tlv(5, bytes([iuc]) + sub)


def ucd_msg(sa, us_chan_id, ccc, minislot_ticks, ds_chan_id, freq_hz, symrate_mult):
    mtype, body = ucd_body(us_chan_id, ccc, minislot_ticks, ds_chan_id, freq_hz, symrate_mult)
    return mgmt_frame(mtype, body, sa)


def ucd_body(us_chan_id, ccc, minislot_ticks, ds_chan_id, freq_hz, symrate_mult):
    """UCD contents without the MAC management header -> (message type, bytes); also the TCC
    UCD substitution (TLV 46.5)."""
    body = struct.pack(">BBBB", us_chan_id, ccc, minislot_ticks, ds_chan_id)
    body += tlv(1, bytes([symrate_mult]))          # x 160 ksym/s
    body += tlv(2, struct.pack(">I", int(freq_hz)))
    body += tlv(3, PREAMBLE)
    if IUC_ADV_LONG in BURST_PROFILES:             # ATDMA: UCD type 29, S-CDMA off
        body += tlv(7, bytes([2]))
        for iuc, prof in BURST_PROFILES.items():
            p = dict(prof)
            p.setdefault("bits", 2 if p.get("mod_qpsk", True) else 4)
            body += burst_descriptor5(iuc, **p)
        return UCD29, body
    for iuc, prof in BURST_PROFILES.items():
        body += burst_descriptor(iuc, **{k: v for k, v in prof.items() if k != "atdma"})
    return UCD, body


def map_ie(sid, iuc, offset):
    return struct.pack(">I", ((sid & 0x3FFF) << 18) | ((iuc & 0xF) << 14) | (offset & 0x3FFF))


def map_msg(sa, us_chan_id, ucd_count, alloc_start, ack_time, ies, length,
            rng_bo=(3, 6), data_bo=(2, 8)):
    """ies: list of (sid, iuc, offset) in mini-slots relative to alloc_start;
    a Null IE at `length` closes the map."""
    elems = b"".join(map_ie(*ie) for ie in ies) + map_ie(0, IUC_NULL, length)
    body = struct.pack(">BBBBIIBBBB", us_chan_id, ucd_count, len(ies) + 1, 0,
                       alloc_start & 0xFFFFFFFF, ack_time & 0xFFFFFFFF,
                       rng_bo[0], rng_bo[1], data_bo[0], data_bo[1])
    return mgmt_frame(MAP, body + elems, sa)


# ---------------------------------------------------------------- MPEG-TS
class TSPacketizer:
    def __init__(self):
        self.cc = 0
        null = np.full(188, 0xFF, dtype=np.uint8)
        null[:4] = [0x47, NULL_PID >> 8, NULL_PID & 0xFF, 0x10]
        self.null = null

    def _hdr(self, pusi):
        h = bytes([0x47, (0x40 if pusi else 0) | (DOCSIS_PID >> 8), DOCSIS_PID & 0xFF,
                   0x10 | self.cc])
        self.cc = (self.cc + 1) & 0xF
        return h

    def pack(self, frames):
        """Pack MAC frames back to back starting at a packet boundary (pointer_field=0).
        Returns a list of 188-byte packets; the last one is stuffed with 0xFF."""
        starts = []
        data = b""
        for f in frames:
            starts.append(len(data))
            data += f
        out = []
        pos = 0
        while pos < len(data):
            nxt = next((s - pos for s in starts if s >= pos), None)
            if nxt is not None and nxt < 183:
                # a frame starts here: PUSI + pointer_field, 183 bytes of data
                pkt = self._hdr(True) + bytes([nxt]) + data[pos:pos + 183]
                pos += 183
            elif nxt == 183:
                # start would land on the last byte: stuff it, frame starts next packet
                pkt = self._hdr(False) + data[pos:pos + 183] + b"\xff"
                pos += 183
            else:
                pkt = self._hdr(False) + data[pos:pos + 184]
                pos += 184
            out.append(np.frombuffer(pkt.ljust(188, b"\xff"), dtype=np.uint8))
        return out


# ---------------------------------------------------------------- ranging (CMTS side)
RNG_CONTINUE, RNG_ABORT, RNG_SUCCESS = 1, 2, 3


def rng_rsp_msg(sa, da, sid, us_chan_id, timing=0, power_qdb=0, freq_hz=0, status=RNG_CONTINUE, t4_mult=None,
                drw_qdb=None):
    """RNG-RSP (MULPIv3.0 6.4.6): timing in 1/10.24 MHz units (positive = the CM transmits
    earlier), power in 1/4 dB, frequency in Hz; t4_mult: T4 timeout multiplier (TLV 13);
    drw_qdb: Dynamic Range Window upper edge, 1/4 dB below Phi (TLV 14)."""
    body = struct.pack(">HB", sid & 0x3FFF, us_chan_id)
    body += tlv(1, struct.pack(">i", int(timing)))
    body += tlv(2, struct.pack(">b", int(power_qdb)))
    body += tlv(3, struct.pack(">h", int(freq_hz)))
    body += tlv(5, bytes([status]))
    if t4_mult:
        body += tlv(13, bytes([t4_mult]))
    if drw_qdb is not None:
        body += tlv(14, bytes([max(0, min(255, int(drw_qdb)))]))
    return mgmt_frame(RNG_RSP, body, sa, da=da)


# ---------------------------------------------------------------- upstream frames (CM -> CMTS)
FC_REQ = 0xC4          # bandwidth request: MAC_PARM = mini-slots, LEN field = SID
FC_QREQ = 0xC8         # queue-depth based request (Multiple Transmit Channel mode): 2-byte MAC_PARM
#                        = bytes / N, then the SID, 7 bytes in all (MULPIv3.0 6.2.4.5)
FC_CONCAT = 0xF8       # concatenation header (FC_TYPE 11, FC_PARM 11100): MAC_PARM = frame count, LEN = bytes


def parse_us_frame(buf):
    """First MAC frame of an upstream burst -> dict, or None if the header check (HCS) fails.
    Returns the total frame length so the caller knows how many bytes to decode."""
    if len(buf) < 6:
        return None
    if buf[0] == FC_QREQ:
        if len(buf) < 7 or crc16_ccitt(buf[:5]) != struct.unpack("<H", buf[5:7])[0]:
            return None
        return dict(kind="REQ", total=7, sid=struct.unpack(">H", buf[3:5])[0] & 0x3FFF, minislots=0,
                    qbytes=struct.unpack(">H", buf[1:3])[0])
    fc, parm = buf[0], buf[1]
    ehdr = parm if (fc & 1) and fc != FC_REQ else 0
    hlen = 6 + ehdr
    if len(buf) < hlen or crc16_ccitt(buf[:hlen - 2]) != struct.unpack("<H", buf[hlen - 2:hlen])[0]:
        return None
    if fc == FC_REQ:
        return dict(kind="REQ", total=6, sid=struct.unpack(">H", buf[2:4])[0] & 0x3FFF, minislots=parm)
    total = 6 + struct.unpack(">H", buf[2:4])[0]
    out = dict(kind="DATA" if fc >> 6 == 0 else "MAC", fc=fc, parm=parm, total=total, hlen=hlen,
               ehdr=bytes(buf[4:4 + ehdr]) if ehdr else b"")
    if fc in (0xC0, 0xC2, 0xC3):
        out["kind"] = "MGMT"
    if out["kind"] != "MGMT" or len(buf) < total:
        return out
    m = buf[hlen:total]
    if len(m) < 24 or zlib.crc32(m[:-4]) != struct.unpack("<I", m[-4:])[0]:
        out["crc_ok"] = False
        return out
    out.update(crc_ok=True, da=bytes(m[0:6]), sa=bytes(m[6:12]), ssap=m[15], version=m[17], mtype=m[18],
               body=bytes(m[20:-4]))
    if out["mtype"] in (REG_REQ, REG_ACK, REG_REQ_MP) and len(out["body"]) >= 2:
        out["sid"] = struct.unpack(">H", out["body"][0:2])[0] & 0x3FFF
    if out["mtype"] in (RNG_REQ, INIT_RNG_REQ, B_INIT_RNG_REQ) and len(out["body"]) >= 3:
        b = out["body"]
        out["sid"] = struct.unpack(">H", b[0:2])[0] & 0x3FFF
        out["ds_chan"] = b[2]
    return out


# ---------------------------------------------------------------- Multiple Transmit Channel mode
SEG_HDR_LEN = 8
IUC_SEG = 0x40         # flag on the IUC of a grant given to the upstream receiver: segment header ON
STUFF = 0xFF           # PHY stuff byte: a segment's padding after its last MAC byte


def parse_seg_header(buf):
    """Segment header (MULPIv3.0 6.3) -> dict(pfi, ptr, seq, sc, req), or None if its HCS fails."""
    if len(buf) < SEG_HDR_LEN or crc16_ccitt(buf[:6]) != struct.unpack("<H", buf[6:8])[0]:
        return None
    w = struct.unpack(">H", buf[0:2])[0]
    sq = struct.unpack(">H", buf[2:4])[0]
    return dict(pfi=w >> 15, ptr=w & 0x3FFF, seq=sq >> 3, sc=sq & 7, req=struct.unpack(">H", buf[4:6])[0])


def seg_header(seq, ptr=None, sc=0, req=0):
    """Segment header bytes (simulated modems, tests); ptr None: no MAC frame starts here."""
    w = (0x8000 | (ptr & 0x3FFF)) if ptr is not None else 0
    h = struct.pack(">HHH", w, ((seq & 0x1FFF) << 3) | (sc & 7), req)
    return h + struct.pack("<H", crc16_ccitt(h))


def qreq_frame(sid, nunits):
    """Queue-depth based request frame (tests, simulated modems)."""
    hdr = struct.pack(">BHH", FC_QREQ, nunits, sid & 0x3FFF)
    return hdr + struct.pack("<H", crc16_ccitt(hdr))


class Reassembler:
    """Continuous Concatenation and Fragmentation (MULPIv3.0 7.2.4): the segments of one upstream
    service flow, from any of its channels, back into MAC frames.

    Segments are put back in sequence-number order (13 bits). A missing number is given up as
    soon as a higher one has arrived on every channel the flow uses (each channel's segments
    arrive in order: a channel's grants are processed in time order), or after `timeout_s`.
    Inside the ordered byte stream, MAC frames follow each other across segment boundaries; a
    stuff byte (0xFF) where a frame header should start is the padding of a segment's end. After
    a loss, parsing resumes at the first MAC header a later segment points to (PFI/pointer)."""

    def __init__(self, channels=None, timeout_s=1.0):
        """channels: the upstream channel ids the flow uses (None: those seen so far).
        timeout_s: a hole waits at most this long. Each channel has its own upstream process,
        and one can deliver 100+ ms behind the other: a 0.1 s timeout cut a REG-ACK whose second
        segment came from the late channel (2026-10-10, the modem then never got online here)."""
        self.channels = set(channels or ())
        self.timeout = timeout_s
        self.next = 0                # next sequence number expected (a flow's first segment is 0)
        self.held = {}               # seq -> (payload, pfi, ptr, arrival time)
        self.last = {}               # channel -> last sequence number seen there
        self.buf = bytearray()       # bytes of the frame being reassembled
        self.sync = False            # buf starts on a MAC header
        self.stats = dict(segments=0, frames=0, lost=0, bad_hcs=0, dup=0)

    @staticmethod
    def _after(a, b):
        """a comes after b (mod 8192)."""
        return 0 < (a - b) % 8192 < 4096

    def add(self, ch, hdr, payload, now):
        """One segment (parse_seg_header() dict, its payload) received on channel `ch` ->
        list of complete MAC frames (bytes)."""
        seq = hdr["seq"]
        self.stats["segments"] += 1
        if seq != self.next and not self._after(seq, self.next) or seq in self.held:
            self.stats["dup"] += 1               # already passed (or a repeat)
            return []
        self.held[seq] = (bytes(payload), hdr["pfi"], hdr["ptr"], now)
        self.last[ch] = seq
        out = []
        while True:
            while self.next in self.held:
                out += self._feed(*self.held.pop(self.next)[:3])
                self.next = (self.next + 1) % 8192
            if not self.held:
                break
            # a hole: lost once every channel has gone past it, or too old
            chans = self.channels | set(self.last)
            past = all(c in self.last and self._after(self.last[c], self.next) for c in chans)
            oldest = min(t for *_, t in self.held.values())
            if not past and now - oldest < self.timeout:
                break
            nxt = min(self.held, key=lambda s: (s - self.next) % 8192)
            self.stats["lost"] += (nxt - self.next) % 8192
            self.next = nxt
            self.buf.clear()
            self.sync = False
        return out

    def _feed(self, payload, pfi, ptr):
        if not self.sync:
            if not pfi or ptr > len(payload):
                return []                        # no MAC header starts in this segment
            self.buf = bytearray(payload[ptr:])
            self.sync = True
        else:
            self.buf += payload
        frames = []
        while self.buf:
            if self.buf[0] == STUFF:             # padding: the rest of this segment
                self.buf.clear()
                break
            if len(self.buf) < 6:
                break
            hlen = 6 + (self.buf[1] if self.buf[0] & 1 else 0)
            if len(self.buf) < hlen:
                break                            # header not complete yet
            info = parse_us_frame(bytes(self.buf[:hlen]))
            if info is None:                     # out of step: wait for the next pointer
                self.stats["bad_hcs"] += 1
                self.buf.clear()
                self.sync = False
                break
            if len(self.buf) < info["total"]:
                break
            frames.append(bytes(self.buf[:info["total"]]))
            del self.buf[:info["total"]]
        self.stats["frames"] += len(frames)
        return frames


def rng_req_msg(sa, da, sid, ds_chan_id, pending=0):
    """RNG-REQ as a CM sends it (timing MAC header). Used by the modem simulator and tests."""
    return mgmt_frame(RNG_REQ, struct.pack(">HBB", sid, ds_chan_id, pending), sa, da=da, fc=0xC0)


def req_frame(sid, minislots):
    hdr = struct.pack(">BBH", FC_REQ, minislots, sid & 0x3FFF)
    return hdr + struct.pack("<H", crc16_ccitt(hdr))


# ---------------------------------------------------------------- data and registration
def data_pdu(eth):
    """Ethernet frame without FCS -> DOCSIS Packet PDU (FC 0x00, the FCS appended)."""
    return mac_frame(0x00, eth + struct.pack("<I", zlib.crc32(eth)))


def concat_frame(frames):
    """Several MAC frames in one burst behind a concatenation header (tests, simulated modems)."""
    return mac_frame(FC_CONCAT, b"".join(frames), mac_parm=len(frames))


def split_concat(frame, info):
    """Concatenated burst -> [(sub-frame bytes, its parse_us_frame info)]; stops at the first
    sub-frame whose header check fails."""
    out, off = [], info["hlen"]
    while off < info["total"]:
        sub = parse_us_frame(frame[off:info["total"]])
        if not sub or sub["total"] <= 0 or off + sub["total"] > info["total"]:
            break
        out.append((bytes(frame[off:off + sub["total"]]), sub))
        off += sub["total"]
    return out


def pdu_ethernet(frame, info):
    """Upstream Packet PDU -> (Ethernet frame without FCS, FCS ok)."""
    eth = bytes(frame[info["hlen"]:info["total"]])
    if len(eth) < 18:
        return eth, False
    return eth[:-4], zlib.crc32(eth[:-4]) == struct.unpack("<I", eth[-4:])[0]


def ehdr_elements(ehdr):
    """Extended header -> [(type, value)] (type and length in one byte each, 4 bits)."""
    out, i = [], 0
    while i < len(ehdr):
        t, n = ehdr[i] >> 4, ehdr[i] & 15
        out.append((t, bytes(ehdr[i + 1:i + 1 + n])))
        i += 1 + n
    return out


def piggyback_request(info):
    """Request EH element (type 1: mini-slots, SID) carried by a data frame, or None."""
    for t, v in ehdr_elements(info.get("ehdr", b"")):
        if t == 1 and len(v) == 3:
            return struct.unpack(">H", v[1:3])[0] & 0x3FFF, v[0]
    return None


def reg_rsp_msg(sa, da, sid, response, tlvs):
    """REG-RSP (MULPIv3.0 6.4.8): SID, response (0 = okay), TLVs."""
    return mgmt_frame(REG_RSP, struct.pack(">HB", sid & 0x3FFF, response) + tlvs, sa, da=da)



# ---------------------------------------------------------------- DOCSIS 3.0: MAC Domain Descriptor
def mdd_msg(sa, ccc, current_dcid, ds_channels, us_channel_ids, ip_mode=0, eae=False, verbose_rcp=True,
            ambiguity=None, power_report=False):
    """MDD (MULPIv3.0 6.4.28), MAC management version 4, one fragment.
    ds_channels: [(channel id, frequency Hz, qam 64|256, primary capable)].
    ambiguity: upstream channel ids a modem may range on first (TLV 8); power_report: TLV 12,
    upstream transmit power reporting on (required before giving a CM several upstream channels:
    MULPIv3.0 6.4.28.1.12).
    TLV 1 is laid out as in the MDD of the SFR network (Cadant CMTS, capture of 2026-10-06)."""
    body = struct.pack(">BBBB", ccc & 0xFF, 1, 1, current_dcid)
    for cid, freq, qam, primary in ds_channels:
        sub = tlv(1, bytes([cid])) + tlv(2, struct.pack(">I", int(freq)))
        sub += tlv(3, bytes([(0 << 4) | (1 if qam == 256 else 0)]))      # annex A, modulation order
        sub += tlv(4, bytes([1 if primary else 0])) + tlv(5, struct.pack(">H", 0x0036))
        body += tlv(1, sub)
    body += tlv(4, tlv(1, bytes([1])) + tlv(2, bytes([1 if verbose_rcp else 0])))   # RCP: 8 MHz spacing,
    #                                                       verbose: the modem details its receive modules/channels
    body += tlv(5, tlv(1, bytes([ip_mode])))                             # IP provisioning: 0 = IPv4 only
    body += tlv(6, bytes([1 if eae else 0]))                             # early authentication: off
    for uid in us_channel_ids:
        body += tlv(7, tlv(1, bytes([uid])) + tlv(2, struct.pack(">H", 0)))
    if ambiguity:
        body += tlv(8, bytes(ambiguity))                                 # upstream ambiguity resolution list
    body += tlv(9, bytes([0]))                                           # upstream frequency range: standard
    body += tlv(10, bytes([1]))                                          # symbol clocks locked to the master clock
    if power_report:
        body += tlv(12, bytes([1]))                                      # upstream transmit power reporting
    return mgmt_frame(MDD, body, sa, version=4)


def reg_rsp_mp_msg(sa, da, sid, response, tlvs):
    """REG-RSP-MP (DOCSIS 3.0, MAC management version 4): SID, response, number of fragments,
    fragment sequence number, TLVs. One fragment; see reg_rsp_mp_msgs for longer ones."""
    body = struct.pack(">HBBB", sid & 0x3FFF, response, 1, 1) + tlvs
    return mgmt_frame(REG_RSP_MP, body, sa, da=da, version=4)


def reg_rsp_mp_msgs(sa, da, sid, response, tlvs, max_tlv=1400):
    """REG-RSP-MP cut into fragments of at most `max_tlv` TLV bytes, at TLV boundaries (with 4
    upstream channels the three TCC UCDs alone are ~1 KB). Returns the list of frames."""
    parts, cur, i = [], b"", 0
    while i < len(tlvs):
        n = 2 + tlvs[i + 1]
        if cur and len(cur) + n > max_tlv:
            parts.append(cur)
            cur = b""
        cur += tlvs[i:i + n]
        i += n
    parts.append(cur)
    return [mgmt_frame(REG_RSP_MP, struct.pack(">HBBB", sid & 0x3FFF, response, len(parts), k + 1) + p,
                       sa, da=da, version=4) for k, p in enumerate(parts)]


def data_pdu_dsid(eth, dsid, seq, prio=0, scc=0):
    """Downstream Packet PDU of a bonded flow: DS-EHDR (EH type 8, 5 bytes: traffic priority,
    sequence change count, 20-bit DSID, 16-bit packet sequence number) for the modem to put the
    frames of the channels back in order. The HCS covers the extended header too."""
    eh = bytes([0x85, ((prio & 7) << 5) | ((scc & 1) << 4) | ((dsid >> 16) & 0xF), (dsid >> 8) & 0xFF,
                dsid & 0xFF, (seq >> 8) & 0xFF, seq & 0xFF])
    payload = eth + struct.pack("<I", zlib.crc32(eth))
    hdr = struct.pack(">BBH", 0x01, len(eh), len(eh) + len(payload)) + eh
    return hdr + struct.pack("<H", crc16_ccitt(hdr)) + payload
