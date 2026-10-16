# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Bench files in TOML (sdr_cmts.py --config, cmts/tomlconf.py): the example files of configs/ give
the options they say; true / false become --x / --no-x; a list is joined with commas; the
[provisioning] section becomes one --prov-args for provision.py; the command line wins over the
file; an unknown key is refused with the nearest names."""
import glob
import os
import shlex
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sdr_cmts  # noqa: E402
import provision  # noqa: E402
from cmts import tomlconf  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


def parse(text, cli=()):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(text)
    try:
        argv = tomlconf.to_argv(f.name, sdr_cmts.parser(), provision.parser())
        return sdr_cmts.parser().parse_args(argv + list(cli))
    finally:
        os.unlink(f.name)


here = os.path.join(os.path.dirname(__file__), "..")
files = sorted(glob.glob(os.path.join(here, "configs", "*.toml")) + glob.glob(os.path.join(here, "packaging", "*.toml")))
bad = []
for path in files:
    try:
        sdr_cmts.parser().parse_args(tomlconf.to_argv(path, sdr_cmts.parser(), provision.parser()))
    except (ValueError, SystemExit) as e:
        bad.append(f"{os.path.basename(path)}: {e}")
check("les fichiers d'exemple (configs/, packaging/) se lisent", files and not bad, f"{len(files)} fichiers {bad}")

a = parse("""
[radio]
ds_radios = 2
radio_serials = ["A1", "B2"]
[downstream]
ds_channels = 4
[host]
performance = false
all-in-one = true
[provisioning]
dns = "1.1.1.1"
us_buffer = 65536
""")
check("valeurs, tirets ou soulignés dans les clés", a.ds_radios == 2 and a.ds_channels == 4 and a.all_in_one)
check("liste jointe par des virgules", a.radio_serials == "A1,B2", a.radio_serials)
check("false sur une option oui/non : --no-…", a.performance is False)
check("[provisioning] : un seul --prov-args pour provision.py",
      shlex.split(a.prov_args) == ["--dns=1.1.1.1", "--us-buffer=65536"]
      and provision.parser().parse_args(shlex.split(a.prov_args)).us_buffer == 65536, a.prov_args)
a = parse("[downstream]\nds_channels = 4\n", cli=["--ds-channels", "2"])
check("la ligne de commande l'emporte sur le fichier", a.ds_channels == 2)
a = sdr_cmts.parser().parse_args(["--us-spacing", "-7e6", "--us-freq", "6.0e7", "--ds-spacing", "-8e6"])
check("nombre négatif en notation scientifique pris pour une valeur", a.us_spacing == -7e6 and a.ds_spacing == -8e6)
path, rest = tomlconf.split_config(["--config", "x.toml", "--freq", "314e6", "--config=y.toml"])
check("--config retiré des arguments (le dernier compte)", path == "y.toml" and rest == ["--freq", "314e6"])
for text, what in (("[downstream]\nds_chanels = 4\n", "faute de frappe"),
                   ("[provisioning]\nmax_cpes = 4\n", "option de provision.py inconnue"),
                   ("[host]\nperformance = 1\n", "oui/non attendu")):
    try:
        parse(text)
        check(f"refusé : {what}", False)
    except ValueError as e:
        check(f"refusé : {what}", True, str(e).split(" : ", 1)[1][:70])
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
