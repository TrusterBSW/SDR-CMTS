# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""The bladeRF driver of the radio process (cmts/sdrdev.py BladeRFRadio) against a fake libbladeRF
that records every call: format by library version, TX metadata flags and timestamps block after
block, SC16 Q11 scaling both ways, device time from the free-running counters (TX and RX), the
bladeRF 1 refused for the upstream. No hardware needed (tools/bladerf_check.py for the real one)."""
import ctypes
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cmts import sdrdev  # noqa: E402

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"{'OK ' if ok else 'KO '} {name}" + (f" : {detail}" if detail else ""))


class Meta(ctypes.Structure):
    _fields_ = [("timestamp", ctypes.c_uint64), ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                ("actual_count", ctypes.c_uint), ("reserved", ctypes.c_uint8 * 32)]


class Version(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint16), ("minor", ctypes.c_uint16), ("patch", ctypes.c_uint16),
                ("describe", ctypes.c_char_p)]


class FakeLib:
    """The libbladeRF calls BladeRFRadio makes; counters: TX = tick, RX = tick - 1000."""
    Meta = Meta
    Version = Version

    def __init__(self, board=b"bladerf2", fpga=(0, 14, 0)):
        self.board, self.fpga = board, fpga
        self.rate, self.freq, self.gain = {}, {}, {}
        self.tx, self.cfg, self.enabled = [], {}, {}
        self.tick = 5_000_000
        self.rx_val = None

    def bladerf_open(self, pdev, ident):
        pdev._obj.value = 1234
        self.ident = ident
        return 0

    def bladerf_get_board_name(self, dev):
        return self.board

    def bladerf_get_serial(self, dev, buf):
        buf.value = b"0123456789abcdef"
        return 0

    def bladerf_set_sample_rate(self, dev, ch, rate, pact):
        self.rate[ch] = rate
        pact._obj.value = rate
        return 0

    def bladerf_get_sample_rate(self, dev, ch, pv):
        pv._obj.value = self.rate[ch]
        return 0

    def bladerf_set_bandwidth(self, dev, ch, bw, pact):
        pact._obj.value = bw
        return 0

    def bladerf_set_frequency(self, dev, ch, f):
        self.freq[ch] = f
        return 0

    def bladerf_get_frequency(self, dev, ch, pv):
        pv._obj.value = self.freq[ch]
        return 0

    def bladerf_set_gain_mode(self, dev, ch, mode):
        self.gain_mode = mode
        return 0

    def bladerf_set_gain(self, dev, ch, g):
        self.gain[ch] = g
        return 0

    def bladerf_get_gain(self, dev, ch, pv):
        pv._obj.value = self.gain[ch]
        return 0

    def bladerf_sync_config(self, dev, layout, fmt, nbuf, blen, nxfer, tmo):
        self.cfg[layout] = (fmt, nbuf, blen, nxfer)
        return 0

    def bladerf_enable_module(self, dev, ch, on):
        self.enabled[ch] = on
        return 0

    def bladerf_get_timestamp(self, dev, direction, pv):
        pv._obj.value = self.tick if direction == sdrdev.DIR_TX else self.tick - 1000
        return 0

    def bladerf_sync_tx(self, dev, ptr, n, pmd, tmo):
        md = pmd._obj
        v = np.ctypeslib.as_array((ctypes.c_int16 * (2 * n)).from_address(ptr)).copy()
        self.tx.append((md.flags, md.timestamp, n, v))
        return 0

    def bladerf_sync_rx(self, dev, ptr, n, pmd, tmo):
        md = pmd._obj
        arr = np.ctypeslib.as_array((ctypes.c_int16 * (2 * n)).from_address(ptr))
        arr[:] = self.rx_val
        md.actual_count = n
        md.timestamp = 7_000_000
        md.status = 0
        self.rx_flags = md.flags
        return 0

    def bladerf_strerror(self, ret):
        return b"erreur"

    def bladerf_fpga_version(self, dev, pv):
        pv._obj.major, pv._obj.minor, pv._obj.patch = self.fpga
        return 0


def params(rx_f=56.5e6):
    return dict(args="*:serial=0123", fs_tx=27.808e6, mcr=27.808e6, tx_center=302e6, tx_bw=24e6, gain=40,
                decim=1, rx_target=rx_f, rx_lo_off=0.0, rx_bw=20e6, rx_gain=20, clock_source="internal")


check("format META : 1 pour libbladeRF 2.4.1 (Debian 12), 2 à partir de 2.6 (format PACKED inséré)",
      sdrdev.bladerf_meta_format((2, 4, 1)) == 1 and sdrdev.bladerf_meta_format((2, 6, 0)) == 2)

fake = FakeLib()
sdrdev._BRF = (fake, (2, 4, 1))
r = sdrdev.BladeRFRadio(params(), True)
check("ouverture : identifiant, débits entiers, fréquences, gain RX manuel",
      fake.ident == b"*:serial=0123" and fake.rate == {sdrdev.CH_TX0: 27808000, sdrdev.CH_RX0: 27808000}
      and fake.freq[sdrdev.CH_TX0] == 302_000_000 and fake.freq[sdrdev.CH_RX0] == 56_500_000
      and fake.gain_mode == sdrdev.GAIN_MGC and r.check() is None, r.describe())
r.open_streams()
check("flux TX et RX en SC16_Q11_META (1 ici), modules activés",
      fake.cfg[sdrdev.TX_X1][0] == 1 and fake.cfg[sdrdev.RX_X1][0] == 1
      and fake.enabled == {sdrdev.CH_TX0: True, sdrdev.CH_RX0: True})
check("compteurs TX - RX lus une fois les deux modules actifs", r.rx_to_tx == 1000, str(r.rx_to_tx))

r.set_time(0.0)                                   # device time 0 = TX counter 5,000,000
check("heure de l'appareil : 0 au réglage", abs(r.time_now()) < 1e-9)
fake.tick += 27_808_000
check("heure de l'appareil : +1 s quand le compteur avance d'une seconde", abs(r.time_now() - 1.0) < 1e-9)

x = np.full(1000, 0.5 + 0.25j, np.complex64)
t0 = 13_904_000                                   # tick of the first block (0.5 s)
r.send(x, t0, True)
r.send(x, t0 + 1000, False)
r.send(x, None, False)
f0, f1, f2 = fake.tx[0], fake.tx[1], fake.tx[2]
check("1er bloc : début de salve à l'horodatage du compteur",
      f0[0] == sdrdev.META_TX_BURST_START and f0[1] == t0 + 5_000_000, f"{f0[:3]}")
check("blocs suivants : mise à jour d'horodatage, contigus",
      f1[0] == sdrdev.META_TX_UPDATE_TIMESTAMP and f1[1] == t0 + 1000 + 5_000_000)
check("bloc sans heure (--tx-untimed) : aucun drapeau", f2[0] == 0)
check("Q11 : 0,5 -> 1023, 0,25 -> 511 (I puis Q)", f0[3][0] == 1023 and f0[3][1] == 511, str(f0[3][:4]))
r.end_burst()
check("fin : fin de salve puis TX coupé", fake.tx[-1][0] == sdrdev.META_TX_BURST_END
      and fake.enabled[sdrdev.CH_TX0] is False)

fake.rx_val = np.tile(np.array([1024, -512], np.int16), 2048)
buf = np.zeros((1, 2048), np.complex64)
n, tick, ovf = r.recv(buf)
check("réception : maintenant (RX_NOW), Q11 -> ±1", fake.rx_flags == sdrdev.META_RX_NOW
      and n == 2048 and np.allclose(buf[0, 0], 0.5 - 0.25j), str(buf[0, 0]))
check("réception : tick = compteur RX + (TX - RX) + base", tick == 7_000_000 + 1000 - 5_000_000, str(tick))

sdrdev._BRF = (FakeLib(), (2, 6, 0))
r2 = sdrdev.BladeRFRadio(params(), False)
r2.open_streams()
check("libbladeRF 2.6 : format 2, émission seule sans flux RX",
      sdrdev._BRF[0].cfg == {sdrdev.TX_X1: (2, 64, 16384, 32)} and r2.describe().endswith("horloge internal")
      and "émission seule" in r2.describe())

sdrdev._BRF = (FakeLib(b"bladerf1"), (2, 4, 1))
try:
    sdrdev.BladeRFRadio(params(), True)
    check("bladeRF 1 et montant à 56,5 MHz : refusé (XB-200)", False)
except RuntimeError as e:
    check("bladeRF 1 et montant à 56,5 MHz : refusé (XB-200)", "XB-200" in str(e), str(e)[:70])
sdrdev._BRF = (FakeLib(fpga=(0, 16, 0)), (2, 4, 1))
try:
    sdrdev.BladeRFRadio(params(), True)
    check("FPGA 0.16 avec libbladeRF 2.4.1 : refusé (métadonnées mal lues)", False)
except RuntimeError as e:
    check("FPGA 0.16 avec libbladeRF 2.4.1 : refusé (métadonnées mal lues)", "hostedxA4.rbf" in str(e), str(e)[:60])
check("FPGA 0.16 avec libbladeRF 2.6 : accepté", sdrdev.fpga_mismatch((2, 6, 0), (0, 16, 0)) is None)
sdrdev._BRF = None
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
