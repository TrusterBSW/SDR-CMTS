# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Shared memory reserved at creation (cmts/usproc.py create_shm): its pages are reserved at once,
so a /dev/shm too small is said at start with what is missing, instead of a SIGBUS in the radio
process at the first write past the room left (Docker's default /dev/shm: 64 MB); a refused
segment is removed, not left behind."""
import errno
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import usproc  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def segments():
    try:
        return {f for f in os.listdir("/dev/shm") if f.startswith("psm_")}
    except OSError:
        return set()


s = usproc.create_shm(1 << 20, "un essai")
check("segment créé et réservé", s.size == 1 << 20)
s.close()
s.unlink()

before = segments()
real = os.posix_fallocate


def full(fd, off, n):
    raise OSError(errno.ENOSPC, "No space left on device")


os.posix_fallocate = full
try:
    usproc.create_shm(110_000_000, "l'anneau d'essai")
    check("place refusée : erreur claire", False)
except usproc.ShmError as e:
    check("place refusée : erreur claire", "/dev/shm trop petit" in str(e) and "110 Mo" in str(e)
          and "--shm-size" in str(e), str(e)[:90])
finally:
    os.posix_fallocate = real
check("segment refusé : supprimé", segments() == before, str(sorted(segments() - before)))
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
