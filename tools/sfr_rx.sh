#!/bin/bash
# Capture of the SFR downstream, RECEIVE ONLY: uhd_rx_cfile opens no transmit stream, and the
# B210's TX/RX port must be DISCONNECTED (nothing on it, or a 50 ohm load). RX2 <- 8 dB <- coupler.
#   sfr_rx.sh check [gain]            0.2 s: levels, clipping, channels seen (file deleted)
#   sfr_rx.sh rec SECONDS [gain]      $DIR/sfr_HHMMSS.sc16 (+ .t0 = time of the first sample)
# CENTER=298e6 RATE=48e6 by default: 274-322 MHz, SFR's two primary channels (282 and 314 MHz,
# 2026-10-06) in one capture, 192 MB/s on disk. Then: sfr_listen.py <file> --out <prefix>
set -u
CENTER=${CENTER:-298e6}
RATE=${RATE:-48e6}
DIR=${DIR:-/root/sfrcap}
CODE=${CODE:-/root/mini-cmts}
if pgrep -f "python3 mini_cmts" >/dev/null; then
  echo "!! le CMTS tourne (il émet) : /root/cmts-tools/stop.sh d'abord"; exit 1
fi
mkdir -p "$DIR"
mode=${1:-check}
if [ "$mode" = check ]; then
  secs=0.25; gain=${2:-30}; out=$DIR/check.sc16
else
  secs=${2:?durée en secondes}; gain=${3:-30}; out=$DIR/sfr_$(date +%H%M%S).sc16
  need=$(python3 -c "print(int($secs * $RATE * 4 / 1e9) + 2)")
  avail=$(df -BG --output=avail "$DIR" | tail -1 | tr -dc 0-9)
  if [ "$avail" -le "$need" ]; then echo "!! $need Go nécessaires, $avail libres"; exit 1; fi
fi
n=$(python3 -c "print(int($secs * $RATE))")
echo "réception seule : $CENTER Hz, $RATE éch/s, gain $gain dB, $secs s -> $out"
# UHD's C++ example rather than uhd_rx_cfile (GNU Radio): with 16 KB USB frames, as the CMTS,
# 40 s at 48 MS/s to disk without one overflow; uhd_rx_cfile lost samples every 1-3 s (2026-10-11)
chrt -f 45 /usr/libexec/uhd/examples/rx_samples_to_file \
  --args "type=b200,master_clock_rate=$RATE,recv_frame_size=16360,num_recv_frames=256" \
  --file "$out" --type short --duration "$secs" --rate "$RATE" --freq "$CENTER" --gain "$gain" \
  --ant RX2 --subdev A:A --spb 200000 > "$out.log" 2>&1
end=$(date +%s.%N)
size=$(stat -c %s "$out")
python3 -c "print($end - $size / 4 / $RATE)" > "$out.t0"
lost=$(python3 -c "print(max(0, int($n - $size / 4)))")
echo "fichier $((size / 1000000)) Mo ($(python3 -c "print(f'{$size / 4 / $RATE:.1f}')") s), échantillons perdus (débordements) : $lost"
grep -iE "error|fail" "$out.log" | head -3
python3 "$CODE/tools/iq_level.py" "$out" --fs "$RATE" --center "$CENTER"
if [ "$mode" = check ]; then rm -f "$out" "$out.t0" "$out.log"; fi
