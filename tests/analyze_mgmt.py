"""Statistics on a DOCSIS management pcap (from origin_capture.py or from our own
transmitter via tests/test_loopback.py) to compare a real CMTS with ours."""
import collections
import struct
import sys

import numpy as np

IUC = {1: "REQ", 2: "REQ/DATA", 3: "INIT-MAINT", 4: "STATION-MAINT", 5: "SHORT", 6: "LONG",
       7: "NULL", 8: "DATA-ACK", 9: "A-SHORT", 10: "A-LONG", 11: "A-UGS"}


def frames(path):
    with open(path, "rb") as f:
        f.read(24)
        while True:
            h = f.read(16)
            if len(h) < 16:
                return
            _, _, n, _ = struct.unpack("<IIII", h)
            yield f.read(n)


def main(path, minislot_ticks=None):
    last_sync = None
    syncs = []
    maps = collections.defaultdict(list)
    for i, fr in enumerate(frames(path)):
        hl = 6 + (fr[1] if fr[0] & 1 else 0)
        mtype = fr[hl + 6 + 6 + 2 + 4]
        ver = fr[hl + 6 + 6 + 2 + 3]
        body = fr[hl + 20:-4]
        if mtype == 1:
            last_sync = struct.unpack(">I", body[:4])[0]
            syncs.append(last_sync)
        elif mtype == 3:
            ch, ucdc, nie, _, start, ack, rs, re, ds, de = struct.unpack(">BBBBIIBBBB", body[:16])
            ies = [struct.unpack(">I", body[16 + 4 * k:20 + 4 * k])[0] for k in range(min(nie, (len(body) - 16) // 4))]
            ies = [(v >> 18, (v >> 14) & 15, v & 0x3FFF) for v in ies]
            length = next((o for s, iuc, o in ies if iuc == 7), None)
            maps[ch].append(dict(start=start, ack=ack, ies=ies, len=length, sync=last_sync,
                                 bo=(rs, re, ds, de), ver=ver, idx=i))
    print(f"SYNC: {len(syncs)} messages, interval median "
          f"{np.median(np.diff(syncs) % (1 << 32)) / 10240:.2f} ms")
    for ch in sorted(maps):
        m = maps[ch]
        starts = np.array([x["start"] for x in m], dtype=np.int64)
        steps = np.diff(starts)
        lens = [x["len"] for x in m if x["len"] is not None]
        iuc = collections.Counter(IUC.get(i, i) for x in m for (_, i, _) in x["ies"])
        # map lead time: alloc start (minislots -> 10.24 MHz counts) vs latest SYNC timestamp
        leads = []
        if minislot_ticks:
            for x in m:
                if x["sync"] is not None:
                    leads.append(((x["start"] * minislot_ticks * 64 - x["sync"]) % (1 << 32)) / 10240)
        im = [x for x in m if any(i == 3 for (_, i, _) in x["ies"])]
        im_len = []
        for x in im:
            ie = x["ies"]
            for k, (sid, i, off) in enumerate(ie):
                if i == 3 and k + 1 < len(ie):
                    im_len.append(ie[k + 1][2] - off)
        print(f"US {ch}: {len(m)} MAPs, version {set(x['ver'] for x in m)}, "
              f"length median {int(np.median(lens))} minislots, contiguous "
              f"{np.mean([b['start'] - a['start'] == a['len'] for a, b in zip(m, m[1:])]) * 100:.0f}%, "
              f"backoffs {m[0]['bo']}")
        print(f"      IE usage: {dict(iuc.most_common())}")
        if im:
            print(f"      INIT-MAINT in {len(im)} MAPs, every ~{len(m) / max(1, len(im)):.1f} MAPs, "
                  f"length median {int(np.median(im_len)) if im_len else '?'} minislots")
        if leads:
            print(f"      alloc start - last SYNC timestamp: min {min(leads):.2f} ms, median {np.median(leads):.2f} ms")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
