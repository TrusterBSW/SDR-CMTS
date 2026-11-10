#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# Cost of the MAC (Python) per downstream frame: the CMTS on the simulated radio (6 DS + 2 US),
# a real UDP flow sent into cmts0 towards a made-up device (48 then 150 Mbit/s, iperf 2), the CPU of
# every MAC thread and the frames handled, then a py-spy profile (PYSPY=path, default /root/py-spy).
# As root on a host with the sdr-cmts package and no CMTS running; transmits nothing.
# i5-9500 (2026-11-01): 0.15 core idle, 0.33 at 48 Mbit/s, 0.42 at 150: ~20 µs per frame.
# ARGS: more options of the CMTS (e.g. ARGS=--no-ds-dataplane: the former Python data path);
# RATES: the UDP rates in Mbit/s (default "0 48 150").
PYSPY=${PYSPY:-/root/py-spy}
ARGS=${ARGS:-}
RATES=${RATES:-0 48 150}
cd /tmp
SDR_CMTS_SDR_SIM=1 sdr-cmts --ds-channels 6 --ds-layout around --us-channels 2 --tap cmts0 --status-http none \
   --color never --log /tmp/mac_load.log $ARGS > /tmp/mac_load.out 2>&1 &
MAC=$!
sleep 25
ip addr show cmts0 | grep -q "10.10.0.1/" || ip addr add 10.10.0.1/24 dev cmts0
ip link set cmts0 up
ip neigh replace 10.10.0.50 lladdr 02:00:00:00:00:50 dev cmts0 nud permanent
cpu() { awk '{print $14+$15}' /proc/$MAC/stat; }
thr() { for t in /proc/$MAC/task/*; do echo "$(basename $t) $(cut -d' ' -f14,15 $t/stat | awk '{print $1+$2}') $(cat $t/comm)"; done; }
txp() { cat /sys/class/net/cmts0/statistics/tx_packets; }
for rate in $RATES; do
  [ $rate -gt 0 ] && (iperf -u -c 10.10.0.50 -b ${rate}M -l 1472 -t 45 >/dev/null 2>&1 &)
  sleep 5
  c0=$(cpu); p0=$(txp); thr > /tmp/t0; sleep 30; c1=$(cpu); p1=$(txp); thr > /tmp/t1
  fps=$(( (p1 - p0) / 30 ))
  python3 - "$rate" "$c0" "$c1" "$fps" <<'PY'
import sys
rate, c0, c1, fps = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
core = (c1 - c0) / 100 / 30
a = {l.split()[0]: (int(l.split()[1]), " ".join(l.split()[2:])) for l in open("/tmp/t0")}
b = {l.split()[0]: (int(l.split()[1]), " ".join(l.split()[2:])) for l in open("/tmp/t1")}
th = sorted(((b[t][0] - a[t][0]) / 100 / 30, b[t][1]) for t in b if t in a)[::-1]
print(f"{rate:>3} Mbit/s : MAC {core:.2f} cœur, {fps} trames/s vers le TAP" + (f" -> {core * 1e6 / fps:.0f} µs de CPU par trame (tout compris)" if fps > 100 else ""))
print("        fils : " + ", ".join(f"{n} {c:.2f}" for c, n in th[:5] if c >= 0.02))
PY
  [ $rate = 150 ] && $PYSPY record --pid $MAC --duration 20 --rate 250 --format raw -o /tmp/mac_prof.txt --nonblocking >/dev/null 2>&1
  sleep 12
done
pkill -f "iperf -u -c 10.10.0.50"
grep -E "jetées|!! " /tmp/mac_load.out | tail -3 | cut -c1-160
kill -INT $MAC; wait $MAC 2>/dev/null
ip neigh del 10.10.0.50 dev cmts0 2>/dev/null
python3 - <<'PY'
import collections
self_t, tot = collections.Counter(), 0
for line in open("/tmp/mac_prof.txt"):
    stack, _, n = line.rpartition(" ")
    n = int(n); tot += n
    frames = stack.split(";")
    leaf = frames[-1]
    self_t[leaf.split(" (")[0] + " (" + leaf.split(" (")[-1] if " (" in leaf else leaf] += n
print(f"profil py-spy à 150 Mbit/s ({tot} échantillons, tous fils) — temps propre des fonctions :")
for f, n in self_t.most_common(14):
    print(f"  {100 * n / tot:5.1f} %  {f[:110]}")
PY
