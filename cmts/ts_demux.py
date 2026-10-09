"""MPEG-TS -> DOCSIS MAC frames (PID 0x1FFE), and DLT_DOCSIS pcap writer."""
import struct

from .docsis_mac import DOCSIS_PID


def mac_frame_len(buf):
    """Total length of the MAC frame at buf[0], or None if not enough bytes / stuffing."""
    if not buf or buf[0] == 0xFF:
        return 0 if buf else None
    if len(buf) < 4:
        return None
    ln = struct.unpack(">H", buf[2:4])[0]
    return 6 + ln  # LEN covers EHDR + payload


class DocsisDemux:
    def __init__(self):
        self.buf = b""
        self.synced = False
        self.frames = []
        self.cc = None
        self.cc_errors = 0

    def push_packet(self, pkt):
        pkt = bytes(pkt)
        if pkt[0] != 0x47:
            return
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid != DOCSIS_PID:
            return
        cc = pkt[3] & 0xF
        if self.cc is not None and cc != (self.cc + 1) & 0xF:
            self.cc_errors += 1
            self.buf, self.synced = b"", False
        self.cc = cc
        pusi = pkt[1] & 0x40
        payload = pkt[4:]
        if pusi:
            ptr = payload[0]
            if self.synced:
                self.buf += payload[1:1 + ptr]
                self._drain(final=True)
            self.buf = payload[1 + ptr:]
            self.synced = True
        elif self.synced:
            self.buf += payload
        self._drain()

    def _drain(self, final=False):
        while self.buf:
            if self.buf[0] == 0xFF:  # stuffing until the next PUSI
                self.buf = b""
                return
            n = mac_frame_len(self.buf)
            if n is None or n > len(self.buf):
                if final:
                    self.buf = b""
                return
            self.frames.append(self.buf[:n])
            self.buf = self.buf[n:]


def write_pcap(path, frames):
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 143))
        for i, fr in enumerate(frames):
            f.write(struct.pack("<IIII", i // 1000, (i % 1000) * 1000, len(fr), len(fr)) + fr)
