#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# Capture of the UPSTREAM band of the modem's origin network (the modem's bursts, 5-65 MHz, under the B210's range) with
# the HackRF on the laptop, RECEIVE ONLY (hackrf_transfer -r). AMPLIFIER OFF: the modem transmits
# up to ~+1 dBm, the HackRF takes -5 dBm at most; on a splitter port beside the modem's.
#   hackrf_us.sh check [lna] [vga]          2 s: levels, clipping, channels (file deleted)
#   hackrf_us.sh rec SECONDS [lna] [vga]    $DIR/us_HHMMSS.s8 (+ .t0)
# CENTER=38e6, 20 MS/s, 15 MHz filter: the origin network's 34.8 and 41.2 MHz channels (2026-10-06); 47.6 folds
# outside them, 54.0 onto 34.8 but ~13-19 MHz out, where the 15 MHz filter cuts it. The B210 takes
# the downstream meanwhile (one receive LO for its two inputs: no 50-65 MHz at the same time).
set -u
CENTER=${CENTER:-38e6}
RATE=20e6
DIR=${DIR:-$HOME/origincap}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$DIR"
mode=${1:-check}
if [ "$mode" = check ]; then
  secs=2; lna=${2:-8}; vga=${3:-10}; out=$DIR/us_check.s8
else
  secs=${2:?durée en secondes}; lna=${3:-8}; vga=${4:-10}; out=$DIR/us_$(date +%H%M%S).s8
fi
n=$(python3 -c "print(int($secs * $RATE))")
echo "réception seule (HackRF) : $CENTER Hz, 20 MS/s, LNA $lna VGA $vga, ampli coupé, $secs s -> $out"
hz=$(python3 -c "print(int(float('$CENTER')))")
hackrf_transfer -r "$out" -f "$hz" -s 20000000 -b 15000000 -a 0 -l "$lna" -g "$vga" -n "$n" > "$out.log" 2>&1
end=$(date +%s.%N)
size=$(stat -c %s "$out" 2>/dev/null || echo 0)
if [ "$size" = 0 ]; then echo "!! rien d'enregistré :"; tail -3 "$out.log"; exit 1; fi
python3 -c "print($end - $size / 2 / $RATE)" > "$out.t0"
echo "fichier $((size / 1000000)) Mo ($(python3 -c "print(f'{$size / 2 / $RATE:.1f}')") s)"
grep -iE "error|fail|overrun|underrun" "$out.log" | head -3
python3 "$HERE/iq_level.py" "$out" --fmt s8 --fs "$RATE" --center "$CENTER" --first 34.8e6 --step 6.4e6 \
  --width 6.0e6 --span "$( [ "$mode" = check ] && echo 2 || echo 1 )"
if [ "$mode" = check ]; then rm -f "$out" "$out.t0" "$out.log"; fi
