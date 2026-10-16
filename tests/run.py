#!/usr/bin/env python3
"""Runs every tests/test_*.py, one process each, and sums up: PASS, FAIL or SKIP.

    python3 tests/run.py               # everything that needs no radio and no root
    python3 tests/run.py mtc tapfeed   # only the tests whose name contains one of these words
    python3 tests/run.py --sudo        # also test_provision_tap.py (creates cmts0, runs provision.py as root)

A test is skipped, not failed, when what it needs is missing: GNU Radio (gr-dtv cross-checks),
tshark, root. The exit code is 0 when nothing failed (the CI uses it).
"""
import argparse
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NEEDS_ROOT = {"test_provision_tap.py"}
# a test stopped by one of these lines lacks an optional tool, it did not fail
MISSING = ("No module named 'gnuradio'", "No module named 'uhd'", "tshark: not found",
           "No such file or directory: 'tshark'")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("words", nargs="*", help="only the tests whose file name contains one of these")
    ap.add_argument("--sudo", action="store_true", help="also the tests that need root (sudo)")
    ap.add_argument("--timeout", type=float, default=900, help="seconds per test (default 900)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the output of every test")
    a = ap.parse_args()
    tests = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if a.words:
        tests = [t for t in tests if any(w in os.path.basename(t) for w in a.words)]
    res = {"PASS": [], "FAIL": [], "SKIP": []}
    t_all = time.monotonic()
    for t in tests:
        name = os.path.basename(t)
        if name in NEEDS_ROOT and not a.sudo:
            res["SKIP"].append(name)
            print(f"SKIP {name:34s} (root : --sudo)", flush=True)
            continue
        t0 = time.monotonic()
        try:
            p = subprocess.run([sys.executable, t], capture_output=True, text=True, timeout=a.timeout,
                               cwd=os.path.dirname(HERE))
            out, rc = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, rc = (e.stdout or b"").decode(errors="replace") + f"\n!! délai de {a.timeout:.0f} s dépassé", -1
        dt = time.monotonic() - t0
        last = next((ln for ln in reversed(out.strip().splitlines()) if ln.strip()), "")
        if rc == 0 and last.startswith("SKIP"):
            verdict, why = "SKIP", last[4:].strip(" ()")
        elif rc != 0 and any(m in out for m in MISSING):
            verdict, why = "SKIP", next(m for m in MISSING if m in out)
        else:
            verdict, why = ("PASS" if rc == 0 else "FAIL"), ""
        res[verdict].append(name)
        print(f"{verdict} {name:34s} {dt:6.1f} s" + (f"  ({why})" if why else ""), flush=True)
        if a.verbose or verdict == "FAIL":
            print("     " + out.strip().replace("\n", "\n     "), flush=True)
    print(f"\n{len(res['PASS'])} réussis, {len(res['FAIL'])} en échec, {len(res['SKIP'])} sautés "
          f"en {time.monotonic() - t_all:.0f} s" + (f" ; échecs : {', '.join(res['FAIL'])}" if res["FAIL"] else ""))
    return 1 if res["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
