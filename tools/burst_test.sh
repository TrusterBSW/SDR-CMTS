#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# Downloads that start after a quiet spell, the case that made both radios underflow together
# (docs/JOURNAL.fr.md, 15/10/2026): N times, IDLE seconds without traffic, then 15 s of iperf3
# downstream through the modem; after each, the TX stalls, GC pauses and lock losses of the run.
#
#   tools/burst_test.sh <run name> [N=3] [IDLE=240]
#
# Run on the PC behind the modem. CMTS_HOST: the CMTS host (ssh root), CLIENT_IP: this PC's
# address behind the modem; iperf3 -s must listen on 10.10.0.1 on the CMTS host. LOG: the CMTS log
# on that host (default: the one named in /root/cmts-tools/<run>.out; the Debian package's service
# writes /var/lib/sdr-cmts/captures/cmts_<date>_<time>.log).
run=${1:?usage: tools/burst_test.sh <nom du run> [N] [IDLE]}; n=${2:-3}; idle=${3:-240}
host=${CMTS_HOST:-10.5.1.3}; client=${CLIENT_IP:-10.10.0.11}
L=${LOG:-$(ssh root@$host "echo /root/mini-cmts-multi/\$(head -1 /root/cmts-tools/$run.out | awk '{print \$2}')")}
cnt() {
  ssh root@$host "echo blocages \$(grep -c 'blocage de' $L) gc \$(grep -c '!! ramasse-miettes' $L) verrou \$(grep -c 'perte de verrou' $L)"
}
for i in $(seq 1 $n); do
  echo "$(date +%T) calme ${idle} s ($(cnt))"
  sleep $idle
  echo "$(date +%T) iperf descendant"
  timeout 40 iperf3 -c 10.10.0.1 -B $client -P 8 -R -t 15 -O 3 2>&1 | grep -E "SUM.*receiver" | awk '{print "   DS", $(NF-2), $(NF-1)}'
  sleep 3
  echo "$(date +%T) après : $(cnt)"
done
ssh root@$host "grep -E 'blocage de|ramasse-miettes|\[MAC\] !!|!! encodeur|!! envoi|!! lecture' $L | cut -c1-200"
