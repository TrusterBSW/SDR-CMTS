#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Decoded upstream bursts saved by mini_cmts.py --save-bursts (burst_*.npz) -> DOCSIS pcap.

  python3 tools/bursts2pcap.py captures/bursts -o captures/bursts.pcap
  wireshark captures/bursts.pcap

Each record is stamped with the file's modification time (written right after the burst was
decoded, so the order and the spacing are right to a few ms). raw_*.npz files hold only raw IQ
of energy found outside the decoding windows: they are skipped. Bursts that could not be decoded
have no frame and are counted, not written.
"""
import argparse
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cmts.pcap import PcapWriter  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="+", help="burst_*.npz files or directories holding them")
    ap.add_argument("-o", "--out", required=True, help="pcap to write")
    a = ap.parse_args()
    files = []
    for s in a.src:
        files += sorted(glob.glob(os.path.join(s, "burst_*.npz"))) if os.path.isdir(s) else [s]
    files.sort(key=os.path.getmtime)
    pcap = PcapWriter(a.out)
    empty = 0
    for f in files:
        frame = bytes(np.load(f)["frame"])
        if not frame:
            empty += 1
            continue
        pcap.write(os.path.getmtime(f), frame)
    pcap.close()
    print(f"{pcap.count} trames écrites dans {a.out}" + (f", {empty} rafales non décodées ignorées" if empty else ""))


if __name__ == "__main__":
    main()
