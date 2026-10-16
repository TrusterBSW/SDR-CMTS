#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""The sdr-cmts(1) manual page, written from the option parsers themselves (sdr_cmts.py and
provision.py): it cannot fall behind the options. packaging/build-deb.sh installs it.

    python3 packaging/gen_man.py > sdr-cmts.1 && man ./sdr-cmts.1
"""
import argparse
import os
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import sdr_cmts  # noqa: E402
import provision  # noqa: E402

GROUPS = {"options": "General", "downstream (comme ds_tx.py)": "Downstream", "upstream": "Upstream",
          "upstream (suite)": "Upstream", "réseau et enregistrement des modems": "Network and modem registration",
          "simulation (sans matériel)": "Simulation (no hardware)"}


def esc(text):
    """Plain text -> roff: backslashes, hyphens of options, a leading dot or quote."""
    out = []
    for line in str(text).replace("\\", "\\e").split("\n"):
        line = line.replace("-", "\\-")
        if line.startswith((".", "'")):
            line = "\\&" + line
        out.append(line)
    return "\n".join(out)


def default_text(act):
    d = act.default
    if d is None or d is argparse.SUPPRESS or d is False or isinstance(act, argparse._HelpAction):
        return ""
    if isinstance(act, argparse.BooleanOptionalAction):
        return " (default: on)" if d else ""
    if isinstance(d, float) and d.is_integer():
        d = f"{d / 1e6:g}e6" if abs(d) >= 1e6 and (d / 1e6).is_integer() else int(d)    # 306e6, 30
    return f" (default: {d})" if d != "" else ""


def option(act):
    if isinstance(act, argparse.BooleanOptionalAction):
        names = " | ".join(f"\\fB{esc(o)}\\fR" for o in act.option_strings)
    else:
        meta = act.metavar or (("{" + ",".join(map(str, act.choices)) + "}") if act.choices else act.dest.upper())
        takes = act.nargs != 0
        names = ", ".join(f"\\fB{esc(o)}\\fR" + (f" \\fI{esc(meta)}\\fR" if takes else "") for o in act.option_strings)
    helptext = (act.help or "").replace("%(default)s", str(act.default)).replace("%%", "%")
    return f".TP\n{names}\n{esc(helptext + default_text(act))}\n"


def options_of(ap, skip=()):
    sections = {}
    for g in ap._action_groups:
        acts = [a for a in g._group_actions if a.option_strings and a.help != argparse.SUPPRESS
                and not isinstance(a, argparse._HelpAction) and a.dest not in skip]
        if acts:
            sections.setdefault(GROUPS.get(g.title, g.title.capitalize()), []).extend(acts)
    return sections


def main():
    try:
        with open(os.path.join(ROOT, "VERSION")) as f:
            version = f.read().strip()
    except OSError:
        version = ""
    date = time.strftime("%Y-%m-%d", time.gmtime(int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))))
    w = sys.stdout.write
    w(f'.TH SDR\\-CMTS 1 "{date}" "sdr\\-cmts {version}" "User Commands"\n')
    w(".SH NAME\nsdr\\-cmts \\- EuroDOCSIS 3.0 CMTS on a software radio (USRP B210, bladeRF 2.0 micro)\n")
    w(".SH SYNOPSIS\n.B sdr\\-cmts\n[\\fB\\-\\-config\\fR \\fIBENCH.toml\\fR] [\\fIOPTION\\fR]...\n.br\n"
      ".B sdr\\-cmts\n\\fB\\-\\-sim\\fR \\fISECONDS\\fR [\\fIOPTION\\fR]...\n")
    w(".SH DESCRIPTION\n" + esc(
        "sdr-cmts is a cable modem termination system for a closed coax lab. It drives one or more "
        "Ettus USRP B210 (UHD) or a Nuand bladeRF 2.0 micro (libbladeRF) and brings unmodified "
        "DOCSIS cable modems online: EuroDOCSIS downstream (J.83 annex A, 64/256-QAM, up to 6 bonded "
        "channels per B210), TDMA/ATDMA upstream up to 64-QAM with DOCSIS 3.0 upstream bonding, "
        "ranging, registration, an Ethernet bridge to the TAP interface cmts0, and, with --all-in-one, "
        "DHCP, TFTP and time of day for the modems and the devices behind them. EuroDOCSIS only: "
        "North American DOCSIS (J.83 annex B, 6 MHz channels) is not implemented.") + "\n")
    w(".PP\n" + esc("The console and the logs are in French. Ctrl+C stops it cleanly (power the modem "
                     "off first). Every run on hardware writes captures/cmts_DATE_TIME.log in the current "
                     "directory.") + "\n")
    w(".SH SAFETY\n" + esc("Closed coax only. Never connect it to an operator's cable network: it transmits. "
                           "Never connect the radio's receive input without attenuation: a modem transmits up "
                           "to +60 dBmV.") + "\n")
    for title, acts in options_of(sdr_cmts.parser()).items():
        w(f'.SH "{esc(title.upper())} OPTIONS"\n' + "".join(option(a) for a in acts))
    w('.SH "PROVISIONING OPTIONS"\n' + esc(
        "Options of the provisioning server (DHCP, TFTP, ToD) started by --all-in-one; give them with "
        "--prov-args \"...\" or in the [provisioning] section of a bench file.") + "\n")
    for acts in options_of(provision.parser(), skip=("iface", "secret")).values():
        w("".join(option(a) for a in acts))
    w('.SH "BENCH FILES"\n' + esc(
        "--config BENCH.toml reads the options from a TOML file. The keys are the long option names "
        "with underscores (--ds-channels becomes ds_channels); sections only group them, except "
        "[provisioning], whose keys are the provisioning options. true and false stand for --x and "
        "--no-x, lists are joined with commas. Options on the command line win over the file. A "
        "misspelt key is refused with the nearest names.") + "\n")
    w(".PP\n.nf\n" + esc('[downstream]\nfreq = 306e6\nds_channels = 6\n\n[upstream]\nus_channels = 2\n\n'
                         '[provisioning]\ndns = "1.1.1.1"') + "\n.fi\n")
    w('.SH "STATUS PAGE"\n' + esc(
        "While it runs, http://127.0.0.1:8090/ (--status-http) shows the channels, the radios "
        "(underflows, late packets, transmit margin), the upstream receivers, the modems and what "
        "they announce (DOCSIS version, receivers, transmitters, vendor, model, embedded components, "
        "the devices behind them), the throughput and the recent alerts. Its Configuration form "
        "rewrites the --config file and restarts the CMTS; by default only from the machine itself "
        "(--status-edit). /status.json gives the same data, /source.tar.gz the program's source.") + "\n")
    w(".SH ENVIRONMENT\n")
    for var, what in (
            ("SDR_CMTS_SDR_SIM=1", "a simulated radio instead of the SDR: it takes the transmit blocks and gives "
             "receive samples (low noise) at the pace of real time, with no hardware. The whole chain runs (MAC, "
             "radio and upstream processes, bridge, status page): to try the CMTS, or to size a host before "
             "buying an SDR (the TX margin in the status lines must stay above ~5 ms). No modem answers, so "
             "the upstream load is not that of a real modem."),
            ("SDR_CMTS_SIM_UNPLUG_S=N", "with the simulated radio: N seconds after start the radio process "
             "aborts, as when an SDR is unplugged; the CMTS stops with exit code 3."),
            ("SDR_CMTS_BLADERF_META_FORMAT=N", "the libbladeRF enum value of SC16_Q11_META, if the library's "
             "version is not the one expected (see BLADERF.md)."),
            ("SDR_CMTS_US_FAST=0", "every upstream burst decoded by the Python path (diagnostic; slower)."),
            ("HOME", "UHD reads ~/.config/uhd.conf (the FPGA image of a B210 clone); the service sets it to /root.")):
        w(f".TP\n.B {esc(var)}\n{esc(what)}\n")
    w(".PP\n" + esc("Other SDR_CMTS_* variables are diagnostics described where the code reads them.") + "\n")
    w(".SH \"EXIT STATUS\"\n")
    for code, what in (("0", "stopped normally (Ctrl+C, systemctl stop)"),
                       ("1", "could not start (no SDR found, TAP interface, bench file refused...)"),
                       ("2", "command line options refused"),
                       ("3", "the radio was lost while running (SDR unplugged): the service starts it again 15 s later")):
        w(f".TP\n.B {code}\n{esc(what)}\n")
    w(".SH FILES\n")
    for path, what in (("/etc/sdr-cmts/sdr-cmts.toml", "the bench of the sdr-cmts service"),
                       ("/usr/share/sdr-cmts/configs/", "example bench files: b210.toml, 2xb210.toml, bladerf.toml"),
                       ("/var/lib/sdr-cmts/captures/", "logs and captures of the service"),
                       ("/etc/security/limits.d/99-sdr-cmts.conf", "real-time priority for the usrp group"),
                       ("/usr/lib/sdr-cmts/tools/bladerf_check.py", "checks a bladeRF before use"),
                       ("/usr/share/doc/sdr-cmts/", "README, installation guide, journal and test book")):
        w(f".TP\n.I {esc(path)}\n{esc(what)}\n")
    w(".SH EXAMPLES\n")
    for cmd, what in (("sudo sdr-cmts --config /etc/sdr-cmts/sdr-cmts.toml", "the installed bench, in the foreground"),
                      ("sudo systemctl enable --now sdr-cmts", "the same bench as a service; journalctl -u sdr-cmts -f shows it"),
                      ("sdr-cmts --config /usr/share/sdr-cmts/configs/b210.toml --ds-channels 4",
                       "an example bench with one option changed"),
                      ("sdr-cmts --sim 3", "a simulated modem, no hardware")):
        w(f".TP\n.B {esc(cmd)}\n{esc(what)}\n")
    w('.SH "SEE ALSO"\n.BR uhd_find_devices (1),\n.BR uhd_usrp_probe (1),\n.BR bladeRF\\-cli (1),\n'
      ".BR systemctl (1),\n.BR journalctl (1)\n.PP\n"
      + esc("/usr/share/doc/sdr-cmts/docs/INSTALL.md, BLADERF.md") + "\n")
    w(".SH LICENSE\n" + esc("GNU Affero General Public License v3.0 only, or a commercial licence from the "
                            "copyright holder: see /usr/share/doc/sdr-cmts/COMMERCIAL.md. DOCSIS and EuroDOCSIS "
                            "are trademarks of CableLabs; this project is not affiliated with or endorsed by "
                            "CableLabs.") + "\n")
    w(".SH AUTHOR\nBILLY Rémi\n")


if __name__ == "__main__":
    main()
