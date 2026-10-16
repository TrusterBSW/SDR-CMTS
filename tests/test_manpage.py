# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""The manual page sdr-cmts(1) (packaging/gen_man.py): every option of sdr_cmts.py and of
provision.py (but the two that --all-in-one sets itself) is described in it, and groff renders it
without a warning when groff is installed."""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import sdr_cmts  # noqa: E402
import provision  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


page = subprocess.run([sys.executable, os.path.join(HERE, "..", "packaging", "gen_man.py")],
                      capture_output=True, text=True, check=True).stdout
text = page.replace("\\-", "-")
missing = [o for ap, skip in ((sdr_cmts.parser(), ()), (provision.parser(), ("--iface", "--secret")))
           for a in ap._actions for o in a.option_strings
           if o.startswith("--") and o != "--help" and o not in skip and not re.search(rf"\\fB{re.escape(o)}\\fR", text)]
check("chaque option longue est décrite", not missing, ", ".join(missing[:8]))
check("sections attendues", all(f".SH {s}" in page for s in ("NAME", "SYNOPSIS", "DESCRIPTION", "SAFETY", "FILES",
                                                             "EXAMPLES", "LICENSE", "ENVIRONMENT",
                                                             '"EXIT STATUS"')))
check("radio simulée et code 3 décrits", "SDR_CMTS_SDR_SIM" in text and "SDR_CMTS_SIM_UNPLUG_S" in text
      and "exit code 3" in text)
if shutil.which("groff"):
    r = subprocess.run(["groff", "-man", "-Tutf8", "-ww", "-z"], input=page, capture_output=True, text=True)
    check("groff sans avertissement", not r.stderr.strip(), r.stderr.strip()[:120])
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
