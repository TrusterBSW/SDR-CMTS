# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""Several B210s (sdr_cmts --ds-radios 2): the bonded channels split into blocks of adjacent
frequencies, one per device; the RCC of 12 channels over the receive modules of a 16-channel
profile (MULPIv3.0 C.1.5.3); 6 channels keep the former single-module RCC."""
import os
import struct
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sdr_cmts  # noqa: E402
from cmts import cmcfg, registration  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


T = cmcfg.tlv


def rcp(rcp_id, nmods, nadj, nrc, per_module=8, primary_rcs=(1,)):
    """A Receive Channel Profile: nmods modules of nadj adjacent channels, nrc receive channels,
    channel i connected to module (i - 1) // per_module + 1."""
    body = T(1, rcp_id) + T(2, b"TEST") + T(3, b"\x02")
    for m in range(1, nmods + 1):
        rng = T(1, struct.pack(">I", 112_000_000)) + T(2, struct.pack(">I", 858_000_000))
        body += T(4, T(1, bytes([m])) + T(2, bytes([nadj])) + T(3, rng))
    for i in range(1, nrc + 1):
        m = (i - 1) // per_module + 1
        sub = T(1, bytes([i])) + T(2, registration._bits(m, 1))
        if i in primary_rcs:
            sub += T(5, b"\x01")
        body += T(5, sub)
    return 48, body


# ---- channel split between the devices
a = types.SimpleNamespace(ds_channels=12, ds_radios=2, freq=306e6, ds_spacing=8e6, ds_layout="around")
chans = sdr_cmts.ds_channels(a)
groups = sdr_cmts.radio_groups(chans, 2)
f = [[chans[i][1] / 1e6 for i in g] for g in groups]
check("12 canaux 'around' : primaire à 306 MHz", chans[0] == (1, 306e6), str(chans[:2]))
check("2 B210 : deux blocs de 6 canaux adjacents", [len(x) for x in f] == [6, 6]
      and max(f[0]) < min(f[1]) and max(f[0]) - min(f[0]) == 40 and max(f[1]) - min(f[1]) == 40, str(f))
check("6 canaux par B210 au plus", sdr_cmts.radio_channels(a) == 6)
a1 = types.SimpleNamespace(ds_channels=6, ds_radios=1)
check("1 B210 : inchangé", sdr_cmts.radio_groups(sdr_cmts.ds_channels(types.SimpleNamespace(
    ds_channels=6, freq=306e6, ds_spacing=8e6, ds_layout="around")), 1) == [[5, 1, 2, 0, 3, 4]]
    or len(sdr_cmts.radio_groups(sdr_cmts.ds_channels(types.SimpleNamespace(
        ds_channels=6, freq=306e6, ds_spacing=8e6, ds_layout="around")), 1)[0]) == 6)

# ---- RCC
tlvs = [rcp(b"\x00\x10\x00\x10\x08", 1, 8, 8), rcp(b"\x00\x10\x00\x10\x16", 2, 8, 16)]
rcc, rid = registration.receive_channel_config(tlvs, chans)
check("12 canaux : profil 16 canaux choisi", rid == b"\x00\x10\x00\x10\x16", rid and rid.hex())
body = cmcfg.parse(cmcfg.parse(rcc)[0][1]) if rcc else []
mods = [dict(cmcfg.parse(v)) for k, v in body if k == 4]
rcs = [dict(cmcfg.parse(v)) for k, v in body if k == 5]
firsts = sorted(struct.unpack(">I", m[4])[0] / 1e6 for m in mods)
check("deux modules, chacun sur un bloc de 8 canaux au plus", len(mods) == 2 and firsts == [266.0, 330.0], str(firsts))
check("12 canaux de réception, chacun relié à un seul module", len(rcs) == 12
      and all(len(registration._bits_set(r[2])) == 1 for r in rcs))
by_mod = {}
for r in rcs:
    m = min(registration._bits_set(r[2]))
    by_mod.setdefault(m, []).append(struct.unpack(">I", r[4])[0])
spans = {m: (max(v) - min(v)) / 8e6 + 1 for m, v in by_mod.items()}
check("chaque module tient ses canaux dans 8 positions", all(s <= 8 for s in spans.values()), str(spans))
prim = [r for r in rcs if 5 in r]
check("le primaire (306 MHz) sur le canal de réception capable d'être primaire",
      len(prim) == 1 and struct.unpack(">I", prim[0][4])[0] == 306_000_000 and prim[0][1] == b"\x01",
      str([(p[1].hex(), struct.unpack('>I', p[4])[0]) for p in prim]))
rcc6, rid6 = registration.receive_channel_config(tlvs, chans[:6])
check("6 canaux : le RCC d'avant (un module, profil 8 canaux)", rid6 == b"\x00\x10\x00\x10\x08"
      and len([1 for k, v in cmcfg.parse(cmcfg.parse(rcc6)[0][1]) if k == 4]) == 1)
check("profil trop petit : pas de RCC", registration.receive_channel_config([tlvs[0]], chans) == (b"", None))
print("\n".join("   " + l for l in registration.describe_rcps(tlvs)))

# ---- a follower without a common 10 MHz: realigned on the primary by groups of 8 TS packets
import numpy as np  # noqa: E402
from cmts import radio  # noqa: E402
from cmts.j83a import J83AEncoder  # noqa: E402

rng = np.random.default_rng(5)
fs, n_nom, g = 27.808e6, 48 * 204 * 4, 8 * 204 * 4          # 256-QAM, 4 samples per symbol
EPS_P, EPS_F, OFF_F = 1.5e-6, -3.5e-6, 180e-6               # device clocks vs the host, follower offset


def dev(h, eps, off):
    return off + h * (1 + eps)


fp, ff = radio.ClockFit(), radio.ClockFit()
for i in range(60):                                          # one reading per second, 0-60 us of USB jitter
    h = 10.0 + i
    fp.add(h + rng.uniform(0, 60e-6), dev(h, EPS_P, 0.0))
    ff.add(h + rng.uniform(0, 60e-6), dev(h, EPS_F, OFF_F))
check("ClockFit : pente de chaque horloge à 0,1 ppm près",
      abs(fp.model[2] - 1 - EPS_P) < 1e-7 and abs(ff.model[2] - 1 - EPS_F) < 1e-7,
      f"{(fp.model[2] - 1) * 1e6:+.2f} / {(ff.model[2] - 1) * 1e6:+.2f} ppm")
t_p = 75.0
want = radio.follower_time(fp.model, ff.model, t_p)
h_true = t_p / (1 + EPS_P)
check("follower_time : la même heure de l'hôte à 50 µs près",
      abs(want - dev(h_true, EPS_F, OFF_F)) < 50e-6, f"{(want - dev(h_true, EPS_F, OFF_F)) * 1e6:+.1f} µs")

# one hour of blocks: the follower's schedule against the true time it should use
adj, worst, ins, drop = 0, 0.0, 0, 0
blk_s = n_nom / fs
fpm, ffm = (0.0, 0.0, 1 + EPS_P), (0.0, OFF_F, 1 + EPS_F)
for seq in range(0, int(3600 / blk_s), 7):                   # (one block in 7: the drift is slow)
    t_air = 0.5 + seq * n_nom / fs
    err = radio.follower_time(fpm, ffm, t_air) - (0.5 + (seq * n_nom + adj) / fs)
    worst = max(worst, abs(err)) if seq > 2000 else worst
    a = radio.group_action(err, g / fs)
    adj += a * g
    ins, drop = ins + (a > 0), drop + (a < 0)
check("une heure à 5 ppm d'écart : décalage tenu sous 3/4 de groupe (~176 µs)",
      worst <= 0.75 * g / fs + 1e-6, f"pire {worst * 1e6:.0f} µs, groupes +{ins} / -{drop}")
check("la suiveuse lente retire des groupes, sans en ajouter une fois calée", drop > 50 and ins <= 1,
      f"+{ins} / -{drop}")

pk = np.tile(radio.NULL_TS, (2, 48, 1)).copy()
pk[:, :, 1:3] = (0x1F, 0xFE)                                 # DOCSIS packets everywhere ...
pk[:, 24:32, 1:3] = (0x1F, 0xFF)                             # ... but group 3 is null
less = radio.regroup(pk, -1)
more = radio.regroup(pk, +1)
check("retrait : le groupe de 8 paquets nuls, pas un groupe de données",
      less.shape == (2, 40, 188) and not np.any(((less[:, :, 1] & 0x1F) == 0x1F) & (less[:, :, 2] == 0xFF)))
check("ajout : 8 paquets nuls devant le bloc", more.shape == (2, 56, 188)
      and np.all(more[:, :8, 2] == 0xFF) and np.array_equal(more[:, 8:], pk))
enc = J83AEncoder(256)
ok_enc = all(enc.encode(b[0]).size == b.shape[1] * 204 for b in (less, more, pk))
check("J.83-A : 40, 48 et 56 paquets s'encodent d'affilée (multiples de 8)", ok_enc)
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
