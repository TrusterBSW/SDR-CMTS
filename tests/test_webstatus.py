# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Status page (cmts/webstatus.py, mini_cmts.py --status-http): the server answers the page and
the last published snapshot as valid JSON (NaN / infinities / numpy values made plain), alerts are
the "!!" log lines, the throughput comes from the interface's kernel counters, a taken port is
reported instead of stopping the CMTS; the snapshot builder of mini_cmts.py runs on the
simulated CMTS."""
import json
import os
import socket
import sys
import urllib.request

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import mini_cmts  # noqa: E402
from cmts import termcolor, webstatus  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


check("adresse : port seul, hôte:port, none",
      webstatus.parse_addr("8090") == ("127.0.0.1", 8090) and webstatus.parse_addr("0.0.0.0:81") == ("0.0.0.0", 81)
      and webstatus.parse_addr("none") is None and webstatus.parse_addr("[::1]:90") == ("::1", 90))

board = webstatus.StatusBoard(dict(sdr="b210", ds=[dict(dcid=1, freq=306e6)]))
srv = webstatus.StatusServer(board, "127.0.0.1", 0, log=lambda *a: None)
port = srv.address[1]
board.alert("[RADIO] tout va bien")
board.alert("[RADIO] !! émission en retard")
board.publish(dict(radios=[dict(name="radio", margin_ms=float("inf"), noise=float("nan"), x=np.float32(1.5))],
                   modems=[dict(mac="00:24:d1:c0:ff:ee", links={2: (0x2001, "online")})]), iface="lo")
board.publish(dict(radios=[], modems=[]), iface="lo")
base = f"http://127.0.0.1:{port}"
page = urllib.request.urlopen(base + "/").read().decode()
check("la page HTML", "<title>mini-cmts</title>" in page and "status.json" in page and "@VERSION@" not in page)
import io, tarfile  # noqa: E401,E402
src = tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(base + "/source.tar.gz").read()))
names = src.getnames()
check("code source servi (AGPL §13)", "mini-cmts/mini_cmts.py" in names and "mini-cmts/LICENSE" in names
      and "mini-cmts/cmts/webstatus.py" in names and not any(n.endswith(".so") for n in names), f"{len(names)} fichiers")
raw = urllib.request.urlopen(base + "/status.json").read().decode()
try:
    d = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    check("JSON strict (sans NaN ni Infinity)", True)
except ValueError as e:
    d = json.loads(raw)
    check("JSON strict (sans NaN ni Infinity)", False, str(e))
check("dernier instantané publié", d["status"]["modems"] == [] and d["info"]["sdr"] == "b210")
check("débit tiré des compteurs du noyau (lo)", d["status"]["rate"] is not None and len(d["history"]) == 1,
      f"{d['status']['rate']}")
check("alertes : les lignes « !! » seulement", [a["line"] for a in d["alerts"]] == ["[RADIO] !! émission en retard"])
try:
    urllib.request.urlopen(base + "/x")
    check("404 ailleurs", False)
except urllib.error.HTTPError as e:
    check("404 ailleurs", e.code == 404)

# port taken: mini_cmts reports it and goes on
s = socket.socket()
s.bind(("127.0.0.1", 0))
s.listen()
lines = []
termcolor.listeners.append(lines.append)
a = mini_cmts.parser().parse_args(["--status-http", f"127.0.0.1:{s.getsockname()[1]}"])
b = mini_cmts.start_status_page(a, {})
termcolor.listeners.remove(lines.append)
check("port déjà pris : signalé, pas d'arrêt", b is None and any("!! page d'état" in ln for ln in lines))
check("--status-http none : pas de page", mini_cmts.start_status_page(
    mini_cmts.parser().parse_args(["--status-http", "none"]), {}) is None)
s.close()
srv.close()

# the Configuration form (cmts/webconfig.py) on a copy of configs/b210.toml
import shutil  # noqa: E402
import tempfile  # noqa: E402
from cmts import tomlconf, webconfig  # noqa: E402
import provision  # noqa: E402

tmpd = tempfile.mkdtemp()
bench = os.path.join(tmpd, "banc.toml")
shutil.copy(os.path.join(os.path.dirname(__file__), "..", "configs", "b210.toml"), bench)
a = mini_cmts.parser().parse_args(tomlconf.to_argv(bench, mini_cmts.parser(), provision.parser()) + ["--us-gain", "20"])
a.config_path, a.cli_locked = bench, {"us_gain"}
asked = []
wc = webconfig.WebConfig(a, bench, a.cli_locked, parse=lambda argv: mini_cmts.parser().parse_args(argv),
                         channels=lambda ns: ([f for _, f in mini_cmts.ds_channels(ns)], [f for _, f in mini_cmts.us_channels(ns)]),
                         to_argv=lambda p: tomlconf.to_argv(p, mini_cmts.parser(), provision.parser()),
                         restart=asked.append)
board = webstatus.StatusBoard({})
srv = webstatus.StatusServer(board, "127.0.0.1", 0, log=lambda *x: None, config=wc)
base = f"http://127.0.0.1:{srv.address[1]}"


def post(body, origin=None):
    req = urllib.request.Request(base + "/config", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


desc = json.loads(urllib.request.urlopen(base + "/config.json").read())
f = {x["key"]: x for x in desc["fields"]}
check("formulaire : valeurs du banc, option de la ligne de commande grisée",
      desc["editable"] and f["ds_channels"]["value"] == 6 and f["freq"]["value"] == 306e6 and f["us_gain"]["locked"])
code, d = post({"freq": "860"})
check("refusé : canaux hors de la bande 108-862 MHz", code == 422 and not asked, d["messages"][0][:70])
code, d = post({"ds_channels": "7"})
check("refusé : 7 DS sur un B210", code == 422 and "6 au plus" in d["messages"][0], d["messages"][0][:70])
code, d = post({"us_mode": "tdma"})
check("refusé : 64-QAM montant en TDMA", code == 422 and any("ATDMA" in x for x in d["messages"]))
code, d = post({"us_gain": "10"})
check("refusé : option donnée sur la ligne de commande", code == 422)
code, d = post({"qam": "64"}, origin="http://evil.example")
check("refusé : requête d'une autre origine", code == 403 and not asked)
code, d = post({"ds_channels": "4", "freq": "314", "qam": "64", "us_mod": "16qam"})
b = mini_cmts.parser().parse_args(tomlconf.to_argv(bench, mini_cmts.parser(), provision.parser()))
check("accepté : fichier écrit (commentaires gardés, .bak), redémarrage demandé",
      code == 200 and asked and (b.ds_channels, b.freq, b.qam, b.us_mod) == (4, 314e6, 64, "16qam")
      and os.path.exists(bench + ".bak") and "# primary channel" in open(bench).read(), d["messages"][0][:60])
wc2 = webconfig.WebConfig(a, None, (), None, None, None, None)
check("sans --config : lecture seule", not wc2.describe()["editable"] and not wc2.apply({"qam": "64"})[0])
check("--status-edit auto : lecture seule depuis le réseau, modifiable depuis la machine",
      not wc.describe(local=False)["editable"] and not wc.apply({"qam": "256"}, local=False)[0]
      and wc.describe(local=True)["editable"])
wc.edit = "off"
check("--status-edit off : lecture seule partout", not wc.describe(local=True)["editable"])
srv.close()
shutil.rmtree(tmpd)

# the snapshot builder on the simulated CMTS (no hardware)
a = mini_cmts.parser().parse_args(["--sim", "1", "--us-process", "--no-radio-process", "--tap", "none"])
mux, core, rx, tb, mcr, decim = mini_cmts.build(a, lambda *x, **k: None)
try:
    snap = mini_cmts.web_snapshot(a, mux, core, rx, [], None, mini_cmts.ds_fill(mux, {}), {})
    info = mini_cmts.web_info(a, mini_cmts.ds_channels(a))
    body = json.dumps(webstatus._finite(dict(info=info, status=snap)), default=webstatus._jsonable, allow_nan=False)
    check("instantané de mini_cmts : canaux, montants, modems, en JSON",
          len(info["ds"]) == a.ds_channels and len(snap["upstream"]) == a.us_channels and "modems" in snap,
          f"{len(body)} octets")
finally:
    for r in getattr(rx, "all_channels", None) or [rx]:
        if hasattr(r, "stop"):
            r.stop()
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
