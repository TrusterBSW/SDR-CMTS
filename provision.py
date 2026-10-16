#!/usr/bin/env python3
"""Provisioning servers for the lab cable modem: DHCP, TFTP (DOCSIS config file) and Time of Day,
on the CMTS bridge interface (cmts0). They replace, for a lab, what a DOCSIS head end runs on
separate machines (a DHCP server, a TFTP server and an RFC 868 time server):

    modem --DOCSIS--> mini_cmts.py (bridge) --> cmts0 (10.10.0.1) <-- provision.py

    sudo python3 provision.py                       # on cmts0, generated config file
    sudo python3 provision.py --config my.cfg       # serve your own config file
    python3 provision.py --make-config cm.cfg       # only write the generated file (no root)
    python3 provision.py --show-config my.cfg       # print a config file and check its MICs

mini_cmts.py --all-in-one starts it (through sudo) with the same shared secret. To use other
servers instead (dnsmasq, ISC DHCP, tftpd...), run mini_cmts.py without --all-in-one and give
cmts0 an address yourself, or bridge it to a real network.

Needs root: ports 67/69/37 and SO_BINDTODEVICE. Serves cmts0 only; never on an operator's network.
"""
import argparse
import ipaddress
import os
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cmts import cmcfg, netpkt  # noqa: E402

RFC868_EPOCH = 2208988800       # seconds from 1900-01-01 to 1970-01-01


def log(msg):
    print(msg, flush=True)


def bound_socket(iface, port, ip="0.0.0.0", broadcast=False):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if broadcast:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
    s.bind((ip, port))
    return s


# ---------------------------------------------------------------- DHCP
class Dhcp:
    def __init__(self, a, net):
        self.a, self.net = a, net
        self.server = ipaddress.IPv4Address(a.server_ip.split("/")[0])
        lo, hi = (ipaddress.IPv4Address(x) for x in a.pool.split("-"))
        self.free = [ipaddress.IPv4Address(i) for i in range(int(lo), int(hi) + 1)]
        self.leases = {}        # chaddr -> IPv4Address
        self.sock = bound_socket(a.iface, 67, broadcast=True)

    def address_for(self, chaddr, wanted=None):
        if chaddr in self.leases:
            return self.leases[chaddr]
        ip = wanted if wanted in self.free else (self.free[0] if self.free else None)
        if ip is not None:
            self.free.remove(ip)
            self.leases[chaddr] = ip
        return ip

    def reply(self, req, mtype, yiaddr, docsis):
        tz = -time.altzone if time.localtime().tm_isdst > 0 else -time.timezone
        opts = [(53, bytes([mtype])), (54, self.server.packed), (51, struct.pack(">I", self.a.lease)),
                (1, self.net.netmask.packed), (3, self.server.packed), (2, struct.pack(">i", tz)),
                (4, self.server.packed)]
        fname = b""
        if docsis:
            fname = self.a.config_name.encode()
            opts += [(66, str(self.server).encode()), (67, fname), (7, self.server.packed)]
        elif self.a.dns:                       # a device behind a modem: name servers (--dns)
            opts.append((6, b"".join(ipaddress.IPv4Address(d).packed for d in self.a.dns.split(","))))
        p = struct.pack(">BBBBIHH", 2, 1, 6, 0, req["xid"], 0, req["flags"])
        p += bytes(4) + (yiaddr.packed if yiaddr else bytes(4)) + self.server.packed
        p += ipaddress.IPv4Address(req["giaddr"]).packed + req["chaddr"].ljust(16, b"\0")
        p += bytes(64) + fname.ljust(128, b"\0") + b"\x63\x82\x53\x63"
        p += b"".join(bytes([c, len(v)]) + v for c, v in opts) + b"\xff"
        return p.ljust(300, b"\0")

    def run(self):
        while True:
            data, (src, sport) = self.sock.recvfrom(4096)
            m = netpkt.parse_dhcp(data)
            if not m or m["op"] != 1:
                continue
            cls = m["opts"].get(60, b"").decode(errors="replace")
            docsis = cls.lower().startswith(("docsis", "pktc")) or not self.a.cm_class_only
            who = f"{netpkt.fmt_mac(m['chaddr'])}" + (f" [{cls}]" if cls else "")
            t = m["type"]
            if t == 1:                                      # DISCOVER -> OFFER
                ip = self.address_for(m["chaddr"])
                if ip is None:
                    log(f"[DHCP] !! DISCOVER de {who} : plus d'adresse libre")
                    continue
                out, kind = self.reply(m, 2, ip, docsis), "OFFER"
            elif t == 3:                                    # REQUEST -> ACK / NAK
                want = m["opts"].get(50)
                want = ipaddress.IPv4Address(want) if want else ipaddress.IPv4Address(m["ciaddr"])
                sid = m["opts"].get(54)
                if sid and sid != self.server.packed:
                    continue                                # another server was chosen
                ip = self.address_for(m["chaddr"], want)
                if ip != want:
                    out, kind, ip = self.reply(m, 6, None, False), "NAK", None
                else:
                    out, kind = self.reply(m, 5, ip, docsis), "ACK"
            elif t in (4, 7):                               # DECLINE / RELEASE
                ip = self.leases.pop(m["chaddr"], None)
                if ip and t == 7:
                    self.free.append(ip)
                log(f"[DHCP] {netpkt.DHCP_TYPES[t]} de {who}")
                continue
            else:
                continue
            dest = (m["giaddr"], 67) if m["giaddr"] != "0.0.0.0" else ("255.255.255.255", 68)
            self.sock.sendto(out, dest)
            log(f"[DHCP] {netpkt.DHCP_TYPES[t]} de {who} → {kind}" + (f" {ip}" if ip else "")
                + (f", fichier « {self.a.config_name} » sur {self.server}" if docsis and ip else ""))


# ---------------------------------------------------------------- TFTP (RFC 1350, 512-byte blocks)
class Tftp:
    def __init__(self, a, files):
        self.a, self.files = a, files
        self.server = a.server_ip.split("/")[0]
        self.sock = bound_socket(a.iface, 69)

    def content(self, name):
        name = name.lstrip("/")
        if name in self.files:
            return self.files[name]
        if self.a.tftp_root:
            root = os.path.realpath(self.a.tftp_root)
            path = os.path.realpath(os.path.join(root, name))
            if path.startswith(root + os.sep) and os.path.isfile(path):
                with open(path, "rb") as f:
                    return f.read()
        return None

    def run(self):
        while True:
            data, peer = self.sock.recvfrom(1500)
            if len(data) < 4 or data[1] != 1:
                continue
            parts = data[2:].split(b"\0")
            name = parts[0].decode(errors="replace")
            body = self.content(name)
            if body is None:
                log(f"[TFTP] !! RRQ « {name} » de {peer[0]} : fichier inconnu")
                self.sock.sendto(b"\0\x05\0\x01file not found\0", peer)
                continue
            log(f"[TFTP] RRQ « {name} » de {peer[0]} : envoi de {len(body)} octets")
            threading.Thread(target=self.send, args=(peer, name, body), daemon=True).start()

    def send(self, peer, name, body):
        s = bound_socket(self.a.iface, 0, ip=self.server)
        s.settimeout(1.0)
        blocks = [body[i:i + 512] for i in range(0, len(body) + 1, 512)]
        if len(blocks[-1]) == 512:
            blocks.append(b"")
        for n, blk in enumerate(blocks, 1):
            pkt = struct.pack(">HH", 3, n & 0xFFFF) + blk
            for _ in range(5):
                s.sendto(pkt, peer)
                try:
                    while True:
                        ack, src = s.recvfrom(1500)
                        if src == peer and ack[:2] == b"\0\x04" and struct.unpack(">H", ack[2:4])[0] == n & 0xFFFF:
                            break
                    break
                except socket.timeout:
                    continue
            else:
                log(f"[TFTP] !! « {name} » vers {peer[0]} : pas d'ACK du bloc {n}, abandon")
                return
        log(f"[TFTP] « {name} » transféré à {peer[0]} ({len(body)} octets, {len(blocks)} blocs)")


# ---------------------------------------------------------------- Time of Day (RFC 868, UDP)
def tod(a):
    s = bound_socket(a.iface, 37)
    while True:
        _, peer = s.recvfrom(64)
        now = int(time.time())
        s.sendto(struct.pack(">I", (now + RFC868_EPOCH) & 0xFFFFFFFF), peer)
        log(f"[ToD] heure envoyée à {peer[0]} : {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))} UTC")


# ---------------------------------------------------------------- main
def config_bytes(a):
    if a.config:
        with open(a.config, "rb") as f:
            return f.read()
    extra = [cmcfg.snmp_community(a.snmp_community)] if a.snmp_community else []
    return cmcfg.build(a.secret, ds_rate=a.ds_rate, us_rate=a.us_rate, max_cpe=a.max_cpe,
                       network_access=not a.no_network_access, extra=extra, us_burst=a.us_burst,
                       us_buffer=a.us_buffer or None)


def show(buf, secret):
    tlvs, problems = cmcfg.check(buf, secret)
    for line in cmcfg.describe(tlvs):
        log(line)
    log("   MIC : " + ("; ".join(problems) if problems else f"CM MIC et CMTS MIC corrects (secret « {secret} »)"))


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iface", default="cmts0", help="interface served (the CMTS bridge)")
    ap.add_argument("--server-ip", default="10.10.0.1/24", help="address given to the interface (gateway, "
                    "DHCP, TFTP and time server)")
    ap.add_argument("--no-ip", action="store_true", help="leave the interface address alone")
    ap.add_argument("--pool", default="10.10.0.10-10.10.0.99", help="DHCP addresses")
    ap.add_argument("--lease", type=int, default=3600, help="DHCP lease time (s)")
    ap.add_argument("--cm-class-only", action="store_true",
                    help="give the config file name only to DHCP clients that announce a docsis class")
    ap.add_argument("--config-name", default="cm.cfg", help="config file name given by DHCP")
    ap.add_argument("--config", help="serve this config file instead of the generated one")
    ap.add_argument("--tftp-root", help="also serve the files of this directory")
    ap.add_argument("--secret", default="docsis-lab", help="CMTS shared secret (CMTS MIC)")
    # 1 Gbit/s: 6 bonded downstream channels carried 258 Mbit/s (2026-10-11), past the former 200
    ap.add_argument("--ds-rate", type=int, default=1_000_000_000, help="generated config: downstream max rate (bit/s)")
    ap.add_argument("--us-rate", type=int, default=1_000_000_000, help="generated config: upstream max rate (bit/s)")
    ap.add_argument("--us-buffer", type=int, default=131072, metavar="BYTES",
                    help="upstream flow buffer in the modem (TLV 24.35 target = maximum): its queue, so the "
                         "delay a TCP upload sees. 128 KB: ping during an upload 237 -> 93 ms on the Sagem 3686, "
                         "throughput unchanged (52 Mbit/s, 2 US); 0 = the modem's own size (2026-10-12)")
    ap.add_argument("--us-burst", type=int, default=64_000,
                    help="generated config: upstream Maximum Traffic Burst (bytes, TLV 24.9; DOCSIS default 3044). "
                         "The modem never requests more than this at once: with a ~60 ms request-to-grant cycle, "
                         "3044 B held the Sagem's upload to ~3.5 Mbit/s (2026-10-09)")
    ap.add_argument("--max-cpe", type=int, default=16,
                    help="generated config: CPE devices allowed behind a modem (a Sagemcom set-top box alone "
                         "uses 5 MAC addresses, 2026-10-14)")
    ap.add_argument("--dns", default="",
                    help="name servers given by DHCP to the devices behind the modems, comma separated (e.g. "
                         "1.1.1.1; with NAT of the cable network on the host for Internet access); default none")
    ap.add_argument("--no-network-access", action="store_true", help="generated config: network access off")
    ap.add_argument("--snmp-community", metavar="NAME",
                    help="generated config: let the modem answer SNMP (read-only) with this community from "
                         "10.10.0.0/24 (TLV 53): DS SNR, codeword errors, US power... via snmpwalk")
    ap.add_argument("--make-config", metavar="FILE", help="write the generated config file and exit")
    ap.add_argument("--show-config", metavar="FILE", help="print a config file, check its MICs and exit")
    return ap


def main():
    a = parser().parse_args()
    if a.show_config:
        with open(a.show_config, "rb") as f:
            show(f.read(), a.secret)
        return 0
    cfg = config_bytes(a)
    if a.make_config:
        with open(a.make_config, "wb") as f:
            f.write(cfg)
        log(f"{a.make_config} : {len(cfg)} octets")
        show(cfg, a.secret)
        return 0
    if os.geteuid() != 0:
        log("!! provision.py doit tourner en root (ports 67, 69, 37) : sudo python3 provision.py ...")
        return 1
    for _ in range(50):                                  # mini_cmts.py may still be creating it
        if os.path.exists(f"/sys/class/net/{a.iface}"):
            break
        time.sleep(0.1)
    else:
        log(f"!! interface {a.iface} absente : lance d'abord mini_cmts.py (il la crée)")
        return 1
    net = ipaddress.IPv4Interface(a.server_ip).network
    if not a.no_ip:
        subprocess.run(["ip", "addr", "replace", a.server_ip, "dev", a.iface], check=True)
        subprocess.run(["ip", "link", "set", a.iface, "up"], check=True)
    log(f"[PROV] {a.iface} {a.server_ip} : DHCP {a.pool}, TFTP « {a.config_name} » ({len(cfg)} octets"
        f"{', ' + a.config if a.config else ', généré'}), ToD")
    show(cfg, a.secret)
    servers = [Dhcp(a, net).run, Tftp(a, {a.config_name: cfg}).run, lambda: tod(a)]
    for f in servers:
        threading.Thread(target=f, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
