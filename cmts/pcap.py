"""pcap writer for DOCSIS MAC frames (link type 143, DLT_DOCSIS: Wireshark's "docsis" dissector)."""
import collections
import os
import struct
import threading

DLT_DOCSIS = 143


class PcapWriter:
    """Thread-safe and non-blocking: write() only queues the record; a writer thread of its own
    does the file I/O, flushed every 0.5 s (Wireshark can open the file while it grows) and synced
    every second. Writing from the callers' threads stalled the transmitter (2026-10-09): under a
    30 Mbit/s downstream the pcap grows ~4 MB/s, the kernel wrote it back ~120 MB at a time every
    30 s (vm.dirty_expire_centisecs), and the write() calls of the downstream encoder thread
    blocked behind it for 50-190 ms. Records beyond `max_queue` waiting are dropped (counted)."""

    def __init__(self, path, max_queue=200000):
        self.path = path
        self.f = open(path, "wb")
        self.f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, DLT_DOCSIS))
        self.f.flush()
        self.q = collections.deque()
        self.max_queue = max_queue
        self.count = 0
        self.dropped = 0
        self.ev = threading.Event()
        self.closing = False
        self.th = threading.Thread(target=self._run, name="pcap", daemon=True)
        self.th.start()

    def write(self, t, frame):
        if self.closing:
            return
        if len(self.q) >= self.max_queue:
            self.dropped += 1
            return
        self.q.append((t, bytes(frame)))          # deque.append is atomic: no lock needed
        self.count += 1

    def _drain(self):
        out = []
        while self.q:
            t, frame = self.q.popleft()
            sec = int(t)
            usec = min(999999, int(round((t - sec) * 1e6)))
            out.append(struct.pack("<IIII", sec, usec, len(frame), len(frame)) + frame)
        if out:
            self.f.write(b"".join(out))

    def _run(self):
        n = 0
        while not self.closing:
            self.ev.wait(0.5)
            self._drain()
            self.f.flush()
            n += 1
            if n % 2 == 0:
                os.fdatasync(self.f.fileno())    # small regular writebacks, never 100+ MB at once

    def close(self):
        self.closing = True
        self.ev.set()
        self.th.join(timeout=5)
        self._drain()
        self.f.close()
