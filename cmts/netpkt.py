"""Just enough Ethernet / ARP / IPv4 / UDP / DHCP / TFTP decoding to log, in one line, what goes
through the CMTS bridge (and to build the replies of provision.py)."""
import ipaddress
import struct

DHCP_TYPES = {1: "DISCOVER", 2: "OFFER", 3: "REQUEST", 4: "DECLINE", 5: "ACK", 6: "NAK", 7: "RELEASE",
              8: "INFORM"}
TFTP_OPS = {1: "RRQ", 2: "WRQ", 3: "DATA", 4: "ACK", 5: "ERROR", 6: "OACK"}


def fmt_mac(b):
    return ":".join(f"{x:02x}" for x in b)


def ip(b):
    return str(ipaddress.IPv4Address(bytes(b)))


def dhcp_options(buf):
    """BOOTP options area (after the magic cookie) -> {code: value}."""
    out, i = {}, 0
    while i < len(buf):
        c = buf[i]
        if c == 255:
            break
        if c == 0:
            i += 1
            continue
        n = buf[i + 1]
        out[c] = bytes(buf[i + 2:i + 2 + n])
        i += 2 + n
    return out


def parse_dhcp(udp_payload):
    p = udp_payload
    if len(p) < 240 or p[236:240] != b"\x63\x82\x53\x63":
        return None
    opts = dhcp_options(p[240:])
    return dict(op=p[0], xid=struct.unpack(">I", p[4:8])[0], flags=struct.unpack(">H", p[10:12])[0],
                ciaddr=ip(p[12:16]), yiaddr=ip(p[16:20]), siaddr=ip(p[20:24]), giaddr=ip(p[24:28]),
                chaddr=bytes(p[28:34]), file=p[108:236].split(b"\0")[0].decode(errors="replace"),
                type=opts.get(53, b"\0")[0], opts=opts)


def summary(eth):
    """One line describing an Ethernet frame (no FCS)."""
    if len(eth) < 14:
        return f"trame courte ({len(eth)} octets)"
    dst, src, et = eth[0:6], eth[6:12], struct.unpack(">H", eth[12:14])[0]
    head = f"{fmt_mac(src)} → {fmt_mac(dst)}"
    p = eth[14:]
    if et == 0x0806 and len(p) >= 28:
        op = struct.unpack(">H", p[6:8])[0]
        if op == 1:
            return f"ARP qui a {ip(p[24:28])} ? demande {ip(p[14:18])} ({fmt_mac(src)})"
        return f"ARP {ip(p[14:18])} est à {fmt_mac(p[8:14])}"
    if et == 0x86DD:
        nh = p[6] if len(p) > 6 else None
        kind = "ICMPv6" if nh == 58 else f"proto {nh}"
        if nh == 58 and len(p) > 40:
            kind += f" type {p[40]}"
        return f"IPv6 {kind} {head}"
    if et != 0x0800 or len(p) < 20:
        return f"ethertype 0x{et:04x} {head} ({len(eth)} octets)"
    ihl = (p[0] & 15) * 4
    proto, s, d = p[9], ip(p[12:16]), ip(p[16:20])
    if proto == 1 and len(p) > ihl:
        return f"ICMP type {p[ihl]} {s} → {d}"
    if proto not in (6, 17) or len(p) < ihl + 8:
        return f"IPv4 proto {proto} {s} → {d}"
    sp, dp = struct.unpack(">HH", p[ihl:ihl + 4])
    if proto == 6:
        return f"TCP {s}:{sp} → {d}:{dp}"
    u = p[ihl + 8:]
    if 67 in (sp, dp) or 68 in (sp, dp):
        m = parse_dhcp(u)
        if m:
            t = DHCP_TYPES.get(m["type"], m["type"])
            cls = m["opts"].get(60, b"").decode(errors="replace")
            extra = ""
            if m["type"] in (2, 5):
                extra = f" → {m['yiaddr']}, TFTP {m['siaddr']} « {m['file']} »"
            elif m["type"] == 3 and 50 in m["opts"]:
                extra = f" demande {ip(m['opts'][50])}"
            return f"DHCP {t} de {fmt_mac(m['chaddr'])} xid {m['xid']:08x}{extra}" + (f" [{cls}]" if cls else "")
    if 69 in (sp, dp) or (len(u) >= 4 and u[0] == 0 and u[1] in TFTP_OPS and dp > 1023 and sp > 1023):
        op = u[1] if len(u) > 1 else 0
        name = TFTP_OPS.get(op, op)
        if op in (1, 2):
            fn = u[2:].split(b"\0")[0].decode(errors="replace")
            return f"TFTP {name} « {fn} » {s} → {d}"
        if op in (3, 4) and len(u) >= 4:
            blk = struct.unpack(">H", u[2:4])[0]
            return f"TFTP {name} bloc {blk}" + (f" ({len(u) - 4} octets)" if op == 3 else "") + f" {s} → {d}"
        if op == 5:
            return f"TFTP ERROR « {u[4:].split(b'\\0')[0].decode(errors='replace')} » {s} → {d}"
        return f"TFTP {name} {s} → {d}"
    if 37 in (sp, dp):
        return f"ToD (heure) {s}:{sp} → {d}:{dp}" + (f" = {struct.unpack('>I', u[:4])[0]}" if len(u) == 4 else "")
    if 514 in (sp, dp):
        return f"syslog {s} → {d}: {u.decode(errors='replace')[:80]}"
    if 161 in (sp, dp) or 162 in (sp, dp):
        return f"SNMP {s}:{sp} → {d}:{dp}"
    return f"UDP {s}:{sp} → {d}:{dp} ({len(u)} octets)"
