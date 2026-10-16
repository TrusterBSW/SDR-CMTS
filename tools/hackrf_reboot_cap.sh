#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# One downstream channel of the modem's origin network captured with the HackRF (RECEIVE ONLY) while the modem is power-cycled
# through its Home Assistant plug, with a tcpdump of the modem's Ethernet side for the timeline.
#   hackrf_reboot_cap.sh [SECONDS] [FREQ]     -> ~/origincap/rb_HHMMSS.s8 (+ .t0, .eth.pcap, .cycle)
# Amplifier ON (allowed by the user 2026-10-12: modem bursts measured at ~-21 dBm, ~-18 dBm while
# ranging, at the HackRF's splitter port, limit -5 dBm), LNA 32 VGA 16, 15 MHz filter: MER ~25 dB.
set -u
secs=${1:-170}
freq=${2:-282e6}
DIR=${DIR:-$HOME/origincap}
HERE=$(cd "$(dirname "$0")" && pwd)
out=$DIR/rb_$(date +%H%M%S)
hz=$(python3 -c "print(int(float('$freq')))")
n=$(python3 -c "print(int($secs * 20e6))")
sudo tcpdump -i enp0s20f0u4 -n -w "$out.eth.pcap" >/dev/null 2>&1 &
tcp=$!
python3 -c "import time; print(time.time())" > "$out.s8.t0"
hackrf_transfer -r "$out.s8" -f "$hz" -s 20000000 -b 15000000 -a 1 -l 32 -g 16 -n "$n" > "$out.log" 2>&1 &
hrf=$!
sleep 5
"$HERE/modem_cycle.sh" 10 > "$out.cycle" 2>&1
wait $hrf
sudo kill -INT $tcp 2>/dev/null
wait $tcp 2>/dev/null
cat "$out.cycle"
echo "$out.s8 : $(($(stat -c %s "$out.s8") / 40000000)) s"
