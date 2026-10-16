"""ANSI colours for the mini-CMTS console. Each line is coloured from its content, so the
modules that produce the messages (ranging.py, upstream.py) stay unchanged:

  rouge gras   !! problèmes, modem oublié, compteurs U/L/O non nuls
  vert gras    RANGING TERMINÉ, modem "ranged"
  vert         RNG-RSP success, priorité temps réel
  jaune        RNG-RSP continue, modem "ranging", RNG-REQ ignorées
  cyan         RNG-REQ reçues (IM / SM en gras)
  magenta      REQ (demandes de bande) et autres trames montantes
  rouge        rafales non décodées
  gris         [US-BRUT], [SIM-CM]
  bleu gras    heure de la ligne d'état
"""
import os
import re
import sys

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = (f"\033[{n}m" for n in (31, 32, 33, 34, 35, 36))

enabled = False


def setup(mode="auto"):
    """auto: colours only on a terminal (not through a pipe or tee), and not when NO_COLOR is set."""
    global enabled
    if mode == "auto":
        enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    else:
        enabled = mode == "always"
    return enabled


def c(text, *codes):
    return "".join(codes) + text + RESET if enabled and codes else text


# (pattern that selects the line, colour of the whole line), first match wins
LINE_RULES = [
    (re.compile(r"RANGING TERMINÉ|MODEM EN LIGNE|^priorité temps réel"), BOLD + GREEN),
    (re.compile(r"^CPU \d|^gouverneur CPU|^calcul DSP en C"), GREEN),
    (re.compile(r"modem oublié"), BOLD + RED),
    (re.compile(r"^!!"), BOLD + RED),
    (re.compile(r"^\[(TX|RX)\] !!"), BOLD + RED),
    (re.compile(r"RNG-RSP success"), GREEN),
    (re.compile(r"RNG-RSP continue"), YELLOW),
    (re.compile(r"non décodée"), RED),
    (re.compile(r"\[US\] RNG-REQ .*ignoré"), YELLOW),
    (re.compile(r"\[US\] RNG-REQ"), CYAN),
    (re.compile(r"\[US\] (REQ|trame)"), MAGENTA),
    (re.compile(r"^\[REG\]"), BOLD + CYAN),
    (re.compile(r"^\[(DHCP|TFTP|ToD|PROV)\]"), BLUE),
    (re.compile(r"^\[PONT\] ↑"), CYAN),
    (re.compile(r"^\[PONT\]"), BLUE),
    (re.compile(r"\[US-BRUT\]|\[SIM-CM\]|^   "), DIM),
    (re.compile(r"^(master clock|émission;|arrêt;|simulation:|fin:)"), BOLD),
]

COUNTER = re.compile(r"\b([ULO]) (\d+)\b")
STATE = re.compile(r"\b(online|registering|ranged|ranging|aucun modem)\b")
STATE_COL = {"online": BOLD + GREEN, "registering": YELLOW, "ranged": GREEN, "ranging": YELLOW, "aucun modem": DIM}


def _inline(line, base):
    """Highlights inside a line; after each one the line's own colour is restored."""
    back = RESET + base

    def sub(rx, fn, s):
        return rx.sub(lambda m: fn(m) + back, s)

    if line.startswith("[") and re.match(r"\[\d\d:\d\d:\d\d\]", line):          # status line
        line = BOLD + BLUE + line[:10] + back + line[10:]
        line = sub(COUNTER, lambda m: (BOLD + RED if m[2] != "0" else GREEN) + m[0], line)
        line = sub(STATE, lambda m: STATE_COL[m[1]] + m[0], line)
    if "[US] RNG-REQ" in line:
        line = sub(re.compile(r" (IM|SM) "), lambda m: BOLD + " " + m[1] + " ", line)
    return sub(re.compile(r"!!.*$"), lambda m: BOLD + RED + m[0], line)      # appended warnings


def colorize(text):
    if not enabled:
        return text
    out = []
    for line in text.split("\n"):
        base = next((col for rx, col in LINE_RULES if rx.search(line)), "")
        body = _inline(line, base) if line else line
        out.append(base + body + RESET if line else line)
    return "\n".join(out)


# ---------------------------------------------------------------- verbosity (-v, -vv, -vvv)
# The console shows lines up to the chosen level; the log file always gets every line.
#   0  essentials: start-up, status line, warnings (!!), modem life (ranging done, forgotten,
#      registration, online), provisioning (DHCP/TFTP/ToD), TX/RX stalls
#   1  -v    ranging exchanges (RNG-REQ/RNG-RSP), bandwidth requests, undecoded bursts, the
#            details under a line (REG-REQ TLVs, watchdog snapshot, config file)
#   2  -vv   every frame through the bridge, simulated modem
#   3  -vvv  raw energy scan, background noise of the bridge (IPv6, mDNS, IGMP)
LEVEL_RULES = [                     # (pattern, level), first match wins
    (re.compile(r"!!"), 0),
    (re.compile(r"^\[US-BRUT\]"), 3),
    (re.compile(r"^\[PONT\] [↑↓] (IPv6|UDP \S+:5353|IPv4 proto 2 )"), 3),
    (re.compile(r"^\[PONT\] [↑↓]"), 2),
    (re.compile(r"^\[SIM-CM\]"), 2),
    (re.compile(r"^\[US\] (RNG-REQ|REQ|rafale|trame)|^\s+RNG-RSP|^\s+début du 1er bloc"), 1),
]
verbosity = 0
_last_level = 0


def set_verbosity(v):
    global verbosity
    verbosity = v


def level_of(line):
    """Level of one console line; indented lines continue the previous one (at least -v)."""
    global _last_level
    for rx, lvl in LEVEL_RULES:
        if rx.search(line):
            if not line[:1].isspace():
                _last_level = lvl
            return lvl
    if line[:1].isspace():
        return max(_last_level, 1)
    _last_level = 0
    return 0


_logfile = None
listeners = []          # fn(text) called with every log() line (e.g. the status page's alerts)


def open_log(path):
    """Also write every log() line, without colours, to this file (flushed line by line)."""
    global _logfile
    _logfile = open(path, "a", buffering=1)


def log(*args, console=True, **kw):
    """print() replacement: colours, verbosity filter on the console, every line to the log file.
    console=False: log file only (e.g. a chatty source over its console rate limit)."""
    kw.setdefault("flush", True)
    text = " ".join(str(x) for x in args)
    if _logfile:
        _logfile.write(text + kw.get("end", "\n"))
    for fn in listeners:
        fn(text)
    if not console:
        return
    shown = [ln for ln in text.split("\n") if level_of(ln) <= verbosity]
    if shown:
        print(colorize("\n".join(shown)), **kw)
