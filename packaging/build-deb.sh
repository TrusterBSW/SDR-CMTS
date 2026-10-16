#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# Builds the Debian package of sdr-cmts (needs dpkg-deb, python3 and NumPy for the manual page):
#   ./packaging/build-deb.sh [out dir]
#   -> sdr-cmts_<VERSION>_all.deb
#   sudo apt install ./sdr-cmts_<VERSION>_all.deb      (pulls the dependencies)
# Architecture all: Python and C sources; the C kernels are compiled by postinst for the CPU of
# the machine it is installed on (-march=native).
set -eu
cd "$(dirname "$0")/.."
VERSION=$(cat VERSION)
OUT=${1:-.}
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

lib=$ROOT/usr/lib/sdr-cmts
install -d "$lib/cmts" "$lib/tools" "$ROOT/usr/bin" "$ROOT/etc/sdr-cmts" "$ROOT/etc/security/limits.d" \
           "$ROOT/lib/systemd/system" "$ROOT/usr/share/sdr-cmts/configs" "$ROOT/usr/share/doc/sdr-cmts/docs" \
           "$ROOT/DEBIAN"
install -m 755 sdr_cmts.py provision.py "$lib/"
install -m 644 VERSION LICENSE COMMERCIAL.md "$lib/"
install -m 644 cmts/*.py "$lib/cmts/"
install -m 644 cmts/usdsp.c cmts/dsdsp.c cmts/fastdsp.c "$lib/cmts/"
install -m 755 tools/bladerf_check.py tools/clock_drift.py "$lib/tools/"
install -m 755 packaging/sdr-cmts "$ROOT/usr/bin/"
install -m 644 packaging/sdr-cmts.toml "$ROOT/etc/sdr-cmts/"
install -m 644 packaging/limits.conf "$ROOT/etc/security/limits.d/99-sdr-cmts.conf"
install -m 644 packaging/sdr-cmts.service "$ROOT/lib/systemd/system/"
install -m 644 configs/*.toml "$ROOT/usr/share/sdr-cmts/configs/"
install -m 644 README.md README.fr.md "$ROOT/usr/share/doc/sdr-cmts/"
install -m 644 docs/*.md "$ROOT/usr/share/doc/sdr-cmts/docs/"
{ printf 'Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\nUpstream-Name: sdr-cmts\n'
  printf 'Comment: dual-licensed, AGPL-3.0-only or a commercial licence from the copyright holder (COMMERCIAL.md)\n\n'
  printf 'Files: *\nCopyright: 2026 BILLY Rémi\nLicense: AGPL-3.0-only\n'
  sed -e 's/^$/./' -e 's/^/ /' LICENSE; } > "$ROOT/usr/share/doc/sdr-cmts/copyright"
install -m 644 COMMERCIAL.md CONTRIBUTING.md "$ROOT/usr/share/doc/sdr-cmts/"
# man sdr-cmts: written from the option parsers (packaging/gen_man.py), so never out of date
install -d "$ROOT/usr/share/man/man1"
python3 packaging/gen_man.py | gzip -9n > "$ROOT/usr/share/man/man1/sdr-cmts.1.gz"
# no per-file backups or build products in the package
find "$ROOT" \( -name '*_pre_*' -o -name '*.so' -o -name '__pycache__' \) -prune -exec rm -rf {} +

install -m 755 packaging/debian/postinst packaging/debian/prerm packaging/debian/postrm "$ROOT/DEBIAN/"
install -m 644 packaging/debian/conffiles "$ROOT/DEBIAN/"
SIZE=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" packaging/debian/control.in > "$ROOT/DEBIAN/control"

mkdir -p "$OUT"
deb="$OUT/sdr-cmts_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$deb" >/dev/null
echo "$deb"
