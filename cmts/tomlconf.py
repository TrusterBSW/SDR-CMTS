# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Bench descriptions in TOML (sdr_cmts.py --config bench.toml).

A TOML file groups the options in sections for reading; the keys are the option names of
sdr_cmts.py (--ds-channels -> ds_channels or ds-channels), the sections are free (radio,
downstream, upstream, host...), except [provisioning], whose keys are the options of provision.py
and become --prov-args. Values: numbers, strings, true / false (--x / --no-x), lists (joined by
commas). The options of the command line come after those of the file, so they win:

    [radio]
    ds_radios = 2
    radio_serials = ["000000367", "JN76WAJ"]
    clock_source = "internal"

    [downstream]
    ds_channels = 4

    [upstream]
    us_channels = 2
    us_target = -20

    [provisioning]
    dns = "1.1.1.1"

    sdr_cmts.py --config configs/2xb210.toml --us-target -18

An unknown key is refused with the nearest names (a typo would otherwise silently keep a default).
"""
import argparse
import difflib
import shlex

try:
    import tomllib
except ModuleNotFoundError:              # Python < 3.11
    tomllib = None

PROVISIONING = "provisioning"


def _options(ap):
    """dest -> argparse action, for the actions with an option string."""
    return {a.dest: a for a in ap._actions if a.option_strings and a.dest != "help"}


def _argv(key, val, opts, where):
    dest = key.replace("-", "_")
    act = opts.get(dest)
    if act is None:
        near = difflib.get_close_matches(dest, list(opts), n=3)
        raise ValueError(f"{where} : option inconnue « {key} »" + (f" (voulais-tu {', '.join(near)} ?)" if near else ""))
    if isinstance(act, argparse.BooleanOptionalAction):    # (option strings: --x, --no-x)
        if not isinstance(val, bool):
            raise ValueError(f"{where} : « {key} » attend true ou false")
        pos = [o for o in act.option_strings if not o.startswith("--no-")]
        neg = [o for o in act.option_strings if o.startswith("--no-")]
        return [pos[0] if val else neg[0]]
    opt = max(act.option_strings, key=len)
    if isinstance(act, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        if not isinstance(val, bool):
            raise ValueError(f"{where} : « {key} » attend true ou false")
        on = isinstance(act, argparse._StoreTrueAction)
        return [opt] if val == on else []
    if isinstance(val, bool):
        raise ValueError(f"{where} : « {key} » attend une valeur, pas true / false")
    if isinstance(val, list):
        val = ",".join(str(v) for v in val)
    return [f"{opt}={val}"]


def to_argv(path, ap, prov_ap=None):
    """The options of a TOML bench file as command line arguments for `ap` (sdr_cmts.py), the
    [provisioning] section as one --prov-args for `prov_ap` (provision.py)."""
    if tomllib is None:
        raise ValueError("--config : Python 3.11 ou plus (tomllib) ; sinon les fichiers @configs/*.args")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    opts = _options(ap)
    argv, prov = [], []
    for section, items in data.items():
        if not isinstance(items, dict):                       # a key outside any section
            items, section = {section: items}, ""
        where = f"{path} [{section}]" if section else str(path)
        if section == PROVISIONING:
            if prov_ap is None:
                raise ValueError(f"{where} : section sans programme de provisionnement")
            popts = _options(prov_ap)
            for key, val in items.items():
                prov += _argv(key, val, popts, where)
            continue
        for key, val in items.items():
            argv += _argv(key, val, opts, where)
    if prov:
        argv.append("--prov-args=" + shlex.join(prov))
    return argv


def split_config(argv):
    """(path of --config, the other arguments) from a command line; path None without one."""
    out, path, i = [], None, 0
    while i < len(argv):
        a = argv[i]
        if a == "--config" and i + 1 < len(argv):
            path, i = argv[i + 1], i + 2
            continue
        if a.startswith("--config="):
            path, i = a.split("=", 1)[1], i + 1
            continue
        out.append(a)
        i += 1
    return path, out


def toml_value(v):
    """A Python value as TOML: 306000000.0 -> 306e6, "x" -> "x", [a, b] -> ["a", "b"]."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer() and abs(v) >= 1e6 and (v / 1e6).is_integer():
            return f"{v / 1e6:g}e6"
        return repr(v) if isinstance(v, float) else str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def update(path, changes, sections):
    """Sets keys of a TOML bench file, keeping its layout and comments: a key already there gets its
    new value on its own line (the comment after it kept), a new one goes at the end of its section
    (sections: key -> section name; created at the end if missing). The file is checked to read back
    the same values before it replaces the old one, kept as <path>.bak."""
    import os
    import re
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    todo = dict(changes)
    section = ""
    for i, ln in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]", ln)
        if m:
            section = m.group(1).strip()
            continue
        m = re.match(r"(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(\"(?:[^\"\\\\]|\\\\.)*\"|\[[^\]]*\]|[^#\s]+)(.*)$", ln)
        if m:
            key = m.group(2).replace("-", "_")
            if key in todo:
                lines[i] = m.group(1) + m.group(2) + m.group(3) + toml_value(todo.pop(key)) + m.group(5)
    for key, val in todo.items():
        sec = sections.get(key, "host")
        head = next((i for i, ln in enumerate(lines) if re.match(rf"\s*\[{re.escape(sec)}\]", ln)), None)
        if head is None:
            lines += ["", f"[{sec}]", f"{key} = {toml_value(val)}"]
            continue
        end = next((j for j in range(head + 1, len(lines)) if re.match(r"\s*\[", lines[j])), len(lines))
        while end > head + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, f"{key} = {toml_value(val)}")
    text = "\n".join(lines)
    data = tomllib.loads(text)                   # still valid TOML, with the values asked for
    flat = {k.replace("-", "_"): v for sec in data.values() if isinstance(sec, dict) for k, v in sec.items()}
    flat.update({k.replace("-", "_"): v for k, v in data.items() if not isinstance(v, dict)})
    for k, v in changes.items():
        if flat.get(k) != v and not (isinstance(v, float) and abs(flat.get(k, 0) - v) < 1e-6 * max(1, abs(v))):
            raise ValueError(f"{k} : relu {flat.get(k)!r} au lieu de {v!r}")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        os.replace(path, f"{path}.bak")
    os.replace(tmp, path)
