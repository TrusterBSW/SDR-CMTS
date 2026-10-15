"""provision.py through the Linux TAP, without radio: this script plays the modem on cmts0
(as mini_cmts.py would hand its frames over) and runs DHCP DISCOVER/OFFER/REQUEST/ACK, ARP,
TFTP of the config file (its MICs checked) and Time of Day.

Needs sudo (creates cmts0 if missing, runs provision.py as root)."""
import os, select, struct, subprocess, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from cmts import cmcfg, netpkt
from cmts.bridge import TapBridge, ensure_tap

CM = bytes.fromhex("247f20a838b8")
IFACE = "cmts0"
ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""), flush=True)


def csum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f">{len(b) // 2}H", b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def udp_frame(src_ip, dst_ip, sport, dport, payload, dst_mac):
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, src_ip, dst_ip)
    hdr = hdr[:10] + struct.pack(">H", csum(hdr)) + hdr[12:]
    return dst_mac + CM + b"\x08\x00" + hdr + udp


def dhcp(mtype, xid, extra=b""):
    p = struct.pack(">BBBBIHH", 1, 1, 6, 0, xid, 0, 0x8000) + bytes(16) + CM.ljust(16, b"\0")
    p += bytes(192) + b"\x63\x82\x53\x63" + bytes([53, 1, mtype, 60, 9]) + b"docsis3.0" + extra + b"\xff"
    return udp_frame(bytes(4), b"\xff" * 4, 68, 67, p.ljust(300, b"\0"), b"\xff" * 6)


def recv(br, want, timeout=3.0, my_ip=None):
    """Frames from the TAP until want(eth) is true; answers ARP requests for my_ip on the way."""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([br.fd], [], [], max(0, end - time.time()))
        if not r:
            break
        eth = os.read(br.fd, 2048)
        if my_ip and eth[12:14] == b"\x08\x06" and eth[20:22] == b"\x00\x01" and eth[38:42] == my_ip:
            arp = struct.pack(">HHBBH", 1, 0x0800, 6, 4, 2) + CM + my_ip + eth[22:28] + eth[28:32]
            os.write(br.fd, eth[6:12] + CM + b"\x08\x06" + arp)
            print(f"    (ARP : {netpkt.ip(my_ip)} est à {netpkt.fmt_mac(CM)})")
            continue
        if want(eth):
            return eth
    return None


def udp_payload(eth):
    ihl = (eth[14] & 15) * 4
    return eth[14 + ihl + 8:]


def is_udp(eth, sport=None, dport=None):
    if eth[12:14] != b"\x08\x00" or eth[23] != 17:
        return False
    ihl = (eth[14] & 15) * 4
    sp, dp = struct.unpack(">HH", eth[14 + ihl:14 + ihl + 4])
    return (sport is None or sp == sport) and (dport is None or dp == dport)


existed = os.path.exists(f"/sys/class/net/{IFACE}")
if not ensure_tap(IFACE, print):
    sys.exit("cmts0 impossible à créer")
br = TapBridge(IFACE, print)                    # carrier up before the servers start
prov = subprocess.Popen(["sudo", sys.executable, os.path.join(os.path.dirname(__file__), "..", "provision.py"),
                         "--iface", IFACE], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    time.sleep(1.5)
    os.write(br.fd, dhcp(1, 0xCAFE0001))
    offer = recv(br, lambda e: is_udp(e, 67, 68) and (netpkt.parse_dhcp(udp_payload(e)) or {}).get("type") == 2)
    m = netpkt.parse_dhcp(udp_payload(offer)) if offer else None
    check("DHCP OFFER", m, netpkt.summary(offer) if offer else "pas de réponse")
    if m:
        server = m["opts"][54]
        yi = bytes(int(x) for x in m["yiaddr"].split("."))
        os.write(br.fd, dhcp(3, 0xCAFE0001, bytes([50, 4]) + yi + bytes([54, 4]) + server))
        ack = recv(br, lambda e: is_udp(e, 67, 68) and (netpkt.parse_dhcp(udp_payload(e)) or {}).get("type") == 5)
        a = netpkt.parse_dhcp(udp_payload(ack)) if ack else None
        check("DHCP ACK avec options DOCSIS", a and all(k in a["opts"] for k in (1, 2, 3, 4, 51, 54, 67))
              and a["file"] == "cm.cfg", netpkt.summary(ack) if ack else "pas de réponse")
        smac = offer[6:12]
        # ToD
        os.write(br.fd, udp_frame(yi, server, 1037, 37, b"", smac))
        tod = recv(br, lambda e: is_udp(e, 37, 1037), my_ip=yi)
        if tod:
            t = struct.unpack(">I", udp_payload(tod)[:4])[0] - 2208988800
        check("ToD (RFC 868)", tod and abs(t - time.time()) < 5, time.strftime("%H:%M:%S", time.gmtime(t)) + " UTC" if tod else "pas de réponse")
        # TFTP
        os.write(br.fd, udp_frame(yi, server, 1069, 69, b"\0\x01cm.cfg\0octet\0", smac))
        data, blocks = b"", 0
        while True:
            d = recv(br, lambda e: is_udp(e, None, 1069) and udp_payload(e)[:2] == b"\0\x03", my_ip=yi)
            if not d:
                break
            p = udp_payload(d)
            blocks += 1
            data += p[4:]
            ihl = (d[14] & 15) * 4
            sport = struct.unpack(">H", d[14 + ihl:14 + ihl + 2])[0]
            os.write(br.fd, udp_frame(yi, server, 1069, sport, b"\0\x04" + p[2:4], smac))
            if len(p) - 4 < 512:
                break
        tl, problems = cmcfg.check(data, "docsis-lab") if data else ([], ["rien reçu"])
        check("TFTP cm.cfg, MIC corrects", data and not problems, f"{len(data)} octets en {blocks} bloc(s)"
              + (f", {problems}" if problems else ""))
finally:
    subprocess.run(["sudo", "pkill", "-f", "provision.py --iface " + IFACE], stderr=subprocess.DEVNULL)
    out = prov.communicate(timeout=5)[0]
    print("--- provision.py :")
    print("\n".join("    " + l for l in out.splitlines() if l.startswith("[")))
if not existed:
    # (a cmts0 left with 10.10.0.1 on this PC took the traffic meant for the CMTS behind the modem
    # once, 2026-10-15: every iperf3 / ping to 10.10.0.1 answered locally)
    subprocess.run(["sudo", "-n", "ip", "link", "del", IFACE], capture_output=True)
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
