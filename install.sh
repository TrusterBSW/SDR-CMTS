#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# Installs sdr-cmts from its Debian package, on Debian 12 / 13, Proxmox VE 8 / 9 or Ubuntu 24.04 and later:
#
#   curl -fsSL https://raw.githubusercontent.com/TrusterBSW/SDR-CMTS/main/install.sh | sudo sh
#   curl -fsSL https://raw.githubusercontent.com/TrusterBSW/SDR-CMTS/main/install.sh | sudo sh -s -- --uhd-images
#   sudo ./install.sh --deb dist/sdr-cmts_0.1.0_all.deb        (a package built here, see packaging/)
#
# Options:
#   --deb FILE     install this .deb instead of downloading the latest one
#   --uhd-images   also download the USRP B210's FPGA images (uhd_images_downloader -t b2xx)
#   --force        go on with a release that is not one of the above
#
# It does not start transmitting: edit /etc/sdr-cmts/sdr-cmts.toml first, check the cabling (closed
# coax, attenuators), then "sudo systemctl enable --now sdr-cmts". Never on an operator's network.
set -eu

BASE=${SDR_CMTS_BASE_URL:-https://github.com/TrusterBSW/SDR-CMTS/raw/main/releases}
DEB="" IMAGES=0 FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
    --deb) DEB=${2:?--deb needs a file}; shift ;;
    --uhd-images) IMAGES=1 ;;
    --force) FORCE=1 ;;
    -h | --help) sed -n '4,17s/^# \{0,1\}//p' "$0" 2>/dev/null || echo "see the head of install.sh"; exit 0 ;;
    *) echo "install.sh: unknown option $1 (--help)" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run it as root (sudo sh, or sudo ./install.sh)"
command -v apt-get >/dev/null || die "apt-get not found: Debian, Ubuntu or Proxmox VE needed"

# the release
. /etc/os-release
ok=0
case "$ID" in
debian) case "${VERSION_ID:-}" in 12 | 13) ok=1 ;; esac ;;
ubuntu) [ "${VERSION_ID%%.*}" -ge 24 ] 2>/dev/null && ok=1 ;;
esac
what="$PRETTY_NAME"
if [ -e /etc/pve ] || command -v pveversion >/dev/null 2>&1; then
    what="Proxmox VE $(pveversion 2>/dev/null | sed -n 's|^pve-manager/\([0-9.]*\).*|\1|p') on $PRETTY_NAME"
fi
if [ "$ok" = 1 ]; then
    say "system: $what"
elif [ "$FORCE" = 1 ]; then
    say "system: $what (not tested, --force)"
else
    die "$what is not supported (Debian 12/13, Proxmox VE 8/9, Ubuntu 24.04 or later); --force to try anyway"
fi
arch=$(dpkg --print-architecture)
case "$arch" in amd64 | arm64) ;; *) say "architecture $arch: not tested (amd64 and arm64 are)" ;; esac

# the package
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
fetch() {   # URL FILE
    if command -v curl >/dev/null; then curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null; then wget -q "$1" -O "$2"
    else apt-get install -y -qq curl >/dev/null && curl -fsSL "$1" -o "$2"; fi
}
if [ -n "$DEB" ]; then
    [ -f "$DEB" ] || die "$DEB: no such file"
    DEB=$(realpath "$DEB")
    say "package: $DEB"
else
    fetch "$BASE/LATEST" "$TMP/LATEST" || die "cannot download $BASE/LATEST"
    name=$(head -1 "$TMP/LATEST" | tr -d '[:space:]')
    case "$name" in sdr-cmts_*_all.deb) ;; *) die "unexpected LATEST: $name" ;; esac
    say "downloading $name"
    fetch "$BASE/$name" "$TMP/$name" || die "cannot download $BASE/$name"
    fetch "$BASE/SHA256SUMS" "$TMP/SHA256SUMS" || die "cannot download $BASE/SHA256SUMS"
    (cd "$TMP" && grep " $name\$" SHA256SUMS | sha256sum -c --quiet -) || die "$name: checksum mismatch"
    DEB="$TMP/$name"
    chmod 644 "$DEB"
fi

# installed by apt: Python, NumPy, SciPy, gcc (the C kernels are compiled now, for this CPU), UHD
say "installing (apt pulls python3, numpy, scipy, gcc, UHD; the C kernels are compiled for this CPU)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y "$DEB"
if [ "$IMAGES" = 1 ]; then
    if command -v uhd_images_downloader >/dev/null; then
        say "USRP B210 FPGA images"
        uhd_images_downloader -t b2xx || say "uhd_images_downloader failed: run it again later"
    else
        say "uhd_images_downloader not found (package uhd-host): B210 images skipped"
    fi
fi

# what is there
say "sdr-cmts $(cat /usr/lib/sdr-cmts/VERSION 2>/dev/null) installed"
for n in usdsp dsdsp fastdsp; do
    [ -f "/usr/lib/sdr-cmts/cmts/_$n.so" ] || say "!! $n.c not compiled: the Python fallback is far too slow"
done
if command -v uhd_find_devices >/dev/null && uhd_find_devices 2>/dev/null | grep -q serial; then
    say "radios plugged in:"
    uhd_find_devices 2>/dev/null | grep -E "serial|name" | sed 's/^ */    /'
fi
cat <<'EOF'

Next:
  sudoedit /etc/sdr-cmts/sdr-cmts.toml          the bench: radio, channels, provisioning (man sdr-cmts)
  sudo sdr-cmts --config /etc/sdr-cmts/sdr-cmts.toml    in the foreground, then power the modem on
  sudo systemctl enable --now sdr-cmts          as a service, now and at every boot
  http://127.0.0.1:8090/                        the status page (status_http = "0.0.0.0:8090" for the LAN)
  SDR_CMTS_SDR_SIM=1 sdr-cmts --tap none        a try without any radio
Closed coax only, with attenuators: never connect it to an operator's network.
EOF
