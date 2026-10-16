#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# Builds the Debian package of mini-cmts (needs only dpkg-deb): ./packaging/build-deb.sh [out dir]
#   -> mini-cmts_<VERSION>_all.deb
#   sudo apt install ./mini-cmts_<VERSION>_all.deb      (pulls the dependencies)
# Architecture all: Python and C sources; the C kernels are compiled by postinst for the CPU of
# the machine it is installed on (-march=native).
set -eu
cd "$(dirname "$0")/.."
VERSION=$(cat VERSION)
OUT=${1:-.}
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

lib=$ROOT/usr/lib/mini-cmts
install -d "$lib/cmts" "$lib/tools" "$ROOT/usr/bin" "$ROOT/etc/mini-cmts" "$ROOT/etc/security/limits.d" \
           "$ROOT/lib/systemd/system" "$ROOT/usr/share/mini-cmts/configs" "$ROOT/usr/share/doc/mini-cmts/docs" \
           "$ROOT/DEBIAN"
install -m 755 mini_cmts.py provision.py "$lib/"
install -m 644 VERSION LICENSE COMMERCIAL.md "$lib/"
install -m 644 cmts/*.py "$lib/cmts/"
install -m 644 cmts/usdsp.c cmts/dsdsp.c cmts/fastdsp.c "$lib/cmts/"
install -m 755 tools/bladerf_check.py tools/clock_drift.py "$lib/tools/"
install -m 755 packaging/mini-cmts "$ROOT/usr/bin/"
install -m 644 packaging/mini-cmts.toml "$ROOT/etc/mini-cmts/"
install -m 644 packaging/limits.conf "$ROOT/etc/security/limits.d/99-mini-cmts.conf"
install -m 644 packaging/mini-cmts.service "$ROOT/lib/systemd/system/"
install -m 644 configs/*.toml "$ROOT/usr/share/mini-cmts/configs/"
install -m 644 README.md README.fr.md "$ROOT/usr/share/doc/mini-cmts/"
install -m 644 docs/*.md "$ROOT/usr/share/doc/mini-cmts/docs/"
{ printf 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\nUpstream-Name: mini-cmts\n'
  printf 'Comment: dual-licensed, AGPL-3.0-only or a commercial licence from the copyright holder (COMMERCIAL.md)\n\n'
  printf 'Files: *\nCopyright: 2026 BILLY Rémi\nLicense: AGPL-3.0-only\n'
  sed -e 's/^$/./' -e 's/^/ /' LICENSE; } > "$ROOT/usr/share/doc/mini-cmts/copyright"
install -m 644 COMMERCIAL.md CONTRIBUTING.md "$ROOT/usr/share/doc/mini-cmts/"
# no per-file backups or build products in the package
find "$ROOT" \( -name '*_pre_*' -o -name '*.so' -o -name '__pycache__' \) -prune -exec rm -rf {} +

install -m 755 packaging/debian/postinst packaging/debian/prerm packaging/debian/postrm "$ROOT/DEBIAN/"
install -m 644 packaging/debian/conffiles "$ROOT/DEBIAN/"
SIZE=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" packaging/debian/control.in > "$ROOT/DEBIAN/control"

mkdir -p "$OUT"
deb="$OUT/mini-cmts_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$deb" >/dev/null
echo "$deb"
