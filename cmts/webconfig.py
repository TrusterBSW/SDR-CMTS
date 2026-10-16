"""The common options of the CMTS, changed from the status page (cmts/webstatus.py, "Configuration").

The page shows a form with the options people change most (radio, downstream and upstream
channels, modulations, frequencies, levels); a change is checked twice, against the limits below
(the bands of EuroDOCSIS, what one radio carries) and by the option parser itself, then written to
the bench file the CMTS was started with (--config, comments kept, the old file as .bak), and the
CMTS restarts on it (a stop as Ctrl+C, then the same command again: the modems re-register in about
a minute). Without --config there is no file to write: the form is shown read-only.

Who may change it (--status-edit): auto = requests from this machine only (the browser on it, or an
SSH tunnel: ssh -L 8090:127.0.0.1:8090 host), the network reads; on = from anywhere the page is
reachable (anyone there could make the radio transmit elsewhere); off = nobody.
"""
import os

# key, label, section of the bench file, kind, details
#   kind "mhz": a frequency in Hz shown in MHz; "int"/"float": plain; "choice": choices [(value, text)]
FIELDS = [
    ("sdr", "Radio", "radio", "choice", dict(choices=[("b210", "USRP B210"), ("bladerf", "bladeRF 2.0 micro")])),
    ("ds_radios", "Nombre de radios (B210)", "radio", "int", dict(min=1, max=4)),
    ("gain", "Gain d'émission (dB)", "radio", "float", dict(min=0, max=89.75, empty="automatique")),
    ("us_gain", "Gain de réception (dB)", "radio", "float", dict(min=0, max=76)),
    ("ds_channels", "Canaux descendants", "downstream", "int", dict(min=1, max=24)),
    ("freq", "Fréquence du canal principal (MHz)", "downstream", "mhz", dict(min=112e6, max=858e6)),
    ("ds_spacing", "Écart entre canaux descendants (MHz)", "downstream", "mhz", dict(min=8e6, max=64e6)),
    ("ds_layout", "Disposition", "downstream", "choice",
     dict(choices=[("around", "autour du principal"), ("up", "au-dessus du principal")])),
    ("qam", "Modulation descendante", "downstream", "choice", dict(choices=[(64, "64-QAM"), (256, "256-QAM")])),
    ("us_channels", "Canaux montants", "upstream", "int", dict(min=1, max=4)),
    ("us_freq", "Fréquence du 1er canal montant (MHz)", "upstream", "mhz", dict(min=5e6, max=65e6)),
    ("us_spacing", "Écart entre canaux montants (MHz)", "upstream", "mhz", dict(min=-20e6, max=20e6)),
    ("us_mode", "Type de canal montant", "upstream", "choice",
     dict(choices=[("atdma", "ATDMA (DOCSIS 2.0)"), ("tdma", "TDMA (DOCSIS 1.x)")])),
    ("us_mod", "Modulation montante (données)", "upstream", "choice",
     dict(choices=[("64qam", "64-QAM"), ("16qam", "16-QAM"), ("qpsk", "QPSK")])),
    ("us_rate", "Débit symbole montant", "upstream", "choice",
     dict(choices=[(32, "5,12 Msym/s"), (16, "2,56 Msym/s"), (8, "1,28 Msym/s")])),
    ("us_target", "Niveau visé des rafales (dBFS)", "upstream", "float", dict(min=-35, max=-6)),
]
SECTIONS = {k: sec for k, _, sec, _, _ in FIELDS}
DS_BAND = (108e6, 862e6)            # EuroDOCSIS downstream
US_BAND = (5e6, 65e6)               # EuroDOCSIS upstream
DS_PER_RADIO = {"b210": 6, "bladerf": 3}


class WebConfig:
    """parse(argv) -> namespace or raises; channels(ns) -> ([DS Hz], [US Hz]); to_argv(path) -> argv
    of a bench file (the TOML of --config); restart(): asks the CMTS to restart."""

    def __init__(self, args, path, locked, parse, channels, to_argv, restart, edit="auto", why=""):
        self.args, self.path, self.locked = args, path, set(locked)
        self.parse, self.channels, self.to_argv, self.restart = parse, channels, to_argv, restart
        self.edit = edit
        self.why = why or ("" if path else "lancé sans --config : aucun fichier de banc à modifier")

    def allowed(self, local=True):
        """(may this client change the bench, why not)."""
        if not self.path or self.why:
            return False, self.why or "modification refusée"
        if self.edit == "off":
            return False, "modification coupée (--status-edit off)"
        if self.edit == "auto" and not local:
            return False, ("modification seulement depuis cette machine (ou un tunnel SSH : "
                           "ssh -L 8090:127.0.0.1:8090 <hôte>), ou avec --status-edit on")
        return True, ""

    def describe(self, local=True):
        out = []
        for key, label, sec, kind, det in FIELDS:
            v = getattr(self.args, key, None)
            out.append(dict(key=key, label=label, section=sec, kind=kind, value=v,
                            locked=key in self.locked, **{k: x for k, x in det.items()}))
        ok, why = self.allowed(local)
        return dict(fields=out, editable=ok, why=why, path=self.path,
                    locked_note="donnée sur la ligne de commande : elle l'emporte sur le fichier" if self.locked else "")

    def _convert(self, key, raw):
        kind, det = next((k, d) for kk, _, _, k, d in FIELDS if kk == key)
        if raw in ("", None) and det.get("empty"):
            return None
        if kind == "choice":
            vals = [c for c, _ in det["choices"]]
            for c in vals:
                if str(c) == str(raw):
                    return c
            raise ValueError(f"{key} : « {raw} » n'est pas un choix possible")
        x = float(raw) * (1e6 if kind == "mhz" else 1)
        if kind == "int":
            if float(raw) != int(float(raw)):
                raise ValueError(f"{key} : un nombre entier")
            x = int(float(raw))
        lo, hi = det.get("min"), det.get("max")
        if lo is not None and x < lo or hi is not None and x > hi:
            scale = 1e6 if kind == "mhz" else 1
            raise ValueError(f"{key} : entre {lo / scale:g} et {hi / scale:g}")
        return x

    def check(self, changes):
        """{key: text from the form} -> ({key: value}, [errors]); the values checked together, as
        the CMTS would run with them."""
        vals, errors = {}, []
        for k, raw in changes.items():
            if k not in SECTIONS:
                errors.append(f"{k} : option non modifiable ici")
                continue
            if k in self.locked:
                errors.append(f"{k} : donnée sur la ligne de commande, elle l'emporterait sur le fichier")
                continue
            try:
                vals[k] = self._convert(k, raw)
            except (ValueError, TypeError) as e:
                errors.append(str(e) if isinstance(e, ValueError) else f"{k} : valeur illisible")
        if errors:
            return vals, errors
        ns = dict(vars(self.args))
        ns.update({k: v for k, v in vals.items() if v is not None})
        sdr = ns.get("sdr", "b210")
        radios = int(ns.get("ds_radios") or 1)
        if ns["ds_channels"] > DS_PER_RADIO.get(sdr, 6) * radios:
            errors.append(f"{ns['ds_channels']} canaux descendants : {DS_PER_RADIO.get(sdr, 6)} au plus par "
                          f"radio ({sdr}, {radios} radio{'s' if radios > 1 else ''})")
        if sdr == "bladerf" and (ns["us_channels"] > 1 or radios > 1):
            errors.append("bladeRF : un seul canal montant et une seule radio (docs/BLADERF.md)")
        if ns["us_mod"] == "64qam" and ns["us_mode"] != "atdma":
            errors.append("64-QAM montant : en ATDMA seulement")
        if ns["us_mode"] == "tdma" and ns["us_rate"] > 16:
            errors.append("TDMA : 2,56 Msym/s au plus")
        if ns["us_channels"] > 1 and abs(ns["us_spacing"]) < ns["us_rate"] * 160e3 * 1.25:
            errors.append("canaux montants qui se chevauchent : écart trop petit pour ce débit symbole")
        try:
            from types import SimpleNamespace
            ds, us = self.channels(SimpleNamespace(**ns))
            half_us = ns["us_rate"] * 160e3 * 1.25 / 2
            bad_ds = [f / 1e6 for f in ds if not DS_BAND[0] + 4e6 <= f <= DS_BAND[1] - 4e6]
            bad_us = [f / 1e6 for f in us if not US_BAND[0] + half_us <= f <= US_BAND[1] - half_us]
            if bad_ds:
                errors.append(f"canaux descendants hors de la bande 108-862 MHz : {', '.join(f'{f:g}' for f in bad_ds)} MHz")
            if bad_us:
                errors.append(f"canaux montants hors de la bande 5-65 MHz : {', '.join(f'{f:g}' for f in bad_us)} MHz")
        except Exception as e:                       # noqa: BLE001
            errors.append(f"plan de fréquences : {e}")
        return vals, errors

    def apply(self, changes, local=True):
        """Checks, writes the bench file, restarts the CMTS -> (ok, [messages])."""
        ok, why = self.allowed(local)
        if not ok:
            return False, [why]
        vals, errors = self.check(changes)
        if errors:
            return False, errors
        vals = {k: v for k, v in vals.items() if v != getattr(self.args, k, None)}
        if not vals:
            return False, ["rien n'a changé"]
        from . import tomlconf
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as d:     # the whole new file through the parser first
            tmp = os.path.join(d, "banc.toml")
            shutil.copy(self.path, tmp)
            unset = [k for k, v in vals.items() if v is None]
            try:
                tomlconf.update(tmp, {k: v for k, v in vals.items() if v is not None}, SECTIONS)
                if unset:
                    _remove_keys(tmp, unset)
                self.parse(self.to_argv(tmp))
            except (ValueError, SystemExit, OSError) as e:
                return False, [f"fichier refusé : {e}"]
            shutil.copy(self.path, self.path + ".bak")
            shutil.copy(tmp, self.path)
        self.restart(", ".join(f"{k}={v}" for k, v in vals.items()))
        return True, [f"{self.path} enregistré (ancien : {os.path.basename(self.path)}.bak) ; redémarrage du CMTS"]


def _remove_keys(path, keys):
    import re
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    keep = [ln for ln in lines if not (m := re.match(r"\s*([A-Za-z0-9_-]+)\s*=", ln))
            or m.group(1).replace("-", "_") not in keys]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(keep))
