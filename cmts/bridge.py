"""Layer 2 bridge between the cable side (DOCSIS Packet PDUs) and a Linux TAP interface (cmts0).

Upstream: every Ethernet frame a modem sends (DHCP, TFTP, ToD, ARP, CPE traffic...) is written
to the TAP; downstream: every frame Linux sends on the TAP is put in a DOCSIS Packet PDU on the
downstream. Whatever runs on cmts0 (provision.py, dnsmasq, a Linux bridge to a real network...)
thus sees the modems as if they were on an Ethernet segment: a "bridged" CMTS.

The TAP is created once (sudo) and owned by the user, so the CMTS then opens it without root.
"""
import fcntl
import getpass
import os
import shutil
import struct
import subprocess
import threading

TUNSETIFF = 0x400454CA
IFF_TAP, IFF_NO_PI = 0x0002, 0x1000


def ensure_tap(name, log):
    """Create the TAP (owned by the current user, link up, ignored by NetworkManager) if needed."""
    if os.path.exists(f"/sys/class/net/{name}"):
        return True
    user = getpass.getuser()
    cmds = [["ip", "tuntap", "add", "dev", name, "mode", "tap", "user", user],
            ["ip", "link", "set", name, "up"]]
    if shutil.which("nmcli"):
        cmds.append(["nmcli", "device", "set", name, "managed", "no"])
    log(f"[PONT] création de l'interface {name} (sudo) : " + " ; ".join(" ".join(c) for c in cmds))
    for c in cmds:
        r = subprocess.run(["sudo", *c], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode and c[0] != "nmcli":
            log(f"[PONT] !! {' '.join(c)} : {r.stderr.strip()}")
            return False
    return True


class TapBridge:
    def __init__(self, name, log):
        self.name, self.log = name, log
        self.fd = os.open("/dev/net/tun", os.O_RDWR)
        fcntl.ioctl(self.fd, TUNSETIFF, struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI))
        self.stats = dict(up=0, down=0, bad_fcs=0)

    def upstream(self, eth):
        """Ethernet frame (no FCS) from a modem -> Linux."""
        os.write(self.fd, eth)
        self.stats["up"] += 1

    def start(self, downstream, stop):
        """downstream(eth) is called for every frame Linux sends on the TAP."""
        def loop():
            while not stop.is_set():
                try:
                    eth = os.read(self.fd, 2048)
                except OSError:
                    break
                self.stats["down"] += 1
                downstream(eth)
        threading.Thread(target=loop, name="tap", daemon=True).start()

    def link_up(self):
        try:
            with open(f"/sys/class/net/{self.name}/operstate") as f:
                return f.read().strip()
        except OSError:
            return "?"
