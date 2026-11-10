#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# Installs the sdr-cmts .deb in this (fresh) system and checks it; run as root in a container of each
# release by packaging/build-all-debs.sh and .github/workflows/packages.yml:
#   sh packaging/check-deb.sh dist/sdr-cmts_<version>_all.deb
# Needs /dev/shm of 1 GB or more (docker run --shm-size=1g) for the start on the simulated radio.
set -eu
DEB=$(realpath "$1")
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
echo "-- $PRETTY_NAME"
rm -f /etc/dpkg/dpkg.cfg.d/excludes /etc/dpkg/dpkg.cfg.d/docker      # images without manual pages
apt-get update -qq
apt-get install -y -qq man-db lintian "$DEB" >/dev/null 2>&1 || apt-get install -y man-db lintian "$DEB"
bad=0
fail() { echo "FAIL $*"; bad=1; }

sdr-cmts --help >/dev/null && echo "ok   sdr-cmts --help" || fail "sdr-cmts --help"
for n in usdsp dsdsp fastdsp; do
    [ -f /usr/lib/sdr-cmts/cmts/_$n.so ] && echo "ok   $n.c compiled at installation" || fail "$n.c not compiled"
done
python3 -c 'import sys; sys.path.insert(0, "/usr/lib/sdr-cmts"); from cmts import cdsp
assert cdsp.AVAILABLE and cdsp.DS_AVAILABLE' && echo "ok   C kernels load" || fail "C kernels do not load"
man=$(command -v man.REAL || command -v man)                    # minimized Ubuntu images: man is a stub
$man -w sdr-cmts 2>/dev/null | grep -q /man1/sdr-cmts.1 && echo "ok   man sdr-cmts" || fail "man sdr-cmts"
systemctl cat sdr-cmts >/dev/null 2>&1 || [ -f /usr/lib/systemd/system/sdr-cmts.service ] &&
    echo "ok   sdr-cmts.service" || fail "sdr-cmts.service missing"
sdr-cmts --restore-host >/dev/null && echo "ok   sdr-cmts --restore-host" || fail "--restore-host"

# the whole chain on the simulated radio: MAC, radio and upstream processes; then a clean stop
cd /tmp
SDR_CMTS_SDR_SIM=1 timeout -s INT 25 sdr-cmts --ds-channels 2 --us-channels 1 --tap none --status-http none \
    --no-performance --no-irq-herd --rt-prio 0 --color never --log none > /tmp/run.out 2>&1 && rc=0 || rc=$?
if { [ "$rc" = 124 ] || [ "$rc" = 0 ]; } && grep -q "marge TX min" /tmp/run.out && grep -q "arrêt;" /tmp/run.out; then
    echo "ok   runs on the simulated radio and stops cleanly ($(grep -c 'marge TX min' /tmp/run.out) status lines)"
else
    fail "run on the simulated radio (code $rc)"; tail -15 /tmp/run.out
fi

out=$(lintian --no-tag-display-limit "$DEB" 2>&1 || true)
printf '%s\n' "$out" | sed 's/^/     lintian: /'
if printf '%s\n' "$out" | grep -q '^E:'; then fail "lintian errors"; else echo "ok   lintian: no error"; fi
exit $bad
