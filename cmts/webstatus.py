# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Status page of the CMTS over HTTP (sdr_cmts.py --status-http, 127.0.0.1:8090 by default).

The status loop of sdr_cmts.py publishes a snapshot every 5 s (a dict: channels, radios,
upstream receivers, bridge, modems); this module serves it, without touching the CMTS itself:

    GET /             the page (HTML + a script polling /status.json)
    GET /status.json  the last snapshot, the throughput history and the recent alerts

The server runs in a daemon thread of the MAC process and only reads what was published: a
request never waits on the CMTS, and a slow client never holds the GIL for long (the JSON is
made once per snapshot). Bound to the loopback by default: the page shows the modems' MAC
addresses, give another address (0.0.0.0:8090) only on a closed lab network.
"""
import collections
import glob
import io
import json
import os
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # sdr_cmts.py and cmts/ live here
SOURCE_GLOBS = ("*.py", "VERSION", "LICENSE", "COMMERCIAL.md", "cmts/*.py", "cmts/*.c", "tools/*.py", "tools/*.sh",
                "configs/*.toml", "packaging/*", "packaging/debian/*")
HISTORY = 360                       # snapshots kept for the throughput charts (30 min at 5 s)
ALERTS = 50                         # last alert lines ("!!" in the log)


def parse_addr(text, default_host="127.0.0.1"):
    """'8090' or 'host:8090' -> (host, port); 'none' / '' / '0' -> None."""
    text = (text or "").strip()
    if text.lower() in ("", "none", "off", "0"):
        return None
    host, _, port = text.rpartition(":")
    return (host.strip("[]") or default_host), int(port)


def iface_bytes(name):
    """(rx_bytes, tx_bytes) of a network interface from the kernel's counters, None if absent.
    For the TAP: rx = frames from the modems (upstream), tx = frames to them (downstream)."""
    try:
        out = []
        for k in ("rx_bytes", "tx_bytes"):
            with open(f"/sys/class/net/{name}/statistics/{k}") as f:
                out.append(int(f.read()))
        return tuple(out)
    except (OSError, ValueError):
        return None


class StatusBoard:
    """What the page shows: the last snapshot, a throughput history, the recent alerts."""

    def __init__(self, info=None):
        self.info = dict(info or {})            # fixed: channels, radios, options
        self.t0 = time.time()
        self.history = collections.deque(maxlen=HISTORY)
        self.alerts = collections.deque(maxlen=ALERTS)
        self._lock = threading.Lock()
        self._json = b"{}"
        self._snap = {}
        self._bytes = None                      # (time, rx, tx) of the last reading

    def alert(self, line):
        """A log line; kept if it is an alert ("!!")."""
        if "!!" in line:
            with self._lock:
                self.alerts.append((time.time(), line.strip()))

    def publish(self, snap, iface=None):
        """A new snapshot (from the status loop); iface: the TAP, for the throughput."""
        now = time.time()
        rate = None
        b = iface_bytes(iface) if iface else None
        if b is not None:
            if self._bytes is not None and now > self._bytes[0]:
                dt = now - self._bytes[0]
                rate = dict(up_mbps=(b[0] - self._bytes[1]) * 8 / dt / 1e6,
                            down_mbps=(b[1] - self._bytes[2]) * 8 / dt / 1e6)
            self._bytes = (now, b[0], b[1])
        snap = dict(snap, time=now, uptime_s=now - self.t0, rate=rate)
        with self._lock:
            if rate is not None:
                self.history.append((round(now, 1), round(rate["down_mbps"], 2), round(rate["up_mbps"], 2)))
            self._snap = snap
            body = dict(info=self.info, status=snap, history=list(self.history),
                        alerts=[dict(time=t, line=s) for t, s in self.alerts])
            self._json = json.dumps(_finite(body), default=_jsonable, ensure_ascii=False).encode()

    def json(self):
        with self._lock:
            return self._json


_source = []


def source_archive():
    """The program's own source as a .tar.gz, built once (GNU AGPL section 13: the users of a
    modified version over a network must be offered its source; the page links here)."""
    if not _source:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for pat in SOURCE_GLOBS:
                for f in sorted(glob.glob(os.path.join(ROOT, pat))):
                    if os.path.isfile(f) and not f.endswith((".so", ".pyc")):
                        tar.add(f, arcname="sdr-cmts/" + os.path.relpath(f, ROOT))
        _source.append(buf.getvalue())
    return _source[0]


def version():
    try:
        with open(os.path.join(ROOT, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return ""


def _finite(x):
    """NaN and infinities -> None (JSON has neither; the page's JSON.parse would refuse them)."""
    if isinstance(x, float):
        return x if x == x and abs(x) != float("inf") else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_finite(v) for v in x]
    if hasattr(x, "item") and hasattr(x, "dtype"):          # numpy scalar
        return _finite(x.item())
    return x


def _jsonable(x):
    if hasattr(x, "item"):                      # numpy scalars
        return x.item()
    if isinstance(x, (set, tuple)):
        return list(x)
    if isinstance(x, bytes):
        return x.hex()
    return str(x)


class StatusServer:
    """config: a cmts.webconfig.WebConfig (the Configuration form), None for none."""

    def __init__(self, board, host="127.0.0.1", port=8090, log=print, config=None):
        self.board = board
        page = PAGE.replace("@VERSION@", version()).encode()

        class Handler(BaseHTTPRequestHandler):
            def _send(self, body, ctype, code=200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _local(self):
                """The request comes from this machine (its browser, or an SSH tunnel)."""
                return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

            def _json(self, obj, code=200):
                self._send(json.dumps(_finite(obj), default=_jsonable, ensure_ascii=False).encode(),
                           "application/json", code)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send(page, "text/html; charset=utf-8")
                elif path == "/status.json":
                    self._send(board.json(), "application/json")
                elif path == "/source.tar.gz":
                    self.send_response(200)
                    body = source_archive()
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Disposition", 'attachment; filename="sdr-cmts-source.tar.gz"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/config.json":
                    self._json(config.describe(self._local()) if config else dict(fields=[], editable=False,
                                                                                  why="pas de formulaire"))
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path.split("?", 1)[0] != "/config" or config is None:
                    self.send_error(404)
                    return
                # a form posted from another site (the browser of someone on this page's network)
                # must not reconfigure the radio: same origin only
                origin = self.headers.get("Origin")
                if origin and origin.split("://", 1)[-1] != self.headers.get("Host", ""):
                    self._json(dict(ok=False, messages=["requête d'une autre origine refusée"]), 403)
                    return
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    changes = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                    if not isinstance(changes, dict):
                        raise ValueError
                except (ValueError, json.JSONDecodeError):
                    self._json(dict(ok=False, messages=["requête illisible"]), 400)
                    return
                ok, msgs = config.apply(changes, self._local())
                self._json(dict(ok=ok, messages=msgs), 200 if ok else 422)

            def log_message(self, *args):       # no line per request in the CMTS log
                pass

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.address = self.httpd.server_address[:2]
        threading.Thread(target=self.httpd.serve_forever, kwargs=dict(poll_interval=0.5),
                         name="status-http", daemon=True).start()
        log(f"page d'état : http://{host if host not in ('0.0.0.0', '::') else '<cet hôte>'}:{self.address[1]}/")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sdr-cmts</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#677084; --line:#dde1e8;
        --ok:#1f8a4c; --warn:#b7791f; --bad:#c53030; --ds:#2b6cb0; --us:#b83280; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14171d; --card:#1c2029; --fg:#e4e7ee; --muted:#9099ab; --line:#2c3240;
          --ok:#48bb78; --warn:#ecc94b; --bad:#fc8181; --ds:#63b3ed; --us:#f687b3; } }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif }
header { padding:14px 16px; display:flex; gap:16px; align-items:baseline; flex-wrap:wrap;
         border-bottom:1px solid var(--line); background:var(--card) }
header h1 { font-size:18px; margin:0 }
header .muted { font-size:13px }
main { padding:16px; display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)) }
section { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; min-width:0 }
section.wide { grid-column:1 / -1 }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:0 0 8px }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums }
th, td { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line); white-space:nowrap }
th { color:var(--muted); font-weight:500; font-size:12px }
.scroll { overflow-x:auto }
.muted { color:var(--muted) }
.ok { color:var(--ok) } .warn { color:var(--warn) } .bad { color:var(--bad) }
.big { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums }
.kpis { display:flex; gap:24px; flex-wrap:wrap }
.kpis div span { display:block; font-size:12px; color:var(--muted) }
svg { width:100%; height:140px; display:block }
.alerts { font:12px/1.5 ui-monospace, monospace; max-height:220px; overflow:auto; white-space:pre-wrap }
button { font:inherit; font-size:12px; padding:3px 9px; border:1px solid var(--line); border-radius:5px;
         background:var(--bg); color:var(--fg); cursor:pointer }
button:hover { border-color:var(--muted) }
button.primary { background:var(--ds); border-color:var(--ds); color:#fff }
.details td { white-space:normal; background:var(--bg) }
.kv { display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:2px 18px; margin:6px 0 10px }
.kv div { display:flex; justify-content:space-between; gap:10px; border-bottom:1px dotted var(--line); padding:2px 0 }
.kv span:first-child { color:var(--muted) }
h3 { font-size:12px; margin:10px 0 2px; color:var(--muted); font-weight:600 }
form .grp { margin-bottom:12px }
form label { display:grid; grid-template-columns:minmax(150px, 280px) 1fr; gap:10px; align-items:center; padding:3px 0 }
form input, form select { font:inherit; padding:3px 6px; border:1px solid var(--line); border-radius:4px;
         background:var(--card); color:var(--fg); width:100%; max-width:220px }
.msg { margin-top:8px; white-space:pre-wrap }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:middle }
</style></head>
<body>
<header><h1>sdr-cmts</h1><span id="sum" class="muted">…</span><span id="age" class="muted"></span></header>
<main>
<section><h2>Débit (pont TAP)</h2>
  <div class="kpis"><div><span>Descendant</span><b class="big" id="dn" style="color:var(--ds)">–</b> Mbit/s</div>
  <div><span>Montant</span><b class="big" id="up" style="color:var(--us)">–</b> Mbit/s</div>
  <div><span>Modems en ligne</span><b class="big" id="nmod">–</b></div></div>
  <svg id="chart" viewBox="0 0 600 140" preserveAspectRatio="none"></svg>
  <div class="muted" style="font-size:12px">30 dernières minutes, <span style="color:var(--ds)">descendant</span> et <span style="color:var(--us)">montant</span></div>
</section>
<section><h2>Radios</h2><div class="scroll"><table id="radios"></table></div></section>
<section><h2>Canaux descendants</h2><div class="scroll"><table id="ds"></table></div><div id="dsq" class="muted" style="margin-top:6px"></div></section>
<section><h2>Canaux montants</h2><div class="scroll"><table id="us"></table></div></section>
<section class="wide"><h2>Modems</h2><div class="scroll"><table id="modems"></table></div></section>
<section class="wide"><h2>Alertes récentes</h2><div id="alerts" class="alerts muted">aucune</div></section>
<section class="wide"><h2>Configuration <button id="cfgbtn" type="button">afficher</button></h2>
  <div id="cfg" hidden><p id="cfgwhy" class="muted"></p><form id="cfgform"></form>
  <div><button id="cfgsave" class="primary" type="button">Enregistrer et redémarrer le CMTS</button>
  <span class="muted">le modem se réenregistre en une minute environ</span></div>
  <div id="cfgmsg" class="msg"></div></div></section>
</main>
<footer class="muted" style="padding:0 16px 16px;font-size:12px">sdr-cmts @VERSION@ · GNU AGPL v3.0
  (ou licence commerciale) · <a href="source.tar.gz" style="color:inherit">code source</a> ·
  <a href="https://github.com/TrusterBSW/SDR-CMTS" style="color:inherit">projet</a></footer>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const f = (x, d=1) => (x === null || x === undefined || Number.isNaN(x)) ? '–' : Number(x).toFixed(d);
const mhz = hz => f(hz / 1e6, 3);
const tbl = (el, head, rows) => { $(el).innerHTML = '<tr>' + head.map(h => `<th>${h}</th>`).join('') + '</tr>' +
  (rows.length ? rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('')
               : `<tr><td colspan="${head.length}" class="muted">–</td></tr>`); };
const cls = (v, warn, bad, up=true) => v === null || v === undefined ? '' :
  (up ? (v >= bad ? 'bad' : v >= warn ? 'warn' : 'ok') : (v <= bad ? 'bad' : v <= warn ? 'warn' : 'ok'));
const dur = s => s < 120 ? `${s|0} s` : s < 7200 ? `${(s/60)|0} min` : `${(s/3600).toFixed(1)} h`;
const stateCls = s => s === 'online' ? 'ok' : (s === 'ranging' ? 'warn' : '');

function chart(hist) {
  const svg = $('chart'), W = 600, H = 140;
  if (hist.length < 2) { svg.innerHTML = ''; return; }
  const t1 = hist[hist.length - 1][0], t0 = t1 - 1800;
  const mx = Math.max(10, ...hist.map(h => Math.max(h[1], h[2]))) * 1.1;
  const pts = k => hist.filter(h => h[0] >= t0).map(h =>
    `${((h[0] - t0) / 1800 * W).toFixed(1)},${(H - h[k] / mx * (H - 4)).toFixed(1)}`).join(' ');
  let g = '';
  for (let v = 0; v <= mx; v += mx > 200 ? 100 : mx > 60 ? 25 : 10) {
    const y = (H - v / mx * (H - 4)).toFixed(1);
    g += `<line x1="0" x2="${W}" y1="${y}" y2="${y}" stroke="var(--line)" stroke-width="1"/>` +
         `<text x="4" y="${y - 2}" font-size="10" fill="var(--muted)">${v}</text>`;
  }
  svg.innerHTML = g + `<polyline points="${pts(1)}" fill="none" stroke="var(--ds)" stroke-width="2" vector-effect="non-scaling-stroke"/>` +
                      `<polyline points="${pts(2)}" fill="none" stroke="var(--us)" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
}

const open = new Set();
let last = null;
const kv = obj => '<div class="kv">' + Object.entries(obj).map(([k, v]) =>
  `<div><span>${esc(k)}</span><span>${esc(Array.isArray(v) ? v.join(', ') : v)}</span></div>`).join('') + '</div>';
function details(m) {
  const a = m.announced, devs = Object.values(m.devices || {});
  let h = '';
  const cm = devs.find(d => d.MAC === m.mac);
  if (cm) {
    const id = {};
    for (const k of ['fabricant', 'modèle', 'numéro de série', 'version matérielle', 'version logicielle',
                     'version du boot', 'OUI', 'type', 'composants', 'composants (détail)', 'classe', 'adresse demandée'])
      if (cm[k] !== undefined) id[k] = cm[k];
    h += '<h3>Identité (DHCP du modem)</h3>' + kv(id);
  }
  if (a) {
    h += `<h3>Capacités annoncées (${esc(a.message)})</h3>` + kv((a['capacités'] || {})['résumé'] || {});
    if (a['identifiant fabricant (OUI)']) h += kv({'identifiant fabricant (OUI)': a['identifiant fabricant (OUI)']});
    if (a['profils de réception']) h += '<h3>Profils de réception (RCP)</h3>' + kv(Object.fromEntries(
      a['profils de réception'].map((r, i) => [`RCP ${i + 1}`, r])));
    const all = ((a['capacités'] || {}).tout || []);
    if (all.length) h += `<details><summary class="muted">les ${all.length} capacités brutes (TLV 5)</summary>` +
      kv(Object.fromEntries(all)) + '</details>';
  } else h += '<p class="muted">Capacités : pas encore de REG-REQ vu depuis le démarrage du CMTS (au prochain enregistrement du modem).</p>';
  const others = devs.filter(d => d.MAC !== m.mac);
  h += '<h3>Équipements derrière le modem (DHCP)</h3>' + (others.length ? '<div class="scroll"><table><tr>' +
    ['Nature', 'MAC', 'Classe (option 60)', 'Nom', 'Fabricant / modèle', 'Adresse demandée'].map(x => `<th>${x}</th>`).join('') + '</tr>' +
    others.map(d => '<tr>' + [d.nature, d.MAC, d.classe, d.nom || '–', [d['fabricant'], d['modèle']].filter(Boolean).join(' ') || '–',
      d['adresse demandée'] || '–'].map(x => `<td>${esc(x)}</td>`).join('') + '</tr>').join('') + '</table></div>'
    : '<p class="muted">aucun vu (ils apparaissent à leur prochaine requête DHCP)</p>');
  return h;
}
$('modems').addEventListener('click', e => {
  const mac = e.target.dataset && e.target.dataset.mac;
  if (!mac) return;
  open.has(mac) ? open.delete(mac) : open.add(mac);
  if (last) render(last);
});

let cfg = null;
async function loadCfg() {
  cfg = await (await fetch('config.json', {cache: 'no-store'})).json();
  $('cfgwhy').textContent = cfg.editable ? `fichier de banc : ${cfg.path}` + (cfg.locked_note ? ` · grisé : ${cfg.locked_note}` : '')
                                         : (cfg.why || 'lecture seule');
  const groups = {radio: 'Radio', downstream: 'Voie descendante', upstream: 'Voie montante'};
  let h = '';
  for (const [g, title] of Object.entries(groups)) {
    h += `<div class="grp"><h3>${title}</h3>`;
    for (const fl of cfg.fields.filter(x => x.section === g)) {
      const dis = (!cfg.editable || fl.locked) ? 'disabled' : '';
      let input;
      if (fl.kind === 'choice')
        input = `<select name="${fl.key}" ${dis}>` + fl.choices.map(([v, t]) =>
          `<option value="${esc(v)}" ${String(v) === String(fl.value) ? 'selected' : ''}>${esc(t)}</option>`).join('') + '</select>';
      else {
        const v = fl.value === null || fl.value === undefined ? '' : (fl.kind === 'mhz' ? +(fl.value / 1e6).toFixed(4) : fl.value);
        const sc = fl.kind === 'mhz' ? 1e6 : 1;
        input = `<input name="${fl.key}" type="number" step="any" value="${esc(v)}" ${dis}` +
          (fl.min !== undefined ? ` min="${fl.min / sc}"` : '') + (fl.max !== undefined ? ` max="${fl.max / sc}"` : '') +
          (fl.empty ? ` placeholder="${esc(fl.empty)}"` : '') + '>';
      }
      h += `<label><span>${esc(fl.label)}</span>${input}</label>`;
    }
    h += '</div>';
  }
  $('cfgform').innerHTML = h;
  $('cfgsave').disabled = !cfg.editable;
}
$('cfgbtn').addEventListener('click', async () => {
  const box = $('cfg');
  box.hidden = !box.hidden;
  $('cfgbtn').textContent = box.hidden ? 'afficher' : 'masquer';
  if (!box.hidden) await loadCfg();
});
$('cfgsave').addEventListener('click', async () => {
  const changes = {};
  for (const el of $('cfgform').elements) {
    if (el.disabled) continue;
    const fl = cfg.fields.find(x => x.key === el.name);
    const cur = fl.value === null || fl.value === undefined ? '' :
      String(fl.kind === 'mhz' ? +(fl.value / 1e6).toFixed(4) : fl.value);
    if (el.value !== cur) changes[el.name] = el.value;
  }
  if (!Object.keys(changes).length) { $('cfgmsg').textContent = 'rien n\'a changé'; return; }
  if (!confirm('Enregistrer ' + Object.entries(changes).map(([k, v]) => `${k} = ${v}`).join(', ') +
               ' et redémarrer le CMTS ?')) return;
  $('cfgmsg').textContent = 'vérification…';
  try {
    const r = await fetch('config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(changes)});
    const d = await r.json();
    $('cfgmsg').innerHTML = `<span class="${d.ok ? 'ok' : 'bad'}">${d.messages.map(esc).join('\n')}</span>`;
    if (d.ok) setTimeout(() => loadCfg().catch(() => {}), 20000);
  } catch (e) { $('cfgmsg').innerHTML = '<span class="bad">CMTS injoignable</span>'; }
});

function render(d) {
  last = d;
  const s = d.status || {}, info = d.info || {};
  $('sum').textContent = [info.sdr, info.ds && `${info.ds.length} DS ${info.qam || ''}`,
    info.us && `${info.us.length} US ${info.us_mode || ''} ${info.us_mod || ''}`,
    s.uptime_s !== undefined && `depuis ${dur(s.uptime_s)}`].filter(Boolean).join(' · ');
  $('age').textContent = s.time ? `mis à jour à ${new Date(s.time * 1000).toLocaleTimeString()}` : '';
  $('dn').textContent = f(s.rate && s.rate.down_mbps); $('up').textContent = f(s.rate && s.rate.up_mbps);
  const mods = s.modems || [];
  $('nmod').textContent = `${mods.filter(m => m.state === 'online').length} / ${mods.length}`;
  chart(d.history || []);
  tbl('radios', ['Radio', 'État', 'U', 'L', 'O', 'Marge TX', 'Écart RX'], (s.radios || []).map(r => [
    esc(r.name), r.alive ? '<span class="ok">en marche</span>' : '<span class="bad">arrêtée</span>',
    `<span class="${r.dU ? 'bad' : ''}">${r.U ?? '–'}</span>`, `<span class="${r.dL ? 'warn' : ''}">${r.L ?? '–'}</span>`,
    `<span class="${r.dO ? 'warn' : ''}">${r.O ?? '–'}</span>`,
    `<span class="${cls(r.margin_ms, 20, 5, false)}">${f(r.margin_ms)} ms</span>`, `${f(r.gap_ms, 0)} ms`]));
  const ds = info.ds || [], fill = (s.downstream || {}).fill || [];
  tbl('ds', ['DCID', 'Fréquence', 'Radio', 'Remplissage'], ds.map((c, k) => [
    c.dcid + (k === 0 ? ' <span class="muted">primaire</span>' : ''), `${mhz(c.freq)} MHz`, esc(c.radio ?? ''),
    fill[k] === null || fill[k] === undefined ? '–' : `<span class="${cls(fill[k], 90, 99)}">${f(fill[k], 0)} %</span>`]));
  const q = s.downstream || {}, b = s.bridge || {};
  $('dsq').innerHTML = `file DS ${q.queue ?? '–'} trames, attente max <span class="${cls(q.wait_max_ms, 50, 90)}">${f(q.wait_max_ms, 0)} ms</span>, ` +
    `jetées ${q.dropped ?? '–'} · file TAP ${b.tap_waiting ?? '–'} (jetées ${b.tap_dropped ?? '–'}, lot max ${b.max_batch ?? '–'})`;
  tbl('us', ['UCID', 'Fréquence', 'Bruit', 'Retard max', 'Rafales', 'Décodées'], (s.upstream || []).map(u => [
    u.ucid, `${mhz(u.freq)} MHz`, `<span class="${cls(u.noise_dbfs, -50, -40)}">${f(u.noise_dbfs)} dBFS</span>`,
    `<span class="${cls(u.lag_ms, 200, 500)}">${f(u.lag_ms, 0)} ms</span>`, u.bursts ?? '–', u.decoded ?? '–']));
  const head = ['MAC', 'SID', 'État', 'Erreur de temps', 'Puissance reçue', 'Fréquence', 'MER', 'Puissance émise', 'Canaux montants', ''];
  $('modems').innerHTML = '<tr>' + head.map(h => `<th>${h}</th>`).join('') + '</tr>' + (mods.length ? mods.map(m => {
    const row = [esc(m.mac), '0x' + Number(m.sid).toString(16).padStart(4, '0'),
      `<span class="${stateCls(m.state)}">${esc(m.state)}</span>`,
      m.err_us === undefined ? '–' : `${f(m.err_us, 2)} µs`, m.power === undefined ? '–' : `${f(m.power)} dBFS`,
      m.freq === undefined ? '–' : `${f(m.freq, 0)} Hz`,
      m.mer === undefined ? '–' : `<span class="${cls(m.mer, 24, 18, false)}">${f(m.mer)} dB</span>`,
      m.pr_dbmv === undefined ? '–' : `${f(m.pr_dbmv)} dBmV`,
      Object.entries(m.links || {}).map(([u, l]) => `US${u} <span class="${stateCls(l[1])}">${esc(l[1])}</span>`).join(', ') || '–',
      `<button type="button" data-mac="${esc(m.mac)}">${open.has(m.mac) ? 'masquer' : 'capacités'}</button>`];
    return '<tr>' + row.map(c => `<td>${c}</td>`).join('') + '</tr>' +
      (open.has(m.mac) ? `<tr class="details"><td colspan="${head.length}">${details(m)}</td></tr>` : '');
  }).join('') : `<tr><td colspan="${head.length}" class="muted">–</td></tr>`);
  const al = d.alerts || [];
  $('alerts').innerHTML = al.length ? al.slice().reverse().map(a =>
    `<span class="muted">${new Date(a.time * 1000).toLocaleTimeString()}</span> <span class="bad">${esc(a.line)}</span>`).join('\n') : 'aucune';
}

async function tick() {
  try { const r = await fetch('status.json', {cache: 'no-store'}); render(await r.json()); }
  catch (e) { $('age').innerHTML = '<span class="bad">CMTS injoignable</span>'; }
}
tick(); setInterval(tick, 5000);
</script>
</body></html>
"""
