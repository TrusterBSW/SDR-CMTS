#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 BILLY Rémi - see LICENSE and COMMERCIAL.md
"""bladeRF check before running the CMTS on it (mini_cmts.py --sdr bladerf): through the same
code as the radio process (cmts/sdrdev.py BladeRFRadio, libbladeRF called directly).

  tools/bladerf_check.py [--id "*:serial=..."] [--seconds 10] [--loop]

  1. the device: board, serial, libbladeRF / firmware / FPGA versions, USB speed, gain ranges;
  2. the CMTS settings (27.808 MS/s, TX 302 MHz, RX 70 MHz, its lowest): rates obtained, TX/RX counters;
  3. TX and RX streaming together for --seconds at that rate, TX timed block by block as the
     radio process sends: underruns, overruns, RX timestamp continuity;
  4. --loop (TX1 and RX1 on the same coax, e.g. both on the coupler, CMTS stopped): RX tuned to
     the TX frequency, a timed burst sent, its arrival found in the RX stream -> TX-to-RX
     delay in samples (constant; the modem's ranging takes it up, it only has to be stable).
"""
import argparse
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from cmts import sdrdev  # noqa: E402


def device_info(ident):
    import ctypes as c
    lib, ver = sdrdev._lib()
    print(f"libbladeRF {'.'.join(map(str, ver))} : format SC16_Q11_META = {sdrdev.bladerf_meta_format(ver)}")
    V = lib.Version                                       # (the structure the driver's prototypes use)
    lib.bladerf_fw_version.argtypes, lib.bladerf_fw_version.restype = [c.c_void_p, c.POINTER(V)], c.c_int
    lib.bladerf_is_fpga_configured.argtypes, lib.bladerf_is_fpga_configured.restype = [c.c_void_p], c.c_int
    has_speed = hasattr(lib, "bladerf_device_speed")
    if has_speed:
        lib.bladerf_device_speed.argtypes, lib.bladerf_device_speed.restype = [c.c_void_p], c.c_int
    dev = c.c_void_p()
    ret = lib.bladerf_open(c.byref(dev), ident.encode() if ident else None)
    if ret < 0:
        sys.exit(f"ouverture impossible : {lib.bladerf_strerror(ret).decode()} (bladeRF branché ? droits USB ?)")
    board = lib.bladerf_get_board_name(dev).decode()
    sn = c.create_string_buffer(64)
    lib.bladerf_get_serial(dev, sn)
    fw, fpga = V(), V()
    lib.bladerf_fw_version(dev, c.byref(fw))
    fpga_ok = lib.bladerf_is_fpga_configured(dev)
    if fpga_ok > 0:
        lib.bladerf_fpga_version(dev, c.byref(fpga))
    speed = {0: "inconnue", 1: "USB 2 (High Speed) : trop lent pour le CMTS", 2: "USB 3 (SuperSpeed)"}.get(
        lib.bladerf_device_speed(dev), "?") if has_speed else "?"
    print(f"carte {board}, série {sn.value.decode()}, firmware {fw.major}.{fw.minor}.{fw.patch}, FPGA "
          + (f"{fpga.major}.{fpga.minor}.{fpga.patch}" if fpga_ok > 0 else "NON CHARGÉ (bladeRF-cli -l <fichier .rbf>)")
          + f", liaison {speed}")
    for name, ch in (("TX1", sdrdev.CH_TX0), ("RX1", sdrdev.CH_RX0)):
        rp = c.POINTER(lib.Range)()
        if lib.bladerf_get_gain_range(dev, ch, c.byref(rp)) == 0:
            r = rp.contents
            print(f"   gain {name} : {r.min * r.scale:.2f} à {r.max * r.scale:.2f} dB (pas {r.step * r.scale:g})")
    lib.bladerf_close(dev)
    if board == "bladerf1":
        print("!! bladeRF 1 (LMS6002D) : 300 MHz - 3,8 GHz, le montant (53-60 MHz) demande le transverter XB-200")
    return board


def params(fs, tx_f, rx_f, gain, rx_gain):
    return dict(args="", fs_tx=fs, mcr=fs, tx_center=tx_f, tx_bw=24e6, gain=gain, decim=1, rx_target=rx_f,
                rx_lo_off=0.0, rx_bw=20e6, rx_gain=rx_gain, clock_source="internal", driver="bladerf")


def stream_test(ident, seconds, gain, rx_gain, fs=27.808e6):
    p = params(fs, 302e6, sdrdev.BLADERF2_RX_MIN_HZ, gain, rx_gain)     # (RX from 70 MHz on the bladeRF 2.0)
    p["args"] = ident
    r = sdrdev.BladeRFRadio(p, True)
    print(r.describe())
    err = r.check()
    if err:
        sys.exit(f"!! {err}")
    r.open_streams()
    print(r.note)
    r.set_time(0.0)
    t0 = r.time_now()
    n = int(round(78336 // 2 * fs / 27.808e6))       # one block of the CMTS (48 TS packets)
    rng = np.random.default_rng(1)
    blk = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.2).astype(np.complex64)
    stop = threading.Event()
    rx_stats = dict(n=0, gaps=0, overflow=0, last=None)

    def rx_loop():
        buf = np.zeros((1, 16384), np.complex64)
        while not stop.is_set():
            k, tick, ovf = r.recv(buf)
            if ovf:
                rx_stats["overflow"] += 1
            if k:
                if rx_stats["last"] is not None and tick != rx_stats["last"]:
                    rx_stats["gaps"] += 1
                rx_stats["last"] = tick + k
                rx_stats["n"] += k
    th = threading.Thread(target=rx_loop, daemon=True)
    th.start()
    tick0 = int(round((t0 + 0.2) * fs))
    k, t_end = 0, time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        r.send(blk, tick0 + k * n, k == 0)
        k += 1
    r.end_burst()
    stop.set()
    th.join(2)
    u = sum(1 for e in r._events if e == "U")
    print(f"{seconds:.0f} s à {fs / 1e6:.3f} MS/s : TX {k} blocs, underruns {u} | RX {rx_stats['n'] / seconds / 1e6:.3f} MS/s, "
          f"overflows {rx_stats['overflow']}, ruptures d'horodatage {rx_stats['gaps']}")
    r.stop_rx()
    r.close()
    return r


def loop_test(ident, gain, rx_gain):
    fs = 27.808e6
    p = params(fs, 302e6, 302e6 + 1e6, gain, rx_gain)    # RX 1 MHz off the TX LO (its leak)
    p["args"] = ident
    r = sdrdev.BladeRFRadio(p, True)
    r.open_streams()
    r.set_time(0.0)
    t = np.arange(40000) / fs
    burst = (0.5 * np.exp(2j * np.pi * -3e6 * t)).astype(np.complex64)    # a tone at 299 MHz
    quiet = np.zeros(200000, np.complex64)
    got = []
    stop = threading.Event()

    def rx_loop():
        buf = np.zeros((1, 16384), np.complex64)
        while not stop.is_set():
            k, tick, ovf = r.recv(buf)
            if k:
                got.append((tick, buf[0, :k].copy()))
    th = threading.Thread(target=rx_loop, daemon=True)
    th.start()
    time.sleep(0.3)
    start = int(round((r.time_now() + 0.2) * fs))
    r.send(quiet, start, True)
    r.send(burst, start + quiet.size, False)
    r.send(quiet, start + quiet.size + burst.size, False)
    time.sleep(2.5)                     # (long: an offset between the TX and RX clocks shows whole)
    r.end_burst()
    stop.set()
    th.join(2)
    r.stop_rx()
    r.close()
    ticks = np.concatenate([np.arange(tk, tk + x.size) for tk, x in got])
    x = np.concatenate([x for _, x in got])
    pw = np.abs(x) ** 2
    thr = np.median(pw) * 100 + 1e-12
    hot = np.nonzero(pw > thr)[0]
    if not hot.size:
        print("boucle : salve non vue (TX1 et RX1 reliés ? gains ?)")
        return
    sent = start + quiet.size
    print(f"boucle : salve émise au tick {sent}, reçue au tick {ticks[hot[0]]} -> retard TX->RX "
          f"{ticks[hot[0]] - sent} échantillons ({(ticks[hot[0]] - sent) / fs * 1e6:.2f} µs), "
          f"niveau {10 * np.log10(np.mean(pw[hot])):.1f} dBFS sur un bruit de {10 * np.log10(np.median(pw)):.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", default="", help='identifiant libbladeRF ("" = le premier, "*:serial=...")')
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--rate", type=float, default=27.808e6, help="débit TX et RX (55.616e6 : 6 DS + 2 US)")
    ap.add_argument("--gain", type=float, default=30.0, help="gain TX (dB, échelle libbladeRF)")
    ap.add_argument("--rx-gain", type=float, default=20.0)
    ap.add_argument("--loop", action="store_true", help="mesure du retard TX->RX (TX1 et RX1 sur le même coax)")
    a = ap.parse_args()
    device_info(a.id)
    stream_test(a.id, a.seconds, a.gain, a.rx_gain, a.rate)
    if a.loop:
        loop_test(a.id, a.gain, a.rx_gain)


if __name__ == "__main__":
    main()
