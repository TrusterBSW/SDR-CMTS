"""Write SYNC/UCD/MAP frames to a DLT_DOCSIS pcap for dissection with tshark."""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cmts import docsis_mac as d

sa = bytes.fromhex("020000cafe01")
frames = [
    d.sync_msg(sa, 0x12345678),
    d.ucd_msg(sa, 1, 1, 4, 1, 40_000_000, 8),
    d.map_msg(sa, 1, 1, 1000, 990, [(d.SID_BROADCAST, d.IUC_INIT_MAINT, 0),
                                   (d.SID_BROADCAST, d.IUC_REQUEST, 64)], 80),
]
out = sys.argv[1]
with open(out, "wb") as f:
    f.write(struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 143))
    for i, fr in enumerate(frames):
        f.write(struct.pack("<IIII", i, 0, len(fr), len(fr)) + fr)
print("wrote", len(frames), "frames")
