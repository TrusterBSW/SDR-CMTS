#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
# Builds THE .deb (Architecture: all: Python and C sources, the C compiled by postinst for the CPU it
# lands on) in a clean Debian 12 container with nothing but dpkg-deb, then installs it in a fresh
# container of each release and checks it: Debian 12 and 13 (so Proxmox VE 8 and 9), Ubuntu 24.04.
#   packaging/build-all-debs.sh [out dir]          (default dist/)
# Checks in each release: apt installs it with its dependencies, the C kernels are compiled, the
# command and the manual page answer, the CMTS starts on the simulated radio (SDR_CMTS_SDR_SIM) and
# stops cleanly, lintian finds no error. TEST_IMAGES ("debian:12 debian:13 ubuntu:24.04") to change.
# The same steps as .github/workflows/packages.yml.
set -eu
cd "$(dirname "$0")/.."
OUT=$(realpath -m "${1:-dist}")
BUILD_IMAGE=${BUILD_IMAGE:-debian:12}
TEST_IMAGES=${TEST_IMAGES:-debian:12 debian:13 ubuntu:24.04}
DOCKER=$(command -v docker || command -v podman) || { echo "docker or podman needed" >&2; exit 1; }
mkdir -p "$OUT"
VERSION=$(cat VERSION)

echo "== $BUILD_IMAGE: the package (dpkg-deb only)"
$DOCKER run --rm -v "$PWD:/src:ro" -v "$OUT:/out" -e OWNER="$(id -u):$(id -g)" "$BUILD_IMAGE" sh -ec '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates >/dev/null
    cp -a /src /tmp/src && cd /tmp/src && git config --global --add safe.directory /tmp/src
    ! python3 -c "import numpy" 2>/dev/null || echo "(numpy present: the build must not need it)"
    deb=$(./packaging/build-deb.sh /out)
    chown "$OWNER" "$deb"
    echo "$deb"'

deb=$OUT/sdr-cmts_${VERSION}_all.deb
[ -f "$deb" ] || { echo "no $deb" >&2; exit 1; }
for img in $TEST_IMAGES; do
    echo "== $img: install and check"
    $DOCKER run --rm --shm-size=1g -v "$OUT:/dist:ro" -v "$PWD/packaging/check-deb.sh:/check-deb.sh:ro" "$img" \
        sh /check-deb.sh "/dist/${deb##*/}"
done
(cd "$OUT" && sha256sum "${deb##*/}" > SHA256SUMS && cat SHA256SUMS)
echo "$deb"
